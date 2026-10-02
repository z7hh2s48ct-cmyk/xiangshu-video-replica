import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { adminRead, exportSummaryReportCsv } from "../api.admin";
import { AnalyticsPage } from "./AnalyticsPage";

vi.mock("../api.admin", () => ({
  adminRead: vi.fn(),
  downloadBillingCsv: vi.fn(),
  exportSummaryReportCsv: vi.fn(),
}));

/** 新导航：默认页签是经营看板，计费明细在「成本核对」页签下。 */
async function openCostTab() {
  fireEvent.click(screen.getByRole("tab", { name: "成本核对" }));
  return screen.findByRole("table", { name: "生成明细" });
}

test.each([
  ["68.000000", "68.0 秒"],
  ["12.350000", "12.4 秒"],
  ["0.000000", "0.0 秒"],
  [null, "处理中 秒"],
])(
  "formats request usage %s without treating pending as zero",
  async (usage, expected) => {
    vi.mocked(adminRead).mockImplementation(async (url) => {
      if (url.includes("catalog")) return { services: [] } as never;
      if (url.includes("statistics"))
        return { totals: {}, periods: [], basis: "" } as never;
      return {
        items: [
          {
            id: "usage-row",
            username: "测试用户",
            service: "asr",
            unit: "second",
            state: usage === null ? "PENDING" : "SUCCEEDED",
            actual_units: usage,
            charged_credits: 0,
            revenue_fen: "0",
            nominal_revenue_fen: "0",
            cost_fen: "0",
            profit_fen: "0",
          },
        ],
        total: 1,
      } as never;
    });
    render(<AnalyticsPage />);
    const table = await openCostTab();
    expect(
      within(table).getByRole("cell", { name: expected }),
    ).toBeInTheDocument();
  },
);

test.each([
  ["0", "136", "¥0.00", "¥1.36"],
  ["100", "225", "¥1.00", "¥2.25"],
  [null, "75", "待核对", "¥0.75"],
])(
  "distinguishes paid revenue %s from consumed face value %s",
  async (revenue, nominal, paidDisplay, nominalDisplay) => {
    const operation = {
      id: "revenue-row",
      username: "测试用户",
      service: "asr",
      unit: "second",
      state: "SUCCEEDED",
      actual_units: "68.000000",
      budget_units: "68.267000",
      charged_credits: 136,
      reserved_credits: 138,
      revenue_fen: revenue,
      nominal_revenue_fen: nominal,
      cost_fen: "68",
      profit_fen: null,
      pricing_snapshot_json: JSON.stringify({
        enabled: true,
        unit_credits: "2",
        discount_basis_points: 10000,
        version: 1,
      }),
      attempts: [
        {
          id: "attempt",
          service: "asr",
          provider: "provider",
          unit: "second",
          usage: "68.267000",
          state: "ACTUAL",
          unit_cost_fen: "1",
          effective_cost_fen: "68",
        },
      ],
    };
    vi.mocked(adminRead).mockImplementation(async (url) => {
      if (url.includes("catalog")) return { services: [] } as never;
      if (url.includes("statistics"))
        return { totals: {}, periods: [], basis: "" } as never;
      if (url.endsWith("/revenue-row")) return operation as never;
      return { items: [operation], total: 1 } as never;
    });
    render(<AnalyticsPage readOnly />);
    const table = await openCostTab();
    // 成本核对页签的明细表只含成本口径列（收入结论在经营看板），按售价折合
    // 与确认收入两列随利润总览页签移除；金额口径由详情抽屉承载。
    const headers = within(table)
      .getAllByRole("columnheader")
      .map((cell) => cell.textContent);
    expect(headers).not.toContain("按售价折合");
    expect(headers).not.toContain("确认收入");
    fireEvent.click(within(table).getByRole("button", { name: "查看详情" }));
    const detail = await screen.findByRole("complementary", {
      name: "生成核算详情",
    });
    expect(detail).toHaveTextContent(
      `按售价折合 ${nominalDisplay} · 确认收入 ${paidDisplay}`,
    );
    expect(detail).toHaveTextContent("预算 68.3 秒，实际 68.0 秒");
    expect(
      within(detail).getByRole("cell", { name: "68.3 秒" }),
    ).toBeInTheDocument();
  },
);

