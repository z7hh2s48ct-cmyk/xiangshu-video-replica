#!/usr/bin/env node
// T1 样式拆分脚本（F-01/P0-1，前端分析报告 2026-09-12）。
// 把 client/src/styles.css 按"类使用方"拆成三份：
//   1. styles.css                —— 仅管理端与内部壳（App.tsx 死壳）所需
//   2. customer/customer-access.css —— 客户全局基础(:root/body/元素) + 账户屏(customer/*.tsx, RootApp)
//   3. legacy-panels.css         —— 客户 lane 经 LiveWorkspacePanel 挂载的老组件
// 处置规则：
//   - 规则的选择器类 ∩ admin/内部壳使用 → 留 styles.css；若同时 ∩ 客户使用 → 拷贝进客户文件（双用）
//   - 裸选择器(:root/body/button/input 等无类规则) → 留 styles.css，且复制进客户基础段
//   - 其余 ∩ 客户使用 → 迁出
//   - @media 块拆到内层规则逐条分类，再按桶重组（避免块级并集误分类）
//   - 客户文件做选择器级清洗：剔除含 .admin- 的选择器；整条全 admin 则丢弃
// 内置断言：admin/内部壳用到且原 styles.css 定义过的类，重写后必须仍被定义（零缺失才退出 0）。
// 已知边界：不支持 @supports 与嵌套 @media（原版 styles.css 两者皆无；引入时需扩展本脚本）。
// 用法：node scripts/split_customer_styles.mjs（从仓库根目录；会先要求 styles.css 无未提交改动）
import { readFileSync, writeFileSync, readdirSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SRC = resolve(ROOT, "client/src");
const cssPath = resolve(SRC, "styles.css");
const css = readFileSync(cssPath, "utf8");

// 客户 lane 经 LiveWorkspacePanel 挂载的老组件（位于 client/src 根目录）
const legacyFiles = [
  "AnalysisWorkspace.tsx","ProjectDetailFlow.tsx","ProjectsPage.tsx",
  "CharacterLibrary.tsx","CharacterSelection.tsx","CharacterReferenceSelection.tsx",
  "SourceFrameSelection.tsx","FirstFrameSelection.tsx","TaskRecordsPanel.tsx",
  "WalletPanel.tsx","PromptMarkdown.tsx","GenerationComposer.tsx",
  "GenerationLauncher.tsx","ScriptEditor.tsx","VideoResultStage.tsx",
  "SimpleCharacterUpload.tsx","SettingsPanel.tsx","WorkspaceTabs.tsx",
];
// 客户账户屏：customer/*.tsx 全量 + RootApp（目录扫描，防手工清单漏项）
const accessFiles = ["RootApp.tsx"];
for (const f of readdirSync(resolve(SRC, "customer"))) {
  if (f.endsWith(".tsx")) accessFiles.push("customer/" + f);
}
const internalFiles = ["App.tsx"];

function classesUsed(files) {
  const set = new Set();
  for (const f of files) {
    const text = readFileSync(resolve(SRC, f), "utf8");
    const lits = [
      ...text.matchAll(/className="([^"]*)"/g),
      ...text.matchAll(/className=\{`([^`]*)`\}/g),
    ];
    for (const m of lits) {
      for (const tok of m[1].split(/\s+/)) {
        if (/^[a-z][a-z0-9_-]*$/i.test(tok)) set.add(tok);
      }
    }
  }
  return set;
}
const legacyU = classesUsed(legacyFiles);
const accessU = classesUsed(accessFiles);
const internalU = classesUsed(internalFiles);
const adminU = classesUsed(["AdminApp.tsx", "admin-main.tsx"]);
for (const entry of readdirSync(resolve(SRC, "admin"), { recursive: true })) {
  if (typeof entry === "string" && entry.endsWith(".tsx")) {
    for (const c of classesUsed(["admin/" + entry])) adminU.add(c);
  }
}
const custU = new Set([...legacyU, ...accessU]);

function parseBlocks(text) {
  const blocks = [];
  let i = 0;
  while (i < text.length) {
    const cm = text.slice(i).match(/^\s*\/\*[\s\S]*?\*\//);
    if (cm) { i += cm[0].length; continue; }
    const ws = text.slice(i).match(/^\s+/);
    if (ws) { i += ws[0].length; continue; }
    const open = text.indexOf("{", i);
    if (open === -1) break;
    const header = text.slice(i, open).trim();
    let depth = 1, j = open + 1;
    while (j < text.length && depth > 0) {
      if (text[j] === "{") depth++; else if (text[j] === "}") depth--;
      j++;
    }
    blocks.push({ header, body: text.slice(open + 1, j - 1), media: header.startsWith("@media") });
    i = j;
  }
  return blocks;
}
function ruleClasses(sel) {
  return new Set([...sel.matchAll(/\.([a-zA-Z][a-zA-Z0-9_-]*)/g)].map((m) => m[1]));
}
function innerRules(body) {
  // 把 @media 的 body 拆成 [selector, ruleText] 对（单层）
  const out = [];
  let i = 0;
  while (i < body.length) {
    const ws = body.slice(i).match(/^\s+/);
    if (ws) { i += ws[0].length; continue; }
    const open = body.indexOf("{", i);
    if (open === -1) break;
    const header = body.slice(i, open).trim();
    let depth = 1, j = open + 1;
    while (j < body.length && depth > 0) {
      if (body[j] === "{") depth++; else if (body[j] === "}") depth--;
      j++;
    }
    out.push({ header, bodyText: body.slice(open + 1, j - 1) });
    i = j;
  }
  return out;
}
// 选择器级清洗：剔除含 .admin- 的选择器；整条全 admin 则丢弃。
function scrubRule(header, body) {
  const kept = header.split(",").map((s) => s.trim()).filter((s) => !/\.admin-/.test(s));
  return kept.length ? kept.join(",\n") + " {" + body + "}" : "";
}
function scrubAdminSelectors(text) {
  let out = "", i = 0;
  while (i < text.length) {
    const open = text.indexOf("{", i);
    if (open === -1) break;
    const prevClose = text.lastIndexOf("}", open);
    const headerStart = Math.max(i, prevClose + 1);
    const header = text.slice(headerStart, open).trim();
    let depth = 1, j = open + 1;
    while (j < text.length && depth > 0) {
      if (text[j] === "{") depth++; else if (text[j] === "}") depth--;
      j++;
    }
    const body = text.slice(open + 1, j - 1);
    if (header.startsWith("@media")) {
      const inner = innerRules(body)
        .map((r) => scrubRule(r.header, r.bodyText))
        .filter(Boolean)
        .join("\n");
      if (inner.trim()) out += header + " {\n" + inner + "\n}\n";
    } else {
      const scrubbed = scrubRule(header, body);
      if (scrubbed) out += scrubbed + "\n";
    }
    i = j;
  }
  return out;
}

// 单条规则（含 @media 内层逐条）分类
function classify(header, body) {
  const selectors = header.split(",").map((s) => s.trim());
  const classes = new Set();
  for (const s of selectors) for (const c of ruleClasses(s)) classes.add(c);
  const isBare = selectors.every((s) => !/\.[a-zA-Z]/.test(s));
  return {
    isBare,
    hitsAdmin: [...classes].some((c) => adminU.has(c)),
    hitsInternal: [...classes].some((c) => internalU.has(c)),
    hitsAccess: [...classes].some((c) => accessU.has(c)),
    hitsLegacy: [...classes].some((c) => legacyU.has(c)),
    hitsCustomer: [...classes].some((c) => custU.has(c)),
    text: header + " {" + body + "}\n",
  };
}

const keepStyles = [], accessOut = [], legacyOut = [], accessGlobals = [];
const stats = { keep: 0, keepAndCopy: 0, moveAccess: 0, moveLegacy: 0, bareKeep: 0 };
const definedOriginal = new Set(), definedNew = new Set();
for (const m of css.matchAll(/\.([a-zA-Z][a-zA-Z0-9_-]*)/g)) definedOriginal.add(m[1]);

for (const b of parseBlocks(css)) {
  if (!b.media) {
    const c = classify(b.header, b.body);
    for (const cl of ruleClasses(b.header)) definedNew.add(cl);
    if (c.isBare) {
      keepStyles.push(c.text); accessGlobals.push(c.text); stats.bareKeep++;
      continue;
    }
    if (c.hitsAdmin || c.hitsInternal) {
      keepStyles.push(c.text); stats.keep++;
      if (c.hitsCustomer) {
        (c.hitsAccess ? accessOut : legacyOut).push(c.text);
        stats.keepAndCopy++;
      }
      continue;
    }
    if (c.hitsAccess) { accessOut.push(c.text); stats.moveAccess++; continue; }
    if (c.hitsLegacy) { legacyOut.push(c.text); stats.moveLegacy++; continue; }
    keepStyles.push(c.text); stats.keep++;
    continue;
  }
  // @media：内层逐条分类后按桶重组
  const buckets = { keep: [], access: [], legacy: [] };
  for (const inner of innerRules(b.body)) {
    const c = classify(inner.header, inner.bodyText);
    for (const cl of ruleClasses(inner.header)) definedNew.add(cl);
    if (c.isBare) { buckets.keep.push(c.text); accessGlobals.push(b.header + " {\n" + c.text + "}\n"); continue; }
    if (c.hitsAdmin || c.hitsInternal) {
      buckets.keep.push(c.text); stats.keep++;
      if (c.hitsCustomer) { (c.hitsAccess ? buckets.access : buckets.legacy).push(c.text); stats.keepAndCopy++; }
      continue;
    }
    if (c.hitsAccess) { buckets.access.push(c.text); stats.moveAccess++; continue; }
    if (c.hitsLegacy) { buckets.legacy.push(c.text); stats.moveLegacy++; continue; }
    buckets.keep.push(c.text); stats.keep++;
  }
  if (buckets.keep.length) keepStyles.push(b.header + " {\n" + buckets.keep.join("") + "}\n");
  if (buckets.access.length) accessOut.push(b.header + " {\n" + buckets.access.join("") + "}\n");
  if (buckets.legacy.length) legacyOut.push(b.header + " {\n" + buckets.legacy.join("") + "}\n");
}

const bannerA = `/* 客户账户屏与共享基础样式（激活/登录/配对/设备/会话/个人中心/充值弹窗）。
 * 由 styles.css 拆出（前端分析报告 F-01/P0-1 修复）：客户构建制品不含 styles.css，
 * 本文件经 RootApp.tsx 引入，服务客户 lane；管理端样式仍在 styles.css（CW-019 两侧隔离）。
 * 再生成：node scripts/split_customer_styles.mjs（见脚本头注释）。
 */\n\n`;
const bannerL = `/* 客户工作台老组件样式（经 LiveWorkspacePanel 挂载：拆解/项目/人物/任务/钱包等）。
 * 由 styles.css 拆出（前端分析报告 F-01/P0-1 修复）；管理端样式仍在 styles.css。
 * 本文件不得引入任何管理域（admin 域）规则（CW-019）。
 * 再生成：node scripts/split_customer_styles.mjs（见脚本头注释）。
 */\n\n`;
writeFileSync(resolve(SRC, "customer/customer-access.css"),
  bannerA + scrubAdminSelectors(accessGlobals.join("")) + "\n" + scrubAdminSelectors(accessOut.join("\n")));
writeFileSync(resolve(SRC, "legacy-panels.css"),
  bannerL + scrubAdminSelectors(legacyOut.join("\n")));
writeFileSync(cssPath,
  `/* 管理端与内部壳基础样式表。\n` +
    ` * 客户 lane 专属样式已拆出：customer/customer-access.css（RootApp 引入）、legacy-panels.css（LiveWorkspacePanel 引入）。\n` +
    ` * 管理入口经 admin-main.tsx 引入本文件（ADMIN-UI-AUDIT-20260911）；客户制品禁止包含本文件（CW-019）。\n` +
    ` * 再生成拆分：node scripts/split_customer_styles.mjs。\n */\n\n` +
    keepStyles.join("\n"));

// 断言：admin/内部壳用到的类重写后仍有定义（零缺失才允许退出 0）
const missingForAdmin = [...adminU].filter((c) => definedOriginal.has(c) && !definedNew.has(c));
const missingForInternal = [...internalU].filter((c) => definedOriginal.has(c) && !definedNew.has(c));
console.log("stats:", stats);
console.log("accessGlobals:", accessGlobals.length, "access:", accessOut.length, "legacy:", legacyOut.length);
console.log("admin 缺失类:", missingForAdmin.length ? missingForAdmin : "无 ✓");
console.log("internal 缺失类:", missingForInternal.length ? missingForInternal : "无 ✓");
if (missingForAdmin.length || missingForInternal.length) process.exit(1);
