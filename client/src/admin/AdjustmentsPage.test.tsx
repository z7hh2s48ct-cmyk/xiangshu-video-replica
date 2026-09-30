import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as adminApi from "../api.admin";
import { AdjustmentsPage } from "./AdjustmentsPage";

// Mock the admin API module
vi.mock("../api.admin", () => ({
  listAdminAdjustments: vi.fn(),
  listAllAdminAdjustments: vi.fn(),
  AdminAdjustmentError: class extends Error {
    constructor(message: string) {
      super(message);
      this.name = "AdminAdjustmentError";
    }
  },
}));

describe("AdjustmentsPage (ADM-02 / T33)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders adjustment list with pagination", async () => {
    const mockAdjustments = [
      {
        adjustment_id: "adj-1",
        order_id: "order-1",
        admin_user_id: "admin-1",
        source_document_type: "CS_TICKET",
        source_document_ref: "TICKET-123",
        reason: "客户补偿",
        request_id: "req-1",
        created_at: "2026-08-24T10:00:00Z",
        amount_fen: 10000,
        credits: 100,
        pricing_scope: "CUSTOMER_STANDARD",
        status: "PAID",
      },
      {
        adjustment_id: "adj-2",
        order_id: "order-2",
        admin_user_id: "admin-2",
        source_document_type: "REFUND_APPROVAL",
        source_document_ref: "REFUND-456",
        reason: "退款",
        request_id: "req-2",
        created_at: "2026-08-24T11:00:00Z",
        amount_fen: 5000,
        credits: 50,
        pricing_scope: "CUSTOMER_STANDARD",
        status: "PAID",
      },
    ];

    vi.mocked(adminApi.listAdminAdjustments).mockResolvedValue({
      items: mockAdjustments,
      total: 2,
      limit: 20,
      offset: 0,
    });

    render(<AdjustmentsPage userId="user-1" />);

    await waitFor(() => {
      expect(screen.getByText("客户补偿")).toBeInTheDocument();
      expect(screen.getByText("退款")).toBeInTheDocument();
    });

    expect(screen.getByText("第 1 / 1 页（共 2 条）")).toBeInTheDocument();
  });

  it("shows loading state while fetching adjustments", () => {
    vi.mocked(adminApi.listAdminAdjustments).mockImplementation(
      () => new Promise(() => {}), // Never resolves
    );

    render(<AdjustmentsPage userId="user-1" />);

    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("handles API error gracefully", async () => {
    vi.mocked(adminApi.listAdminAdjustments).mockRejectedValue(
      new adminApi.AdminAdjustmentError("网络错误"),
    );

    render(<AdjustmentsPage userId="user-1" />);

    await waitFor(() => {
      expect(screen.getByText("加载失败：网络错误")).toBeInTheDocument();
    });
  });

  it("supports pagination navigation", async () => {
    const mockAdjustments = Array.from({ length: 20 }, (_, i) => ({
      adjustment_id: `adj-${i}`,
      order_id: `order-${i}`,
      admin_user_id: "admin-1",
      source_document_type: "CS_TICKET",
      source_document_ref: `TICKET-${i}`,
      reason: `调账 ${i}`,
      request_id: `req-${i}`,
      created_at: "2026-08-24T10:00:00Z",
      amount_fen: 1000,
      credits: 10,
      pricing_scope: "CUSTOMER_STANDARD",
      status: "PAID",
    }));

    vi.mocked(adminApi.listAdminAdjustments).mockResolvedValue({
      items: mockAdjustments,
      total: 50,
      limit: 20,
      offset: 0,
    });

    render(<AdjustmentsPage userId="user-1" />);

    await waitFor(() => {
      expect(screen.getByText("第 1 / 3 页（共 50 条）")).toBeInTheDocument();
    });

    const nextPageButton = screen.getByRole("button", { name: "下一页" });
    fireEvent.click(nextPageButton);

    expect(adminApi.listAdminAdjustments).toHaveBeenCalledWith("user-1", {
      limit: 20,
      offset: 20,
    });
  });

  it("displays source document type badges correctly", async () => {
    const mockAdjustments = [
      {
        adjustment_id: "adj-1",
        order_id: "order-1",
        admin_user_id: "admin-1",
        source_document_type: "CS_TICKET",
        source_document_ref: "TICKET-123",
        reason: "客户补偿调账",
        request_id: "req-1",
        created_at: "2026-08-24T10:00:00Z",
        amount_fen: 10000,
        credits: 100,
        pricing_scope: "CUSTOMER_STANDARD",
        status: "PAID",
      },
      {
        adjustment_id: "adj-2",
        order_id: "order-2",
        admin_user_id: "admin-1",
        source_document_type: "REFUND_APPROVAL",
        source_document_ref: "REFUND-456",
        reason: "退款审批调账",
        request_id: "req-2",
        created_at: "2026-08-24T11:00:00Z",
        amount_fen: 5000,
        credits: 50,
        pricing_scope: "CUSTOMER_STANDARD",
        status: "PAID",
      },
    ];

    vi.mocked(adminApi.listAdminAdjustments).mockResolvedValue({
      items: mockAdjustments,
      total: 2,
      limit: 20,
      offset: 0,
    });

    render(<AdjustmentsPage userId="user-1" />);

    await waitFor(() => {
      expect(screen.getByText("客服工单")).toBeInTheDocument();
      expect(screen.getByText("退款审批")).toBeInTheDocument();
    });
  });

  it("shows empty state when no adjustments exist", async () => {
    vi.mocked(adminApi.listAdminAdjustments).mockResolvedValue({
      items: [],
      total: 0,
      limit: 20,
      offset: 0,
    });

    render(<AdjustmentsPage userId="user-1" />);

    await waitFor(() => {
      expect(screen.getByText("暂无调账记录")).toBeInTheDocument();
    });
  });

  it("marks missing historical balance snapshots instead of guessing", async () => {
    vi.mocked(adminApi.listAllAdminAdjustments).mockResolvedValue({
      items: [
        {
          adjustment_id: "adj-history",
          order_id: "order-history",
          admin_user_id: "admin-1",
          source_document_type: "CS_TICKET",
          source_document_ref: "HISTORY-1",
          reason: "历史调账",
          request_id: "req-history",
          created_at: "2026-08-20T10:00:00Z",
          amount_fen: 1000,
          credits: 10,
          pricing_scope: "CUSTOMER_STANDARD",
          status: "PAID",
          admin_username: "admin-1",
          target_user_id: "user-history",
          target_username: "customer-history",
          balance_before: null,
          balance_after: null,
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });

    render(<AdjustmentsPage />);

    expect(await screen.findByText("历史未记录")).toBeInTheDocument();
  });

  it("formats amount in yuan correctly", async () => {
    const mockAdjustments = [
      {
        adjustment_id: "adj-1",
        order_id: "order-1",
        admin_user_id: "admin-1",
        source_document_type: "CS_TICKET",
        source_document_ref: "TICKET-123",
        reason: "客户补偿",
        request_id: "req-1",
        created_at: "2026-08-24T10:00:00Z",
        amount_fen: 10050, // 100.50 元
        credits: 100,
        pricing_scope: "CUSTOMER_STANDARD",
        status: "PAID",
      },
    ];

    vi.mocked(adminApi.listAdminAdjustments).mockResolvedValue({
      items: mockAdjustments,
      total: 1,
      limit: 20,
      offset: 0,
    });

    render(<AdjustmentsPage userId="user-1" />);

    await waitFor(() => {
      expect(screen.getByText("¥100.50")).toBeInTheDocument();
    });
  });

  it("shows a reversal with a signed credits delta and the ZPay note", async () => {
    // B1: a reverse adjustment has no recharge order and no in-system money
    // movement, so the amount column must not claim "¥0.00" and the credits
    // column must not render a plus sign in front of a negative number.
    const mockAdjustments = [
      {
        adjustment_id: "adj-rev",
        order_id: "",
        admin_user_id: "admin-1",
        source_document_type: "REFUND_APPROVAL",
        source_document_ref: "REFUND-2026-0918",
        reason: "客户诉求退款",
        request_id: "req-rev",
        created_at: "2026-09-18T10:00:00Z",
        amount_fen: 0,
        credits: -50,
        pricing_scope: "",
        status: "",
      },
    ];

    vi.mocked(adminApi.listAdminAdjustments).mockResolvedValue({
      items: mockAdjustments,
      total: 1,
      limit: 20,
      offset: 0,
    });

    render(<AdjustmentsPage userId="user-1" />);

    await waitFor(() => {
      expect(screen.getByText("-50 积分")).toBeInTheDocument();
    });
    expect(screen.queryByText("+-50 积分")).not.toBeInTheDocument();
    expect(screen.queryByText("¥0.00")).not.toBeInTheDocument();
    expect(
      screen.getByText("账本反向记账，实际退付在支付通道后台办理"),
    ).toBeInTheDocument();
  });

  it("does not show the reversal note for a forward adjustment", async () => {
    const mockAdjustments = [
      {
        adjustment_id: "adj-fwd",
        order_id: "order-1",
        admin_user_id: "admin-1",
        source_document_type: "REFUND_APPROVAL",
        source_document_ref: "REFUND-REVERSAL",
        reason: "撤回一笔退款",
        request_id: "req-fwd",
        created_at: "2026-09-18T11:00:00Z",
        amount_fen: 3000,
        credits: 3,
        pricing_scope: "INTERNAL",
        status: "PAID",
      },
    ];

    vi.mocked(adminApi.listAdminAdjustments).mockResolvedValue({
      items: mockAdjustments,
      total: 1,
      limit: 20,
      offset: 0,
    });

    render(<AdjustmentsPage userId="user-1" />);

    await waitFor(() => {
      expect(screen.getByText("+3 积分")).toBeInTheDocument();
    });
    expect(
      screen.queryByText("账本反向记账，实际退付在支付通道后台办理"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("¥30.00")).toBeInTheDocument();
  });
});
