const PAIR_PAGE_URL = "http://127.0.0.1:8756/pair";
// 和 manifest.json 里 content_scripts 对 linkedin.js 的 matches 规则保持一致：
// 只有匹配这个规则的标签页才会真的注入内容脚本、能响应下面的抓取请求。
const LINKEDIN_JOB_URL_PATTERN = /^https:\/\/www\.linkedin\.com\/jobs\//;
const SEND_JOB_RESET_DELAY_MS = 4000;

const STATUS_TEXT = {
  disconnected: "未连接",
  app_detected_unpaired: "已检测到本地 App，待配对",
  connecting: "正在连接…",
  connected: "已连接",
  pairing_rejected: "配对失败",
};

const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const statusDetail = document.getElementById("status-detail");
const retryBtn = document.getElementById("retry-btn");
const pairingCard = document.getElementById("pairing-card");
const notDetectedCard = document.getElementById("not-detected-card");
const linkedinHintCard = document.getElementById("linkedin-hint-card");
const openPairPageBtn = document.getElementById("open-pair-page-btn");
const tokenInput = document.getElementById("token-input");
const saveTokenBtn = document.getElementById("save-token-btn");
const sendJobReady = document.getElementById("send-job-ready");
const sendJobNotDetected = document.getElementById("send-job-not-detected");
const sendJobBtn = document.getElementById("send-job-btn");
const sendJobStatus = document.getElementById("send-job-status");

// 当前激活标签页里,能不能对着它发"抓取当前职位"消息(null 表示检测到当前
// 标签页不是 LinkedIn 职位页面,按钮应该处于"未检测到"状态)。
let jobTabId = null;

function render(state) {
  if (!state) return;
  statusDot.className = `dot dot-${state.status}`;
  statusText.textContent = STATUS_TEXT[state.status] || state.status;

  const details = [];
  if (state.lastError) details.push(state.lastError);
  if (state.status === "connected" && state.lastPongAt) {
    details.push(`最近心跳: ${new Date(state.lastPongAt).toLocaleTimeString()}`);
  }
  statusDetail.textContent = details.join(" · ");

  const needsPairing = state.status === "app_detected_unpaired" || state.status === "pairing_rejected";
  pairingCard.hidden = !needsPairing;
  notDetectedCard.hidden = state.status !== "disconnected";
  linkedinHintCard.hidden = state.status !== "connected";
}

function showFatalError(text) {
  statusDot.className = "dot dot-pairing_rejected";
  statusText.textContent = "插件后台异常";
  statusDetail.textContent = text;
}

function sendMessageSafe(message, callback) {
  chrome.runtime.sendMessage(message, (response) => {
    // 如果后台 service worker 压根没注册消息监听（比如权限配置错误导致脚本
    // 提前抛异常退出），chrome.runtime.lastError 会被置位，response 是
    // undefined。不检查这个的话,侧边栏会一直卡在初始占位文案上,看起来
    // 像"检测中"卡死,但其实是后台脚本根本没跑起来。
    if (chrome.runtime.lastError) {
      showFatalError(
        `无法联系插件后台：${chrome.runtime.lastError.message}。请到 chrome://extensions 打开 JobPilot 的"检查视图 -> service worker"看控制台报错。`
      );
      return;
    }
    callback?.(response);
  });
}

function refreshFromStorage() {
  chrome.storage.local.get("jobpilot_connection_state", ({ jobpilot_connection_state }) => {
    render(jobpilot_connection_state);
  });
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.jobpilot_connection_state) {
    render(changes.jobpilot_connection_state.newValue);
  }
});

retryBtn.addEventListener("click", () => {
  statusText.textContent = "检测中…";
  sendMessageSafe({ type: "jobpilot:retry-connect" });
});

openPairPageBtn.addEventListener("click", () => {
  chrome.tabs.create({ url: PAIR_PAGE_URL });
});

saveTokenBtn.addEventListener("click", async () => {
  const token = tokenInput.value.trim();
  if (!token) return;
  await chrome.storage.local.set({ jobpilot_pairing_token: token });
  tokenInput.value = "";
  sendMessageSafe({ type: "jobpilot:retry-connect" });
});

