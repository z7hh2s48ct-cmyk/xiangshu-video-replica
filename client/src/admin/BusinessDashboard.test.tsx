import { render, screen, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { adminRead } from "../api.admin";
import { BusinessDashboard } from "./BusinessDashboard";

vi.mock("../api.admin", () => ({
  adminRead: vi.fn(),
}));

const overview = {
  start: "2026-09-01",
  end: "2026-09-30",
  prev_start: "2026-08-01",
  prev_end: "2026-08-31",
  metrics: {
    recharge_fen: 15000,
    paying_customers: 4,
    new_paying_customers: 1,
    revenue_fen: 80000,
    cost_fen: 20000,
    unknown_cost_count: 2,
    pending_count: 1,
    prepaid_credits: 355,
    prepaid_fen: 355,
  },
  prev: {
    recharge_fen: 30000,
    paying_customers: 2,
    new_paying_customers: 2,
    revenue_fen: 60000,
    cost_fen: 30000,
    unknown_cost_count: 0,
    pending_count: 0,
    prepaid_credits: 300,
    prepaid_fen: 300,
  },
  daily: [
    { day: "2026-09-01", revenue_fen: 5000, cost_fen: 1200 },
    { day: "2026-09-02", revenue_fen: 3000, cost_fen: null },
  ],
  modules: [
    {
      service: "video_768p",
      label: "视频生成 · 768P",
      revenue_fen: 60000,
      cost_fen: 15000,
      unknown_cost_count: 1,
      pending_count: 0,
    },
    {
      service: "oral",
      label: "数字人口播",
      revenue_fen: 20000,
      cost_fen: 5000,
      unknown_cost_count: 1,
      pending_count: 1,
    },
  ],
  top_customers: [
    {
      user_id: "cust_1",
      username: "customer-1",
      display_name: "客户一公司",
      revenue_fen: 50000,
    },
  ],
};

function mockOverview(result: unknown = overview) {
  vi.mocked(adminRead).mockImplementation(async (url) => {
    if (String(url).includes("business/overview")) return result as never;
    throw new Error(`unexpected adminRead call: ${String(url)}`);
  });
}

describe("BusinessDashboard", () => {
  test("renders the 8 metric cards with known-amount wording and period-over-period deltas", async () => {
    mockOverview();
    render(<BusinessDashboard />);

    const panel = await screen.findByRole("region", { name: "经营看板" });
    const cards = within(panel).getByRole("region", { name: "经营指标卡" });
    expect(within(cards).getAllByText("¥150.00").length).toBe(1);
    // 确认收入环比 +33%
    expect(within(cards).getAllByText("环比 +33%").length).toBeGreaterThan(0);
    // 成本卡与毛利卡都标注待核对；毛利口径 = 确认收入 − 成本。
    expect(within(cards).getAllByText(/含 3 项待核对/).length).toBe(1);
    expect(within(cards).getAllByText("含 2 项待核对").length).toBe(1);
    expect(within(cards).getByText("¥600.00")).toBeInTheDocument(); // 毛利
    expect(within(cards).getByText("355 积分")).toBeInTheDocument();
    // 预收折合金额依赖积分兑换比例
    expect(within(cards).getByText(/折合 ¥3.55/)).toBeInTheDocument();
  });

  test("renders daily trend and module breakdown without treating missing evidence as zero", async () => {
    mockOverview();
    render(<BusinessDashboard />);

    const trend = await screen.findByRole("img", {
      name: /收入与成本按日趋势图/,
    });
    expect(trend).toBeInTheDocument();
    const modules = screen.getByRole("table", { name: "业务模块收入成本构成" });
    expect(
      within(modules).getByRole("cell", { name: "视频生成 · 768P" }),
    ).toBeInTheDocument();
    expect(within(modules).getAllByText(/含 1 项待核对/).length).toBe(2);
    const top = screen.getByRole("table", { name: "Top 10 客户" });
    expect(within(top).getByText("客户一公司")).toBeInTheDocument();
  });

  test("keeps the empty state honest when the range has no data", async () => {
    mockOverview({ metrics: null });
    render(<BusinessDashboard />);
    expect(
      await screen.findByText("这个区间没有经营数据。"),
    ).toBeInTheDocument();
  });
});
