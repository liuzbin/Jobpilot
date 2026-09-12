/**
 * JobPilot 插件后台 service worker：负责跟本地 App 的连接状态机。
 *
 * 状态机： disconnected(本地App未检测到) -> app_detected_unpaired(检测到,未配对)
 *          -> connecting -> connected；websocket 异常断开则回到 disconnected 重试;
 *          服务端因 token 不对拒绝握手(4401)则进入 pairing_rejected 并清掉本地存的坏 token。
 *
 * MV3 的 service worker 会在空闲一段时间后被回收，纯 JS 定时器(setTimeout/setInterval)
 * 在 SW 被回收后不会再触发。这里的应对方式：
 * - 尚未建立 WebSocket 连接时的周期性健康检查，用 chrome.alarms 实现（alarms 能在 SW
 *   被回收后重新把它唤醒），而不是用 setInterval。
 * - 一旦 WebSocket 建立成功，Chrome 会因为存在"进行中的网络连接"而不回收这个 SW，
 *   这段时间内用 setInterval 做 ping/pong 是安全的。
 */

const API_BASE = "http://127.0.0.1:8756";
const WS_URL = "ws://127.0.0.1:8756/ws/heartbeat";
const HEALTH_POLL_ALARM = "jobpilot-health-poll";
const PING_INTERVAL_MS = 10000;
const PONG_TIMEOUT_MS = 15000;

let ws = null;
let pingTimer = null;
let pongTimeoutTimer = null;

const state = {
  status: "disconnected", // disconnected | app_detected_unpaired | connecting | connected | pairing_rejected
  lastError: null,
  lastPongAt: null,
};

function setState(patch) {
  Object.assign(state, patch);
  chrome.storage.local.set({ jobpilot_connection_state: state });
}

async function getToken() {
  const { jobpilot_pairing_token: token } = await chrome.storage.local.get("jobpilot_pairing_token");
  return token || null;
}

function clearPingTimers() {
  if (pingTimer) clearInterval(pingTimer);
  if (pongTimeoutTimer) clearTimeout(pongTimeoutTimer);
  pingTimer = null;
  pongTimeoutTimer = null;
}

async function checkHealthAndMaybeConnect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return; // 已经连着或正在连,不重复发起
  }
  try {
    const resp = await fetch(`${API_BASE}/api/health`, { method: "GET" });
    if (!resp.ok) throw new Error(`health status ${resp.status}`);
    const token = await getToken();
    if (!token) {
      setState({ status: "app_detected_unpaired", lastError: null });
      return;
    }
    connectWebSocket(token);
  } catch (e) {
    setState({ status: "disconnected", lastError: "本地 App 未检测到，请确认已启动" });
  }
}

function connectWebSocket(token) {
  setState({ status: "connecting" });
  try {
    ws = new WebSocket(`${WS_URL}?token=${encodeURIComponent(token)}`);
  } catch (e) {
    setState({ status: "disconnected", lastError: String(e) });
    return;
  }

  ws.onopen = () => {
    setState({ status: "connected", lastError: null });
    clearPingTimers();
    pingTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send("ping");
        pongTimeoutTimer = setTimeout(() => {
          // 超时没收到 pong,认为连接已经失效,主动断开触发下一轮重连
          if (ws) ws.close();
        }, PONG_TIMEOUT_MS);
      }
    }, PING_INTERVAL_MS);
  };

  ws.onmessage = (event) => {
    if (typeof event.data === "string" && event.data.startsWith("pong:")) {
      if (pongTimeoutTimer) clearTimeout(pongTimeoutTimer);
      setState({ lastPongAt: event.data.slice(5) });
    }
  };

  ws.onclose = (event) => {
    clearPingTimers();
    ws = null;
    if (event.code === 4401) {
      setState({ status: "pairing_rejected", lastError: "配对码无效，请重新配对" });
      chrome.storage.local.remove("jobpilot_pairing_token");
    } else if (event.code === 4403) {
      setState({ status: "pairing_rejected", lastError: "来源校验失败，请联系开发者" });
    } else {
      setState({ status: "disconnected", lastError: `连接断开 (code=${event.code})` });
    }
  };

  ws.onerror = () => {
    // 具体处理交给 onclose,这里不重复设置状态
  };
}

