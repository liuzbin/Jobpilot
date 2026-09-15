/**
 * 对应《JobPilot 实施方案》Phase 3 自测要求："对多个真实 LinkedIn 职位页面
 * HTML 快照做回归测试，覆盖几种常见页面布局，确认关键字段都能正确提取，
 * 对无法识别的字段有合理兜底（留空而不是抓错）"。
 *
 * 用 jsdom 把几份模拟的 LinkedIn 页面布局（新版详情页 / 稍旧版 / 更旧的公开
 * 职位页 topcard 布局 / 完全无法识别的布局）加载成真实 DOM，跑
 * `extractLinkedInJob`，用 Node 内置的 assert 断言结果，不需要额外的测试框架
 * （沿用本项目 `scripts/check_permissions.js` 的"纯 Node 脚本 + 内置 assert"
 * 风格）。用法：`node content_scripts/linkedin_parser.test.js`。
 */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const { extractLinkedInJob, _blockTextOf } = require("./linkedin_parser.js");

let failures = 0;
let passed = 0;

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`  ok - ${name}`);
  } catch (err) {
    failures += 1;
    console.error(`  FAIL - ${name}`);
    console.error(`    ${err.message}`);
  }
}

function loadFixture(filename) {
  const html = fs.readFileSync(path.join(__dirname, "__fixtures__", filename), "utf-8");
  return new JSDOM(html).window.document;
}

test("新版详情页布局（job-details-jobs-unified-top-card__*）：关键字段全部提取正确", () => {
  const doc = loadFixture("layout_new.html");
  const result = extractLinkedInJob(doc, "https://www.linkedin.com/jobs/view/3999999999/?refId=abc");

  assert.equal(result.title, "Backend Engineer");
  assert.equal(result.company, "Acme Corp");
  assert.equal(result.location, "Toronto, ON");
  assert.equal(result.extra_meta_raw, "Toronto, ON · 2 weeks ago · 80 people clicked apply");
  assert.match(result.description_raw, /Backend Engineer with 3\+ years/);
  // 页面里有 <link rel="canonical">，应该优先用它而不是带查询参数的 location.href
  assert.equal(result.source_url, "https://www.linkedin.com/jobs/view/3999999999/");
});

test("稍旧版布局（jobs-unified-top-card__*）：关键字段全部提取正确，且没有 canonical 时回退用传入的 URL", () => {
  const doc = loadFixture("layout_older.html");
  const result = extractLinkedInJob(doc, "https://www.linkedin.com/jobs/view/1111111111/");

  assert.equal(result.title, "Data Engineer");
  assert.equal(result.company, "Beta Inc");
  assert.equal(result.location, "Vancouver, BC");
  assert.equal(result.extra_meta_raw, "Vancouver, BC · 3 days ago · Promoted by hirer");
  assert.match(result.description_raw, /ETL pipelines on top of Spark and Airflow/);
  assert.equal(result.source_url, "https://www.linkedin.com/jobs/view/1111111111/");
});

test("更旧的公开职位页布局（topcard__*）：关键字段全部提取正确", () => {
  const doc = loadFixture("layout_public_topcard.html");
  const result = extractLinkedInJob(doc, "https://www.linkedin.com/jobs/view/2222222222/");

  assert.equal(result.title, "Frontend Developer");
  assert.equal(result.company, "Gamma LLC");
  assert.equal(result.location, "Remote");
  assert.match(result.extra_meta_raw, /25 people clicked apply/);
  assert.match(result.description_raw, /React and TypeScript/);
});

test("完全无法识别的布局：所有字段都老老实实留空，不瞎猜（description_raw 兜底成空字符串而不是 null，方便后端校验）", () => {
  const doc = loadFixture("layout_missing_fields.html");
  const result = extractLinkedInJob(doc, "https://www.linkedin.com/jobs/view/000/");

  assert.equal(result.title, null);
  assert.equal(result.company, null);
  assert.equal(result.location, null);
  assert.equal(result.extra_meta_raw, null);
  assert.equal(result.description_raw, "");
  // source_url 不依赖页面内容能不能识别，传入的当前页面地址永远是可靠的兜底
  assert.equal(result.source_url, "https://www.linkedin.com/jobs/view/000/");
});

test("只用空白字符构成的字段（元素存在但没有实际文本）也算没抓到，不返回空字符串或纯空白", () => {
  const doc = new JSDOM(
    `<!doctype html><html><body>
       <div class="job-details-jobs-unified-top-card__job-title">   </div>
     </body></html>`
  ).window.document;
  const result = extractLinkedInJob(doc, null);
  assert.equal(result.title, null);
  assert.equal(result.source_url, null);
});

// 打磨阶段用户反馈修复：JD 正文如果有段落/bullet 结构，应该按块级标签换行
// 保留下来，而不是像标题/公司名那样把所有空白（含换行）压成一个空格，
// 否则一段有格式的 JD 在 Dashboard 页面里会挤成一整坨看不出结构的文字。
test("JD 正文有多段落 + bullet 列表结构时，description_raw 按块级标签保留换行，行内标签不拆碎句子", () => {
  const doc = new JSDOM(
    `<!doctype html><html><body>
       <div class="job-details-jobs-unified-top-card__job-title"><h1>Backend Engineer</h1></div>
       <div id="job-details">
         <p>We are looking for a <strong>Backend Engineer</strong> with 3+ years of experience.</p>
         <p>Responsibilities:</p>
         <ul>
           <li>Build and maintain <a href="#">payment services</a>.</li>
           <li>Own on-call rotation.</li>
         </ul>
       </div>
     </body></html>`
  ).window.document;
  const result = extractLinkedInJob(doc, "https://www.linkedin.com/jobs/view/333/");

  const lines = result.description_raw.split("\n");
  assert.equal(lines.length, 4);
  assert.equal(lines[0], "We are looking for a Backend Engineer with 3+ years of experience.");
  assert.equal(lines[1], "Responsibilities:");
  assert.equal(lines[2], "Build and maintain payment services.");
  assert.equal(lines[3], "Own on-call rotation.");
});

test("JD 正文只有单个容器包纯文本（没有 <p>/<li> 这类块级子标签）时，行为和以前完全一样：合并成单行", () => {
  const doc = new JSDOM(
    `<!doctype html><html><body>
       <div id="job-details">
         We are looking for a Backend Engineer with 3+ years of experience.
         You will build and maintain payment services used by millions of users.
       </div>
     </body></html>`
  ).window.document;
  const result = extractLinkedInJob(doc, null);
  assert.equal(
    result.description_raw,
    "We are looking for a Backend Engineer with 3+ years of experience. You will build and maintain payment services used by millions of users."
  );
});

test("_blockTextOf：<br> 换行、连续空白行、以及只有空白的元素都被正确处理", () => {
  const doc = new JSDOM(
    `<!doctype html><html><body>
       <div id="x">Line one<br><br>Line two<div>   </div>Line three</div>
     </body></html>`
  ).window.document;
  const el = doc.querySelector("#x");
  assert.equal(_blockTextOf(el), "Line one\nLine two\nLine three");
  assert.equal(_blockTextOf(null), null);
});

console.log(`\n${passed} passed, ${failures} failed`);
if (failures > 0) {
  process.exit(1);
}
