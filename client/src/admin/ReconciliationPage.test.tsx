import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import { listReconciliationItems, repairPaidRechargeLedger } from "../api";
import { ReconciliationPage } from "./ReconciliationPage";

vi.mock("../api", () => ({
  listReconciliationItems: vi.fn(),
  repairPaidRechargeLedger: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listReconciliationItems).mockResolvedValue({
    anomaly: "paid_without_charge",
    items: [
      {
        order_id: "internal-id",
        order_no: "merchant-20261001",
        username: "review",
        display_name: "验收公司",
        amount_fen: 10000,
        credits: 100,
      },
    ],
    total: 1,
    limit: 20,
    offset: 0,
  });
  vi.mocked(repairPaidRechargeLedger).mockResolvedValue({
    order_no: "merchant-20261001",
    outcome: "repaired",
    credits: 100,
  });
});

test("补单使用商户单号并保留操作原因，不能使用内部订单编号", async () => {
  render(<ReconciliationPage />);
  fireEvent.click(await screen.findByRole("button", { name: "补记积分" }));
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "核查已支付未入账" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认补记" }));
  await waitFor(() =>
    expect(repairPaidRechargeLedger).toHaveBeenCalledWith(
      "merchant-20261001",
      "核查已支付未入账",
    ),
  );
  expect(listReconciliationItems).toHaveBeenCalledTimes(2);
});

test("审计员可查看异常但没有补单及确认入口", async () => {
  render(<ReconciliationPage readOnly />);
  await screen.findByText("验收公司");
  expect(screen.queryByRole("button", { name: "补记积分" })).toBeNull();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(repairPaidRechargeLedger).not.toHaveBeenCalled();
});

test("旧响应缺商户单号时禁止补单，不能发送占位符", async () => {
  vi.mocked(listReconciliationItems).mockResolvedValue({
    anomaly: "paid_without_charge",
    items: [{ order_id: "internal-only", username: "review" }],
    total: 1,
    limit: 20,
    offset: 0,
  });
  render(<ReconciliationPage />);
  const button = await screen.findByRole("button", { name: "补记积分" });
  expect(button).toBeDisabled();
  fireEvent.click(button);
  expect(repairPaidRechargeLedger).not.toHaveBeenCalled();
});
