import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  cancelSourceFrameTask,
  confirmSourceFrame,
  extractSourceFrames,
  getAssetDownloadUrl,
  getLatestProjectSourceFrameSelection,
  getLatestProjectSourceFrames,
  getLatestProjectSourceFrameTask,
  type SourceFrameTask,
  waitForSourceFrameTask,
} from "./api";
import { SourceFrameSelection } from "./SourceFrameSelection";

vi.mock("./api", () => ({
  cancelSourceFrameTask: vi.fn(),
  confirmSourceFrame: vi.fn(),
  extractSourceFrames: vi.fn(),
  getAssetDownloadUrl: vi.fn(),
  getLatestProjectSourceFrameTask: vi.fn(),
  getLatestProjectSourceFrameSelection: vi.fn(),
  getLatestProjectSourceFrames: vi.fn(),
  readSourceFrameCandidates: vi.fn((version) => version.payload),
  waitForSourceFrameTask: vi.fn(),
}));

const candidatesVersion = {
  id: "source-candidates-1",
  project_id: "project-1",
  asset_id: "reference-1",
  kind: "source_frame_candidates",
  version_number: 1,
  payload: {
    requested_timestamps_seconds: [0.5, 1.5, 2.5],
    semantic_quality_status: "VERIFIED",
    candidates: [
      { asset_id: "source-1", timestamp_seconds: 1.5, score: 0.83 },
      { asset_id: "source-2", timestamp_seconds: 0.5, score: 0.52 },
    ],
  },
  created_by_user_id: "employee_1",
  created_at: "2030-01-01T00:00:00Z",
};

const sourceFrameTask = {
  id: "source-frame-task-1",
  project_id: "project-1",
  asset_id: "reference-1",
  timestamps_seconds: [0.5, 1.5, 2.5],
  status: "PENDING" as const,
  attempt: 0,
  result_version_id: null,
  error_code: null,
  error_message: null,
  retryable: false,
  created_at: "2030-01-01T00:00:00Z",
  updated_at: "2030-01-01T00:00:00Z",
  started_at: null,
  completed_at: null,
};

