import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as adminApi from "../api.admin";
import { CustomerRefundSection } from "./CustomerRefundSection";

vi.mock("../api.admin", () => ({
  createCustomerAdjustment: vi.fn(),
  AdminActivationError: class extends Error {
    readonly status: number | undefined;
    constructor(message: string, status?: number) {
      super(message);
      this.name = "AdminActivationError";
      this.status = status;
    }
  },
}));

function fillForm({
  credits = "30",
  ref = "RF-0928-01",
  reason = "客户申请退还未用积分",
}: {
  credits?: string;
  ref?: string;
  reason?: string;
} = {}) {
  fireEvent.change(screen.getByLabelText("扣减积分"), {
    target: { value: credits },
  });
  fireEvent.change(screen.getByLabelText("审批单号"), {
    target: { value: ref },
  });
  fireEvent.change(screen.getByLabelText("原因"), {
    target: { value: reason },
  });
  fireEvent.click(screen.getByRole("button", { name: "提交退款扣减" }));
}

describe("CustomerRefundSection（P0-2 退款扣减迁入客户详情）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("blocks amounts above the available balance before calling the API", () => {
    render(
      <CustomerRefundSection
        availableCredits={20}
        onRefunded={vi.fn()}
        readOnly={false}
        userId="user-1"
      />,
    );
    fillForm({ credits: "30" });
    expect(screen.getByText(/最多可扣减 20 积分/)).toBeInTheDocument();
    expect(adminApi.createCustomerAdjustment).not.toHaveBeenCalled();
  });

  it("requires an approval reference to align with the offline refund", () => {
    render(
      <CustomerRefundSection
        availableCredits={100}
        onRefunded={vi.fn()}
        readOnly={false}
        userId="user-1"
      />,
    );
    fillForm({ ref: "  " });
    expect(screen.getByText(/请填写审批单号/)).toBeInTheDocument();
    expect(adminApi.createCustomerAdjustment).not.toHaveBeenCalled();
  });

  it("sends a negative reversal and reports the new balance", async () => {
    const onRefunded = vi.fn();
    vi.mocked(adminApi.createCustomerAdjustment).mockResolvedValue({
      request_id: "req-1",
      wallet_balance_after: 70,
    } as Awaited<ReturnType<typeof adminApi.createCustomerAdjustment>>);
    render(
      <CustomerRefundSection
        availableCredits={100}
        onRefunded={onRefunded}
        readOnly={false}
        userId="user-1"
      />,
    );
    fillForm();
    const dialog = await screen.findByRole("dialog", { name: "确认退款扣减" });
    expect(dialog).toHaveTextContent("扣减 30 积分");
    fireEvent.click(screen.getByRole("button", { name: "确认扣减" }));

    await waitFor(() => expect(onRefunded).toHaveBeenCalled());
    const [userId, input, reason] = vi.mocked(adminApi.createCustomerAdjustment)
      .mock.calls[0];
    expect(userId).toBe("user-1");
    expect(input).toEqual({
      sourceDocumentType: "REFUND_APPROVAL",
      sourceDocumentRef: "RF-0928-01",
      credits: -30,
    });
    expect(reason).toBe("客户申请退还未用积分");
    expect(await screen.findByText(/余额 70 积分/)).toBeInTheDocument();
  });

  it("reuses the idempotency key after an uncertain failure", async () => {
    vi.mocked(adminApi.createCustomerAdjustment)
      .mockRejectedValueOnce(new Error("网络超时"))
      .mockResolvedValueOnce({
        request_id: "req-2",
        wallet_balance_after: 70,
      } as Awaited<ReturnType<typeof adminApi.createCustomerAdjustment>>);
    render(
      <CustomerRefundSection
        availableCredits={100}
        onRefunded={vi.fn()}
        readOnly={false}
        userId="user-1"
      />,
    );
    fillForm();
    await screen.findByRole("dialog", { name: "确认退款扣减" });
    fireEvent.click(screen.getByRole("button", { name: "确认扣减" }));
    expect(await screen.findByText(/结果未确认/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认扣减" }));

    await waitFor(() =>
      expect(adminApi.createCustomerAdjustment).toHaveBeenCalledTimes(2),
    );
    const keys = vi
      .mocked(adminApi.createCustomerAdjustment)
      .mock.calls.map((call) => call[3]);
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
  });

  it("shows a read-only note to auditors", () => {
    render(
      <CustomerRefundSection
        availableCredits={100}
        onRefunded={vi.fn()}
        readOnly
        userId="user-1"
      />,
    );
    expect(
      screen.getByText("审计员仅可查看，不能扣减积分。"),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("扣减积分")).toBeNull();
  });
});
