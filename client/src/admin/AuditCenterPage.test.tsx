import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { AuditCenterPage } from "./AuditCenterPage";

// 审计中心是「v4 导航合并」后的容器页：本身没有业务逻辑，只负责按页签选对
// 子页。两个子页各自已有独立测试（AuditEventsPage.test.tsx /
// AdjustmentsPage.test.tsx），所以这里只钉接线，不重复子组件的断言 ——
// 这也正是本页此前完全没有覆盖的那一层。
vi.mock("./AuditEventsPage", () => ({
  AuditEventsPage: () => <div data-testid="audit-events">审计事件</div>,
}));
vi.mock("./AdjustmentsPage", () => ({
  AdjustmentsPage: () => <div data-testid="adjustments">调账记录</div>,
}));

test("defaults to the audit log tab", () => {
  render(<AuditCenterPage />);

  expect(
    screen.getByRole("tablist", { name: "审计中心页签" }),
  ).toBeInTheDocument();
  expect(screen.getByTestId("audit-events")).toBeInTheDocument();
  expect(screen.queryByTestId("adjustments")).toBeNull();
});

test("switches to the adjustment archive and back", () => {
  render(<AuditCenterPage />);

  fireEvent.click(screen.getByRole("tab", { name: "调账记录" }));
  expect(screen.getByTestId("adjustments")).toBeInTheDocument();
  expect(screen.queryByTestId("audit-events")).toBeNull();

  fireEvent.click(screen.getByRole("tab", { name: "审计日志" }));
  expect(screen.getByTestId("audit-events")).toBeInTheDocument();
  expect(screen.queryByTestId("adjustments")).toBeNull();
});
