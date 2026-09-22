import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { adminRead } from "../api.admin";
import { AnalyticsPage } from "./AnalyticsPage";

vi.mock("../api.admin", () => ({
  adminRead: vi.fn(),
  downloadBillingCsv: vi.fn(),
}));

const settled = {
  user_id: "u-1",
  source_id: "action-1",
  username: "测试用户",
  operation_count: 2,
  pending_count: 0,
  failed_count: 0,
  reserved_credits: 5,
  charged_credits: 5,
  attempt_count: 3,
  unknown_attempt_count: 0,
  inspection_attempt_count: 1,
  inspection_unknown_count: 0,
  known_cost_fen: "12",
  inspection_cost_fen: "7",
  unknown_cost_count: 0,
  services: ["analysis_repair", "first_frame"],
  modules: ["replica", "replacement"],
  first_at: "2026-09-21T02:00:00+00:00",
  last_at: "2026-09-21T02:05:00+00:00",
  cost_fen: "12",
  total_count: 2,
};
const uncertain = {
  ...settled,
  user_id: "u-2",
  source_id: "action-2",
  username: "另一位用户",
  operation_count: 1,
  charged_credits: 0,
  attempt_count: 2,
  unknown_attempt_count: 1,
  inspection_attempt_count: 0,
  inspection_cost_fen: "0",
  unknown_cost_count: 1,
  cost_fen: null,
};

function mockPanorama(items: unknown[], detail: unknown) {
  vi.mocked(adminRead).mockImplementation(async (url) => {
    if (url.includes("catalog")) return { services: [] } as never;
    if (url.includes("statistics"))
      return { totals: {}, periods: [], basis: "" } as never;
    if (url.includes("source-actions/")) return detail as never;
    if (url.includes("source-actions"))
      return { items, total: items.length } as never;
    return { items: [], total: 0 } as never;
  });
}

test("folds one business action into a row and keeps unproven costs visible", async () => {
  mockPanorama([settled, uncertain], {});
  render(<AnalyticsPage />);
  fireEvent.click(
    await screen.findByRole("button", { name: "查看业务动作全景" }),
  );
  const table = await screen.findByRole("table", { name: "业务动作列表" });
  const headers = within(table)
    .getAllByRole("columnheader")
    .map((cell) => cell.textContent);
  const rows = within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell"));
  const settledCells = rows[0];
  expect(settledCells[headers.indexOf("业务动作")]).toHaveTextContent(
    "action-1",
  );
  expect(settledCells[headers.indexOf("用户")]).toHaveTextContent("测试用户");
  expect(settledCells[headers.indexOf("请求与调用")]).toHaveTextContent(
    "2 条请求 · 3 次调用",
  );
  expect(settledCells[headers.indexOf("积分")]).toHaveTextContent("5 积分");
  expect(settledCells[headers.indexOf("供应商成本")]).toHaveTextContent(
    "¥0.12",
  );
  expect(settledCells[headers.indexOf("质检成本")]).toHaveTextContent(
    "质检 ¥0.07 · 1 次",
  );
  // 证据不齐的动作不能把已知成本显示成最终成本。
  const uncertainCells = rows[1];
  expect(uncertainCells[headers.indexOf("供应商成本")]).toHaveTextContent(
    "待核对 · 已知 ¥0.12",
  );
  expect(uncertainCells[headers.indexOf("质检成本")]).toHaveTextContent("无");
  expect(screen.getByText("共 2 个业务动作")).toBeInTheDocument();
});

test("opens one action panorama with its own requests and provider calls", async () => {
  const attempts = [
    {
      id: "at-1",
      operation_id: "op-1",
      service: "first_frame",
      provider: "ark",
      unit: "image",
      usage: "1.000000",
      unit_cost_fen: "2",
      cost_fen: "2",
      effective_cost_fen: "2",
      evidence_reference: null,
      state: "ACTUAL",
    },
    {
      id: "at-2",
      operation_id: "op-1",
      service: "quality_inspection",
      provider: "ark",
      unit: "call",
      usage: "1.000000",
      unit_cost_fen: "7",
      cost_fen: "7",
      effective_cost_fen: "7",
      evidence_reference: "ref-7",
      state: "ACTUAL",
    },
  ];
  mockPanorama([settled], {
    action: settled,
    scopes: [settled],
    operations: [
      {
        id: "op-1",
        user_id: "u-1",
        username: "测试用户",
        service: "first_frame",
        module: "replacement",
        unit: "image",
        state: "SUCCEEDED",
        budget_units: "1.000000",
        actual_units: "1.000000",
        reserved_credits: 5,
        charged_credits: 5,
        cost_fen: "9",
        attempts,
      },
      {
        id: "op-2",
        user_id: "u-1",
        username: "测试用户",
        service: "analysis_repair",
        module: "replica",
        unit: "call",
        state: "SUCCEEDED",
        budget_units: "1.000000",
        actual_units: "1.000000",
        reserved_credits: 0,
        charged_credits: 0,
        cost_fen: "3",
        attempts: [
          {
            ...attempts[0],
            id: "at-3",
            operation_id: "op-2",
            service: "analysis_repair",
            unit_cost_fen: "3",
            cost_fen: "3",
            effective_cost_fen: "3",
          },
        ],
      },
    ],
  });
  render(<AnalyticsPage />);
  fireEvent.click(
    await screen.findByRole("button", { name: "查看业务动作全景" }),
  );
  const table = await screen.findByRole("table", { name: "业务动作列表" });
  fireEvent.click(within(table).getByRole("button", { name: "查看动作全景" }));
  const detail = await screen.findByRole("complementary", {
    name: "业务动作明细",
  });
  expect(detail).toHaveTextContent("2 条请求 · 3 次调用 · 净扣 5 积分");
  const requests = within(detail).getByRole("table", { name: "动作内请求" });
  expect(within(requests).getAllByRole("row").slice(1)).toHaveLength(2);
  const calls = within(detail).getByRole("table", {
    name: "first_frame 供应商调用",
  });
  expect(
    within(calls).getByRole("cell", { name: "¥0.07" }),
  ).toBeInTheDocument();
  expect(within(calls).getByText("凭据已核对")).toBeInTheDocument();
  expect(
    vi
      .mocked(adminRead)
      .mock.calls.some(([url]) =>
        String(url).includes("source-actions/action-1?user_id=u-1"),
      ),
  ).toBe(true);
});

test("scopes platform actions without a customer id", async () => {
  const platform = {
    ...settled,
    user_id: null,
    source_id: "platform-9",
    username: "平台后台",
  };
  mockPanorama([platform], {
    action: platform,
    scopes: [platform],
    operations: [],
  });
  render(<AnalyticsPage />);
  fireEvent.click(
    await screen.findByRole("button", { name: "查看业务动作全景" }),
  );
  const table = await screen.findByRole("table", { name: "业务动作列表" });
  expect(within(table).getByText("平台后台")).toBeInTheDocument();
  fireEvent.click(within(table).getByRole("button", { name: "查看动作全景" }));
  await screen.findByRole("complementary", { name: "业务动作明细" });
  expect(
    vi
      .mocked(adminRead)
      .mock.calls.some(([url]) =>
        String(url).includes("source-actions/platform-9?platform=true"),
      ),
  ).toBe(true);
});
