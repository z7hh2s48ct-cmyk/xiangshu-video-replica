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
  // AccountsPage 刻意不接 readOnly：它整页只有查询与重新读取，没有任何写操作
  // 或危险按钮可收敛（无 adminWrite、无 ConfirmDialog），所以不透传是对的 ——
  // 加一个什么都不做的 prop 只会误导。
  AccountsPage: () => <div data-testid="accounts">积分流水</div>,
}));
vi.mock("./AdjustmentsPage", () => ({
  AdjustmentsPage: () => <div data-testid="adjustments">人工调整</div>,
}));
vi.mock("./ReconciliationPage", () => ({
  ReconciliationPage: () => <div data-testid="reconciliation">对账异常</div>,
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