// ---------- 发送当前职位到 JobPilot(固定在侧边栏里,不再依赖页面悬浮按钮) ----------

function setSendJobStatus(text) {
  sendJobStatus.textContent = text || "";
}

function resetSendJobStatusLater() {
  setTimeout(() => {
    if (sendJobBtn.dataset.jobpilotState !== "sending") setSendJobStatus("");
  }, SEND_JOB_RESET_DELAY_MS);
}

/** 根据当前激活标签页的 URL,决定侧边栏是显示"可以发送"还是"未检测到职位信息"。
 * 只做 URL 层面的判断(是否匹配 LinkedIn 职位页面规则),不在这里就去抓取内容——
 * 抓取放到用户真正点击发送按钮的那一刻,理由和原来悬浮按钮的做法一致：不管
 * 标签页停留了多久、SPA 内部切换过多少次职位,点击时读到的都应该是最新状态。
 */
function refreshJobTabState() {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const tab = tabs && tabs[0];
    const isJobPage = !!(tab && tab.url && LINKEDIN_JOB_URL_PATTERN.test(tab.url));
    jobTabId = isJobPage ? tab.id : null;
    sendJobReady.hidden = !isJobPage;
    sendJobNotDetected.hidden = isJobPage;
    if (isJobPage) {
      sendJobBtn.disabled = false;
      sendJobBtn.textContent = "发送到 JobPilot";
      sendJobBtn.dataset.jobpilotState = "idle";
      setSendJobStatus("");
    }
  });
}

sendJobBtn.addEventListener("click", () => {
  if (jobTabId === null || sendJobBtn.dataset.jobpilotState === "sending") return;
  sendJobBtn.dataset.jobpilotState = "sending";
  sendJobBtn.disabled = true;
  setSendJobStatus("正在读取当前页面的职位信息…");

  chrome.tabs.sendMessage(jobTabId, { type: "jobpilot:extract-current-job" }, (extractResponse) => {
    if (chrome.runtime.lastError || !extractResponse) {
      // 标签页已经跳转走、或者内容脚本还没来得及注入完成,这两种情况都当成
      // "现在这个标签页其实抓不到职位信息"处理,重新走一次检测而不是死报错。
      sendJobBtn.dataset.jobpilotState = "idle";
      sendJobBtn.disabled = false;
      refreshJobTabState();
      return;
    }
    if (!extractResponse.ok) {
      sendJobBtn.dataset.jobpilotState = "idle";
      sendJobBtn.disabled = false;
      setSendJobStatus(extractResponse.error || "没抓到正文，试试手动粘贴");
      resetSendJobStatusLater();
      return;
    }

    setSendJobStatus("发送中…");
    sendMessageSafe({ type: "jobpilot:send-job", payload: extractResponse.job }, (sendResponse) => {
      sendJobBtn.dataset.jobpilotState = "idle";
      sendJobBtn.disabled = false;
      if (sendResponse?.ok) {
        setSendJobStatus("已发送，正在打开…");
      } else {
        setSendJobStatus(sendResponse?.error || "发送失败");
      }
      resetSendJobStatusLater();
    });
  });
});

// 标签页切换、当前标签页导航到新地址、切换浏览器窗口焦点,都可能改变"当前
// 激活标签页是不是 LinkedIn 职位页面"这件事,都需要重新检测一次。
chrome.tabs.onActivated.addListener(refreshJobTabState);
chrome.tabs.onUpdated.addListener((_tabId, changeInfo) => {
  if (changeInfo.status === "complete" || changeInfo.url) refreshJobTabState();
});
chrome.windows.onFocusChanged.addListener(refreshJobTabState);
// 侧边栏本身从隐藏变回可见时(比如用户切换到别的扩展面板又切回来),也重新
// 检测一次,防止拿着一份过期的检测结果。
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refreshJobTabState();
});

// 初始渲染：先读一次已存的状态,再主动问一次 background 拿最新的（防止 storage 里是旧值）
refreshFromStorage();
sendMessageSafe({ type: "jobpilot:get-state" }, (response) => {
  if (response?.state) render(response.state);
});
refreshJobTabState();
