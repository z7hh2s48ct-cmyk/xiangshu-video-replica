import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AdminRechargeOrder } from "../api.admin";
import { OrdersPage } from "./OrdersPage";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

function blobResponse() {
  return Promise.resolve({
    ok: true,
    status: 200,
    headers: new Headers({
      "X-Export-Total": "5001",
      "X-Export-Returned": "5000",
      "X-Export-Truncated": "true",
    }),
    blob: async () => new Blob(["id\n1"], { type: "text/csv" }),
  });
}

const pendingOrder: AdminRechargeOrder = {
  id: "order-1",
  user_id: "user-1",
  username: "operator-1",
  display_name: "运营一号",
  order_no: "202608190001",
  status: "PENDING",
  amount_fen: 10050,
  credits: 10,
  channel: "alipay",
  provider: "zpay",
  provider_trade_no: null,
  transaction_id: null,
  created_at: "2026-08-19 10:00:00",
  paid_at: null,
};

const reconciliation = {
  wallet_count: 1,
  wallet_mismatch_count: 0,
  paid_order_without_charge_count: 2,
  charge_without_paid_order_count: 1,
  pending_order_count: 1,
};

/** A settled WeChat order: its trade reference lives in transaction_id, never
 *  in provider_trade_no, which migration 083 constrains to stay NULL. */
const paidWechatOrder: AdminRechargeOrder = {
  ...pendingOrder,
  id: "order-2",
  order_no: "202608190002",
  status: "PAID",
  channel: "wxpay",
  provider: "wechat_native",
  transaction_id: "4200001234202608190001",
  paid_at: "2026-08-19 10:05:00",
};

const pendingWechatOrder: AdminRechargeOrder = {
  ...pendingOrder,
  id: "order-3",
  order_no: "202608190003",
  channel: "wxpay",
  provider: "wechat_native",
};

function installFetch(orders: AdminRechargeOrder[] = [pendingOrder]) {
  const fetchMock = vi.fn((url: string, _init?: RequestInit) => {
    if (url.includes("/api/control/recharge-orders?")) {
      const { searchParams } = new URL(String(url));
      return jsonResponse({
        items: orders.map((order) => ({ ...order })),
        total: 25,
        limit: 20,
        offset: Number(searchParams.get("offset") ?? "0"),
      });
    }
    if (url.endsWith("/api/control/billing-reconciliation")) {
      return jsonResponse(reconciliation);
    }
    if (url.includes("/api/control/recharge-orders/202608190001/sync")) {
      return jsonResponse({ ...pendingOrder, status: "PAID" });
    }
    if (
      new URL(url).pathname.endsWith("/api/control/recharge-orders.csv") ||
      new URL(url).pathname.endsWith("/api/control/wallet-transactions.csv")
    ) {
      return blobResponse();
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  // 只替换静态方法，保留 URL 构造器（页面代码里仍会 new URL）。
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:test");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
    () => undefined,
  );
  return fetchMock;
}

describe("OrdersPage", () => {
  it("exports the active filters and reports a limited result", async () => {
    const fetchMock = installFetch();
    render(<OrdersPage />);
    await screen.findByText("¥100.50");
    fireEvent.change(screen.getByLabelText("订单账号"), {
      target: { value: "customer" },
    });
    fireEvent.change(screen.getByLabelText("支付渠道"), {
      target: { value: "alipay" },
    });
    fireEvent.change(screen.getByLabelText("订单起始时间"), {
      target: { value: "2026-09-12" },
    });
    fireEvent.change(screen.getByLabelText("订单截止时间"), {
      target: { value: "2026-09-12" },
    });
    fireEvent.change(screen.getByLabelText("订单状态"), {
      target: { value: "PAID" },
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "筛选" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "筛选" }));
    const button = screen.getByRole("button", { name: "导出充值订单 CSV" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(
      await screen.findByText(/当前筛选共 5001 条，本次仅导出 5000 条/),
    ).toBeInTheDocument();
    const call = fetchMock.mock.calls.find(([url]) =>
      new URL(url).pathname.endsWith("recharge-orders.csv"),
    );
    expect(call).toBeDefined();
    const query = new URL(String(call?.[0])).searchParams;
    expect(Object.fromEntries(query)).toEqual({
      status: "PAID",
      username: "customer",
      channel: "alipay",
      created_from: "2026-09-12",
      created_to: "2026-09-12",
    });
    expect(
      screen.queryByRole("button", { name: "导出账务流水 CSV" }),
    ).toBeNull();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders orders with exact money, badges and reconciliation metrics", async () => {
    installFetch();
    render(<OrdersPage />);

    expect(await screen.findByText("¥100.50")).toBeInTheDocument();
    // 状态筛选下拉与订单徽章都含"待支付"。
    expect(screen.getAllByText("待支付").length).toBeGreaterThan(0);
    expect(
      screen.getByText((_, element) => element?.textContent === "待支付订单 1"),
    ).toBeInTheDocument();
  });

  it("filters by order status on submit", async () => {
    const fetchMock = installFetch();
    render(<OrdersPage />);

    await screen.findByText("¥100.50");
    fireEvent.change(screen.getByLabelText("订单状态"), {
      target: { value: "PAID" },
    });
    fireEvent.click(screen.getByRole("button", { name: "筛选" }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("status=PAID"),
        ),
      ).toBe(true),
    );
  });

  it("confirms the ZPay order sync with a reason before firing it", async () => {
    const fetchMock = installFetch();
    render(<OrdersPage />);

    fireEvent.click(await screen.findByRole("button", { name: "查单同步" }));

    // 确认前不发请求。
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("/recharge-orders/202608190001/sync"),
      ),
    ).toBe(false);

    await screen.findByRole("dialog", { name: /查单同步 202608190001/ });
    // A4：查单已是管理端写——原因必填。
    fireEvent.click(screen.getByRole("button", { name: "确认查单" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "请填写操作原因",
    );
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("/recharge-orders/202608190001/sync"),
      ),
    ).toBe(false);

    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客服反馈未到账" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认查单" }));

    await waitFor(() => {
      const syncCall = fetchMock.mock.calls.find(([url]) =>
        String(url).includes("/recharge-orders/202608190001/sync"),
      );
      expect(syncCall).toBeDefined();
      const init = syncCall?.[1] as RequestInit | undefined;
      expect(JSON.parse(String(init?.body))).toMatchObject({
        confirm: true,
        reason: "客服反馈未到账",
      });
      expect(new Headers(init?.headers).get("Idempotency-Key")).toBeTruthy();
    });
    expect(await screen.findByText(/状态已同步/)).toBeInTheDocument();
  });

  it("hides write actions for auditors", async () => {
    installFetch();
    render(<OrdersPage readOnly />);

    expect(await screen.findByText("¥100.50")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "查单同步" })).toBeNull();
  });

  it("shows the WeChat trade number and names the payment provider", async () => {
    installFetch([paidWechatOrder]);
    render(<OrdersPage />);

    // Reading only provider_trade_no would leave every WeChat order blank here,
    // which is exactly the column reconciliation and refunds are matched on.
    expect(
      await screen.findByText("4200001234202608190001"),
    ).toBeInTheDocument();
    expect(screen.getByText("微信官方")).toBeInTheDocument();
  });

  it("does not offer ZPay-only 查单同步 on a pending WeChat order", async () => {
    installFetch([pendingWechatOrder]);
    render(<OrdersPage />);

    expect(await screen.findByText("微信官方")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "查单同步" })).toBeNull();
    expect(screen.getByText("客户详情核验")).toBeInTheDocument();
  });
});
