import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OverviewPage } from "./OverviewPage";

const summaryPayload = {
  today: {
    generation_count: 328,
    succeeded: 302,
    success_rate_pct: 92.1,
    output_seconds: 4860,
    cost_fen: 48620,
    revenue_fen: 161460,
    gross_fen: 112840,
    margin_pct: 69.9,
    online_devices: 47,
    active_customers: 89,
    recharge_fen: 485000,
    recharge_orders: 12,
    // 2026-09-18 匹配梳理 P3：这两个字段后端一直返回，此前总览未展示。
    pending_operations: 4,
    unknown_revenue_operations: 1,
  },
  trend: [
    { day: "2026-08-30", succeeded: 210, failed: 28, cost_fen: 39000 },
    { day: "2026-09-05", succeeded: 302, failed: 12, cost_fen: 48620 },
  ],
  todos: {
    pending_pairings: 3,
    failed_tasks_7d: 5,
    analysis_failures_7d: 3,
    analysis_failure_reasons: [
      {
        error_code: "ANALYSIS_PROVIDER_FAILED",
        failure_phase: "http",
        reason: "model gemini-3.8-flash is not available",
        count: 3,
      },
    ],
    reconciliation_problems: 2,
    expiring_codes_7d: 0,
    unconfigured_rates: 2,
    unknown_cost_records: 0,
  },
  device_slots: { bound: 918, total: 1024 },
};

