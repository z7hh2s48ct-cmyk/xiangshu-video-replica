import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { getFundsSummary } from "../api";
import { exportSummaryReportCsv } from "../api.admin";
import { FundsOverview } from "./FundsOverview";

vi.mock("../api", () => ({ getFundsSummary: vi.fn() }));
vi.mock("../api.admin", () => ({ exportSummaryReportCsv: vi.fn() }));
const summary = {
  start: "2026-09-01",
  end: "2026-09-30",
  recharge_fen: 35000,
  orders: 4,
  offline_fen: 5000,
  grant_credits: 30,
  refund_credits: 20,
  refund_fen: null,
  net_fen: null,
  prepaid_credits: 350,
  prepaid_fen: null,
  reconciliation_problems: 3,
  by_channel: [{ provider: "zpay", orders: 3, amount_fen: 30000 }],
  by_method: [
    { method: "alipay", orders: 1, amount_fen: 10000 },
    { method: "wxpay", orders: 1, amount_fen: 20000 },
    { method: "unknown", orders: 1, amount_fen: 0 },
    { method: "offline", orders: 1, amount_fen: 5000 },
  ],
};
beforeEach(() => {
  vi.mocked(getFundsSummary).mockReset();
  vi.mocked(exportSummaryReportCsv).mockReset();
  vi.mocked(getFundsSummary).mockResolvedValue(summary);
});
test("selected month read and export use exact same dates, actual methods and unknowns", async () => {
  vi.mocked(exportSummaryReportCsv).mockResolvedValue({
    filename: "funds.csv",
    bytes: 100,
  });
  render(
    <FundsOverview onOpenReconciliation={() => {}} selectedMonth="2026-09" />,
  );
  const table = await screen.findByRole("table", { name: "支付方式分布" });
  expect(
    within(table).getByRole("cell", { name: "支付宝" }),
  ).toBeInTheDocument();
  expect(within(table).getByRole("cell", { name: "微信" })).toBeInTheDocument();
  expect(
    within(table).getByRole("cell", { name: "支付方式未知" }),
  ).toBeInTheDocument();
  expect(table).not.toHaveTextContent("zpay");
  expect(getFundsSummary).toHaveBeenCalledWith("2026-09-01", "2026-09-30");
  fireEvent.click(screen.getByRole("button", { name: "导出月度资金报表 CSV" }));
  await waitFor(() =>
    expect(exportSummaryReportCsv).toHaveBeenCalledWith({
      kind: "funds",
      start_date: "2026-09-01",
      end_date: "2026-09-30",
    }),
  );
  expect(screen.getAllByText(/未配置积分兑换比例/).length).toBeGreaterThan(0);
});
test("auditor can choose month and open anomalies but cannot export", async () => {
  const onOpen = vi.fn();
  render(<FundsOverview readOnly onOpenReconciliation={onOpen} />);
  await screen.findByRole("table", { name: "支付方式分布" });
  fireEvent.change(screen.getByLabelText("资金报表月份"), {
    target: { value: "2026-08" },
  });
  await waitFor(() =>
    expect(getFundsSummary).toHaveBeenCalledWith("2026-08-01", "2026-08-31"),
  );
  fireEvent.click(screen.getByRole("button", { name: /对账异常/ }));
  expect(onOpen).toHaveBeenCalledOnce();
  expect(
    screen.queryByRole("button", { name: "导出月度资金报表 CSV" }),
  ).toBeNull();
});
