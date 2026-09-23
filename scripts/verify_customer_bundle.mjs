#!/usr/bin/env node
/**
 * CW-019 客户构建制品排除断言（承接 CW-011「改入口前建立客户依赖图及管理代码排除断言」）
 *
 * 目标层级：AUTOMATED_VERIFIED。
 *
 * 本脚本扫描 `client/dist`（客户构建制品），执行两类断言并输出产物清单：
 *
 *  1) 产物清单：每个文件的相对路径 + 字节数 + SHA-256，打印到 stdout，
 *     即 CW-019 规格「必交证据：构建依赖清单、产物文件清单/哈希」的直接来源。
 *  2) 排除断言：
 *     a. 禁止文件名：dist 内不得出现 admin.html、admin-main*.js、任何名字含 "admin"
 *        的 chunk（.js/.css/.html/.mjs/.cjs），防止管理入口文件被卷进客户包。
 *     b. 禁止内容特征串（管理域）：AdminApp / api.admin / exchangeAdminSession /
 *        fetchAdminSession / loginAdminWithPassword / /api/control/ /
 *        X-Control-Proxy-Token / 激活码批次 / 审计中心 / 强制下线 /
 *        总览仪表盘 / 资金流水。
 *     c. 禁止内容特征串（内部入口域）：getDevelopmentUserId / X-Dev-User-Id /
 *        internalAccessToken（CW-015 后这些应已从客户入口链路消失）。
 *  3) 前置校验：`client/dist` 不存在时明确报错退出 1，不得静默通过
 *     （对齐 CW-007「缺库不得伪绿」的同源原则）。同时校验产物结构：
 *     `index.html` 必须存在且非空、`assets/` 下至少有一个 `.js`，否则一个只含
 *     favicon 的空壳目录也会「通过」全部排除断言。
 *  4) 退出码：任一断言失败 → 打印命中文件与特征串 → process.exit(1)。
 *  5) 阳性对照（positive control）：上面 2b 的管理域特征串必须在**管理制品**
 *     `client/dist-admin` 里至少命中一条，否则说明这些特征串已随构建配置腐烂
 *     （见下），"客户制品 0 命中" 就失去证据价值 → 退出 1。
 *     因此本脚本要求 `client/dist-admin` 存在：请先 `npm run build:all`
 *     （而不是只 `npm run build`）。缺阳性对照样本即等同缺库伪绿。
 *  6) 包含向断言（ADMIN-BUNDLE-CSS-CONTRACT-20260912）：管理制品 CSS 必须含
 *     基础样式表标记（.admin-shell{ 与 --admin-bg）——排除断言发现不了
 *     「管理制品缺自身依赖」的半裸渲染回归（CW-019 双入口拆分实际发生过）。
 *
 * 关于「压缩后失效的特征串」（重要，勿误删也勿误信）：
 * `build.minify: "oxc"` 会 mangle 局部标识符，所以 `AdminApp`、
 * `exchangeAdminSession`、`fetchAdminSession`、`loginAdminWithPassword`、
 * `api.admin`、`internalAccessToken` 这类**标识符形状**的特征串在压缩产物里
 * 根本不存在；`X-Control-Proxy-Token` 由 nginx 注入、客户端代码从不出现；
 * `X-Dev-User-Id` 只存在于类型声明与注释，编译期即擦除；`getDevelopmentUserId`
 * 在 CW-015 后已从活代码删除。它们按 CW-019 交接文档 §5.5 的原始清单保留在
 * 产物扫描里（不放宽前三条：万一将来关掉 minify 或改动构建，它们立刻恢复
 * 检测力），但**真正有齿的层级是源码级合同测试** `client/src/entryContract.test.ts`
 * ——源码不压缩，标识符原样可见。真正在压缩产物里存活的是字符串字面量：
 * `/api/control/`、`总览仪表盘`、`审计中心`、`资金流水`、`运营管理后台`、`ASX1.`，
 * 这六条构成阳性对照集合（POSITIVE_CONTROL_NEEDLES）。
 *
 * 与源码级合同测试（`client/src/entryContract.test.ts`）形成双层保护：
 * 源码不含管理引用（vitest 秒级）→ 产物不含管理特征串（本脚本，构建后），
 * 且产物层断言由阳性对照自证有效。
 *
 * 用法：
 *   node scripts/verify_customer_bundle.mjs             # 从仓库根目录
 *   node ../scripts/verify_customer_bundle.mjs          # 从 client 目录（npm run verify:customer-bundle）
 *
 * 依赖：零第三方依赖，只用 Node 原生 node:fs / node:path / node:crypto / node:url。
 */

