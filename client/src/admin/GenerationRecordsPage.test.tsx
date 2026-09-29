import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as adminApi from "../api.admin";
import { GenerationRecordsPage } from "./GenerationRecordsPage";

vi.mock("../api.admin", () => ({
  getAdminGenerationRecords: vi.fn(),
  getAdminGenerationRecordSummary: vi.fn(),
  getAdminGenerationRecordCalls: vi.fn(),
  getExternalCallResponse: vi.fn(),
  getAdminAnalysisDiagnostics: vi.fn(),
  reconcileFirstFrameTask: vi.fn(),
}));

/** 首帧记录行：默认"提交结果待核对"，正是可对账的那一档。 */
function firstFrameRecord(
  overrides: Partial<adminApi.AdminGenerationRecord> = {},
): adminApi.AdminGenerationRecord {
  return {
    record_id: "ff-1",
    record_type: "FIRST_FRAME_IMAGE",
    operation: "GENERATE",
    user_id: "user-1",
    username: "customer-1",
    display_name: "客户一",
    project_id: "project-1",
    project_name: "演示项目",
    status: "SUBMISSION_UNCERTAIN",
    provider: "apilio",
    model: "gpt-image-2",
    provider_cost: null,
    provider_cost_status: "UNAVAILABLE",
    record_data_status: "VALID",
    charged_credits: 0,
    result_reference: null,
    provider_reference: null,
    error_code: null,
    error_message: null,
    created_at: "2026-09-02T10:00:00Z",
    completed_at: null,
    ...overrides,
  } as adminApi.AdminGenerationRecord;
}

