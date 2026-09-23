import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WalletPanel } from "./WalletPanel";

const wallet = {
  available_credits: 12,
  reserved_credits: 2,
  internal_unit_price_fen: 1000,
  min_recharge_fen: 10000,
  recharge_step_fen: 1000,
};

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

describe("WalletPanel", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    window.localStorage.clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("shows internal price, balances, ledger and creates a preset recharge", async () => {
    const submit = vi
      .spyOn(HTMLFormElement.prototype, "submit")
      .mockImplementation(() => undefined);
    const fetchMock = vi.fn((url: string, options?: RequestInit) => {
      if (url.endsWith("/api/wallet")) {
        return jsonResponse(wallet);
      }
      if (url.includes("/api/wallet/transactions?")) {
        return jsonResponse({
          items: [
            {
              id: "tx-1",
              user_id: "user-1",
              type: "CHARGE",
              available_delta: 10,
              reserved_delta: 0,
              recharge_order_id: "order-1",
              task_id: null,
              billing_round: null,
              created_at: "2026-08-19 10:00:00",
            },
          ],
          total: 1,
          limit: 20,
          offset: 0,
        });
      }
      if (url.includes("/api/recharge-orders?") && options?.method !== "POST") {
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }
      if (url.endsWith("/api/recharge-orders") && options?.method === "POST") {
        return jsonResponse(
          {
            order_no: "202608190001",
            status: "PENDING",
            amount_fen: 20000,
            credits: 20,
            gateway_url: "https://zpayz.cn/submit.php",
            method: "POST",
            form_fields: {
              pid: "merchant",
              type: "alipay",
              out_trade_no: "202608190001",
              sign: "signature",
              sign_type: "MD5",
            },
          },
          201,
        );
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<WalletPanel currentUserId="user-1" />);

    expect(await screen.findByText("10元 / 条")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("冻结 2 条")).toBeInTheDocument();
    expect(screen.getByText("充值到账")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "充值200元" }));

    await waitFor(() => expect(submit).toHaveBeenCalledOnce());
    const paymentForm = submit.mock.instances[0] as HTMLFormElement;
    expect(paymentForm.target).toBe("_blank");
    expect(paymentForm.getAttribute("rel")).toBe("noopener");
    const createCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url).endsWith("/api/recharge-orders") &&
        options?.method === "POST",
    );
    expect(createCall?.[1]?.body).toBe(JSON.stringify({ amount_fen: 20000 }));
    expect(window.localStorage.getItem("wallet.pendingOrderNo:user-1")).toBe(
      "202608190001",
    );
    expect(window.localStorage.getItem("wallet.pendingOrderNo")).toBeNull();
  });

  it("rejects a custom amount that is not an integer 10-yuan step", async () => {
    const fetchMock = vi.fn((url: string, _options?: RequestInit) => {
      if (url.endsWith("/api/wallet")) {
        return jsonResponse(wallet);
      }
      return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<WalletPanel />);
    await screen.findByText("10元 / 条");
    fireEvent.change(screen.getByLabelText("自定义充值金额（元）"), {
      target: { value: "101" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认充值" }));

    expect(
      await screen.findByText("充值金额须为100元起，并按10元递增。"),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([, options]) => options?.method === "POST"),
    ).toBe(false);
  });

  it("restores a pending order and stops polling after it becomes paid", async () => {
    vi.useFakeTimers();
    window.localStorage.setItem("wallet.pendingOrderNo:user-1", "pending-1");
    window.localStorage.setItem(
      "wallet.pendingOrderNo:user-2",
      "other-user-order",
    );
    let statusChecks = 0;
    let walletReads = 0;
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/wallet")) {
        walletReads += 1;
        return jsonResponse({
          ...wallet,
          available_credits: walletReads === 1 ? 12 : 22,
        });
      }
      if (url.includes("/api/wallet/transactions?")) {
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }
      if (url.includes("/api/recharge-orders?")) {
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }
      if (url.endsWith("/api/recharge-orders/pending-1")) {
        statusChecks += 1;
        return jsonResponse({
          order_no: "pending-1",
          status: statusChecks === 1 ? "PENDING" : "PAID",
          amount_fen: 10000,
          credits: 10,
          channel: "alipay",
          created_at: "2026-08-19 10:00:00",
          paid_at: statusChecks === 1 ? null : "2026-08-19 10:00:02",
        });
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<WalletPanel currentUserId="user-1" />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(statusChecks).toBe(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(statusChecks).toBe(2);
    expect(
      screen.getByText("充值已到账，钱包余额已更新。"),
    ).toBeInTheDocument();
    expect(
      window.localStorage.getItem("wallet.pendingOrderNo:user-1"),
    ).toBeNull();
    expect(window.localStorage.getItem("wallet.pendingOrderNo:user-2")).toBe(
      "other-user-order",
    );
    expect(walletReads).toBeGreaterThanOrEqual(2);
  });

  it("stops automatic polling after one minute while keeping the pending order", async () => {
    vi.useFakeTimers();
    window.localStorage.setItem("wallet.pendingOrderNo", "pending-long");
    let statusChecks = 0;
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/wallet")) {
        return jsonResponse(wallet);
      }
      if (
        url.includes("/api/wallet/transactions?") ||
        url.includes("/api/recharge-orders?")
      ) {
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }
      if (url.endsWith("/api/recharge-orders/pending-long")) {
        statusChecks += 1;
        return jsonResponse({
          order_no: "pending-long",
          status: "PENDING",
          amount_fen: 10000,
          credits: 10,
          channel: "alipay",
          created_at: "2026-08-19 10:00:00",
          paid_at: null,
        });
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<WalletPanel />);
    await act(async () => {
      await vi.runAllTimersAsync();
    });

    expect(statusChecks).toBe(30);
    expect(
      screen.getByText("支付结果仍待确认，可稍后刷新页面继续查询。"),
    ).toBeInTheDocument();
    expect(window.localStorage.getItem("wallet.pendingOrderNo")).toBe(
      "pending-long",
    );
    expect(vi.getTimerCount()).toBe(0);
  });
});
