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

// 管理端配置的档位：200 元 → 21000 积分（含赠送 1000），无折扣权益。
const basicPackage = {
  id: "pkg-200",
  name: "标准档",
  amount_fen: 20000,
  credits: 21000,
  discount_rate: null,
  discount_interfaces: [],
  sort_order: 0,
  is_active: true,
  version: 1,
  created_at: "2026-09-22 10:00:00",
  updated_at: "2026-09-22 10:00:00",
};

// 带权益档位：100 元 → 11000 积分（含赠送 1000）＋视频生成 9 折。
const discountPackage = {
  id: "pkg-100",
  name: "畅享档",
  amount_fen: 10000,
  credits: 11000,
  discount_rate: "0.9000",
  discount_interfaces: ["video_generation"],
  sort_order: 1,
  is_active: true,
  version: 1,
  created_at: "2026-09-22 10:00:00",
  updated_at: "2026-09-22 10:00:00",
};

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

type LedgerFixtureRow = Record<string, unknown>;

/** 按服务端的规则把逐笔流水行合并成条目：同一 operation 的暂扣 / 实扣 / 退回是一条。 */
function ledgerFromRows(
  rows: LedgerFixtureRow[],
  page: { total?: number; limit?: number; offset?: number } = {},
) {
  const cycleTypes = new Set(["RESERVE", "SETTLE", "RELEASE"]);
  const groups = new Map<string, LedgerFixtureRow[]>();
  const order: { key: string; cycle: boolean }[] = [];
  for (const row of rows) {
    const cycle =
      Boolean(row.billing_operation_id) && cycleTypes.has(String(row.type));
    const key = cycle ? String(row.billing_operation_id) : String(row.id);
    if (!groups.has(key)) {
      groups.set(key, []);
      order.push({ key, cycle });
    }
    groups.get(key)?.push(row);
  }
  // 服务端保证周期内按资金走向排：暂扣 → 实扣 → 退回（不靠同一事务里相同的时间戳）。
  const typeOrder: Record<string, number> = {
    RESERVE: 0,
    SETTLE: 1,
    RELEASE: 2,
  };
  const items = order.map(({ key, cycle }) => {
    const members = [...(groups.get(key) ?? [])];
    if (cycle) {
      members.sort(
        (a, b) =>
          (typeOrder[String(a.type)] ?? 9) - (typeOrder[String(b.type)] ?? 9),
      );
    }
    const sum = (type: string, pick: (row: LedgerFixtureRow) => number) =>
      members
        .filter((row) => row.type === type)
        .reduce((total, row) => total + pick(row), 0);
    const reserved = sum("RESERVE", (row) => Number(row.reserved_delta));
    const charged = sum("SETTLE", (row) => -Number(row.reserved_delta));
    const refunded = sum("RELEASE", (row) => Number(row.available_delta));
    const outcome = !cycle
      ? "POSTED"
      : charged > 0
        ? refunded > 0
          ? "PARTIAL"
          : "COMPLETED"
        : refunded > 0
          ? "FAILED"
          : "PENDING";
    const times = members.map((row) => String(row.created_at)).sort();
    return {
      key,
      kind: cycle ? "cycle" : "row",
      outcome,
      reserved_credits: cycle ? reserved : 0,
      charged_credits: cycle ? charged : 0,
      refunded_credits: cycle ? refunded : 0,
      net_available_delta: members.reduce(
        (total, row) => total + Number(row.available_delta),
        0,
      ),
      started_at: times[0],
      updated_at: times[times.length - 1],
      rows: members,
    };
  });
  const count = (...outcomes: string[]) =>
    items.filter((item) => outcomes.includes(item.outcome)).length;
  return {
    items,
    total: page.total ?? items.length,
    limit: page.limit ?? 20,
    offset: page.offset ?? 0,
    counts: {
      total: items.length,
      pending: count("PENDING"),
      completed: count("COMPLETED"),
      refunded: count("PARTIAL", "FAILED"),
      posted: count("POSTED"),
    },
  };
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

/** 走完一次产品级确认框：勾选「我已知晓」→ 点确认按钮（P0 清单 #2 起的高危交互）。 */
async function acknowledgeAndConfirm(dialogName: string, confirmLabel: string) {
  const dialog = await screen.findByRole("dialog", { name: dialogName });
  fireEvent.click(within(dialog).getByRole("checkbox"));
  fireEvent.click(within(dialog).getByRole("button", { name: confirmLabel }));
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
        if (url.endsWith("/api/customer/recharge-packages")) {
          return jsonResponse({ items: [discountPackage] });
        }
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
    // 档位由管理端配置：赠送积分与折扣权益都要透传到客户可见的卡片上。
    const packageCard = screen.getByRole("button", { name: "购买套餐畅享档" });
    expect(packageCard).toHaveTextContent("100元 → 11000 积分");
    expect(packageCard).toHaveTextContent("含赠送 1000 积分");
    expect(packageCard).toHaveTextContent("视频生成 9折");
  });

  it("disables packages priced below the effective minimum recharge amount", async () => {
    // 管理端可配任意档位（含 50 元档），但起充额 100 元时低于它的档位不可下单：
    // 下单会被后端 422 RECHARGE_PACKAGE_BELOW_MINIMUM 拒绝，前端必须先行置灰。
    const belowMinimumPackage = {
      ...basicPackage,
      id: "pkg-50",
      name: "五十元档",
      amount_fen: 5000,
      credits: 5500,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.includes("/api/generation/price-quote")) {
          return Promise.reject(new Error("生成单价暂不可用，请稍后重试。"));
        }
        if (url.endsWith("/api/customer/wallet")) return jsonResponse(wallet);
        if (url.endsWith("/api/customer/recharge-packages")) {
          return jsonResponse({ items: [belowMinimumPackage, basicPackage] });
        }
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }),
    );
    render(
      <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
    );
    const lowCard = await screen.findByRole("button", {
      name: "购买套餐五十元档",
    });
    expect(lowCard).toBeDisabled();
    expect(lowCard).toHaveTextContent("低于起充金额");
    const okCard = screen.getByRole("button", { name: "购买套餐标准档" });
    expect(okCard).toBeEnabled();
  });

  it("shows the balance and creates a preset recharge under the customer session", async () => {
    const submit = vi
      .spyOn(HTMLFormElement.prototype, "submit")
      .mockImplementation(() => undefined);
    const fetchMock = vi.fn((url: string, options?: RequestInit) => {
      if (url.endsWith("/api/customer/recharge-packages")) {
        return jsonResponse({ items: [basicPackage] });
      }
      if (url.endsWith("/api/customer/wallet")) {
        return jsonResponse(wallet);
      }
      if (url.includes("/api/customer/wallet/ledger?")) {
        return jsonResponse(
          ledgerFromRows([
            {
              id: "tx-1",
              user_id: "user-1",
              type: "CHARGE",
              available_delta: 10,
              reserved_delta: 0,
              recharge_order_id: "order-1",
              task_id: null,
              billing_round: null,
              credit_source: "zpay",
              created_at: "2026-08-19T02:00:00Z",
            },
          ]),
        );
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
    expect(screen.getByText("暂扣中 2 积分")).toBeInTheDocument();
    // 入账不是消费：标题说「积分入账」，来源说「在线充值」，不落到「早期版本消费」上。
    expect(await screen.findByText("积分入账")).toBeInTheDocument();
    expect(screen.getByText("在线充值")).toBeInTheDocument();
    expect(screen.queryByText("早期版本消费")).toBeNull();

    // 套餐点击直接按套餐下单：到账积分与权益随订单快照，不走自定义金额。
    fireEvent.click(screen.getByRole("button", { name: "购买套餐标准档" }));

    await waitFor(() => expect(submit).toHaveBeenCalledOnce());
    const paymentForm = submit.mock.instances[0] as HTMLFormElement;
    expect(paymentForm.target).toBe("_blank");
    expect(paymentForm.getAttribute("rel")).toBe("noopener");
    const createCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url).endsWith("/api/customer/recharge-orders") &&
        options?.method === "POST",
    );
    expect(createCall?.[1]?.body).toBe(
      JSON.stringify({ amount_fen: 20000, package_id: "pkg-200" }),
    );
  });

  it("shows the frozen pricing basis of a consumption row in the ledger", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/api/customer/wallet")) return jsonResponse(wallet);
        if (url.includes("/api/customer/wallet/ledger?")) {
          return jsonResponse(
            ledgerFromRows([
              {
                id: "tx-priced",
                user_id: "user-1",
                type: "SETTLE",
                available_delta: 0,
                reserved_delta: -3,
                recharge_order_id: null,
                task_id: "task-1",
                billing_round: 2,
                created_at: "2026-09-22 10:00:00",
                service: "asr",
                service_name: "语音转写",
                actor_user_id: "sub-1",
                actor_name: "剪辑助手",
                credit_price_version: 3,
                pricing: {
                  service: "asr",
                  version: 3,
                  unit: "second",
                  units: "3.000000",
                  unit_credits: "2.000000",
                  unit_rounding: "ceil",
                  discount_basis_points: 9500,
                  consumption_rounding: "floor",
                  credits: 5,
                  enabled: true,
                  free_reason: null,
                },
              },
              {
                // 充值行没有计价快照：不得出现空的计费依据。
                id: "tx-charge",
                user_id: "user-1",
                type: "CHARGE",
                available_delta: 10,
                reserved_delta: 0,
                recharge_order_id: "order-1",
                task_id: null,
                billing_round: null,
                created_at: "2026-09-22 09:00:00",
                pricing: null,
              },
            ]),
          );
        }
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }),
    );
    render(
      <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
    );

    expect(await screen.findByText("语音转写")).toBeInTheDocument();
    const summary = screen.getByText("计费依据 · 费率 V3");
    expect(summary.tagName).toBe("SUMMARY");
    expect(summary.closest("details")?.open).toBe(false);
    expect(
      screen.getByText("单价：2 积分/秒，不足 1 秒按 1 秒计"),
    ).toBeInTheDocument();
    expect(screen.getByText("计费轮次：第 2 轮")).toBeInTheDocument();
    expect(screen.getByText("操作人：剪辑助手")).toBeInTheDocument();
    expect(screen.getAllByText(/计费依据/)).toHaveLength(1);
  });

  it("merges a settled billing cycle into one row with expandable detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/api/customer/wallet")) return jsonResponse(wallet);
        if (url.includes("/api/customer/wallet/ledger?")) {
          const cycle = {
            user_id: "user-1",
            recharge_order_id: null,
            task_id: "task-1",
            billing_round: 1,
            billing_operation_id: "op-1",
            service: "asr",
            service_name: "语音转写",
            auth_source: "session",
          };
          return jsonResponse(
            ledgerFromRows([
              {
                ...cycle,
                id: "tx-release",
                type: "RELEASE",
                available_delta: 2,
                reserved_delta: -2,
                created_at: "2026-09-22T02:00:01Z",
              },
              {
                ...cycle,
                id: "tx-settle",
                type: "SETTLE",
                available_delta: 0,
                reserved_delta: -3,
                created_at: "2026-09-22T02:00:01Z",
              },
              {
                ...cycle,
                id: "tx-reserve",
                type: "RESERVE",
                available_delta: -5,
                reserved_delta: 5,
                created_at: "2026-09-22T02:00:00Z",
              },
              {
                id: "tx-charge",
                user_id: "user-1",
                type: "CHARGE",
                available_delta: 10,
                reserved_delta: 0,
                recharge_order_id: "order-1",
                task_id: null,
                billing_round: null,
                created_at: "2026-09-22T01:00:00Z",
              },
            ]),
          );
        }
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }),
    );
    render(
      <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
    );

    // 合并：三笔只剩一行，资金去向讲清暂扣 → 实扣 → 退回，花费是整组净额。
    expect(await screen.findByText("暂扣 5")).toBeVisible();
    expect(screen.getByText("实扣 3")).toBeVisible();
    expect(screen.getByText("退回 +2")).toBeVisible();
    expect(screen.getByText("-3 积分")).toBeVisible();
    expect(screen.queryByRole("table", { name: "逐笔明细" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /查看 3 笔明细/ }));
    const itemised = await screen.findByRole("table", { name: "逐笔明细" });
    expect(
      within(itemised)
        .getAllByRole("row")
        .slice(1)
        .map((row) => row.children[1].textContent),
    ).toEqual(["暂扣", "实扣", "退回"]);
    expect(within(itemised).getByText("+2 积分")).toBeVisible();
  });

  it("resumes polling an outstanding pending payment after a remount", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/customer/recharge-packages")) {
        return jsonResponse({ items: [] });
      }
      if (url.endsWith("/api/customer/wallet")) {
        return jsonResponse(wallet);
      }
      if (url.includes("/api/customer/wallet/ledger?")) {
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
    let closed = false;
    const fetchMock = vi.fn((url: string, options?: RequestInit) => {
      if (url.endsWith("/api/customer/recharge-packages")) {
        return jsonResponse({ items: [] });
      }
      if (url.endsWith("/api/customer/wallet")) {
        return jsonResponse(wallet);
      }
      if (url.includes("/api/customer/wallet/ledger?")) {
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
    await acknowledgeAndConfirm("删除这个待支付订单？", "删除订单");

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
      if (url.includes("/api/customer/wallet/ledger")) {
        const offset = Number(new URL(url).searchParams.get("offset") ?? "0");
        return jsonResponse(
          ledgerFromRows(
            [
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
                  offset === 0
                    ? "2026-09-07T02:00:00Z"
                    : "2026-08-01T01:00:00Z",
              },
            ],
            { total: 21, offset },
          ),
        );
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
      await within(ledger as HTMLElement).findByText("2026/9/7 10:00:00"),
    ).toBeInTheDocument();
    fireEvent.click(
      within(ledger as HTMLElement).getByRole("button", { name: "下一页" }),
    );
    expect(
      await within(ledger as HTMLElement).findByText("2026/8/1 09:00:00"),
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
        String(url).endsWith("/api/customer/wallet/ledger?limit=20&offset=20"),
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
      if (url.includes("/api/customer/wallet/ledger?")) {
        const offset = Number(new URL(url).searchParams.get("offset") ?? "0");
        if (offset === 20 && failLedgerPage) {
          return Promise.reject(new Error("额度流水加载失败"));
        }
        return jsonResponse(
          ledgerFromRows(
            [
              {
                id: `tx-${offset}`,
                user_id: "user-1",
                type: "CHARGE",
                available_delta: 1,
                reserved_delta: 0,
                recharge_order_id: `order-${offset}`,
                task_id: null,
                billing_round: null,
                // 解析不了的时间原样显示，正好当作「翻到了哪一页」的标记。
                created_at:
                  offset === 0 ? "ledger-page-one" : "ledger-page-two",
              },
            ],
            { total: 21, offset },
          ),
        );
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
      if (url.endsWith("/api/customer/recharge-packages")) {
        return jsonResponse({ items: [basicPackage] });
      }
      if (url.endsWith("/api/customer/wallet")) {
        return jsonResponse(wallet);
      }
      if (url.includes("/api/customer/wallet/ledger?")) {
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

    fireEvent.click(screen.getByRole("button", { name: "购买套餐标准档" }));
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
        if (url.includes("/api/customer/wallet/ledger?")) {
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
      await acknowledgeAndConfirm("删除这个待支付订单？", "删除订单");
      // 订单动作的错误留在确认框内（P0 清单 #2：失败可就地重试），
      // 不再走页面的错误条。
      const orderDialog = await screen.findByRole("dialog", {
        name: "删除这个待支付订单？",
      });
      await waitFor(() => {
        expect(within(orderDialog).getByRole("alert")).toHaveTextContent(
          "关闭订单失败",
        );
      });

      await act(async () => {
        resolveLedger?.(
          await jsonResponse(
            ledgerFromRows([
              {
                id: "late-ledger",
                user_id: "user-1",
                type: "CHARGE",
                available_delta: 37,
                reserved_delta: 0,
                recharge_order_id: "ledger-order",
                task_id: null,
                billing_round: null,
                created_at: "2026-09-07T03:00:00Z",
              },
            ]),
          ),
        );
      });
      expect(await screen.findByText("+37 积分")).toBeInTheDocument();
      expect(within(orderDialog).getByRole("alert")).toHaveTextContent(
        "关闭订单失败",
      );

      // Deliver the real polling result after the user operation and ledger.
      // No sleep or transient DOM node can accidentally satisfy this ordering.
      await act(async () => {
        if (pollOutcome === "success") {
          resolvePoll(await jsonResponse(pendingOrder));
        } else {
          rejectPoll(new Error("查询订单失败"));
        }
      });
      // 用户动作的结论不被后台轮询的迟到结果改写——这条保证现在落在确认框里。
      expect(within(orderDialog).getByRole("alert")).toHaveTextContent(
        "关闭订单失败",
      );
      expect(within(orderDialog).getByRole("alert")).not.toHaveTextContent(
        "查询订单失败",
      );
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

  it("shows the discount granted by a package on the price card", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.includes("/api/generation/price-quote")) {
          return jsonResponse({
            resolution: url.includes("resolution=2K") ? "2K" : "768P",
            duration_seconds: 4,
            quantity: 1,
            unit_price_fen_per_second: 1000,
            estimated_seconds: 4,
            estimated_price_fen: 4000,
            discount_rate: "0.9000",
            discount_source: "recharge_package",
          });
        }
        if (url.endsWith("/api/customer/wallet")) {
          return jsonResponse(wallet);
        }
        if (url.endsWith("/api/customer/recharge-packages")) {
          return jsonResponse({ items: [discountPackage] });
        }
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }),
    );
    render(
      <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
    );

    // 后端折扣来源是 token（recharge_package），面向客户要翻译成「充值套餐」。
    expect(await screen.findByText("已享9折（充值套餐）")).toBeInTheDocument();
  });

  it("keeps the custom amount usable when packages fail to load", async () => {
    const submit = vi
      .spyOn(HTMLFormElement.prototype, "submit")
      .mockImplementation(() => undefined);
    const fetchMock = vi.fn((url: string, options?: RequestInit) => {
      if (url.endsWith("/api/customer/recharge-packages")) {
        return Promise.reject(new Error("套餐接口不可用"));
      }
      if (url.endsWith("/api/customer/wallet")) {
        return jsonResponse(wallet);
      }
      if (url.includes("/api/customer/wallet/ledger?")) {
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
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
            order_no: "202609220001",
            status: "PENDING",
            amount_fen: 10000,
            credits: 10,
            gateway_url: "https://zpayz.cn/submit.php",
            method: "POST",
            form_fields: {
              pid: "merchant",
              type: "alipay",
              out_trade_no: "202609220001",
              sign: "signature",
              sign_type: "MD5",
            },
          },
          201,
        );
      }
      return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerWalletPanel store={fakeStore()} onSessionExpired={vi.fn()} />,
    );

    expect(
      await screen.findByText("充值套餐暂不可用，可使用自定义金额充值。"),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("自定义充值金额（元）"), {
      target: { value: "100" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认充值" }));

    await waitFor(() => expect(submit).toHaveBeenCalledOnce());
    const createCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url).endsWith("/api/customer/recharge-orders") &&
        options?.method === "POST",
    );
    // 自定义金额不带 package_id：按基础汇率到账，不享受套餐赠送/权益。
    expect(createCall?.[1]?.body).toBe(JSON.stringify({ amount_fen: 10000 }));
  });
});
