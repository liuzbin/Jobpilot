/**
 * JobPilot 内容脚本：注入到已知 ATS（Workday / Greenhouse / Lever）的投递
 * 页面，负责浏览器环境里的胶水代码——"扫描页面字段 → 发给本地 App 算填表
 * 计划 → 把计划真正写进 DOM"这几步的编排，以及简历 PDF 附件注入、题库
 * 回存、投递提交追踪。
 *
 * 决策逻辑（每个字段该填什么、要不要填、合规安全限制）完全不在这个文件
 * 里，全部在后端 `app.services.autofill.build_autofill_plan`（见
 * backend/app/services/autofill.py 顶部的安全设计说明）——这个文件只是
 * "忠实执行本地 App 算好的计划"，不自己做任何判断，这是从 Phase 3
 * （linkedin.js 只做胶水、抓取逻辑在纯函数 linkedin_parser.js 里）延续
 * 下来的分层原则，Phase 4 在"决策"这一侧又多了一层安全考量，更不应该让
 * 浏览器胶水代码里掺进任何决策逻辑。
 *
 * 这个文件本身（浏览器 DOM 操作、chrome.* 消息通信）无法在没有真实 Chrome
 * 的环境里做有意义的单元测试,依赖 README 里的手动验收清单在真实 Workday/
 * Greenhouse/Lever 页面上人工确认,和 linkedin.js 当初的处理方式一致。
 */

