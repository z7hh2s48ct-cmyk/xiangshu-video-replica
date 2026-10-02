import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { FundsPage } from "./FundsPage";

// 资金中心是「v4 导航合并」后的容器页：只负责按页签选子页并把只读态透下去。
// 子页各自已有独立测试（OrdersPage / AccountsPage / AdjustmentsPage /
// ReconciliationPage），FundsOverview 是新建的聚合视图，这里统一钉接线。
vi.mock("./OrdersPage", () => ({
  OrdersPage: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="orders">{`readOnly=${String(readOnly)}`}</div>
  ),
}));
vi.mock("./AccountsPage", () => ({
  // 整表导出（账务流水 CSV）是写级动作，只读角色不渲染入口，所以要接 readOnly。
  AccountsPage: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="accounts">{`readOnly=${String(readOnly)}`}</div>
  ),
}));
vi.mock("./AdjustmentsPage", () => ({
  AdjustmentsPage: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="adjustments">{`readOnly=${String(readOnly)}`}</div>
  ),
}));
vi.mock("./ReconciliationPage", () => ({
  ReconciliationPage: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="reconciliation">{`readOnly=${String(readOnly)}`}</div>
  ),
}));
vi.mock("./FundsOverview", () => ({
  FundsOverview: ({
    onOpenReconciliation,
  }: {
    onOpenReconciliation: () => void;
  }) => (
    <button type="button" onClick={onOpenReconciliation}>
      跳对账异常
    </button>
  ),
}));

test("defaults to the funds overview tab", () => {
  render(<FundsPage readOnly />);

  expect(
    screen.getByRole("tablist", { name: "资金中心页签" }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "跳对账异常" }),
  ).toBeInTheDocument();
});

test("forwards readOnly to the orders tab", () => {
  render(<FundsPage readOnly />);
  fireEvent.click(screen.getByRole("tab", { name: "收款订单" }));

  expect(screen.getByTestId("orders")).toHaveTextContent("readOnly=true");
});

test("keeps the writable state on the orders tab when not read-only", () => {
  render(<FundsPage />);
  fireEvent.click(screen.getByRole("tab", { name: "收款订单" }));

  expect(screen.getByTestId("orders")).toHaveTextContent("readOnly=false");
});

test("forwards readOnly to every tab that carries a whole-table export", () => {
  render(<FundsPage readOnly />);

  fireEvent.click(screen.getByRole("tab", { name: "积分流水" }));
  expect(screen.getByTestId("accounts")).toHaveTextContent("readOnly=true");
  fireEvent.click(screen.getByRole("tab", { name: "人工调整" }));
  expect(screen.getByTestId("adjustments")).toHaveTextContent("readOnly=true");
});

test("switches between ledger, adjustments and reconciliation tabs", () => {
  render(<FundsPage />);

  fireEvent.click(screen.getByRole("tab", { name: "积分流水" }));
  expect(screen.getByTestId("accounts")).toBeInTheDocument();
  expect(screen.queryByTestId("orders")).toBeNull();

  fireEvent.click(screen.getByRole("tab", { name: "人工调整" }));
  expect(screen.getByTestId("adjustments")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("tab", { name: "对账异常" }));
  expect(screen.getByTestId("reconciliation")).toBeInTheDocument();
});

test("overview shortcut jumps straight to the reconciliation tab", () => {
  render(<FundsPage />);

  fireEvent.click(screen.getByRole("button", { name: "跳对账异常" }));
  expect(screen.getByTestId("reconciliation")).toBeInTheDocument();
});

test("auditor reconciliation remains read-only after navigating from overview", () => {
  render(<FundsPage readOnly />);
  fireEvent.click(screen.getByRole("button", { name: "跳对账异常" }));
  expect(screen.getByTestId("reconciliation")).toHaveTextContent(
    "readOnly=true",
  );
});
