import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { AdminApp } from "./AdminApp";

// CW-019: 管理端独立入口挂载文件。
// 与客户入口 main.tsx 物理分离：客户构建制品（client/dist）不含本文件，
// 管理构建制品（client/dist-admin）不含 main.tsx。断言由
// `scripts/verify_customer_bundle.mjs` 与 `client/src/entryContract.test.ts` 双层保证。
//
// 挂载点 id 与客户入口一致（root），复用同一套 nginx / Tauri 排障经验。
//
// ADMIN-UI-AUDIT-20260911: styles.css 是客户/管理共享的基础样式表
// （.admin-shell 布局、全局 input/button、--admin-* 设计令牌定义）。
// 拆分双入口后它只剩客户壳 App.tsx 一处导入，管理端产物随之半裸渲染；
// 管理入口必须在此显式导入，契约由 entryContract.test.ts 钉住。
import "./styles.css";
import "./scrollbars.css";

const root = document.getElementById("root");

if (!root) {
  throw new Error("找不到应用挂载节点");
}

createRoot(root).render(
  <StrictMode>
    <AdminApp />
  </StrictMode>,
);
