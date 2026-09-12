/**
 * JobPilot 内容脚本：注入到 LinkedIn 职位页面，负责把当前职位信息"一键发送"
 * 到本地 App。
 *
 * 抓取逻辑本身是纯函数 `extractLinkedInJob`（定义在同目录 `linkedin_parser.js`，
 * 已经用 jsdom 跑过多种页面布局的回归测试，见 `linkedin_parser.test.js`）；
 * 这个文件只负责浏览器环境里的胶水代码：注入一个悬浮按钮、点击时抓取当前
 * DOM、把结果转发给 background service worker。
 *
 * 之所以把"真正发 HTTP 请求给本地 App"这一步放在 background 而不是这里直接
 * fetch：内容脚本运行在页面自己的执行上下文里，从这里发起的请求 Origin 头
 * 是页面的 `https://www.linkedin.com`，过不了后端 `require_paired_request`
 * 的 Origin 校验（要求必须是 `chrome-extension://` 开头）；只有从插件自己的
 * 执行上下文（background/侧边栏）发起的请求才会带上插件的 Origin。这也是
 * Phase 0 就定下的配对鉴权设计的直接推论，不是这里额外加的限制。
 *
 * LinkedIn 的职位列表/详情页是单页应用，切换到不同职位卡片通常不会整页刷新、
 * 也就不会重新执行这个内容脚本，所以这里刻意不在脚本加载时就抓一次数据缓存
 * 起来，而是把抓取放到按钮点击的那一刻——不管用户之前在同一个标签页里看过
 * 多少个不同职位，点击时读到的永远是当前这个职位的最新 DOM。
 */

(function () {
  const BUTTON_ID = "jobpilot-send-button";
  const RESET_DELAY_MS = 4000;
  const REINJECT_CHECK_INTERVAL_MS = 3000;

  function injectStyles() {
    if (document.getElementById("jobpilot-style")) return;
    const style = document.createElement("style");
    style.id = "jobpilot-style";
    style.textContent = `
      #${BUTTON_ID} {
        position: fixed;
        right: 24px;
        bottom: 24px;
        z-index: 2147483647;
        background: #0a66c2;
        color: #fff;
        border: none;
        border-radius: 24px;
        padding: 12px 20px;
        font-size: 14px;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
        cursor: pointer;
        transition: background 0.2s;
      }
      #${BUTTON_ID}:hover { background: #084a92; }
      #${BUTTON_ID}[data-jobpilot-state="sending"] { background: #6b7280; cursor: wait; }
      #${BUTTON_ID}[data-jobpilot-state="success"] { background: #16a34a; }
      #${BUTTON_ID}[data-jobpilot-state="error"] { background: #dc2626; }
    `;
    document.head.appendChild(style);
  }

  function setButtonState(btn, state, label) {
    btn.dataset.jobpilotState = state;
    btn.textContent = label;
  }

  function resetButtonLater(btn) {
    setTimeout(() => {
      // 用户可能已经点了下一次（比如失败后立刻重试），这时 state 已经不是
      // 触发这次重置时的那个状态了，不要覆盖掉更新的状态。
      if (btn.dataset.jobpilotState !== "sending") {
        setButtonState(btn, "idle", "发送到 JobPilot");
      }
    }, RESET_DELAY_MS);
  }

  function onClick() {
    const btn = document.getElementById(BUTTON_ID);
    if (!btn || btn.dataset.jobpilotState === "sending") return;

    const job = extractLinkedInJob(document, window.location.href);
    if (!job.description_raw) {
      setButtonState(btn, "error", "没抓到正文，试试手动粘贴");
      resetButtonLater(btn);
      return;
    }

    setButtonState(btn, "sending", "发送中…");
    chrome.runtime.sendMessage({ type: "jobpilot:send-job", payload: job }, (response) => {
      if (chrome.runtime.lastError) {
        setButtonState(btn, "error", "插件后台无响应");
        resetButtonLater(btn);
        return;
      }
      if (response?.ok) {
        setButtonState(btn, "success", "已发送，正在打开…");
      } else {
        setButtonState(btn, "error", response?.error || "发送失败");
      }
      resetButtonLater(btn);
    });
  }

  function ensureButton() {
    let btn = document.getElementById(BUTTON_ID);
    if (btn) return btn;
    injectStyles();
    btn = document.createElement("button");
    btn.id = BUTTON_ID;
    btn.type = "button";
    btn.dataset.jobpilotState = "idle";
    btn.textContent = "发送到 JobPilot";
    btn.addEventListener("click", onClick);
    document.body.appendChild(btn);
    return btn;
  }

  function init() {
    ensureButton();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // 低频兜底检查：万一按钮被页面自身的重渲染意外移除（LinkedIn 是单页应用，
  // 会频繁增删 DOM 节点），把它加回来。用低频 setInterval 而不是
  // MutationObserver，是刻意的简化——这个按钮不需要对页面变化做到毫秒级
  // 响应，用一个轻量的定时检查换取更简单、更不容易因为监听整个 body 子树
  // 变化而产生额外性能开销的实现。
  setInterval(ensureButton, REINJECT_CHECK_INTERVAL_MS);
})();
