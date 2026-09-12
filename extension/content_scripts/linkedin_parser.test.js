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

const { extractLinkedInJob } = require("./linkedin_parser.js");

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

console.log(`\n${passed} passed, ${failures} failed`);
if (failures > 0) {
  process.exit(1);
}