test("lands on the business dashboard by default and keeps cost page behind its tab", async () => {
  vi.mocked(adminRead).mockImplementation(async (url) => {
    if (url.includes("business/overview"))
      return {
        start: "2026-09-01",
        end: "2026-09-30",
        prev_start: "2026-08-01",
        prev_end: "2026-08-31",
        metrics: {
          recharge_fen: 15000,
          paying_customers: 2,
          new_paying_customers: 1,
          revenue_fen: 100,
          cost_fen: 2.5,
          unknown_cost_count: 1,
          pending_count: 0,
          prepaid_credits: 355,
          prepaid_fen: 355,
        },
        prev: {
          recharge_fen: 0,
          paying_customers: 0,
          new_paying_customers: 0,
          revenue_fen: 0,
          cost_fen: 0,
          unknown_cost_count: 0,
          pending_count: 0,
          prepaid_credits: 0,
          prepaid_fen: 0,
        },
        daily: [],
        modules: [],
        top_customers: [],
      } as never;
    if (url.includes("catalog")) return { services: [] } as never;
    if (url.includes("statistics"))
      return { totals: {}, periods: [], basis: "" } as never;
    return { items: [], total: 0 } as never;
  });
  render(<AnalyticsPage />);

  const dashboard = await screen.findByRole("region", { name: "经营看板" });
  expect(within(dashboard).getByText("¥150.00")).toBeInTheDocument();
  expect(
    within(dashboard).getAllByText(/含 1 项待核对/).length,
  ).toBeGreaterThan(0);
  expect(
    within(dashboard).queryByRole("table", { name: "生成明细" }),
  ).toBeNull();

  fireEvent.click(screen.getByRole("tab", { name: "成本核对" }));
  const cost = await screen.findByRole("table", { name: "周期汇总" });
  expect(
    within(cost).getByRole("columnheader", { name: "平台承担成本" }),
  ).toBeInTheDocument();
  // 队列卡就位：三个队列与清单同口径。
  expect(screen.getByRole("button", { name: /待结算/ })).toBeInTheDocument();
});

test("cost queue cards count with the same scope as the filtered list", async () => {
  vi.mocked(adminRead).mockImplementation(async (url) => {
    if (url.includes("business/overview")) return { metrics: null } as never;
    if (url.includes("catalog")) return { services: [] } as never;
    if (url.includes("statistics"))
      return { totals: {}, periods: [], basis: "" } as never;
    if (url.includes("attention=pending"))
      return { items: [], total: 3 } as never;
    if (
      url.includes("attention=unknown_cost") &&
      url.includes("start=2019-01-01")
    )
      return { items: [], total: 7 } as never;
    if (url.includes("attention=unknown_cost"))
      return { items: [], total: 5 } as never;
    return { items: [], total: 0 } as never;
  });
  render(<AnalyticsPage />);
  fireEvent.click(screen.getByRole("tab", { name: "成本核对" }));

  await waitFor(() =>
    expect(screen.getByRole("button", { name: /待结算/ })).toHaveTextContent(
      "3",
    ),
  );
  expect(screen.getByRole("button", { name: /待核对成本/ })).toHaveTextContent(
    "5",
  );
  expect(screen.getByRole("button", { name: /历史待核对/ })).toHaveTextContent(
    "7",
  );

  // 点击「历史待核对」→ 明细区以全历史 + unknown_cost 筛选重挂（明细请求带 limit=100）。
  fireEvent.click(screen.getByRole("button", { name: /历史待核对/ }));
  await waitFor(() => {
    const called = vi
      .mocked(adminRead)
      .mock.calls.some(
        ([url]) =>
          String(url).includes("start=2019-01-01") &&
          String(url).includes("attention=unknown_cost") &&
          String(url).includes("limit=100"),
      );
    expect(called).toBe(true);
  });
});

test("attention dropdown offers revenue-pending alongside cost filters", async () => {
  vi.mocked(adminRead).mockImplementation(async (url) => {
    if (url.includes("business/overview")) return { metrics: null } as never;
    if (url.includes("catalog")) return { services: [] } as never;
    if (url.includes("statistics"))
      return { totals: {}, periods: [], basis: "" } as never;
    return { items: [], total: 0 } as never;
  });
  render(<AnalyticsPage />);
  await openCostTab();

  const attention = screen.getByLabelText("只看");
  expect(
    within(attention).getByRole("option", { name: "收入待核对" }),
  ).toBeInTheDocument();
  fireEvent.change(attention, { target: { value: "unknown_revenue" } });
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  await waitFor(() => {
    const called = vi
      .mocked(adminRead)
      .mock.calls.some(([url]) => String(url).includes("unknown_revenue"));
    expect(called).toBe(true);
  });
});

