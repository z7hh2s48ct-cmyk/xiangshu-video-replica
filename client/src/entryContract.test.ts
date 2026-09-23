/// <reference types="node" />
// @vitest-environment node

import { createHash } from "node:crypto";
import { copyFileSync, existsSync, readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import viteConfig from "../vite.config";

vi.mock("node:fs", async (importOriginal) => ({
  ...(await importOriginal<typeof import("node:fs")>()),
  copyFileSync: vi.fn(),
  cpSync: vi.fn(),
  mkdirSync: vi.fn(),
}));

it("生产品牌资源通过校验，缺失、篡改图形或混入审核图仍被拒绝", async () => {
  const { assertProductionPublicAssets } = await import(
    new URL("../../scripts/verify_customer_bundle.mjs", import.meta.url).href
  );
  const manifest = [
    "favicon.svg",
    "favicon.png",
    "favicon.ico",
    "studio/brand.png",
    "studio/logo-mark.svg",
    "platforms/douyin.ico",
    "platforms/wechat_channels.ico",
    "platforms/xiaohongshu.ico",
  ].map((rel) => {
    const content = readFileSync(new URL(`../public/${rel}`, import.meta.url));
    return {
      rel,
      bytes: content.length,
      sha256: createHash("sha256").update(content).digest("hex"),
    };
  });
  expect(() => assertProductionPublicAssets(manifest)).not.toThrow();
  expect(() =>
    assertProductionPublicAssets(
      manifest.filter((item) => item.rel !== "studio/logo-mark.svg"),
    ),
  ).toThrow(/missing or changed/);
  expect(() =>
    assertProductionPublicAssets(
      manifest.map((item) =>
        item.rel === "studio/logo-mark.svg"
          ? { ...item, sha256: "wrong" }
          : item,
      ),
    ),
  ).toThrow(/missing or changed/);
  expect(() =>
    assertProductionPublicAssets([
      ...manifest,
      { rel: "studio/review-only.png", bytes: 1, sha256: "test" },
    ]),
  ).toThrow(/Review-only assets/);
});

it("客户正式构建复制侧栏图形与浏览器图标", () => {
  const plugin = viteConfig.plugins
    ?.flat()
    .find(
      (item) =>
        item &&
        typeof item === "object" &&
        "name" in item &&
        item.name === "customer-public-assets",
    );
  if (
    !plugin ||
    typeof plugin !== "object" ||
    !("writeBundle" in plugin) ||
    typeof plugin.writeBundle !== "function"
  ) {
    throw new Error("customer-public-assets must expose writeBundle");
  }
  Reflect.apply(plugin.writeBundle, {}, [{}, {}]);
  const destinations = vi
    .mocked(copyFileSync)
    .mock.calls.map(([, target]) => String(target).replaceAll("\\", "/"));
  for (const asset of [
    "studio/logo-mark.svg",
    "favicon.svg",
    "favicon.png",
    "favicon.ico",
  ]) {
    expect(destinations.some((path) => path.endsWith(`/dist/${asset}`))).toBe(
      true,
    );
  }
});

/**
 * CW-019 客户/管理独立构建制品 · 源码级入口合同测试
 *
 * 承接 CW-011「改入口前建立客户依赖图及管理代码排除断言」的第一层（源码层）：
 * 用 vitest 直接读取入口与共享组件的源码文本，断言客户依赖图不认识管理域与
 * 内部域的模块引用。产物级断言由 `scripts/verify_customer_bundle.mjs` 承担
 * （第二层，构建后扫描 dist 并做阳性对照）。
 *
 * 两层结合，形成「源码不含管理引用 → 产物不含管理特征串」的双向合同，
 * 与 CW-019 验收底线「客户所有 chunk 不得含内部/管理入口」直接对应。
 *
 * 源码层还承担一部分**产物层做不到**的断言，原因见各用例注释：
 *  - 标识符形状的管理 API 名（AdminApp / exchangeAdminSession / …）会被
 *    `minify: "oxc"` mangle，在压缩产物里根本不存在；
 *  - 内部壳 `./App` 只被测试引用、不进任何生产制品，产物层没有阳性对照样本；
 *  - `SettingsPanel` 的控制面注入契约一旦回退成组件内运行时三元分支，
 *    打包器就会保留两侧引用，而 Rolldown 忽略全部 tree-shake 配置。
 *
 * 这些断言在开发期秒级反馈（无需 build），CI Linux quality gate 里
 * `npm run check` 会通过 vitest 覆盖到本文件。
 */

function readSource(relativePath: string): string {
  // import.meta.url 是本测试文件的 file:// URL，用它拼接兄弟文件路径
  // 不受 vitest 启动 cwd 影响，跨工作区/子进程稳定。
  return readFileSync(new URL(relativePath, import.meta.url), "utf8");
}

/**
 * 读取源码并剔除整行注释，供**形状类**正则（如 JSX 标签）使用。
 *
 * 动机：这些文件的 docblock 会正当地叙述设计边界与历史，其中就会出现被断言
 * 的形状本身。CW-019 开发期实测踩过两次——main.tsx 的注释提到 "admin-main.tsx"
 * 字样被宽松断言误伤；RootApp.tsx 的注释写「internal `<App/>` fallback is
 * deleted」被 `<App[\s/>]` 命中。合法文档不该被判成管理/内部代码回流。
 *
 * 逐行剔除 `//`、`*`、`/*` 开头的行是粗粒度近似（不解析块注释边界），
 * 对本文件的用途足够：断言目标是「代码里不出现该形状」，而注释里的形状
 * 一律不构成模块依赖，剔除它们不会漏掉真实回归。
 */
function readCodeOnly(relativePath: string): string {
  return readSource(relativePath)
    .split("\n")
    .filter((line) => {
      const trimmed = line.trimStart();
      return (
        !trimmed.startsWith("//") &&
        !trimmed.startsWith("*") &&
        !trimmed.startsWith("/*")
      );
    })
    .join("\n");
}

// 断言只匹配**真正的模块引用语法**（静态 import / 动态 import / require）与
// JSX 使用形态，不匹配裸标识符。理由：这些文件的 docblock 会正当地叙述
// 「AdminApp 已迁至 admin-main.tsx」这类历史与边界，裸标识符断言会把合法注释
// 误判成管理代码回流。CW-019 开发期已实测踩过一次——main.tsx 的说明注释提到
// "admin-main.tsx" 字样，被宽松断言误伤。
const ADMIN_APP_MODULE_REF =
  /(?:from|import|require)\s*\(?\s*["'][^"']*\/AdminApp["']/;
const ADMIN_APP_JSX = /<AdminApp[\s/>]/;
const ROOT_APP_MODULE_REF =
  /(?:from|import|require)\s*\(?\s*["'][^"']*\/RootApp["']/;
const ROOT_APP_JSX = /<RootApp[\s/>]/;
const ADMIN_MAIN_MODULE_REF =
  /(?:from|import|require)\s*\(?\s*["'][^"']*admin-main["']/;
/** 内部壳 `client/src/App.tsx`（内部访问令牌界面）。 */
const INTERNAL_SHELL_MODULE_REF =
  /(?:from|import|require)\s*\(?\s*["']\.\/App["']/;
const INTERNAL_SHELL_JSX = /<App[\s/>]/;

describe("CW-019 customer entry contract (source-level)", () => {
  it("客户唯一入口 RootApp.tsx 不再引用 AdminApp", () => {
    const source = readSource("./RootApp.tsx");
    expect(source).not.toMatch(ADMIN_APP_MODULE_REF);
    expect(source).not.toMatch(ADMIN_APP_JSX);
    // 懒加载形态同属模块引用，上面的正则已覆盖 import("./AdminApp")；
    // 这里再显式钉一次，避免将来改写正则时静默丢掉懒加载分支。
    expect(source).not.toMatch(/import\(\s*["'][^"']*\/AdminApp["']\s*\)/);
  });

  it("客户唯一入口 RootApp.tsx 不再分支到 /admin 路径", () => {
    const source = readSource("./RootApp.tsx");
    // CW-019 拆包后，/admin 由独立管理制品（dist-admin）提供，
    // 客户 RootApp 命中 /admin 时走默认客户壳（降级但不泄露管理代码）。
    expect(source).not.toMatch(/path\s*===\s*["']\/admin["']/);
    expect(source).not.toMatch(/path\.startsWith\(\s*["']\/admin\/?["']\s*\)/);
  });

  it("客户入口挂载文件 main.tsx 只挂载 RootApp，不含 AdminApp", () => {
    const source = readSource("./main.tsx");
    expect(source).toMatch(ROOT_APP_MODULE_REF);
    expect(source).toMatch(ROOT_APP_JSX);
    expect(source).not.toMatch(ADMIN_APP_MODULE_REF);
    expect(source).not.toMatch(ADMIN_APP_JSX);
    // 客户入口不引用管理端挂载文件。注释里叙述 admin-main.tsx 的路径是允许且
    // 必要的（说明双入口边界），只要不形成模块依赖即可。
    expect(source).not.toMatch(ADMIN_MAIN_MODULE_REF);
  });

  it("管理端独立挂载文件 admin-main.tsx 存在且只挂载 AdminApp", () => {
    const source = readSource("./admin-main.tsx");
    expect(source).toMatch(ADMIN_APP_MODULE_REF);
    expect(source).toMatch(ADMIN_APP_JSX);
    // 管理入口不引用 RootApp / 客户壳：物理分离，不共享挂载路径。
    expect(source).not.toMatch(ROOT_APP_MODULE_REF);
    expect(source).not.toMatch(ROOT_APP_JSX);
    // 挂载点缺失必须抛错，措辞与 main.tsx 一致，便于运维定位。
    expect(source).toMatch(/找不到应用挂载节点/);
  });

  it("管理端入口必须引入共享基础样式表 styles.css", () => {
    // ADMIN-UI-AUDIT-20260911：CW-019 拆分双入口后，styles.css 只剩客户壳
    // App.tsx 一处导入；管理端产物（dist-admin）随之缺失 .admin-shell 布局、
    // 全局 input/button 规则和 --admin-* 设计令牌定义，页面级 admin-*.css
    // 引用的这些变量全部落空，管理后台在产线以半裸样式渲染。源码层契约：
    // 管理端挂载文件必须显式导入基础样式表，防止再回归。
    const source = readCodeOnly("./admin-main.tsx");
    expect(source).toMatch(/import\s+"\.\/styles\.css";/);
  });

  it("管理端独立入口 HTML admin.html 存在且引用 admin-main.tsx", () => {
    const source = readSource("../admin.html");
    expect(source).toMatch(/admin-main\.tsx/);
    // 管理入口不得引用客户 main.tsx（避免共享挂载链）。
    expect(source).not.toMatch(/["']\/src\/main\.tsx["']/);
    // 挂载点 id 与 admin-main.tsx 一致，且沿用与客户 index.html 相同的
    // 结构约定（div#root），便于运维复用现有 nginx / Tauri 排障经验。
    expect(source).toMatch(/id=["']root["']/);
  });

  it("泳道 A 内部壳 App.tsx 已删除，客户入口不得重新引入", () => {
    // 死代码清理：内部壳 client/src/App.tsx 及其配套的 AnalysisWorkspace /
    // ProjectDetailFlow / CharacterReferenceSelection / GenerationComposer /
    // GenerationLauncher / ScriptEditor / WorkspaceTabs 已整体删除——CW-019 后
    // 它们已不可达（main.tsx 只挂载 RootApp）。
    //
    // 原断言「客户入口不引用 ./App」在文件删除后恒真、失去检测力，故改为直接
    // 断言文件不存在：一旦有人把内部访问令牌界面加回来（CW-013 红线），
    // 本断言立刻失败。
    expect(
      existsSync(new URL("./App.tsx", import.meta.url)),
      "内部壳 App.tsx 不得复活",
    ).toBe(false);
    // 双保险：客户入口仍不得以任何形式引用内部壳。
    // INTERNAL_SHELL_JSX 是形状类正则，故读剔注释版本（RootApp.tsx 的 docblock
    // 正当地叙述了 `<App/>` fallback 已删除这一历史）。
    for (const entry of ["./main.tsx", "./RootApp.tsx"]) {
      const source = readCodeOnly(entry);
      expect(source, `${entry} 不得引用内部壳 ./App`).not.toMatch(
        INTERNAL_SHELL_MODULE_REF,
      );
      expect(source, `${entry} 不得渲染内部壳 <App/>`).not.toMatch(
        INTERNAL_SHELL_JSX,
      );
    }
  });

  it("SettingsPanel 不静态引用控制面 API（控制面后端必须由管理端注入）", () => {
    // CW-019 的核心不变量。客户入口经
    // CustomerWorkspace → StudioWorkspace → SettingsPanel 静态复用本组件，
    // 只要组件内出现 `source === "control" ? getControlSettings() : getSettings()`
    // 这类运行时三元分支，打包器就会保留两侧引用，/api/control/ 立刻串进客户
    // 制品。构建层补救不了：实测 Vite 8.2.1 / Rolldown 1.2.4 忽略
    // rollupOptions.treeshake——sideEffects hint、moduleSideEffects 函数式、
    // 全局 false 三种写法的产物哈希字节级相同。所以用秒级源码断言守住，
    // 不必等一次完整 build 才发现回归。
    //
    // 本组件的 docblock 正当地叙述了控制面 API 名与设计动机，故先剔除注释行
    // 再匹配，避免把合法文档误判成回归（同 ADMIN_APP_* 收窄为模块引用的动机）。
    const codeOnly = readCodeOnly("./SettingsPanel.tsx");
    expect(codeOnly).not.toMatch(/\b(?:get|update|test)Control[A-Za-z]*\b/);
    // 工作台面后端内置于本模块；控制面后端只能以 prop 名出现，由管理端注入。
    expect(codeOnly).toMatch(/\bworkspaceBackend\b/);
    expect(codeOnly).toMatch(/\bcontrolBackend\b/);
  });

  it("客户账户屏样式必须随客户入口加载（customer-access.css）", () => {
    // F-01/P0-1（前端分析报告 2026-09-12）：styles.css 仅由管理入口
    // （admin-main.tsx）与内部死壳（App.tsx）引入，客户制品不含它。全局
    // reset、:root 令牌与激活/登录/配对等账户屏样式已拆入
    // customer/customer-access.css，必须由 RootApp 显式加载；否则激活页以
    // 无样式 HTML 渲染（历史实测：品牌 SVG 以 1264px 原始尺寸铺满首屏，
    // 表单被推出首屏之外）。
    expect(readSource("./RootApp.tsx")).toMatch(
      /import\s+"\.\s*\/customer\/customer-access\.css";/,
    );
  });

  it("老组件样式必须随 LiveWorkspacePanel 加载（legacy-panels.css）", () => {
    // F-01/P0-1：LiveWorkspacePanel 挂载的拆解/项目/人物/任务/钱包等老组件，
    // 其类规则原依赖 styles.css（客户制品不加载）。拆出的 legacy-panels.css
    // 必须随该挂载点加载，否则深创作面板在生产客户包里是无样式 HTML。
    expect(readSource("./studio/LiveWorkspacePanel.tsx")).toMatch(
      /import\s+"\.\.\s*\/legacy-panels\.css";/,
    );
  });

  it("客户侧拆分样式不得包含管理域选择器", () => {
    // CW-019 延伸：拆分产物只服务客户 lane，任何 .admin- 选择器进入客户
    // 制品都算管理域样式泄漏。横幅注释也不得出现该字面量，避免误命中。
    for (const file of [
      "./customer/customer-access.css",
      "./legacy-panels.css",
    ]) {
      expect(readSource(file), `${file} 不得包含管理域选择器`).not.toMatch(
        /\.admin-/,
      );
    }
  });

  it("styles.css 不得再承载客户账户屏样式（已拆出，防回流）", () => {
    // 拆分后 styles.css 只服务管理端与内部壳。若有人把账户屏样式改回本文件，
    // 客户制品会再次丢失它们——这里钉住拆分事实（两个客户独占类）。
    const source = readSource("./styles.css");
    expect(source).not.toMatch(/\.customer-access-shell\b/);
    expect(source).not.toMatch(/\.customer-access-brand\b/);
  });
});
