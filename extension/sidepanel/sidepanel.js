const PAIR_PAGE_URL = "http://127.0.0.1:8756/pair";

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
const openPairPageBtn = document.getElementById("open-pair-page-btn");
const tokenInput = document.getElementById("token-input");
const saveTokenBtn = document.getElementById("save-token-btn");

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
}

function showFatalError(text) {
  statusDot.className = "dot dot-pairing_rejected";
  statusText.textContent = "插件后台异常";
  statusDetail.textContent = text;
}

function sendMessageSafe(message, callback) {
  chrome.runtime.sendMessage(message, (response) => {
    // 如果后台 service worker 压根没注册消息监听（比如权限配置错误导致脚本
    // 提前抛异常退出），㆛�e.runtime.lastError 会被置位，response 是
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

// 初始渲染：先读一次已存的状态,再主动问一次 background 拿最新的（防止 storage 里是旧值）
refreshFromStorage();
sendMessageSafe({ type: "jobpilot:get-state" }, (response) => {
  if (response?.state) render(response.state);
});
