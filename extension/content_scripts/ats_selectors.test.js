/**
 * Phase 4：ATS 平台识别与平台特殊适配（ats_selectors.js）。
 *
 * 这里不追求"精确复现某家 ATS 真实线上页面的完整 DOM"（云端环境没有真实
 * Workday/Greenhouse/Lever 账号，无法验证），而是基于三家平台公开可观察到
 * 的、文档化程度较高的约定（Workday 的 data-automation-id 测试钩子、
 * Lever 的 .application-label 标签写法）构造最小化的 fixture，验证识别
 * 逻辑和标签/field_id 解析逻辑本身是对的；真实页面上是否还有别的变体，
 * 依赖 README 里的手动验收清单，遇到抓不到的情况就往这里补一条新用例，
 * 和 Phase 3 对待 linkedin_parser.js 的方式完全一致。
 */

const assert = require("node:assert/strict");
const { JSDOM } = require("jsdom");

const { detectAtsPlatform, scannerOptionsFor } = require("./ats_selectors.js");
const { scanFormFields } = require("./form_scanner.js");

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
    console.error(`    ${err.stack || err.message}`);
  }
}

function docFrom(html) {
  return new JSDOM(`<!doctype html><html><body>${html}</body></html>`).window.document;
}

test("按 hostname 识别 Workday", () => {
  const doc = docFrom("<div></div>");
  const platform = detectAtsPlatform(doc, "https://acme.myworkdayjobs.com/en-US/External/job/123/apply");
  assert.equal(platform, "workday");
});

test("按 hostname 识别 Greenhouse", () => {
  const doc = docFrom("<div></div>");
  const platform = detectAtsPlatform(doc, "https://job-boards.greenhouse.io/acme/jobs/123");
  assert.equal(platform, "greenhouse");
});

test("按 hostname 识别 Lever", () => {
  const doc = docFrom("<div></div>");
  const platform = detectAtsPlatform(doc, "https://jobs.lever.co/acme/abc-123/apply");
  assert.equal(platform, "lever");
});

test("hostname 识别不出来时退化成扫描 DOM 特征（Workday data-automation-id）", () => {
  const doc = docFrom('<div data-automation-id="email"><input type="text" /></div>');
  const platform = detectAtsPlatform(doc, null);
  assert.equal(platform, "workday");
});

test("hostname 识别不出来时退化成扫描 DOM 特征（Lever .application-question）", () => {
  const doc = docFrom('<div class="application-question"><div class="application-label">Name</div></div>');
  const platform = detectAtsPlatform(doc, null);
  assert.equal(platform, "lever");
});

test("既不是已知 hostname、DOM 也没有任何已知特征标记时返回 null", () => {
  const doc = docFrom("<form><input type='text' name='x' /></form>");
  const platform = detectAtsPlatform(doc, "https://careers.example.com/apply");
  assert.equal(platform, null);
});

test("Workday: 用 data-automation-id 作为 field_id，而不是随机生成的 name/id", () => {
  const doc = docFrom(`
    <div data-automation-id="legalNameSection_firstName">
      <label for="input-random-abc123">First Name</label>
      <input id="input-random-abc123" type="text" />
    </div>
  `);
  const fields = scanFormFields(doc, scannerOptionsFor("workday"));
  assert.equal(fields.length, 1);
  assert.equal(fields[0].field_id, "legalNameSection_firstName");
  assert.equal(fields[0].label, "First Name");
});

test("Lever: 标签从 .application-label 取，而不是标准 label[for]（Lever 页面上根本没有）", () => {
  const doc = docFrom(`
    <div class="application-question">
      <div class="application-label">Email</div>
      <input name="email" type="email" />
    </div>
    <div class="application-question">
      <div class="application-label">Phone</div>
      <input name="phone" type="tel" />
    </div>
  `);
  const fields = scanFormFields(doc, scannerOptionsFor("lever"));
  assert.equal(fields.length, 2);
  const byName = Object.fromEntries(
    Array.from(doc.querySelectorAll("input")).map((el, i) => [el.name, fields[i].label])
  );
  assert.equal(byName.email, "Email");
  assert.equal(byName.phone, "Phone");
});

test("Greenhouse: 通用扫描逻辑（标准 label[for]）已经足够，不需要特殊 override", () => {
  const doc = docFrom(`
    <label for="first_name">First Name</label>
    <input id="first_name" name="job_application[first_name]" type="text" />
  `);
  const fields = scanFormFields(doc, scannerOptionsFor("greenhouse"));
  assert.equal(fields[0].label, "First Name");
  // Greenhouse 场景下 field_id 走通用逻辑：优先 name，其次 id
  assert.equal(fields[0].field_id, "job_application[first_name]");
});

console.log(`\n${passed} passed, ${failures} failed`);
if (failures > 0) {
  process.exit(1);
}
