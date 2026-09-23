import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as adminApi from "../api.admin";
import { AnalysisDiagnosticPanel } from "./AnalysisDiagnosticPanel";

vi.mock("../api.admin", () => ({
  getAdminAnalysisDiagnostics: vi.fn(),
}));

const PROVIDER_ADVICE = "稍后重试一次；持续失败核对接入商服务状态。";
const WORKER_ADVICE = "直接重试；频繁出现请检查 worker 容器资源与重启记录。";

const DIAGNOSTIC: adminApi.AdminAnalysisDiagnosticRecord = {
  task_id: "analysis-task-1",
  request_id: "req-enqueue-1",
  username: "customer-1",
  display_name: "客户一",
  project_id: "project-1",
  project_name: "演示项目",
  status: "FAILED",
  attempt: 3,
  error_code: "ANALYSIS_PROVIDER_FAILED",
  error_message: "视频拆解服务拒绝了请求（HTTP 400）：model not available",
  failure_phase: "http",
  retryable: true,
  upstream_status: 400,
  upstream_reason: "model not available",
  advice: PROVIDER_ADVICE,
  created_at: "2026-09-21T10:00:00Z",
  completed_at: "2026-09-21T10:05:00Z",
  attempts: [
    {
      attempt: 1,
      status: "INTERRUPTED",
      error_code: "ANALYSIS_WORKER_INTERRUPTED",
      error_message: "拆解任务执行中断，请重新拆解。",
      failure_phase: null,
      retryable: true,
      upstream_status: null,
      upstream_reason: null,
      advice: WORKER_ADVICE,
      request_id: "req-enqueue-1",
      created_at: "2026-09-21T10:01:00Z",
      completed_at: "2026-09-21T10:01:00Z",
    },
    {
      attempt: 2,
      status: "FAILED",
      error_code: "ANALYSIS_PROVIDER_UNREACHABLE",
      error_message: "无法连接视频拆解服务，请检查网络后重试。",
      failure_phase: "network",
      retryable: true,
      upstream_status: null,
      upstream_reason: "URLError",
      advice: null,
      request_id: "req-attempt-2",
      created_at: "2026-09-21T10:03:00Z",
      completed_at: "2026-09-21T10:03:00Z",
    },
    {
      attempt: 3,
      status: "FAILED",
      error_code: "ANALYSIS_PROVIDER_FAILED",
      error_message: "视频拆解服务拒绝了请求（HTTP 400）：model not available",
      failure_phase: "http",
      retryable: true,
      upstream_status: 400,
      upstream_reason: "model not available",
      advice: PROVIDER_ADVICE,
      request_id: "req-enqueue-1",
      created_at: "2026-09-21T10:05:00Z",
      completed_at: "2026-09-21T10:05:00Z",
    },
  ],
};

describe("AnalysisDiagnosticPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(adminApi.getAdminAnalysisDiagnostics).mockResolvedValue({
      items: [DIAGNOSTIC],
      total: 1,
    });
  });

  it("answers a task number with the failure history and upstream diagnostic", async () => {
    render(<AnalysisDiagnosticPanel />);

    fireEvent.change(screen.getByLabelText("诊断任务编号"), {
      target: { value: "analysis-task-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));

    expect(await screen.findByText("演示项目")).toBeInTheDocument();
    expect(adminApi.getAdminAnalysisDiagnostics).toHaveBeenCalledWith({
      taskId: "analysis-task-1",
      requestId: undefined,
    });
    // 任务卡回答「谁、哪次请求、最后怎么样」。
    // 同一问题编号在任务卡与第 1、3 次尝试各出现一次：同一次入队的重试共用编号。
    expect(screen.getAllByText("req-enqueue-1")).toHaveLength(3);
    // 上游说明各有出处：任务卡是「最后一次」，第 3 次尝试是「当次」。
    expect(screen.getAllByText("model not available")).toHaveLength(2);
    // 重试历史按次序摆开：中断 → 网络失败 → 上游拒绝，每次结论都有出处。
    expect(screen.getByText("第 1 次尝试")).toBeInTheDocument();
    expect(screen.getByText("第 2 次尝试")).toBeInTheDocument();
    expect(screen.getByText("第 3 次尝试")).toBeInTheDocument();
    expect(screen.getByText("执行中断")).toBeInTheDocument();
    expect(screen.getByText("URLError")).toBeInTheDocument();
    // P2-2：错误码旁边直接给修复建议（任务卡 + 当次尝试各一份）。
    expect(screen.getAllByText(PROVIDER_ADVICE)).toHaveLength(2);
    expect(screen.getByText(WORKER_ADVICE)).toBeInTheDocument();
  });

  it("omits the fix hint when the error code has no runbook entry", async () => {
    vi.mocked(adminApi.getAdminAnalysisDiagnostics).mockResolvedValue({
      items: [
        {
          ...DIAGNOSTIC,
          error_code: "LEGACY_ANALYSIS_CODE",
          advice: null,
          attempts: [],
        },
      ],
      total: 1,
    });
    render(<AnalysisDiagnosticPanel initialTaskId="analysis-task-1" />);

    expect(await screen.findByText("LEGACY_ANALYSIS_CODE")).toBeInTheDocument();
    expect(screen.queryByText("修复建议")).toBeNull();
  });

  it("refuses to search without any key", async () => {
    render(<AnalysisDiagnosticPanel />);

    expect(screen.getByRole("button", { name: "查询" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await Promise.resolve();
    expect(adminApi.getAdminAnalysisDiagnostics).not.toHaveBeenCalled();
  });

  it("reports a miss instead of an empty history", async () => {
    vi.mocked(adminApi.getAdminAnalysisDiagnostics).mockResolvedValue({
      items: [],
      total: 0,
    });
    render(<AnalysisDiagnosticPanel />);

    fireEvent.change(screen.getByLabelText("诊断问题编号"), {
      target: { value: "req-404" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));

    expect(
      await screen.findByText("未找到匹配的任务记录，请核对编号。"),
    ).toBeInTheDocument();
    expect(adminApi.getAdminAnalysisDiagnostics).toHaveBeenCalledWith({
      taskId: undefined,
      requestId: "req-404",
    });
  });

  it("inspects the handed-over task immediately", async () => {
    render(<AnalysisDiagnosticPanel initialTaskId="analysis-task-1" />);

    await waitFor(() =>
      expect(adminApi.getAdminAnalysisDiagnostics).toHaveBeenCalledWith({
        taskId: "analysis-task-1",
        requestId: undefined,
      }),
    );
    expect(await screen.findByText("演示项目")).toBeInTheDocument();
  });

  it("surfaces backend failures without inventing a result", async () => {
    vi.mocked(adminApi.getAdminAnalysisDiagnostics).mockRejectedValue(
      new Error("boom"),
    );
    render(<AnalysisDiagnosticPanel />);

    fireEvent.change(screen.getByLabelText("诊断任务编号"), {
      target: { value: "analysis-task-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));

    expect(await screen.findByText("诊断查询失败：boom")).toBeInTheDocument();
    expect(screen.queryByText("演示项目")).toBeNull();
  });
});
