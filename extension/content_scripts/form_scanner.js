/**
 * 纯函数：把一个表单页面（Document）扫描成一份"字段清单"，每一项包含
 * field_id / label / input_type / options，直接对应后端
 * `POST /api/autofill/plan` 接口的入参形状（见
 * backend/app/api/routes_extension.py 的 ExtensionFormField）。
 *
 * 和 linkedin_parser.js 遵循同一条设计原则："找不到就留空，不瞎猜"——这里
 * 具体体现为：找不到一个字段的标签文本，就直接把这个字段排除在扫描结果之外
 * （宁可少填一个字段、让用户自己手填，也不要拿一个错误/无意义的标签喂给
 * 后端的字段映射逻辑，导致填错内容）。
 *
 * 这个文件只做通用的、不依赖任何具体 ATS 平台特殊 DOM 结构的扫描逻辑；
 * 各家 ATS（Workday/Greenhouse/Lever）的特殊情况（比如 Lever 用一个独立的
 * <div class="application-label"> 而不是标准 <label for> 来标注字段）在
 * ats_selectors.js 里通过 `resolveLabelOverride` 回调扩展，不污染这里的
 * 通用逻辑。
 *
 * 另一个关键副作用：扫描过程中会给每个识别到的字段元素打上
 * `data-jobpilot-field-id` 属性（单选组则给组内每个 radio 都打上同一个
 * 值）。这是因为"扫描"和"真正把值写进 DOM"是两个独立的步骤（扫描结果要
 * 先发给本地 App 算出填表计划，算完再回来找到对应的 DOM 元素真正填值），
 * 必须有一种可靠的方式，能在算完计划之后重新精确定位到当初扫描到的那个
 * 元素——不能依赖 label 文本反查（可能重复），也不能依赖数组下标反查
 * （页面这段时间可能已经有 DOM 变化），所以选择"扫描时就地打标记"这个
 * 最直接可靠的办法。
 */

const FIELD_ID_ATTR = "data-jobpilot-field-id";

const TEXT_LIKE_TYPES = new Set(["text", "email", "tel", "url", "number", "search"]);

function _normalizeWhitespace(text) {
  return (text || "").replace(/\s+/g, " ").trim();
}

function _textFromId(doc, id) {
  if (!id) return null;
  const el = doc.getElementById(id);
  return el ? _normalizeWhitespace(el.textContent) : null;
}

