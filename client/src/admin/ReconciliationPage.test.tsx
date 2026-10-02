import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import { listReconciliationItems, repairPaidRechargeLedger } from "../api";
import { adminWrite } from "../api.admin";
import { ReconciliationPage } from "./ReconciliationPage";

vi.mock("../api", () => ({
  listReconciliationItems: vi.fn(),
  repairPaidRechargeLedger: vi.fn(),
}));
vi.mock("../api.admin", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api.admin")>()),
  adminWrite: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listReconciliationItems).mockResolvedValue({
    anomaly: "paid_without_charge",
    items: [
      {
        order_id: "internal-id",
        order_no: "merchant-20261001",
        user_id: "stable-customer",
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

test("人工核对保存当前快照和原因，客户可直达，异常仍显示", async () => {
  vi.mocked(listReconciliationItems).mockResolvedValue({
    anomaly: "wallet_mismatch",
    items: [
      {
        user_id: "stable-customer",
        username: "review",
        display_name: "验收公司",
        available_credits: 30,
        reserved_credits: 0,
        ledger_available_credits: 20,
        ledger_reserved_credits: 0,
        snapshot: "a".repeat(64),
        verification: {
          state: "needs_recheck",
          reason: "上次差异待处理",
          operator: "核对人",
          at: "2026-10-01T01:00:00Z",
        },
      },
    ],
    total: 1,
    limit: 20,
    offset: 0,
  });
  vi.mocked(adminWrite).mockResolvedValue({ state: "verified" });
  render(<ReconciliationPage initialAnomaly="wallet_mismatch" />);
  expect(await screen.findByRole("button", { name: /验收公司/ })).toBeEnabled();
  expect(screen.getByText("数据已变化，需重新核对")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "记录核对结果" }));
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "核对当前差异，待处理" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认记录" }));
  await waitFor(() =>
    expect(adminWrite).toHaveBeenCalledWith(
      "/api/control/billing-reconciliation/verify",
      {
        anomaly: "wallet_mismatch",
        entity_id: "stable-customer",
        snapshot: "a".repeat(64),
      },
      "核对当前差异，待处理",
      "记录核对结果失败",
      expect.any(String),
    ),
  );
  expect(
    await screen.findByText("已记录核对说明，异常仍保留；请继续处理账务差异。"),
  ).toBeVisible();
  expect(listReconciliationItems).toHaveBeenCalledTimes(2);
});

test("核对提交超时后再次确认使用同一幂等键和说明", async () => {
  vi.mocked(listReconciliationItems).mockResolvedValue({
    anomaly: "wallet_mismatch",
    items: [{ user_id: "u-1", username: "review", snapshot: "a".repeat(64) }],
    total: 1,
    limit: 20,
    offset: 0,
  });
  vi.mocked(adminWrite)
    .mockRejectedValueOnce(new Error("timeout"))
    .mockResolvedValueOnce({ state: "verified" });
  render(<ReconciliationPage initialAnomaly="wallet_mismatch" />);
  fireEvent.click(await screen.findByRole("button", { name: "记录核对结果" }));
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "已核查，等待处理差异" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认记录" }));
  fireEvent.click(
    await screen.findByRole("button", { name: "重试原核对记录" }),
  );
  await waitFor(() => expect(adminWrite).toHaveBeenCalledTimes(2));
  expect(vi.mocked(adminWrite).mock.calls[0]).toEqual(
    vi.mocked(adminWrite).mock.calls[1],
  );
});

test("审计员可看核对历史，没有人工核对写入口", async () => {
  vi.mocked(listReconciliationItems).mockResolvedValue({
    anomaly: "charge_without_paid_order",
    items: [
      {
        transaction_id: "technical-tx",
        order_no: "MERCHANT-1",
        user_id: "u-1",
        username: "review",
        snapshot: "a".repeat(64),
        verification: {
          state: "verified",
          reason: "已查，待处理",
          operator: "核对人",
          at: "2026-10-01T01:00:00Z",
        },
      },
    ],
    total: 1,
    limit: 20,
    offset: 0,
  });
  render(
    <ReconciliationPage initialAnomaly="charge_without_paid_order" readOnly />,
  );
  expect(await screen.findByText("已核对，待处理")).toBeVisible();
  expect(screen.getByRole("link", { name: "MERCHANT-1" })).toHaveAttribute(
    "href",
    "#admin/funds?intent=order&orderNo=MERCHANT-1&userId=u-1",
  );
  expect(screen.getByText("technical-tx")).not.toBeVisible();
  expect(screen.queryByRole("button", { name: "记录核对结果" })).toBeNull();
  fireEvent.click(screen.getByText("查看核对记录"));
  expect(screen.getByText("已查，待处理")).toBeVisible();
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