function installFetch(payload: unknown = summaryPayload) {
  const fetchMock = vi.fn((url: string) => {
    if (url.includes("/api/control/dashboard/summary")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => payload,
      });
    }
    return Promise.resolve({ ok: false, status: 404, json: async () => ({}) });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  vi.stubGlobal("URL", {
    createObjectURL: vi.fn(() => "blob:test"),
    revokeObjectURL: vi.fn(),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("OverviewPage", () => {
  it("renders KPI cards, trend, todos and quick actions from the summary", async () => {
    const onNavigate = vi.fn();
    installFetch();
    render(<OverviewPage onNavigate={onNavigate} />);

    expect(await screen.findByText("328")).toBeInTheDocument();
    expect(screen.getByText("¥4850.00")).toBeInTheDocument();
    expect(screen.getByText("¥486.20")).toBeInTheDocument();
    expect(screen.getByText("¥1128.40")).toBeInTheDocument();
    expect(screen.queryByText("918 台")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "登录设备" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "有效客户" })).toHaveTextContent(
      "89",
    );
    expect(screen.getByText("近 7 日生成与成本")).toBeInTheDocument();
    expect(screen.getByText("成本（元）")).toBeInTheDocument();
    expect(screen.getByText("成功生成数（条）")).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: /图标/ })).toHaveLength(7);
    expect(screen.getByText("资金账务核对")).toBeInTheDocument();
    expect(screen.queryByText("点击下钻资金流水")).toBeNull();

    // 待办计数
    expect(screen.queryByText("待批准配对")).not.toBeInTheDocument();
    expect(screen.getByText("对账不一致")).toBeInTheDocument();
    expect(screen.queryByText("即将过期激活码")).not.toBeInTheDocument();

    // 快捷操作跳转
    expect(
      screen.queryByRole("button", { name: "快速发码" }),
    ).not.toBeInTheDocument();
    // P0-1：收款开通与赠送分开入口，「后台加款」字样不再出现。
    expect(
      screen.queryByRole("button", { name: "后台加款" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "开通套餐（已收款）" }));
    expect(onNavigate).toHaveBeenCalledWith("customerPackage");
    fireEvent.click(screen.getByRole("button", { name: "赠送积分" }));
    expect(onNavigate).toHaveBeenCalledWith("customerAdjustments");
    fireEvent.click(screen.getByRole("button", { name: "退款扣减" }));
    expect(onNavigate).toHaveBeenCalledWith("customerRefund");
    fireEvent.click(screen.getByRole("button", { name: "成本核对" }));
    expect(onNavigate).toHaveBeenCalledWith("costDetails");

    const missingRates = screen.getByText("成本待配置").closest("li");
    fireEvent.click(within(missingRates as HTMLElement).getByRole("button"));
    expect(onNavigate).toHaveBeenCalledWith("rates");
    const failedTasks = screen.getByText("失败任务待处理").closest("li");
    fireEvent.click(within(failedTasks as HTMLElement).getByRole("button"));
    expect(onNavigate).toHaveBeenCalledWith("failedGenerationRecords");

    // 2026-09-18 匹配梳理 P3：这两个字段后端一直在返回，总览此前只字未提。
    // 它们决定"今天的数字能不能信"，所以摆进待办并可一键去处理。
    const pendingOps = screen.getByText("今日操作待结算").closest("li");
    expect(pendingOps).toHaveTextContent("4");
    expect(pendingOps).toHaveTextContent("当日金额尚未定稿");
    // P0-5：这两项是计费操作口径，落到经营分析而不是资金流水。
    fireEvent.click(within(pendingOps as HTMLElement).getByRole("button"));
    expect(onNavigate).toHaveBeenCalledWith("pendingOperations");

    const unknownRevenue = screen.getByText("今日收入未确定").closest("li");
    expect(unknownRevenue).toHaveTextContent("1");
    fireEvent.click(within(unknownRevenue as HTMLElement).getByRole("button"));
    expect(onNavigate).toHaveBeenCalledWith("unknownRevenue");

    // 拆解失败单列一行，并把最集中的上游原因摆在行上。
    expect(screen.getByText("拆解失败待排查")).toBeInTheDocument();
    expect(
      screen.getByText("model gemini-3.8-flash is not available"),
    ).toBeInTheDocument();
    const analysisFailures = screen.getByText("拆解失败待排查").closest("li");
    fireEvent.click(
      within(analysisFailures as HTMLElement).getByRole("button"),
    );
    expect(onNavigate).toHaveBeenCalledWith("analysisFailures");
  });

  it("lets auditors open todo lists but hides quick write actions", async () => {
    const onNavigate = vi.fn();
    installFetch();
    render(<OverviewPage onNavigate={onNavigate} readOnly />);

    const failedTasks = (await screen.findByText("失败任务待处理")).closest(
      "li",
    );
    fireEvent.click(
      within(failedTasks as HTMLElement).getByRole("button", {
        name: "去查看",
      }),
    );
    expect(onNavigate).toHaveBeenCalledWith("failedGenerationRecords");
    expect(screen.queryByRole("button", { name: "去处理" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "开通套餐（已收款）" }),
    ).toBeNull();
  });

  it("shows an error banner when the summary request fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("network down"))),
    );
    render(<OverviewPage />);

    expect(await screen.findByText(/network down/)).toBeInTheDocument();
  });

  it("keeps unsettled and legacy costs unknown in cards and chart", async () => {
    const onNavigate = vi.fn();
    installFetch({
      ...summaryPayload,
      today: {
        ...summaryPayload.today,
        cost_fen: null,
        gross_fen: null,
        margin_pct: null,
        pending_operations: 1,
        legacy_cost_records: 2,
        legacy_settlements: 1,
      },
      trend: [{ day: "2026-09-05", succeeded: 1, failed: 0, cost_fen: null }],
      todos: {
        ...summaryPayload.todos,
        unknown_cost_records: 3,
      },
    });
    render(<OverviewPage onNavigate={onNavigate} />);
    const cost = await screen.findByRole("region", { name: "今日成本" });
    expect(cost).toHaveTextContent("待核对");
    expect(cost).not.toHaveTextContent("¥0.00");
    expect(screen.getByRole("region", { name: "今日毛利" })).toHaveTextContent(
      "待核对",
    );
    expect(screen.getByTitle("成本待核对")).toBeInTheDocument();
    expect(screen.queryByTitle("成本 ¥0.00")).not.toBeInTheDocument();
    expect(
      screen.getByText(/2 条历史成本、1 条历史结算待核对/),
    ).toBeInTheDocument();
    // P0-5 收口：KpiCard 的「待核对」不是一个终点，待办行要能一键落到
    // 条数同口径的 attention=unknown_cost 清单（AdminApp 三层映射已就绪）。
    const unknownCost = screen.getByText("今日成本待核对").closest("li");
    expect(unknownCost).toHaveTextContent("3");
    expect(unknownCost).toHaveTextContent("成本金额尚未确定");
    fireEvent.click(within(unknownCost as HTMLElement).getByRole("button"));
    expect(onNavigate).toHaveBeenCalledWith("unknownCost");
  });
});
