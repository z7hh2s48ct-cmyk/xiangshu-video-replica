import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { adminRead, downloadBillingCsv } from "../api.admin";
import { BillingEconomics } from "./BillingEconomics";

vi.mock("../api.admin", () => ({
  adminRead: vi.fn(),
  adminWrite: vi.fn(),
  downloadBillingCsv: vi.fn(),
}));

/** 与组件 initialFilters 同口径的上海时区「今天」，断言查询窗口用。 */
function shanghaiToday() {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

const totals = {
  operation_count: 3,
  provider_call_count: 1,
  shared_collection_charge_count: 0,
  charged_credits: 5,
  known_revenue_fen: 200,
  known_cost_fen: 50,
  profit_fen: 150,
  unknown_cost_count: 1,
  unknown_revenue_count: 0,
  pending_count: 1,
  refunded_credits: 0,
  seconds: "3",
  images: "0",
  calls: "0",
  platform_cost_fen: "10",
};

function mockBilling({
  items = [],
  total,
  detail,
}: {
  items?: unknown[];
  total?: number;
  detail?: unknown;
} = {}) {
  // 调用记录不随测试清理，显式重置后再断言本轮请求，避免读到上一轮的残留。
  const read = vi.mocked(adminRead);
  read.mockReset();
  read.mockImplementation(async (url) => {
    if (url.includes("catalog")) return { services: [] } as never;
    if (url.includes("statistics"))
      return { totals, periods: [], basis: "按结算时间统计" } as never;
    // 详情端点形如 /billing/operations/{id}；列表端点带 ? 查询串。
    if (/operations\/[^?]+$/.test(String(url))) return (detail ?? {}) as never;
    return { items, total: total ?? items.length } as never;
  });
}

/** 已发出的请求地址中，包含给定片段的那些。 */
function callsTo(fragment: string) {
  return vi
    .mocked(adminRead)
    .mock.calls.map(([url]) => String(url))
    .filter((url) => url.includes(fragment));
}

const pendingOperation = {
  id: "op-detail",
  user_id: "u-1",
  collection_batch_id: null,
  username: "测试用户",
  service: "asr",
  unit: "second",
  budget_units: "10.000000",
  actual_units: "8.000000",
  charged_credits: 2,
  reserved_credits: 3,
  revenue_fen: "100",
  nominal_revenue_fen: "100",
  cost_fen: null,
  profit_fen: null,
  state: "PENDING",
  completed_at: null,
  created_at: "2026-09-28T02:00:00+00:00",
  pricing_snapshot_json: JSON.stringify({
    enabled: true,
    unit_credits: "2",
    unit_rounding: "ceil",
    discount_basis_points: 10000,
    version: 3,
  }),
};

test.each(["pending", "unknown_cost"] as const)(
  "arrives from the dashboard todo with today's window and attention intent %s",
  async (attention) => {
    mockBilling();
    render(<BillingEconomics initialAttention={attention} />);
    await waitFor(() =>
      expect(callsTo("statistics").length).toBeGreaterThan(0),
    );
    const today = shanghaiToday();
    const statistics = callsTo("statistics")[0];
    expect(statistics).toContain(`start=${today}`);
    expect(statistics).toContain(`end=${today}`);
    // 总览待办只承诺「今日操作待结算 / 成本待核对」的条数一致：
    // 清单端点消费 attention，点进去必须落到同口径的行。
    const operations = callsTo("operations?")[0];
    expect(operations).toContain(`start=${today}`);
    expect(operations).toContain(`end=${today}`);
    expect(operations).toContain(`attention=${attention}`);
    expect(operations).toContain("limit=100&offset=0");
  },
);

test("defaults to the month so far without an attention filter", async () => {
  mockBilling();
  render(<BillingEconomics />);
  await waitFor(() => expect(callsTo("statistics").length).toBeGreaterThan(0));
  const today = shanghaiToday();
  for (const fragment of ["statistics", "operations?"]) {
    const url = callsTo(fragment)[0];
    expect(url).toContain(`start=${today.slice(0, 7)}-01`);
    expect(url).toContain(`end=${today}`);
    expect(url).not.toContain("attention=");
    // 空筛选不占位：用户 ID、业务、模块、服务商都不出现在查询串里。
    expect(url).not.toContain("user_id=");
    expect(url).not.toContain("service=");
  }
});

test("re-queries the list with the selected intent after submit", async () => {
  mockBilling();
  render(<BillingEconomics />);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "查询" })).toBeEnabled(),
  );
  fireEvent.change(screen.getByLabelText("只看"), {
    target: { value: "unknown_cost" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  await waitFor(() =>
    expect(
      callsTo("operations?").some((url) =>
        url.includes("attention=unknown_cost"),
      ),
    ).toBe(true),
  );
});

test("opens the request detail with snapshot, usage and a reconciliation form", async () => {
  mockBilling({ items: [pendingOperation], detail: pendingOperation });
  render(<BillingEconomics />);
  const table = await screen.findByRole("table", { name: "生成明细" });
  fireEvent.click(within(table).getByRole("button", { name: "查看详情" }));
  const detail = await screen.findByRole("complementary", {
    name: "生成核算详情",
  });
  expect(detail).toHaveTextContent("生成编号：op-detail · 处理中 / 待核对");
  expect(detail).toHaveTextContent(
    "受理时售价：2 积分 / 秒；折扣 10 折；价格版本 3",
  );
  expect(detail).toHaveTextContent("预算 10.0 秒，实际 8.0 秒");
  // 管理端可对缺时长的成功交付补录：PENDING + asr 在允许补录的业务列表里。
  expect(
    within(detail).getByRole("form", { name: "核对成功时长" }),
  ).toBeInTheDocument();
});

test("auditor sessions read the same detail without reconciliation forms", async () => {
  mockBilling({ items: [pendingOperation], detail: pendingOperation });
  render(<BillingEconomics readOnly />);
  const table = await screen.findByRole("table", { name: "生成明细" });
  fireEvent.click(within(table).getByRole("button", { name: "查看详情" }));
  const detail = await screen.findByRole("complementary", {
    name: "生成核算详情",
  });
  expect(detail).toHaveTextContent("生成编号：op-detail");
  expect(
    within(detail).queryByRole("form", { name: "核对成功时长" }),
  ).toBeNull();
});

test("exports the current query and reports the download", async () => {
  vi.mocked(downloadBillingCsv).mockReset().mockResolvedValue("明细已导出。");
  mockBilling();
  render(<BillingEconomics initialAttention="pending" />);
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "导出当前查询 CSV" }),
    ).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "导出当前查询 CSV" }));
  expect(await screen.findByText("明细已导出。")).toBeInTheDocument();
  expect(vi.mocked(downloadBillingCsv)).toHaveBeenCalledWith(
    expect.stringContaining("attention=pending"),
  );
});

test("surfaces a load failure as an alert and hides the report tables", async () => {
  const read = vi.mocked(adminRead);
  read.mockReset();
  read.mockImplementation(async (url) => {
    if (url.includes("catalog")) return { services: [] } as never;
    throw new Error("读取经营统计失败：统计暂不可用（500）");
  });
  render(<BillingEconomics />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "读取经营统计失败：统计暂不可用（500）",
  );
  expect(screen.queryByRole("table", { name: "周期汇总" })).toBeNull();
  expect(screen.queryByRole("table", { name: "生成明细" })).toBeNull();
});