describe("SourceFrameSelection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getLatestProjectSourceFrames).mockResolvedValue(
      candidatesVersion,
    );
    vi.mocked(getLatestProjectSourceFrameSelection).mockResolvedValue({
      version: null,
      stale: false,
    });
    vi.mocked(getLatestProjectSourceFrameTask).mockResolvedValue(null);
    vi.mocked(getAssetDownloadUrl).mockImplementation(async (assetId) => ({
      url: `https://private.example/${assetId}.jpg`,
    }));
    vi.mocked(confirmSourceFrame).mockResolvedValue({
      ...candidatesVersion,
      id: "source-selection-1",
      kind: "source_frame_selection",
      payload: {
        source_frame_asset_id: "source-1",
        character_features: {
          orientation: "FRONT",
          shot_size: "HALF_BODY",
          face_visible: true,
          body_completeness: "UPPER_BODY",
        },
      },
    });
    vi.mocked(extractSourceFrames).mockResolvedValue(sourceFrameTask);
    vi.mocked(cancelSourceFrameTask).mockResolvedValue({
      ...sourceFrameTask,
      status: "FAILED",
      error_code: "SOURCE_FRAME_TASK_CANCELLED",
      error_message: "取帧任务已停止，可以重新开始。",
      retryable: true,
    });
    vi.mocked(waitForSourceFrameTask).mockResolvedValue({
      ...sourceFrameTask,
      status: "SUCCEEDED",
      result_version_id: candidatesVersion.id,
    });
  });

  it("recommends the best candidate but requires the user to confirm it", async () => {
    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    expect(await screen.findByText("原视频画面")).toBeInTheDocument();
    expect(
      await screen.findByText("已推荐画面 1，请确认或更换源画面。"),
    ).toBeInTheDocument();
    expect(confirmSourceFrame).not.toHaveBeenCalled();
    expect(screen.getByAltText("候选源画面 1")).toHaveAttribute(
      "src",
      "https://private.example/source-1.jpg",
    );
    expect(screen.queryByText(/技术画质参考/)).toBeNull();
    expect(screen.queryByLabelText("人物朝向")).toBeNull();
    expect(screen.queryByLabelText("人物景别")).toBeNull();
    expect(screen.queryByLabelText("面部可见性")).toBeNull();
    expect(screen.queryByLabelText("身体完整度")).toBeNull();
    const confirmButton = screen.getByRole("button", {
      name: "使用这张画面",
    });
    await waitFor(() => expect(confirmButton).toBeEnabled());
    fireEvent.click(confirmButton);
    await waitFor(() =>
      expect(confirmSourceFrame).toHaveBeenCalledWith(
        "project-1",
        "source-1",
        null,
      ),
    );
  });

  it("prefers the actual opening frame over a higher-scoring middle frame", async () => {
    vi.mocked(getLatestProjectSourceFrames).mockResolvedValue({
      ...candidatesVersion,
      payload: {
        ...candidatesVersion.payload,
        candidates: [
          { asset_id: "source-1", timestamp_seconds: 6, score: 0.95 },
          { asset_id: "opening", timestamp_seconds: 0, score: 0.65 },
        ],
      },
    });
    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );
    await waitFor(() =>
      expect(screen.getByDisplayValue("opening")).toBeChecked(),
    );
    expect(confirmSourceFrame).not.toHaveBeenCalled();
  });

  it("keeps the chosen source visible and confirmable with compact alternatives collapsed", async () => {
    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
        simplified
      />,
    );
    expect(await screen.findByAltText("当前原画面")).toBeVisible();
    const alternatives = screen.getByText("查看或更换源画面");
    expect(alternatives.closest("details")).not.toHaveAttribute("open");
    const confirm = screen.getByRole("button", { name: "使用这张画面" });
    expect(confirm).toBeVisible();
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(confirmSourceFrame).toHaveBeenCalledWith(
        "project-1",
        "source-1",
        null,
      ),
    );
    expect(screen.getByAltText("当前原画面")).toBeVisible();
    fireEvent.click(alternatives);
    fireEvent.click(screen.getByDisplayValue("source-2"));
    expect(screen.getByText("待手动确认")).toBeVisible();
    expect(screen.getByAltText("当前原画面")).toHaveAttribute(
      "src",
      "https://private.example/source-2.jpg",
    );
    expect(confirmSourceFrame).toHaveBeenCalledTimes(1);
  });

  it("places the current frame beside the alternatives in the replica row layout", async () => {
    render(
      <SourceFrameSelection
        candidatesAlwaysVisible
        projectId="project-1"
        referenceAssetId="reference-1"
        simplified
      />,
    );
    const currentFrame = await screen.findByAltText("当前原画面");
    const alternatives = screen.getByText("查看或更换源画面");
    const row = currentFrame.closest(".source-frame-selection__row");
    expect(row).not.toBeNull();
    expect(row).toContainElement(
      alternatives.closest("details") as HTMLElement,
    );
    const currentColumn = row?.querySelector(
      ".source-frame-selection__current",
    );
    expect(currentColumn).not.toBeNull();
    expect(currentColumn).toContainElement(currentFrame as HTMLElement);
    expect(currentColumn).toContainElement(
      screen.getByRole("button", { name: "使用这张画面" }),
    );
    expect(alternatives.closest("details")).toHaveAttribute("open");
  });

  it("keeps the replica row layout free of the long explanatory copy", async () => {
    render(
      <SourceFrameSelection
        candidatesAlwaysVisible
        projectId="project-1"
        referenceAssetId="reference-1"
        simplified
      />,
    );
    await screen.findByAltText("当前原画面");
    expect(screen.queryByText(/选择人物清晰、无遮挡的画面/)).toBeNull();
    expect(screen.queryByText(/后段画面也能作为人物与构图参考/)).toBeNull();
  });

  it("keeps the descriptive copy in the full-size layout", async () => {
    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );
    expect(
      await screen.findByText(/选择人物清晰、无遮挡的画面/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/后段画面也能作为人物与构图参考/),
    ).toBeInTheDocument();
  });

  it("hides and locks the previous source frame while new inputs are loading", async () => {
    const { rerender } = render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
        simplified
      />,
    );
    expect(await screen.findByAltText("当前原画面")).toBeVisible();
    const pendingVersion = new Promise<typeof candidatesVersion>(
      () => undefined,
    );
    vi.mocked(getLatestProjectSourceFrames).mockReturnValueOnce(pendingVersion);

    rerender(
      <SourceFrameSelection
        projectId="project-2"
        referenceAssetId="reference-2"
        simplified
      />,
    );

    expect(screen.queryByAltText("当前原画面")).toBeNull();
    expect(screen.queryByAltText("候选源画面 1")).toBeNull();
    expect(screen.getByRole("button", { name: "使用这张画面" })).toBeDisabled();
  });

  it("keeps the selected candidate when manual confirmation fails", async () => {
    vi.mocked(confirmSourceFrame).mockRejectedValueOnce(
      new Error("确认暂不可用"),
    );

    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    const useButton = await screen.findByRole("button", {
      name: "使用这张画面",
    });
    await waitFor(() => expect(useButton).toBeEnabled());
    fireEvent.click(useButton);

    expect(await screen.findByText("确认暂不可用")).toBeInTheDocument();
    expect(screen.getByDisplayValue("source-1")).toBeChecked();
    expect(confirmSourceFrame).toHaveBeenCalledOnce();
  });

  it("re-extracts at adaptive timestamps without exposing technical inputs", async () => {
    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
        videoDurationSeconds={12}
      />,
    );

    await screen.findByAltText("候选源画面 1");
    fireEvent.click(screen.getByRole("button", { name: "重新取帧" }));

    await waitFor(() =>
      expect(extractSourceFrames).toHaveBeenCalledWith(
        "project-1",
        "reference-1",
        [0, 3.6, 6, 8.4, 10.8],
      ),
    );
    expect(screen.queryByLabelText("重新取帧时间点（秒）")).toBeNull();
  });

  it("extracts one manually chosen timestamp for human intervention", async () => {
    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
        videoDurationSeconds={12}
      />,
    );

    await screen.findByAltText("候选源画面 1");
    fireEvent.change(screen.getByLabelText("取帧时间（秒）"), {
      target: { value: "4.2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "取出画面" }));

    await waitFor(() =>
      expect(extractSourceFrames).toHaveBeenCalledWith(
        "project-1",
        "reference-1",
        [4.2],
      ),
    );
  });

  it("keeps an unavailable preview out of the manual fallback", async () => {
    vi.mocked(getAssetDownloadUrl).mockRejectedValueOnce(
      new Error("签名 URL 不可用"),
    );
    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    expect(await screen.findByText("预览加载失败")).toBeInTheDocument();
    expect(screen.getByDisplayValue("source-1")).toBeDisabled();
    expect(screen.getByRole("button", { name: "使用这张画面" })).toBeDisabled();
  });

  it("requires manual confirmation when semantic scoring is unavailable", async () => {
    vi.mocked(getLatestProjectSourceFrames).mockResolvedValue({
      ...candidatesVersion,
      payload: {
        ...candidatesVersion.payload,
        semantic_quality_status: "UNAVAILABLE",
      },
    });

    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    expect(
      await screen.findByText("已推荐画面，请查看后确认。"),
    ).toBeInTheDocument();
    expect(confirmSourceFrame).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "使用这张画面" }),
      ).toBeEnabled(),
    );
  });

  it("loads existing candidate previews for a read-only auditor without writing", async () => {
    render(
      <SourceFrameSelection
        projectId="project-1"
        readOnly
        referenceAssetId="reference-1"
      />,
    );

    expect(await screen.findByAltText("候选源画面 1")).toHaveAttribute(
      "src",
      "https://private.example/source-1.jpg",
    );
    expect(getAssetDownloadUrl).toHaveBeenCalledTimes(2);
    expect(extractSourceFrames).not.toHaveBeenCalled();
    expect(confirmSourceFrame).not.toHaveBeenCalled();
  });

  it("accepts a legacy selection without character features", async () => {
    const onSelectionChange = vi.fn();
    vi.mocked(getLatestProjectSourceFrameSelection).mockResolvedValue({
      stale: false,
      version: {
        ...candidatesVersion,
        id: "source-selection-legacy",
        kind: "source_frame_selection",
        payload: { source_frame_asset_id: "source-1" },
      },
    });

    render(
      <SourceFrameSelection
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    expect(
      await screen.findByText("已确认源画面，将保留原视频的构图与动作。"),
    ).toBeInTheDocument();
    expect(onSelectionChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ id: "source-selection-legacy" }),
    );
    expect(confirmSourceFrame).not.toHaveBeenCalled();
  });

  it("locks candidate choice while confirmation is in flight", async () => {
    let resolveConfirmation: (() => void) | undefined;
    vi.mocked(confirmSourceFrame).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveConfirmation = () =>
            resolve({
              ...candidatesVersion,
              id: "source-selection-1",
              kind: "source_frame_selection",
              payload: {
                source_frame_asset_id: "source-1",
                character_features: {
                  orientation: "FRONT",
                  shot_size: "HALF_BODY",
                  face_visible: true,
                  body_completeness: "UPPER_BODY",
                },
              },
            });
        }),
    );
    const onBusyChange = vi.fn();
    render(
      <SourceFrameSelection
        onBusyChange={onBusyChange}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    const confirmButton = await screen.findByRole("button", {
      name: "使用这张画面",
    });
    await waitFor(() => expect(confirmButton).toBeEnabled());
    fireEvent.click(confirmButton);
    await waitFor(() => expect(confirmSourceFrame).toHaveBeenCalledOnce());
    expect(onBusyChange).toHaveBeenLastCalledWith(true);
    expect(screen.getByText("确认中")).toBeInTheDocument();
    expect(screen.getByDisplayValue("source-1")).toBeDisabled();
    expect(screen.getByDisplayValue("source-2")).toBeDisabled();

    resolveConfirmation?.();
    await waitFor(() => expect(onBusyChange).toHaveBeenLastCalledWith(false));
  });

  it("ignores a confirmation response after the project input changes", async () => {
    const savedSelection = {
      ...candidatesVersion,
      id: "source-selection-1",
      kind: "source_frame_selection",
      payload: {
        source_frame_asset_id: "source-1",
        character_features: {
          orientation: "FRONT",
          shot_size: "HALF_BODY",
          face_visible: true,
          body_completeness: "UPPER_BODY",
        },
      },
    };
    let resolveConfirmation:
      | ((selection: typeof savedSelection) => void)
      | undefined;
    const pendingConfirmation = new Promise<typeof savedSelection>(
      (resolve) => {
        resolveConfirmation = resolve;
      },
    );
    vi.mocked(confirmSourceFrame).mockReturnValue(pendingConfirmation);
    const onSelectionChange = vi.fn();
    const onBusyChange = vi.fn();
    const { rerender } = render(
      <SourceFrameSelection
        onBusyChange={onBusyChange}
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    const confirmButton = await screen.findByRole("button", {
      name: "使用这张画面",
    });
    await waitFor(() => expect(confirmButton).toBeEnabled());
    fireEvent.click(confirmButton);
    await waitFor(() => expect(confirmSourceFrame).toHaveBeenCalledOnce());

    rerender(
      <SourceFrameSelection
        onBusyChange={onBusyChange}
        onSelectionChange={onSelectionChange}
        projectId="project-2"
        referenceAssetId="reference-2"
      />,
    );
    await screen.findByAltText("候选源画面 1");
    vi.mocked(extractSourceFrames).mockReturnValueOnce(
      new Promise(() => undefined),
    );
    fireEvent.click(screen.getByRole("button", { name: "重新取帧" }));
    await waitFor(() => expect(onBusyChange).toHaveBeenLastCalledWith(true));
    await act(async () => {
      resolveConfirmation?.(savedSelection);
      await pendingConfirmation;
    });

    expect(onSelectionChange).not.toHaveBeenCalledWith(savedSelection);
    expect(onBusyChange).toHaveBeenLastCalledWith(true);
    expect(onBusyChange.mock.calls.map(([busy]) => busy)).toEqual([
      true,
      false,
      true,
    ]);
  });

  it("releases confirmation busy ownership when the input context changes", async () => {
    let resolveConfirmation:
      | ((selection: typeof candidatesVersion) => void)
      | undefined;
    const pendingConfirmation = new Promise<typeof candidatesVersion>(
      (resolve) => {
        resolveConfirmation = resolve;
      },
    );
    vi.mocked(confirmSourceFrame).mockReturnValue(pendingConfirmation);
    const onBusyChange = vi.fn();
    const { rerender } = render(
      <SourceFrameSelection
        onBusyChange={onBusyChange}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );
    const confirmButton = await screen.findByRole("button", {
      name: "使用这张画面",
    });
    await waitFor(() => expect(confirmButton).toBeEnabled());
    fireEvent.click(confirmButton);
    await waitFor(() => expect(onBusyChange).toHaveBeenLastCalledWith(true));

    rerender(
      <SourceFrameSelection
        onBusyChange={onBusyChange}
        projectId="project-2"
        referenceAssetId="reference-2"
      />,
    );
    await screen.findByAltText("候选源画面 1");
    expect(onBusyChange.mock.calls.map(([busy]) => busy)).toEqual([
      true,
      false,
    ]);

    await act(async () => {
      resolveConfirmation?.(candidatesVersion);
      await pendingConfirmation;
    });
    expect(onBusyChange.mock.calls.map(([busy]) => busy)).toEqual([
      true,
      false,
    ]);
  });

  it("offers recovery when a new project load fails after old candidates", async () => {
    const { rerender } = render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );
    await screen.findByAltText("候选源画面 1");
    vi.mocked(getLatestProjectSourceFrames).mockRejectedValueOnce(
      new Error("新项目读取失败"),
    );

    rerender(
      <SourceFrameSelection
        projectId="project-2"
        referenceAssetId="reference-2"
      />,
    );

    expect(await screen.findByText("新项目读取失败")).toBeVisible();
    expect(screen.queryByAltText("候选源画面 1")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重新开始取帧" }));
    await waitFor(() =>
      expect(extractSourceFrames).toHaveBeenCalledWith(
        "project-2",
        "reference-2",
        [0, 1.5, 2.5],
      ),
    );
  });

  it("drops an old active task when the next project load fails", async () => {
    vi.mocked(getLatestProjectSourceFrameTask).mockResolvedValueOnce({
      ...sourceFrameTask,
      status: "RUNNING",
    });
    vi.mocked(waitForSourceFrameTask).mockReturnValueOnce(
      new Promise(() => undefined),
    );
    const { rerender } = render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );
    await screen.findByText("正在从原视频批量提取候选画面。");
    vi.mocked(getLatestProjectSourceFrames).mockRejectedValueOnce(
      new Error("新项目读取失败"),
    );

    rerender(
      <SourceFrameSelection
        projectId="project-2"
        referenceAssetId="reference-2"
      />,
    );

    expect(await screen.findByText("新项目读取失败")).toBeVisible();
    expect(screen.queryByText("正在取帧，请稍候。")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重新开始取帧" }));
    await waitFor(() =>
      expect(extractSourceFrames).toHaveBeenCalledWith(
        "project-2",
        "reference-2",
        [0, 1.5, 2.5],
      ),
    );
  });

  it("does not reload an old project after a stale extraction completes", async () => {
    let resolveExtraction: ((task: typeof sourceFrameTask) => void) | undefined;
    const pendingExtraction = new Promise<typeof sourceFrameTask>((resolve) => {
      resolveExtraction = resolve;
    });
    vi.mocked(extractSourceFrames).mockReturnValue(pendingExtraction);
    const { rerender } = render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    await screen.findByAltText("候选源画面 1");
    fireEvent.click(screen.getByRole("button", { name: "重新取帧" }));
    await waitFor(() => expect(extractSourceFrames).toHaveBeenCalledOnce());
    rerender(
      <SourceFrameSelection
        projectId="project-2"
        referenceAssetId="reference-2"
      />,
    );
    await waitFor(() =>
      expect(getLatestProjectSourceFrames).toHaveBeenCalledWith("project-2"),
    );
    const loadCount = vi.mocked(getLatestProjectSourceFrames).mock.calls.length;

    await act(async () => {
      resolveExtraction?.(sourceFrameTask);
      await pendingExtraction;
    });

    expect(getLatestProjectSourceFrames).toHaveBeenCalledTimes(loadCount);
  });

  it("keeps the new extraction busy when an old enqueue resolves late", async () => {
    let resolveOldExtraction:
      | ((task: typeof sourceFrameTask) => void)
      | undefined;
    const oldExtraction = new Promise<typeof sourceFrameTask>((resolve) => {
      resolveOldExtraction = resolve;
    });
    const newExtraction = new Promise<typeof sourceFrameTask>(() => undefined);
    vi.mocked(extractSourceFrames)
      .mockReturnValueOnce(oldExtraction)
      .mockReturnValueOnce(newExtraction);
    const onBusyChange = vi.fn();
    const { rerender } = render(
      <SourceFrameSelection
        onBusyChange={onBusyChange}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    await screen.findByAltText("候选源画面 1");
    fireEvent.click(screen.getByRole("button", { name: "重新取帧" }));
    await waitFor(() => expect(extractSourceFrames).toHaveBeenCalledOnce());

    rerender(
      <SourceFrameSelection
        onBusyChange={onBusyChange}
        projectId="project-2"
        referenceAssetId="reference-2"
      />,
    );
    const newExtractButton = await screen.findByRole("button", {
      name: "重新取帧",
    });
    await waitFor(() => expect(newExtractButton).toBeEnabled());
    fireEvent.click(newExtractButton);
    await waitFor(() => expect(extractSourceFrames).toHaveBeenCalledTimes(2));
    expect(onBusyChange.mock.calls.map(([busy]) => busy)).toEqual([
      true,
      false,
      true,
    ]);

    await act(async () => {
      resolveOldExtraction?.(sourceFrameTask);
      await oldExtraction;
    });

    expect(onBusyChange.mock.calls.map(([busy]) => busy)).toEqual([
      true,
      false,
      true,
    ]);
  });

  it("releases workspace navigation after the durable extraction task is accepted", async () => {
    let resolveTask: ((task: SourceFrameTask) => void) | undefined;
    const pendingTask = new Promise<SourceFrameTask>((resolve) => {
      resolveTask = resolve;
    });
    vi.mocked(waitForSourceFrameTask).mockReturnValue(pendingTask);
    const onBusyChange = vi.fn();
    render(
      <SourceFrameSelection
        onBusyChange={onBusyChange}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    await screen.findByAltText("候选源画面 1");
    fireEvent.click(screen.getByRole("button", { name: "重新取帧" }));

    await waitFor(() => expect(extractSourceFrames).toHaveBeenCalledOnce());
    await waitFor(() => expect(onBusyChange).toHaveBeenLastCalledWith(false));
    expect(
      screen.getByText("已进入处理队列，后台即将开始取帧。"),
    ).toBeInTheDocument();

    await act(async () => {
      resolveTask?.({
        ...sourceFrameTask,
        status: "SUCCEEDED",
        result_version_id: candidatesVersion.id,
      });
      await pendingTask;
    });
  });

  it("resumes a pending extraction task after remount without enqueueing again", async () => {
    vi.mocked(getLatestProjectSourceFrames)
      .mockResolvedValueOnce(null)
      .mockResolvedValue(candidatesVersion);
    vi.mocked(getLatestProjectSourceFrameTask).mockResolvedValueOnce(
      sourceFrameTask,
    );

    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    expect(await screen.findByAltText("候选源画面 1")).toBeInTheDocument();
    expect(waitForSourceFrameTask).toHaveBeenCalledWith(sourceFrameTask.id);
    expect(extractSourceFrames).not.toHaveBeenCalled();
  });

  it("does not expose old candidates while a replacement task is active", async () => {
    vi.mocked(getLatestProjectSourceFrameTask).mockResolvedValue({
      ...sourceFrameTask,
      status: "RUNNING",
    });
    vi.mocked(waitForSourceFrameTask).mockReturnValue(
      new Promise<SourceFrameTask>(() => undefined),
    );
    const onSelectionChange = vi.fn();

    render(
      <SourceFrameSelection
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    expect(
      await screen.findByText("正在从原视频批量提取候选画面。"),
    ).toBeInTheDocument();
    expect(screen.queryByAltText("候选源画面 1")).toBeNull();
    expect(onSelectionChange).toHaveBeenLastCalledWith(null);
  });

  it("lets the user stop a pending task and restart extraction", async () => {
    vi.mocked(getLatestProjectSourceFrames).mockResolvedValue(null);
    vi.mocked(getLatestProjectSourceFrameTask).mockResolvedValue(
      sourceFrameTask,
    );
    vi.mocked(waitForSourceFrameTask).mockReturnValue(
      new Promise<SourceFrameTask>(() => undefined),
    );

    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
        videoDurationSeconds={12}
      />,
    );

    expect(
      await screen.findByText("已进入处理队列，后台即将开始取帧。"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "停止并重新取帧" }));

    await waitFor(() =>
      expect(cancelSourceFrameTask).toHaveBeenCalledWith(sourceFrameTask.id),
    );
    await waitFor(() =>
      expect(extractSourceFrames).toHaveBeenCalledWith(
        "project-1",
        "reference-1",
        [0, 3.6, 6, 8.4, 10.8],
      ),
    );
  });

  it("offers a direct retry after the latest task failed", async () => {
    vi.mocked(getLatestProjectSourceFrames).mockResolvedValue(null);
    vi.mocked(getLatestProjectSourceFrameTask).mockResolvedValue({
      ...sourceFrameTask,
      status: "FAILED",
      error_code: "SOURCE_FRAME_TASK_RECOVERY_REQUIRED",
      error_message: "取帧任务执行中断，请重新开始。",
      retryable: true,
    });

    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    expect(
      await screen.findByText("取帧任务执行中断，请重新开始。"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新开始取帧" }));

    await waitFor(() => expect(extractSourceFrames).toHaveBeenCalledOnce());
  });

  // P0-03-02：候选自动提取与特征预填（红灯先行）。

  it("auto-extracts default candidates when the project has none", async () => {
    vi.mocked(getLatestProjectSourceFrames).mockResolvedValueOnce(null);
    render(
      <SourceFrameSelection
        videoDurationSeconds={12}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    await waitFor(() =>
      expect(extractSourceFrames).toHaveBeenCalledWith(
        "project-1",
        "reference-1",
        [0, 3.6, 6, 8.4, 10.8],
      ),
    );
    expect(await screen.findByAltText("候选源画面 1")).toBeInTheDocument();
  });

  it("requires confirmation after candidates are extracted", async () => {
    vi.mocked(getLatestProjectSourceFrames).mockResolvedValueOnce(null);
    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    expect(await screen.findByAltText("候选源画面 1")).toBeInTheDocument();
    expect(
      await screen.findByText("已推荐画面 1，请确认或更换源画面。"),
    ).toBeInTheDocument();
    expect(confirmSourceFrame).not.toHaveBeenCalled();
  });

  it("does not auto-extract for a read-only auditor", async () => {
    vi.mocked(getLatestProjectSourceFrames).mockResolvedValueOnce(null);
    render(
      <SourceFrameSelection
        projectId="project-1"
        readOnly
        referenceAssetId="reference-1"
      />,
    );

    expect(await screen.findByText("尚未提取候选源画面。")).toBeInTheDocument();
    expect(extractSourceFrames).not.toHaveBeenCalled();
  });

  it("auto-extracts again after switching to another project without candidates", async () => {
    vi.mocked(getLatestProjectSourceFrames).mockResolvedValueOnce(null);
    const { rerender } = render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );
    await waitFor(() => expect(extractSourceFrames).toHaveBeenCalledOnce());

    vi.mocked(getLatestProjectSourceFrames).mockResolvedValueOnce(null);
    rerender(
      <SourceFrameSelection
        projectId="project-2"
        referenceAssetId="reference-2"
      />,
    );
    await waitFor(() =>
      expect(extractSourceFrames).toHaveBeenCalledWith(
        "project-2",
        "reference-2",
        [0, 1.5, 2.5],
      ),
    );
  });

  it("preselects the highest-scored candidate after loading", async () => {
    render(
      <SourceFrameSelection
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    await screen.findByAltText("候选源画面 1");
    expect(screen.getByDisplayValue("source-1")).toBeChecked();
    expect(screen.getByDisplayValue("source-2")).not.toBeChecked();
  });

  it("passes the latest visual suggestion to manual confirmation", async () => {
    render(
      <SourceFrameSelection
        featureSuggestion={{
          body_completeness: "FACE_ONLY",
          face_visible: true,
          orientation: "FRONT",
          shot_size: "CLOSE_UP",
        }}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    const confirmButton = await screen.findByRole("button", {
      name: "使用这张画面",
    });
    await waitFor(() => expect(confirmButton).toBeEnabled());
    fireEvent.click(confirmButton);
    await waitFor(() =>
      expect(confirmSourceFrame).toHaveBeenCalledWith("project-1", "source-1", {
        body_completeness: "FACE_ONLY",
        face_visible: true,
        orientation: "FRONT",
        shot_size: "CLOSE_UP",
      }),
    );
    expect(screen.queryByLabelText("人物朝向")).toBeNull();
  });

  it("does not reload across parent re-renders with a changing busy callback", async () => {
    const { rerender } = render(
      <SourceFrameSelection
        featureSuggestion={{
          body_completeness: "UPPER_BODY",
          face_visible: true,
          orientation: "FRONT",
          shot_size: "HALF_BODY",
        }}
        onBusyChange={vi.fn()}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    await screen.findByAltText("候选源画面 1");
    const initialLoadCount = vi.mocked(getLatestProjectSourceFrames).mock.calls
      .length;
    expect(confirmSourceFrame).not.toHaveBeenCalled();

    rerender(
      <SourceFrameSelection
        featureSuggestion={{
          body_completeness: "UPPER_BODY",
          face_visible: true,
          orientation: "FRONT",
          shot_size: "HALF_BODY",
        }}
        onBusyChange={vi.fn()}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    expect(getLatestProjectSourceFrames).toHaveBeenCalledTimes(
      initialLoadCount,
    );
    expect(confirmSourceFrame).not.toHaveBeenCalled();
  });

  it("keeps an existing confirmed source frame over incoming suggestions", async () => {
    vi.mocked(getLatestProjectSourceFrameSelection).mockResolvedValue({
      stale: false,
      version: {
        ...candidatesVersion,
        id: "source-selection-1",
        kind: "source_frame_selection",
        payload: {
          source_frame_asset_id: "source-2",
          character_features: {
            orientation: "LEFT_45",
            shot_size: "HALF_BODY",
            face_visible: false,
            body_completeness: "UPPER_BODY",
          },
        },
      },
    });
    render(
      <SourceFrameSelection
        featureSuggestion={{
          body_completeness: "FULL_BODY",
          face_visible: true,
          orientation: "FRONT",
          shot_size: "FULL_BODY",
        }}
        projectId="project-1"
        referenceAssetId="reference-1"
      />,
    );

    expect(
      await screen.findByText("已确认源画面，将保留原视频的构图与动作。"),
    ).toBeInTheDocument();
    expect(screen.getByDisplayValue("source-2")).toBeChecked();
    expect(confirmSourceFrame).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("人物朝向")).toBeNull();
  });
});