(function () {
  const BUTTON_ID = "jobpilot-autofill-button";
  const RESUME_BUTTON_ID = "jobpilot-attach-resume-button";
  const RESET_DELAY_MS = 4000;

  function sendMessageAsync(message) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(message, (response) => {
        if (chrome.runtime.lastError) {
          resolve({ ok: false, error: chrome.runtime.lastError.message });
          return;
        }
        resolve(response);
      });
    });
  }

  // ---------- 把计划里的一条 action 真正写进 DOM ----------

  // 大部分现代 ATS 前端是 React/Vue/Ember 这类框架驱动的，直接给
  // `input.value = xxx` 赋值不会触发框架自己的状态更新（框架监听的是原生
  // setter 被劫持前的事件，不是值本身），所以要用"找到原生 value setter
  // 再调用"这个业界通用的技巧,配合手动派发 input/change 事件,才能让
  // React 之类的框架感知到这次修改。
  function _setNativeValue(el, value) {
    const proto = Object.getPrototypeOf(el);
    const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
    if (descriptor && descriptor.set) {
      descriptor.set.call(el, value);
    } else {
      el.value = value;
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function _findByFieldId(fieldId) {
    return Array.from(document.querySelectorAll(`[data-jobpilot-field-id]`)).filter(
      (el) => el.getAttribute("data-jobpilot-field-id") === fieldId
    );
  }

  function _applyFill(fieldId, value) {
    const [el] = _findByFieldId(fieldId);
    if (!el) return false;
    _setNativeValue(el, value);
    return true;
  }

  function _applySelect(fieldId, value) {
    const elements = _findByFieldId(fieldId);
    if (!elements.length) return false;
    const first = elements[0];

    if (first.tagName.toLowerCase() === "select") {
      const option = Array.from(first.options).find(
        (opt) => opt.value === value || opt.textContent.trim() === value
      );
      if (!option) return false;
      first.value = option.value;
      first.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }

    // radio 组：elements 是同一个 field_id 下的所有 radio,找 value 或者
    // 关联标签文本匹配的那一个勾上。
    const target = elements.find((el) => el.value === value) || elements.find((el) => {
      const label = document.querySelector(`label[for="${CSS.escape(el.id || "")}"]`);
      return label && label.textContent.trim() === value;
    });
    if (!target) return false;
    target.checked = true;
    target.dispatchEvent(new Event("click", { bubbles: true }));
    target.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  function applyAutofillPlan(plan) {
    let filled = 0;
    const essayFieldsNeedingSave = [];
    for (const action of plan) {
      if (action.action === "fill") {
        const ok = _applyFill(action.field_id, action.value);
        if (ok) filled += 1;
      } else if (action.action === "select") {
        const ok = _applySelect(action.field_id, action.value);
        if (ok) filled += 1;
      } else if (action.action === "skip" && action.reason === "no_similar_qa_bank_entry") {
        // 题库里没有相似问答：这是一道需要用户自己组织语言回答的问题，
        // 记下来,等用户自己填完之后主动提示"要不要存进题库"。
        essayFieldsNeedingSave.push(action.field_id);
      }
    }
    essayFieldsNeedingSave.forEach(attachQaSaveHint);
    return filled;
  }

  // ---------- 题库回存：用户自己填完一道题库里没有的问答题之后，提示存起来 ----------

  function attachQaSaveHint(fieldId) {
    const [el] = _findByFieldId(fieldId);
    if (!el) return;
    if (el.dataset.jobpilotQaHintAttached) return;
    el.dataset.jobpilotQaHintAttached = "1";

    el.addEventListener("blur", () => {
      const value = (el.value || "").trim();
      if (!value) return;
      if (el.dataset.jobpilotQaSaved === value) return; // 同样的内容不重复提示

      const label = _labelTextForSaveHint(el);
      if (!label) return;

      const ok = window.confirm(
        `要把这道题记进 JobPilot 的题库吗？下次遇到类似问题可以直接复用。\n\n问题：${label}\n答案：${value.slice(0, 100)}${value.length > 100 ? "…" : ""}`
      );
      if (!ok) return;

      el.dataset.jobpilotQaSaved = value;
      sendMessageAsync({
        type: "jobpilot:save-qa",
        payload: { questionText: label, answerText: value, jdId: window.__jobpilotJdId || null },
      });
    });
  }

  function _labelTextForSaveHint(el) {
    if (el.id) {
      const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label) return label.textContent.trim();
    }
    return el.getAttribute("aria-label") || el.getAttribute("placeholder") || null;
  }

  // ---------- 简历 PDF 附件注入 ----------

  // 简历上传控件是普通 <input type="file">，form_scanner.js 故意不扫描它
  // （文件类字段不属于"填文本/选选项"这类通用逻辑，需要完全不同的处理：
  // 用 DataTransfer 构造一个 FileList 塞进去），这里单独找。
  function _findResumeFileInput() {
    const fileInputs = Array.from(document.querySelectorAll('input[type="file"]'));
    const RESUME_HINT_PATTERN = /resume|cv|简历/i;
    return fileInputs.find((el) => {
      const label = _labelTextForSaveHint(el) || "";
      const name = el.getAttribute("name") || "";
      const id = el.id || "";
      return RESUME_HINT_PATTERN.test(label) || RESUME_HINT_PATTERN.test(name) || RESUME_HINT_PATTERN.test(id);
    });
  }

  function _base64ToUint8Array(base64) {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }

  async function attachResumePdf(jdId) {
    const fileInput = _findResumeFileInput();
    if (!fileInput) {
      window.alert("没有在这个页面上找到简历上传控件，请手动上传。");
      return;
    }
    const response = await sendMessageAsync({ type: "jobpilot:fetch-resume-pdf", payload: { jdId } });
    if (!response?.ok) {
      window.alert(`获取简历 PDF 失败：${response?.error || "未知错误"}`);
      return;
    }
    const bytes = _base64ToUint8Array(response.base64);
    const file = new File([bytes], response.filename || "resume.pdf", { type: "application/pdf" });
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);
    fileInput.files = dataTransfer.files;
    fileInput.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // ---------- 提交按钮点击追踪（"去投递"关联机制的消费端） ----------

  const SUBMIT_TEXT_PATTERN = /submit application|submit|apply now|提交申请|提交|投递/i;

  function _looksLikeSubmitButton(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === "button" || (tag === "input" && ["submit", "button"].includes((el.getAttribute("type") || "").toLowerCase()))) {
      const text = (el.textContent || el.value || "").trim();
      return SUBMIT_TEXT_PATTERN.test(text) || (el.getAttribute("type") || "").toLowerCase() === "submit";
    }
    return false;
  }

  function installSubmitTracker(jdId) {
    if (!jdId) return; // 没有关联到任何 JD（用户不是从 Dashboard 点"去投递"过来的），什么都不做
    // 用捕获阶段监听而不是冒泡阶段：有些 ATS 会在冒泡阶段的处理函数里
    // `event.preventDefault()` 并且不再继续派发（比如先做前端校验），
    // 用捕获阶段能保证"用户点击了提交按钮"这个事实本身一定能被记录到，
    // 不依赖后续冒泡是否被拦截——这里只是记录"点击"这个动作本身，不代表
    // 表单真的提交成功了，属于前面设计里说过的"尽力而为、不追求 100%
    // 精确"的降级方案。
    document.addEventListener(
      "click",
      (event) => {
        const el = event.target && event.target.closest ? event.target.closest("button, input") : null;
        if (!el || !_looksLikeSubmitButton(el)) return;
        sendMessageAsync({ type: "jobpilot:submit-clicked", payload: { jdId } });
      },
      { capture: true }
    );
  }

  // ---------- 悬浮按钮 UI ----------

  function injectStyles() {
    if (document.getElementById("jobpilot-autofill-style")) return;
    const style = document.createElement("style");
    style.id = "jobpilot-autofill-style";
    style.textContent = `
      #${BUTTON_ID}, #${RESUME_BUTTON_ID} {
        position: fixed;
        right: 24px;
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
      }
      #${BUTTON_ID} { bottom: 24px; }
      #${RESUME_BUTTON_ID} { bottom: 72px; background: #374151; }
      #${BUTTON_ID}[data-state="busy"], #${RESUME_BUTTON_ID}[data-state="busy"] { background: #6b7280; cursor: wait; }
      #${BUTTON_ID}[data-state="error"] { background: #dc2626; }
    `;
    document.head.appendChild(style);
  }

  async function onAutofillClick() {
    const btn = document.getElementById(BUTTON_ID);
    if (!btn || btn.dataset.state === "busy") return;
    btn.dataset.state = "busy";
    btn.textContent = "扫描中…";

    // detectAtsPlatform/scannerOptionsFor/scanFormFields 是普通的全局函数
    // 声明（分别定义在 ats_selectors.js / form_scanner.js），manifest.json
    // 里把它们排在这个文件之前一起注入同一个内容脚本执行环境，所以这里
    // 可以直接按名字调用，不需要模块引入——和 linkedin.js 直接调用
    // linkedin_parser.js 里的 extractLinkedInJob 是同一种写法。
    const platform = window.__jobpilotAtsPlatform;
    const scannerOptions = scannerOptionsFor(platform);
    const fields = scanFormFields(document, scannerOptions);

    if (!fields.length) {
      btn.dataset.state = "error";
      btn.textContent = "没扫描到可填字段";
      setTimeout(() => {
        btn.dataset.state = "idle";
        btn.textContent = "JobPilot 自动填表";
      }, RESET_DELAY_MS);
      return;
    }

    btn.textContent = "计算填表方案中…";
    const response = await sendMessageAsync({
      type: "jobpilot:autofill-scan",
      payload: { fields, jdId: window.__jobpilotJdId || null },
    });

    if (!response?.ok) {
      btn.dataset.state = "error";
      btn.textContent = response?.error || "生成填表计划失败";
      setTimeout(() => {
        btn.dataset.state = "idle";
        btn.textContent = "JobPilot 自动填表";
      }, RESET_DELAY_MS);
      return;
    }

    const filled = applyAutofillPlan(response.plan || []);
    btn.dataset.state = "idle";
    btn.textContent = `已填 ${filled} 个字段`;
    setTimeout(() => {
      btn.textContent = "JobPilot 自动填表";
    }, RESET_DELAY_MS);
  }

  function ensureButtons() {
    injectStyles();
    if (!document.getElementById(BUTTON_ID)) {
      const btn = document.createElement("button");
      btn.id = BUTTON_ID;
      btn.type = "button";
      btn.dataset.state = "idle";
      btn.textContent = "JobPilot 自动填表";
      btn.addEventListener("click", onAutofillClick);
      document.body.appendChild(btn);
    }
    if (!document.getElementById(RESUME_BUTTON_ID)) {
      const resumeBtn = document.createElement("button");
      resumeBtn.id = RESUME_BUTTON_ID;
      resumeBtn.type = "button";
      resumeBtn.textContent = "附加简历 PDF";
      resumeBtn.addEventListener("click", () => attachResumePdf(window.__jobpilotJdId || null));
      document.body.appendChild(resumeBtn);
    }
  }

  async function init() {
    window.__jobpilotAtsPlatform = detectAtsPlatform(document, window.location.href);
    const hostname = window.location.hostname;
    const lookup = await sendMessageAsync({
      type: "jobpilot:lookup-pending-application",
      payload: { hostname },
    });
    window.__jobpilotJdId = lookup?.jdId || null;

    ensureButtons();
    if (window.__jobpilotJdId) {
      installSubmitTracker(window.__jobpilotJdId);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