describe("GenerationRecordsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(adminApi.getAdminAnalysisDiagnostics).mockResolvedValue({
      items: [],
      total: 0,
    });
    vi.mocked(adminApi.getAdminGenerationRecordSummary).mockResolvedValue({
      total: 3,
      counts: [
        { record_type: "VIDEO", status: "RUNNING", count: 1 },
        { record_type: "FIRST_FRAME_IMAGE", status: "SUCCEEDED", count: 1 },
        { record_type: "SOURCE_FRAME_AI_SCORE", status: "SUCCEEDED", count: 1 },
      ],
      failure_reasons: [],
    });
    // 调用日志区块懒加载：默认给空列表，展开详情不会打到未 mock 的路径。
    vi.mocked(adminApi.getAdminGenerationRecordCalls).mockResolvedValue({
      items: [],
      total: 0,
    });
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      items: [
        {
          record_id: "video-1",
          record_type: "VIDEO",
          operation: "I2V",
          user_id: "user-1",
          username: "customer-1",
          display_name: "客户一",
          project_id: "project-1",
          project_name: "演示项目",
          status: "RUNNING",
          provider: "minimax",
          model: "Hailuo-02",
          provider_cost: 1.25,
          provider_cost_status: "ESTIMATED",
          record_data_status: "VALID",
          charged_credits: 0,
          result_reference: null,
          provider_reference: null,
          error_code: null,
          error_message: null,
          created_at: "2026-09-02T11:00:00Z",
          completed_at: null,
        },
        {
          record_id: "first-frame-1",
          record_type: "FIRST_FRAME_IMAGE",
          operation: "GENERATE",
          user_id: "user-1",
          username: "customer-1",
          display_name: "客户一",
          project_id: "project-1",
          project_name: "演示项目",
          status: "SUCCEEDED",
          provider: "apilio",
          model: "gpt-image-2",
          provider_cost: null,
          provider_cost_status: "UNAVAILABLE",
          record_data_status: "VALID",
          charged_credits: 0,
          result_reference: "version-1",
          provider_reference: null,
          error_code: null,
          error_message: null,
          created_at: "2026-09-02T10:00:00Z",
          completed_at: "2026-09-02T10:01:00Z",
        },
        {
          record_id: "source-score-1",
          record_type: "SOURCE_FRAME_AI_SCORE",
          operation: "SCORE_CANDIDATES",
          user_id: "user-1",
          username: "customer-1",
          display_name: "客户一",
          project_id: "project-1",
          project_name: "演示项目",
          status: "SUCCEEDED",
          provider: "apilio_gemini",
          model: "gemini-2.5-flash",
          provider_cost: null,
          provider_cost_status: "UNAVAILABLE",
          record_data_status: "VALID",
          charged_credits: 0,
          result_reference: "version-2",
          provider_reference: null,
          error_code: null,
          error_message: null,
          created_at: "2026-09-02T09:00:00Z",
          completed_at: "2026-09-02T09:01:00Z",
        },
      ],
      total: 3,
      limit: 50,
      offset: 0,
    });
  });

  it("shows image generation and AI scoring with honest cost status", async () => {
    render(<GenerationRecordsPage />);

    expect(await screen.findByText("人物置换首帧")).toBeInTheDocument();
    expect(screen.getByText("源画面 AI 评分")).toBeInTheDocument();
    expect(screen.getAllByText("上游未回传")).toHaveLength(2);
    expect(screen.getByText("估算 1.25")).toBeInTheDocument();
    expect(screen.getByText("apilio / gpt-image-2")).toBeInTheDocument();
    expect(
      screen.getByText("apilio_gemini / gemini-2.5-flash"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("customer-1")).toHaveLength(3);
  });

  it("shows the calls panel total with a truncation note (P0-9 #27)", async () => {
    // 调用日志固定最多 200 条（服务端 _CALL_LIST_LIMIT），面板必须把真实
    // 总条数与截断说明摆出来，而不是静默只给前 200 条。
    vi.mocked(adminApi.getAdminGenerationRecordCalls).mockResolvedValue({
      items: [
        {
          call_id: "call-1",
          created_at: "2026-09-02T11:00:00Z",
          provider: "minimax",
          model: "Hailuo-02",
          endpoint: "v1/video_generation",
          method: "POST",
          url: "https://api.example.com/v1/video_generation",
          attempt: 1,
          http_status: 200,
          latency_ms: 320,
          outcome: "SUCCEEDED",
          provider_task_id: "provider-task-1",
          provider_request_id: null,
          provider_error_code: null,
          provider_message: null,
          error_message: null,
          request_summary: null,
          response_body_bytes: 128,
          has_response_body: true,
        },
      ],
      total: 201,
    });
    render(<GenerationRecordsPage />);

    fireEvent.click((await screen.findAllByText("查看详情"))[0]);
    expect(
      await screen.findByText("共 201 次调用，仅列出最早的 1 次。"),
    ).toBeInTheDocument();
    expect(screen.getByText("v1/video_generation")).toBeInTheDocument();
    expect(adminApi.getAdminGenerationRecordCalls).toHaveBeenCalledWith(
      "VIDEO",
      "video-1",
    );
  });

  it("reloads the current page on demand", async () => {
    render(<GenerationRecordsPage />);
    await screen.findByText("人物置换首帧");

    fireEvent.click(screen.getByRole("button", { name: "刷新记录" }));

    await waitFor(() => {
      expect(adminApi.getAdminGenerationRecords).toHaveBeenCalledTimes(2);
    });
  });

  it("混合带时区和旧 UTC 时间的首帧耗时不被显示为零", async () => {
    const page = await adminApi.getAdminGenerationRecords({});
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      ...page,
      items: [
        {
          ...page.items[1],
          created_at: "2026-09-14T15:00:03.355544+00:00",
          completed_at: "2026-09-14 15:01:05",
        },
      ],
      total: 1,
    });
    render(<GenerationRecordsPage />);
    expect(await screen.findByText("1 分 2 秒")).toBeInTheDocument();
  });

  it("opens failed records with the filter already applied", async () => {
    render(<GenerationRecordsPage initialStatus="FAILED" />);

    await waitFor(() => {
      expect(adminApi.getAdminGenerationRecords).toHaveBeenCalledWith(
        expect.objectContaining({ status: "FAILED" }),
      );
    });
    expect(screen.getByLabelText("生成状态")).toHaveValue("FAILED");
    // 失败阶段只有拆解任务有，其他类型下不该出现这个筛选项。
    expect(screen.queryByLabelText("失败阶段")).toBeNull();
  });

  it("offers a single 已取消 option that covers both spellings (P0-6)", async () => {
    render(<GenerationRecordsPage />);
    await waitFor(() =>
      expect(adminApi.getAdminGenerationRecords).toHaveBeenCalled(),
    );
    const cancelled = screen.getAllByRole("option", { name: "已取消" });
    expect(cancelled).toHaveLength(1);
    expect(cancelled[0]).toHaveValue("CANCELED,CANCELLED");
  });

  it("filters analysis failures by phase and surfaces upstream detail", async () => {
    const upstreamReason = "model gemini-3.8-flash is not available";
    const fixAdvice = "稍后重试一次；持续失败核对接入商服务状态。";
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      items: [
        {
          record_id: "analysis-1",
          record_type: "ANALYSIS",
          operation: "video_analysis",
          user_id: "user-1",
          username: "customer-1",
          display_name: "客户一",
          project_id: "project-1",
          project_name: "演示项目",
          status: "FAILED",
          provider: "apilio",
          model: "gemini-3.8-flash",
          provider_cost: null,
          provider_cost_status: "UNAVAILABLE",
          record_data_status: "VALID",
          charged_credits: 4,
          result_reference: null,
          provider_reference: null,
          error_code: "ANALYSIS_PROVIDER_FAILED",
          error_message: "视频拆解服务拒绝了请求（HTTP 400）",
          created_at: "2026-09-21T10:00:00Z",
          completed_at: "2026-09-21T10:00:05Z",
          failure_phase: "http",
          retryable: false,
          upstream_status: 400,
          upstream_reason: upstreamReason,
        },
      ],
      total: 1,
      limit: 50,
      offset: 0,
    });
    vi.mocked(adminApi.getAdminGenerationRecordSummary).mockResolvedValue({
      total: 1,
      counts: [{ record_type: "ANALYSIS", status: "FAILED", count: 1 }],
      failure_reasons: [
        {
          error_code: "ANALYSIS_PROVIDER_FAILED",
          failure_phase: "http",
          reason: upstreamReason,
          retryable: false,
          count: 1,
          advice: fixAdvice,
        },
        // 旧数据没有错误码，runbook 也给不出建议：这一行不该凭空长出一句建议。
        {
          error_code: null,
          failure_phase: null,
          reason: null,
          retryable: true,
          count: 2,
          advice: null,
        },
      ],
    });

    render(
      <GenerationRecordsPage
        initialStatus="FAILED"
        initialRecordType="ANALYSIS"
      />,
    );

    expect(screen.getByLabelText("生成类型")).toHaveValue("ANALYSIS");
    fireEvent.change(screen.getByLabelText("失败阶段"), {
      target: { value: "http" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));

    await waitFor(() => {
      expect(adminApi.getAdminGenerationRecords).toHaveBeenLastCalledWith(
        expect.objectContaining({
          status: "FAILED",
          recordType: "ANALYSIS",
          failurePhase: "http",
        }),
      );
    });
    expect(adminApi.getAdminGenerationRecordSummary).toHaveBeenLastCalledWith(
      expect.objectContaining({
        recordType: "ANALYSIS",
        failurePhase: "http",
      }),
    );
    // 聚合块把「哪一步失败、能不能重试、上游怎么说」摆在列表之前。
    expect(screen.getByText("视频拆解 失败 1")).toBeInTheDocument();
    expect(
      screen.getByText(
        "上游拒绝（HTTP） · ANALYSIS_PROVIDER_FAILED · 不可重试 · 1 条",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(`上游说明：${upstreamReason}`)).toBeInTheDocument();
    // P2-2：聚合行直接把错误码译成下一步动作；没有映射的旧行不多说一句。
    expect(screen.getByText(`修复建议：${fixAdvice}`)).toBeInTheDocument();
    expect(screen.getAllByText(/修复建议：/)).toHaveLength(1);

    fireEvent.click(screen.getByText("查看详情"));
    expect(screen.getByText("400")).toBeInTheDocument();
    expect(screen.getByText("不可重试")).toBeInTheDocument();
    expect(screen.getByText(upstreamReason)).toBeInTheDocument();
  });

  it("shows the failure category and who is expected to handle it", async () => {
    // P2-1：分类与处理人回答「这条失败该归谁办」，运营先筛一遍再分工。
    const advice = "稍后重试一次；持续失败核对接入商服务状态。";
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      items: [
        firstFrameRecord({
          status: "FAILED",
          error_code: "ANALYSIS_PROVIDER_FAILED",
          advice,
          failure_category: "PROVIDER_FAULT",
          failure_owner: "OPS",
        }),
      ],
      total: 1,
    } as unknown as Awaited<
      ReturnType<typeof adminApi.getAdminGenerationRecords>
    >);
    vi.mocked(adminApi.getAdminGenerationRecordSummary).mockResolvedValue({
      total: 1,
      counts: [
        { record_type: "FIRST_FRAME_IMAGE", status: "FAILED", count: 1 },
      ],
      failure_reasons: [
        {
          error_code: "ANALYSIS_PROVIDER_FAILED",
          failure_phase: "http",
          reason: null,
          retryable: true,
          count: 1,
          advice,
          failure_category: "PROVIDER_FAULT",
          failure_owner: "OPS",
        },
      ],
    });

    render(<GenerationRecordsPage initialStatus="FAILED" />);

    // 聚合行把「谁办 + 归哪类」放在最前，再才是环节与错误码这类排查细节。
    expect(
      await screen.findByText(/运营重试 · 服务商故障 · 上游拒绝（HTTP）/),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByText("查看详情"));
    expect(screen.getByText("原因分类")).toBeInTheDocument();
    expect(screen.getByText("服务商故障")).toBeInTheDocument();
    expect(screen.getByText("处理人")).toBeInTheDocument();
    expect(screen.getByText("运营重试")).toBeInTheDocument();
  });

  it("shows read-only oral failure details without a retry action", async () => {
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      items: [
        {
          record_id: "oral-1",
          record_type: "ORAL_VIDEO",
          operation: "TTS",
          user_id: "user-1",
          username: "customer-1",
          display_name: "客户一",
          project_id: null,
          project_name: null,
          status: "FAILED",
          provider: "hifly",
          model: null,
          provider_cost: null,
          provider_cost_status: "UNAVAILABLE",
          record_data_status: "VALID",
          charged_credits: 12,
          result_reference: null,
          provider_reference: "hifly-task-1",
          error_code: "ORAL_TASK_FAILED",
          error_message: "数字人服务生成失败",
          created_at: "2026-09-02T11:00:00Z",
          completed_at: "2026-09-02T11:01:00Z",
        },
      ],
      total: 1,
      limit: 50,
      offset: 0,
    });

    render(<GenerationRecordsPage initialStatus="FAILED" />);

    expect(await screen.findByText("口播视频")).toBeInTheDocument();
    expect(screen.getByText("12 积分")).toBeInTheDocument();
    expect(screen.getByText("1 分 0 秒")).toBeInTheDocument();
    fireEvent.click(screen.getByText("查看详情"));
    expect(screen.getByText("数字人服务生成失败")).toBeInTheDocument();
    expect(screen.getByText("hifly-task-1")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /重试/ })).toBeNull();
  });

  it("submits draft filters once instead of loading while typing", async () => {
    render(<GenerationRecordsPage />);
    await screen.findByText("人物置换首帧");

    fireEvent.change(screen.getByLabelText("生成账号"), {
      target: { value: "customer-2" },
    });
    fireEvent.change(screen.getByLabelText("生成状态"), {
      target: { value: "SUCCEEDED" },
    });
    await Promise.resolve();
    expect(adminApi.getAdminGenerationRecords).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "查询" }));

    await waitFor(() => {
      expect(adminApi.getAdminGenerationRecords).toHaveBeenCalledTimes(2);
    });
    expect(adminApi.getAdminGenerationRecords).toHaveBeenLastCalledWith(
      expect.objectContaining({
        offset: 0,
        status: "SUCCEEDED",
        username: "customer-2",
      }),
    );
  });

  it("ignores an older response after a newer refresh finishes", async () => {
    let resolveFirst:
      | ((value: adminApi.AdminGenerationRecordPage) => void)
      | undefined;
    vi.mocked(adminApi.getAdminGenerationRecords)
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveFirst = resolve;
        }),
      )
      .mockResolvedValueOnce({
        items: [],
        total: 0,
        limit: 50,
        offset: 0,
      });

    render(<GenerationRecordsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "刷新中…" }));
    await waitFor(() =>
      expect(screen.getByText("暂无生成记录。")).toBeInTheDocument(),
    );

    resolveFirst?.({
      items: [
        {
          record_id: "stale-record",
          record_type: "VIDEO",
          operation: "I2V",
          user_id: "user-1",
          username: "stale-user",
          display_name: "旧数据",
          project_id: null,
          project_name: null,
          status: "SUCCEEDED",
          provider: "stale-provider",
          model: "stale-model",
          provider_cost: 1,
          provider_cost_status: "KNOWN",
          record_data_status: "VALID",
          charged_credits: 1,
          result_reference: null,
          provider_reference: null,
          error_code: null,
          error_message: null,
          created_at: "2026-09-01T00:00:00Z",
          completed_at: null,
        },
      ],
      total: 1,
      limit: 50,
      offset: 0,
    });

    await waitFor(() => expect(screen.queryByText("stale-user")).toBeNull());
  });

  it("switches to the task-diagnosis tab and hides the records view", async () => {
    render(<GenerationRecordsPage />);
    await screen.findByText("人物置换首帧");

    fireEvent.click(screen.getByRole("tab", { name: "任务诊断" }));

    expect(screen.getByLabelText("诊断任务编号")).toBeInTheDocument();
    expect(screen.queryByLabelText("生成账号")).toBeNull();
  });

  it("opens the diagnosis for a failed analysis record", async () => {
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      items: [
        {
          record_id: "analysis-1",
          record_type: "ANALYSIS",
          operation: "ANALYZE_VIDEO",
          user_id: "user-1",
          username: "customer-1",
          display_name: "客户一",
          project_id: "project-1",
          project_name: "演示项目",
          status: "FAILED",
          provider: "apilio",
          model: "gemini-3.8-flash",
          provider_cost: null,
          provider_cost_status: "UNAVAILABLE",
          record_data_status: "VALID",
          charged_credits: 0,
          result_reference: null,
          provider_reference: null,
          error_code: "ANALYSIS_PROVIDER_FAILED",
          error_message: "视频拆解服务拒绝了请求（HTTP 400）",
          created_at: "2026-09-21T10:00:00Z",
          completed_at: "2026-09-21T10:00:05Z",
          failure_phase: "http",
          retryable: true,
          upstream_status: 400,
          upstream_reason: "model not available",
        },
      ],
      total: 1,
      limit: 50,
      offset: 0,
    });

    render(
      <GenerationRecordsPage
        initialStatus="FAILED"
        initialRecordType="ANALYSIS"
      />,
    );

    fireEvent.click(await screen.findByText("查看详情"));
    fireEvent.click(screen.getByRole("button", { name: "查看诊断" }));

    await waitFor(() =>
      expect(adminApi.getAdminAnalysisDiagnostics).toHaveBeenCalledWith({
        taskId: "analysis-1",
        requestId: undefined,
      }),
    );
    expect(screen.getByLabelText("诊断任务编号")).toHaveValue("analysis-1");
  });

  it("offers first-frame reconcile only for a SUBMISSION_UNCERTAIN row", async () => {
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      items: [
        firstFrameRecord({ record_id: "ff-uncertain" }),
        firstFrameRecord({ record_id: "ff-done", status: "SUCCEEDED" }),
      ],
      total: 2,
      limit: 50,
      offset: 0,
    });

    render(<GenerationRecordsPage />);
    for (const summary of await screen.findAllByText("查看详情")) {
      fireEvent.click(summary);
    }

    // 服务端也只在 SUBMISSION_UNCERTAIN 放行，其余状态一律 409；前端按同一
    // 条件显示入口，避免运营点开就是错。
    expect(
      screen.getByRole("button", { name: "重新对账首帧任务 ff-uncertain" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "重新对账首帧任务 ff-done" }),
    ).toBeNull();
  });

  it("hides the reconcile entry from the read-only auditor role", async () => {
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      items: [firstFrameRecord({ record_id: "ff-uncertain" })],
      total: 1,
      limit: 50,
      offset: 0,
    });

    render(<GenerationRecordsPage readOnly />);
    fireEvent.click(await screen.findByText("查看详情"));

    expect(
      screen.queryByRole("button", { name: "重新对账首帧任务 ff-uncertain" }),
    ).toBeNull();
  });

  it("reconciles a stuck first-frame task and reports the server verdict", async () => {
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      items: [firstFrameRecord({ record_id: "ff-stuck" })],
      total: 1,
      limit: 50,
      offset: 0,
    });
    vi.mocked(adminApi.reconcileFirstFrameTask).mockResolvedValue({
      task_id: "ff-stuck",
      result: "RESUMED",
      detail_code: null,
    });

    render(<GenerationRecordsPage />);
    fireEvent.click(await screen.findByText("查看详情"));
    fireEvent.click(
      screen.getByRole("button", { name: "重新对账首帧任务 ff-stuck" }),
    );
    await screen.findByRole("dialog");

    fireEvent.click(screen.getByRole("button", { name: "重新对账" }));

    // standard 级：不收原因，因此按空串发出（该端点也不消费 reason）。
    await waitFor(() =>
      expect(adminApi.reconcileFirstFrameTask).toHaveBeenCalledWith(
        "ff-stuck",
        "",
      ),
    );
    expect(
      await screen.findByText(/已对账：任务 ff-stuck 供应商侧已受理/),
    ).toBeInTheDocument();
    // 对账后重新拉取列表：状态已被服务端改写，旧行不该留在页面上。
    await waitFor(() =>
      expect(adminApi.getAdminGenerationRecords).toHaveBeenCalledTimes(2),
    );
  });

  it("surfaces a rejected reconcile inside the dialog", async () => {
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      items: [firstFrameRecord({ record_id: "ff-raced" })],
      total: 1,
      limit: 50,
      offset: 0,
    });
    vi.mocked(adminApi.reconcileFirstFrameTask).mockRejectedValue(
      new Error("Only SUBMISSION_UNCERTAIN tasks can be reconciled."),
    );

    render(<GenerationRecordsPage />);
    fireEvent.click(await screen.findByText("查看详情"));
    fireEvent.click(
      screen.getByRole("button", { name: "重新对账首帧任务 ff-raced" }),
    );
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: "重新对账" }));

    expect(
      await screen.findByText(
        "Only SUBMISSION_UNCERTAIN tasks can be reconciled.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});