// 不依赖浏览器全局的 `CSS.escape`（在纯 Node/jsdom 环境下跑单元测试时，
// `CSS` 这个全局对象根本不存在，直接引用会抛 ReferenceError）：属性选择器
// 里只有引号和反斜杠需要转义，手写一个够用的最小实现即可。
function _escapeAttrValue(value) {
  return String(value).replace(/["\\]/g, "\\$&");
}

/**
 * 通用标签解析链，按优先级依次尝试：
 * 1. <label for="el.id">
 * 2. 元素被 <label> 包裹（label 包 input 是另一种合法写法，不需要 for/id）
 * 3. aria-labelledby 指向的元素文本
 * 4. aria-label 属性
 * 5. placeholder 属性（最弱的信号，只在前面都找不到时用）
 * 找不到就返回 null,调用方应该把这个字段直接排除,而不是拿 null 当标签用。
 */
function resolveGenericLabel(el, doc) {
  if (el.id) {
    const forLabel = doc.querySelector(`label[for="${_escapeAttrValue(el.id)}"]`);
    const text = forLabel ? _normalizeWhitespace(forLabel.textContent) : null;
    if (text) return text;
  }
  const wrappingLabel = el.closest ? el.closest("label") : null;
  if (wrappingLabel) {
    // label 包 input 时，label 自身的 textContent 会把 input 的值也算进去
    // （对 <input> 一般没事，但保险起见还是把子节点里属于表单控件的部分
    // 排除掉，只取纯文本节点拼起来的内容）。
    const clone = wrappingLabel.cloneNode(true);
    clone.querySelectorAll("input, select, textarea").forEach((node) => node.remove());
    const text = _normalizeWhitespace(clone.textContent);
    if (text) return text;
  }
  const labelledBy = el.getAttribute("aria-labelledby");
  if (labelledBy) {
    // aria-labelledby 允许引用多个 id，用空格分隔
    const parts = labelledBy
      .split(/\s+/)
      .map((id) => _textFromId(doc, id))
      .filter(Boolean);
    if (parts.length) return _normalizeWhitespace(parts.join(" "));
  }
  const ariaLabel = el.getAttribute("aria-label");
  if (ariaLabel && _normalizeWhitespace(ariaLabel)) return _normalizeWhitespace(ariaLabel);

  const placeholder = el.getAttribute("placeholder");
  if (placeholder && _normalizeWhitespace(placeholder)) return _normalizeWhitespace(placeholder);

  return null;
}

function _assignFieldId(el, preferredId) {
  const existing = el.getAttribute(FIELD_ID_ATTR);
  if (existing) return existing;
  const fieldId = preferredId || `field_${Math.random().toString(36).slice(2, 10)}`;
  el.setAttribute(FIELD_ID_ATTR, fieldId);
  return fieldId;
}

/**
 * 给一个字段元素挑一个尽量稳定的 field_id：优先用平台特殊标记（由
 * ats_selectors.js 的 pickPreferredId 回调提供，比如 Workday 的
 * data-automation-id——Workday 的 name/id 是框架生成的随机值，每次加载都
 * 不一样，data-automation-id 才是跨会话稳定的标识），其次是标准的 name/id
 * 属性，最后兜底随机生成一个。
 */
function _preferredIdFor(el, pickPreferredId) {
  if (pickPreferredId) {
    const custom = pickPreferredId(el);
    if (custom) return custom;
  }
  return el.getAttribute("name") || el.id || null;
}

function _inputTypeOf(el) {
  const tag = el.tagName.toLowerCase();
  if (tag === "textarea") return "textarea";
  if (tag === "select") return "select";
  const type = (el.getAttribute("type") || "text").toLowerCase();
  if (type === "radio") return "radio";
  if (type === "checkbox") return "checkbox";
  if (TEXT_LIKE_TYPES.has(type)) return type;
  return null; // 其余 input type（hidden/submit/button/file 等）不属于我们要填的字段
}

function _selectOptions(selectEl) {
  return Array.from(selectEl.options)
    .map((opt) => ({ value: opt.value, label: _normalizeWhitespace(opt.textContent) }))
    // 过滤掉没有文本的选项，以及 value="" 的占位提示项（"请选择"/"Select
    // one..." 这类，几乎所有下拉框的约定都是用空 value 表示"还没选"，
    // 这种选项本身不是一个真实可选的答案，不应该出现在候选列表里）。
    .filter((opt) => opt.label && opt.value !== "");
}

function _radioGroupLabel(radios, doc, resolveLabelOverride) {
  // 单选组的标签一般不在某一个 radio 自己身上，而是在包裹整组的
  // <fieldset><legend> 或者一个带 role="radiogroup" 的容器上；如果这些都
  // 找不到，宁可放弃这个组（返回 null 让调用方跳过），也不要瞎猜。
  const first = radios[0];
  const fieldset = first.closest ? first.closest("fieldset") : null;
  if (fieldset) {
    const legend = fieldset.querySelector("legend");
    const text = legend ? _normalizeWhitespace(legend.textContent) : null;
    if (text) return text;
  }
  const group = first.closest ? first.closest('[role="radiogroup"]') : null;
  if (group) {
    const text = resolveGenericLabel(group, doc);
    if (text) return text;
  }
  if (resolveLabelOverride) {
    const overridden = resolveLabelOverride(first, doc);
    if (overridden) return overridden;
  }
  return null;
}

/**
 * @param {Document} doc
 * @param {Object} [opts]
 * @param {(el: Element, doc: Document) => string|null} [opts.resolveLabelOverride]
 *   平台特殊标签解析，优先于通用解析链尝试；返回 null/undefined 时回退到
 *   通用解析链。
 * @param {(el: Element) => string|null} [opts.pickPreferredId]
 *   平台特殊 field_id 来源（例如 Workday 的 data-automation-id）。
 * @returns {Array<{field_id: string, label: string, input_type: string, options: Array}>}
 */
function scanFormFields(doc, opts = {}) {
  const { resolveLabelOverride, pickPreferredId } = opts;
  const fields = [];
  const seenRadioGroups = new Set();

  const candidates = Array.from(doc.querySelectorAll("input, textarea, select"));
  for (const el of candidates) {
    const inputType = _inputTypeOf(el);
    if (!inputType) continue;
    if (el.disabled) continue;

    if (inputType === "radio") {
      const name = el.getAttribute("name");
      const groupKey = name || el;
      if (seenRadioGroups.has(groupKey)) continue;
      seenRadioGroups.add(groupKey);

      const radios = name
        ? Array.from(doc.querySelectorAll(`input[type="radio"][name="${_escapeAttrValue(name)}"]`))
        : [el];
      const label = _radioGroupLabel(radios, doc, resolveLabelOverride);
      if (!label) continue;

      const fieldId = _preferredIdFor(el, pickPreferredId) || `radio_${name || fields.length}`;
      const finalId = _assignFieldId(el, fieldId);
      radios.forEach((r) => r.setAttribute(FIELD_ID_ATTR, finalId));

      const options = radios.map((r) => ({
        value: r.value,
        label: resolveGenericLabel(r, doc) || r.value,
      }));
      fields.push({ field_id: finalId, label, input_type: "radio", options });
      continue;
    }

    const label = (resolveLabelOverride && resolveLabelOverride(el, doc)) || resolveGenericLabel(el, doc);
    if (!label) continue;

    const preferredId = _preferredIdFor(el, pickPreferredId);
    const fieldId = _assignFieldId(el, preferredId);

    if (inputType === "select") {
      fields.push({ field_id: fieldId, label, input_type: "select", options: _selectOptions(el) });
    } else if (inputType === "checkbox") {
      fields.push({ field_id: fieldId, label, input_type: "checkbox", options: [] });
    } else {
      fields.push({ field_id: fieldId, label, input_type: inputType, options: [] });
    }
  }

  return fields;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { scanFormFields, resolveGenericLabel, FIELD_ID_ATTR };
}
