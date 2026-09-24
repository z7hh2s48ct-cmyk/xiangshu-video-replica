import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { TaskRecordsPanel } from "./TaskRecordsPanel";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    createGenerationResultPreviewUrl: vi.fn(),
    createGenerationTaskPreviewUrl: vi.fn(),
    downloadGenerationResult: vi.fn(),
    downloadGenerationTaskResult: vi.fn(),
    openVideoDownloadFolder: vi.fn(),
    getGenerationBatch: vi.fn(),
    getLatestGenerationReconcileOperation: vi.fn(),
    listGenerationBatches: vi.fn(),
    regenerateGenerationBatch: vi.fn(),
    regenerateGenerationTask: vi.fn(),
    renameGenerationBatch: vi.fn(),
    deleteGenerationBatch: vi.fn(),
    retryGenerationTask: vi.fn(),
    confirmGenerationTaskNotCharged: vi.fn(),
    reconcileUncertainTask: vi.fn(),
    waitForGenerationReconcileOperation: vi.fn(),
  };
});

function task(overrides: Partial<api.GenerationTask> = {}): api.GenerationTask {
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

function batch(
  overrides: Partial<api.GenerationBatch> = {},
): api.GenerationBatch {
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

function listItem(
  overrides: Partial<api.GenerationBatchListItem> = {},
): api.GenerationBatchListItem {
  const detail = batch();
  return {
    id: detail.id,
    project_id: detail.project_id,
    project_name: "夏日咖啡馆口播复刻",
    created_by_user_id: "employee_1",
    created_by_display_name: "林夏",
    prompt_version_id: detail.prompt_version_id,
    status: detail.status,
    quantity: detail.quantity,
    creation_kind: detail.creation_kind,
    created_at: "2026-08-16 10:00:00",
    updated_at: "2026-08-16 10:00:06",
    progress: detail.progress,
    total_estimated_cost: 2.5,
    total_actual_cost: 3,
    needs_attention_count: 1,
    has_results: true,
    tasks: detail.tasks.map(
      ({ prompt_snapshot: _promptSnapshot, ...item }) => item,
    ),
    ...overrides,
  };
}

function reconcileOperation(
  overrides: Partial<api.GenerationReconcileOperation> = {},
): api.GenerationReconcileOperation {
  return {
    id: "reconcile-operation-1",
    task_id: "task-reconcile",
    status: "PENDING",
    attempt: 0,
    error_code: null,
    error_message: null,
    retryable: false,
    created_at: "2026-08-16 10:00:00",
    updated_at: "2026-08-16 10:00:00",
    started_at: null,
    completed_at: null,
    ...overrides,
  };
}

// 任务页默认落点是生成结果舞台；运维能力的既有用例统一先切到运维视图。
async function switchToOpsView() {
  fireEvent.click(await screen.findByRole("button", { name: "运维详情" }));
}

describe("TaskRecordsPanel", () => {
  it("displays historical batch timestamps in Beijing time", async () => {
    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="customer"
      />,
    );
    expect(await screen.findByText(/2026\/8\/16 18:00:00/)).toBeInTheDocument();
  });
  it("preserves an unconfirmed download warning without claiming failure or hiding preview", async () => {
    vi.mocked(api.downloadGenerationResult).mockRejectedValueOnce(
      new api.VideoDownloadUnconfirmedError(),
    );
    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="customer"
      />,
    );
    const video = await screen.findByLabelText("结果预览 task-ok");
    fireEvent.click(screen.getByRole("button", { name: "下载 MP4" }));
    expect(
      await screen.findByText(
        "尚未确认保存结果，请先检查所选文件夹，避免重复下载。",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/下载失败/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("结果预览 task-ok")).toBe(video);
  });

  it("hides customer-only empty operations and manual batch lookup", async () => {
    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="customer"
      />,
    );
    await screen.findByLabelText("结果预览 task-ok");
    expect(
      screen.queryByRole("button", { name: "运维详情" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/兼容查询/)).not.toBeInTheDocument();
  });

  it("keeps settlement delivery errors visible while allowing an available direct video", async () => {
    const delivered = task({
      status: "SUCCEEDED",
      archive_status: "ARCHIVE_FAILED",
      direct_result_available: true,
      result_asset_id: null,
      stage: "ARCHIVE_FAILED",
      error_code: "SETTLEMENT_FAILED",
      error_message_redacted: "费用结算失败，请联系管理员。",
      available_actions: ["RETRY"],
    });
    vi.mocked(api.getGenerationBatch).mockResolvedValue(
      batch({ quantity: 1, tasks: [delivered] }),
    );
    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="customer"
      />,
    );
    await screen.findByLabelText("结果预览 task-ok");
    expect(screen.getByRole("button", { name: "下载 MP4" })).toBeEnabled();
    expect(screen.getAllByText("需要处理").length).toBeGreaterThan(0);
    await switchToOpsView();
    expect(
      screen.getByText("费用结算失败，请联系管理员。"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "下载 MP4 task-ok" }),
    ).toBeEnabled();
  });
  it("waits for saving, prevents duplicate download, and exposes the saved path", async () => {
    let finish!: (value: {
      status: "saved";
      path: string;
      downloadId: string;
    }) => void;
    vi.mocked(api.downloadGenerationResult).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="customer"
      />,
    );
    await screen.findByLabelText("结果预览 task-ok");
    const button = screen.getByRole("button", { name: "下载 MP4" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(button).toBeDisabled();
    expect(api.downloadGenerationResult).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("下载成功")).not.toBeInTheDocument();
    await act(async () =>
      finish({
        status: "saved",
        path: "C:\\Videos\\result.mp4",
        downloadId: "download-1",
      }),
    );
    expect(await screen.findByText("下载成功")).toBeInTheDocument();
    expect(screen.getByText("C:\\Videos\\result.mp4")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "打开文件夹" }));
    expect(api.openVideoDownloadFolder).toHaveBeenCalledWith("download-1");
  });

  it.each([
    ["cancelled", "已取消下载"],
    ["started", "已交给浏览器下载，请查看浏览器下载列表。"],
  ] as const)(
    "reports %s without claiming a saved local file",
    async (status, message) => {
      vi.mocked(api.downloadGenerationResult).mockResolvedValue({ status });
      render(
        <TaskRecordsPanel
          handoffBatch={null}
          onHandoffConsumed={vi.fn()}
          userRole="customer"
        />,
      );
      const video = await screen.findByLabelText("结果预览 task-ok");
      fireEvent.click(screen.getByRole("button", { name: "下载 MP4" }));
      expect(await screen.findByText(message)).toBeInTheDocument();
      expect(screen.queryByText("下载成功")).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "打开文件夹" }),
      ).not.toBeInTheDocument();
      expect(screen.getByLabelText("结果预览 task-ok")).toBe(video);
      expect(screen.getByRole("button", { name: "下载 MP4" })).toBeEnabled();
    },
  );

  it("retains the saved path when opening the folder fails and shows it in task operations", async () => {
    vi.mocked(api.getGenerationBatch).mockResolvedValue(
      batch({ tasks: [task({ available_actions: ["REGENERATE"] })] }),
    );
    vi.mocked(api.downloadGenerationResult).mockResolvedValue({
      status: "saved",
      path: "C:\\Videos\\saved.mp4",
      downloadId: "download-2",
    });
    vi.mocked(api.openVideoDownloadFolder).mockRejectedValue(
      new Error("native denied https://private.example/?token=secret"),
    );
    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="customer"
      />,
    );
    await screen.findByLabelText("结果预览 task-ok");
    await switchToOpsView();
    fireEvent.click(screen.getByRole("button", { name: "下载 MP4 task-ok" }));
    expect(await screen.findByText("下载成功")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "打开文件夹" }));
    expect(
      await screen.findByText("无法打开文件夹，请按上方路径查看视频。"),
    ).toBeInTheDocument();
    expect(screen.getByText("C:\\Videos\\saved.mp4")).toBeInTheDocument();
    expect(
      screen.queryByText(/secret|private\.example/),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("结果预览 task-ok")).toBeInTheDocument();
  });

  it("isolates a late download completion from a different selected batch", async () => {
    let finish: ((result: api.VideoDownloadResult) => void) | undefined;
    vi.mocked(api.downloadGenerationResult).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    vi.mocked(api.listGenerationBatches).mockResolvedValue({
      items: [
        listItem(),
        listItem({ id: "batch-2", project_name: "第二个项目" }),
      ],
      next_cursor: null,
    });
    vi.mocked(api.getGenerationBatch).mockImplementation(async (id) =>
      batch({ id }),
    );
    render(
      <TaskRecordsPanel
        currentUserId="user-a"
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="customer"
      />,
    );
    await screen.findByLabelText("结果预览 task-ok");
    fireEvent.click(screen.getByRole("button", { name: "下载 MP4" }));
    fireEvent.click(screen.getByRole("button", { name: "打开批次 batch-2" }));
    await screen.findByRole("heading", { name: "第二个项目" });
    await act(async () =>
      finish?.({
        status: "saved",
        path: "C:\\Private\\first.mp4",
        downloadId: "first-download",
      }),
    );
    expect(
      screen.queryByText("C:\\Private\\first.mp4"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("下载成功")).not.toBeInTheDocument();
  });

  it("does not expose a late saved path after the signed-in account changes", async () => {
    let finish: ((result: api.VideoDownloadResult) => void) | undefined;
    vi.mocked(api.downloadGenerationResult).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const props = {
      handoffBatch: null,
      onHandoffConsumed: vi.fn(),
      userRole: "customer" as const,
    };
    const { rerender } = render(
      <TaskRecordsPanel {...props} currentUserId="user-a" />,
    );
    await screen.findByLabelText("结果预览 task-ok");
    fireEvent.click(screen.getByRole("button", { name: "下载 MP4" }));
    rerender(<TaskRecordsPanel {...props} currentUserId="user-b" />);
    await act(async () =>
      finish?.({
        status: "saved",
        path: "C:\\Private\\account-a.mp4",
        downloadId: "account-a-download",
      }),
    );
    expect(
      screen.queryByText("C:\\Private\\account-a.mp4"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("下载成功")).not.toBeInTheDocument();
  });
  beforeEach(() => {
    window.localStorage.clear();
    vi.mocked(api.listGenerationBatches).mockResolvedValue({
      items: [listItem()],
      next_cursor: null,
    });
    vi.mocked(api.getGenerationBatch).mockResolvedValue(batch());
    // 预签名 URL 直连播放：无限模式避免舞台自动签发耗尽 mock 后循环。
    vi.mocked(api.createGenerationResultPreviewUrl).mockImplementation(
      async (assetId) => `https://stage-preview/${assetId}`,
    );
    vi.mocked(api.createGenerationTaskPreviewUrl).mockImplementation(
      async (taskId) => `https://provider-preview/${taskId}`,
    );
    vi.mocked(api.downloadGenerationResult).mockResolvedValue({
      status: "started",
    });
    vi.mocked(api.downloadGenerationTaskResult).mockResolvedValue({
      status: "started",
    });
    vi.mocked(api.openVideoDownloadFolder).mockResolvedValue();
    vi.mocked(api.retryGenerationTask).mockImplementation(async (_taskId) =>
      task(),
    );
    vi.mocked(api.confirmGenerationTaskNotCharged).mockImplementation(
      async (_taskId) => task(),
    );
    vi.mocked(api.reconcileUncertainTask).mockImplementation(async (_taskId) =>
      reconcileOperation({ task_id: _taskId }),
    );
    vi.mocked(api.getLatestGenerationReconcileOperation).mockResolvedValue(
      null,
    );
    vi.mocked(api.waitForGenerationReconcileOperation).mockImplementation(
      async (operationId) =>
        reconcileOperation({
          id: operationId,
          status: "SUCCEEDED",
          completed_at: "2026-08-16 10:00:05",
        }),
    );
    vi.mocked(api.regenerateGenerationBatch).mockImplementation(
      async (_batchId) => batch({ id: "batch-regenerated" }),
    );
    vi.mocked(api.regenerateGenerationTask).mockImplementation(
      async (_taskId) => batch({ id: "batch-task-regenerated", quantity: 1 }),
    );
    vi.mocked(api.renameGenerationBatch).mockImplementation(
      async (_batchId, displayName) => batch({ display_name: displayName }),
    );
    vi.mocked(api.deleteGenerationBatch).mockResolvedValue();
  });

  afterEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();
    vi.useRealTimers();
    window.localStorage.clear();
  });

  it("opens the newest batch on the result stage with automatic streaming preview", async () => {
    let previewCount = 0;
    vi.mocked(api.createGenerationResultPreviewUrl).mockReset();
    vi.mocked(api.createGenerationResultPreviewUrl).mockImplementation(
      async () => `https://stage-preview-${++previewCount}`,
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );

    expect(await screen.findByText("夏日咖啡馆口播复刻")).toBeInTheDocument();
    expect(api.listGenerationBatches).toHaveBeenCalledWith({ limit: 20 });
    await waitFor(() =>
      expect(api.getGenerationBatch).toHaveBeenCalledWith("batch-1"),
    );

    // 默认落点是生成结果舞台：完成态任务自动签发在线播放地址并直接播放。
    const video = await screen.findByLabelText("结果预览 task-ok");
    expect(video).toHaveAttribute("src", "https://stage-preview-1");
    expect(video).toHaveAttribute("preload", "auto");
    expect(screen.queryByText(/质检/)).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "查看结果 2：task-audio-failed" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "结果信息" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/MiniMax/i)).not.toBeInTheDocument();
    expect(
      screen.getByText("生成通道 / 费用").nextElementSibling,
    ).toHaveTextContent("¥1.50");
    expect(screen.getByText("技术详情").closest("details")).not.toHaveAttribute(
      "open",
    );

    // 切换任务操作后不再展示质检，保留刷新预览与下载能力。
    fireEvent.click(screen.getByRole("button", { name: "运维详情" }));
    expect(
      await screen.findByRole("button", { name: "刷新预览 task-ok" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/质检/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "刷新预览 task-ok" }));
    // 舞台卸载后旧 video 节点不再更新，改在运维视图内重新查询。
    const refreshedVideo = await screen.findByLabelText("结果预览 task-ok");
    await waitFor(() =>
      expect(refreshedVideo).toHaveAttribute("src", "https://stage-preview-2"),
    );
    fireEvent.click(screen.getByRole("button", { name: "下载 MP4 task-ok" }));

    await waitFor(() =>
      expect(api.downloadGenerationResult).toHaveBeenCalledWith(
        "asset-ok",
        "task-ok.mp4",
      ),
    );
  });

  it("restores only the current user's last batch", async () => {
    window.localStorage.setItem("generation.batchId:user-a", "batch-user-a");
    window.localStorage.setItem("generation.batchId:user-b", "batch-user-b");
    vi.mocked(api.getGenerationBatch).mockImplementation(async (batchId) =>
      batch({ id: batchId }),
    );

    render(
      <TaskRecordsPanel
        currentUserId="user-a"
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );

    await waitFor(() =>
      expect(api.getGenerationBatch).toHaveBeenCalledWith("batch-user-a"),
    );
    expect(api.getGenerationBatch).not.toHaveBeenCalledWith("batch-user-b");
  });

  it("downloads a direct visual-review result and retries download without regenerating", async () => {
    const direct = task({
      available_actions: ["REGENERATE"],
      archive_status: "DIRECT",
      result_asset_id: null,
      direct_result_available: true,
      quality_status: "VISUAL_QUALITY_FAILED",
      quality_issue_codes: ["VIDEO_IDENTITY_DRIFT"],
      stage: "QUALITY_FAILED",
    });
    vi.mocked(api.getGenerationBatch).mockResolvedValue(
      batch({ tasks: [direct] }),
    );
    vi.mocked(api.downloadGenerationTaskResult).mockRejectedValueOnce(
      new Error("expired URL"),
    );
    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="customer"
      />,
    );
    const video = await screen.findByLabelText("结果预览 task-ok");
    fireEvent.click(screen.getByRole("button", { name: "下载 MP4" }));
    expect(
      await screen.findByText("下载失败，请检查网络和保存位置后重试。"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("结果预览 task-ok")).toBe(video);
    fireEvent.click(screen.getByRole("button", { name: "下载 MP4" }));
    await waitFor(() =>
      expect(api.downloadGenerationTaskResult).toHaveBeenCalledTimes(2),
    );
    expect(api.downloadGenerationTaskResult).toHaveBeenLastCalledWith(
      "task-ok",
      "task-ok.mp4",
    );
    expect(api.downloadGenerationResult).not.toHaveBeenCalled();
    expect(api.regenerateGenerationTask).not.toHaveBeenCalled();
    expect(api.regenerateGenerationBatch).not.toHaveBeenCalled();
    await switchToOpsView();
    expect(screen.queryByText(/质检/)).not.toBeInTheDocument();
    expect(screen.getByText("task-ok").closest("li")).not.toHaveTextContent(
      "需要处理",
    );
    expect(screen.queryByText("质检待完成")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下载 MP4 task-ok" }));
    await waitFor(() =>
      expect(api.downloadGenerationTaskResult).toHaveBeenCalledTimes(3),
    );
  });

  it.each([
    ["AUDIO_VALIDATION_UNAVAILABLE", "音频质检暂不可用"],
    ["VISUAL_VALIDATION_UNAVAILABLE", "画面质检暂不可用"],
  ])(
    "keeps %s advisory without marking a completed result as needing attention",
    async (code, label) => {
      const direct = task({
        available_actions: ["REGENERATE"],
        archive_status: "DIRECT",
        result_asset_id: null,
        direct_result_available: true,
        quality_status: "PENDING",
        quality_issue_codes: [code],
      });
      const completed = batch({
        status: "SUCCEEDED",
        quantity: 1,
        tasks: [direct],
        progress: {
          ...batch().progress,
          total_count: 1,
          terminal_count: 1,
          counts: {
            ...batch().progress.counts,
            succeeded: 1,
            needs_attention: 0,
          },
        },
      });
      vi.mocked(api.getGenerationBatch).mockResolvedValue(completed);
      vi.mocked(api.listGenerationBatches).mockResolvedValue({
        items: [
          listItem({
            status: completed.status,
            quantity: completed.quantity,
            tasks: completed.tasks,
            progress: completed.progress,
            needs_attention_count: 0,
          }),
        ],
        next_cursor: null,
      });
      render(
        <TaskRecordsPanel
          handoffBatch={null}
          onHandoffConsumed={vi.fn()}
          userRole="customer"
        />,
      );
      await screen.findByLabelText("结果预览 task-ok");
      expect(screen.queryByText(label)).not.toBeInTheDocument();
      expect(screen.queryByText(/需(?:要)?处理/)).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "下载 MP4" }));
      await waitFor(() =>
        expect(api.downloadGenerationTaskResult).toHaveBeenCalledWith(
          "task-ok",
          "task-ok.mp4",
        ),
      );
      await switchToOpsView();
      expect(screen.queryByText(label)).not.toBeInTheDocument();
      expect(screen.queryByText(/需(?:要)?处理/)).not.toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "下载 MP4 task-ok" }),
      ).toBeEnabled();
      expect(api.regenerateGenerationTask).not.toHaveBeenCalled();
      expect(api.regenerateGenerationBatch).not.toHaveBeenCalled();
    },
  );

  it("does not offer a failed archive record as a playable or downloadable result", async () => {
    vi.mocked(api.getGenerationBatch).mockResolvedValue(
      batch({
        tasks: [
          task({
            status: "FAILED",
            archive_status: "ARCHIVE_FAILED",
            stage: "ARCHIVE_FAILED",
          }),
        ],
        progress: {
          ...batch().progress,
          total_count: 1,
          terminal_count: 1,
          counts: {
            ...batch().progress.counts,
            succeeded: 0,
            failed: 1,
            needs_attention: 1,
          },
        },
      }),
    );
    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="customer"
      />,
    );
    await screen.findByRole("heading", { name: "结果信息" });
    expect(
      screen.queryByRole("button", { name: "下载 MP4" }),
    ).not.toBeInTheDocument();
    expect(api.createGenerationResultPreviewUrl).not.toHaveBeenCalled();
    await switchToOpsView();
    expect(screen.queryByText("结果已归档")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "下载 MP4 task-ok" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("任务已结束 1 / 1")).toBeInTheDocument();
  });

  it.each([false, true])(
    "replaces direct preview after the same task is archived (old signing delayed=%s)",
    async (delayed) => {
      const direct = batch({
        status: "SUCCEEDED",
        quantity: 1,
        tasks: [
          task({
            archive_status: "DIRECT",
            result_asset_id: null,
            direct_result_available: true,
          }),
        ],
      });
      const archived = batch({
        ...direct,
        tasks: [task({ result_asset_id: "normalized-asset" })],
      });
      let finishOld!: (url: string) => void;
      const pendingOld = new Promise<string>((resolve) => {
        finishOld = resolve;
      });
      vi.mocked(api.getGenerationBatch).mockResolvedValue(direct);
      vi.mocked(api.createGenerationTaskPreviewUrl).mockReturnValue(
        delayed
          ? pendingOld
          : Promise.resolve("https://provider-preview/old-direct"),
      );
      render(
        <TaskRecordsPanel
          handoffBatch={null}
          onHandoffConsumed={vi.fn()}
          userRole="customer"
        />,
      );
      await waitFor(() =>
        expect(api.createGenerationTaskPreviewUrl).toHaveBeenCalledWith(
          "task-ok",
        ),
      );
      if (!delayed)
        expect(
          await screen.findByLabelText("结果预览 task-ok"),
        ).toHaveAttribute("src", "https://provider-preview/old-direct");
      vi.mocked(api.getGenerationBatch).mockResolvedValue(archived);
      fireEvent.click(screen.getByRole("button", { name: "刷新" }));
      await waitFor(() =>
        expect(api.getGenerationBatch).toHaveBeenCalledTimes(2),
      );
      if (delayed)
        await act(async () => {
          finishOld("https://provider-preview/old-direct");
        });
      await waitFor(() =>
        expect(screen.getByLabelText("结果预览 task-ok")).toHaveAttribute(
          "src",
          "https://stage-preview/normalized-asset",
        ),
      );
      expect(api.createGenerationResultPreviewUrl).toHaveBeenCalledWith(
        "normalized-asset",
      );
    },
  );

  it("synchronizes list progress without remounting video or flashing during background polls", async () => {
    vi.useFakeTimers();
    const running = batch({
      status: "RUNNING",
      quantity: 1,
      tasks: [
        task({
          status: "ARCHIVING",
          stage: "ARCHIVING",
          archive_status: "ARCHIVING",
          result_asset_id: null,
          direct_result_available: true,
          quality_status: "PENDING",
        }),
      ],
      progress: {
        ...batch().progress,
        total_count: 1,
        terminal_count: 0,
        progress_percent: 0,
        counts: {
          ...batch().progress.counts,
          succeeded: 0,
          archiving: 1,
          needs_attention: 0,
        },
      },
    });
    const completed = batch({
      status: "SUCCEEDED",
      quantity: 1,
      tasks: [
        task({
          archive_status: "DIRECT",
          result_asset_id: null,
          direct_result_available: true,
        }),
      ],
      progress: {
        ...batch().progress,
        total_count: 1,
        terminal_count: 1,
        counts: {
          ...batch().progress.counts,
          succeeded: 1,
          needs_attention: 0,
        },
      },
    });
    vi.mocked(api.listGenerationBatches).mockResolvedValue({
      items: [listItem({ status: "QUEUED", progress: running.progress })],
      next_cursor: null,
    });
    let completePoll: (value: api.GenerationBatch) => void = () => undefined;
    const pendingPoll = new Promise<api.GenerationBatch>((resolve) => {
      completePoll = resolve;
    });
    vi.mocked(api.getGenerationBatch)
      .mockResolvedValue(completed)
      .mockResolvedValueOnce(running)
      .mockReturnValueOnce(pendingPoll);
    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="customer"
      />,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const video = screen.getByLabelText("结果预览 task-ok") as HTMLVideoElement;
    video.currentTime = 3;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(screen.queryByText("正在刷新任务记录")).not.toBeInTheDocument();
    await act(async () => {
      completePoll(completed);
      await pendingPoll;
    });
    const card = screen.getByRole("button", { name: "打开批次 batch-1" });
    expect(card).not.toHaveTextContent("排队中");
    expect(card).toHaveTextContent("已完成");
    expect(card).not.toHaveTextContent("需处理 1");
    expect(screen.getByLabelText("结果预览 task-ok")).toBe(video);
    expect(video.currentTime).toBe(3);
    expect(api.createGenerationTaskPreviewUrl).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(card).not.toHaveTextContent("排队中");
    expect(card).toHaveTextContent("已完成");
  });

  it.each(["刷新", "打开批次 batch-1"])(
    "refreshes terminal details through %s without replacing the player",
    async (buttonName) => {
      render(
        <TaskRecordsPanel
          handoffBatch={null}
          onHandoffConsumed={vi.fn()}
          userRole="employee"
        />,
      );
      const video = (await screen.findByLabelText(
        "结果预览 task-ok",
      )) as HTMLVideoElement;
      video.currentTime = 4;
      const updated = batch({
        status: "SUCCEEDED",
        tasks: [task()],
        progress: {
          ...batch().progress,
          counts: { ...batch().progress.counts, needs_attention: 0 },
        },
      });
      vi.mocked(api.getGenerationBatch).mockResolvedValue(updated);
      fireEvent.click(screen.getByRole("button", { name: buttonName }));
      await waitFor(() =>
        expect(api.getGenerationBatch).toHaveBeenCalledTimes(2),
      );
      expect(
        screen.getByRole("button", { name: "打开批次 batch-1" }),
      ).toHaveTextContent("已完成");
      expect(screen.getByLabelText("结果预览 task-ok")).toBe(video);
      expect(video.currentTime).toBe(4);
      expect(api.createGenerationResultPreviewUrl).toHaveBeenCalledTimes(1);
    },
  );

  it("keeps direct-result download unavailable to an auditor", async () => {
    vi.mocked(api.getGenerationBatch).mockResolvedValue(
      batch({
        tasks: [
          task({
            archive_status: "DIRECT",
            result_asset_id: null,
            direct_result_available: true,
          }),
        ],
      }),
    );
    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="auditor"
      />,
    );
    await screen.findByRole("heading", { name: "结果信息" });
    expect(
      screen.queryByRole("button", { name: "下载 MP4" }),
    ).not.toBeInTheDocument();
    expect(api.downloadGenerationTaskResult).not.toHaveBeenCalled();
    expect(api.createGenerationTaskPreviewUrl).not.toHaveBeenCalled();
  });

  it("keeps polling after repeated network failures and recovers automatically", async () => {
    vi.useFakeTimers();
    window.localStorage.setItem("generation.batchId", "batch-1");
    vi.mocked(api.getGenerationBatch).mockReset();
    for (let attempt = 0; attempt < 6; attempt += 1) {
      vi.mocked(api.getGenerationBatch).mockRejectedValueOnce(
        new Error("temporary network failure"),
      );
    }
    vi.mocked(api.getGenerationBatch).mockResolvedValueOnce(
      batch({ tasks: [task({ result_asset_id: null })] }),
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100_000);
    });

    expect(api.getGenerationBatch).toHaveBeenCalledTimes(7);
    expect(screen.queryByText(/网络连接失败/)).not.toBeInTheDocument();
  });

  it("lets the user refresh immediately after a network failure", async () => {
    window.localStorage.setItem("generation.batchId", "batch-1");
    vi.mocked(api.getGenerationBatch)
      .mockRejectedValueOnce(new Error("temporary network failure"))
      .mockResolvedValueOnce(
        batch({ tasks: [task({ result_asset_id: null })] }),
      );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "立即刷新" }));
    await waitFor(() =>
      expect(api.getGenerationBatch).toHaveBeenCalledTimes(2),
    );
    expect(screen.queryByText(/网络连接失败/)).not.toBeInTheDocument();
  });

  it("plays only through a signed archive preview and ignores provider internals", async () => {
    vi.mocked(api.getGenerationBatch).mockResolvedValue(
      batch({
        tasks: [
          task({
            provider: "metaso",
          }),
          task({
            id: "task-audio-failed",
            result_asset_id: "asset-audio-failed",
          }),
        ],
      }),
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );

    const video = await screen.findByLabelText("结果预览 task-ok");
    expect(video).toHaveAttribute("src", "https://stage-preview/asset-ok");
    expect(api.createGenerationResultPreviewUrl).toHaveBeenCalledWith(
      "asset-ok",
    );
  });

  it("shows a provider result while the task is preparing playback", async () => {
    vi.mocked(api.getGenerationBatch).mockResolvedValue(
      batch({
        tasks: [
          task({
            status: "ARCHIVING",
            archive_status: "ARCHIVING",
            stage: "ARCHIVING",
            result_asset_id: null,
            direct_result_available: true,
          }),
        ],
      }),
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );

    const video = await screen.findByLabelText("结果预览 task-ok");
    expect(video).toHaveAttribute("src", "https://provider-preview/task-ok");
    expect(api.createGenerationTaskPreviewUrl).toHaveBeenCalledWith("task-ok");
    expect(api.createGenerationResultPreviewUrl).not.toHaveBeenCalled();
  });

  it("lets the user renew an expired archive preview", async () => {
    vi.mocked(api.getGenerationBatch).mockResolvedValue(
      batch({
        tasks: [
          task({
            provider: "metaso",
          }),
          task({ id: "task-audio-failed" }),
        ],
      }),
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );

    const video = await screen.findByLabelText("结果预览 task-ok");
    expect(video).toHaveAttribute("src", "https://stage-preview/asset-ok");

    // 归档预览本身过期时，不保留 0:00 / 0:00 黑屏，改为给出恢复动作。
    fireEvent.error(video);
    expect(await screen.findByText("暂时无法播放")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重新获取播放地址" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "下载原文件" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重新获取播放地址" }));
    await waitFor(() =>
      expect(api.createGenerationResultPreviewUrl).toHaveBeenCalledTimes(2),
    );
  });

  it("tolerates a streaming preview response that finishes after the panel unmounts", async () => {
    let resolvePreview: (url: string) => void = () => undefined;
    const pendingPreview = new Promise<string>((resolve) => {
      resolvePreview = resolve;
    });
    vi.mocked(api.createGenerationResultPreviewUrl).mockReset();
    vi.mocked(api.createGenerationResultPreviewUrl).mockReturnValue(
      pendingPreview,
    );

    const { unmount } = render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );
    await waitFor(() =>
      expect(api.createGenerationResultPreviewUrl).toHaveBeenCalledWith(
        "asset-ok",
      ),
    );

    // 预签名 URL 由服务端管理过期，卸载后晚到的响应只是被丢弃。
    unmount();
    await act(async () => {
      resolvePreview("https://late-stage-preview");
      await pendingPreview;
    });
  });

  it("appends cursor pages without duplicating an existing batch", async () => {
    vi.mocked(api.listGenerationBatches)
      .mockResolvedValueOnce({ items: [listItem()], next_cursor: "cursor-2" })
      .mockResolvedValueOnce({
        items: [
          listItem(),
          listItem({
            id: "batch-2",
            project_id: "project-2",
            project_name: "庭院改造复刻",
          }),
        ],
        next_cursor: null,
      });

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "加载更多任务记录" }),
    );

    await waitFor(() =>
      expect(api.listGenerationBatches).toHaveBeenNthCalledWith(2, {
        limit: 20,
        cursor: "cursor-2",
      }),
    );
    expect(
      screen.getAllByRole("button", { name: "打开批次 batch-1" }),
    ).toHaveLength(1);
    expect(
      screen.getByRole("button", { name: "打开批次 batch-2" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "加载更多任务记录" }),
    ).not.toBeInTheDocument();
  });

  it("renames a batch inline from the history list", async () => {
    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );

    expect(await screen.findByText("夏日咖啡馆口播复刻")).toBeInTheDocument();

    // 取消编辑不触发请求，回到正常卡片。
    fireEvent.click(screen.getByRole("button", { name: "重命名" }));
    expect(screen.getByLabelText("修改批次名称")).toHaveValue(
      "夏日咖啡馆口播复刻",
    );
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByLabelText("修改批次名称")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "重命名" }));
    fireEvent.change(screen.getByLabelText("修改批次名称"), {
      target: { value: "爆款 v2 批次" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(api.renameGenerationBatch).toHaveBeenCalledWith(
        "batch-1",
        "爆款 v2 批次",
      ),
    );
    // 列表卡优先显示 display_name，改名后立刻生效。
    expect(await screen.findAllByText("爆款 v2 批次")).toHaveLength(2);
    expect(screen.queryByText("夏日咖啡馆口播复刻")).toBeNull();
  });

  it("deletes a batch after explicit confirmation and switches selection", async () => {
    const confirmSpy = vi.spyOn(window, "confirm");
    vi.mocked(api.listGenerationBatches).mockResolvedValue({
      items: [
        listItem(),
        listItem({
          id: "batch-2",
          project_id: "project-2",
          project_name: "庭院改造复刻",
          needs_attention_count: 0,
        }),
      ],
      next_cursor: null,
    });

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );

    expect(await screen.findByText("夏日咖啡馆口播复刻")).toBeInTheDocument();

    const deleteButton = screen.getByRole("button", {
      name: "删除批次 夏日咖啡馆口播复刻",
    });

    // 删除仅影响账号列表，后台生成、费用与进行中任务都保留。
    confirmSpy.mockReturnValueOnce(false);
    fireEvent.click(deleteButton);
    expect(confirmSpy).toHaveBeenCalledWith(
      expect.stringContaining("仅从本账号任务列表移除"),
    );
    expect(confirmSpy.mock.calls[0]?.[0]).toContain("后台生成和费用记录保留");
    expect(confirmSpy.mock.calls[0]?.[0]).toContain("不会取消正在进行的任务");
    expect(api.deleteGenerationBatch).not.toHaveBeenCalled();

    confirmSpy.mockReturnValueOnce(true);
    fireEvent.click(deleteButton);

    await waitFor(() =>
      expect(api.deleteGenerationBatch).toHaveBeenCalledWith("batch-1"),
    );
    await waitFor(() =>
      expect(screen.queryByText("夏日咖啡馆口播复刻")).toBeNull(),
    );
    // 删除的是当前选中批次：自动切换到剩余首个批次。
    await waitFor(() =>
      expect(api.getGenerationBatch).toHaveBeenCalledWith("batch-2"),
    );
    expect(screen.getByText("庭院改造复刻")).toBeInTheDocument();
    vi.mocked(api.listGenerationBatches).mockResolvedValue({
      items: [listItem({ id: "batch-2", project_name: "庭院改造复刻" })],
      next_cursor: null,
    });
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    await waitFor(() =>
      expect(api.listGenerationBatches).toHaveBeenCalledTimes(2),
    );
    expect(
      screen.queryByRole("button", { name: "打开批次 batch-1" }),
    ).not.toBeInTheDocument();
    // 等待新批次的舞台自动签发走完，避免异步链泄漏进下一个用例。
    await waitFor(() =>
      expect(api.createGenerationResultPreviewUrl).toHaveBeenCalledWith(
        "asset-ok",
      ),
    );
  });

  it("ignores an old account's late delete response after the account changes", async () => {
    let finishDelete: (() => void) | undefined;
    vi.mocked(api.deleteGenerationBatch).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishDelete = resolve;
        }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const props = {
      handoffBatch: null,
      onHandoffConsumed: vi.fn(),
      userRole: "customer" as const,
    };
    const { rerender } = render(
      <TaskRecordsPanel {...props} currentUserId="user-a" />,
    );
    fireEvent.click(
      await screen.findByRole("button", {
        name: "删除批次 夏日咖啡馆口播复刻",
      }),
    );
    vi.mocked(api.listGenerationBatches).mockResolvedValue({
      items: [listItem({ id: "user-b-batch", project_name: "乙账号项目" })],
      next_cursor: null,
    });
    rerender(<TaskRecordsPanel {...props} currentUserId="user-b" />);
    await screen.findByRole("button", { name: "打开批次 user-b-batch" });
    await act(async () => finishDelete?.());
    expect(
      screen.getByRole("button", { name: "打开批次 user-b-batch" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("还没有任务记录")).not.toBeInTheDocument();
    expect(api.deleteGenerationBatch).toHaveBeenCalledTimes(1);
  });

  it("removes the last running record without cancelling it or restoring an older list response", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const running = batch({
      quantity: 1,
      status: "RUNNING",
      tasks: [
        task({
          status: "RUNNING",
          stage: "RUNNING",
          result_asset_id: null,
          archive_status: "PENDING",
        }),
      ],
    });
    vi.mocked(api.getGenerationBatch).mockResolvedValue(running);
    vi.mocked(api.listGenerationBatches).mockResolvedValueOnce({
      items: [listItem({ status: "RUNNING", tasks: running.tasks })],
      next_cursor: null,
    });
    let finishHistory:
      | ((page: api.GenerationBatchListPage) => void)
      | undefined;
    vi.mocked(api.listGenerationBatches).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishHistory = resolve;
        }),
    );
    render(
      <TaskRecordsPanel
        currentUserId="user-a"
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="customer"
      />,
    );
    const remove = await screen.findByRole("button", {
      name: "删除批次 夏日咖啡馆口播复刻",
    });
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    fireEvent.click(remove);
    await screen.findByRole("heading", { name: "还没有任务记录" });
    await act(async () =>
      finishHistory?.({ items: [listItem()], next_cursor: null }),
    );
    expect(
      screen.queryByRole("button", { name: "打开批次 batch-1" }),
    ).not.toBeInTheDocument();
    expect(window.localStorage.getItem("generation.batchId:user-a")).toBeNull();
    expect(api.deleteGenerationBatch).toHaveBeenCalledWith("batch-1");
    expect(api.regenerateGenerationBatch).not.toHaveBeenCalled();
    expect(api.retryGenerationTask).not.toHaveBeenCalled();
    expect(api.reconcileUncertainTask).not.toHaveBeenCalled();
  });

  it("keeps result metadata readable for auditors without preview, download, or reconcile actions", async () => {
    vi.mocked(api.getGenerationBatch).mockResolvedValue(
      batch({
        tasks: [
          task({
            status: "SUBMISSION_UNCERTAIN",
            stage: "SUBMISSION_UNCERTAIN",
            available_actions: ["RECONCILE"],
          }),
        ],
      }),
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="auditor"
      />,
    );

    expect(
      await screen.findByText("审计只读，不可预览或下载结果"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /预览/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /下载 MP4/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /对账/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /安全重试|重试归档|确认未计费/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /付费重新生成|整批付费再次生成/ }),
    ).not.toBeInTheDocument();
    // 审计身份同样看不到批次卡的重命名/删除操作。
    expect(
      screen.queryByRole("button", { name: "重命名" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "删除批次 夏日咖啡馆口播复刻" }),
    ).not.toBeInTheDocument();
    expect(api.createGenerationResultPreviewUrl).not.toHaveBeenCalled();
  });

  it("requires an explicit reason and payment confirmation before regenerating a frozen batch", async () => {
    vi.mocked(api.getGenerationBatch).mockImplementation(async (batchId) =>
      batchId === "batch-regenerated"
        ? batch({
            id: "batch-regenerated",
            source_batch_id: "batch-1",
            generation_reason: "再生成一批备选",
          })
        : batch(),
    );
    vi.mocked(api.regenerateGenerationBatch).mockResolvedValue(
      batch({
        id: "batch-regenerated",
        source_batch_id: "batch-1",
        generation_reason: "再生成一批备选",
      }),
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );
    await switchToOpsView();

    const regenerate = await screen.findByRole("button", {
      name: "整批付费再次生成",
    });
    expect(regenerate).toBeDisabled();
    fireEvent.change(screen.getByLabelText("整批重生成原因"), {
      target: { value: "再生成一批备选" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", { name: "确认新建 2 个付费任务" }),
    );
    fireEvent.click(regenerate);

    await waitFor(() =>
      expect(api.regenerateGenerationBatch).toHaveBeenCalledWith("batch-1", {
        idempotency_key: expect.any(String),
        estimated_cost_snapshot: 2.5,
        generation_reason: "再生成一批备选",
      }),
    );
    expect(await screen.findByText("来源批次 batch-1")).toBeInTheDocument();
  });

  it("reuses the same paid task idempotency key after a failed response", async () => {
    vi.mocked(api.getGenerationBatch).mockResolvedValue(
      batch({
        quantity: 1,
        progress: {
          total_count: 1,
          terminal_count: 1,
          progress_percent: 100,
          counts: {
            pending: 0,
            submitting: 0,
            queued: 0,
            running: 0,
            archiving: 0,
            succeeded: 1,
            failed: 0,
            cancelled: 0,
            needs_attention: 1,
          },
        },
        tasks: [
          task({
            id: "task-paid-regenerate",
            quality_status: "AUDIO_QUALITY_FAILED",
            quality_issue_codes: ["AUDIO_QUALITY_FAILED"],
            available_actions: ["REGENERATE"],
          }),
        ],
      }),
    );
    vi.mocked(api.regenerateGenerationTask)
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(
        batch({
          id: "batch-task-regenerated",
          quantity: 1,
          source_batch_id: "batch-1",
          source_task_id: "task-paid-regenerate",
        }),
      );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );
    await switchToOpsView();

    const regenerate = await screen.findByRole("button", {
      name: "付费重新生成 task-paid-regenerate",
    });
    fireEvent.change(
      screen.getByLabelText("重新生成原因 task-paid-regenerate"),
      { target: { value: "音频质检失败后重新生成" } },
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "确认为任务 task-paid-regenerate 新增一次付费生成",
      }),
    );
    fireEvent.click(regenerate);
    expect(
      await screen.findByText("付费重新生成失败，已保留本次请求供重试。"),
    ).toBeInTheDocument();
    fireEvent.click(regenerate);

    await waitFor(() =>
      expect(api.regenerateGenerationTask).toHaveBeenCalledTimes(2),
    );
    const firstRequest = vi.mocked(api.regenerateGenerationTask).mock
      .calls[0][1];
    const replayRequest = vi.mocked(api.regenerateGenerationTask).mock
      .calls[1][1];
    expect(replayRequest.idempotency_key).toBe(firstRequest.idempotency_key);
    expect(firstRequest).toEqual({
      idempotency_key: expect.any(String),
      estimated_cost_snapshot: 1.25,
      generation_reason: "音频质检失败后重新生成",
    });
  });

  it("keeps superseded quality failures visible as history without active attention", async () => {
    vi.mocked(api.getGenerationBatch).mockResolvedValue(
      batch({
        status: "COMPLETED_WITH_FAILURES",
        quantity: 1,
        progress: {
          total_count: 1,
          terminal_count: 1,
          progress_percent: 100,
          counts: {
            pending: 0,
            submitting: 0,
            queued: 0,
            running: 0,
            archiving: 0,
            succeeded: 1,
            failed: 0,
            cancelled: 0,
            needs_attention: 0,
          },
          historical_counts: {
            archive_failed: 0,
            audio_quality_failed: 1,
            failed: 0,
            superseded: 1,
          },
        },
        tasks: [
          task({
            id: "historical-audio-failure",
            quality_status: "AUDIO_QUALITY_FAILED",
            quality_issue_codes: ["AUDIO_QUALITY_FAILED"],
            superseded_by_task_id: "task-replacement",
            superseded_at: "2026-08-16 11:00:00",
            available_actions: [],
          }),
        ],
      }),
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );
    await switchToOpsView();

    expect(
      screen.queryByText(/历史事实：失败 0 · 归档失败 0 · 音频质检失败 1/),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(/已由任务 task-replacement 替代/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: "付费重新生成 historical-audio-failure",
      }),
    ).not.toBeInTheDocument();
  });

  it("does not describe pending quality checks as passed", async () => {
    vi.mocked(api.getGenerationBatch).mockResolvedValue(
      batch({
        tasks: [
          task({
            id: "task-pending-quality",
            quality_status: "PENDING",
            result_asset_id: null,
            stage: "RUNNING",
            status: "RUNNING",
          }),
        ],
      }),
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );
    await switchToOpsView();

    expect(screen.queryByText(/质检|检查结果尚未返回/)).not.toBeInTheDocument();
    expect(
      screen.queryByText("音频正常，结果可进入人工确认。"),
    ).not.toBeInTheDocument();
  });

  it("does not write a completed reconcile response into a newly selected batch", async () => {
    let finishReconcile:
      | ((value: api.GenerationReconcileOperation) => void)
      | undefined;
    vi.mocked(api.reconcileUncertainTask).mockReturnValue(
      new Promise((resolve) => {
        finishReconcile = resolve;
      }),
    );
    vi.mocked(api.listGenerationBatches).mockResolvedValue({
      items: [
        listItem({
          tasks: [
            task({
              id: "task-a",
              status: "SUBMISSION_UNCERTAIN",
              stage: "SUBMISSION_UNCERTAIN",
              available_actions: ["RECONCILE"],
            }),
          ],
        }),
        listItem({
          id: "batch-2",
          project_id: "project-2",
          project_name: "庭院改造复刻",
          tasks: [task({ id: "task-b" })],
        }),
      ],
      next_cursor: null,
    });
    vi.mocked(api.getGenerationBatch).mockImplementation(async (batchId) =>
      batchId === "batch-2"
        ? batch({ id: "batch-2", tasks: [task({ id: "task-b" })] })
        : batch({
            tasks: [
              task({
                id: "task-a",
                status: "SUBMISSION_UNCERTAIN",
                stage: "SUBMISSION_UNCERTAIN",
                available_actions: ["RECONCILE"],
              }),
            ],
          }),
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );
    await switchToOpsView();

    fireEvent.click(await screen.findByRole("button", { name: "对账 task-a" }));
    fireEvent.click(screen.getByRole("button", { name: "打开批次 batch-2" }));
    expect(await screen.findByText("task-b")).toBeInTheDocument();

    await act(async () => {
      finishReconcile?.(
        reconcileOperation({
          id: "reconcile-task-a",
          task_id: "task-a",
        }),
      );
      await Promise.resolve();
    });

    expect(api.reconcileUncertainTask).toHaveBeenCalledWith(
      "task-a",
      expect.objectContaining({ idempotency_key: expect.any(String) }),
    );
    expect(screen.getByText("task-b")).toBeInTheDocument();
    expect(screen.queryByText("task-a")).not.toBeInTheDocument();
  });

  it("releases the reconcile action after durable enqueue while the worker continues", async () => {
    const uncertainBatch = batch({
      tasks: [
        task({
          id: "task-background-reconcile",
          status: "SUBMISSION_UNCERTAIN",
          stage: "SUBMISSION_UNCERTAIN",
          available_actions: ["RECONCILE"],
        }),
      ],
    });
    vi.mocked(api.listGenerationBatches).mockResolvedValue({
      items: [listItem({ tasks: uncertainBatch.tasks })],
      next_cursor: null,
    });
    vi.mocked(api.getGenerationBatch).mockResolvedValue(uncertainBatch);
    vi.mocked(api.reconcileUncertainTask).mockResolvedValue(
      reconcileOperation({
        id: "background-reconcile-operation",
        task_id: "task-background-reconcile",
      }),
    );
    vi.mocked(api.waitForGenerationReconcileOperation).mockReturnValue(
      new Promise(() => undefined),
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );
    await switchToOpsView();
    fireEvent.click(
      await screen.findByRole("button", {
        name: "对账 task-background-reconcile",
      }),
    );

    await waitFor(() =>
      expect(api.waitForGenerationReconcileOperation).toHaveBeenCalledWith(
        "background-reconcile-operation",
      ),
    );
    expect(
      screen.getByRole("button", { name: "对账 task-background-reconcile" }),
    ).toBeEnabled();
    expect(
      screen.getByText("任务已进入后台对账，可离开本页继续其他操作。"),
    ).toBeInTheDocument();
  });

  it("重新进入任务页时恢复未完成的后台对账", async () => {
    const uncertainBatch = batch({
      tasks: [
        task({
          id: "task-reconcile-recovery",
          status: "SUBMISSION_UNCERTAIN",
          stage: "SUBMISSION_UNCERTAIN",
          available_actions: ["RECONCILE"],
        }),
      ],
    });
    vi.mocked(api.listGenerationBatches).mockResolvedValue({
      items: [listItem({ tasks: uncertainBatch.tasks })],
      next_cursor: null,
    });
    vi.mocked(api.getGenerationBatch).mockResolvedValue(uncertainBatch);
    vi.mocked(api.getLatestGenerationReconcileOperation).mockResolvedValue(
      reconcileOperation({
        id: "recovered-reconcile-operation",
        task_id: "task-reconcile-recovery",
        status: "RUNNING",
      }),
    );
    vi.mocked(api.waitForGenerationReconcileOperation).mockResolvedValue(
      reconcileOperation({
        id: "recovered-reconcile-operation",
        task_id: "task-reconcile-recovery",
        status: "SUCCEEDED",
      }),
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );

    await waitFor(() =>
      expect(api.getLatestGenerationReconcileOperation).toHaveBeenCalledWith(
        "task-reconcile-recovery",
      ),
    );
    await waitFor(() =>
      expect(api.waitForGenerationReconcileOperation).toHaveBeenCalledWith(
        "recovered-reconcile-operation",
      ),
    );
    await waitFor(() =>
      expect(api.getGenerationBatch).toHaveBeenCalledTimes(2),
    );
  });

  it("restarts polling after a safe retry requeues a terminal task", async () => {
    const failed = batch({
      status: "COMPLETED_WITH_FAILURES",
      quantity: 1,
      progress: {
        total_count: 1,
        terminal_count: 1,
        progress_percent: 100,
        counts: {
          pending: 0,
          submitting: 0,
          queued: 0,
          running: 0,
          archiving: 0,
          succeeded: 0,
          failed: 1,
          cancelled: 0,
          needs_attention: 0,
        },
      },
      tasks: [
        task({
          id: "task-retry-poll",
          status: "FAILED",
          stage: "FAILED",
          archive_status: "PENDING",
          quality_status: "PENDING",
          result_asset_id: null,
          available_actions: ["RETRY"],
        }),
      ],
    });
    const requeued = batch({
      status: "QUEUED",
      quantity: 1,
      progress: {
        total_count: 1,
        terminal_count: 0,
        progress_percent: 0,
        counts: {
          pending: 1,
          submitting: 0,
          queued: 0,
          running: 0,
          archiving: 0,
          succeeded: 0,
          failed: 0,
          cancelled: 0,
          needs_attention: 0,
        },
      },
      tasks: [
        task({
          id: "task-retry-poll",
          status: "PENDING",
          stage: "PENDING",
          archive_status: "PENDING",
          quality_status: "PENDING",
          result_asset_id: null,
        }),
      ],
    });
    const completed = batch({
      status: "SUCCEEDED",
      quantity: 1,
      progress: {
        total_count: 1,
        terminal_count: 1,
        progress_percent: 100,
        counts: {
          pending: 0,
          submitting: 0,
          queued: 0,
          running: 0,
          archiving: 0,
          succeeded: 1,
          failed: 0,
          cancelled: 0,
          needs_attention: 0,
        },
      },
      tasks: [task({ id: "task-retry-poll", stage: "COMPLETED" })],
    });
    vi.mocked(api.getGenerationBatch)
      .mockResolvedValueOnce(failed)
      .mockResolvedValueOnce(requeued)
      .mockResolvedValueOnce(completed);
    vi.mocked(api.retryGenerationTask).mockResolvedValue(requeued.tasks[0]);

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );
    await switchToOpsView();

    const retry = await screen.findByRole("button", {
      name: "安全重试 task-retry-poll",
    });
    fireEvent.change(screen.getByLabelText("处理原因 task-retry-poll"), {
      target: { value: "本地签名服务已恢复" },
    });
    fireEvent.click(retry);

    await waitFor(() =>
      expect(api.getGenerationBatch).toHaveBeenCalledTimes(3),
    );
    expect(await screen.findByText("阶段：已完成")).toBeInTheDocument();
  });

  it("restarts polling after an admin confirms an uncertain task was not charged", async () => {
    const uncertain = batch({
      status: "NEEDS_ATTENTION",
      quantity: 1,
      progress: {
        total_count: 1,
        terminal_count: 0,
        progress_percent: 0,
        counts: {
          pending: 0,
          submitting: 0,
          queued: 0,
          running: 0,
          archiving: 0,
          succeeded: 0,
          failed: 0,
          cancelled: 0,
          needs_attention: 1,
        },
      },
      tasks: [
        task({
          id: "task-confirm-poll",
          status: "SUBMISSION_UNCERTAIN",
          stage: "SUBMISSION_UNCERTAIN",
          archive_status: "PENDING",
          quality_status: "PENDING",
          result_asset_id: null,
          available_actions: ["CONFIRM_NOT_CHARGED"],
        }),
      ],
    });
    const requeued = batch({
      status: "QUEUED",
      quantity: 1,
      progress: {
        total_count: 1,
        terminal_count: 0,
        progress_percent: 0,
        counts: {
          pending: 1,
          submitting: 0,
          queued: 0,
          running: 0,
          archiving: 0,
          succeeded: 0,
          failed: 0,
          cancelled: 0,
          needs_attention: 0,
        },
      },
      tasks: [
        task({
          id: "task-confirm-poll",
          status: "PENDING",
          stage: "PENDING",
          archive_status: "PENDING",
          quality_status: "PENDING",
          result_asset_id: null,
        }),
      ],
    });
    const completed = batch({
      status: "SUCCEEDED",
      quantity: 1,
      progress: {
        total_count: 1,
        terminal_count: 1,
        progress_percent: 100,
        counts: {
          pending: 0,
          submitting: 0,
          queued: 0,
          running: 0,
          archiving: 0,
          succeeded: 1,
          failed: 0,
          cancelled: 0,
          needs_attention: 0,
        },
      },
      tasks: [task({ id: "task-confirm-poll", stage: "COMPLETED" })],
    });
    vi.mocked(api.getGenerationBatch)
      .mockResolvedValueOnce(uncertain)
      .mockResolvedValueOnce(requeued)
      .mockResolvedValueOnce(completed);
    vi.mocked(api.confirmGenerationTaskNotCharged).mockResolvedValue(
      requeued.tasks[0],
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="admin"
      />,
    );
    await switchToOpsView();

    const confirm = await screen.findByRole("button", {
      name: "确认未计费 task-confirm-poll",
    });
    fireEvent.change(screen.getByLabelText("处理原因 task-confirm-poll"), {
      target: { value: "账单已确认未计费" },
    });
    fireEvent.click(confirm);

    await waitFor(() =>
      expect(api.getGenerationBatch).toHaveBeenCalledTimes(3),
    );
    expect(await screen.findByText("阶段：已完成")).toBeInTheDocument();
  });

  it("offers only server-approved retry, reconcile, and admin confirmation actions", async () => {
    vi.mocked(api.getGenerationBatch).mockResolvedValue(
      batch({
        quantity: 3,
        progress: {
          total_count: 3,
          terminal_count: 1,
          progress_percent: 33,
          counts: {
            pending: 0,
            submitting: 0,
            queued: 0,
            running: 0,
            archiving: 0,
            succeeded: 0,
            failed: 0,
            cancelled: 0,
            needs_attention: 3,
          },
        },
        tasks: [
          task({
            id: "task-archive",
            archive_status: "ARCHIVE_FAILED",
            stage: "ARCHIVE_FAILED",
            result_asset_id: null,
            available_actions: ["RETRY"],
          }),
          task({
            id: "task-reconcile",
            status: "SUBMISSION_UNCERTAIN",
            stage: "SUBMISSION_UNCERTAIN",
            result_asset_id: null,
            available_actions: ["RECONCILE"],
          }),
          task({
            id: "task-confirm",
            status: "SUBMISSION_UNCERTAIN",
            stage: "SUBMISSION_UNCERTAIN",
            result_asset_id: null,
            available_actions: ["CONFIRM_NOT_CHARGED"],
          }),
        ],
      }),
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="admin"
      />,
    );
    await switchToOpsView();

    const retryButton = await screen.findByRole("button", {
      name: "重试归档 task-archive",
    });
    expect(retryButton).toBeDisabled();
    fireEvent.change(screen.getByLabelText("处理原因 task-archive"), {
      target: { value: "对象存储已恢复" },
    });
    fireEvent.click(retryButton);
    await waitFor(() =>
      expect(api.retryGenerationTask).toHaveBeenCalledWith(
        "task-archive",
        expect.objectContaining({
          idempotency_key: expect.any(String),
          retry_reason: "对象存储已恢复",
        }),
      ),
    );

    fireEvent.click(
      screen.getByRole("button", { name: "对账 task-reconcile" }),
    );
    await waitFor(() =>
      expect(api.reconcileUncertainTask).toHaveBeenCalledWith(
        "task-reconcile",
        expect.objectContaining({ idempotency_key: expect.any(String) }),
      ),
    );

    const confirmButton = screen.getByRole("button", {
      name: "确认未计费 task-confirm",
    });
    expect(confirmButton).toBeDisabled();
    fireEvent.change(screen.getByLabelText("处理原因 task-confirm"), {
      target: { value: "供应商账单已核对" },
    });
    fireEvent.click(confirmButton);
    await waitFor(() =>
      expect(api.confirmGenerationTaskNotCharged).toHaveBeenCalledWith(
        "task-confirm",
        expect.objectContaining({
          idempotency_key: expect.any(String),
          reason: "供应商账单已核对",
        }),
      ),
    );
  });

  it("renders the ops detail with an overview bar, fact table, and collapsed controls", async () => {
    vi.mocked(api.getGenerationBatch).mockResolvedValue(
      batch({
        status: "SUCCEEDED",
        quantity: 1,
        progress: {
          total_count: 1,
          terminal_count: 1,
          progress_percent: 100,
          counts: {
            pending: 0,
            submitting: 0,
            queued: 0,
            running: 0,
            archiving: 0,
            succeeded: 1,
            failed: 0,
            cancelled: 0,
            needs_attention: 0,
          },
        },
        tasks: [
          task({
            id: "task-ops-layout",
            prompt_snapshot: {
              status: "LOCKED",
              resolution: "1080x1920",
              output_duration_seconds: 15,
            },
            available_actions: ["RECONCILE", "REGENERATE"],
          }),
        ],
      }),
    );

    render(
      <TaskRecordsPanel
        handoffBatch={null}
        onHandoffConsumed={vi.fn()}
        userRole="employee"
      />,
    );
    await switchToOpsView();

    // 概览条：状态徽章、进度百分比与完成计数同层呈现。
    expect(await screen.findByText("100%")).toBeInTheDocument();
    expect(screen.getByText("任务已结束 1 / 1")).toBeInTheDocument();
    expect(screen.getByText("成功 1")).toBeInTheDocument();

    // 左栏事实表：快照解析出的分辨率与成片时长与模型、费用并列。
    expect(screen.getByText("生成能力")).toBeInTheDocument();
    expect(screen.getByText("1080x1920")).toBeInTheDocument();
    expect(screen.getByText("成片时长")).toBeInTheDocument();
    expect(screen.getByText("15 秒")).toBeInTheDocument();
    expect(screen.getByText("提交时间")).toBeInTheDocument();
    expect(screen.getByText("2026/8/16 18:00:00")).toBeInTheDocument();

    // 右栏操作区默认收起，展开后呈现对账与付费重生成入口。
    const resolutionControls = screen
      .getByText("处理此任务", { selector: "summary" })
      .closest("details");
    expect(resolutionControls).not.toHaveAttribute("open");
    const paidRegeneration = screen
      .getByText("付费重新生成", { selector: "summary" })
      .closest("details");
    expect(paidRegeneration).not.toHaveAttribute("open");

    fireEvent.click(screen.getByText("处理此任务", { selector: "summary" }));
    expect(
      screen.getByRole("button", { name: "对账 task-ops-layout" }),
    ).toBeInTheDocument();
  });
});
