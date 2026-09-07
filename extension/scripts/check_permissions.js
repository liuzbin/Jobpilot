#!/usr/bin/env node
/**
 * 静态检查：扫描插件代码里用到的 chrome.* API,对照 manifest.json 里声明的
 * permissions,提前发现"用了某个 API 但忘记声明权限"这类问题——
 * 就是这次 chrome.alarms 踩过的那个坑,补一个自动化检查,以后不会再犯。
 *
 * 用法：node scripts/check_permissions.js
 * 退出码非 0 表示检查不通过。
 */

const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const manifest = JSON.parse(fs.readFileSync(path.join(ROOT, "manifest.json"), "utf8"));
const declaredPermissions = new Set(manifest.permissions || []);

// 已知需要在 permissions 里声明才能使用的 chrome.* 命名空间
// （chrome.runtime / chrome.tabs.create 这类不需要额外权限的不在此列）
const NAMESPACE_TO_PERMISSION = {
  alarms: "alarms",
  storage: "storage",
  sidePanel: "sidePanel",
  scripting: "scripting",
  notifications: "notifications",
  contextMenus: "contextMenus",
  webRequest: "webRequest",
  downloads: "downloads",
};

const jsFiles = [];
function walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "node_modules" || entry.name === "scripts") continue;
      walk(full);
    } else if (entry.name.endsWith(".js")) {
      jsFiles.push(full);
    }
  }
}
walk(ROOT);

const usedNamespaces = new Set();
const usageRegex = /chrome\.([a-zA-Z]+)\./g;
for (const file of jsFiles) {
  const content = fs.readFileSync(file, "utf8");
  let match;
  while ((match = usageRegex.exec(content)) !== null) {
    usedNamespaces.add(match[1]);
  }
}

const missing = [];
for (const ns of usedNamespaces) {
  const requiredPermission = NAMESPACE_TO_PERMISSION[ns];
  if (requiredPermission && !declaredPermissions.has(requiredPermission)) {
    missing.push(`chrome.${ns} 用到了,但 manifest.json 的 permissions 里没有声明 "${requiredPermission}"`);
  }
}

if (missing.length > 0) {
  console.error("权限检查未通过：");
  for (const m of missing) console.error(" - " + m);
  process.exit(1);
}

console.log(`权限检查通过。用到的 chrome.* 命名空间: ${[...usedNamespaces].sort().join(", ")}`);
console.log(`已声明的 permissions: ${[...declaredPermissions].sort().join(", ")}`);