function ensureHealthPollAlarm() {
  // 防御性 try/catch：万一权限没声明对、或者某个 Chrome 版本行为有出入,
  // chrome.alarms 调用抛异常也不应该把整个后台脚本"炸掉"，导致下面注册
  // onMessage 监听器的代码都跑不到（之前踩过这个坑：忘记在 manifest.json
  // 里声明 "alarms" 权限，chrome.alarms 是 undefined,一调用就抛异常,
  // 后面所有 addListener 全部没注册上，侧边栏表现为永远"检测中"）。
  try {
    chrome.alarms.get(HEALTH_POLL_ALARM, (existing) => {
      if (!existing) {
        chrome.alarms.create(HEALTH_POLL_ALARM, { periodInMinutes: 1, delayInMinutes: 0.01 });
      }
    });
  } catch (e) {
    console.error("JobPilot: chrome.alarms 不可用,请确认 manifest.json 里声明了 alarms 权限", e);
  }
}

/**
 * Phase 3：把内容脚本（LinkedIn 页面）抓到的 JD 发给本地 App。
 *
 * 之所以这一步放在 background 而不是内容脚本里直接 fetch：内容脚本的网络
 * 请求 Origin 头是页面自己的域名（https://www.linkedin.com），过不了后端
 * `require_paired_request` 的 Origin 校验；只有从插件自己的执行上下文
 * （background/侧边栏，Origin 是 chrome-extension://...）发起的请求才会
 * 带上插件的 Origin，这是 Phase 0 就定下的配对鉴权设计的直接推论。
 */
async function sendJobToLocalApp(payload) {
  const token = await getToken();
  if (!token) {
    return { ok: false, error: "还没有完成配对，请先在侧边栏完成配对再试一次" };
  }
  let resp;
  try {
    resp = await fetch(`${API_BASE}/api/jobs`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-JobPilot-Token": token },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    return { ok: false, error: "连不上本地 App，请确认它正在运行" };
  }
  if (!resp.ok) {
    let detail = `本地 App 返回错误 (${resp.status})`;
    try {
      const body = await resp.json();
      if (body?.detail) detail = body.detail;
    } catch (e) {
      // 响应体不是 JSON 就用上面的默认文案,不额外报错
    }
    return { ok: false, error: detail };
  }
  const body = await resp.json();
  try {
    await chrome.tabs.create({ url: body.dashboard_url });
  } catch (e) {
    // 打开新标签页失败(极少见)不应该让"已经发送成功入库"这个事实被掩盖掉,
    // 仍然回报成功,只是带上这条附加信息。
    return { ok: true, dashboard_url: body.dashboard_url, warning: "已入库，但自动打开页面失败，请手动去 Dashboard 查看" };
  }
  return { ok: true, dashboard_url: body.dashboard_url };
}

// 消息监听器最先注册：这是侧边栏"重新检测"按钮能不能响应的关键,
// 不应该排在任何可能抛异常的代码之后。
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "jobpilot:retry-connect") {
    checkHealthAndMaybeConnect().then(() => sendResponse({ ok: true }));
    return true; // 保持通道打开,等待异步 sendResponse
  }
  if (message?.type === "jobpilot:get-state") {
    sendResponse({ state });
    return false;
  }
  if (message?.type === "jobpilot:send-job") {
    sendJobToLocalApp(message.payload || {}).then(sendResponse);
    return true; // 保持通道打开,等待异步 sendResponse
  }
  return false;
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel?.setPanelBehavior?.({ openPanelOnActionClick: true }).catch(() => {});
  ensureHealthPollAlarm();
  checkHealthAndMaybeConnect();
});

chrome.runtime.onStartup.addListener(() => {
  ensureHealthPollAlarm();
  checkHealthAndMaybeConnect();
});

try {
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === HEALTH_POLL_ALARM) {
      checkHealthAndMaybeConnect();
    }
  });
} catch (e) {
  console.error("JobPilot: 注册 alarms.onAlarm 监听失败", e);
}

// service worker 每次被唤醒（包括刚被 chrome.alarms 或消息重新拉起）都跑一次,
// 保证状态不会因为 SW 被回收而"卡"在旧状态上。
ensureHealthPollAlarm();
checkHealthAndMaybeConnect();
