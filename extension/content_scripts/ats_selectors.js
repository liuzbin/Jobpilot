/**
 * ATS（Applicant Tracking System，招聘系统）平台识别 + 平台特殊 DOM 结构
 * 适配，目前覆盖三家最常见的第三方投递系统：Workday、Greenhouse、Lever。
 *
 * 和 linkedin_parser.js 面对的问题不一样：LinkedIn 是单一站点，页面结构
 * 只有"新旧版本"的差异；ATS 场景是"每家公司都在用别人家做的系统，页面
 * 结构由 ATS 供应商决定，同一家供应商的不同客户实例长得几乎一样"，所以
 * 这里的策略是先识别"这是哪家 ATS"，再应用对应供应商的已知 DOM 模式，
 * 而不是像 linkedin_parser.js 那样维护一份扁平的选择器优先级列表。
 *
 * 三家已知模式（公开可观察到的、相对稳定的约定，不是内部实现细节，但仍然
 * 属于"最佳努力、需要真实页面持续验证"的规则库——这一点在 README 的手动
 * 验收清单里有专门说明，和 Phase 3 对待 LinkedIn 抓取规则的态度一致）：
 *
 * - Workday（*.myworkdayjobs.com）：整个应用是一个高度动态的单页应用，
 *   几乎所有可交互控件都带有稳定的 `data-automation-id` 属性（这是 Workday
 *   自己的自动化测试钩子，官方从未打算改动，相对可靠）；反而 `name`/`id`
 *   属性是框架运行时生成的随机字符串，每次会话都会变，不能用来做 field_id。
 *   所以 Workday 适配的核心是"优先用 data-automation-id 当 field_id"。
 * - Greenhouse：分两种嵌入形态，一种是较旧的直接嵌入表单（`id="first_name"`
 *   这类语义化 id，标准 `<label for>` 标注），一种是新版 job-boards 站点
 *   （`name="job_application[first_name]"` 这类命名空间化的 name 属性），
 *   两种都遵循标准的 `<label for>` 关联,不需要特殊标签解析,通用扫描逻辑
 *   已经能处理,这里只需要负责"识别出这是 Greenhouse"这一半。
 * - Lever：标签不是标准的 `<label for>`，而是紧挨着控件的
 *   `<div class="application-label">标签文字</div>`，同一个问题的容器
 *   通常是 `.application-question`，需要专门的标签解析逻辑。
 */

const WORKDAY_HOSTNAME_PATTERN = /myworkdayjobs\.com$/i;
const GREENHOUSE_HOSTNAME_PATTERN = /(^|\.)greenhouse\.io$/i;
const LEVER_HOSTNAME_PATTERN = /(^|\.)lever\.co$/i;

function _hostnameOf(locationHref) {
  if (!locationHref) return null;
  try {
    return new URL(locationHref).hostname;
  } catch (e) {
    return null;
  }
}

/**
 * 识别当前页面属于哪家 ATS。优先看 URL hostname（最可靠，供应商域名基本
 * 不会变），hostname 判断不出来时再退化成扫描 DOM 里有没有这家供应商的
 * 特征标记——这个 DOM 兜底主要是为了方便离线用固定的 HTML fixture 跑
 * 单元测试（测试不会有真实的 `https://xxx.myworkdayjobs.com` 地址）。
 *
 * @returns {"workday"|"greenhouse"|"lever"|null}
 */
function detectAtsPlatform(doc, locationHref) {
  const hostname = _hostnameOf(locationHref);
  if (hostname) {
    if (WORKDAY_HOSTNAME_PATTERN.test(hostname)) return "workday";
    if (GREENHOUSE_HOSTNAME_PATTERN.test(hostname)) return "greenhouse";
    if (LEVER_HOSTNAME_PATTERN.test(hostname)) return "lever";
  }
  if (doc.querySelector("[data-automation-id]")) return "workday";
  if (doc.querySelector(".application-label, .application-question")) return "lever";
  if (doc.querySelector('#application_form, form[id^="application"], [name^="job_application["]')) {
    return "greenhouse";
  }
  return null;
}

/**
 * Workday 字段的 field_id 优先取自身或最近祖先的 `data-automation-id`；
 * 找不到就返回 null，交给通用逻辑退化成 name/id/随机生成。
 */
function workdayPickPreferredId(el) {
  const own = el.getAttribute("data-automation-id");
  if (own) return own;
  const ancestor = el.closest ? el.closest("[data-automation-id]") : null;
  return ancestor ? ancestor.getAttribute("data-automation-id") : null;
}

function _normalizeWhitespace(text) {
  return (text || "").replace(/\s+/g, " ").trim();
}

/**
 * Lever 的标签解析：控件本身通常没有 id/aria-label，标签文字在同一个
 * `.application-question` 容器里的 `.application-label` 节点上。
 */
function leverResolveLabel(el, doc) {
  const container = el.closest ? el.closest(".application-question") : null;
  const scope = container || doc;
  const labelEl = scope.querySelector ? scope.querySelector(".application-label") : null;
  const text = labelEl ? _normalizeWhitespace(labelEl.textContent) : null;
  return text || null;
}

/**
 * 给指定平台返回 form_scanner.scanFormFields 需要的 `resolveLabelOverride`
 * / `pickPreferredId` 回调；Greenhouse 没有特殊处理，通用扫描逻辑已经够用，
 * 返回空对象让调用方直接使用通用逻辑。
 */
function scannerOptionsFor(platform) {
  if (platform === "workday") {
    return { pickPreferredId: workdayPickPreferredId };
  }
  if (platform === "lever") {
    return { resolveLabelOverride: leverResolveLabel };
  }
  return {};
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    detectAtsPlatform,
    workdayPickPreferredId,
    leverResolveLabel,
    scannerOptionsFor,
  };
}
