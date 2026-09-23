import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { RootApp } from "./RootApp";
import "./scrollbars.css";

// CW-019: 这是客户构建制品（client/dist）的唯一入口挂载文件。
// 管理端由 client/src/admin-main.tsx + client/admin.html 独立挂载，
// 产出到 client/dist-admin，两个制品物理分离。

const root = document.getElementById("root");

if (!root) {
  throw new Error("找不到应用挂载节点");
}

createRoot(root).render(
  <StrictMode>
    <RootApp />
  </StrictMode>,
);
