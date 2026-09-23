import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  confirmFirstFrame,
  generateFirstFrames,
  getAssetDownloadUrl,
  getLatestFirstFrameTask,
  getLatestProjectFirstFrameSelection,
  getLatestProjectFirstFrames,
  getProjectFirstFrameHistory,
  resumeFirstFrameGeneration,
} from "./api";
import { FirstFrameSelection } from "./FirstFrameSelection";

const referenceSelection = {
  id: "reference-selection-1",
  project_id: "project-1",
  source_frame_version_id: "source-selection-1",
  character_version_id: "character-version-3",
  recommended_asset_ids_json: ["character-front"],
  selected_asset_ids_json: ["character-front"],
  recommendation_reason_json: {},
  character_version_snapshot_json: {},
  selected_by: "employee_1",
  selected_at: "2030-01-01T00:00:00Z",
};

vi.mock("./api", () => ({
  confirmFirstFrame: vi.fn(),
  generateFirstFrames: vi.fn(),
  getAssetDownloadUrl: vi.fn(),
  getLatestProjectFirstFrames: vi.fn(),
  getLatestProjectFirstFrameSelection: vi.fn(),
  getLatestFirstFrameTask: vi.fn(),
  getProjectFirstFrameHistory: vi.fn(),
  resumeFirstFrameGeneration: vi.fn(),
  getWorkspacePricing: vi.fn(async () => ({
    prices: [{ subject: "first_frame", unit_credits: 2 }],
  })),
  readFirstFrameCandidates: vi.fn((version) => version.payload),
  readFirstFrameSelectionPayload: vi.fn((version) => version.payload),
}));

const candidatesVersion = {
  id: "first-frame-candidates-2",
  project_id: "project-1",
  asset_id: "source-1",
  kind: "first_frame_candidates",
  version_number: 2,
  payload: {
    provider: "apilio",
    model: "nano-banana-pro-2k",
    prompt: "replace the person",
    candidates: [
      {
        asset_id: "first-1",
        storage_key: "projects/project-1/first-1.png",
        storage_uri: "local://first-1",
        sha256: "hash-1",
        size_bytes: 100,
        content_type: "image/png",
        quality: {
          passed: true,
          attempt: 2,
          issue_codes: [],
          inspection: { head_only_replacement_detected: false },
        },
      },
      {
        asset_id: "first-2",
        storage_key: "projects/project-1/first-2.png",
        storage_uri: "local://first-2",
        sha256: "hash-2",
        size_bytes: 100,
        content_type: "image/png",
      },
    ],
  },
  created_by_user_id: "employee_1",
  created_at: "2030-01-01T00:00:00Z",
};

const pendingFirstFrameTask = {
  id: "first-frame-task-1",
  project_id: "project-1",
  status: "RUNNING" as const,
  stage: "GENERATING" as const,
  attempt: 1,
  result_version_id: null,
  error_code: null,
  error_message: null,
  retryable: true,
  created_at: "2030-01-01T00:00:00Z",
  updated_at: "2030-01-01T00:00:01Z",
  started_at: "2030-01-01T00:00:01Z",
  completed_at: null,
};