import { createHash } from "node:crypto";
import { lstatSync, readdirSync, readFileSync } from "node:fs";
import { relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

// ─────────────────────────────────────────────────────────────────────────────
// 路径解析：以脚本自身位置为基准，不受调用方 cwd 影响。
// 脚本位于 <repo>/scripts/verify_customer_bundle.mjs，客户制品在 <repo>/client/dist，
// 管理制品（阳性对照样本）在 <repo>/client/dist-admin。
// ─────────────────────────────────────────────────────────────────────────────
const scriptDir = fileURLToPath(new URL(".", import.meta.url));
const repoRoot = resolve(scriptDir, "..");
const customerDistDir = resolve(repoRoot, "client", "dist");
const adminDistDir = resolve(repoRoot, "client", "dist-admin");

// ─────────────────────────────────────────────────────────────────────────────
// 断言常量：与 §5.5 的禁止清单严格对应。
// ─────────────────────────────────────────────────────────────────────────────

/** 禁止文件名（精确匹配或前缀匹配，均大小写敏感，与 Vite 产物一致）。 */
const FORBIDDEN_FILE_NAMES = Object.freeze({
  exact: new Set(["admin.html"]),
  prefixes: ["admin-main"],
});

/** 任何名字（去扩展名后）含 "admin" 的 chunk 都被视为管理入口渗透。
 *  用不区分大小写的 substring 匹配，覆盖 admin/Admin/ADMIN 变体。 */
const FORBIDDEN_CHUNK_NAME_TOKEN = "admin";

/** 会被扫描内容的文本产物扩展名。二进制资源（图片/字体/媒体）只入清单不扫描内容。 */
const TEXT_EXTENSIONS = new Set([
  ".js",
  ".mjs",
  ".cjs",
  ".css",
  ".html",
  ".map",
  ".json",
  ".svg",
  ".txt",
]);

/** 禁止内容特征串（管理域）。命中即证明客户制品包含管理业务代码或其路由。
 *  CW-019 交接文档 §5.5 item 3 的原始清单逐条保留、不放宽；后续追加
 *  `运营管理后台`、`ASX1.`、`总览仪表盘`、`资金流水`。其中后两条替代已退出
 *  当前管理制品的旧文案，只用于恢复阳性对照，不删除旧禁止项。 */
const FORBIDDEN_CONTENT_ADMIN = Object.freeze([
  "AdminApp",
  "api.admin",
  "exchangeAdminSession",
  "fetchAdminSession",
  "loginAdminWithPassword",
  "/api/control/",
  "X-Control-Proxy-Token",
  "激活码批次",
  "总览仪表盘",
  "审计中心",
  "强制下线",
  "资金流水",
  "运营管理后台",
  "ASX1.",
]);

/** 阳性对照集合：必须是 FORBIDDEN_CONTENT_ADMIN 的子集，且每一条都被实测证明
 *  在管理制品 `client/dist-admin` 中命中 ≥1 次（2026-09-10，Vite 8.2.1 +
 *  minify: "oxc"）。若某条在管理制品里也命中 0 次，说明特征串已腐烂
 *  （构建配置变更、文案改写、压缩策略改变），此时"客户制品 0 命中"不再构成
 *  证据 → 退出 1，强制维护者更新清单。这是消灭「断言静默失效」这一整类
 *  伪绿的关键：needle 失效与制品干净在输出上原本完全同形。 */
const POSITIVE_CONTROL_NEEDLES = Object.freeze([
  "/api/control/",
  "总览仪表盘",
  "审计中心",
  "资金流水",
  "运营管理后台",
  "ASX1.",
]);

/** 禁止内容特征串（内部入口域）。CW-015 后应已从客户入口链路消失；
 *  若在内部 P0 兼容路径仍合法存在，PR 内说明依据后再调整，不得为过门禁而放宽。
 *  实测三条在压缩产物里均恒不命中（标识符被 mangle / 仅存在于类型与注释 /
 *  活代码已删），因此产物层没有可用的阳性对照样本。它们按 §5.5 item 4 原文保留
 *  在此（不删、不放宽），实际检测力由源码级 `entryContract.test.ts` 承担：断言
 *  内部壳 `client/src/App.tsx` 文件不存在（已于泳道 A 死代码清理中删除），
 *  且客户入口 `main.tsx` / `RootApp.tsx` 不引用该壳。 */
const FORBIDDEN_CONTENT_INTERNAL = Object.freeze([
  "getDevelopmentUserId",
  "X-Dev-User-Id",
  "internalAccessToken",
]);

// ─────────────────────────────────────────────────────────────────────────────
// 前置校验：制品目录必须存在且是真实目录，否则明确失败（不得静默通过）。
// 用 lstatSync 而非 statSync：statSync 跟随符号链接，`client/dist` 本身指向别处
// 时照样通过，而 walk() 只拒绝目录**内部**的软链，根节点会漏网。
// ─────────────────────────────────────────────────────────────────────────────
function assertArtifactDir(dirAbs, label, buildHint) {
  let stats;
  try {
    stats = lstatSync(dirAbs);
  } catch (err) {
    console.error(`[verify_customer_bundle] ${label}不存在：${dirAbs}`);
    console.error(`[verify_customer_bundle] ${buildHint}`);
    console.error(`[verify_customer_bundle] 原始错误：${err.message}`);
    process.exit(1);
  }
  if (stats.isSymbolicLink()) {
    console.error(
      `[verify_customer_bundle] ${label}是符号链接（拒绝处理，避免哈希歧义）：${dirAbs}`,
    );
    process.exit(1);
  }
  if (!stats.isDirectory()) {
    console.error(
      `[verify_customer_bundle] ${label}不是目录：${dirAbs}`,
    );
    process.exit(1);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 递归遍历：收集所有产物文件的绝对路径，按 POSIX 相对路径排序，输出稳定清单。
// ─────────────────────────────────────────────────────────────────────────────
function walk(dirAbs, out = []) {
  for (const entry of readdirSync(dirAbs, { withFileTypes: true })) {
    const entryAbs = resolve(dirAbs, entry.name);
    if (entry.isDirectory()) {
      walk(entryAbs, out);
    } else if (entry.isFile()) {
      out.push(entryAbs);
    } else if (entry.isSymbolicLink()) {
      // 客户产物不应含符号链接；若出现，视为异常并直接失败，避免哈希歧义。
      console.error(
        `[verify_customer_bundle] 客户产物含符号链接（拒绝处理）：${entryAbs}`,
      );
      process.exit(1);
    }
  }
  return out;
}

function toPosixRelative(absPath, baseDir) {
  return relative(baseDir, absPath).split(sep).join("/");
}

function extname(pathname) {
  const idx = pathname.lastIndexOf(".");
  return idx === -1 ? "" : pathname.slice(idx).toLowerCase();
}

function basenameNoExt(pathname) {
  const base = pathname.slice(pathname.lastIndexOf("/") + 1);
  const dot = base.lastIndexOf(".");
  return dot === -1 ? base : base.slice(0, dot);
}

// ─────────────────────────────────────────────────────────────────────────────
// 断言 1：文件名（chunk 名）不得含管理入口标识。
// ─────────────────────────────────────────────────────────────────────────────
function checkForbiddenFileNames(relPaths) {
  const hits = [];
  for (const rel of relPaths) {
    const base = rel.slice(rel.lastIndexOf("/") + 1);
    const baseNoExt = basenameNoExt(rel);

    if (FORBIDDEN_FILE_NAMES.exact.has(base)) {
      hits.push({ file: rel, reason: `禁止文件名（精确）：${base}` });
      continue;
    }
    if (
      FORBIDDEN_FILE_NAMES.prefixes.some(
        (prefix) => base === prefix || base.startsWith(`${prefix}-`) || base.startsWith(`${prefix}.`),
      )
    ) {
      hits.push({ file: rel, reason: `禁止文件名（前缀）：${base}` });
      continue;
    }
    // 只对 chunk 类扩展名做 "admin" 名字扫描，避免误伤业务图片资源名。
    const ext = extname(rel);
    const isChunkLike =
      ext === ".js" ||
      ext === ".mjs" ||
      ext === ".cjs" ||
      ext === ".css" ||
      ext === ".html";
    if (
      isChunkLike &&
      baseNoExt.toLowerCase().includes(FORBIDDEN_CHUNK_NAME_TOKEN)
    ) {
      hits.push({
        file: rel,
        reason: `chunk 名含 "${FORBIDDEN_CHUNK_NAME_TOKEN}"：${baseNoExt}`,
      });
    }
  }
  return hits;
}

// ─────────────────────────────────────────────────────────────────────────────
// 断言 2：文本产物内容不得含管理/内部特征串。
// ─────────────────────────────────────────────────────────────────────────────

/** 读取产物文本；读不了直接失败，不得跳过（跳过 = 漏检 = 伪绿）。 */
function readArtifactText(abs, rel) {
  try {
    return readFileSync(abs, "utf8");
  } catch (err) {
    console.error(
      `[verify_customer_bundle] 无法读取产物文件：${rel} (${err.message})`,
    );
    process.exit(1);
  }
}

/** 只保留文本类扩展名的产物（二进制资源入清单但不扫内容）。 */
function collectTextArtifacts(absPaths, baseDir) {
  const collected = [];
  for (const abs of absPaths) {
    const rel = toPosixRelative(abs, baseDir);
    if (!TEXT_EXTENSIONS.has(extname(rel))) continue;
    collected.push({ rel, text: readArtifactText(abs, rel) });
  }
  return collected;
}

function checkForbiddenContent(textArtifacts) {
  const hits = [];
  for (const { rel, text } of textArtifacts) {
    for (const needle of FORBIDDEN_CONTENT_ADMIN) {
      if (text.includes(needle)) {
        hits.push({ file: rel, needle, domain: "admin" });
      }
    }
    for (const needle of FORBIDDEN_CONTENT_INTERNAL) {
      if (text.includes(needle)) {
        hits.push({ file: rel, needle, domain: "internal" });
      }
    }
  }
  return hits;
}

/** 统计每条特征串命中的**文件数**（阳性对照用；≥1 即证明该特征串仍有检测力）。 */
function countNeedleFiles(textArtifacts, needles) {
  const counts = new Map(needles.map((needle) => [needle, 0]));
  for (const { text } of textArtifacts) {
    for (const needle of needles) {
      if (text.includes(needle)) counts.set(needle, counts.get(needle) + 1);
    }
  }
  return counts;
}

// ─────────────────────────────────────────────────────────────────────────────
// 断言 3（F-01/P0-1）：客户制品 CSS 必须自带账户屏与老组件样式。
// styles.css 只服务管理端与内部壳，客户制品不含它；激活/登录/配对屏样式在
// customer/customer-access.css（RootApp 引入），深创作面板样式在
// legacy-panels.css（LiveWorkspacePanel 引入）。若这两类锚点在客户 CSS 产物
// 中缺席，说明拆分文件被移除或引入链断裂——历史上这正是激活页无样式渲染
// （品牌 SVG 铺满首屏）的根因。同时禁止任何 .admin- 选择器进入客户 CSS。
// ─────────────────────────────────────────────────────────────────────────────
const REQUIRED_CUSTOMER_CSS_NEEDLES = Object.freeze([
  ".customer-access", // 账户屏样式锚点
  ".stage-block", // 拆解工作台老组件样式锚点
  ".recharge-dialog", // 客户充值弹窗样式锚点（收款主链路）
]);
const FORBIDDEN_CUSTOMER_CSS_NEEDLE = ".admin-";

function checkCustomerStyleCoverage(textArtifacts) {
  const problems = [];
  const cssArtifacts = textArtifacts.filter(({ rel }) => {
    const base = rel.split("/").pop() ?? "";
    return rel.startsWith("assets/") && base.endsWith(".css");
  });
  if (cssArtifacts.length === 0) {
    problems.push("assets/ 下没有任何 CSS 产物（客户样式未打包）");
    return problems;
  }
  for (const needle of REQUIRED_CUSTOMER_CSS_NEEDLES) {
    const hit = cssArtifacts.some(({ text }) => text.includes(needle));
    if (!hit) {
      problems.push(
        `客户 CSS 产物缺少必需锚点 "${needle}"（账户屏/老组件样式未随包）`,
      );
    }
  }
  for (const { rel, text } of cssArtifacts) {
    if (text.includes(FORBIDDEN_CUSTOMER_CSS_NEEDLE)) {
      problems.push(`客户 CSS 产物包含管理域选择器 "${FORBIDDEN_CUSTOMER_CSS_NEEDLE}"：${rel}`);
    }
  }
  return problems;
}

// ─────────────────────────────────────────────────────────────────────────────
// 产物清单 + 哈希：SHA-256 + 字节数 + POSIX 相对路径，按路径字典序稳定输出。
// ─────────────────────────────────────────────────────────────────────────────
function buildManifest(absPaths, baseDir) {
  const entries = absPaths.map((abs) => {
    const buf = readFileSync(abs);
    const hash = createHash("sha256").update(buf).digest("hex");
    return {
      rel: toPosixRelative(abs, baseDir),
      bytes: buf.byteLength,
      sha256: hash,
    };
  });
  entries.sort((a, b) => (a.rel < b.rel ? -1 : a.rel > b.rel ? 1 : 0));
  return entries;
}

function printManifest(entries, label) {
  // 表头不写死「customer bundle」：本函数同时为客户制品与管理制品（阳性对照样本）
  // 打印清单，label 才是制品身份的唯一来源。
  console.log(`# CW-019 build artifact manifest (${label})`);
  console.log("# sha256                                                            bytes  path");
  for (const { sha256, bytes, rel } of entries) {
    console.log(`${sha256}  ${String(bytes).padStart(7)}  ${rel}`);
  }
  console.log(`# total files: ${entries.length}`);
}

/** 产物结构正向断言：只断言「不含管理代码」不够——一个只含 favicon.svg 的空壳
 *  dist 会通过全部排除断言并报 ✅。这里要求客户制品真的是一个可用的 SPA，
 *  与 deploy/customer-git-rollout.sh 的 `[[ -s index.html && -d assets ]]` 同规格。 */
function assertCustomerArtifactShape(manifest) {
  const problems = [];
  const indexEntry = manifest.find((entry) => entry.rel === "index.html");
  for (const asset of ["studio/logo-mark.svg", "favicon.svg", "favicon.png", "favicon.ico"]) {
    if (!manifest.some((entry) => entry.rel === asset && entry.bytes > 0)) {
      problems.push(`品牌资源缺失或为空：${asset}`);
    }
  }
  if (!indexEntry) {
    problems.push("index.html 缺失（客户 SPA 入口不存在）");
  } else if (indexEntry.bytes === 0) {
    problems.push("index.html 为空文件");
  }
  const jsAssets = manifest.filter(
    (entry) => entry.rel.startsWith("assets/") && entry.rel.endsWith(".js"),
  );
  if (jsAssets.length === 0) {
    problems.push("assets/ 下没有任何 .js（客户 bundle 未产出）");
  }
  if (problems.length > 0) {
    console.error("");
    console.error(
      "[verify_customer_bundle] ❌ 客户构建制品结构不完整，排除断言无意义：",
    );
    for (const problem of problems) {
      console.error(`  ${problem}`);
    }
    console.error(
      "[verify_customer_bundle] 请先执行 `npm run build`（客户构建）再运行本断言；" +
        "残缺产物通过检查等同 CW-007 禁止的缺库伪绿。",
    );
    process.exit(1);
  }
}

/** 阳性对照：管理域特征串必须在管理制品 client/dist-admin 中命中 ≥1 个文件。
 *  命中 0 说明特征串已随构建配置/文案/压缩策略腐烂，此时「客户制品 0 命中」
 *  不再是证据。管理制品缺失同样失败——没有阳性样本就无法证明 assays 有效。 */
function runPositiveControl() {
  assertArtifactDir(
    adminDistDir,
    "管理构建制品（阳性对照样本）目录",
    "请先执行 `npm run build:all`（同时产出客户与管理制品）再运行本断言；" +
      "缺阳性对照样本即等同缺库伪绿，无法证明特征串仍有检测力。",
  );
  const adminAbsPaths = walk(adminDistDir);
  if (adminAbsPaths.length === 0) {
    console.error(
      `[verify_customer_bundle] 管理构建制品目录为空：${adminDistDir}`,
    );
    console.error(
      "[verify_customer_bundle] 空目录不得视为通过（等同缺库伪绿）。",
    );
    process.exit(1);
  }
  // 两份制品清单都要进 stdout：任务证据（账本 §14 / CW-019-EVIDENCE.md）以本脚本
  // 输出为构建依赖清单的唯一来源，只印客户侧会让管理侧产物无哈希可追溯。
  printManifest(buildManifest(adminAbsPaths, adminDistDir), "client/dist-admin");

  const adminTexts = collectTextArtifacts(adminAbsPaths, adminDistDir);
  if (adminTexts.length === 0) {
    console.error(
      `[verify_customer_bundle] 管理构建制品无文本产物：${adminDistDir}`,
    );
    process.exit(1);
  }
  const counts = countNeedleFiles(adminTexts, POSITIVE_CONTROL_NEEDLES);
  const rotten = POSITIVE_CONTROL_NEEDLES.filter(
    (needle) => counts.get(needle) === 0,
  );

  console.log("");
  console.log("# CW-019 positive control (client/dist-admin)");
  console.log("# 每条管理域特征串在管理制品中命中的文件数（必须 ≥1）");
  for (const needle of POSITIVE_CONTROL_NEEDLES) {
    console.log(`  ${String(counts.get(needle)).padStart(3)}  "${needle}"`);
  }

  if (rotten.length > 0) {
    console.error("");
    console.error(
      "[verify_customer_bundle] ❌ 阳性对照失败：以下管理域特征串在管理制品中也命中 0 次，" +
        "说明它们已失去检测力（构建/压缩/文案变更），客户制品的「0 命中」不再构成证据：",
    );
    for (const needle of rotten) {
      console.error(`  "${needle}"`);
    }
    console.error(
      "[verify_customer_bundle] 处理方式：确认管理端确实不再包含该特征后，" +
        "同步更新 FORBIDDEN_CONTENT_ADMIN 与 POSITIVE_CONTROL_NEEDLES，" +
        "并在 PR 内说明依据。不得直接删除阳性对照。",
    );
    process.exit(1);
  }
  return counts;
}

/**
 * ADMIN-BUNDLE-CSS-CONTRACT-20260912：双向合同的「包含向」断言。
 *
 * 排除断言只证明「客户制品不含管理代码」；CW-019 拆分双入口后 styles.css
 * 只剩客户壳 App.tsx 一处导入，管理制品整份缺失基础样式表（.admin-shell
 * 布局、--admin-* 令牌定义），产线半裸渲染时全部既有断言依旧全绿。这里
 * 钉住两个在 CSS 压缩后仍字面存活的最小标记（壳层选择器 + 令牌定义），
 * 缺任一即失败。源码级契约见 client/src/entryContract.test.ts。
 */
const ADMIN_BASE_STYLESHEET_NEEDLES = Object.freeze([
  ".admin-shell{",
  "--admin-bg",
]);

function runAdminBaseStylesheetControl() {
  assertArtifactDir(
    adminDistDir,
    "管理构建制品（包含向断言样本）目录",
    "请先执行 `npm run build:all`（同时产出客户与管理制品）再运行本断言。",
  );
  const cssAbsPaths = walk(adminDistDir).filter((abs) => abs.endsWith(".css"));
  const missing = ADMIN_BASE_STYLESHEET_NEEDLES.filter(
    (needle) =>
      !cssAbsPaths.some((abs) => readFileSync(abs, "utf8").includes(needle)),
  );
  if (missing.length > 0) {
    console.error("");
    console.error(
      "[verify_customer_bundle] ❌ 管理制品缺失基础样式表标记（半裸渲染回归）：",
    );
    for (const needle of missing) {
      console.error(`  "${needle}" 在 client/dist-admin/**/*.css 中 0 命中`);
    }
    console.error(
      '  处理方式：检查 client/src/admin-main.tsx 是否仍显式 import "./styles.css"；' +
        "源码级契约见 client/src/entryContract.test.ts。",
    );
    process.exit(1);
  }
  console.log("");
  console.log("# ADMIN-BUNDLE-CSS-CONTRACT positive control (client/dist-admin)");
  console.log("# 基础样式表标记在管理制品 CSS 中的命中（必须全部 ≥1）");
  for (const needle of ADMIN_BASE_STYLESHEET_NEEDLES) {
    const hits = cssAbsPaths.filter((abs) =>
      readFileSync(abs, "utf8").includes(needle),
    ).length;
    console.log(`  ${String(hits).padStart(3)}  "${needle}"`);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 主流程
// ─────────────────────────────────────────────────────────────────────────────
export function assertProductionPublicAssets(manifest) {
  const required = [
    "favicon.svg",
    "favicon.png",
    "favicon.ico",
    "studio/brand.png",
    "studio/logo-mark.svg",
    "platforms/douyin.ico",
    "platforms/wechat_channels.ico",
    "platforms/xiaohongshu.ico",
  ];
  for (const rel of required) {
    const source = readFileSync(resolve(repoRoot, "client/public", rel));
    const expected = createHash("sha256").update(source).digest("hex");
    const built = manifest.find((entry) => entry.rel === rel);
    if (!built || built.bytes === 0 || built.sha256 !== expected) {
      throw new Error(`Production public asset missing or changed: ${rel}`);
    }
  }
  const reviewImages = manifest.filter(
    (entry) => entry.rel.startsWith("studio/") && !required.includes(entry.rel),
  );
  if (reviewImages.length > 0) {
    throw new Error(`Review-only assets in customer bundle: ${reviewImages.map((entry) => entry.rel).join(", ")}`);
  }
  console.log("# PUBLIC-ASSETS: production brand and official logos verified; review images excluded");
}

function main() {
  assertArtifactDir(
    customerDistDir,
    "客户构建制品目录",
    "请先执行 `npm run build:all`（同时产出客户与管理制品）再运行本断言；" +
      "缺库伪绿等同 CW-007 禁止的 skip 通过。",
  );

  const absPaths = walk(customerDistDir);
  if (absPaths.length === 0) {
    console.error(
      `[verify_customer_bundle] 客户构建制品目录为空：${customerDistDir}`,
    );
    console.error(
      "[verify_customer_bundle] 空目录不得视为通过（等同缺库伪绿）。",
    );
    process.exit(1);
  }

  const manifest = buildManifest(absPaths, customerDistDir);
  printManifest(manifest, "client/dist");
  assertCustomerArtifactShape(manifest);
  assertProductionPublicAssets(manifest);

  // 阳性对照先跑：特征串一旦腐烂，后面的「0 命中」就毫无证据价值，
  // 让它先失败可以最早暴露问题，而不是在一份无效报告末尾附注。
  const positiveCounts = runPositiveControl();

  // 包含向断言（ADMIN-BUNDLE-CSS-CONTRACT）：管理制品必须自带基础样式表，
  // 同样先于排除断言执行——「半裸渲染」是最严重的现场回归，让它最早失败。
  runAdminBaseStylesheetControl();

  const relPaths = manifest.map((entry) => entry.rel);
  const nameHits = checkForbiddenFileNames(relPaths);
  const customerTexts = collectTextArtifacts(absPaths, customerDistDir);
  const contentHits = checkForbiddenContent(customerTexts);
  const styleCoverageProblems = checkCustomerStyleCoverage(customerTexts);

  if (nameHits.length > 0 || contentHits.length > 0) {
    console.error("");
    console.error(
      "[verify_customer_bundle] ❌ 客户构建制品未通过管理/内部代码排除断言：",
    );
    if (nameHits.length > 0) {
      console.error("");
      console.error("── 禁止文件名命中 ──");
      for (const { file, reason } of nameHits) {
        console.error(`  ${file}: ${reason}`);
      }
    }
    if (contentHits.length > 0) {
      console.error("");
      console.error("── 禁止内容特征串命中 ──");
      // 按 file → needle 分组，便于运维快速定位泄漏源。
      const byFile = new Map();
      for (const { file, needle, domain } of contentHits) {
        if (!byFile.has(file)) byFile.set(file, []);
        byFile.get(file).push({ needle, domain });
      }
      for (const [file, needles] of byFile) {
        console.error(`  ${file}:`);
        for (const { needle, domain } of needles) {
          console.error(`    [${domain}] "${needle}"`);
        }
      }
    }
    if (styleCoverageProblems.length > 0) {
      console.error("");
      console.error("── 客户样式覆盖断言失败（F-01/P0-1）──");
      for (const problem of styleCoverageProblems) {
        console.error(`  ${problem}`);
      }
    }
    console.error("");
    console.error(
      "[verify_customer_bundle] 客户所有 chunk 不得含内部/管理入口（CW-019 验收底线）；" +
        "命中即证明物理分离未生效，禁止用懒加载隐藏代替排除。",
    );
    process.exit(1);
  }

  console.log("");
  console.log(
    "[verify_customer_bundle] ✅ 客户构建制品通过管理/内部代码排除断言。",
  );
  console.log(
    `[verify_customer_bundle] 客户样式覆盖：账户屏/老组件锚点齐备，管理域选择器 0 命中（F-01/P0-1）。`,
  );
  console.log(
    `[verify_customer_bundle] 扫描文件数：${manifest.length}；禁止文件名命中：0；禁止特征串命中：0。`,
  );
  const effectiveNeedles = [...positiveCounts.values()].filter(
    (count) => count > 0,
  ).length;
  console.log(
    `[verify_customer_bundle] 阳性对照：${effectiveNeedles}/${POSITIVE_CONTROL_NEEDLES.length} ` +
      "条管理域特征串在 client/dist-admin 中命中 ≥1 文件，排除断言的有效性已自证。",
  );
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
