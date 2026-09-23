import { render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { CustomersManagementPage } from "./CustomersManagementPage";

vi.mock("./CustomersPage", () => ({
  CustomersPage: ({ initialIntent }: { initialIntent?: string }) => (
    <p>客户列表（意图：{initialIntent || "无"}）</p>
  ),
}));

test("customer management exposes only the customer list", () => {
  render(<CustomersManagementPage />);
  expect(screen.getByText("客户列表（意图：无）")).toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: "激活码" })).not.toBeInTheDocument();
  expect(
    screen.queryByRole("tab", { name: "在线会话" }),
  ).not.toBeInTheDocument();
});

// 总览快捷入口的 intent 必须原样透传到客户列表页，否则入口仍然没有下文。
test("forwards the navigation intent to the customer list", () => {
  render(<CustomersManagementPage initialIntent="customerAdjustments" />);
  expect(
    screen.getByText("客户列表（意图：customerAdjustments）"),
  ).toBeInTheDocument();
});
