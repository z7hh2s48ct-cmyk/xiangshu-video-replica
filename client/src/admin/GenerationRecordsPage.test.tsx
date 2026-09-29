import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as adminApi from "../api.admin";
import { GenerationRecordsPage } from "./GenerationRecordsPage";

vi.mock("../api.admin", () => ({
  getAdminGenerationRecords: vi.fn(),
  getAdminGenerationRecordSummary: vi.fn(),
  getAdminGenerationRecordCalls: vi.fn(),
  getAdminGenerationRecordThumbnail: vi.fn(),
  getGenerationRecordContent: vi.fn(),
  getExternalCallResponse: vi.fn(),
  getAdminAnalysisDiagnostics: vi.fn(),
  reconcileFirstFrameTask: vi.fn(),
  retryGenerationRecord: vi.fn(),
  createCustomerAdjustment: vi.fn(),
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
    has_preview: false,
    ...overrides,
  } as adminApi.AdminGenerationRecord;
}

/** 视频失败记录行：默认「未触达服务商的失败」，可原地重试、需补偿的那一档。 */
function videoFailureRecord(
  overrides: Partial<adminApi.AdminGenerationRecord> = {},
): adminApi.AdminGenerationRecord {
  return {
    record_id: "video-failed-1",
    record_type: "VIDEO",
    operation: "I2V",
    user_id: "user-1",
    username: "customer-1",
    display_name: "客户一",
    project_id: "project-1",
    project_name: "演示项目",
    status: "FAILED",
    provider: null,
    model: null,
    provider_cost: null,
    provider_cost_status: "UNAVAILABLE",
    record_data_status: "VALID",
    charged_credits: 6,
    result_reference: null,
    provider_reference: null,
    error_code: "H3_SETTINGS_UNAVAILABLE",
    error_message: "生成服务配置不可用",
    created_at: "2026-09-29T10:00:00Z",
    completed_at: "2026-09-29T10:00:05Z",
    failure_category: "配置问题",
    failure_owner: "技术处理",
    advice: "到管理端检查生成服务设置并测试连接后重试。",
    credits_refunded: false,
    has_preview: false,
    ...overrides,
  } as adminApi.AdminGenerationRecord;
}

/** 成功出片的视频记录：有可预览的成片与缩略图（P2-2）。 */
function videoSuccessRecord(
  overrides: Partial<adminApi.AdminGenerationRecord> = {},
): adminApi.AdminGenerationRecord {
  return {
    record_id: "video-done-1",
    record_type: "VIDEO",
    operation: "I2V",
    user_id: "user-1",
    username: "customer-1",
    display_name: "客户一",
    project_id: "project-1",
    project_name: "演示项目",
    status: "SUCCEEDED",
    provider: "minimax",
    model: "Hailuo-02",
    provider_cost: 1.25,
    provider_cost_status: "ESTIMATED",
    record_data_status: "VALID",
    charged_credits: 6,
    result_reference: "asset-1",
    provider_reference: "provider-task-1",
    error_code: null,
    error_message: null,
    created_at: "2026-09-29T10:00:00Z",
    completed_at: "2026-09-29T10:05:00Z",
    has_preview: true,
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
    // 缩略图默认给「没有派生小图」：展开详情的用例都会渲染它，不该让它们各自
    // 关心图片；要验图片的用例自己覆盖这一条。
    vi.mocked(adminApi.getAdminGenerationRecordThumbnail).mockResolvedValue({
      record_type: "FIRST_FRAME_IMAGE",
      record_id: "ff-1",
      url: null,
      expires_in_seconds: 604800,
    } as Awaited<
      ReturnType<typeof adminApi.getAdminGenerationRecordThumbnail>
    >);
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
          has_preview: false,
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
          has_preview: false,
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
          has_preview: false,
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

    // P2-1：类型下拉从词典生成后，“人物置换首帧/源画面 AI 评分”
    // 同时出现在下拉选项与表格单元格，断言限定到表格内。
    const table = await screen.findByRole("table", {
      name: "用户生成记录列表",
    });
    expect(within(table).getByText("人物置换首帧")).toBeInTheDocument();
    expect(within(table).getByText("源画面 AI 评分")).toBeInTheDocument();
    expect(within(table).getAllByText("成本待核对")).toHaveLength(2);
    expect(within(table).getByText("估算 ¥1.25")).toBeInTheDocument();
    expect(screen.getByText("apilio / gpt-image-2")).toBeInTheDocument();
    expect(
      screen.getByText("apilio_gemini / gemini-2.5-flash"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("customer-1")).toHaveLength(3);
    // P2-2：这三条都没有产物，预览列如实占位而不是留空。
    expect(within(table).getAllByText("无产物")).toHaveLength(3);
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
          has_preview: false,
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
          record_type: "ANALYSIS",
          error_code: "ANALYSIS_PROVIDER_FAILED",
          failure_phase: "http",
          reason: upstreamReason,
          retryable: false,
          count: 1,
          advice: fixAdvice,
        },
        // 旧数据没有错误码与可重试判定，runbook 也给不出建议：这一行不该凭空
        // 长出一句建议，也不该把「未判定」写成「不可重试」。
        {
          record_type: "ANALYSIS",
          error_code: null,
          failure_phase: null,
          reason: null,
          retryable: null,
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
    // 聚合块把「哪个任务、哪一步失败、谁来处理、上游怎么说」摆在列表之前。
    expect(screen.getByText("视频拆解 失败 1")).toBeInTheDocument();
    expect(
      screen.getByText(
        "视频拆解 · 上游拒绝（HTTP） · ANALYSIS_PROVIDER_FAILED · 不可重试 · 1 条",
      ),
    ).toBeInTheDocument();
    // retryable 缺失的历史行省掉重试判定，只说事实。
    expect(
      screen.getByText("视频拆解 · 未知阶段 · 未记录错误码 · 2 条"),
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
          record_type: "FIRST_FRAME_IMAGE",
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
          has_preview: false,
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
          has_preview: false,
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
          has_preview: false,
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

  it("retries a failed video task in place and reports the queue verdict", async () => {
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      items: [videoFailureRecord()],
      total: 1,
      limit: 50,
      offset: 0,
    });
    vi.mocked(adminApi.retryGenerationRecord).mockResolvedValue({
      task_id: "video-failed-1",
      status: "PENDING",
      archive_status: "PENDING",
    });

    render(<GenerationRecordsPage initialStatus="FAILED" />);
    fireEvent.click(await screen.findByText("查看详情"));

    // P1-1：详情给出失败分类、处理人与积分退回状态。
    expect(screen.getByText("配置问题")).toBeInTheDocument();
    expect(screen.getByText("技术处理")).toBeInTheDocument();
    expect(screen.getByText("未退回")).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "重试任务 video-failed-1" }),
    );
    await screen.findByRole("dialog");
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "运营手动重试" },
    });
    fireEvent.click(screen.getByRole("button", { name: "重新入队" }));

    await waitFor(() =>
      expect(adminApi.retryGenerationRecord).toHaveBeenCalledWith(
        "video-failed-1",
        "运营手动重试",
      ),
    );
    expect(
      await screen.findByText(/已重新入队：任务 video-failed-1/),
    ).toBeInTheDocument();
    // 重试后重新拉取列表：状态已被服务端改写，旧行不该留在页面上。
    await waitFor(() =>
      expect(adminApi.getAdminGenerationRecords).toHaveBeenCalledTimes(2),
    );
  });

  it("compensates a failed record through the audited adjustment", async () => {
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      items: [videoFailureRecord()],
      total: 1,
      limit: 50,
      offset: 0,
    });
    vi.mocked(adminApi.createCustomerAdjustment).mockResolvedValue({
      adjustment_id: "adj-1",
      order_id: "order-1",
      credits: "8",
      amount_fen: "0",
      pricing_scope: "COMPENSATION",
      wallet_balance_after: 106,
      source_document_type: "CREDIT_COMPENSATION",
      source_document_ref: "video-failed-1",
      request_id: "req-1",
    });

    render(<GenerationRecordsPage initialStatus="FAILED" />);
    fireEvent.click(await screen.findByText("查看详情"));
    fireEvent.click(
      screen.getByRole("button", { name: "补偿积分 video-failed-1" }),
    );
    await screen.findByRole("dialog");

    // 默认填该条被扣的积分，运营可改。
    const creditsInput = screen.getByLabelText("补偿积分数量");
    expect(creditsInput).toHaveValue("6");
    fireEvent.change(creditsInput, { target: { value: "8" } });
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客服补偿" },
    });
    // reasonAndAck 级：确认后直接进客户钱包，必须勾选知晓。
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "发放补偿" }));

    await waitFor(() =>
      expect(adminApi.createCustomerAdjustment).toHaveBeenCalledWith(
        "user-1",
        {
          sourceDocumentType: "CREDIT_COMPENSATION",
          sourceDocumentRef: "video-failed-1",
          credits: 8,
        },
        "客服补偿",
        expect.any(String),
      ),
    );
    expect(
      await screen.findByText("已补偿 8 积分：客户余额现为 106 积分。"),
    ).toBeInTheDocument();
  });

  it("copies a customer-facing note with category, advice and credit status", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      items: [videoFailureRecord()],
      total: 1,
      limit: 50,
      offset: 0,
    });

    render(<GenerationRecordsPage initialStatus="FAILED" />);
    fireEvent.click(await screen.findByText("查看详情"));
    fireEvent.click(
      screen.getByRole("button", { name: "复制客户说明 video-failed-1" }),
    );

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const note = writeText.mock.calls[0][0] as string;
    expect(note).toContain("任务编号：video-failed-1");
    expect(note).toContain("业务类型：视频生成");
    expect(note).toContain("失败分类：配置问题");
    expect(note).toContain(
      "处理建议：到管理端检查生成服务设置并测试连接后重试。",
    );
    expect(note).toContain(
      "积分处理：本次消耗的积分将按流程退回或补偿，请留意后续通知。",
    );
    expect(
      await screen.findByText(/已复制任务 video-failed-1 的客户说明/),
    ).toBeInTheDocument();
  });

  it("opens the audited video preview (P2-2)", async () => {
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValueOnce("blob:content");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      items: [videoSuccessRecord()],
      total: 1,
      limit: 50,
      offset: 0,
    });
    vi.mocked(adminApi.getGenerationRecordContent).mockResolvedValue(
      new Blob(["mp4"], { type: "video/mp4" }),
    );

    render(<GenerationRecordsPage />);

    // 行内不再自带缩略图请求：缩略图只在详情展开时由 RecordThumbnail 按需取。
    fireEvent.click(
      await screen.findByRole("button", { name: "查看成片 video-done-1" }),
    );

    // content 是高敏读取（服务端写审计）：只在显式点击时拉取一次。
    const dialog = await screen.findByRole("dialog", { name: "成片预览" });
    await waitFor(() =>
      expect(adminApi.getGenerationRecordContent).toHaveBeenCalledWith(
        "VIDEO",
        "video-done-1",
      ),
    );
    expect(
      within(dialog).getByText("任务 video-done-1 · customer-1 · 视频生成"),
    ).toBeInTheDocument();
    expect(within(dialog).getByLabelText("成片 video-done-1")).toHaveAttribute(
      "src",
      "blob:content",
    );
    expect(createObjectURL).toHaveBeenCalledTimes(1);

    fireEvent.click(within(dialog).getByRole("button", { name: "关闭" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "成片预览" })).toBeNull(),
    );
    // 关闭即释放预览 object URL，不禁锢内存。
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:content");
  });

  it("previews image-kind results (P2-2)", async () => {
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:image");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      items: [
        firstFrameRecord({
          record_id: "ff-done",
          status: "SUCCEEDED",
          has_preview: true,
        }),
      ],
      total: 1,
      limit: 50,
      offset: 0,
    });
    vi.mocked(adminApi.getGenerationRecordContent).mockResolvedValue(
      new Blob(["png"], { type: "image/png" }),
    );

    render(<GenerationRecordsPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "查看图片 ff-done" }),
    );

    const dialog = await screen.findByRole("dialog", { name: "生成图预览" });
    expect(
      await within(dialog).findByRole("img", { name: "生成图 ff-done" }),
    ).toHaveAttribute("src", "blob:image");

    // Esc 与「关闭」同一口径：关层并释放 object URL。
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "生成图预览" })).toBeNull(),
    );
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:image");
  });

  it("hides the whole preview column from the read-only auditor role (P2-2)", async () => {
    vi.mocked(adminApi.getAdminGenerationRecords).mockResolvedValue({
      items: [videoSuccessRecord()],
      total: 1,
      limit: 50,
      offset: 0,
    });

    render(<GenerationRecordsPage readOnly />);

    const table = await screen.findByRole("table", {
      name: "用户生成记录列表",
    });
    // 媒体端点对 auditor 一律 403：整列都不该出现。
    expect(within(table).queryByText("结果预览")).toBeNull();
    expect(screen.queryByRole("button", { name: /查看成片/ })).toBeNull();
    expect(adminApi.getGenerationRecordContent).not.toHaveBeenCalled();
  });
});