test("customer name search reaches the operations query", async () => {
  vi.mocked(adminRead).mockImplementation(async (url) => {
    if (url.includes("business/overview")) return { metrics: null } as never;
    if (url.includes("catalog")) return { services: [] } as never;
    if (url.includes("statistics"))
      return { totals: {}, periods: [], basis: "" } as never;
    return { items: [], total: 0 } as never;
  });
  render(<AnalyticsPage />);
  await openCostTab();

  fireEvent.change(screen.getByLabelText("客户"), {
    target: { value: "某某公司" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  await waitFor(() => {
    const called = vi
      .mocked(adminRead)
      .mock.calls.some(([url]) => String(url).includes("username="));
    expect(called).toBe(true);
  });
});

describe("current range summary export", () => {
  beforeEach(() => {
    vi.mocked(adminRead).mockReset();
    vi.mocked(exportSummaryReportCsv).mockReset();
    vi.mocked(adminRead).mockImplementation(
      async () =>
        ({
          metrics: null,
          services: [],
          totals: {},
          periods: [],
          items: [],
          total: 0,
        }) as never,
    );
  });
  test("exports the dashboard selected month without independent date fields", async () => {
    vi.mocked(exportSummaryReportCsv).mockResolvedValue({
      filename: "business_2026-09-01_2026-09-30.csv",
      bytes: 2048,
    });
    render(<AnalyticsPage initialStart="2026-09-01" initialEnd="2026-09-30" />);
    expect(screen.queryByLabelText("导出开始日期")).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "导出当前经营报表 CSV" }),
    );
    await waitFor(() =>
      expect(exportSummaryReportCsv).toHaveBeenCalledWith({
        kind: "business",
        start_date: "2026-09-01",
        end_date: "2026-09-30",
      }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "范围 2026-09-01 至 2026-09-30",
    );
  });
  test("surfaces server export errors and hides export for auditor", async () => {
    vi.mocked(exportSummaryReportCsv).mockRejectedValue(
      new Error("导出失败：范围无效"),
    );
    const view = render(<AnalyticsPage />);
    fireEvent.click(
      screen.getByRole("button", { name: "导出当前经营报表 CSV" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("范围无效");
    view.unmount();
    render(<AnalyticsPage readOnly />);
    expect(
      screen.queryByRole("region", { name: "当前范围报表导出" }),
    ).toBeNull();
  });
  test("queue counts and click carry exactly the applied dates and customer through tabs", async () => {
    const urls: string[] = [];
    vi.mocked(adminRead).mockImplementation(async (url) => {
      urls.push(url);
      if (url.includes("catalog")) return { services: [] } as never;
      if (url.includes("statistics"))
        return { totals: {}, periods: [] } as never;
      return { items: [], total: 3 } as never;
    });
    render(
      <AnalyticsPage
        initialTab="cost"
        initialStart="2026-09-01"
        initialEnd="2026-09-30"
      />,
    );
    await screen.findByRole("table", { name: "生成明细" });
    fireEvent.change(screen.getByLabelText("客户"), {
      target: { value: "同名公司" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /待结算/ })).not.toBeDisabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: /待结算/ }));
    await waitFor(() =>
      expect(
        urls.some((url) => {
          const p = new URL(url, "http://local").searchParams;
          return (
            p.get("limit") === "100" &&
            p.get("attention") === "pending" &&
            p.get("start") === "2026-09-01" &&
            p.get("end") === "2026-09-30" &&
            p.get("username") === "同名公司"
          );
        }),
      ).toBe(true),
    );
    fireEvent.click(screen.getByRole("tab", { name: "采集账单" }));
    expect(screen.getByLabelText("客户")).toHaveValue("同名公司");
    fireEvent.click(screen.getByRole("tab", { name: "账务明细" }));
    expect(screen.getByLabelText("开始日期")).toHaveValue("2026-09-01");
  });
});
