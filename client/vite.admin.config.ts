import { copyFileSync, existsSync, mkdirSync, renameSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vitest/config";

// CW-019 管理端独立构建配置。
//
// 与 client/vite.config.ts 的关键差异：
//   * base = "/admin/"：管理制品由 nginx `location ^~ /admin/ { alias .../dist-admin/; }`
//     提供，所有资源引用需带 /admin/ 前缀。
//   * build.outDir = "dist-admin"：与客户制品 dist/ 物理分离，
//     客户产物排除断言（scripts/verify_customer_bundle.mjs）直接扫描 dist/，
//     两个目录互不覆盖。
//   * rollupOptions.input = admin.html：显式钉住管理入口，防止 Vite 自动扫描
//     把 index.html 或其他 html 拉进管理构建（对称于客户配置钉住 index.html）。
//
// envPrefix、test 段、dev server 的 /api 代理保持与客户配置一致；不含任何
// 客户专属逻辑（Tauri 平台判断、customer 路由重写等）。

const scriptDir = fileURLToPath(new URL(".", import.meta.url));

/**
 * Vite MPA 输出保留源文件 basename：admin.html → dist-admin/admin.html。
 * nginx 的 SPA fallback（`try_files $uri $uri/ /admin/index.html;`）约定入口
 * 文件名为 index.html，故构建后重命名以对齐部署面。dev 模式不触发 writeBundle，
 * 客户 vite.config.ts 的 /admin 重写插件负责本地联调。
 *
 * 同一个钩子补一个 favicon 单文件拷贝：`build.copyPublicDir: false` 关掉了
 * public/ 的全量复制（见下方注释），而 admin.html 的 `<link rel="icon"
 * href="/favicon.svg">` 已被 base 改写成 `/admin/favicon.svg`，不补就 404。
 */
/**
 * Dev-only: Vite's SPA fallback with base="/admin/" strips the prefix and
 * serves index.html (the customer entry) for /admin/ requests. This plugin
 * rewrites those requests to /admin.html so the correct admin entry is served.
 * In production build, renameAdminHtmlPlugin handles the rename to index.html.
 */
const adminDevEntryPlugin: Plugin = {
  name: "admin-dev-entry-rewrite",
  apply: "serve",
  configureServer(server) {
    server.middlewares.use((req, _res, next) => {
      const url = req.url ?? "";
      // Rewrite /admin/ → /admin/admin.html so that after Vite strips the
      // base prefix ("/admin/"), it resolves to /admin.html in the project root.
      // Without this, Vite's SPA fallback serves index.html (customer entry).
      if (
        url === "/admin/" ||
        url === "/admin" ||
        url === "/admin/index.html"
      ) {
        req.url = "/admin/admin.html";
      }
      next();
    });
  },
};

const renameAdminHtmlPlugin: Plugin = {
  name: "cw019-rename-admin-html",
  apply: "build",
  writeBundle(options) {
    const outDir = options.dir ?? resolve(scriptDir, "dist-admin");
    const from = resolve(outDir, "admin.html");
    const to = resolve(outDir, "index.html");
    try {
      renameSync(from, to);
    } catch (err) {
      // 幂等只指「目标已就位」。emptyOutDir 每次构建都清空目录、writeBundle 每个
      // output 只触发一次，正常路径不存在二次进入；走到 ENOENT 而目标又不在，说明
      // 管理入口 HTML 根本没产出（rollupOptions.input 被改名、Vite/Rolldown 的 MPA
      // 输出命名策略变化、将来引入多 output）。这种情况必须失败：否则 build:admin
      // 仍 exit 0、CI 三个 step 全绿（verify 只扫 dist，不扫 dist-admin），故障被
      // 推迟到部署现场，表现为 nginx `rewrite or internal redirection cycle` 500。
      if ((err as NodeJS.ErrnoException).code === "ENOENT" && existsSync(to)) {
        return;
      }
      throw new Error(
        `[cw019] 管理制品入口缺失：无法把 ${from} 重命名为 ${to}` +
          `（原始错误：${(err as Error).message}）`,
      );
    }
    const faviconSource = resolve(scriptDir, "public/favicon.svg");
    if (!existsSync(faviconSource)) {
      throw new Error(
        `[cw019] 管理制品缺 favicon：${faviconSource} 不存在，` +
          "但 client/admin.html 引用了 /favicon.svg（构建后为 /admin/favicon.svg）。" +
          "请恢复该文件，或同时移除 admin.html 里的 icon 引用。",
      );
    }
    copyFileSync(faviconSource, resolve(outDir, "favicon.svg"));
    for (const icon of ["favicon.png", "favicon.ico"]) {
      copyFileSync(resolve(scriptDir, "public", icon), resolve(outDir, icon));
    }
    // The admin and customer surfaces share the official wordmark. Copy only
    // this asset; the rest of public/studio remains customer-only.
    const brandSource = resolve(scriptDir, "public/studio/brand.png");
    if (!existsSync(brandSource)) {
      throw new Error(`[admin] 共享品牌 Logo 缺失：${brandSource}`);
    }
    mkdirSync(resolve(outDir, "studio"), { recursive: true });
    copyFileSync(brandSource, resolve(outDir, "studio/brand.png"));
  },
};

export default defineConfig({
  plugins: [react(), adminDevEntryPlugin, renameAdminHtmlPlugin],
  base: "/admin/",
  clearScreen: false,
  server: {
    host: "127.0.0.1",
    // 与客户 dev server（5173）错开，允许两端联调时并行运行；
    // 单端调试时也可通过客户 dev server 的 /admin 重写访问管理端。
    port: 5174,
    strictPort: true,
    watch: {
      ignored: ["**/src-tauri/**"],
    },
    // 管理端 fetch 走 /api/control/*，本地联调需要与客户配置相同的同源代理
    // 与 X-Control-Proxy-Token 注入（须与后端 CONTROL_PROXY_TOKEN_DIGEST 配对）。
    proxy: {
      "/api": {
        target:
          process.env.VITE_DEV_API_PROXY_TARGET ?? "http://127.0.0.1:8000",
        changeOrigin: true,
        headers: {
          "X-Control-Proxy-Token":
            process.env.VITE_DEV_CONTROL_PROXY_TOKEN ?? "dev-proxy-token-1234",
        },
      },
    },
  },
  envPrefix: ["VITE_", "TAURI_ENV_*"],
  build: {
    outDir: "dist-admin",
    emptyOutDir: true,
    // CW-019: 断开 public/ 到管理制品的隐式全量搬运。Vite 默认 copyPublicDir
    // 会把 client/public/ 整个复制进 dist-admin，其中 studio/*.png 约 25 MB 是
    // 客户工作台专用参考图；管理端只复用 brand.png。这不仅是体积问题：
    // 它是一条无评审、无断言、自动生效的
    // 跨制品耦合通道——今后任何人往 public/ 放客户专属素材，都会静默出现在管理
    // 制品并被管理站 nginx 以公开 URL 伺服，与 CW-019 建立的制品隔离前提相反。
    // 管理制品只需 favicon.svg 和 studio/brand.png，由 writeBundle 显式复制。
    copyPublicDir: false,
    target:
      process.env.TAURI_ENV_PLATFORM === "windows" ? "chrome105" : "safari13",
    minify: process.env.TAURI_ENV_DEBUG ? false : "oxc",
    sourcemap: Boolean(process.env.TAURI_ENV_DEBUG),
    rollupOptions: {
      input: resolve(scriptDir, "admin.html"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    restoreMocks: true,
    // 与客户 vite.config.ts 保持一致：Node ≥ 25 默认启用 Web Storage API，
    // 会让 jsdom 跳过安装自己的 Storage，测试拿到无方法的 localStorage stub。
    // Node 24（CI）上该 flag 是无害 no-op。
    execArgv: ["--no-experimental-webstorage"],
  },
});
