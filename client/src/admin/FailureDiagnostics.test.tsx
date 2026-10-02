import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { adminRead, getAdminGenerationRecords } from "../api.admin";
import { FailureDiagnostics } from "./FailureDiagnostics";

vi.mock("../api.admin", () => ({
  getAdminGenerationRecords: vi.fn(),
  adminRead: vi.fn(),
  getGenerationRecordHistory: vi.fn().mockResolvedValue({
    items: [],
    total: 0,
    measurementStartedAt: "2026-10-02T00:00:00Z",
    historyRule: "迁移前历史未知",
  }),
}));
vi.mock("./AnalysisDiagnosticPanel", () => ({
  AnalysisDiagnosticPanel: () => null,
}));
vi.mock("./RecordCallsPanel", () => ({ RecordCallsPanel: () => null }));
afterEach(() => vi.clearAllMocks());

it("待归类供应商错误码按需读取，不混入客户提示词", async () => {
  vi.mocked(getAdminGenerationRecords).mockResolvedValue({
    items: [],
    total: 0,
    limit: 20,
    offset: 0,
  });
  vi.mocked(adminRead).mockResolvedValue([
    {
      provider: "synthetic-provider",
      error_code: "SYNTHETIC_UNKNOWN",
      count: 3,
      last_seen_at: "2026-10-02T00:00:00Z",
    },
  ]);
  render(<FailureDiagnostics />);
  expect(adminRead).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "读取待归类错误码" }));
  await screen.findByText(/SYNTHETIC_UNKNOWN/);
  expect(adminRead).toHaveBeenCalledWith(
    "/api/control/provider-errors/pending",
    "读取待归类错误码失败",
  );
});

it("按原六类和待归类请求服务端完整队列，并保留非拆解诊断", async () => {
  vi.mocked(getAdminGenerationRecords).mockResolvedValue({
    items: [
      {
        record_type: "VIDEO",
        record_id: "stable-video-task",
        user_id: "stable-customer",
        username: "客户甲",
        status: "FAILED",
        error_code: "UNKNOWN",
        failure_category: null,
        project_name: "视频项目",
        created_at: "2026-10-01T00:00:00Z",
      },
    ],
    total: 25,
    limit: 20,
    offset: 0,
  } as never);
  render(<FailureDiagnostics />);
  await screen.findByText("视频生成 · 视频项目");
  fireEvent.change(screen.getByLabelText("诊断原因分类"), {
    target: { value: "UNCLASSIFIED" },
  });
  await waitFor(() =>
    expect(getAdminGenerationRecords).toHaveBeenLastCalledWith(
      expect.objectContaining({
        diagnostics: true,
        failureCategory: "UNCLASSIFIED",
        offset: 0,
      }),
    ),
  );
  expect(
    screen.getByLabelText("诊断原因分类").querySelectorAll("option"),
  ).toHaveLength(8);
  fireEvent.click(screen.getByRole("button", { name: "查看诊断详情" }));
  expect(screen.getByLabelText("任务诊断详情")).toHaveTextContent(
    "stable-video-task",
  );
  expect(screen.getByRole("link", { name: "查看客户" })).toHaveAttribute(
    "href",
    "#admin/customersMgmt?userId=stable-customer",
  );
});
