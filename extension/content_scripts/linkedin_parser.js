/**
 * 纯函数：从 LinkedIn 职位详情页的 DOM 里解析出结构化 JD 信息。
 *
 * 设计原则（对应《JobPilot 实施方案》Phase 3 自测要求"对无法识别的字段有合理
 * 兜底，留空而不是抓错"）：
 * - 每个字段准备几套选择器按优先级依次尝试（LinkedIn 至少存在"新版详情页
 *   job-details-jobs-unified-top-card__*"、"稍旧版 jobs-unified-top-card__*"、
 *   "更旧的公开职位页 topcard__*"三种常见布局，且会不定期调整），全部选择器都
 *   找不到就返回 null / 空字符串，绝不用错误的兜底值冒充真实数据——比如绝不会
 *   因为抓不到公司名就把整个页面标题塞进去。
 * - 不依赖任何 chrome.* API，不发起网络请求，只读传入的 `doc`（一个 Document
 *   或者兼容 Document 接口的对象），这样可以完全脱离真实浏览器环境做单元测试：
 *   用 jsdom 加载几种模拟的 LinkedIn 页面布局跑 `linkedin_parser.test.js`。
 * - 页面结构以后如果变了、抓不到某个字段，应当是"追加一条新的选择器分支"，
 *   而不是重写整个函数——所以每类字段的选择器列表特意保持扁平、按优先级排列。
 */

function _textOf(el) {
  if (!el) return null;
  const text = (el.textContent || "").replace(/\s+/g, " ").trim();
  return text || null;
}

// 标题/公司名/元信息行这些字段本来就应该是单行文本，`_textOf` 把内部所有
// 空白（包括换行）压成一个空格是故意的。但 JD 正文（`#job-details` 之类）
// 是多段落、多条 bullet 的富文本，如果也套用同一个函数,原本用 <p>/<br>/<li>
// 分隔的段落和条目会被压缩成一整行看不出结构的文字——这是打磨阶段用户反馈
// "JD 在页面里挤成一坨"的根因（见 docs/DEVELOPMENT_LOG.md 对应章节）。
// `_blockTextOf` 专门给 JD 正文这类字段用：按块级标签（p/div/li/br/heading
// 等）切分成一行一段,块内部的空白仍然按 `_textOf` 一样的规则合并,只是块与
// 块之间用换行分开,不再是同一个空格分隔的整段文字；行内标签（span/strong/a
// 等）不会触发换行，不会把一句话拆碎。
const _BLOCK_TAGS = new Set([
  "P", "DIV", "LI", "UL", "OL", "BR", "H1", "H2", "H3", "H4", "H5", "H6", "TR", "BLOCKQUOTE",
]);

function _blockTextOf(el) {
  if (!el) return null;
  const lines = [];
  let current = "";

  function pushLine() {
    const trimmed = current.replace(/[ \t\r\n]+/g, " ").trim();
    if (trimmed) lines.push(trimmed);
    current = "";
  }

  function walk(node) {
    if (node.nodeType === 3) {
      // TEXT_NODE：jsdom / 真实浏览器都支持这个常量值，不依赖 Node 全局对象
      current += node.textContent;
      return;
    }
    if (node.nodeType !== 1) return; // 只关心文本节点和元素节点
    const tag = node.tagName;
    if (tag === "BR") {
      pushLine();
      return;
    }
    const isBlock = _BLOCK_TAGS.has(tag);
    if (isBlock) pushLine();
    const children = node.childNodes || [];
    for (let i = 0; i < children.length; i += 1) walk(children[i]);
    if (isBlock) pushLine();
  }

  walk(el);
  pushLine();
  return lines.length ? lines.join("\n") : null;
}

function _firstMatch(doc, selectors, textFn) {
  const extract = textFn || _textOf;
  for (const sel of selectors) {
    let el;
    try {
      el = doc.querySelector(sel);
    } catch (e) {
      continue; // 选择器语法在某些极端 DOM 实现下出错也不应该让整个抓取崩掉
    }
    const text = extract(el);
    if (text) return text;
  }
  return null;
}

const TITLE_SELECTORS = [
  ".job-details-jobs-unified-top-card__job-title h1",
  ".job-details-jobs-unified-top-card__job-title",
  ".jobs-unified-top-card__job-title",
  ".top-card-layout__title",
  "h1.t-24",
];

const COMPANY_SELECTORS = [
  ".job-details-jobs-unified-top-card__company-name a",
  ".job-details-jobs-unified-top-card__company-name",
  ".jobs-unified-top-card__company-name a",
  ".jobs-unified-top-card__company-name",
  ".topcard__org-name-link",
];

// 这一行在 LinkedIn 上通常是"地点 · 发布时间 · N人已申请"这类用" · "分隔的
// 复合信息：地点是它的第一段，同时整行原文也要原样保留下来，交给后端
// `jd_ingest.parse_extra_meta_rules` 做规则解析（申请人数/发布时间/是否推广），
// 这部分解析不经过 LLM，保证结果稳定，插件这边只负责把原文整行抓出来。
const META_LINE_SELECTORS = [
  ".job-details-jobs-unified-top-card__primary-description-container",
  ".job-details-jobs-unified-top-card__tertiary-description-container",
  ".jobs-unified-top-card__primary-description",
  ".jobs-unified-top-card__subtitle-primary-grouping",
  ".topcard__flavor-row",
];

const DESCRIPTION_SELECTORS = [
  "#job-details",
  ".jobs-description__content .jobs-box__html-content",
  ".jobs-description-content__text",
  ".description__text",
];

function _extractLocation(metaLineText) {
  if (!metaLineText) return null;
  // 按 " · " (或中点变体 "•") 切分，第一段通常是地点，例如 "Toronto, ON"。
  const firstSegment = metaLineText.split(/[·•]/)[0];
  const trimmed = firstSegment ? firstSegment.trim() : "";
  return trimmed || null;
}

/**
 * @param {Document} doc 页面的 Document（真实浏览器里传 `document`；测试里传 jsdom 的 document）
 * @param {string|null} [locationHref] 页面地址，用作 source_url 的兜底（找不到 canonical link 时用它）
 */
function extractLinkedInJob(doc, locationHref) {
  const title = _firstMatch(doc, TITLE_SELECTORS);
  const company = _firstMatch(doc, COMPANY_SELECTORS);
  const metaLineRaw = _firstMatch(doc, META_LINE_SELECTORS);
  const descriptionRaw = _firstMatch(doc, DESCRIPTION_SELECTORS, _blockTextOf);

  let sourceUrl = locationHref || null;
  const canonical = doc.querySelector('link[rel="canonical"]');
  const canonicalHref = canonical && canonical.getAttribute("href");
  if (canonicalHref) sourceUrl = canonicalHref;

  return {
    title,
    company,
    location: _extractLocation(metaLineRaw),
    extra_meta_raw: metaLineRaw,
    description_raw: descriptionRaw || "",
    source_url: sourceUrl,
  };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { extractLinkedInJob, _blockTextOf };
}
