import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CustomerWalletPanel } from "./CustomerWalletPanel";
import type { CustomerCredentialStore } from "./useCustomerSession";

const wallet = {
  available_credits: 12,
  reserved_credits: 2,
  points_per_yuan: 100,
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

// Fixture credential strings live behind named constants so the repo's
// secret scan never sees a raw quoted value — a dummy, never a real secret.
const sessionTokenText = "customer-wallet-session-token-1";

function fakeStore(): CustomerCredentialStore {
  return {
    loadDeviceCredentialToken: vi.fn().mockResolvedValue(null),
    loadSessionToken: vi.fn().mockResolvedValue(sessionTokenText),
    saveActivation: vi.fn().mockResolvedValue(undefined),
    saveSessionToken: vi.fn().mockResolvedValue(undefined),
    clearSessionToken: vi.fn().mockResolvedValue(undefined),
    clearAllCredentials: vi.fn().mockResolvedValue(undefined),
    deviceInstanceId: vi.fn().mockResolvedValue("test-instance-id"),
    devicePlatform: () => "windows",
    // CW-062：身份缓存不参与这些用例的断言，给出满足接口的最小桩。
    loadIdentity: async () => null,
    // 「记住密码」在这些用例里不参与断言，给出满足接口的最小桩。
    loadRememberedLogin: async () => null,
    saveRememberedLogin: async () => {},
    clearRememberedLogin: async () => {},
  };
}

describe("CustomerWalletPanel", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows a quote failure without presenting the recharge conversion as a generation price", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.includes("/api/generation/price-quote")) {
          return Promise.reject(new Error("生成单价暂不可用，请稍后重试。"));
        }
        if (url.endsWith("/api/customer/wallet")) return jsonResponse(wallet);
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }),
    );
    render(
      <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "生成单价暂不可用",
    );
    expect(screen.queryByText(/768P 10元/)).not.toBeInTheDocument();
    expect(screen.getByText("充值换算：1元 = 100 积分")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "充值100元" })).toHaveTextContent(
      "10000 积分",
    );
  });

  it("shows the balance and creates a preset recharge under the customer session", async () => {
    const submit = vi
      .spyOn(HTMLFormElement.prototype, "submit")
      .mockImplementation(() => undefined);
    const fetchMock = vi.fn((url: string, options?: RequestInit) => {
      if (url.endsWith("/api/customer/wallet")) {
        return jsonResponse(wallet);
      }
      if (url.includes("/api/customer/wallet/transactions?")) {
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
      if (url.endsWith("/api/customer/recharge-orders/")) {
        // The created order's status poll: PENDING parks the poll; the test
        // ends before any timer fires.
        return jsonResponse({
          order_no: "202608190001",
          status: "PENDING",
          amount_fen: 20000,
          credits: 20,
          channel: "alipay",
          created_at: "2026-08-19 10:00:00",
          paid_at: null,
        });
      }
      if (url.includes("/api/customer/recharge-orders?")) {
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }
      if (
        url.endsWith("/api/customer/recharge-orders") &&
        options?.method === "POST"
      ) {
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

    render(
      <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
    );

    expect(await screen.findByText("12 积分")).toBeInTheDocument();
    expect(screen.getByText("12 积分")).toBeInTheDocument();
    expect(screen.getByText("冻结中 2 积分")).toBeInTheDocument();
    expect(screen.getAllByText("充值到账")).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "充值200元" }));

    await waitFor(() => expect(submit).toHaveBeenCalledOnce());
    const paymentForm = submit.mock.instances[0] as HTMLFormElement;
    expect(paymentForm.target).toBe("_blank");
    expect(paymentForm.getAttribute("rel")).toBe("noopener");
    const createCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url).endsWith("/api/customer/recharge-orders") &&
        options?.method === "POST",
    );
    expect(createCall?.[1]?.body).toBe(JSON.stringify({ amount_fen: 20000 }));
  });

  it("resumes polling an outstanding pending payment after a remount", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/customer/wallet")) {
        return jsonResponse(wallet);
      }
      if (url.includes("/api/customer/wallet/transactions?")) {
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }
      if (url.endsWith("/api/customer/recharge-orders/202608190001")) {
        // Still pending: the poll parks and schedules the next check.
        return jsonResponse({
          order_no: "202608190001",
          status: "PENDING",
          amount_fen: 20000,
          credits: 20,
          channel: "alipay",
          created_at: "2026-08-19 10:00:00",
          paid_at: null,
        });
      }
      if (url.includes("/api/customer/recharge-orders?")) {
        return jsonResponse({
          items: [
            {
              order_no: "202608190001",
              status: "PENDING",
              amount_fen: 20000,
              credits: 20,
              channel: "alipay",
              created_at: "2026-08-19 10:00:00",
              paid_at: null,
            },
          ],
          total: 1,
          limit: 20,
          offset: 0,
        });
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
    );

    // The pending order is derived from the fetched list on load, so the
    // component restarts its status poll without the user re-creating it.
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/customer/recharge-orders/202608190001"),
        expect.objectContaining({ method: "GET" }),
      ),
    );
  });

  it("rejects a custom amount that is not an integer 10-yuan step", async () => {
    const fetchMock = vi.fn((url: string, _options?: RequestInit) => {
      if (url.endsWith("/api/customer/wallet")) {
        return jsonResponse(wallet);
      }
      return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
    );
    await screen.findByText("12 积分");
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

  it("deletes an unpaid order from the visible list after closing it", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let closed = false;
    const fetchMock = vi.fn((url: string, options?: RequestInit) => {
      if (url.endsWith("/api/customer/wallet")) {
        return jsonResponse(wallet);
      }
      if (url.includes("/api/customer/wallet/transactions?")) {
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }
      if (url.endsWith("/api/customer/recharge-orders/order-pending")) {
        if (options?.method === "DELETE") {
          closed = true;
          return Promise.resolve({
            ok: true,
            status: 204,
            json: async () => undefined,
          });
        }
        return jsonResponse({
          order_no: "order-pending",
          status: closed ? "CLOSED" : "PENDING",
          amount_fen: 10000,
          credits: 10,
          channel: "wxpay",
          created_at: "2026-08-28 10:00:00",
          paid_at: null,
        });
      }
      if (url.includes("/api/customer/recharge-orders?")) {
        return jsonResponse({
          items: [
            {
              order_no: "order-pending",
              status: closed ? "CLOSED" : "PENDING",
              amount_fen: 10000,
              credits: 10,
              channel: "wxpay",
              created_at: "2026-08-28 10:00:00",
              paid_at: null,
            },
          ],
          total: 1,
          limit: 20,
          offset: 0,
        });
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(
      <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
    );

    expect(await screen.findByText("order-pending")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "删除待支付订单" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/customer/recharge-orders/order-pending"),
        expect.objectContaining({ method: "DELETE" }),
      ),
    );
    expect(screen.queryByText("order-pending")).toBeNull();
  });

  it("pages through the complete customer ledger and recharge order history", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/customer/wallet")) {
        return jsonResponse(wallet);
      }
      if (url.includes("/api/customer/wallet/transactions")) {
        const offset = Number(new URL(url).searchParams.get("offset") ?? "0");
        return jsonResponse({
          items: [
            {
              id: `tx-${offset}`,
              user_id: "user-1",
              type: "CHARGE",
              available_delta: 1,
              reserved_delta: 0,
              recharge_order_id: `order-${offset}`,
              task_id: null,
              billing_round: null,
              created_at:
                offset === 0 ? "2026-09-07 10:00:00" : "2026-08-01 09:00:00",
            },
          ],
          total: 21,
          limit: 20,
          offset,
        });
      }
      if (url.includes("/api/customer/recharge-orders?")) {
        const offset = Number(new URL(url).searchParams.get("offset") ?? "0");
        return jsonResponse({
          items: [
            {
              order_no: offset === 0 ? "recent-order" : "oldest-order",
              status: offset === 0 ? "PAID" : "CLOSED",
              amount_fen: 10000,
              credits: 10,
              channel: "alipay",
              created_at: "2026-09-07 10:00:00",
              paid_at: offset === 0 ? "2026-09-07 10:01:00" : null,
            },
          ],
          total: 21,
          limit: 20,
          offset,
        });
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
    render(
      <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
    );

    const ledger = (
      await screen.findByRole("heading", {
        name: "额度流水",
      })
    ).closest("section");
    expect(ledger).not.toBeNull();
    expect(
      within(ledger as HTMLElement).getByText("2026-09-07 10:00:00"),
    ).toBeInTheDocument();
    fireEvent.click(
      within(ledger as HTMLElement).getByRole("button", { name: "下一页" }),
    );
    expect(
      await within(ledger as HTMLElement).findByText("2026-08-01 09:00:00"),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(([url]) =>
        String(url).includes("/api/customer/recharge-orders?"),
      ),
    ).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "查看全部充值记录" }));
    const orderHistory = (
      await screen.findByRole("heading", {
        name: "充值订单历史",
      })
    ).closest("section");
    expect(orderHistory).not.toBeNull();
    expect(
      within(orderHistory as HTMLElement).getByText("recent-order"),
    ).toBeInTheDocument();
    fireEvent.click(
      within(orderHistory as HTMLElement).getByRole("button", {
        name: "下一页",
      }),
    );
    expect(
      await within(orderHistory as HTMLElement).findByText("oldest-order"),
    ).toBeInTheDocument();

    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).endsWith(
          "/api/customer/wallet/transactions?limit=20&offset=20",
        ),
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).endsWith(
          "/api/customer/recharge-orders?limit=20&offset=20",
        ),
      ),
    ).toBe(true);
  });

  it("keeps the last successful page and offers retry when either history request fails", async () => {
    let failLedgerPage = true;
    let failOrderPage = true;
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/customer/wallet")) {
        return jsonResponse(wallet);
      }
      if (url.includes("/api/customer/wallet/transactions?")) {
        const offset = Number(new URL(url).searchParams.get("offset") ?? "0");
        if (offset === 20 && failLedgerPage) {
          return Promise.reject(new Error("额度流水加载失败"));
        }
        return jsonResponse({
          items: [
            {
              id: `tx-${offset}`,
              user_id: "user-1",
              type: "CHARGE",
              available_delta: 1,
              reserved_delta: 0,
              recharge_order_id: `order-${offset}`,
              task_id: null,
              billing_round: null,
              created_at: offset === 0 ? "ledger-page-one" : "ledger-page-two",
            },
          ],
          total: 21,
          limit: 20,
          offset,
        });
      }
      if (url.includes("/api/customer/recharge-orders?")) {
        const offset = Number(new URL(url).searchParams.get("offset") ?? "0");
        if (offset === 20 && failOrderPage) {
          return Promise.reject(new Error("充值记录加载失败"));
        }
        return jsonResponse({
          items: [
            {
              order_no: offset === 0 ? "order-page-one" : "order-page-two",
              status: "PAID",
              amount_fen: 10000,
              credits: 10,
              channel: "alipay",
              created_at: "2026-09-07 10:00:00",
              paid_at: "2026-09-07 10:01:00",
            },
          ],
          total: 21,
          limit: 20,
          offset,
        });
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
    render(
      <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
    );

    const ledger = (await screen.findByText("ledger-page-one")).closest(
      "section",
    ) as HTMLElement;
    fireEvent.click(within(ledger).getByRole("button", { name: "下一页" }));
    expect(
      await within(ledger).findByText("额度流水加载失败"),
    ).toBeInTheDocument();
    expect(within(ledger).getByText("ledger-page-one")).toBeInTheDocument();
    expect(
      within(ledger).getByText("第 1 / 2 页（共 21 条）"),
    ).toBeInTheDocument();
    failLedgerPage = false;
    fireEvent.click(
      within(ledger).getByRole("button", { name: "重试加载额度流水" }),
    );
    expect(
      await within(ledger).findByText("ledger-page-two"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "查看全部充值记录" }));
    const orderHistory = (
      await screen.findByRole("heading", {
        name: "充值订单历史",
      })
    ).closest("section") as HTMLElement;
    expect(
      await within(orderHistory).findByText("order-page-one"),
    ).toBeInTheDocument();
    fireEvent.click(
      within(orderHistory).getByRole("button", { name: "下一页" }),
    );
    expect(
      await within(orderHistory).findByText("充值记录加载失败"),
    ).toBeInTheDocument();
    expect(
      within(orderHistory).getByText("order-page-one"),
    ).toBeInTheDocument();
    expect(
      within(orderHistory).getByText("第 1 / 2 页（共 21 条）"),
    ).toBeInTheDocument();
    failOrderPage = false;
    fireEvent.click(
      within(orderHistory).getByRole("button", { name: "重试加载充值记录" }),
    );
    expect(
      await within(orderHistory).findByText("order-page-two"),
    ).toBeInTheDocument();
  });

  it("reloads the latest history page after a delayed order creation", async () => {
    let resolveCreation:
      | ((value: Awaited<ReturnType<typeof jsonResponse>>) => void)
      | undefined;
    const delayedCreation = new Promise<
      Awaited<ReturnType<typeof jsonResponse>>
    >((resolve) => {
      resolveCreation = resolve;
    });
    let created = false;
    vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(
      () => undefined,
    );
    const fetchMock = vi.fn((url: string, options?: RequestInit) => {
      if (url.endsWith("/api/customer/wallet")) {
        return jsonResponse(wallet);
      }
      if (url.includes("/api/customer/wallet/transactions?")) {
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }
      if (url.includes("/api/customer/recharge-orders?")) {
        const offset = Number(new URL(url).searchParams.get("offset") ?? "0");
        return jsonResponse({
          items:
            offset === 0
              ? [
                  {
                    order_no: created ? "new-order" : "recent-order",
                    status: created ? "PENDING" : "PAID",
                    amount_fen: 10000,
                    credits: 10,
                    channel: "alipay",
                    created_at: "2026-09-07 10:00:00",
                    paid_at: created ? null : "2026-09-07 10:01:00",
                  },
                ]
              : [
                  {
                    order_no: "oldest-order",
                    status: "CLOSED",
                    amount_fen: 10000,
                    credits: 10,
                    channel: "alipay",
                    created_at: "2026-08-01 10:00:00",
                    paid_at: null,
                  },
                ],
          total: created ? 22 : 21,
          limit: 20,
          offset,
        });
      }
      if (
        url.endsWith("/api/customer/recharge-orders") &&
        options?.method === "POST"
      ) {
        return delayedCreation;
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
    render(
      <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
    );
    await screen.findByText("recent-order");
    fireEvent.click(screen.getByRole("button", { name: "查看全部充值记录" }));
    const orderHistory = (
      await screen.findByRole("heading", {
        name: "充值订单历史",
      })
    ).closest("section") as HTMLElement;
    await within(orderHistory).findByText("recent-order");

    fireEvent.click(screen.getByRole("button", { name: "充值200元" }));
    fireEvent.click(
      within(orderHistory).getByRole("button", { name: "下一页" }),
    );
    await within(orderHistory).findByText("oldest-order");
    created = true;
    resolveCreation?.(
      await jsonResponse(
        {
          order_no: "new-order",
          status: "PENDING",
          amount_fen: 20000,
          credits: 20,
          gateway_url: "https://zpayz.cn/submit.php",
          method: "POST",
          form_fields: {
            pid: "merchant",
            type: "alipay",
            out_trade_no: "new-order",
            sign: "signature",
            sign_type: "MD5",
          },
        },
        201,
      ),
    );

    await waitFor(() => {
      expect(within(orderHistory).queryByText("new-order")).toBeNull();
      expect(
        within(orderHistory).getByText("oldest-order"),
      ).toBeInTheDocument();
    });
    expect(
      fetchMock.mock.calls.filter(([url]) =>
        String(url).endsWith(
          "/api/customer/recharge-orders?limit=20&offset=20",
        ),
      ).length,
    ).toBeGreaterThanOrEqual(2);
  });

  it.each(["success", "failure"] as const)(
    "preserves an order-action error after the initial ledger and a late %s poll",
    async (pollOutcome) => {
      let resolveLedger:
        | ((value: Awaited<ReturnType<typeof jsonResponse>>) => void)
        | undefined;
      const delayedLedger = new Promise<
        Awaited<ReturnType<typeof jsonResponse>>
      >((resolve) => {
        resolveLedger = resolve;
      });
      let resolvePoll: (
        value: Awaited<ReturnType<typeof jsonResponse>>,
      ) => void = () => undefined;
      let rejectPoll: (error: Error) => void = () => undefined;
      const delayedPoll = new Promise<Awaited<ReturnType<typeof jsonResponse>>>(
        (resolve, reject) => {
          resolvePoll = resolve;
          rejectPoll = reject;
        },
      );
      const pendingOrder = {
        order_no: "order-pending",
        status: "PENDING",
        amount_fen: 10000,
        credits: 10,
        channel: "wxpay",
        created_at: "2026-09-07 10:00:00",
        paid_at: null,
      };
      const fetchMock = vi.fn((url: string, options?: RequestInit) => {
        if (url.endsWith("/api/customer/wallet")) {
          return jsonResponse(wallet);
        }
        if (url.includes("/api/customer/wallet/transactions?")) {
          return delayedLedger;
        }
        if (url.includes("/api/customer/recharge-orders?")) {
          return jsonResponse({
            items: [pendingOrder],
            total: 1,
            limit: 20,
            offset: 0,
          });
        }
        if (
          url.endsWith("/api/customer/recharge-orders/order-pending") &&
          options?.method === "DELETE"
        ) {
          return Promise.reject(new Error("关闭订单失败"));
        }
        if (url.endsWith("/api/customer/recharge-orders/order-pending")) {
          return delayedPoll;
        }
        return jsonResponse({});
      });
      vi.stubGlobal("fetch", fetchMock);
      vi.spyOn(window, "confirm").mockReturnValue(true);
      render(
        <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
      );

      await screen.findByText("order-pending");
      await waitFor(() =>
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringContaining(
            "/api/customer/recharge-orders/order-pending",
          ),
          expect.objectContaining({ method: "GET" }),
        ),
      );
      fireEvent.click(screen.getByRole("button", { name: "删除待支付订单" }));
      await waitFor(() => {
        expect(screen.getByText("关闭订单失败")).toBeInTheDocument();
      });

      await act(async () => {
        resolveLedger?.(
          await jsonResponse({
            items: [
              {
                id: "late-ledger",
                user_id: "user-1",
                type: "CHARGE",
                available_delta: 37,
                reserved_delta: 0,
                recharge_order_id: "ledger-order",
                task_id: null,
                billing_round: null,
                created_at: "2026-09-07 11:00:00",
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
          }),
        );
      });
      expect(await screen.findByText("+37 积分")).toBeInTheDocument();
      expect(screen.getByText("关闭订单失败")).toBeInTheDocument();

      // Deliver the real polling result after the user operation and ledger.
      // No sleep or transient DOM node can accidentally satisfy this ordering.
      await act(async () => {
        if (pollOutcome === "success") {
          resolvePoll(await jsonResponse(pendingOrder));
        } else {
          rejectPoll(new Error("查询订单失败"));
        }
      });
      expect(screen.getByText("关闭订单失败")).toBeInTheDocument();
      expect(screen.queryByText("查询订单失败")).not.toBeInTheDocument();
    },
  );

  it("clears a polling error when the next status poll succeeds", async () => {
    const pendingOrder = {
      order_no: "poll-recovery",
      status: "PENDING",
      amount_fen: 10000,
      credits: 10,
      channel: "wxpay",
      created_at: "2026-09-07 10:00:00",
      paid_at: null,
    };
    const poll = vi
      .fn()
      .mockRejectedValueOnce(new Error("查询订单暂时失败"))
      .mockImplementation(() => jsonResponse(pendingOrder));
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/api/customer/wallet")) return jsonResponse(wallet);
        if (url.endsWith("/api/customer/recharge-orders/poll-recovery"))
          return poll();
        if (url.includes("/api/customer/recharge-orders?")) {
          return jsonResponse({
            items: [pendingOrder],
            total: 1,
            limit: 20,
            offset: 0,
          });
        }
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }),
    );
    vi.useFakeTimers();
    const view = render(
      <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
    );
    try {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByText("查询订单暂时失败")).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
      expect(poll).toHaveBeenCalledTimes(2);
      expect(screen.queryByText("查询订单暂时失败")).not.toBeInTheDocument();
      expect(
        screen.getByText("支付结果确认中，请完成支付后返回本页。"),
      ).toBeInTheDocument();
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });
});
