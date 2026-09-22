import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { FundsPage } from "./FundsPage";

// 资金流水是「v4 导航合并」后的容器页：只负责按页签选子页并把只读态透下去。
// 两个子页各自已有独立测试（OrdersPage.test.tsx / AccountsPage.test.tsx），
// 这里只钉接线。
vi.mock("./OrdersPage", () => ({
  OrdersPage: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="orders">{`readOnly=${String(readOnly)}`}</div>
  ),
}));
vi.mock("./AccountsPage", () => ({
  // AccountsPage 刻意不接 readOnly：它整页只有查询与重新读取，没有任何写操作
  // 或危险按钮可收敛（无 adminWrite、无 ConfirmDialog），所以不透传是对的 ——
  // 加一个什么都不做的 prop 只会误导。
  AccountsPage: () => <div data-testid="accounts">额度流水</div>,
}));

test("defaults to the recharge orders tab and forwards readOnly to it", () => {
  render(<FundsPage readOnly />);

  expect(
    screen.getByRole("tablist", { name: "资金流水页签" }),
  ).toBeInTheDocument();
  expect(screen.getByTestId("orders")).toHaveTextContent("readOnly=true");
});

test("hands the writable state to the orders tab when not read-only", () => {
  render(<FundsPage />);

  expect(screen.getByTestId("orders")).toHaveTextContent("readOnly=false");
});

test("switches to the wallet ledger tab", () => {
  render(<FundsPage />);

  fireEvent.click(screen.getByRole("tab", { name: "额度流水" }));
  expect(screen.getByTestId("accounts")).toBeInTheDocument();
  expect(screen.queryByTestId("orders")).toBeNull();

  fireEvent.click(screen.getByRole("tab", { name: "充值订单" }));
  expect(screen.getByTestId("orders")).toBeInTheDocument();
});
