/**
 * Phase 4：通用表单字段扫描（form_scanner.js）。用 jsdom 构造各种标签
 * 关联方式（label[for]、label 包裹、aria-labelledby、aria-label、
 * placeholder、完全没有标签）验证扫描结果，同时覆盖 select/radio/checkbox
 * 三种非纯文本控件、以及"扫描会给元素打 field_id 标记方便之后回填"这个
 * 关键副作用。
 */

const assert = require("node:assert/strict");
const { JSDOM } = require("jsdom");

const { scanFormFields, resolveGenericLabel, FIELD_ID_ATTR } = require("./form_scanner.js");

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

function byId(fields, fieldId) {
  return fields.find((f) => f.field_id === fieldId);
}

test("label[for] 关联能正确取到标签文本", () => {
  const doc = docFrom(`
    <label for="email">Email Address</label>
    <input id="email" name="email" type="email" />
  `);
  const fields = scanFormFields(doc);
  assert.equal(fields.length, 1);
  assert.equal(fields[0].label, "Email Address");
  assert.equal(fields[0].input_type, "email");
  assert.equal(fields[0].field_id, "email");
});

test("label 包裹 input 时能取到标签文本，且不包含 input 自身的值", () => {
  const doc = docFrom(`
    <label>Full Name <input name="full_name" type="text" value="Zhang Wei" /></label>
  `);
  const fields = scanFormFields(doc);
  assert.equal(fields[0].label, "Full Name");
});

test("aria-labelledby 指向的元素文本能被取到", () => {
  const doc = docFrom(`
    <span id="phone-label">Phone Number</span>
    <input name="phone" type="tel" aria-labelledby="phone-label" />
  `);
  const fields = scanFormFields(doc);
  assert.equal(fields[0].label, "Phone Number");
});

test("aria-label 属性能被取到（优先级低于 label[for]/aria-labelledby）", () => {
  const doc = docFrom(`<input name="city" type="text" aria-label="Current City" />`);
  const fields = scanFormFields(doc);
  assert.equal(fields[0].label, "Current City");
});

test("placeholder 是最弱的兜底信号，前面几种都没有才会用它", () => {
  const doc = docFrom(`<input name="portfolio" type="url" placeholder="Portfolio URL" />`);
  const fields = scanFormFields(doc);
  assert.equal(fields[0].label, "Portfolio URL");
});

test("完全找不到任何标签的字段被排除在结果之外，不返回 null 标签", () => {
  const doc = docFrom(`<input name="mystery" type="text" />`);
  const fields = scanFormFields(doc);
  assert.equal(fields.length, 0);
});

test("select 控件能提取 options 列表", () => {
  const doc = docFrom(`
    <label for="location">Current Location</label>
    <select id="location" name="location">
      <option value="">请选择</option>
      <option value="to">Toronto, ON</option>
      <option value="van">Vancouver, BC</option>
    </select>
  `);
  const fields = scanFormFields(doc);
  assert.equal(fields[0].input_type, "select");
  assert.deepEqual(
    fields[0].options.map((o) => o.value),
    ["to", "van"] // 空白 option（占位提示文字）被过滤掉
  );
});

test("checkbox 被识别为独立的选择类字段", () => {
  const doc = docFrom(`
    <label for="agree">I agree to the terms</label>
    <input id="agree" name="agree" type="checkbox" />
  `);
  const fields = scanFormFields(doc);
  assert.equal(fields[0].input_type, "checkbox");
});

test("radio 组会被合并成一个字段，标签取自 fieldset legend，options 是各个选项", () => {
  const doc = docFrom(`
    <fieldset>
      <legend>Are you authorized to work in this country?</legend>
      <label><input type="radio" name="work_auth" value="yes" /> Yes</label>
      <label><input type="radio" name="work_auth" value="no" /> No</label>
    </fieldset>
  `);
  const fields = scanFormFields(doc);
  assert.equal(fields.length, 1);
  assert.equal(fields[0].input_type, "radio");
  assert.equal(fields[0].label, "Are you authorized to work in this country?");
  assert.deepEqual(
    fields[0].options.map((o) => o.value),
    ["yes", "no"]
  );
});

test("radio 组找不到任何组标签时整组跳过，不瞎猜", () => {
  const doc = docFrom(`
    <input type="radio" name="mystery_group" value="a" />
    <input type="radio" name="mystery_group" value="b" />
  `);
  const fields = scanFormFields(doc);
  assert.equal(fields.length, 0);
});

test("扫描会给每个字段元素打上 data-jobpilot-field-id 属性，方便之后回填定位", () => {
  const doc = docFrom(`
    <label for="email">Email</label>
    <input id="email" name="email" type="email" />
  `);
  scanFormFields(doc);
  const input = doc.getElementById("email");
  assert.equal(input.getAttribute(FIELD_ID_ATTR), "email");
});

test("radio 组内每个选项都会被打上同一个 field_id", () => {
  const doc = docFrom(`
    <fieldset>
      <legend>Gender</legend>
      <label><input type="radio" name="gender" value="m" /> Male</label>
      <label><input type="radio" name="gender" value="f" /> Female</label>
    </fieldset>
  `);
  const fields = scanFormFields(doc);
  const radios = Array.from(doc.querySelectorAll('input[name="gender"]'));
  const ids = radios.map((r) => r.getAttribute(FIELD_ID_ATTR));
  assert.equal(ids[0], ids[1]);
  assert.equal(ids[0], fields[0].field_id);
});

test("disabled 字段被跳过", () => {
  const doc = docFrom(`
    <label for="email">Email</label>
    <input id="email" name="email" type="email" disabled />
  `);
  const fields = scanFormFields(doc);
  assert.equal(fields.length, 0);
});

test("hidden/submit/file 等控件类型不会被当成可填字段", () => {
  const doc = docFrom(`
    <input type="hidden" name="csrf" value="abc" />
    <input type="submit" value="Submit" />
    <label for="resume">Resume</label>
    <input id="resume" type="file" />
  `);
  const fields = scanFormFields(doc);
  assert.equal(fields.length, 0);
});

test("平台特殊 resolveLabelOverride 优先于通用解析链", () => {
  const doc = docFrom(`<div class="application-question"><div class="application-label">Full Name</div><input name="name" type="text" /></div>`);
  const overrideCalls = [];
  const fields = scanFormFields(doc, {
    resolveLabelOverride: (el) => {
      overrideCalls.push(el);
      const container = el.closest(".application-question");
      const labelEl = container && container.querySelector(".application-label");
      return labelEl ? labelEl.textContent.trim() : null;
    },
  });
  assert.equal(fields[0].label, "Full Name");
  assert.equal(overrideCalls.length, 1);
});

test("平台特殊 pickPreferredId 优先于 name/id 生成 field_id", () => {
  const doc = docFrom(`
    <div data-automation-id="legalNameSection_firstName">
      <label for="input-99">First Name</label>
      <input id="input-99" type="text" />
    </div>
  `);
  const fields = scanFormFields(doc, {
    pickPreferredId: (el) => {
      const ancestor = el.closest("[data-automation-id]");
      return ancestor ? ancestor.getAttribute("data-automation-id") : null;
    },
  });
  assert.equal(fields[0].field_id, "legalNameSection_firstName");
});

console.log(`\n${passed} passed, ${failures} failed`);
if (failures > 0) {
  process.exit(1);
}
