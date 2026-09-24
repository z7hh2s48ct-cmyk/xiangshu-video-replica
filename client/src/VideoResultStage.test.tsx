import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { GenerationBatch, GenerationTask } from "./api";
import {
  generationBatchDisplayStatus,
  hasGenerationResultSource,
  VideoResultStage,
} from "./VideoResultStage";

function task(overrides: Partial<GenerationTask> = {}): GenerationTask {
  return {
    id: "task-ok",
    status: "SUCCEEDED",
    archive_status: "ARCHIVED",
    quality_status: "AUDIO_OK",
    quality_issue_codes: [],
    result_asset_id: "asset-ok",
    direct_result_available: false,
    stage: "COMPLETED",
    provider: "fake_h3",
    model: "MiniMax-H3",
    provider_task_id_tail: "34567890",
    attempt: 1,
    archive_retry_count: 0,
    estimated_cost: 1.25,
    actual_cost: 1.5,
    error_code: null,
    error_message_redacted: null,
    submitted_at: "2026-08-16 10:00:00",
    started_at: "2026-08-16 10:00:01",
    completed_at: "2026-08-16 10:00:06",
    duration_seconds: 5,
    retry_of_task_id: null,
    superseded_by_task_id: null,
    superseded_at: null,
    retry_reason: null,
    retry_requested_at: null,
    available_actions: [],
    prompt_snapshot: null,
    ...overrides,
  };
}

function batch(overrides: Partial<GenerationBatch> = {}): GenerationBatch {
  return {
    id: "batch-1",
    project_id: "project-1",
    prompt_version_id: "prompt-1",
    status: "NEEDS_ATTENTION",
    quantity: 2,
    stale: false,
    creation_kind: "replica",
    progress: {
      total_count: 2,
      terminal_count: 2,
      progress_percent: 100,
      counts: {
        pending: 0,
        submitting: 0,
        queued: 0,
        running: 0,
        archiving: 0,
        succeeded: 2,
        failed: 0,
        cancelled: 0,
        needs_attention: 1,
      },
    },
    tasks: [
      task(),
      task({
        id: "task-audio-failed",
        quality_status: "AUDIO_QUALITY_FAILED",
        quality_issue_codes: ["AUDIO_QUALITY_FAILED"],
        result_asset_id: "asset-audio-failed",
        stage: "QUALITY_FAILED",
      }),
    ],
    ...overrides,
  };
}

// 后端时间是 "YYYY-MM-DD HH:mm:ss"（UTC）；按偏移生成以驱动进度插值。
function serverTime(offsetSeconds: number): string {
  return new Date(Date.now() - offsetSeconds * 1000)
    .toISOString()
    .slice(0, 19)
    .replace("T", " ");
}

function renderStage(
  overrides: Partial<Parameters<typeof VideoResultStage>[0]> = {},
) {
  const props = {
    activeResultAction: "",
    activeTaskAction: "",
    batch: batch(),
    batchTitle: "夏日咖啡馆口播复刻",
    canOperate: true,
    onDownload: vi.fn(),
    onOpenOpsDetail: vi.fn(),
    onRegenerate: vi.fn(),
    onRequestPreview: vi.fn(),
    previewUrls: {} as Record<string, string>,
    resultErrors: {} as Record<string, string>,
    ...overrides,
    onPreviewSourceError: overrides.onPreviewSourceError ?? vi.fn(),
  };
  render(<VideoResultStage {...props} />);
  return props;
}