describe("FirstFrameSelection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getLatestProjectFirstFrames).mockResolvedValue({
      version: candidatesVersion,
      stale: false,
    });
    vi.mocked(getProjectFirstFrameHistory).mockResolvedValue([
      candidatesVersion,
    ]);
    vi.mocked(getLatestProjectFirstFrameSelection).mockResolvedValue({
      version: null,
      stale: false,
    });
    vi.mocked(getLatestFirstFrameTask).mockResolvedValue(null);
    vi.mocked(resumeFirstFrameGeneration).mockResolvedValue(candidatesVersion);
    vi.mocked(getAssetDownloadUrl).mockImplementation(async (assetId) => ({
      url: `https://private.example/${assetId}.png`,
    }));
    vi.mocked(confirmFirstFrame).mockResolvedValue({
      ...candidatesVersion,
      id: "first-frame-selection-1",
      kind: "first_frame_selection",
      payload: { first_frame_asset_id: "first-1" },
    });
    vi.mocked(generateFirstFrames).mockResolvedValue(candidatesVersion);
  });

  it("restores the saved scene setting with the candidate version", async () => {
    vi.mocked(getLatestProjectFirstFrames).mockResolvedValue({
      version: {
        ...candidatesVersion,
        payload: { ...candidatesVersion.payload, replace_scene: true },
      },
      stale: false,
    });
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
        simplified
      />,
    );
    await screen.findByRole("button", { name: "再生成1张" });
    expect(screen.getByLabelText("场景设置")).toHaveValue("replace");
    expect(screen.queryByText("画幅或场景已更改，请重新生成。")).toBeNull();
  });

  it("shows the chosen candidate at full preview size without automatically confirming it", async () => {
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
        simplified
      />,
    );
    fireEvent.click(await screen.findByRole("radio", { name: /首帧候选 1/ }));
    expect(await screen.findByAltText("当前首帧预览")).toHaveAttribute(
      "src",
      "https://private.example/first-1.png",
    );
    fireEvent.click(screen.getByRole("radio", { name: /首帧候选 2/ }));
    expect(screen.getByAltText("当前首帧预览")).toHaveAttribute(
      "src",
      "https://private.example/first-2.png",
    );
    expect(confirmFirstFrame).not.toHaveBeenCalled();
  });

  it("hides and locks the previous first-frame material while inputs change", async () => {
    const { rerender } = render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
        simplified
      />,
    );
    fireEvent.click(await screen.findByRole("radio", { name: /首帧候选 1/ }));
    expect(screen.getByAltText("当前首帧预览")).toBeVisible();

    const pendingLatest = new Promise<never>(() => undefined);
    vi.mocked(getLatestProjectFirstFrames).mockReturnValueOnce(pendingLatest);
    rerender(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-2"
        simplified
      />,
    );

    expect(screen.queryByAltText("当前首帧预览")).toBeNull();
    expect(screen.queryByAltText("首帧候选 1")).toBeNull();
    expect(
      screen.getByRole("button", { name: /生成1张首帧|再生成1张/ }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "使用这张首帧" })).toBeDisabled();
  });

  it("sends the selected image aspect ratio in simplified mode", async () => {
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
        simplified
      />,
    );
    await screen.findByRole("button", { name: "再生成1张" });
    fireEvent.change(screen.getByLabelText("图片画幅"), {
      target: { value: "9:16" },
    });
    fireEvent.click(screen.getByRole("button", { name: "再生成1张" }));
    await waitFor(() =>
      expect(generateFirstFrames).toHaveBeenCalledWith(
        "project-1",
        expect.objectContaining({ aspect_ratio: "9:16" }),
        expect.any(Function),
      ),
    );
  });

  it("invalidates adopted output when scene changes and sends replacement setting", async () => {
    const onSelectionChange = vi.fn();
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
        simplified
        onSelectionChange={onSelectionChange}
      />,
    );
    await screen.findByRole("button", { name: "再生成1张" });
    onSelectionChange.mockClear();
    fireEvent.change(screen.getByLabelText("场景设置"), {
      target: { value: "replace" },
    });
    expect(onSelectionChange).toHaveBeenCalledWith(null);
    expect(screen.getByRole("button", { name: "使用这张首帧" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "再生成1张" }));
    await waitFor(() =>
      expect(generateFirstFrames).toHaveBeenCalledWith(
        "project-1",
        expect.objectContaining({ replace_scene: true }),
        expect.any(Function),
      ),
    );
  });

  it("does not resume an old completed task after the scene binding changes", async () => {
    vi.mocked(getLatestProjectFirstFrames).mockResolvedValue({
      version: null,
      stale: true,
    });
    vi.mocked(getLatestFirstFrameTask).mockResolvedValue({
      ...pendingFirstFrameTask,
      status: "SUCCEEDED",
      result_version_id: "old-candidates",
    });
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
        simplified
      />,
    );
    await screen.findByText("上游输入已更新，请重新生成首帧。");
    await waitFor(() => expect(getLatestFirstFrameTask).toHaveBeenCalled());
    expect(resumeFirstFrameGeneration).not.toHaveBeenCalled();
  });

  it("restores completed candidates without marking the workspace as generating", async () => {
    const onBusyChange = vi.fn();
    vi.mocked(getLatestFirstFrameTask).mockResolvedValue({
      ...pendingFirstFrameTask,
      status: "SUCCEEDED",
      result_version_id: candidatesVersion.id,
    });
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
        onBusyChange={onBusyChange}
      />,
    );
    await screen.findByText("已自动预选最新候选，请查看后确认。");
    expect(onBusyChange).not.toHaveBeenCalledWith(true);
    expect(resumeFirstFrameGeneration).not.toHaveBeenCalled();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("shows scene comparison and confirms manual-review output without a failed-QC override", async () => {
    const manual = {
      ...candidatesVersion,
      payload: {
        ...candidatesVersion.payload,
        review_mode: "HUMAN_CONFIRMATION",
        source_frame_asset_id: "source-original",
        character_reference_asset_ids: [
          "scene-reference",
          "full-character-reference",
        ],
        candidates: [
          { ...candidatesVersion.payload.candidates[0], quality: null },
        ],
      },
    };
    vi.mocked(getLatestProjectFirstFrames).mockResolvedValue({
      version: manual,
      stale: false,
    });
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
        simplified
      />,
    );
    fireEvent.click(await screen.findByText("查看原图与人物参考"));
    expect(
      await screen.findAllByRole("img", { name: "所选场景形象" }),
    ).toHaveLength(2);
    expect(
      screen.getByRole("img", { name: "原视频源画面" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: /首帧候选 1/ }));
    fireEvent.click(screen.getByRole("button", { name: "使用这张首帧" }));
    await waitFor(() =>
      expect(confirmFirstFrame).toHaveBeenCalledWith("project-1", "first-1"),
    );
    expect(screen.queryByText(/该候选未通过自动质检/)).not.toBeInTheDocument();
  });

  it("shows a visible error for every unavailable character reference", async () => {
    vi.mocked(getLatestProjectFirstFrames).mockResolvedValue({
      version: {
        ...candidatesVersion,
        payload: {
          ...candidatesVersion.payload,
          review_mode: "HUMAN_CONFIRMATION",
          source_frame_asset_id: "source-original",
          character_reference_asset_ids: [
            "scene-reference",
            "missing-character-reference",
          ],
        },
      },
      stale: false,
    });
    vi.mocked(getAssetDownloadUrl).mockImplementation(async (assetId) => {
      if (assetId === "missing-character-reference") {
        throw new Error("预览暂不可用");
      }
      return { url: `https://private.example/${assetId}.png` };
    });

    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
        simplified
      />,
    );
    fireEvent.click(await screen.findByText("查看原图与人物参考"));

    expect(
      await screen.findByText("所选场景形象预览暂不可用，请刷新重试。"),
    ).toBeVisible();
    expect(screen.getByRole("img", { name: "所选场景形象" })).toBeVisible();
  });

  it("labels unchecked output and requires explicit human confirmation", async () => {
    const unverified = {
      ...candidatesVersion,
      payload: {
        ...candidatesVersion.payload,
        candidates: candidatesVersion.payload.candidates.map((candidate) => ({
          ...candidate,
          quality: null,
        })),
      },
    };
    vi.mocked(getLatestProjectFirstFrames).mockResolvedValue({
      version: unverified,
      stale: false,
    });
    vi.mocked(getProjectFirstFrameHistory).mockResolvedValue([unverified]);
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );
    expect(await screen.findAllByText("检查人物、服装和肢体。")).toHaveLength(
      2,
    );
    fireEvent.click(screen.getByRole("radio", { name: /首帧候选 1/ }));
    fireEvent.click(screen.getByRole("button", { name: "使用这张首帧" }));
    await waitFor(() =>
      expect(confirmFirstFrame).toHaveBeenCalledWith("project-1", "first-1"),
    );
  });

  it("shows the Apilio model, candidates, and requires a visible choice before confirmation", async () => {
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );

    expect(await screen.findByText("人物置换首帧")).toBeInTheDocument();
    expect(
      screen.getByText("当前模式：高清图像 · 正式服务"),
    ).toBeInTheDocument();
    expect(screen.getByAltText("首帧候选 1")).toHaveAttribute(
      "src",
      "https://private.example/first-1.png",
    );
    expect(screen.queryByText(/整身人物质检通过/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "使用这张首帧" })).toBeDisabled();

    fireEvent.click(screen.getByRole("radio", { name: /首帧候选 1/ }));
    fireEvent.click(screen.getByRole("button", { name: "使用这张首帧" }));

    await waitFor(() =>
      expect(confirmFirstFrame).toHaveBeenCalledWith("project-1", "first-1"),
    );
    expect(
      await screen.findByText("已确认首帧候选 1，可继续生成视频。"),
    ).toBeInTheDocument();
  });

  it("lets humans select a legacy candidate without an AI-score gate", async () => {
    const rejectedVersion = {
      ...candidatesVersion,
      id: "first-frame-candidates-rejected",
      payload: {
        ...candidatesVersion.payload,
        candidates: [
          {
            asset_id: "first-rejected",
            storage_key: "projects/project-1/first-rejected.png",
            storage_uri: "local://first-rejected",
            sha256: "hash-rejected",
            size_bytes: 100,
            content_type: "image/png",
            quality: {
              passed: false,
              attempt: 2,
              issue_codes: ["OUTFIT_MISMATCH"],
              inspection: {},
            },
          },
        ],
      },
    };
    vi.mocked(getLatestProjectFirstFrames).mockResolvedValue({
      version: rejectedVersion,
      stale: false,
    });
    vi.mocked(getProjectFirstFrameHistory).mockResolvedValue([rejectedVersion]);

    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );

    await screen.findByAltText("首帧候选 1");
    expect(
      screen.queryByText("质检未通过：OUTFIT_MISMATCH"),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: /首帧候选 1/ }));
    fireEvent.click(screen.getByRole("button", { name: "使用这张首帧" }));

    await waitFor(() =>
      expect(confirmFirstFrame).toHaveBeenCalledWith(
        "project-1",
        "first-rejected",
      ),
    );
  });

  it("re-signs an expired first-frame preview once and then disables it", async () => {
    let firstAssetCalls = 0;
    vi.mocked(getAssetDownloadUrl).mockImplementation(async (assetId) => {
      if (assetId === "first-1") {
        firstAssetCalls += 1;
        return {
          url: `https://private.example/first-1-${firstAssetCalls}.png`,
        };
      }
      return { url: `https://private.example/${assetId}.png` };
    });

    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );

    const preview = await screen.findByAltText("首帧候选 1");
    fireEvent.error(preview);
    await waitFor(() =>
      expect(screen.getByAltText("首帧候选 1")).toHaveAttribute(
        "src",
        "https://private.example/first-1-2.png",
      ),
    );
    fireEvent.error(screen.getByAltText("首帧候选 1"));
    expect(
      await screen.findByText("预览加载失败，请重新生成"),
    ).toBeInTheDocument();
    expect(firstAssetCalls).toBe(2);
    expect(screen.getByRole("radio", { name: /首帧候选 1/ })).toBeDisabled();
  });

  it("allows the employee to change the model, edit the prompt, and regenerate candidates", async () => {
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );
    await screen.findByText("人物置换首帧");
    expect(screen.getByLabelText("首帧编辑提示词")).toHaveAttribute(
      "rows",
      "10",
    );

    fireEvent.change(screen.getByLabelText("首帧生成模式"), {
      target: { value: "gpt-image-2" },
    });
    fireEvent.change(screen.getByLabelText("首帧编辑提示词"), {
      target: { value: "Use the selected character identity." },
    });
    fireEvent.click(screen.getByRole("button", { name: "再生成1张" }));

    await waitFor(() =>
      expect(generateFirstFrames).toHaveBeenCalledWith(
        "project-1",
        {
          model: "gpt-image-2",
          prompt: "Use the selected character identity.",
          quantity: 1,
          replace_scene: false,
          character_version_id: "character-version-3",
          character_reference_selection_id: "reference-selection-1",
        },
        expect.any(Function),
      ),
    );
  });

  it("lets the server compose the contact-sheet prompt in simplified mode", async () => {
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        simplified
        sourceFrameSelectionId="source-selection-1"
      />,
    );
    await screen.findByText("人物置换首帧");

    fireEvent.click(screen.getByRole("button", { name: "再生成1张" }));

    await waitFor(() =>
      expect(generateFirstFrames).toHaveBeenCalledWith(
        "project-1",
        {
          model: "gpt-image-2",
          prompt: undefined,
          quantity: 1,
          replace_scene: false,
          character_version_id: "character-version-3",
          character_reference_selection_id: "reference-selection-1",
        },
        expect.any(Function),
      ),
    );
  });

  it("keeps one generation request alive across page changes and reattaches progress", async () => {
    let resolveGeneration:
      | ((version: typeof candidatesVersion) => void)
      | undefined;
    const pendingGeneration = new Promise<typeof candidatesVersion>(
      (resolve) => {
        resolveGeneration = resolve;
      },
    );
    vi.mocked(generateFirstFrames).mockImplementation(
      (_projectId, _input, onTaskUpdate) => {
        onTaskUpdate?.({
          ...pendingFirstFrameTask,
          status: "PENDING",
          stage: "QUEUED",
          attempt: 0,
          started_at: null,
        });
        return pendingGeneration;
      },
    );

    const firstPage = render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        simplified
        sourceFrameSelectionId="source-selection-1"
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "再生成1张" }));

    expect(
      await screen.findByRole("progressbar", {
        name: "人物置换首帧生成进度",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("正在排队")).toBeInTheDocument();
    expect(
      screen.getByText("可离开页面，返回后继续查看。"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/本地服务未关闭/)).toBeNull();

    firstPage.unmount();
    vi.mocked(getLatestFirstFrameTask).mockResolvedValue(pendingFirstFrameTask);
    vi.mocked(resumeFirstFrameGeneration).mockReturnValue(pendingGeneration);
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        simplified
        sourceFrameSelectionId="source-selection-1"
      />,
    );

    expect(
      await screen.findByRole("progressbar", {
        name: "人物置换首帧生成进度",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("正在生成首帧")).toBeInTheDocument();
    expect(screen.queryByText(/任务 first-frame-task-1/)).toBeNull();
    expect(
      screen.getByRole("progressbar", { name: "人物置换首帧生成进度" }),
    ).toBeInTheDocument();
    expect(generateFirstFrames).toHaveBeenCalledOnce();

    await act(async () => {
      resolveGeneration?.(candidatesVersion);
      await pendingGeneration;
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("progressbar", {
          name: "人物置换首帧生成进度",
        }),
      ).toBeNull(),
    );
  });

  it("reattaches a resolved generation until the replacement page consumes it", async () => {
    let resolveGeneration:
      | ((version: typeof candidatesVersion) => void)
      | undefined;
    const pendingGeneration = new Promise<typeof candidatesVersion>(
      (resolve) => {
        resolveGeneration = resolve;
      },
    );
    let releaseStaleLoad:
      | ((value: { version: typeof candidatesVersion; stale: false }) => void)
      | undefined;
    const staleLoad = new Promise<{
      version: typeof candidatesVersion;
      stale: false;
    }>((resolve) => {
      releaseStaleLoad = resolve;
    });
    vi.mocked(generateFirstFrames).mockReturnValue(pendingGeneration);

    const firstPage = render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        simplified
        sourceFrameSelectionId="source-selection-1"
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "再生成1张" }));
    await screen.findByRole("progressbar", {
      name: "人物置换首帧生成进度",
    });

    vi.mocked(getLatestProjectFirstFrames).mockReturnValueOnce(staleLoad);
    await act(async () => {
      resolveGeneration?.(candidatesVersion);
      await pendingGeneration;
    });
    await waitFor(() =>
      expect(getLatestProjectFirstFrames).toHaveBeenCalledTimes(2),
    );

    firstPage.unmount();
    vi.mocked(getLatestFirstFrameTask).mockResolvedValue({
      ...pendingFirstFrameTask,
      status: "SUCCEEDED",
      result_version_id: candidatesVersion.id,
      completed_at: "2030-01-01T00:00:10Z",
    });
    vi.mocked(resumeFirstFrameGeneration).mockResolvedValue(candidatesVersion);
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        simplified
        sourceFrameSelectionId="source-selection-1"
      />,
    );

    expect(
      await screen.findByText("已自动预选最新候选，请查看后确认。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /首帧候选 2/ })).toBeChecked();
    expect(
      screen.queryByRole("progressbar", {
        name: "人物置换首帧生成进度",
      }),
    ).toBeNull();

    await act(async () => {
      releaseStaleLoad?.({ version: candidatesVersion, stale: false });
      await staleLoad;
    });
  });

  it("shows a background generation failure when the user returns", async () => {
    let rejectGeneration: ((reason: Error) => void) | undefined;
    const pendingGeneration = new Promise<typeof candidatesVersion>(
      (_resolve, reject) => {
        rejectGeneration = reject;
      },
    );
    vi.mocked(generateFirstFrames).mockReturnValue(pendingGeneration);

    const firstPage = render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        simplified
        sourceFrameSelectionId="source-selection-1"
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "再生成1张" }));
    await screen.findByRole("progressbar", {
      name: "人物置换首帧生成进度",
    });
    firstPage.unmount();

    await act(async () => {
      rejectGeneration?.(new Error("云端首帧生成失败，请重试。"));
      await pendingGeneration.catch(() => undefined);
    });

    vi.mocked(getLatestFirstFrameTask).mockResolvedValue({
      ...pendingFirstFrameTask,
      status: "FAILED",
      error_code: "PROVIDER_ERROR",
      error_message: "云端首帧生成失败，请重试。",
      completed_at: "2030-01-01T00:00:10Z",
    });

    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        simplified
        sourceFrameSelectionId="source-selection-1"
      />,
    );

    expect(
      await screen.findByText("云端首帧生成失败，请重试。"),
    ).toBeInTheDocument();
    expect(generateFirstFrames).toHaveBeenCalledOnce();
    expect(
      screen.queryByRole("progressbar", {
        name: "人物置换首帧生成进度",
      }),
    ).toBeNull();
  });

  it("loads history and uses the binding-less generation path for a legacy character", async () => {
    render(
      <FirstFrameSelection
        legacyCharacterSelected
        projectId="project-1"
        referenceSelection={null}
        sourceFrameSelectionId="source-selection-1"
      />,
    );
    await screen.findByRole("button", { name: "版本 #2" });

    fireEvent.change(screen.getByLabelText("首帧编辑提示词"), {
      target: { value: "Keep the legacy character identity." },
    });
    fireEvent.click(screen.getByRole("button", { name: "再生成1张" }));

    await waitFor(() =>
      expect(generateFirstFrames).toHaveBeenCalledWith(
        "project-1",
        {
          model: "nano-banana-pro-2k",
          prompt: "Keep the legacy character identity.",
          quantity: 1,
          replace_scene: false,
        },
        expect.any(Function),
      ),
    );
  });

  it("keeps legacy history visible but disables generation without a current source", async () => {
    render(
      <FirstFrameSelection
        legacyCharacterSelected
        projectId="project-1"
        referenceSelection={null}
        sourceFrameSelectionId={null}
      />,
    );

    expect(
      await screen.findByRole("button", { name: "版本 #2" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "再生成1张" })).toBeDisabled();
  });

  it("clearly marks local fake output instead of presenting it as a provider result", async () => {
    vi.mocked(getLatestProjectFirstFrames).mockResolvedValue({
      stale: false,
      version: {
        ...candidatesVersion,
        payload: { ...candidatesVersion.payload, provider: "fake" },
      },
    });
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );

    expect(
      await screen.findByText("模拟输出：未接入正式生成服务。"),
    ).toBeInTheDocument();
  });

  // 问题3（全面放开+警示）：确认指向历史候选版本时仍是有效确认（后端按
  // selection 指向的版本放行生成），前端如实保留并给出“基于旧输入生成”警示。
  it("keeps an older-version confirmation effective with a history warning", async () => {
    vi.mocked(getLatestProjectFirstFrameSelection).mockResolvedValue({
      stale: false,
      version: {
        ...candidatesVersion,
        id: "first-frame-selection-1",
        kind: "first_frame_selection",
        payload: {
          first_frame_candidates_version_id: "first-frame-candidates-older",
          first_frame_asset_id: "first-1",
        },
      },
    });
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );

    expect(
      await screen.findByText(
        "已确认历史版本首帧（基于旧输入），仍可用于生成。",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /首帧候选 1/ })).not.toBeChecked();
  });

  it("loads existing candidate previews for a read-only auditor without writing", async () => {
    render(
      <FirstFrameSelection
        projectId="project-1"
        readOnly
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );

    expect(await screen.findByAltText("首帧候选 1")).toHaveAttribute(
      "src",
      "https://private.example/first-1.png",
    );
    expect(getAssetDownloadUrl).toHaveBeenCalledTimes(2);
    expect(generateFirstFrames).not.toHaveBeenCalled();
    expect(confirmFirstFrame).not.toHaveBeenCalled();
  });

  it("treats a stale latest generation as an upstream gate instead of a load error", async () => {
    vi.mocked(getLatestProjectFirstFrames).mockResolvedValue({
      version: null,
      stale: true,
    });

    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );

    expect(
      await screen.findByText("上游输入已更新，请重新生成首帧。"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/读取候选首帧失败/)).toBeNull();
  });

  it("keeps the valid latest confirmation while browsing history", async () => {
    const historicalVersion = {
      ...candidatesVersion,
      id: "first-frame-candidates-1",
      version_number: 1,
      payload: {
        ...candidatesVersion.payload,
        candidates: [
          {
            ...candidatesVersion.payload.candidates[0],
            asset_id: "historical-first-1",
          },
        ],
      },
    };
    const confirmedSelection = {
      ...candidatesVersion,
      id: "first-frame-selection-1",
      kind: "first_frame_selection",
      payload: {
        first_frame_candidates_version_id: candidatesVersion.id,
        first_frame_asset_id: "first-1",
      },
    };
    vi.mocked(getProjectFirstFrameHistory).mockResolvedValue([
      candidatesVersion,
      historicalVersion,
    ]);
    vi.mocked(getLatestProjectFirstFrameSelection).mockResolvedValue({
      version: confirmedSelection,
      stale: false,
    });
    const onSelectionChange = vi.fn();
    render(
      <FirstFrameSelection
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );

    await waitFor(() =>
      expect(onSelectionChange).toHaveBeenLastCalledWith(confirmedSelection),
    );
    fireEvent.click(screen.getByRole("button", { name: "版本 #1" }));

    expect(
      await screen.findByText("正在查看历史版本（基于旧输入），可选中后确认。"),
    ).toBeInTheDocument();
    expect(onSelectionChange).toHaveBeenLastCalledWith(confirmedSelection);
  });

  it("ignores a confirmation response after the reference input changes", async () => {
    const confirmedSelection = {
      ...candidatesVersion,
      id: "first-frame-selection-1",
      kind: "first_frame_selection",
      payload: {
        first_frame_candidates_version_id: candidatesVersion.id,
        first_frame_asset_id: "first-1",
      },
    };
    let resolveConfirmation:
      | ((selection: typeof confirmedSelection) => void)
      | undefined;
    const pendingConfirmation = new Promise<typeof confirmedSelection>(
      (resolve) => {
        resolveConfirmation = resolve;
      },
    );
    vi.mocked(confirmFirstFrame).mockReturnValue(pendingConfirmation);
    const onSelectionChange = vi.fn();
    const onBusyChange = vi.fn();
    const { rerender } = render(
      <FirstFrameSelection
        onBusyChange={onBusyChange}
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );

    await screen.findByAltText("首帧候选 1");
    fireEvent.click(screen.getByRole("radio", { name: /首帧候选 1/ }));
    fireEvent.click(screen.getByRole("button", { name: "使用这张首帧" }));
    await waitFor(() => expect(confirmFirstFrame).toHaveBeenCalledOnce());
    expect(onBusyChange).toHaveBeenLastCalledWith(true);

    rerender(
      <FirstFrameSelection
        onBusyChange={onBusyChange}
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        referenceSelection={{
          ...referenceSelection,
          id: "reference-selection-2",
        }}
        sourceFrameSelectionId="source-selection-1"
      />,
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "再生成1张" })).toBeEnabled(),
    );
    vi.mocked(generateFirstFrames).mockReturnValueOnce(
      new Promise(() => undefined),
    );
    fireEvent.click(screen.getByRole("button", { name: "再生成1张" }));
    await waitFor(() => expect(onBusyChange).toHaveBeenLastCalledWith(true));
    await act(async () => {
      resolveConfirmation?.(confirmedSelection);
      await pendingConfirmation;
    });

    expect(onSelectionChange).not.toHaveBeenCalledWith(confirmedSelection);
    expect(onBusyChange).toHaveBeenLastCalledWith(true);
    expect(
      screen.getByRole("button", { name: "正在生成" }),
    ).toBeInTheDocument();
  });

  it("accepts a confirmation response across a same-input reload", async () => {
    const confirmedSelection = {
      ...candidatesVersion,
      id: "first-frame-selection-1",
      kind: "first_frame_selection",
      payload: {
        first_frame_candidates_version_id: candidatesVersion.id,
        first_frame_asset_id: "first-1",
      },
    };
    let resolveConfirmation:
      | ((selection: typeof confirmedSelection) => void)
      | undefined;
    const pendingConfirmation = new Promise<typeof confirmedSelection>(
      (resolve) => {
        resolveConfirmation = resolve;
      },
    );
    vi.mocked(confirmFirstFrame).mockReturnValue(pendingConfirmation);
    const onSelectionChange = vi.fn();
    const { rerender } = render(
      <FirstFrameSelection
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );

    await screen.findByAltText("首帧候选 1");
    fireEvent.click(screen.getByRole("radio", { name: /首帧候选 1/ }));
    fireEvent.click(screen.getByRole("button", { name: "使用这张首帧" }));
    await waitFor(() => expect(confirmFirstFrame).toHaveBeenCalledOnce());

    rerender(
      <FirstFrameSelection
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        referenceSelection={referenceSelection}
        simplified
        sourceFrameSelectionId="source-selection-1"
      />,
    );
    await act(async () => {
      resolveConfirmation?.(confirmedSelection);
      await pendingConfirmation;
    });

    expect(onSelectionChange).toHaveBeenCalledWith(confirmedSelection);
    expect(
      screen.getByRole("button", { name: "再生成1张" }),
    ).toBeInTheDocument();
  });

  it("ignores a confirmation response after the component unmounts", async () => {
    const confirmedSelection = {
      ...candidatesVersion,
      id: "first-frame-selection-1",
      kind: "first_frame_selection",
      payload: {
        first_frame_candidates_version_id: candidatesVersion.id,
        first_frame_asset_id: "first-1",
      },
    };
    let resolveConfirmation:
      | ((selection: typeof confirmedSelection) => void)
      | undefined;
    const pendingConfirmation = new Promise<typeof confirmedSelection>(
      (resolve) => {
        resolveConfirmation = resolve;
      },
    );
    vi.mocked(confirmFirstFrame).mockReturnValue(pendingConfirmation);
    const onSelectionChange = vi.fn();
    const page = render(
      <FirstFrameSelection
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );

    await screen.findByAltText("首帧候选 1");
    fireEvent.click(screen.getByRole("radio", { name: /首帧候选 1/ }));
    fireEvent.click(screen.getByRole("button", { name: "使用这张首帧" }));
    await waitFor(() => expect(confirmFirstFrame).toHaveBeenCalledOnce());

    page.unmount();
    await act(async () => {
      resolveConfirmation?.(confirmedSelection);
      await pendingConfirmation;
    });

    expect(onSelectionChange).not.toHaveBeenCalledWith(confirmedSelection);
  });

  it("locks candidate controls while confirmation is pending", async () => {
    let resolveConfirmation:
      | ((selection: typeof candidatesVersion) => void)
      | undefined;
    const pendingConfirmation = new Promise<typeof candidatesVersion>(
      (resolve) => {
        resolveConfirmation = resolve;
      },
    );
    vi.mocked(confirmFirstFrame).mockReturnValue(pendingConfirmation);
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );

    await screen.findByAltText("首帧候选 1");
    fireEvent.click(screen.getByRole("radio", { name: /首帧候选 1/ }));
    fireEvent.click(screen.getByRole("button", { name: "使用这张首帧" }));
    await waitFor(() => expect(confirmFirstFrame).toHaveBeenCalledOnce());

    expect(screen.getByRole("radio", { name: /首帧候选 1/ })).toBeDisabled();
    expect(screen.getByRole("radio", { name: /首帧候选 2/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "版本 #2" })).toBeDisabled();

    await act(async () => {
      resolveConfirmation?.(candidatesVersion);
      await pendingConfirmation;
    });

    expect(screen.getByRole("radio", { name: /首帧候选 1/ })).toBeEnabled();
  });

  it("ignores a legacy confirmation response after the source selection changes", async () => {
    const confirmedSelection = {
      ...candidatesVersion,
      id: "first-frame-selection-legacy",
      kind: "first_frame_selection",
      payload: {
        first_frame_candidates_version_id: candidatesVersion.id,
        first_frame_asset_id: "first-1",
      },
    };
    let resolveConfirmation:
      | ((selection: typeof confirmedSelection) => void)
      | undefined;
    const pendingConfirmation = new Promise<typeof confirmedSelection>(
      (resolve) => {
        resolveConfirmation = resolve;
      },
    );
    vi.mocked(confirmFirstFrame).mockReturnValue(pendingConfirmation);
    const onSelectionChange = vi.fn();
    const { rerender } = render(
      <FirstFrameSelection
        legacyCharacterSelected
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        referenceSelection={null}
        sourceFrameSelectionId="source-selection-1"
      />,
    );

    await screen.findByAltText("首帧候选 1");
    fireEvent.click(screen.getByRole("radio", { name: /首帧候选 1/ }));
    fireEvent.click(screen.getByRole("button", { name: "使用这张首帧" }));
    await waitFor(() => expect(confirmFirstFrame).toHaveBeenCalledOnce());

    rerender(
      <FirstFrameSelection
        legacyCharacterSelected
        onSelectionChange={onSelectionChange}
        projectId="project-1"
        referenceSelection={null}
        sourceFrameSelectionId="source-selection-2"
      />,
    );
    await act(async () => {
      resolveConfirmation?.(confirmedSelection);
      await pendingConfirmation;
    });

    expect(onSelectionChange).not.toHaveBeenCalledWith(confirmedSelection);
  });

  // P0-03-04：首帧候选生成完成后自动预选最新一张，确认压缩为一次点击；
  // 不改候选生成的人工触发与付费语义（红线 3：仅显式点击才生成）。
  it("auto-selects the newest candidate after regeneration for one-click confirmation (P0-03-04)", async () => {
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );
    await screen.findByText("人物置换首帧");

    // 红线 3：进入页面给不自动触发生成；预存候选仅展示不预选。
    expect(generateFirstFrames).not.toHaveBeenCalled();
    expect(
      await screen.findByRole("radio", { name: /首帧候选 1/ }),
    ).not.toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: "再生成1张" }));
    await waitFor(() => expect(generateFirstFrames).toHaveBeenCalledOnce());

    expect(
      await screen.findByText("已自动预选最新候选，请查看后确认。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /首帧候选 2/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /首帧候选 1/ })).not.toBeChecked();

    const confirmButton = screen.getByRole("button", {
      name: "使用这张首帧",
    });
    await waitFor(() => expect(confirmButton).toBeEnabled());
    fireEvent.click(confirmButton);
    await waitFor(() =>
      expect(confirmFirstFrame).toHaveBeenCalledWith("project-1", "first-2"),
    );
  });

  it("preserves the generated candidate selection across a same-version reload", async () => {
    const firstSelectionChange = vi.fn();
    const { rerender } = render(
      <FirstFrameSelection
        onSelectionChange={firstSelectionChange}
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );
    await screen.findByText("人物置换首帧");
    fireEvent.click(screen.getByRole("button", { name: "再生成1张" }));
    await screen.findByText("已自动预选最新候选，请查看后确认。");
    expect(screen.getByRole("radio", { name: /首帧候选 2/ })).toBeChecked();

    const callsBeforeReload = vi.mocked(getLatestProjectFirstFrames).mock.calls
      .length;
    rerender(
      <FirstFrameSelection
        onSelectionChange={vi.fn()}
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );
    await waitFor(() =>
      expect(getLatestProjectFirstFrames).toHaveBeenCalledTimes(
        callsBeforeReload + 1,
      ),
    );

    expect(screen.getByRole("radio", { name: /首帧候选 2/ })).toBeChecked();
  });

  // P0-03-04：旧确认与新生成候选不一致时同样预选第一张，重新确认保持单击。
  it("auto-selects the newest candidate after regenerating over a stale confirmation (P0-03-04)", async () => {
    vi.mocked(getLatestProjectFirstFrameSelection).mockResolvedValue({
      version: {
        ...candidatesVersion,
        id: "first-frame-selection-1",
        kind: "first_frame_selection",
        payload: {
          first_frame_candidates_version_id: "first-frame-candidates-1",
          first_frame_asset_id: "first-1",
        },
      },
      stale: false,
    });
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );
    expect(
      await screen.findByText(
        "已确认历史版本首帧（基于旧输入），仍可用于生成。",
      ),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "再生成1张" }));
    await waitFor(() => expect(generateFirstFrames).toHaveBeenCalledOnce());

    expect(
      await screen.findByText(
        "已确认历史版本首帧（基于旧输入），仍可用于生成。",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /首帧候选 2/ })).toBeChecked();

    const confirmButton = screen.getByRole("button", {
      name: "使用这张首帧",
    });
    await waitFor(() => expect(confirmButton).toBeEnabled());
    fireEvent.click(confirmButton);
    await waitFor(() =>
      expect(confirmFirstFrame).toHaveBeenCalledWith("project-1", "first-2"),
    );
  });

  // 问题3（全面放开+警示）：历史版本候选可选、可确认；确认时向后端指明
  // 候选版本，并给出“该图基于旧输入生成”的警示。历史版本仍不自动预选。
  it("allows selecting and confirming a candidate from a history version", async () => {
    const olderVersion = {
      ...candidatesVersion,
      id: "first-frame-candidates-1",
      version_number: 1,
    };
    vi.mocked(getProjectFirstFrameHistory).mockResolvedValue([
      candidatesVersion,
      olderVersion,
    ]);
    vi.mocked(confirmFirstFrame).mockResolvedValue({
      ...candidatesVersion,
      id: "first-frame-selection-history",
      kind: "first_frame_selection",
      payload: {
        first_frame_candidates_version_id: "first-frame-candidates-1",
        first_frame_asset_id: "first-1",
      },
    });
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );
    await screen.findByText("人物置换首帧");
    expect(screen.getByRole("radio", { name: /首帧候选 1/ })).not.toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: "版本 #1" }));
    expect(
      await screen.findByText("正在查看历史版本（基于旧输入），可选中后确认。"),
    ).toBeInTheDocument();
    const historyRadio = screen.getByRole("radio", { name: /首帧候选 1/ });
    await waitFor(() => expect(historyRadio).toBeEnabled());
    expect(historyRadio).not.toBeChecked();
    fireEvent.click(historyRadio);

    const confirmButton = screen.getByRole("button", { name: "使用这张首帧" });
    await waitFor(() => expect(confirmButton).toBeEnabled());
    fireEvent.click(confirmButton);
    await waitFor(() =>
      expect(confirmFirstFrame).toHaveBeenCalledWith("project-1", "first-1", {
        candidatesVersionId: "first-frame-candidates-1",
      }),
    );
    expect(
      await screen.findByText(
        "已确认首帧候选 1（基于旧输入生成），可继续生成视频。",
      ),
    ).toBeInTheDocument();
  });

  // 问题3：上游输入已更新（最新候选 stale）不再阻断历史版本确认——
  // 历史图天然基于旧输入，后端按历史路径放行，前端如实放行并给警示。
  it("keeps a history version confirmable after the latest inputs changed", async () => {
    const olderVersion = {
      ...candidatesVersion,
      id: "first-frame-candidates-1",
      version_number: 1,
    };
    vi.mocked(getProjectFirstFrameHistory).mockResolvedValue([
      candidatesVersion,
      olderVersion,
    ]);
    vi.mocked(getLatestProjectFirstFrames).mockResolvedValue({
      version: null,
      stale: true,
    });
    render(
      <FirstFrameSelection
        projectId="project-1"
        referenceSelection={referenceSelection}
        sourceFrameSelectionId="source-selection-1"
      />,
    );

    await screen.findByRole("button", { name: "版本 #1" });
    fireEvent.click(screen.getByRole("button", { name: "版本 #1" }));
    expect(
      await screen.findByText("正在查看历史版本（基于旧输入），可选中后确认。"),
    ).toBeInTheDocument();
    const historyRadio = screen.getByRole("radio", { name: /首帧候选 1/ });
    await waitFor(() => expect(historyRadio).toBeEnabled());
    fireEvent.click(historyRadio);

    const confirmButton = screen.getByRole("button", { name: "使用这张首帧" });
    await waitFor(() => expect(confirmButton).toBeEnabled());
    fireEvent.click(confirmButton);
    await waitFor(() =>
      expect(confirmFirstFrame).toHaveBeenCalledWith("project-1", "first-1", {
        candidatesVersionId: "first-frame-candidates-1",
      }),
    );
  });
});
