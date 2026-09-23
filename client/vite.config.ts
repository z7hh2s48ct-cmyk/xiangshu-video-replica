import { copyFileSync, cpSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vitest/config";

const scriptDir = fileURLToPath(new URL(".", import.meta.url));

/**
 * CW-019 dev-only：把 /admin 与 /admin/* 重写到 /admin.html，让本地联调
 * 可以用客户 dev server（127.0.0.1:5173）同时访问客户壳与管理端。
 *
 * 生产不适用：客户构建制品物理排除 admin.html（scripts/verify_customer_bundle.mjs
 * 断言），管理制品由 `vite build --config vite.admin.config.ts` 独立输出到
 * client/dist-admin，nginx `location ^~ /admin/` alias 到该目录。
 *
 * 只在 dev server 生效（configureServer 只被 `vite dev` 调用），不影响 build。
 */
const adminDevRewritePlugin: Plugin = {
  name: "cw019-admin-dev-rewrite",
  apply: "serve",
  configureServer(server) {
    server.middlewares.use((req, _res, next) => {
      const url = req.url ?? "";
      // 精确 /admin 与 /admin/…；不匹配 /administrator 之类的其它路径。
      if (url === "/admin/favicon.png" || url === "/admin/favicon.ico") {
        req.url = url.replace("/admin", "");
      } else if (url === "/admin" || url.startsWith("/admin/")) {
        req.url = "/admin.html";
      }
      next();
    });
  },
};

export default defineConfig({
  plugins: [
    react(),
    adminDevRewritePlugin,
    {
      name: "customer-public-assets",
      apply: "build",
      writeBundle() {
        const output = resolve(scriptDir, "dist");
        mkdirSync(output, { recursive: true });
        copyFileSync(
          resolve(scriptDir, "public/favicon.svg"),
          resolve(output, "favicon.svg"),
        );
        for (const icon of ["favicon.png", "favicon.ico"]) {
          copyFileSync(
            resolve(scriptDir, "public", icon),
            resolve(output, icon),
          );
        }
        cpSync(
          resolve(scriptDir, "public/platforms"),
          resolve(output, "platforms"),
          { recursive: true },
        );
        // Production login, welcome and account pages share this brand image.
        mkdirSync(resolve(output, "studio"), { recursive: true });
        copyFileSync(
          resolve(scriptDir, "public/studio/brand.png"),
          resolve(output, "studio/brand.png"),
        );
        copyFileSync(
          resolve(scriptDir, "public/studio/logo-mark.svg"),
          resolve(output, "studio/logo-mark.svg"),
        );
      },
    },
  ],
  clearScreen: false,
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    watch: {
      ignored: ["**/src-tauri/**"],
    },
    // 浏览器端联调：同源代理到本地后端，避免跨源凭据请求
    // （管理端 fetch 带 credentials: "include"，跨源时需要
    //  allow_credentials，开发期统一走同源代理更简单）。
    // 控制面路由（内部通道）依赖反代注入 X-Control-Proxy-Token，
    // 本地联调由该代理以开发令牌代为注入（须与后端
    // CONTROL_PROXY_TOKEN_DIGEST 配对）。
    proxy: {
      "/api": {
        // Forward the loopback development client's address to a trusted
        // container proxy. Production continues to use its own nginx boundary.
        xfwd: true,
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
    // CW-019: 客户构建 outDir 名字必须保持 "dist"——Tauri 双包
    // （tauri.conf.json / tauri.customer.conf.json）的 frontendDist: "../dist"
    // 依赖此路径；改名会同时断掉内部 NSIS 与客户云 NSIS 两条 CI 门禁。
    outDir: "dist",
    // The /studio review images are only served by the development server.
    copyPublicDir: false,
    emptyOutDir: true,
    target:
      process.env.TAURI_ENV_PLATFORM === "windows" ? "chrome105" : "safari13",
    minify: process.env.TAURI_ENV_DEBUG ? false : "oxc",
    sourcemap: Boolean(process.env.TAURI_ENV_DEBUG),
    rollupOptions: {
      // CW-019: 显式钉住客户唯一入口。Vite 8 在 input 缺省时以 index.html 为
      // 入口，但同目录出现 admin.html 时可能被自动扫描卷入客户包，破坏
      // scripts/verify_customer_bundle.mjs 的管理代码排除断言。
      input: resolve(scriptDir, "index.html"),
      // MATERIAL-PERF-D（P1-4）：react/react-dom 拆 vendor chunk——业务代码
      // 迭代时框架代码继续命中浏览器长效缓存。不动页面级分包（页面组件
      // 经 StudioWorkspace 静态导入，lazy 化涉及渲染语义，归后续任务）。
      // Vite 8 底层为 rolldown：分包用 output.advancedChunks 而非 rollup 的
      // manualChunks。
      output: {
        advancedChunks: {
          groups: [
            {
              name: "react-vendor",
              test: /node_modules[\\/](react|react-dom)[\\/]/,
            },
          ],
        },
      },
    },
  },
  test: {
    // 只收集 src/ 下的单测；tests/e2e/ 是 Playwright spec（由 e2e 运行器执行，
    // vitest 环境里既无法解析也无从驱动），不能落进默认的 **/*.spec.ts 收集范围。
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    restoreMocks: true,
    // CI 负载下单条用例（多段 findBy + 网络重试时序）可能超过 vitest 默认
    // 5s；20s 只放宽上限，正常用例仍按实际耗时结束。
    testTimeout: 20000,
    // Node >= 25 ships the Web Storage API enabled by default, and that
    // native globalThis.localStorage makes the jsdom environment skip
    // installing its own Storage (window keys already present on globalThis
    // are skipped), leaving tests with a method-less localStorage stub.
    // Disable the native implementation in test workers so jsdom's Storage
    // is used on every supported Node version; on Node 24 (CI) the flag is a
    // harmless no-op against an already-off default.
    execArgv: ["--no-experimental-webstorage"],
  },
});