describe("VideoResultStage", () => {
  it("displays legacy UTC completion timestamps in Beijing time", () => {
    renderStage();
    expect(screen.getAllByText("2026/8/16 18:00:06").length).toBeGreaterThan(0);
  });
  it("shows delivered legacy QC results as complete without post-processing copy", () => {
    const delivered = task({
      stage: "QUALITY_FAILED",
      quality_status: "VISUAL_QUALITY_FAILED",
      quality_issue_codes: ["VIDEO_IDENTITY_DRIFT"],
      archive_status: "DIRECT",
      result_asset_id: null,
      direct_result_available: true,
    });
    renderStage({
      batch: batch({ quantity: 1, tasks: [delivered] }),
      previewUrls: { "task-ok": "https://preview.example/video.mp4" },
    });
    expect(
      screen.queryByText(/质检|保存成片|需要处理/),
    ).not.toBeInTheDocument();
    expect(screen.getByText("已完成")).toBeInTheDocument();
    expect(
      screen.queryByRole("list", { name: "生成阶段" }),
    ).not.toBeInTheDocument();
    expect(delivered.quality_status).toBe("VISUAL_QUALITY_FAILED");
  });
  it("uses the same player controls and replays after the ended event", () => {
    renderStage({
      previewUrls: { "task-ok": "https://preview.example/result.mp4" },
    });
    const video = screen.getByLabelText("结果预览 task-ok") as HTMLVideoElement;
    let paused = true;
    Object.defineProperty(video, "paused", { get: () => paused });
    const play = vi.spyOn(video, "play").mockImplementation(async () => {
      paused = false;
      fireEvent.play(video);
    });
    const pause = vi.spyOn(video, "pause").mockImplementation(() => {
      paused = true;
      fireEvent.pause(video);
    });
    fireEvent.click(
      screen.getByRole("button", { name: "播放 结果预览 task-ok" }),
    );
    expect(play).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "暂停" }));
    expect(pause).toHaveBeenCalledTimes(1);
    fireEvent.click(video);
    expect(play).toHaveBeenCalledTimes(2);
    paused = true;
    video.currentTime = 12;
    Object.defineProperty(video, "ended", { value: true });
    fireEvent.ended(video);
    fireEvent.click(
      screen.getByRole("button", { name: "播放 结果预览 task-ok" }),
    );
    expect(play).toHaveBeenCalledTimes(3);
    expect(video.currentTime).toBe(0);
  });

  it("displays visual findings honestly without blocking direct preview or download", () => {
    const visualTask = task({
      archive_status: "DIRECT",
      result_asset_id: null,
      direct_result_available: true,
      quality_status: "VISUAL_QUALITY_FAILED",
      quality_issue_codes: [
        "VIDEO_IDENTITY_DRIFT",
        "VIDEO_MOTION_DISCONTINUITY",
        "VIDEO_SEVERE_FLICKER",
      ],
      stage: "QUALITY_FAILED",
    });
    const props = renderStage({
      batch: batch({ tasks: [visualTask] }),
      previewUrls: { "task-ok": "https://preview.example/result.mp4" },
    });
    expect(
      screen.queryByText(
        /画面质检未通过|人物一致性变化|动作连续性问题|画面闪烁/,
      ),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/音频质检未通过|建议再次生成/),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下载 MP4" }));
    expect(props.onDownload).toHaveBeenCalledWith(visualTask);
  });

  it.each([
    ["AUDIO_OK", [], "音频质检通过", "passed"],
    ["AUDIO_QUALITY_FAILED", [], "音频质检未通过", "failed"],
    ["VISUAL_VALIDATION_UNAVAILABLE", [], "画面质检暂不可用", "unavailable"],
    [
      "AUDIO_QUALITY_FAILED",
      ["AUDIO_VALIDATION_UNAVAILABLE"],
      "音频质检暂不可用",
      "unavailable",
    ],
    ["PENDING", [], "质检待完成", "pending"],
  ])("does not show legacy %s checks on delivered media", (status, codes) => {
    const result = task({
      quality_status: status as string,
      quality_issue_codes: codes as string[],
    });
    renderStage({ batch: batch({ quantity: 1, tasks: [result] }) });
    expect(screen.queryByText(/质检/)).not.toBeInTheDocument();
    expect(result.quality_status).toBe(status);
    expect(result.quality_issue_codes).toEqual(codes);
  });

  it("does not normalize truncated snapshots or hide uncertain billing", () => {
    expect(generationBatchDisplayStatus(batch({ quantity: 3 }))).toBe(
      "NEEDS_ATTENTION",
    );
    expect(
      generationBatchDisplayStatus(
        batch({
          quantity: 1,
          tasks: [task({ status: "SUBMISSION_UNCERTAIN" })],
        }),
      ),
    ).toBe("NEEDS_ATTENTION");
    expect(
      generationBatchDisplayStatus(
        batch({
          quantity: 1,
          tasks: [task({ status: "FAILED", stage: "COMPLETED" })],
        }),
      ),
    ).toBe("NEEDS_ATTENTION");
  });

  it("keeps submission uncertainty visible even when a legacy stage says completed", () => {
    renderStage({
      batch: batch({
        quantity: 1,
        tasks: [
          task({
            status: "SUBMISSION_UNCERTAIN",
            stage: "COMPLETED",
            result_asset_id: null,
            direct_result_available: false,
          }),
        ],
      }),
    });
    expect(
      screen.getByText("需要处理", { selector: "strong" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("已完成")).not.toBeInTheDocument();
  });

  it("does not treat a failed archive asset placeholder as an available video", () => {
    const failedTask = task({
      archive_status: "ARCHIVE_FAILED",
      status: "FAILED",
      stage: "ARCHIVE_FAILED",
      quality_status: "VISUAL_VALIDATION_UNAVAILABLE",
    });
    const props = renderStage({
      batch: batch({
        tasks: [failedTask],
        progress: {
          ...batch().progress,
          total_count: 1,
          terminal_count: 1,
          counts: { ...batch().progress.counts, succeeded: 0, failed: 1 },
        },
      }),
    });
    expect(
      screen.getByText("需要处理", { selector: "strong" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "下载 MP4" }),
    ).not.toBeInTheDocument();
    expect(props.onRequestPreview).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("list", { name: "生成阶段" }),
    ).not.toBeInTheDocument();
    expect(hasGenerationResultSource(failedTask)).toBe(false);
    expect(
      hasGenerationResultSource({
        ...failedTask,
        direct_result_available: true,
      }),
    ).toBe(true);
  });
  it("renders the stage progress narrative with reassurance while rendering", () => {
    renderStage({
      batch: batch({
        status: "QUEUED",
        quantity: 1,
        progress: {
          total_count: 1,
          terminal_count: 0,
          progress_percent: 0,
          counts: {
            pending: 0,
            submitting: 0,
            queued: 0,
            running: 1,
            archiving: 0,
            succeeded: 0,
            failed: 0,
            cancelled: 0,
            needs_attention: 0,
          },
        },
        tasks: [
          task({
            status: "RUNNING",
            stage: "RUNNING",
            archive_status: "PENDING",
            quality_status: "PENDING",
            result_asset_id: null,
            submitted_at: serverTime(65),
            started_at: serverTime(60),
            completed_at: null,
            duration_seconds: null,
          }),
        ],
      }),
    });

    // Provider 没有真实进度百分比：主视图只呈现后端阶段，不伪造数值。
    expect(
      screen.queryByRole("progressbar", { name: "生成进度" }),
    ).not.toBeInTheDocument();

    // 阶段叙事：步骤条 + 阶段文案 + 时间预期。
    const stageList = screen.getByRole("list", { name: "生成阶段" });
    expect(stageList.querySelectorAll("li")[2]).toHaveClass(
      "video-stage-step--active",
    );
    expect(stageList.querySelectorAll("li")[0]).toHaveClass(
      "video-stage-step--done",
    );
    expect(
      screen.getByText("AI 正在基于你的首帧渲染画面、动作与口型…"),
    ).toBeInTheDocument();
    expect(screen.getByText(/已用时 1 分/)).toBeInTheDocument();
    expect(screen.queryByText(/\d+%/)).not.toBeInTheDocument();
  });

  it("shows an explicit not-lost reassurance when rendering runs long", () => {
    renderStage({
      batch: batch({
        tasks: [
          task({
            status: "RUNNING",
            stage: "RUNNING",
            archive_status: "PENDING",
            quality_status: "PENDING",
            result_asset_id: null,
            submitted_at: serverTime(500),
            started_at: serverTime(400),
          }),
        ],
      }),
    });

    expect(
      screen.getByText("渲染时间超过常规预估，任务仍在后台继续执行。"),
    ).toBeInTheDocument();
  });

  it("streams the finished result automatically with an info bar", () => {
    const onRequestPreview = vi.fn();
    renderStage({
      onRequestPreview,
      previewUrls: { "task-ok": "https://stage-preview/asset-ok" },
    });

    const video = screen.getByLabelText("结果预览 task-ok");
    expect(video).toHaveAttribute("src", "https://stage-preview/asset-ok");
    expect(video).toHaveAttribute("preload", "auto");
    expect(
      screen.getByRole("heading", { name: "结果信息" }),
    ).toBeInTheDocument();
    expect(screen.getByText("任务已结束 2 / 2")).toBeInTheDocument();
    expect(screen.queryByText(/MiniMax/i)).not.toBeInTheDocument();
    expect(screen.queryByText("音频质检通过")).not.toBeInTheDocument();
    expect(
      screen.getByText("生成通道 / 费用").nextElementSibling,
    ).toHaveTextContent("¥1.50");
    expect(
      screen.getByRole("button", { name: "下载 MP4" }),
    ).toBeInTheDocument();
    // 已有播放地址时不重复签发。
    expect(onRequestPreview).not.toHaveBeenCalled();
  });

  it("shows a provider result while the task is still preparing playback", () => {
    renderStage({
      batch: batch({
        tasks: [
          task({
            status: "ARCHIVING",
            stage: "ARCHIVING",
            archive_status: "PENDING",
            quality_status: "PENDING",
            result_asset_id: null,
            direct_result_available: true,
          }),
        ],
      }),
      previewUrls: { "task-ok": "https://provider.example/result.mp4" },
    });

    expect(screen.getByLabelText("结果预览 task-ok")).toHaveAttribute(
      "src",
      "https://provider.example/result.mp4",
    );
  });

  it("waits for a click with sound on: no autoplay, custom controls", () => {
    renderStage({
      previewUrls: { "task-ok": "https://stage-preview/asset-ok" },
    });

    const video = screen.getByLabelText("结果预览 task-ok");
    // 不自动播放、不静音：有声播放由用户点击开启。
    expect(video).not.toHaveAttribute("autoplay");
    expect(video).not.toHaveAttribute("muted");
    expect(video).not.toHaveAttribute("loop");
    expect(video).not.toHaveAttribute("controls");
    // 首帧等待点击：中央大播放按钮 + 常驻控制条。
    expect(
      screen.getByRole("button", { name: "播放 结果预览 task-ok" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "播放" })).toBeInTheDocument();
    expect(screen.getByLabelText("播放进度")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "静音" })).toBeInTheDocument();
    expect(screen.getByLabelText("音量")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "全屏" })).toBeInTheDocument();
    expect(screen.getByText("0:00 / 0:00")).toBeInTheDocument();
  });

  it("requests a streaming url automatically for archived results", () => {
    const onRequestPreview = vi.fn();
    renderStage({ onRequestPreview });

    expect(onRequestPreview).toHaveBeenCalledWith(
      expect.objectContaining({ id: "task-ok" }),
    );
  });

  it("stops auto-retrying a failed streaming request until manual retry", () => {
    const onRequestPreview = vi.fn();
    renderStage({
      onRequestPreview,
      resultErrors: { "task-ok": "预览链接获取失败，请重试。" },
    });

    expect(onRequestPreview).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "重新获取播放地址" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新获取播放地址" }));
    expect(onRequestPreview).toHaveBeenCalledTimes(1);
  });

  it("offers direct download and preview recovery while background processing continues", () => {
    const directTask = task({
      status: "ARCHIVING",
      stage: "ARCHIVING",
      archive_status: "PENDING",
      result_asset_id: null,
      direct_result_available: true,
    });
    const props = renderStage({
      batch: batch({ tasks: [directTask] }),
      resultErrors: { "task-ok": "视频已生成，但播放地址暂时不可用。" },
    });
    fireEvent.click(screen.getByRole("button", { name: "下载原文件" }));
    expect(props.onDownload).toHaveBeenCalledWith(directTask);
    fireEvent.click(screen.getByRole("button", { name: "重新获取播放地址" }));
    expect(props.onRequestPreview).toHaveBeenCalledTimes(1);
  });

  it("keeps a direct result read-only for auditors", () => {
    const props = renderStage({
      canOperate: false,
      batch: batch({
        tasks: [
          task({
            archive_status: "DIRECT",
            result_asset_id: null,
            direct_result_available: true,
          }),
        ],
      }),
    });
    expect(props.onRequestPreview).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("button", { name: "下载 MP4" }),
    ).not.toBeInTheDocument();
  });

  it("switches the active result from the filmstrip", () => {
    const onRequestPreview = vi.fn();
    renderStage({
      onRequestPreview,
      previewUrls: { "task-ok": "https://stage-preview/asset-ok" },
    });

    expect(screen.getByLabelText("结果预览 task-ok")).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "查看结果 2：task-audio-failed" }),
    );

    // 历史检查结果不再影响结果展示。
    expect(
      screen.queryByText(/音频质检未通过：检查结果仅供参考/),
    ).not.toBeInTheDocument();
    expect(onRequestPreview).toHaveBeenCalledWith(
      expect.objectContaining({ id: "task-audio-failed" }),
    );
  });

  it("requires a preset reason and payment confirmation for paid regeneration", () => {
    const onRegenerate = vi.fn();
    renderStage({
      onRegenerate,
      batch: batch({
        quantity: 1,
        tasks: [task({ available_actions: ["REGENERATE"] })],
      }),
    });

    fireEvent.click(screen.getByRole("button", { name: "再次生成" }));
    const submit = screen.getByRole("button", { name: "确认再次生成" });
    expect(submit).toBeDisabled();

    fireEvent.click(screen.getByRole("radio", { name: "画面质量不佳" }));
    expect(submit).toBeDisabled();

    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "确认为任务 task-ok 新增一次付费生成",
      }),
    );
    fireEvent.change(screen.getByLabelText("再次生成补充说明 task-ok"), {
      target: { value: "人物手部细节问题" },
    });
    fireEvent.click(submit);

    expect(onRegenerate).toHaveBeenCalledWith(
      expect.objectContaining({ id: "task-ok" }),
      "画面质量不佳：人物手部细节问题",
    );
  });

  it("keeps auditors read-only without streaming requests", () => {
    const onRequestPreview = vi.fn();
    renderStage({ canOperate: false, onRequestPreview });

    expect(
      screen.getByText("审计只读，不可预览或下载结果"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "下载 MP4" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "再次生成" }),
    ).not.toBeInTheDocument();
    expect(onRequestPreview).not.toHaveBeenCalled();
  });

  it("guides attention tasks to the ops view", () => {
    const onOpenOpsDetail = vi.fn();
    renderStage({
      onOpenOpsDetail,
      batch: batch({
        tasks: [
          task({
            id: "task-uncertain",
            status: "SUBMISSION_UNCERTAIN",
            stage: "SUBMISSION_UNCERTAIN",
            archive_status: "PENDING",
            quality_status: "PENDING",
            result_asset_id: null,
            available_actions: ["RECONCILE"],
          }),
        ],
      }),
    });

    expect(
      screen.getByText(/该任务需要人工处理（对账、归档重试或账单确认）/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "运维详情" }));
    expect(onOpenOpsDetail).toHaveBeenCalledTimes(1);
  });

  it("never renders a provider brand from a failed task", () => {
    renderStage({
      batch: batch({
        tasks: [
          task({
            status: "FAILED",
            stage: "FAILED",
            archive_status: "PENDING",
            quality_status: "PENDING",
            result_asset_id: null,
            error_code: "METASO_UPSTREAM_TIMEOUT",
            error_message_redacted: "MiniMax H3 request timed out",
          }),
        ],
      }),
    });

    expect(
      screen.getByText(
        "视频生成服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/MiniMax|Metaso|H3/i)).not.toBeInTheDocument();
  });
});
