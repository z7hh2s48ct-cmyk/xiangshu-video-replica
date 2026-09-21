import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { StudioAsset, StudioContextValue } from "./types";

const { useStudio } = vi.hoisted(() => ({
  useStudio: vi.fn<() => StudioContextValue>(),
}));

vi.mock("./context", () => ({ useStudio }));

// 复刻模块（模块①）：部分 mock api/live，其余保持原实现。
const replicaApi = vi.hoisted(() => ({
  getAssetDownloadUrl: vi.fn(),
  getMaterialBatchPreviews: vi.fn(),
  selectCharacterReferences: vi.fn(),
  startVideoAnalysis: vi.fn(),
  getAnalysisTask: vi.fn(),
  waitForAnalysisTask: vi.fn(),
  getLatestProjectShotCards: vi.fn(),
  getLatestProjectAnalysis: vi.fn(async () => ({ id: "av-x", payload: {} })),
  getLatestGenerationPrompt: vi.fn(),
  getLatestScriptVersion: vi.fn(),
  getLatestScriptRewriteTask: vi.fn<
    (...args: [string, string?, string?]) => Promise<unknown>
  >(async () => null),
  getScriptRewriteTask: vi.fn(),
  rewriteProjectScript: vi.fn(),
  waitForScriptRewriteTask: vi.fn(),
  getLatestProjectFirstFrameSelection: vi.fn(),
  getGenerationPriceQuote: vi.fn(),
  saveGenerationPrompt: vi.fn(),
  createScriptVersion: vi.fn(),
  compileGenerationPrompt: vi.fn(),
  saveShotCards: vi.fn(),
}));
const replicaLive = vi.hoisted(() => ({
  readAudioDuration: vi.fn(),
  readVideoDuration: vi.fn(),
  readVideoFirstFrame: vi.fn(),
  uploadOralAudioMaterial: vi.fn(),
  uploadReferenceAudioMaterial: vi.fn(),
  uploadWorkbenchSourceVideo: vi.fn(),
  uploadVideoMaterial: vi.fn(),
  runReplicaGeneration: vi.fn(),
  loadSavedScriptList: vi.fn(async () => []),
  validateOralAudioFile: vi.fn(),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...replicaApi,
}));
vi.mock("./live", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...replicaLive,
}));

// 人物替换（模块②）：叶子组件打桩，专测组合与置位链路。
vi.mock("../CharacterSelection", () => ({
  CharacterSelection: (props: {
    onVersionChange?: (s: unknown) => void;
    readOnly?: boolean;
  }) => (
    <button
      data-read-only={String(Boolean(props.readOnly))}
      type="button"
      onClick={() =>
        props.onVersionChange?.({
          character_version_id: "cv-1",
          character_snapshot: { identity: { id: "ident-1" } },
        })
      }
    >
      stub-选择人物
    </button>
  ),
}));
vi.mock("../SourceFrameSelection", () => ({
  SourceFrameSelection: (props: {
    onSelectionChange?: (s: unknown) => void;
    readOnly?: boolean;
  }) => (
    <>
      <button
        data-read-only={String(Boolean(props.readOnly))}
        type="button"
        onClick={() => props.onSelectionChange?.({ id: "sfv-1", payload: {} })}
      >
        stub-确认源画面
      </button>
      <button
        type="button"
        onClick={() => props.onSelectionChange?.({ id: "sfv-2", payload: {} })}
      >
        stub-切换源画面
      </button>
    </>
  ),
}));
vi.mock("../FirstFrameSelection", () => ({
  FirstFrameSelection: (props: {
    onSelectionChange?: (s: unknown) => void;
    referenceSelection?: { id: string } | null;
    readOnly?: boolean;
  }) => (
    <>
      {props.referenceSelection ? (
        <span>stub-参考匹配-{props.referenceSelection.id}</span>
      ) : null}
      <button
        data-read-only={String(Boolean(props.readOnly))}
        type="button"
        onClick={() =>
          props.onSelectionChange?.({
            id: "ffv-1",
            payload: {
              first_frame_candidates_version_id: "cand-1",
              first_frame_asset_id: "ff-asset-1",
            },
          })
        }
      >
        stub-确认置换首帧
      </button>
      <button
        type="button"
        onClick={() =>
          props.onSelectionChange?.({
            id: "ffv-2",
            payload: {
              first_frame_candidates_version_id: "cand-2",
              first_frame_asset_id: "ff-asset-1",
            },
          })
        }
      >
        stub-更新置换首帧版本
      </button>
      <button type="button" onClick={() => props.onSelectionChange?.(null)}>
        stub-撤销置换首帧
      </button>
    </>
  ),
}));

import {
  CopyPage,
  OralPage,
  ReplacementPage,
  ReplicaPage,
  VideoPage,
} from "./CreationPages";

function studio(
  overrides: Partial<StudioContextValue> = {},
): StudioContextValue {
  return {
    state: {
      page: "copy",
      draft: {
        id: "draft-1",
        ipId: "person-1",
        sourceId: "source-1",
        sourceAssetId: "source-1",
        projectId: "project-1",
        selectedShotId: "shot-2",
        originalImageId: "original-1",
        imageId: "target-1",
        firstFrameId: "frame-1",
        avatarId: "avatar-1",
        voiceId: "voice-1",
        audioId: "audio-1",
        script: {
          id: "script-1",
          title: "建房预算",
          original: "原始文案",
          text: "已确认的乡墅口播终稿",
          version: 3,
          confirmed: true,
        },
        prompt: "庭院镜头缓慢推进",
        referenceIds: ["reference-1"],
        resolution: "768P",
        ratio: "16:9",
        duration: 8,
        count: 1,
        frameConfirmed: true,
        style: "standard",
        subtitles: false,
        quoteRevision: 1,
      },
      savedScripts: [],
      favorites: [],
    },
    data: {
      people: [
        {
          id: "person-1",
          name: "张工",
          role: "乡墅设计师",
          version: 1,
          scope: "乡墅设计",
          audience: "自建房家庭",
          expression: "专业通俗",
          sceneLookCount: 0,
          photoIds: [],
          avatars: [
            {
              id: "avatar-1",
              name: "设计室讲解",
              imageId: "target-1",
              ready: true,
              origin: "视频制作",
              duration: "00:42",
            },
          ],
          voices: [
            {
              id: "voice-1",
              name: "张工本人音色 V1",
              confirmed: true,
              isDefault: true,
            },
          ],
        },
      ],
      assets: [
        {
          id: "original-1",
          name: "原始画面",
          kind: "image",
          group: "项目",
          source: "视频复刻",
          saved: true,
        },
        {
          id: "target-1",
          name: "张工庭院讲解",
          kind: "image",
          group: "人物照片",
          personId: "person-1",
          source: "人物库场景造型",
          saved: true,
        },
        {
          id: "frame-1",
          name: "乡墅首帧",
          kind: "image",
          group: "项目",
          source: "人物置换",
          saved: true,
        },
        {
          id: "reference-1",
          name: "乡墅外观.jpg",
          kind: "image",
          group: "参考素材",
          source: "素材库",
          saved: true,
        },
        {
          id: "audio-1",
          name: "建房预算-录音.wav",
          kind: "audio",
          duration: "00:42",
          group: "完整口播音频",
          source: "素材库",
          saved: true,
        },
      ],
      videos: [],
      tasks: [],
      projects: [],
      errors: [],
      materials: [],
      loading: false,
      stats: null,
      analytics7: null,
      analytics30: null,
    },
    review: true,
    user: {
      id: "customer-1",
      username: "customer-1",
      display_name: "客户",
      role: "customer",
    },
    navigate: vi.fn(),
    patchDraft: vi.fn(),
    patchState: vi.fn(),
    updateData: vi.fn(),
    notify: vi.fn(),
    openPicker: vi.fn(),
    openLive: vi.fn(),
    requestGeneration: vi.fn(),
    saveDraft: vi.fn(),
    discardSavedDraft: vi.fn(),
    confirmFinalDraft: vi.fn(),
    extractScriptFromUpload: vi.fn(),
    refresh: vi.fn(),
    ...overrides,
  };
}

const referenceCapabilities = {
  extended_modes_enabled: true,
  t2v_enabled: true,
  i2v_enabled: true,
  r2v_enabled: true,
  last_frame_enabled: true,
  max_reference_images: 8,
  max_reference_videos: 3,
  max_reference_audios: 3,
  max_quantity: 4,
};

describe("V1.4 创作页面", () => {
  beforeEach(() => {
    useStudio.mockReset();
    replicaLive.loadSavedScriptList.mockReset().mockResolvedValue([]);
    replicaApi.getAssetDownloadUrl.mockReset();
    replicaApi.selectCharacterReferences.mockReset();
    replicaApi.getLatestGenerationPrompt.mockReset().mockResolvedValue({
      stale: false,
      stale_reasons: [],
      version: null,
    });
    replicaApi.getLatestScriptRewriteTask.mockReset();
    replicaApi.getLatestScriptRewriteTask.mockResolvedValue(null);
    sessionStorage.clear();
    replicaApi.getScriptRewriteTask.mockReset();
    replicaApi.rewriteProjectScript.mockReset();
    replicaApi.waitForScriptRewriteTask.mockReset();
    replicaLive.uploadVideoMaterial.mockReset();
    replicaLive.readAudioDuration.mockReset();
    replicaLive.readVideoDuration.mockReset();
    replicaLive.readVideoFirstFrame.mockReset();
    replicaLive.readVideoFirstFrame.mockResolvedValue(undefined);
    replicaApi.getMaterialBatchPreviews.mockReset();
    replicaApi.getMaterialBatchPreviews.mockResolvedValue({
      previews: {},
      thumbnails: {},
    });
    replicaLive.uploadReferenceAudioMaterial.mockReset();
    replicaLive.uploadOralAudioMaterial.mockReset();
    replicaLive.validateOralAudioFile.mockReset();
    replicaLive.readAudioDuration.mockResolvedValue(42);
    replicaLive.validateOralAudioFile.mockReturnValue(undefined);
    replicaApi.getAssetDownloadUrl.mockImplementation(async (assetId) => ({
      url: `https://signed.example/${assetId}.png`,
    }));
  });

  it("sizes the replica columns from the current source video and resets on replacement", () => {
    const value = studio();
    const asset = {
      id: "source-1",
      name: "来源视频".repeat(40),
      kind: "video" as const,
      url: "/portrait.mp4",
      group: "项目",
      source: "上传",
      saved: true,
    };
    value.data.assets.push(asset);
    useStudio.mockReturnValue(value);
    const view = render(<ReplicaPage />);
    const row = view.container.querySelector(".media-row") as HTMLElement;
    const video = row.querySelector("video");
    if (!video) throw new Error("replica source preview missing");
    Object.defineProperties(video, {
      videoWidth: { value: 1920 },
      videoHeight: { value: 1080 },
    });
    fireEvent.loadedMetadata(video);
    expect(row.style.getPropertyValue("--row-ratio")).toBe(String(1920 / 1080));
    asset.url = "/next.mp4";
    view.rerender(<ReplicaPage />);
    expect(row.style.getPropertyValue("--row-ratio")).toBe(String(9 / 16));
  });

  it("提取原文后不显示二创编辑框或终稿按钮", () => {
    const value = studio();
    value.state.draft.script = {
      ...value.state.draft.script,
      text: "原始文案",
      confirmed: false,
      resultKind: "extracted",
    };
    useStudio.mockReturnValue(value);
    render(<CopyPage />);
    expect(screen.getByLabelText("来源原文")).toHaveValue("原始文案");
    expect(screen.queryByLabelText("二创文案")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "确认终稿" }),
    ).not.toBeInTheDocument();
  });

  it("按要求二创无需人物，向同一个接口传原文和补充要求", async () => {
    const value = studio({ review: false });
    value.state.draft = {
      ...value.state.draft,
      ipId: undefined,
      rewriteMethod: "custom",
      rewriteInstructions: "更口语化",
      rewriteLength: "200",
    };
    replicaApi.rewriteProjectScript.mockResolvedValue({
      id: "custom-1",
      project_id: "project-1",
      identity_id: null,
      source_asset_id: "source-1",
      source_text: "原始文案",
      instructions: "目标约 200 字。\n更口语化",
      status: "SUCCEEDED",
      result: { rewritten_text: "按要求生成的文案" },
    });
    useStudio.mockReturnValue(value);
    render(<CopyPage />);
    fireEvent.click(screen.getByRole("button", { name: "生成二创文案" }));
    expect(replicaApi.rewriteProjectScript).toHaveBeenCalledWith(
      "project-1",
      "原始文案",
      undefined,
      "source-1",
      expect.any(String),
      "目标约 200 字。\n更口语化",
    );
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith(
        expect.objectContaining({
          script: expect.objectContaining({
            text: "按要求生成的文案",
            resultKind: "rewritten",
          }),
        }),
      ),
    );
  });

  it.each([450, 5000])(
    "文案字数可选自定义，%s 字随改写要求提交",
    async (wordCount) => {
      const value = studio({ review: false });
      value.state.draft.rewriteMethod = "custom";
      value.state.draft.rewriteInstructions = "更口语化";
      value.patchDraft = vi.fn((patch) => {
        value.state.draft = { ...value.state.draft, ...patch };
      });
      replicaApi.rewriteProjectScript.mockReturnValue(new Promise(() => {}));
      useStudio.mockReturnValue(value);
      const view = render(<CopyPage />);
      expect(screen.queryByText("目标长度")).not.toBeInTheDocument();
      fireEvent.change(screen.getByLabelText("文案字数"), {
        target: { value: "custom" },
      });
      view.rerender(<CopyPage />);
      expect(
        screen.getByRole("button", { name: "生成二创文案" }),
      ).toBeDisabled();
      fireEvent.change(screen.getByLabelText("自定义文案字数"), {
        target: { value: String(wordCount) },
      });
      view.rerender(<CopyPage />);
      expect(screen.getByLabelText("自定义文案字数")).toHaveValue(wordCount);
      fireEvent.click(screen.getByRole("button", { name: "生成二创文案" }));
      expect(replicaApi.rewriteProjectScript).toHaveBeenCalledWith(
        "project-1",
        "原始文案",
        undefined,
        "source-1",
        expect.any(String),
        `目标约 ${wordCount} 字。\n更口语化`,
      );
    },
  );

  it.each([undefined, 0, -1, 1.5, 5001])(
    "自定义字数 %s 不合法时禁止生成",
    (wordCount) => {
      const value = studio({ review: false });
      value.state.draft.rewriteMethod = "custom";
      value.state.draft.rewriteInstructions = "更口语化";
      value.state.draft.rewriteLength = "custom";
      value.state.draft.rewriteWordCount = wordCount;
      useStudio.mockReturnValue(value);
      render(<CopyPage />);
      expect(screen.getByLabelText("自定义文案字数")).toHaveAttribute(
        "aria-invalid",
        "true",
      );
      expect(
        screen.getByRole("button", { name: "生成二创文案" }),
      ).toBeDisabled();
      expect(replicaApi.rewriteProjectScript).not.toHaveBeenCalled();
    },
  );

  it("已有结果重新生成后离开并返回可恢复，人工改稿转为候选", async () => {
    const value = studio({ review: false });
    value.state.draft.rewriteMethod = "custom";
    value.state.draft.rewriteInstructions = "更口语化";
    const task = {
      id: "recover-new",
      project_id: "project-1",
      identity_id: null,
      source_asset_id: "source-1",
      source_text: "原始文案",
      instructions: "更口语化",
      status: "RUNNING",
      result: null,
    };
    replicaApi.rewriteProjectScript.mockResolvedValue(task);
    replicaApi.waitForScriptRewriteTask.mockReturnValueOnce(
      new Promise(() => {}),
    );
    useStudio.mockReturnValue(value);
    const view = render(<CopyPage />);
    fireEvent.click(screen.getByRole("button", { name: "生成二创文案" }));
    await waitFor(() =>
      expect(replicaApi.waitForScriptRewriteTask).toHaveBeenCalledWith(
        "recover-new",
      ),
    );
    view.unmount();
    // 云端仍是提交前草稿，而本地已收到服务端 taskId。
    const acceptedPending = JSON.parse(
      sessionStorage.getItem("studio:pending-copy:customer-1") ?? "null",
    );
    expect(acceptedPending.taskId).toBe("recover-new");
    value.state.draft.pendingRewrite = {
      ...acceptedPending,
      taskId: undefined,
    };
    value.state.draft.script = {
      ...value.state.draft.script,
      text: "离开期间人工修改",
    };
    replicaApi.getScriptRewriteTask.mockResolvedValue({
      ...task,
      status: "SUCCEEDED",
      result: { rewritten_text: "恢复的新结果" },
    });
    const restoredView = render(<CopyPage />);
    await waitFor(() =>
      expect(replicaApi.getScriptRewriteTask).toHaveBeenCalledWith(
        "recover-new",
      ),
    );
    expect(
      await screen.findByRole("button", { name: "应用候选稿" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("二创文案")).toHaveValue("离开期间人工修改");
    value.state.draft.script = {
      ...value.state.draft.script,
      original: "新的来源原文",
    };
    restoredView.rerender(<CopyPage />);
    expect(
      screen.queryByRole("button", { name: "应用候选稿" }),
    ).not.toBeInTheDocument();
  });

  it.each([402, 422])("明确提交失败 %s 时清除待恢复记录", async (status) => {
    const value = studio({ review: false });
    replicaApi.rewriteProjectScript.mockRejectedValue({
      status,
      message: "未受理",
    });
    useStudio.mockReturnValue(value);
    render(<CopyPage />);
    fireEvent.click(screen.getByRole("button", { name: "生成二创文案" }));
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        pendingRewrite: undefined,
      }),
    );
    expect(sessionStorage.getItem("studio:pending-copy:customer-1")).toBeNull();
  });

  it.each([false, true])(
    "已受理任务失败后根据 retryable=%s 决定复用请求编号",
    async (retryable) => {
      const value = studio({ review: false });
      value.state.draft.rewriteMethod = "custom";
      value.state.draft.rewriteInstructions = "简洁表达";
      vi.mocked(value.patchDraft).mockImplementation(
        (patch: Partial<typeof value.state.draft>) => {
          Object.assign(value.state.draft, patch);
        },
      );
      const task = {
        id: "failed-task",
        project_id: "project-1",
        identity_id: null,
        source_asset_id: "source-1",
        source_text: "原始文案",
        instructions: "简洁表达",
        status: "FAILED",
        result: null,
        retryable,
        error_code: "DEEPSEEK_REQUEST_FAILED",
        error_message: "配置需要检查",
      };
      replicaApi.rewriteProjectScript.mockResolvedValue(task);
      useStudio.mockReturnValue(value);
      render(<CopyPage />);
      fireEvent.click(screen.getByRole("button", { name: "生成二创文案" }));
      await waitFor(() =>
        expect(value.notify).toHaveBeenCalledWith(
          "文案优化服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
        ),
      );
      const key = replicaApi.rewriteProjectScript.mock.calls[0]?.[4];
      if (!retryable) {
        expect(value.state.draft.pendingRewrite).toBeUndefined();
        expect(
          sessionStorage.getItem("studio:pending-copy:customer-1"),
        ).toBeNull();
      }
      fireEvent.click(screen.getByRole("button", { name: "生成二创文案" }));
      await waitFor(() =>
        expect(replicaApi.rewriteProjectScript).toHaveBeenCalledTimes(2),
      );
      const retriedKey = replicaApi.rewriteProjectScript.mock.calls[1]?.[4];
      if (retryable) expect(retriedKey).toBe(key);
      else expect(retriedKey).not.toBe(key);
    },
  );

  it("响应丢失的请求重进页面不误取历史任务，重试沿用幂等键", async () => {
    const value = studio({ review: false });
    replicaApi.rewriteProjectScript.mockRejectedValue(new Error("网络中断"));
    useStudio.mockReturnValue(value);
    const view = render(<CopyPage />);
    fireEvent.click(screen.getByRole("button", { name: "生成二创文案" }));
    await waitFor(() => expect(value.notify).toHaveBeenCalledWith("网络中断"));
    const key = replicaApi.rewriteProjectScript.mock.calls[0]?.[4];
    view.unmount();
    // A new device/session restores the cloud pending record, without the
    // original browser's idempotency-key map.
    value.state.draft.pendingRewrite = JSON.parse(
      sessionStorage.getItem("studio:pending-copy:customer-1") ?? "null",
    );
    expect(value.state.draft.pendingRewrite?.requestKey).toBe(key);
    sessionStorage.clear();
    value.state.draft.script = {
      ...value.state.draft.script,
      version: value.state.draft.script.version + 1,
    };
    replicaApi.getLatestScriptRewriteTask.mockClear();
    render(<CopyPage />);
    expect(replicaApi.getLatestScriptRewriteTask).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "生成二创文案" }));
    await waitFor(() =>
      expect(replicaApi.rewriteProjectScript).toHaveBeenCalledTimes(2),
    );
    expect(replicaApi.rewriteProjectScript.mock.calls[1]?.[4]).toBe(key);
  });

  it("旧 IP 档案任务不得作为当前人物资料的结果恢复", async () => {
    const value = studio({ review: false });
    value.state.draft.script = {
      ...value.state.draft.script,
      text: "",
      confirmed: false,
      resultKind: "extracted",
    };
    replicaApi.getLatestScriptRewriteTask.mockResolvedValue({
      id: "old-profile",
      project_id: "project-1",
      identity_id: "person-1",
      source_asset_id: "source-1",
      source_text: "原始文案",
      status: "SUCCEEDED",
      ip_profile_snapshot: { display_name: "张工", role: "旧职业" },
      result: { rewritten_text: "旧职业稿" },
    });
    useStudio.mockReturnValue(value);
    render(<CopyPage />);
    await waitFor(() =>
      expect(replicaApi.getLatestScriptRewriteTask).toHaveBeenCalled(),
    );
    await Promise.resolve();
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("二创文案")).not.toBeInTheDocument();
  });

  it("文案终稿可带入数字人口播并保留同一草稿", () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    render(<CopyPage />);
    fireEvent.click(screen.getByRole("button", { name: "用于数字人口播" }));
    expect(value.navigate).toHaveBeenCalledWith("oral", { returnTo: "copy" });
  });

  it("未选择人物 IP 时用于数字人口播仍可跳转，并提示进入口播页后补选", () => {
    const base = studio();
    const value = studio({
      state: {
        ...base.state,
        draft: { ...base.state.draft, ipId: undefined },
      },
    });
    useStudio.mockReturnValue(value);
    render(<CopyPage />);
    expect(
      screen.getByText("未选择人物 IP，进入数字人口播页后可选择"),
    ).toBeInTheDocument();
    const oralButton = screen.getByRole("button", { name: "用于数字人口播" });
    expect(oralButton).toBeEnabled();
    fireEvent.click(oralButton);
    expect(value.navigate).toHaveBeenCalledWith("oral", { returnTo: "copy" });
  });

  it("审计员可查看创作内容但文案、视频与口播提交控件只读", () => {
    const value = studio({
      review: false,
      user: {
        id: "auditor-1",
        username: "auditor-1",
        display_name: "审计员",
        role: "auditor",
      },
    });
    value.state.savedScripts = [
      {
        id: "saved-audit-script",
        title: "审计历史文案",
        original: "历史原稿",
        text: "历史终稿",
        version: 2,
        confirmed: true,
      },
    ];
    useStudio.mockReturnValue(value);
    const view = render(<CopyPage />);

    expect(screen.getByLabelText("二创文案")).toBeDisabled();
    expect(screen.getByRole("button", { name: "保存版本" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "确认终稿" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "更换人物" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "更换人物" }));
    expect(value.openPicker).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("tab", { name: "我的文案" }));
    const savedScript = screen.getByRole("button", {
      name: /审计历史文案/,
    });
    expect(savedScript).toBeDisabled();
    fireEvent.click(savedScript);
    expect(value.patchDraft).not.toHaveBeenCalled();

    value.state = { ...value.state, page: "video" };
    view.rerender(<VideoPage />);
    expect(screen.getByLabelText("提示词")).toBeDisabled();
    expect(screen.getByRole("button", { name: "生成视频" })).toBeDisabled();

    value.data.assets.push({
      id: "reference-video",
      name: "参考运镜.mp4",
      kind: "video",
      group: "参考素材",
      source: "素材库",
      saved: true,
    });
    value.state = {
      ...value.state,
      page: "reference",
      draft: {
        ...value.state.draft,
        referenceIds: ["reference-1", "reference-video", "ghost-reference"],
      },
    };
    view.rerender(<VideoPage />);
    expect(screen.getByRole("button", { name: "整理参考素材" })).toBeDisabled();
    vi.mocked(value.patchDraft).mockClear();
    fireEvent.click(screen.getByRole("button", { name: "整理参考素材" }));
    expect(value.patchDraft).not.toHaveBeenCalled();

    value.state = { ...value.state, page: "oral" };
    view.rerender(<OralPage />);
    expect(screen.getByRole("button", { name: "生成口播视频" })).toBeDisabled();
    expect(value.saveDraft).not.toHaveBeenCalled();
    expect(value.confirmFinalDraft).not.toHaveBeenCalled();
    expect(value.requestGeneration).not.toHaveBeenCalled();
  });

  it("复刻入口在同一纵向长页展示三个业务区域", () => {
    const value = studio();
    value.state = { ...value.state, page: "replica" };
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);
    expect(
      screen.getByRole("heading", { name: "1 视频拆解" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "2 首帧置换" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "3 文案与生成" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /1 视频拆解/ })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /下一步|返回首帧/ }),
    ).toBeNull();
  });

  it("历史人物置换链接兼容进入同一复刻长页", () => {
    const value = replacementStudio();
    useStudio.mockReturnValue(value);

    render(<ReplacementPage />);

    expect(
      screen.getByRole("heading", { name: "1 视频拆解" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "2 首帧置换" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "3 文案与生成" }),
    ).toBeInTheDocument();
  });

  it("视频复刻优先显示草稿来源，不被历史列表选择覆盖", () => {
    const value = studio();
    value.data.assets.push({
      id: "source-1",
      name: "当前草稿来源",
      kind: "video",
      duration: "00:08",
      group: "项目",
      source: "用户上传",
      saved: true,
    });
    value.data.videos.push({
      id: "history-video",
      title: "历史浏览视频",
      author: "作者",
      platform: "抖音",
      category: "建房预算",
      poster: "",
      duration: "00:30",
      likes: 1,
      collections: 0,
      shares: 0,
      description: "历史内容",
    });
    value.state = { ...value.state, selectedVideoId: "history-video" };
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);
    // 参考视频面板不再显示来源标注行，改由播放器的时长角标观察选中的是哪一个：
    // 两个候选都无封面，Media 因此各自渲染自己的时长，优先级一错就会翻成 00:30。
    expect(screen.getByText("00:08")).toBeInTheDocument();
    expect(screen.queryByText("00:30")).not.toBeInTheDocument();
  });

  it("审核模式展示完整三镜头并用当前IP人物图作为目标首帧", () => {
    const value = studio();
    value.data.assets.push({
      id: "source-1",
      name: "来源视频",
      kind: "video",
      group: "项目",
      source: "上传",
      saved: true,
    });
    const target = value.data.assets.find((asset) => asset.id === "target-1");
    const frame = value.data.assets.find((asset) => asset.id === "frame-1");
    if (target) target.url = "/target-person.png";
    if (frame) frame.url = "/unrelated-frame.png";
    useStudio.mockReturnValue(value);

    const { container } = render(<ReplicaPage />);

    expect(container.querySelectorAll(".creation-shot-card")).toHaveLength(0);
    // 摘要卡片与完整分镜合并为一张可编辑表后，镜头内容落在单元格输入框里。
    expect(container.querySelectorAll(".shot-table tbody tr")).toHaveLength(3);
    expect(screen.getByDisplayValue("镜头缓推庭院")).toBeInTheDocument();
    expect(screen.getByDisplayValue("人物出镜讲解")).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: "运动" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("最终提示词")).toBeInTheDocument();
  });

  it("文案工坊可直接更换参与二创的人物IP", () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    render(<CopyPage />);

    fireEvent.click(screen.getByRole("button", { name: "更换人物" }));

    expect(value.openPicker).toHaveBeenCalledWith("person");
  });

  it("按当前项目、来源原文和人物IP发起异步二创", async () => {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      draft: { ...value.state.draft, scriptEdited: false },
    };
    replicaApi.rewriteProjectScript.mockResolvedValue({
      id: "rewrite-1",
      project_id: "project-1",
      identity_id: "person-1",
      source_asset_id: "source-1",
      source_text: "原始文案",
      ip_profile_snapshot: {
        display_name: "张工",
        role: "乡墅设计师",
        service_scope: "乡墅设计",
        target_audience: "自建房家庭",
        expression_style: "专业通俗",
        profile_version: 7,
      },
      status: "PENDING",
      result: null,
    });
    replicaApi.waitForScriptRewriteTask.mockResolvedValue({
      id: "rewrite-1",
      project_id: "project-1",
      identity_id: "person-1",
      source_asset_id: "source-1",
      source_text: "原始文案",
      ip_profile_snapshot: {
        display_name: "张工",
        role: "乡墅设计师",
        service_scope: "乡墅设计",
        target_audience: "自建房家庭",
        expression_style: "专业通俗",
        profile_version: 7,
      },
      status: "SUCCEEDED",
      result: { rewritten_text: "张工定位的二创稿" },
    });
    useStudio.mockReturnValue(value);
    render(<CopyPage />);

    fireEvent.click(screen.getByRole("button", { name: "生成二创文案" }));

    expect(replicaApi.rewriteProjectScript).toHaveBeenCalledWith(
      "project-1",
      "原始文案",
      "person-1",
      "source-1",
      expect.any(String),
      "",
    );
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        pendingRewrite: undefined,
        rewriteCandidate: undefined,
        script: expect.objectContaining({
          text: "张工定位的二创稿",
          confirmed: false,
        }),
        scriptEdited: true,
      }),
    );
  });

  it.each([
    ["缺少来源项目", { projectId: undefined }, /来源项目/],
    ["缺少来源素材", { sourceId: undefined }, /来源视频/],
    ["缺少人物IP", { ipId: undefined }, /人物 IP/],
    [
      "正文为空",
      { script: { ...studio().state.draft.script, text: "", original: "" } },
      /待改写正文/,
    ],
  ])("%s时禁用二创并显示原因", (_name, draftPatch, message) => {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      draft: { ...value.state.draft, ...draftPatch },
    };
    useStudio.mockReturnValue(value);
    render(<CopyPage />);

    expect(screen.getByRole("button", { name: "生成二创文案" })).toBeDisabled();
    expect(screen.getAllByText(message).length).toBeGreaterThan(0);
    expect(replicaApi.rewriteProjectScript).not.toHaveBeenCalled();
  });

  it("改写失败保留原稿并可重试", async () => {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      draft: { ...value.state.draft, scriptEdited: false },
    };
    replicaApi.rewriteProjectScript
      .mockRejectedValueOnce(new Error("临时失败"))
      .mockResolvedValueOnce({
        id: "rewrite-retry",
        project_id: "project-1",
        identity_id: "person-1",
        source_asset_id: "source-1",
        source_text: "原始文案",
        ip_profile_snapshot: {
          display_name: "张工",
          role: "乡墅设计师",
          service_scope: "乡墅设计",
          target_audience: "自建房家庭",
          expression_style: "专业通俗",
          profile_version: 7,
        },
        status: "SUCCEEDED",
        result: { rewritten_text: "重试成功稿" },
      });
    useStudio.mockReturnValue(value);
    render(<CopyPage />);
    const rewrite = screen.getByRole("button", { name: "生成二创文案" });

    fireEvent.click(rewrite);
    await waitFor(() => expect(value.notify).toHaveBeenCalledWith("临时失败"));
    expect(
      vi.mocked(value.patchDraft).mock.calls.some(([patch]) => patch.script),
    ).toBe(false);
    expect(screen.getByLabelText("二创文案")).toHaveValue(
      "已确认的乡墅口播终稿",
    );

    fireEvent.click(rewrite);
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith(
        expect.objectContaining({
          script: expect.objectContaining({ text: "重试成功稿" }),
        }),
      ),
    );
    expect(replicaApi.rewriteProjectScript).toHaveBeenCalledTimes(2);
    expect(replicaApi.rewriteProjectScript.mock.calls[1]?.[4]).toBe(
      replicaApi.rewriteProjectScript.mock.calls[0]?.[4],
    );
  });

  it("仅恢复同人物且同正文的未完成改写任务", async () => {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      draft: {
        ...value.state.draft,
        scriptEdited: false,
        script: { ...value.state.draft.script, text: "", confirmed: false },
      },
    };
    replicaApi.getLatestScriptRewriteTask.mockResolvedValue({
      id: "rewrite-restored",
      project_id: "project-1",
      identity_id: "person-1",
      source_asset_id: "source-1",
      source_text: "原始文案",
      ip_profile_snapshot: {
        display_name: "张工",
        role: "乡墅设计师",
        service_scope: "乡墅设计",
        target_audience: "自建房家庭",
        expression_style: "专业通俗",
        profile_version: 7,
      },
      status: "RUNNING",
      result: null,
    });
    replicaApi.waitForScriptRewriteTask.mockResolvedValue({
      id: "rewrite-restored",
      project_id: "project-1",
      identity_id: "person-1",
      source_asset_id: "source-1",
      source_text: "原始文案",
      ip_profile_snapshot: {
        display_name: "张工",
        role: "乡墅设计师",
        service_scope: "乡墅设计",
        target_audience: "自建房家庭",
        expression_style: "专业通俗",
        profile_version: 7,
      },
      status: "SUCCEEDED",
      result: { rewritten_text: "恢复完成的同稿结果" },
    });
    useStudio.mockReturnValue(value);
    render(<CopyPage />);

    expect(replicaApi.getLatestScriptRewriteTask).toHaveBeenCalledWith(
      "project-1",
      "person-1",
      "source-1",
    );
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith(
        expect.objectContaining({
          script: expect.objectContaining({ text: "恢复完成的同稿结果" }),
        }),
      ),
    );
  });

  it("A到B再回A后旧人物请求不得覆盖当前稿", async () => {
    let resolveOld: ((value: unknown) => void) | undefined;
    const oldResult = new Promise((resolve) => {
      resolveOld = resolve;
    });
    replicaApi.rewriteProjectScript.mockResolvedValue({
      id: "rewrite-old",
      project_id: "project-1",
      identity_id: "person-1",
      source_asset_id: "source-1",
      source_text: "原始文案",
      ip_profile_snapshot: {
        display_name: "张工",
        role: "乡墅设计师",
        service_scope: "乡墅设计",
        target_audience: "自建房家庭",
        expression_style: "专业通俗",
        profile_version: 7,
      },
      status: "PENDING",
      result: null,
    });
    replicaApi.waitForScriptRewriteTask.mockReturnValue(oldResult);
    const original = studio({ review: false });
    original.state = {
      ...original.state,
      draft: { ...original.state.draft, scriptEdited: false },
    };
    let current = original;
    useStudio.mockImplementation(() => current);
    const view = render(<CopyPage />);
    fireEvent.click(screen.getByRole("button", { name: "生成二创文案" }));
    await waitFor(() =>
      expect(replicaApi.waitForScriptRewriteTask).toHaveBeenCalled(),
    );

    current = {
      ...original,
      state: {
        ...original.state,
        draft: { ...original.state.draft, ipId: "person-2" },
      },
    };
    view.rerender(<CopyPage />);
    current = original;
    view.rerender(<CopyPage />);
    resolveOld?.({
      id: "rewrite-old",
      project_id: "project-1",
      identity_id: "person-1",
      source_asset_id: "source-1",
      source_text: "原始文案",
      ip_profile_snapshot: {
        display_name: "张工",
        role: "乡墅设计师",
        service_scope: "乡墅设计",
        target_audience: "自建房家庭",
        expression_style: "专业通俗",
        profile_version: 7,
      },
      status: "SUCCEEDED",
      result: { rewritten_text: "迟到的旧A稿" },
    });

    await Promise.resolve();
    expect(
      vi.mocked(original.patchDraft).mock.calls.some(([patch]) => patch.script),
    ).toBe(false);
    expect(original.notify).not.toHaveBeenCalledWith(
      expect.stringContaining("完成"),
    );
  });

  it("同项目同人物同正文切换来源后不得恢复旧来源任务", async () => {
    let resolveOld: ((value: unknown) => void) | undefined;
    replicaApi.getLatestScriptRewriteTask.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
    );
    const sourceA = studio({ review: false });
    sourceA.state = {
      ...sourceA.state,
      draft: {
        ...sourceA.state.draft,
        sourceId: "source-a",
        sourceAssetId: "source-a",
        scriptEdited: false,
      },
    };
    const sourceB = {
      ...sourceA,
      state: {
        ...sourceA.state,
        draft: {
          ...sourceA.state.draft,
          sourceId: "source-b",
          sourceAssetId: "source-b",
        },
      },
    };
    let current = sourceA;
    useStudio.mockImplementation(() => current);
    const view = render(<CopyPage />);
    current = sourceB;
    view.rerender(<CopyPage />);
    resolveOld?.({
      id: "rewrite-source-a",
      project_id: "project-1",
      identity_id: "person-1",
      source_asset_id: "source-a",
      source_text: "原始文案",
      ip_profile_snapshot: {
        display_name: "张工",
        role: "乡墅设计师",
        service_scope: "乡墅设计",
        target_audience: "自建房家庭",
        expression_style: "专业通俗",
        profile_version: 7,
      },
      status: "RUNNING",
      result: null,
    });

    await Promise.resolve();
    expect(replicaApi.waitForScriptRewriteTask).not.toHaveBeenCalled();
    expect(sourceA.patchDraft).not.toHaveBeenCalled();
  });

  it("审核示例与只读账号不调用改写接口", () => {
    const reviewValue = studio({ review: true });
    useStudio.mockReturnValue(reviewValue);
    const view = render(<CopyPage />);
    expect(screen.getByRole("button", { name: "生成二创文案" })).toBeDisabled();
    expect(screen.getByText(/审核示例/)).toBeInTheDocument();
    view.unmount();

    const auditor = studio({
      review: false,
      user: {
        id: "auditor-1",
        username: "audit",
        display_name: "审核",
        role: "auditor",
      },
    });
    auditor.state = {
      ...auditor.state,
      draft: { ...auditor.state.draft, scriptEdited: false },
    };
    useStudio.mockReturnValue(auditor);
    render(<CopyPage />);
    expect(screen.getByRole("button", { name: "生成二创文案" })).toBeDisabled();
    expect(screen.getByText(/只读权限/)).toBeInTheDocument();
    expect(replicaApi.rewriteProjectScript).not.toHaveBeenCalled();
  });

  function replacementStudio() {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "replacement",
      draft: { ...value.state.draft, projectId: "project-1" },
    };
    value.data = {
      ...value.data,
      projects: [
        {
          id: "project-1",
          name: "替换测试项目",
          owner_user_id: "employee_1",
          status: "ACTIVE",
          reference_asset_id: "asset-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        },
      ],
    };
    return value;
  }

  it("人物替换：无项目时引导先准备项目", () => {
    const value = studio({ review: false });
    value.state = { ...value.state, page: "replacement" };
    value.data = { ...value.data, projects: [] };
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);
    expect(screen.getByText(/先在视频复刻中准备好项目/)).toBeInTheDocument();
  });

  it("人物替换：首次挂载保留当前项目已确认首帧", () => {
    const value = replacementStudio();
    value.state.draft.firstFrameId = "frame-1";
    value.state.draft.firstFrameSelectionVersionId = "ffv-saved";
    useStudio.mockReturnValue(value);

    render(<ReplacementPage />);

    expect(value.patchDraft).not.toHaveBeenCalledWith(
      expect.objectContaining({ firstFrameId: undefined }),
    );
  });

  it("人物替换：切换项目时清理上一项目的首帧确认", () => {
    const value = replacementStudio();
    useStudio.mockReturnValue(value);
    const view = render(<ReplacementPage />);
    vi.mocked(value.patchDraft).mockClear();
    value.data.projects.push({
      ...value.data.projects[0],
      id: "project-2",
      name: "替换测试项目 2",
    });
    value.state = {
      ...value.state,
      draft: { ...value.state.draft, projectId: "project-2" },
    };

    view.rerender(<ReplacementPage />);

    expect(value.patchDraft).toHaveBeenCalledWith(
      expect.objectContaining({
        firstFrameId: undefined,
        firstFrameSelectionVersionId: undefined,
        frameConfirmed: false,
      }),
    );
  });

  it("人物替换：审核模式在工作台内展示完整三段示例", () => {
    const value = replacementStudio();
    value.review = true;
    useStudio.mockReturnValue(value);

    const { container } = render(<ReplacementPage />);

    expect(screen.getByText("源画面")).toBeInTheDocument();
    expect(screen.getByText("场景形象")).toBeInTheDocument();
    const sceneSelect = screen.getByLabelText("人物场景形象");
    expect(sceneSelect).toBeDisabled();
    expect(
      within(sceneSelect).getByRole("option", { name: /张工·.*庭院讲解/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("置换首帧")).toBeInTheDocument();
    expect(screen.getAllByText(/审核示例/).length).toBeGreaterThan(0);
    expect(
      container.querySelectorAll(".creation-review-candidates figure"),
    ).toHaveLength(3);
    // 三段示例与最终预览都改由媒体行承载，不再使用旧的 stage/final 网格。
    expect(container.querySelector(".creation-replica-video")).not.toBeNull();
    expect(container.querySelector(".creation-final-preview")).not.toBeNull();
    const analysisPanel = container.querySelector(".creation-replica-analyze");
    expect(analysisPanel).not.toBeNull();
    // 拆解控制只保留标题与操作，镜头细节全部落在同一张可编辑分镜表里。
    const shotList = container.querySelector(".creation-shot-list");
    expect(shotList).not.toBeNull();
    expect(
      within(shotList as HTMLElement).getByDisplayValue("推进"),
    ).toBeInTheDocument();
    expect(
      within(shotList as HTMLElement).getByDisplayValue("中景"),
    ).toBeInTheDocument();
    expect(
      within(shotList as HTMLElement).getAllByDisplayValue("切镜"),
    ).toHaveLength(3);
    expect(
      container.querySelector(
        ".creation-shot-list .creation-shot-summary-list",
      ),
    ).toBeNull();
    // ③ 折叠：完整分镜表默认收起，展开入口文案随之调整。
    expect(
      screen.getByText("展开编辑完整分镜").closest("details"),
    ).not.toHaveAttribute("open");
    expect(
      screen.getByRole("button", { name: "存入我的提示词" }),
    ).toBeDisabled();
    expect(replicaApi.selectCharacterReferences).not.toHaveBeenCalled();
    expect(replicaApi.getLatestGenerationPrompt).not.toHaveBeenCalled();
    expect(value.patchDraft).not.toHaveBeenCalledWith(
      expect.objectContaining({ firstFrameId: undefined }),
    );
  });

  it("人物替换：无需预生成提示词，确认首帧后进入最终合成", async () => {
    const value = replacementStudio();
    replicaApi.selectCharacterReferences.mockResolvedValue({
      id: "crs-1",
      payload: {},
    });
    value.state.draft.prompt = "";
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    await waitFor(() =>
      expect(replicaApi.selectCharacterReferences).toHaveBeenCalledWith(
        "project-1",
        {
          character_version_id: "cv-1",
          source_frame_selection_version_id: "sfv-1",
        },
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "stub-确认置换首帧" }));

    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        firstFrameId: "ff-asset-1",
        firstFrameSelectionVersionId: "ffv-1",
        frameConfirmed: true,
      }),
    );
    expect(
      screen.getByRole("heading", { name: "3 文案与生成" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /下一步/ })).toBeNull();
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("人物替换：同资产新确认版本会更新，撤销后清空交接", async () => {
    const value = replacementStudio();
    replicaApi.selectCharacterReferences.mockResolvedValue({
      id: "crs-1",
      payload: {},
    });
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    await waitFor(() =>
      expect(replicaApi.selectCharacterReferences).toHaveBeenCalled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "stub-确认置换首帧" }));
    fireEvent.click(
      screen.getByRole("button", { name: "stub-更新置换首帧版本" }),
    );

    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        firstFrameId: "ff-asset-1",
        firstFrameSelectionVersionId: "ffv-2",
        frameConfirmed: true,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "stub-撤销置换首帧" }));
    expect(value.patchDraft).toHaveBeenLastCalledWith({
      firstFrameId: undefined,
      firstFrameSelectionVersionId: undefined,
      frameConfirmed: false,
    });
  });

  it("人物参考匹配失败后重试会发起第二次请求并呈现成功结果", async () => {
    const value = replacementStudio();
    replicaApi.selectCharacterReferences
      .mockRejectedValueOnce(new Error("匹配服务暂时不可用"))
      .mockResolvedValueOnce({ id: "crs-retry", payload: {} });
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    expect(await screen.findByText("匹配服务暂时不可用")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重试匹配人物参考" }));

    expect(
      await screen.findByText("stub-参考匹配-crs-retry"),
    ).toBeInTheDocument();
    expect(replicaApi.selectCharacterReferences).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("匹配服务暂时不可用")).toBeNull();
  });

  it("人物参考匹配防重复重试并忽略切换源帧前的迟到结果", async () => {
    let resolveRetry:
      | ((value: { id: string; payload: object }) => void)
      | undefined;
    replicaApi.selectCharacterReferences
      .mockRejectedValueOnce(new Error("首次匹配失败"))
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveRetry = resolve;
          }),
      )
      .mockResolvedValueOnce({ id: "crs-new-source", payload: {} });
    const value = replacementStudio();
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    const retry = await screen.findByRole("button", {
      name: "重试匹配人物参考",
    });
    fireEvent.click(retry);
    fireEvent.click(retry);
    await waitFor(() =>
      expect(replicaApi.selectCharacterReferences).toHaveBeenCalledTimes(2),
    );

    fireEvent.click(screen.getByRole("button", { name: "stub-切换源画面" }));
    expect(
      await screen.findByText("stub-参考匹配-crs-new-source"),
    ).toBeInTheDocument();
    resolveRetry?.({ id: "crs-old-source", payload: {} });
    await Promise.resolve();

    expect(screen.queryByText("stub-参考匹配-crs-old-source")).toBeNull();
    expect(replicaApi.selectCharacterReferences).toHaveBeenLastCalledWith(
      "project-1",
      {
        character_version_id: "cv-1",
        source_frame_selection_version_id: "sfv-2",
      },
    );
  });

  it("同项目 ID 的数据对象刷新不会取消正在进行的匹配", async () => {
    let resolveMatch:
      | ((value: { id: string; payload: object }) => void)
      | undefined;
    replicaApi.selectCharacterReferences.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveMatch = resolve;
        }),
    );
    const initial = replacementStudio();
    let current = initial;
    useStudio.mockImplementation(() => current);
    const view = render(<ReplacementPage />);

    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    await waitFor(() =>
      expect(replicaApi.selectCharacterReferences).toHaveBeenCalledTimes(1),
    );
    current = {
      ...current,
      data: {
        ...current.data,
        projects: current.data.projects.map((project) => ({ ...project })),
      },
    };
    view.rerender(<ReplacementPage />);
    resolveMatch?.({ id: "crs-same-project", payload: {} });

    expect(
      await screen.findByText("stub-参考匹配-crs-same-project"),
    ).toBeInTheDocument();
    expect(replicaApi.selectCharacterReferences).toHaveBeenCalledTimes(1);
  });

  it("源帧 A 切到 B 再返回 A 时会重新匹配并隔离前两次迟到响应", async () => {
    const resolvers: Array<(value: { id: string; payload: object }) => void> =
      [];
    replicaApi.selectCharacterReferences.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolvers.push(resolve);
        }),
    );
    const value = replacementStudio();
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    await waitFor(() => expect(resolvers).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "stub-切换源画面" }));
    await waitFor(() => expect(resolvers).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    await waitFor(() => expect(resolvers).toHaveLength(3));

    resolvers[2]?.({ id: "crs-a-current", payload: {} });
    expect(
      await screen.findByText("stub-参考匹配-crs-a-current"),
    ).toBeInTheDocument();
    resolvers[0]?.({ id: "crs-a-old", payload: {} });
    resolvers[1]?.({ id: "crs-b-old", payload: {} });
    await Promise.resolve();

    expect(screen.queryByText("stub-参考匹配-crs-a-old")).toBeNull();
    expect(screen.queryByText("stub-参考匹配-crs-b-old")).toBeNull();
    expect(replicaApi.selectCharacterReferences).toHaveBeenCalledTimes(3);
  });

  it("源帧 A 匹配已失败后切到 B 再返回 A 会发起新请求", async () => {
    replicaApi.selectCharacterReferences
      .mockRejectedValueOnce(new Error("A 匹配失败"))
      .mockResolvedValueOnce({ id: "crs-b", payload: {} })
      .mockResolvedValueOnce({ id: "crs-a-retry", payload: {} });
    const value = replacementStudio();
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    expect(await screen.findByText("A 匹配失败")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "stub-切换源画面" }));
    expect(await screen.findByText("stub-参考匹配-crs-b")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));

    expect(
      await screen.findByText("stub-参考匹配-crs-a-retry"),
    ).toBeInTheDocument();
    expect(replicaApi.selectCharacterReferences).toHaveBeenCalledTimes(3);
  });

  it("同一源帧版本重复回调不重复匹配也不清除已确认首帧", async () => {
    let resolveMatch:
      | ((value: { id: string; payload: object }) => void)
      | undefined;
    replicaApi.selectCharacterReferences.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveMatch = resolve;
        }),
    );
    const value = replacementStudio();
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    await waitFor(() =>
      expect(replicaApi.selectCharacterReferences).toHaveBeenCalledTimes(1),
    );
    fireEvent.click(screen.getByRole("button", { name: "stub-确认置换首帧" }));
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenLastCalledWith({
        firstFrameId: "ff-asset-1",
        firstFrameSelectionVersionId: "ffv-1",
        frameConfirmed: true,
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    expect(replicaApi.selectCharacterReferences).toHaveBeenCalledTimes(1);
    expect(value.patchDraft).toHaveBeenLastCalledWith({
      firstFrameId: "ff-asset-1",
      firstFrameSelectionVersionId: "ffv-1",
      frameConfirmed: true,
    });

    resolveMatch?.({ id: "crs-same-source", payload: {} });
    expect(
      await screen.findByText("stub-参考匹配-crs-same-source"),
    ).toBeInTheDocument();
  });

  it("视频页按确认首帧 ID 恢复签名预览并交给生成请求", async () => {
    const value = studio({ review: false });
    value.videoCapabilitiesStatus = "ready";
    value.videoCapabilities = {
      extended_modes_enabled: false,
      t2v_enabled: false,
      i2v_enabled: true,
      r2v_enabled: false,
      last_frame_enabled: false,
      max_reference_images: 8,
      max_reference_videos: 3,
      max_reference_audios: 3,
      max_quantity: 4,
    };
    value.state = {
      ...value.state,
      page: "video",
      draft: {
        ...value.state.draft,
        firstFrameId: "ff-asset-new",
        firstFrameSelectionVersionId: "ffv-new",
        frameConfirmed: true,
      },
    };
    value.data = {
      ...value.data,
      assets: value.data.assets.filter((asset) => asset.id !== "frame-1"),
    };
    value.updateData = vi.fn((update) => {
      value.data = update(value.data);
    });
    useStudio.mockReturnValue(value);

    render(<VideoPage />);

    expect(screen.getByRole("button", { name: "生成视频" })).toBeDisabled();
    expect(
      await screen.findByRole("img", { name: "首帧预览" }),
    ).toHaveAttribute("src", "https://signed.example/ff-asset-new.png");
    expect(replicaApi.getAssetDownloadUrl).toHaveBeenCalledWith("ff-asset-new");
    expect(value.data.assets[0]).toMatchObject({
      id: "ff-asset-new",
      assetId: "ff-asset-new",
      kind: "image",
      source: "人物置换",
    });

    const generate = screen.getByRole("button", { name: "生成视频" });
    expect(generate).toBeEnabled();
    fireEvent.click(generate);
    expect(value.requestGeneration).toHaveBeenCalledWith("视频生成");
    expect(value.state.draft.firstFrameId).toBe("ff-asset-new");
  });

  it("首帧 ID 切换后忽略旧签名请求的迟到响应", async () => {
    let resolveOld: ((value: { url: string }) => void) | undefined;
    let resolveNew: ((value: { url: string }) => void) | undefined;
    replicaApi.getAssetDownloadUrl.mockImplementation(
      (assetId: string) =>
        new Promise((resolve) => {
          if (assetId === "ff-old") resolveOld = resolve;
          if (assetId === "ff-new") resolveNew = resolve;
        }),
    );
    const initial = studio({ review: false });
    initial.state = {
      ...initial.state,
      page: "video",
      draft: { ...initial.state.draft, firstFrameId: "ff-old" },
    };
    initial.data = {
      ...initial.data,
      assets: initial.data.assets.filter((asset) => asset.id !== "frame-1"),
    };
    let current = initial;
    current.updateData = vi.fn((update) => {
      current.data = update(current.data);
    });
    useStudio.mockImplementation(() => current);
    const view = render(<VideoPage />);

    current = {
      ...current,
      state: {
        ...current.state,
        draft: { ...current.state.draft, firstFrameId: "ff-new" },
      },
    };
    view.rerender(<VideoPage />);
    resolveNew?.({ url: "https://signed.example/new.png" });
    expect(
      await screen.findByRole("img", { name: "首帧预览" }),
    ).toHaveAttribute("src", "https://signed.example/new.png");

    resolveOld?.({ url: "https://signed.example/old.png" });
    await Promise.resolve();
    expect(screen.getByRole("img", { name: "首帧预览" })).toHaveAttribute(
      "src",
      "https://signed.example/new.png",
    );
    expect(current.data.assets.some((asset) => asset.id === "ff-old")).toBe(
      false,
    );
  });

  it("首帧签名读取失败后可重试并恢复预览", async () => {
    replicaApi.getAssetDownloadUrl
      .mockRejectedValueOnce(new Error("签名服务暂时不可用"))
      .mockResolvedValueOnce({ url: "https://signed.example/retried.png" });
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "video",
      draft: { ...value.state.draft, firstFrameId: "ff-retry" },
    };
    value.data = {
      ...value.data,
      assets: value.data.assets.filter((asset) => asset.id !== "frame-1"),
    };
    value.data.assets.push({
      id: "ff-retry",
      assetId: "ff-retry",
      name: "待恢复的已确认首帧",
      kind: "image",
      group: "置换首帧",
      source: "人物置换",
      saved: true,
    });
    useStudio.mockReturnValue(value);

    render(<VideoPage />);

    expect(await screen.findByText("首帧预览加载失败")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试加载首帧" }));
    expect(
      await screen.findByRole("img", { name: "首帧预览" }),
    ).toHaveAttribute("src", "https://signed.example/retried.png");
    expect(replicaApi.getAssetDownloadUrl).toHaveBeenCalledTimes(2);
  });

  it("参考模式未开放时可主动刷新而不提交付费任务", () => {
    const value = studio();
    const retry = vi.fn();
    value.state = { ...value.state, page: "reference" };
    useStudio.mockReturnValue({
      ...value,
      review: false,
      videoCapabilitiesStatus: "ready",
      videoCapabilities: {
        extended_modes_enabled: false,
        t2v_enabled: false,
        i2v_enabled: true,
        r2v_enabled: false,
        last_frame_enabled: false,
        max_reference_images: 8,
        max_reference_videos: 3,
        max_reference_audios: 3,
        max_quantity: 4,
      },
      retryVideoCapabilities: retry,
    });
    render(<VideoPage />);
    fireEvent.click(screen.getByRole("button", { name: "刷新开放状态" }));
    expect(retry).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "生成视频" })).toBeDisabled();
  });

  it.each(["loading", "error", "closed", "closed-image", "open-image"])(
    "文图生视频按能力状态决定能否提交：%s",
    (state) => {
      const value = studio({ review: false });
      value.state = {
        ...value.state,
        page: "video",
        draft: {
          ...value.state.draft,
          firstFrameId: state.endsWith("image") ? "frame-1" : undefined,
        },
      };
      value.data.assets = value.data.assets.map((asset) =>
        asset.id === "frame-1"
          ? { ...asset, url: "https://assets.example/frame.png" }
          : asset,
      );
      value.videoCapabilitiesStatus =
        state === "loading" || state === "error" ? state : "ready";
      value.videoCapabilities =
        state === "loading"
          ? undefined
          : {
              extended_modes_enabled: false,
              t2v_enabled: false,
              i2v_enabled: state !== "closed-image",
              r2v_enabled: false,
              last_frame_enabled: false,
              max_reference_images: 8,
              max_reference_videos: 3,
              max_reference_audios: 3,
              max_quantity: 4,
            };
      value.retryVideoCapabilities = vi.fn();
      useStudio.mockReturnValue(value);
      render(<VideoPage />);
      const submit = screen.getByRole("button", { name: "生成视频" });
      if (state === "open-image") {
        expect(submit).toBeEnabled();
      } else {
        expect(submit).toBeDisabled();
        if (state.startsWith("closed") || state === "error") {
          fireEvent.click(
            screen.getByRole("button", {
              name: state.startsWith("closed")
                ? "刷新开放状态"
                : "重试读取视频能力",
            }),
          );
          expect(value.retryVideoCapabilities).toHaveBeenCalledOnce();
        }
      }
    },
  );

  it.each(["completed", "cancelled"] as const)(
    "终态 %s 不继续显示等待提示",
    (status) => {
      const value = studio();
      value.state = {
        ...value.state,
        page: "video",
        draft: {
          ...value.state.draft,
          firstFrameId: undefined,
          videoBatchId: "done",
        },
      };
      value.data = {
        ...value.data,
        tasks: [
          {
            id: "done",
            backendKind: "generation_batch",
            backendId: "done",
            title: "庭院",
            type: "视频生成",
            status,
            submitted: "2026-09-15T03:45:09+00:00",
          },
        ],
      };
      useStudio.mockReturnValue(value);
      render(<VideoPage />);
      if (status === "completed") {
        fireEvent.click(screen.getByRole("button", { name: "查看成片" }));
        expect(value.navigate).toHaveBeenCalledWith(
          "task-detail",
          expect.objectContaining({
            selectedTaskId: "done",
            selectedTaskBackendId: "done",
            returnTo: "video",
          }),
        );
      } else {
        expect(screen.getByText("任务已取消")).toBeInTheDocument();
        expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
        expect(
          screen.queryByRole("button", { name: "查看成片" }),
        ).not.toBeInTheDocument();
      }
      expect(screen.queryByText(/已等待/)).not.toBeInTheDocument();
    },
  );

  it.each([
    "2026-09-15T03:45:09+00:00",
    "2026-09-15T03:45:09Z",
    "2026-09-15 03:45:09",
    "2026-09-15 03:45:09.123456+00",
    "2026-09-15 11:45:09+08",
  ])("生成等待时长正确读取时区时间 %s", (submitted) => {
    const clock = vi
      .spyOn(Date, "now")
      .mockReturnValue(Date.parse("2026-09-15T03:46:19Z"));
    const value = studio();
    value.state = {
      ...value.state,
      page: "video",
      draft: {
        ...value.state.draft,
        firstFrameId: undefined,
        videoBatchId: "running",
      },
    };
    value.data = {
      ...value.data,
      tasks: [
        {
          id: "running",
          title: "庭院",
          type: "视频生成",
          status: "running",
          submitted,
        },
      ],
    };
    useStudio.mockReturnValue(value);
    render(<VideoPage />);
    expect(screen.getByText("已等待 1 分 10 秒")).toBeInTheDocument();
    clock.mockRestore();
  });

  it("历史视频任务不遮挡新首帧加载失败与重试", async () => {
    replicaApi.getAssetDownloadUrl.mockRejectedValue(
      new Error("签名服务暂时不可用"),
    );
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "video",
      draft: {
        ...value.state.draft,
        firstFrameId: "ff-after-task",
        videoBatchId: "old-video-task",
      },
    };
    value.data = {
      ...value.data,
      assets: value.data.assets.filter((asset) => asset.id !== "frame-1"),
      tasks: [
        {
          id: "old-video-task",
          title: "上一次视频生成",
          type: "视频生成",
          status: "completed",
          submitted: "今天 10:00",
        },
      ],
    };
    useStudio.mockReturnValue(value);

    render(<VideoPage />);

    expect(await screen.findByText("首帧预览加载失败")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重试加载首帧" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成视频" })).toBeDisabled();
  });

  it("视频生成在文图和多参考两种模式之间切换", () => {
    const value = studio({
      state: { ...studio().state, page: "video" },
    });
    useStudio.mockReturnValue(value);
    render(<VideoPage />);
    fireEvent.click(screen.getByRole("tab", { name: "参考生视频" }));
    expect(value.navigate).toHaveBeenCalledWith("reference");
  });

  it("首尾帧空卡片从卡片内选择素材库或本地上传", () => {
    const value = studio();
    value.state = {
      ...value.state,
      page: "video",
      draft: {
        ...value.state.draft,
        firstFrameId: undefined,
        tailFrameId: undefined,
      },
    };
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    expect(
      screen.getByRole("button", { name: "添加首帧" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "添加尾帧" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "本机上传" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "添加首帧" }));
    const dialog = screen.getByRole("dialog", { name: "选择首帧来源" });
    expect(
      within(dialog).getByRole("button", { name: "从素材库选择" }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "本机上传" }),
    ).toBeInTheDocument();
    fireEvent.click(
      within(dialog).getByRole("button", { name: "从素材库选择" }),
    );
    expect(value.openPicker).toHaveBeenCalledWith("first-frame");
    expect(screen.queryByRole("dialog", { name: "选择首帧来源" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "添加尾帧" }));
    fireEvent.click(
      within(screen.getByRole("dialog", { name: "选择尾帧来源" })).getByRole(
        "button",
        { name: "从素材库选择" },
      ),
    );
    expect(value.openPicker).toHaveBeenCalledWith("tail-frame");
  });

  it("已选首帧在原卡片预览并可点击更换", () => {
    const value = studio();
    value.state = { ...value.state, page: "video" };
    value.data.assets = value.data.assets.map((asset) =>
      asset.id === "frame-1"
        ? { ...asset, url: "https://assets.example/frame.png" }
        : asset,
    );
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    const card = screen.getByRole("button", { name: "更换首帧" });
    expect(within(card).getByRole("img", { name: "首帧" })).toHaveAttribute(
      "src",
      "https://assets.example/frame.png",
    );
    fireEvent.click(card);
    expect(
      screen.getByRole("dialog", { name: "选择首帧来源" }),
    ).toBeInTheDocument();
  });

  it("首帧来源弹层复用本地上传链路并在完成后关闭", async () => {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "video",
      draft: { ...value.state.draft, firstFrameId: undefined },
    };
    replicaLive.uploadVideoMaterial.mockResolvedValue({
      id: "local-first",
      name: "first.jpg",
      kind: "image",
      url: "https://assets.example/local-first.jpg",
      group: "首帧素材",
      source: "本机上传",
      saved: true,
    });
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    fireEvent.click(screen.getByRole("button", { name: "添加首帧" }));
    fireEvent.change(screen.getByLabelText("上传首帧"), {
      target: {
        files: [new File(["image"], "first.jpg", { type: "image/jpeg" })],
      },
    });

    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        firstFrameId: "local-first",
      }),
    );
    expect(value.updateData).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog", { name: "选择首帧来源" })).toBeNull();
  });

  it("文图与参考空占位默认竖屏且不随输出比例变化", () => {
    const value = studio({ state: { ...studio().state, page: "video" } });
    useStudio.mockReturnValue(value);
    const view = render(<VideoPage />);
    for (const page of ["video", "reference"] as const) {
      for (const ratio of ["9:16", "16:9", "1:1", "21:9", "4:3", "3:4"]) {
        value.state = {
          ...value.state,
          page,
          draft: { ...value.state.draft, ratio },
        };
        view.rerender(<VideoPage />);
        for (const preview of view.container.querySelectorAll(
          ".creation-video-grid .video-preview",
        )) {
          expect(preview).toHaveStyle({ aspectRatio: "0.5625" });
        }
        expect(
          view.container.querySelector(".creation-preview-media"),
        ).not.toBeNull();
      }
    }
  });

  it("文图与参考模式共用四个下拉参数并保留生成入口", () => {
    const value = studio({
      state: { ...studio().state, page: "video" },
    });
    useStudio.mockReturnValue(value);
    const view = render(<VideoPage />);

    let grid = view.container.querySelector(".creation-video-grid");
    expect(
      screen.getByRole("combobox", { name: "分辨率" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "时长" })).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "画面比例" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "生成数量" }),
    ).toBeInTheDocument();
    expect(grid?.querySelector(":scope > .creation-video-form")).not.toBeNull();
    const controls = grid?.querySelector(":scope > .creation-video-controls");
    expect(controls).not.toBeNull();
    expect(
      within(
        view.container.querySelector(
          ".creation-video-bottom-bar",
        ) as HTMLElement,
      ).getByRole("button", {
        name: "生成视频",
      }),
    ).toBeInTheDocument();
    expect(
      grid?.querySelector(":scope > .creation-video-preview"),
    ).not.toBeNull();

    value.state = { ...value.state, page: "reference" };
    view.rerender(<VideoPage />);
    grid = view.container.querySelector(".creation-video-grid");
    expect(
      screen.getByRole("combobox", { name: "分辨率" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "时长" })).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "画面比例" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "生成数量" }),
    ).toBeInTheDocument();
    expect(
      grid?.querySelector(":scope > .creation-video-controls"),
    ).not.toBeNull();
  });

  // 供应商 ratio 契约（2026-09-19 真实付费核对）：图生比例由首帧决定，
  // 文生不支持自动。
  it("图生视频画面比例选择器禁用并提示由首帧决定", () => {
    const value = studio({
      state: { ...studio().state, page: "video" },
    });
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    const ratio = screen.getByRole("combobox", { name: "画面比例" });
    expect(ratio).toBeDisabled();
    expect(ratio).toHaveValue("自动");
    expect(
      screen.getByText("图生视频的画面比例由首帧图片决定，无需选择。"),
    ).toBeInTheDocument();
  });

  it("文生视频隐藏自动选项，默认展示具体比例", () => {
    const base = studio();
    const value = studio({
      state: {
        ...base.state,
        page: "video",
        draft: { ...base.state.draft, firstFrameId: undefined },
      },
    });
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    const ratio = screen.getByRole("combobox", {
      name: "画面比例",
    }) as HTMLSelectElement;
    expect(ratio).not.toBeDisabled();
    const labels = Array.from(ratio.options).map((option) => option.value);
    expect(labels).not.toContain("自动");
    expect(labels).toContain("16:9");
    expect(ratio).toHaveValue("16:9");
  });

  it("文生视频草稿为自动时选择器回落展示 9:16", () => {
    const base = studio();
    const value = studio({
      state: {
        ...base.state,
        page: "video",
        draft: { ...base.state.draft, firstFrameId: undefined, ratio: "自动" },
      },
    });
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    expect(screen.getByRole("combobox", { name: "画面比例" })).toHaveValue(
      "9:16",
    );
  });

  it("图生视频底部确认栏展示自动而非草稿具体比例", () => {
    const base = studio();
    const value = studio({
      state: {
        ...base.state,
        page: "video",
        // 草稿默认 16:9，但图生实际提交 adaptive，扣费确认处必须诚实。
        draft: { ...base.state.draft, ratio: "16:9" },
      },
    });
    useStudio.mockReturnValue(value);
    const view = render(<VideoPage />);

    const bar = view.container.querySelector(
      ".creation-video-bottom-bar",
    ) as HTMLElement;
    expect(bar).not.toBeNull();
    expect(bar.textContent).toContain("图生视频");
    expect(bar.textContent).toContain("自动");
    expect(bar.textContent).not.toContain("16:9");
  });

  it("参考生视频比例选择器可用且保留自动选项", () => {
    const value = studio({
      state: { ...studio().state, page: "reference" },
    });
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    const ratio = screen.getByRole("combobox", {
      name: "画面比例",
    }) as HTMLSelectElement;
    expect(ratio).not.toBeDisabled();
    const labels = Array.from(ratio.options).map((option) => option.value);
    expect(labels).toContain("自动");
    expect(ratio).toHaveValue("16:9");
  });

  it("上传区域拒绝多文件与禁用态，单文件拖入复用上传链路", async () => {
    const value = studio({
      review: false,
      state: { ...studio().state, page: "reference" },
    });
    useStudio.mockReturnValue(value);
    const view = render(<VideoPage />);
    const file = new File(["image"], "reference.jpg", { type: "image/jpeg" });
    fireEvent.drop(screen.getByRole("button", { name: "上传文件" }), {
      dataTransfer: { files: [file] },
    });
    expect(replicaLive.uploadVideoMaterial).not.toHaveBeenCalled();
    value.videoCapabilities = {
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 8,
      max_reference_videos: 3,
      max_reference_audios: 3,
      max_quantity: 4,
    };
    view.rerender(<VideoPage />);
    fireEvent.drop(screen.getByRole("button", { name: "上传文件" }), {
      dataTransfer: { files: [file, file] },
    });
    expect(replicaLive.uploadVideoMaterial).not.toHaveBeenCalled();
    expect(value.notify).toHaveBeenCalledWith(
      "请每次添加一个文件，便于核对参考素材编号。",
    );
    replicaLive.uploadVideoMaterial.mockResolvedValue({
      id: "dropped",
      name: "reference.jpg",
      kind: "image",
      group: "参考素材",
      source: "本机上传",
      saved: true,
    });
    fireEvent.drop(screen.getByRole("button", { name: "上传文件" }), {
      dataTransfer: { files: [file] },
    });
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        referenceIds: ["reference-1", "dropped"],
      }),
    );
    expect(replicaLive.uploadVideoMaterial).toHaveBeenCalledTimes(1);
  });

  it("顶部切入 AI 视频不能绕过尚未完成的复刻交接", () => {
    const value = studio();
    value.state = {
      ...value.state,
      page: "video",
      draft: { ...value.state.draft, replicaPreparationPending: true },
    };
    useStudio.mockReturnValue(value);
    render(<VideoPage />);
    expect(screen.getByRole("button", { name: "生成视频" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "返回复刻准备" }));
    expect(value.navigate).toHaveBeenCalledWith("replica");
    expect(value.requestGeneration).not.toHaveBeenCalled();
  });

  it("参考素材接受图片/视频/音频并按类展示", () => {
    const value = studio();
    value.state = {
      ...value.state,
      page: "reference",
      draft: {
        ...value.state.draft,
        referenceIds: ["reference-1", "reference-video", "reference-audio"],
      },
    };
    value.data.assets.push(
      {
        id: "reference-video",
        name: "庭院运镜.mp4",
        kind: "video",
        group: "参考素材",
        source: "素材库",
        saved: true,
      },
      {
        id: "reference-audio",
        name: "环境声.wav",
        kind: "audio",
        group: "参考素材",
        source: "素材库",
        saved: true,
      },
    );
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    // 三类参考素材都按 kind 标签展示，不再被判为无效
    expect(screen.getByText("图片 · 素材库")).toBeInTheDocument();
    expect(screen.getByText("视频 · 素材库")).toBeInTheDocument();
    expect(screen.getByText("音频 · 素材库")).toBeInTheDocument();
    expect(screen.queryByText(/无效素材/)).toBeNull();
    expect(screen.queryByRole("button", { name: "整理参考素材" })).toBeNull();
  });

  it("参考素材放在添加区下方，图片视频音频均可单击预览", async () => {
    const value = studio();
    value.state = {
      ...value.state,
      page: "reference",
      draft: {
        ...value.state.draft,
        referenceIds: ["reference-1", "reference-video", "reference-audio"],
      },
    };
    value.data.assets = value.data.assets.map((asset) =>
      asset.id === "reference-1"
        ? { ...asset, url: "https://media.example/reference.jpg" }
        : asset,
    );
    value.data.assets.push(
      {
        id: "reference-video",
        name: "庭院运镜.mp4",
        kind: "video",
        group: "参考素材",
        source: "素材库",
        saved: true,
      },
      {
        id: "reference-audio",
        name: "环境声.wav",
        kind: "audio",
        group: "参考素材",
        source: "素材库",
        saved: true,
      },
    );
    useStudio.mockReturnValue(value);
    const view = render(<VideoPage />);

    const upload = screen.getByRole("button", { name: "上传文件" });
    const firstPreview = screen.getByRole("button", {
      name: "预览 乡墅外观.jpg",
    });
    expect(
      upload.compareDocumentPosition(firstPreview) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    fireEvent.click(firstPreview);
    let dialog = screen.getByRole("dialog", { name: "预览 乡墅外观.jpg" });
    expect(
      within(dialog).getByRole("img", { name: "乡墅外观.jpg" }),
    ).toBeVisible();
    fireEvent.click(within(dialog).getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(firstPreview));

    const videoPreview = screen.getByRole("button", {
      name: "预览 庭院运镜.mp4",
    });
    fireEvent.click(videoPreview);
    dialog = screen.getByRole("dialog", { name: "预览 庭院运镜.mp4" });
    await waitFor(() =>
      expect(dialog.querySelector("video")).toHaveAttribute(
        "src",
        "https://signed.example/reference-video.png",
      ),
    );
    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(videoPreview));

    fireEvent.click(screen.getByRole("button", { name: "预览 环境声.wav" }));
    dialog = screen.getByRole("dialog", { name: "预览 环境声.wav" });
    await waitFor(() =>
      expect(within(dialog).getByLabelText("环境声.wav")).toHaveAttribute(
        "src",
        "https://signed.example/reference-audio.png",
      ),
    );
    expect(replicaApi.getAssetDownloadUrl).toHaveBeenCalledWith(
      "reference-video",
    );
    expect(replicaApi.getAssetDownloadUrl).toHaveBeenCalledWith(
      "reference-audio",
    );

    view.unmount();
  });

  it("参考素材缩略图：视频取服务端首帧，图片取签名地址，音频只出波形占位", async () => {
    const value = studio({
      videoCapabilities: referenceCapabilities,
    });
    value.state = {
      ...value.state,
      page: "reference",
      draft: {
        ...value.state.draft,
        referenceIds: ["reference-1", "reference-video", "reference-audio"],
      },
    };
    value.data.assets = value.data.assets.map((asset) =>
      asset.id === "reference-1" ? { ...asset, assetId: "asset-image" } : asset,
    );
    value.data.assets.push(
      {
        id: "reference-video",
        assetId: "asset-video",
        name: "庭院运镜.mp4",
        kind: "video",
        group: "参考素材",
        source: "素材库",
        saved: true,
      },
      {
        id: "reference-audio",
        assetId: "asset-audio",
        name: "环境声.wav",
        kind: "audio",
        group: "参考素材",
        source: "素材库",
        saved: true,
      },
    );
    replicaApi.getMaterialBatchPreviews.mockResolvedValue({
      previews: {
        "asset-image": { url: "https://signed.example/asset-image.jpg" },
        "asset-video": { url: "https://signed.example/asset-video.mp4" },
      },
      thumbnails: {
        "asset-video": "https://signed.example/asset-video.thumb.jpg",
      },
    });
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    const videoRow = screen.getByRole("button", { name: "预览 庭院运镜.mp4" });
    await waitFor(() =>
      expect(videoRow.querySelector("img")).toHaveAttribute(
        "src",
        "https://signed.example/asset-video.thumb.jpg",
      ),
    );
    fireEvent.click(videoRow);
    const referenceCanvas = screen.getByLabelText("参考画布");
    expect(referenceCanvas).toHaveAttribute(
      "src",
      "https://signed.example/asset-video.mp4",
    );
    expect(referenceCanvas).toHaveAttribute(
      "poster",
      "https://signed.example/asset-video.thumb.jpg",
    );
    const imageRow = screen.getByRole("button", { name: "预览 乡墅外观.jpg" });
    await waitFor(() =>
      expect(imageRow.querySelector("img")).toHaveAttribute(
        "src",
        "https://signed.example/asset-image.jpg",
      ),
    );
    // 音频不出图像，也不会为它发批量请求
    const audioRow = screen.getByRole("button", { name: "预览 环境声.wav" });
    expect(audioRow.querySelector("img")).toBeNull();
    expect(replicaApi.getMaterialBatchPreviews).toHaveBeenCalledWith(
      "customer-1",
      [
        { id: "asset-image", populate: false },
        { id: "asset-video", populate: false },
      ],
      {},
    );
  });

  it("参考素材缩略图解析失败给出占位与重试入口，重试后恢复", async () => {
    const value = studio({
      videoCapabilities: referenceCapabilities,
    });
    value.state = {
      ...value.state,
      page: "reference",
      draft: {
        ...value.state.draft,
        referenceIds: ["reference-video"],
      },
    };
    value.data.assets.push({
      id: "reference-video",
      assetId: "asset-video",
      name: "庭院运镜.mp4",
      kind: "video",
      group: "参考素材",
      source: "素材库",
      saved: true,
    });
    replicaApi.getMaterialBatchPreviews.mockResolvedValueOnce({
      previews: {},
      thumbnails: {},
    });
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    const videoRow = screen.getByRole("button", { name: "预览 庭院运镜.mp4" });
    await waitFor(() =>
      expect(
        within(videoRow).getByText("缩略图暂不可用，请重试。"),
      ).toBeInTheDocument(),
    );

    replicaApi.getMaterialBatchPreviews.mockResolvedValue({
      previews: {
        "asset-video": { url: "https://signed.example/asset-video.mp4" },
      },
      thumbnails: {
        "asset-video": "https://signed.example/asset-video.thumb.jpg",
      },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "重试 庭院运镜.mp4 的缩略图" }),
    );
    await waitFor(() =>
      expect(videoRow.querySelector("img")).toHaveAttribute(
        "src",
        "https://signed.example/asset-video.thumb.jpg",
      ),
    );
    expect(replicaApi.getMaterialBatchPreviews).toHaveBeenCalledTimes(2);
  });

  it("参考素材列表落在上传输入框下方，计数独立成行", () => {
    const value = studio({
      videoCapabilities: referenceCapabilities,
    });
    value.state = {
      ...value.state,
      page: "reference",
      draft: { ...value.state.draft, referenceIds: ["reference-1"] },
    };
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    const dropzone = screen.getByRole("button", { name: "上传文件" });
    const library = screen.getByRole("button", { name: /从素材库选择/ });
    const preview = screen.getByRole("button", { name: "预览 乡墅外观.jpg" });
    const counter = screen.getByText("参考图 1/8 · 视频 0/3 · 音频 0/3");

    // 素材库入口与上传框同属一个合并上传区，素材列表在其下方
    expect(dropzone.parentElement?.contains(library)).toBe(true);
    expect(
      dropzone.compareDocumentPosition(preview) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      library.compareDocumentPosition(preview) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      preview.compareDocumentPosition(counter) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // 计数不再挤在按钮文案里
    expect(within(library).queryByText(/参考图 1\/8/)).toBeNull();
  });

  it("参考生视频导入首帧模板时给出可执行的转换入口", () => {
    const value = studio({
      videoCapabilities: referenceCapabilities,
    });
    value.state = {
      ...value.state,
      page: "reference",
      draft: {
        ...value.state.draft,
        prompt: "用@2的人物替换视频@1中的主要角色",
        referenceIds: ["reference-1"],
        promptBindingsStale: true,
        importedPromptContext: {
          route: "text_image",
          duration_seconds: 8,
          mode: "I2VA",
          generation_assets: [{ label: "<Picture 1>", purpose: "first_frame" }],
        },
      },
    };
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    expect(
      screen.getByRole("button", { name: "AI 优化提示词" }),
    ).toHaveTextContent("按当前素材 AI 转换");
    expect(screen.getByRole("alert")).toHaveTextContent(
      "导入模板与当前生成模式不同",
    );
    expect(
      screen.getByText(
        /导入模板：首帧生视频（I2VA）；当前模式：参考生视频（Ref2VA）/,
      ),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("提示词"), {
      target: { value: "手动改写后的参考提示词" },
    });
    expect(value.patchDraft).toHaveBeenCalledWith({
      prompt: "手动改写后的参考提示词",
      promptEdited: true,
      importedPromptContext: undefined,
    });
  });

  it.each([
    ["文生视频", undefined],
    ["图生视频", "frame-1"],
  ])("%s可从剪贴板导入分镜脚本并提示 AI 优化", async (_mode, firstFrameId) => {
    const readText = vi
      .fn()
      .mockResolvedValue(
        "镜头编号\t开始(秒)\t结束(秒)\t动作\n镜头 1\t0\t4\t人物走向镜头",
      );
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { readText },
    });
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "video",
      draft: {
        ...value.state.draft,
        firstFrameId,
        prompt: "",
      },
    };
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    expect(screen.getByLabelText("提示词")).toHaveAttribute("rows", "48");
    fireEvent.click(screen.getByRole("button", { name: "导入分镜脚本" }));

    await waitFor(() => expect(readText).toHaveBeenCalledOnce());
    expect(value.patchDraft).toHaveBeenCalledWith({
      prompt: expect.stringMatching(/分镜表[\s\S]+H3[\s\S]+镜头 1\t0\t4/),
      promptEdited: true,
      importedPromptContext: undefined,
      promptBindingsStale: false,
    });
    expect(
      screen.getByRole("button", { name: "AI 优化提示词" }),
    ).toHaveTextContent("AI 优化");
    expect(value.notify).toHaveBeenCalledWith(
      "分镜表已导入，请点击“AI 优化”。",
    );
  });

  it("本机上传视频后立即用本地首帧作为缩略图", async () => {
    replicaLive.readVideoDuration.mockResolvedValue(10);
    replicaLive.readVideoFirstFrame.mockResolvedValue(
      "data:image/jpeg;base64,FRAME",
    );
    replicaLive.uploadVideoMaterial.mockResolvedValue({
      id: "dropped-video",
      name: "庭院.mp4",
      kind: "video",
      group: "参考素材",
      source: "本机上传",
      saved: true,
    });
    const updateData = vi.fn();
    const value = studio({
      review: false,
      updateData,
      videoCapabilities: referenceCapabilities,
    });
    value.state = {
      ...value.state,
      page: "reference",
      draft: { ...value.state.draft, referenceIds: [] },
    };
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    fireEvent.change(screen.getByLabelText("上传参考素材"), {
      target: {
        files: [new File(["video"], "庭院.mp4", { type: "video/mp4" })],
      },
    });

    await waitFor(() => expect(updateData).toHaveBeenCalled());
    const updater = updateData.mock.calls[0]?.[0];
    const next = updater({ ...value.data });
    expect(next.materials[0]).toMatchObject({
      id: "dropped-video",
      poster: "data:image/jpeg;base64,FRAME",
    });
    expect(value.patchDraft).toHaveBeenCalledWith({
      referenceIds: ["dropped-video"],
    });
  });

  it("移除参考素材不会误打开预览", () => {
    const value = studio();
    value.state = { ...value.state, page: "reference" };
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    fireEvent.click(screen.getByRole("button", { name: "移除 乡墅外观.jpg" }));

    expect(value.patchDraft).toHaveBeenCalledWith({ referenceIds: [] });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("参考素材本机上传对超过 15 秒的视频在发请求前拦截", async () => {
    replicaLive.readVideoDuration.mockResolvedValue(20);
    const value = studio({
      review: false,
      videoCapabilities: {
        extended_modes_enabled: true,
        t2v_enabled: true,
        i2v_enabled: true,
        r2v_enabled: true,
        last_frame_enabled: true,
        max_reference_images: 8,
        max_reference_videos: 3,
        max_reference_audios: 3,
        max_quantity: 4,
      },
      state: {
        ...studio().state,
        page: "reference",
        draft: { ...studio().state.draft, referenceIds: [] },
      },
    });
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    fireEvent.change(screen.getByLabelText("上传参考素材"), {
      target: {
        files: [new File(["video"], "庭院.mp4", { type: "video/mp4" })],
      },
    });

    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith(
        "参考视频时长不能超过 15 秒，请裁剪后再上传。",
      ),
    );
    expect(replicaLive.uploadVideoMaterial).not.toHaveBeenCalled();
  });

  it("参考素材本机上传接受不超过 15 秒的视频", async () => {
    replicaLive.readVideoDuration.mockResolvedValue(12);
    replicaLive.uploadVideoMaterial.mockResolvedValue({
      id: "reference-video",
      name: "庭院.mp4",
      kind: "video",
      group: "参考素材",
      source: "本机上传",
      saved: true,
    });
    const value = studio({
      review: false,
      videoCapabilities: {
        extended_modes_enabled: true,
        t2v_enabled: true,
        i2v_enabled: true,
        r2v_enabled: true,
        last_frame_enabled: true,
        max_reference_images: 8,
        max_reference_videos: 3,
        max_reference_audios: 3,
        max_quantity: 4,
      },
      state: {
        ...studio().state,
        page: "reference",
        draft: { ...studio().state.draft, referenceIds: [] },
      },
    });
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    fireEvent.change(screen.getByLabelText("上传参考素材"), {
      target: {
        files: [new File(["video"], "庭院.mp4", { type: "video/mp4" })],
      },
    });

    await waitFor(() =>
      expect(replicaLive.uploadVideoMaterial).toHaveBeenCalledWith(
        expect.any(File),
        "参考素材",
        expect.any(Function),
      ),
    );
    expect(replicaLive.readVideoDuration).toHaveBeenCalled();
  });

  it("参考素材本机上传对超过 15 秒的音频在发请求前拦截", async () => {
    replicaLive.readAudioDuration.mockResolvedValue(30);
    const value = studio({
      review: false,
      videoCapabilities: {
        extended_modes_enabled: true,
        t2v_enabled: true,
        i2v_enabled: true,
        r2v_enabled: true,
        last_frame_enabled: true,
        max_reference_images: 8,
        max_reference_videos: 3,
        max_reference_audios: 3,
        max_quantity: 4,
      },
      state: {
        ...studio().state,
        page: "reference",
        draft: { ...studio().state.draft, referenceIds: [] },
      },
    });
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    fireEvent.change(screen.getByLabelText("上传参考素材"), {
      target: {
        files: [new File(["audio"], "环境声.mp3", { type: "audio/mpeg" })],
      },
    });

    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith(
        "参考音频时长不能超过 15 秒，请裁剪后再上传。",
      ),
    );
    expect(replicaLive.uploadReferenceAudioMaterial).not.toHaveBeenCalled();
  });

  it("参考素材本机上传以 reference 用途接受不超过 15 秒的音频", async () => {
    replicaLive.readAudioDuration.mockResolvedValue(10);
    replicaLive.uploadReferenceAudioMaterial.mockResolvedValue({
      id: "reference-audio",
      name: "环境声.mp3",
      kind: "audio",
      group: "参考素材",
      source: "本机上传",
      saved: true,
    });
    const value = studio({
      review: false,
      videoCapabilities: {
        extended_modes_enabled: true,
        t2v_enabled: true,
        i2v_enabled: true,
        r2v_enabled: true,
        last_frame_enabled: true,
        max_reference_images: 8,
        max_reference_videos: 3,
        max_reference_audios: 3,
        max_quantity: 4,
      },
      state: {
        ...studio().state,
        page: "reference",
        draft: { ...studio().state.draft, referenceIds: [] },
      },
    });
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    fireEvent.change(screen.getByLabelText("上传参考素材"), {
      target: {
        files: [new File(["audio"], "环境声.mp3", { type: "audio/mpeg" })],
      },
    });

    await waitFor(() =>
      expect(replicaLive.uploadReferenceAudioMaterial).toHaveBeenCalledWith(
        expect.any(File),
        10,
        expect.any(Function),
      ),
    );
  });

  it("参考素材上传器标注视频与音频的 15 秒上限", () => {
    const value = studio({
      review: false,
      state: {
        ...studio().state,
        page: "reference",
        draft: { ...studio().state.draft, referenceIds: [] },
      },
    });
    useStudio.mockReturnValue(value);
    render(<VideoPage />);

    expect(
      screen.getByText("视频、音频各累计 ≤15 秒；参考合计 ≤12 项"),
    ).toBeInTheDocument();
  });

  it("参考图上传完成时按最新草稿追加而不复活已移除引用", async () => {
    let resolveUpload: ((asset: StudioAsset) => void) | undefined;
    replicaLive.uploadVideoMaterial.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve;
        }),
    );
    let current = studio({
      review: false,
      videoCapabilities: {
        extended_modes_enabled: true,
        t2v_enabled: true,
        i2v_enabled: true,
        r2v_enabled: true,
        last_frame_enabled: true,
        max_reference_images: 4,
        max_reference_videos: 3,
        max_reference_audios: 3,
        max_quantity: 4,
      },
      state: {
        ...studio().state,
        page: "reference",
        draft: { ...studio().state.draft, referenceIds: ["reference-1"] },
      },
    });
    useStudio.mockImplementation(() => current);
    const view = render(<VideoPage />);

    fireEvent.change(screen.getByLabelText("上传参考素材"), {
      target: {
        files: [new File(["image"], "庭院.jpg", { type: "image/jpeg" })],
      },
    });
    current = {
      ...current,
      state: {
        ...current.state,
        draft: { ...current.state.draft, referenceIds: ["reference-b"] },
      },
      data: {
        ...current.data,
        assets: [
          ...current.data.assets,
          {
            id: "reference-b",
            name: "参考图 B.jpg",
            kind: "image",
            group: "参考素材",
            source: "素材库",
            saved: true,
          },
        ],
      },
    };
    view.rerender(<VideoPage />);
    resolveUpload?.({
      id: "reference-new",
      name: "庭院.jpg",
      kind: "image",
      group: "参考素材",
      source: "本机上传",
      saved: true,
    });

    await waitFor(() =>
      expect(current.patchDraft).toHaveBeenCalledWith({
        referenceIds: ["reference-b", "reference-new"],
      }),
    );
  });

  it("参考图上传期间达到上限时不再回填迟到结果", async () => {
    let resolveUpload: ((asset: StudioAsset) => void) | undefined;
    replicaLive.uploadVideoMaterial.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve;
        }),
    );
    let current = studio({
      review: false,
      videoCapabilities: {
        extended_modes_enabled: true,
        t2v_enabled: true,
        i2v_enabled: true,
        r2v_enabled: true,
        last_frame_enabled: true,
        max_reference_images: 1,
        max_reference_videos: 3,
        max_reference_audios: 3,
        max_quantity: 4,
      },
      state: {
        ...studio().state,
        page: "reference",
        draft: { ...studio().state.draft, referenceIds: [] },
      },
    });
    useStudio.mockImplementation(() => current);
    const view = render(<VideoPage />);
    fireEvent.change(screen.getByLabelText("上传参考素材"), {
      target: {
        files: [new File(["image"], "迟到.jpg", { type: "image/jpeg" })],
      },
    });
    current = {
      ...current,
      state: {
        ...current.state,
        draft: { ...current.state.draft, referenceIds: ["reference-1"] },
      },
    };
    view.rerender(<VideoPage />);
    vi.mocked(current.patchDraft).mockClear();
    resolveUpload?.({
      id: "reference-late",
      name: "迟到.jpg",
      kind: "image",
      group: "参考素材",
      source: "本机上传",
      saved: true,
    });

    await waitFor(() =>
      expect(current.notify).toHaveBeenCalledWith("当前最多选择 1 张参考图。"),
    );
    expect(current.patchDraft).not.toHaveBeenCalled();
  });

  it("离开参考生视频后忽略仍在上传的参考图", async () => {
    let resolveUpload: ((asset: StudioAsset) => void) | undefined;
    replicaLive.uploadVideoMaterial.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve;
        }),
    );
    let current = studio({
      review: false,
      videoCapabilities: {
        extended_modes_enabled: true,
        t2v_enabled: true,
        i2v_enabled: true,
        r2v_enabled: true,
        last_frame_enabled: true,
        max_reference_images: 4,
        max_reference_videos: 3,
        max_reference_audios: 3,
        max_quantity: 4,
      },
      state: {
        ...studio().state,
        page: "reference",
        draft: { ...studio().state.draft, referenceIds: [] },
      },
    });
    useStudio.mockImplementation(() => current);
    const view = render(<VideoPage />);
    fireEvent.change(screen.getByLabelText("上传参考素材"), {
      target: {
        files: [new File(["image"], "离开.jpg", { type: "image/jpeg" })],
      },
    });
    current = { ...current, state: { ...current.state, page: "video" } };
    view.rerender(<VideoPage />);
    vi.mocked(current.patchDraft).mockClear();
    resolveUpload?.({
      id: "reference-abandoned",
      name: "离开.jpg",
      kind: "image",
      group: "参考素材",
      source: "本机上传",
      saved: true,
    });
    await Promise.resolve();

    expect(current.patchDraft).not.toHaveBeenCalled();
  });

  it("切换草稿后不把旧草稿仍在上传的参考图写入新草稿", async () => {
    let resolveUpload: ((asset: StudioAsset) => void) | undefined;
    replicaLive.uploadVideoMaterial.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve;
        }),
    );
    let current = studio({
      review: false,
      videoCapabilities: {
        extended_modes_enabled: true,
        t2v_enabled: true,
        i2v_enabled: true,
        r2v_enabled: true,
        last_frame_enabled: true,
        max_reference_images: 4,
        max_reference_videos: 3,
        max_reference_audios: 3,
        max_quantity: 4,
      },
      state: {
        ...studio().state,
        page: "reference",
        draft: { ...studio().state.draft, referenceIds: [] },
      },
    });
    useStudio.mockImplementation(() => current);
    const view = render(<VideoPage />);
    fireEvent.change(screen.getByLabelText("上传参考素材"), {
      target: {
        files: [new File(["image"], "旧草稿.jpg", { type: "image/jpeg" })],
      },
    });
    current = {
      ...current,
      state: {
        ...current.state,
        draft: { ...current.state.draft, id: "draft-2" },
      },
    };
    view.rerender(<VideoPage />);
    vi.mocked(current.patchDraft).mockClear();
    resolveUpload?.({
      id: "reference-old-draft",
      name: "旧草稿.jpg",
      kind: "image",
      group: "参考素材",
      source: "本机上传",
      saved: true,
    });
    await Promise.resolve();

    expect(current.patchDraft).not.toHaveBeenCalled();
  });

  it("旧音频口播入口也只展示视频分身、克隆声音和文案", () => {
    useStudio.mockReturnValue(
      studio({ state: { ...studio().state, page: "oral-audio" } }),
    );
    render(<OralPage />);
    expect(
      screen.getByRole("heading", { name: "口播文案" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("选择口播音频")).toBeNull();
    expect(screen.queryByRole("tab", { name: "用已有音频生成" })).toBeNull();
  });

  it("文案口播不再把未实现的网感模板表现为可用", () => {
    useStudio.mockReturnValue(studio());
    render(<OralPage />);

    expect(screen.queryByRole("button", { name: "网感模板" })).toBeNull();
    expect(screen.getByText("标准口播")).toBeInTheDocument();
  });

  it("照片分身即使处于就绪状态也不能用于新的口播", () => {
    const value = studio();
    value.data.people[0].avatars[0].origin = "照片制作";
    useStudio.mockReturnValue(value);
    render(<OralPage />);
    expect(screen.getByRole("button", { name: "生成口播视频" })).toBeDisabled();
  });

  it("更换口播IP只打开人物选择器，由统一草稿层执行防串人清理", () => {
    const value = studio({
      state: { ...studio().state, page: "oral" },
    });
    useStudio.mockReturnValue(value);
    render(<OralPage />);
    fireEvent.click(screen.getByRole("button", { name: "更换 IP" }));
    expect(value.openPicker).toHaveBeenCalledWith("person");
  });

  it("文案口播正文只读，修改时返回唯一文案工坊", () => {
    const value = studio({
      state: { ...studio().state, page: "oral" },
    });
    useStudio.mockReturnValue(value);
    render(<OralPage />);
    expect(
      screen.queryByRole("textbox", { name: "口播文案" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "去文案工坊修改" }));
    expect(value.navigate).toHaveBeenCalledWith("copy", { returnTo: "oral" });
  });

  it("口播分身名称已含人物名时不重复拼接", () => {
    const value = studio();
    value.data.people[0] = {
      ...value.data.people[0],
      avatars: [
        {
          ...value.data.people[0].avatars[0],
          name: "张工 · 设计室讲解",
        },
      ],
    };
    useStudio.mockReturnValue(value);

    render(<OralPage />);

    expect(screen.getAllByText("张工 · 设计室讲解")).toHaveLength(2);
    expect(screen.queryByText("张工 · 张工 · 设计室讲解")).toBeNull();
  });

  it("补充声音与分身时带入当前IP和当前口播模式", () => {
    const missingVoice = studio();
    missingVoice.state = {
      ...missingVoice.state,
      page: "oral",
      draft: { ...missingVoice.state.draft, voiceId: undefined },
    };
    useStudio.mockReturnValue(missingVoice);
    const view = render(<OralPage />);
    fireEvent.click(screen.getByRole("button", { name: "管理声音" }));
    expect(missingVoice.navigate).toHaveBeenCalledWith("person-voices", {
      returnTo: "oral",
      selectedPersonId: "person-1",
    });

    view.unmount();
    const missingAvatar = studio();
    missingAvatar.state = {
      ...missingAvatar.state,
      page: "oral-audio",
      draft: { ...missingAvatar.state.draft, avatarId: undefined },
    };
    useStudio.mockReturnValue(missingAvatar);
    render(<OralPage />);
    fireEvent.click(
      screen.getByRole("button", { name: "去人物库制作口播分身" }),
    );
    expect(missingAvatar.navigate).toHaveBeenCalledWith("person-avatars", {
      returnTo: "oral",
      selectedPersonId: "person-1",
    });
  });
});

describe("视频复刻（模块①）", () => {
  beforeEach(() => {
    useStudio.mockReset();
    replicaApi.startVideoAnalysis.mockClear();
    replicaApi.getAnalysisTask.mockClear();
    replicaApi.getLatestProjectShotCards.mockReset();
    replicaApi.getLatestProjectAnalysis.mockReset();
    replicaApi.getLatestGenerationPrompt.mockReset();
    replicaApi.getLatestScriptVersion.mockReset();
    replicaApi.saveGenerationPrompt.mockReset();
    replicaApi.getGenerationPriceQuote.mockReset();
    replicaApi.getLatestProjectShotCards.mockResolvedValue(null);
    replicaApi.getLatestProjectAnalysis.mockResolvedValue({
      id: "av-empty",
      payload: {},
    });
    replicaApi.getLatestGenerationPrompt.mockResolvedValue({
      stale: false,
      stale_reasons: [],
      version: null,
    });
    replicaApi.getLatestScriptVersion.mockResolvedValue({
      stale: false,
      stale_reasons: [],
      version: null,
    });
    replicaApi.getGenerationPriceQuote.mockResolvedValue({
      resolution: "768P",
      duration_seconds: 4,
      quantity: 1,
      unit_price_fen_per_second: 120,
      estimated_seconds: 4,
      estimated_price_fen: 480,
    });
  });

  const shot = {
    shot_id: "s1",
    start_time: 0,
    end_time: 8,
    shot_type: "中景",
    composition: "",
    camera_motion: "推进",
    subject: "院落",
    action: "镜头缓推庭院",
    scene: "乡墅庭院",
    spoken_text: "这栋房子的采光设计",
    transition: "切镜",
  };

  it("项目缺失时即使草稿残留首帧也持续显示前置检查", () => {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "replica",
      draft: {
        ...value.state.draft,
        projectId: undefined,
        firstFrameId: "frame-1",
      },
    };
    useStudio.mockReturnValue(value);

    render(<ReplicaPage />);

    const checklist = screen.getByRole("region", { name: "生成前检查" });
    expect(checklist).toHaveTextContent("来源视频");
    expect(checklist).toHaveTextContent("请先上传参考视频或选择已有项目");
  });

  function replicaStudio(_legacyStep?: 1 | 3) {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "replica",
      draft: {
        ...value.state.draft,
        projectId: "project-1",
        sourceAssetId: "asset-1",
        prompt: "",
      },
    };
    value.data = {
      ...value.data,
      projects: [
        {
          id: "project-1",
          name: "复刻测试项目",
          owner_user_id: "employee_1",
          status: "ACTIVE",
          reference_asset_id: "asset-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        },
      ],
    };
    return value;
  }

  function mockAnalysisSuccess(options: { existingShotCards?: boolean } = {}) {
    replicaApi.startVideoAnalysis.mockResolvedValue({
      id: "task-1",
      status: "RUNNING",
    });
    replicaApi.waitForAnalysisTask.mockResolvedValue({
      id: "task-1",
      status: "SUCCEEDED",
    });
    replicaApi.getLatestProjectShotCards.mockResolvedValue({
      id: options.existingShotCards ? "scv-existing" : "scv-1",
      payload: {
        source_analysis_version_id: "av-1",
        duration_seconds: 8,
        shots: [shot],
      },
    });
    replicaApi.getLatestProjectAnalysis.mockResolvedValue({
      id: "av-1",
      payload: {
        analysis: {
          summary: "庭院复刻",
          duration_seconds: 8,
          original_script: "这栋房子的采光设计非常好",
          shots: [shot],
        },
      },
    });
    replicaApi.saveShotCards.mockResolvedValue({
      id: "scv-1",
      payload: {
        source_analysis_version_id: "av-1",
        duration_seconds: 4,
        shots: [shot],
      },
    });
  }

  async function prepareFinalReplica() {
    replicaApi.createScriptVersion.mockResolvedValue({ id: "script-final" });
    replicaApi.compileGenerationPrompt.mockResolvedValue({
      id: "prompt-final",
      payload: { prompt_text: "最终新稿" },
    });
    const confirmScript = screen.queryByRole("button", { name: /^确认$/ });
    if (confirmScript) fireEvent.click(confirmScript);
    const compression = screen.queryByLabelText(/内容压缩到/);
    if (compression) fireEvent.click(compression);
    const openingAction = screen.queryByLabelText("开场衔接");
    if (openingAction)
      fireEvent.change(openingAction, {
        target: { value: "从当前确认首帧自然衔接到原视频动作。" },
      });
    fireEvent.click(screen.getByRole("button", { name: "合成最终提示词" }));
    await waitFor(() =>
      expect(replicaApi.compileGenerationPrompt).toHaveBeenCalled(),
    );
    const adopt = screen.queryByRole("button", { name: "采用这份最终稿" });
    if (adopt) fireEvent.click(adopt);
    // ⑧ 就绪徽标与 PromptEditor 状态行都含「已就绪」，这里只要求出现其一。
    await waitFor(() =>
      expect(screen.getAllByText(/已就绪/).length).toBeGreaterThan(0),
    );
  }

  it("合成最终提示词时按草稿的时长逐秒下发，不再折叠成 4 或 15", async () => {
    // 回归：早期 H3 只支持 4/15 两档，前端因此把 4–15 的选择压成
    // `duration <= 9 ? 4 : 15`。PR #160 把契约放开到 4–15 每秒后这个映射就
    // 过期了——默认 8 秒会被悄悄发成 4 秒，用户选 12 秒会变成 15 秒，中间值
    // 永远拿不到。后端 output_duration_seconds 的约束是 ge=4, le=15。
    const value = replicaStudio();
    value.state = {
      ...value.state,
      draft: { ...value.state.draft, duration: 8 },
    };
    useStudio.mockReturnValue(value);
    mockAnalysisSuccess();
    render(<ReplicaPage />);
    await screen.findByText(/已拆解/);
    await prepareFinalReplica();

    expect(replicaApi.compileGenerationPrompt).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({ output_duration_seconds: 8 }),
    );
  });

  it("离开内容配置后迟到拆解回执不写入当前草稿", async () => {
    const value = replicaStudio();
    mockAnalysisSuccess();
    let finish!: (result: unknown) => void;
    replicaApi.waitForAnalysisTask.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    useStudio.mockReturnValue(value);
    const view = render(<ReplicaPage />);
    await screen.findByRole("button", { name: "重新拆解" });
    fireEvent.click(screen.getByRole("button", { name: "重新拆解" }));
    await waitFor(() => expect(finish).toBeDefined());
    view.unmount();
    vi.mocked(value.patchDraft).mockClear();
    finish({ id: "task-1", status: "SUCCEEDED" });
    await Promise.resolve();
    await Promise.resolve();
    expect(value.patchDraft).not.toHaveBeenCalled();
  });

  it("拆解遇到积分不足时打开钱包侧栏，不让用户自己找充值入口", async () => {
    const value = replicaStudio();
    replicaApi.startVideoAnalysis.mockRejectedValueOnce(
      Object.assign(new Error("积分不足，本次需要 5 积分。"), {
        code: "INSUFFICIENT_CREDITS",
      }),
    );
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);
    fireEvent.click(screen.getByRole("button", { name: "启动 AI 拆解" }));

    // 服务端文案原样透传，用户看得到还差多少。
    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith("积分不足，本次需要 5 积分。"),
    );
    expect(value.openLive).toHaveBeenCalledWith("wallet");
    expect(screen.getByRole("alert")).toHaveTextContent(
      "积分不足，本次需要 5 积分。",
    );
  });

  it("拆解等待时展示真实运行状态而不是只有按钮忙碌", async () => {
    const value = replicaStudio();
    replicaApi.startVideoAnalysis.mockResolvedValueOnce({
      id: "task-wait",
      status: "PENDING",
    });
    replicaApi.waitForAnalysisTask.mockImplementationOnce((_id, update) => {
      update({ id: "task-wait", status: "RUNNING" });
      return new Promise(() => {});
    });
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);
    fireEvent.click(screen.getByRole("button", { name: "启动 AI 拆解" }));
    expect(await screen.findByText(/正在分析视频画面与口播/)).toHaveTextContent(
      "已等待",
    );
    expect(screen.getByText(/正在分析视频画面与口播/)).toHaveTextContent(
      "请勿重复提交",
    );
    expect(replicaApi.startVideoAnalysis).toHaveBeenCalledTimes(1);
  });

  it("拆解的其他失败不打开钱包侧栏", async () => {
    const value = replicaStudio();
    replicaApi.startVideoAnalysis.mockRejectedValueOnce(
      new Error("来源视频已失效，请重新上传"),
    );
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);
    fireEvent.click(screen.getByRole("button", { name: "启动 AI 拆解" }));

    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith("来源视频已失效，请重新上传"),
    );
    expect(value.openLive).not.toHaveBeenCalled();
  });

  it("拆解已完成但没有分镜时，重新拆解强制新建任务而不复用旧任务", async () => {
    // 回归：视频过短 / 无有效镜头时任务 SUCCEEDED 却不落分镜。旧逻辑把
    // 「重新拆解」判定为复用已完成的旧任务（getAnalysisTask），用户既拿不到
    // 分镜、也点不动第二次拆解，陷入死循环；现在只有任务仍在排队 / 运行中
    // 才复用，其余情况一律带 force 新建。
    const value = replicaStudio();
    value.state = {
      ...value.state,
      draft: {
        ...value.state.draft,
        analysisTaskId: "task-done",
        analysisTaskStatus: "SUCCEEDED",
      },
    };
    mockAnalysisSuccess();
    replicaApi.getLatestProjectShotCards.mockResolvedValue({
      id: "scv-empty",
      payload: {
        source_analysis_version_id: "av-1",
        duration_seconds: 8,
        shots: [],
      },
    });
    replicaApi.getLatestProjectAnalysis.mockResolvedValue({
      id: "av-1",
      payload: {
        analysis: {
          summary: "",
          duration_seconds: 8,
          original_script: "",
          shots: [],
        },
      },
    });
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: "启动 AI 拆解" }),
    );

    await waitFor(() =>
      expect(replicaApi.startVideoAnalysis).toHaveBeenCalledWith(
        "project-1",
        "asset-1",
        undefined,
        true,
      ),
    );
    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith(
        "拆解完成。请确认文案和置换首帧，再合成最终提示词。",
      ),
    );
    expect(replicaApi.getAnalysisTask).not.toHaveBeenCalled();
  });

  it("首帧置换前显示短提示，选定首帧后在同页展示最终合成", async () => {
    const value = replicaStudio();
    value.state.draft.firstFrameId = undefined;
    mockAnalysisSuccess();
    useStudio.mockReturnValue(value);
    const view = render(<ReplicaPage />);
    fireEvent.click(screen.getByRole("button", { name: "启动 AI 拆解" }));
    await screen.findAllByDisplayValue(/院落/);
    expect(
      screen.getByRole("heading", { name: "2 首帧置换" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "合成最终提示词" })).toBeNull();
    expect(screen.queryByLabelText("最终提示词")).toBeNull();
    expect(
      screen.getByText("请先完成首帧置换并选定图片，再合成最终提示词。"),
    ).toBeInTheDocument();
    value.state.draft = {
      ...value.state.draft,
      firstFrameId: "confirmed-frame",
    };
    view.rerender(<ReplicaPage />);
    expect(
      screen.getByRole("button", { name: "合成最终提示词" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "存入我的提示词" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "去 AI 视频创作" }),
    ).toBeDisabled();
  });

  it("切项目后忽略迟到的分镜保存", async () => {
    const value = replicaStudio();
    mockSavedReplicaVersions();
    let finishSave: ((value: { id: string }) => void) | undefined;
    replicaApi.saveShotCards.mockImplementation(
      () =>
        new Promise((resolve) => {
          finishSave = resolve;
        }),
    );
    useStudio.mockReturnValue(value);
    const view = render(<ReplicaPage />);
    const action = await screen.findByLabelText("s1 动作");
    fireEvent.change(action, { target: { value: "新动作" } });

    fireEvent.click(screen.getByRole("button", { name: "保存为自定义" }));
    value.state = {
      ...value.state,
      draft: { ...value.state.draft, projectId: "project-2" },
    };
    value.data.projects.push({
      ...value.data.projects[0],
      id: "project-2",
      name: "项目 2",
    });
    view.rerender(<ReplicaPage />);
    finishSave?.({ id: "late-shot-version" });
    await Promise.resolve();
    expect(value.notify).not.toHaveBeenCalledWith("分镜已保存。");
  });

  function mockSavedReplicaVersions() {
    replicaApi.getLatestProjectShotCards.mockResolvedValue({
      id: "scv-saved",
      payload: {
        source_analysis_version_id: "av-saved",
        duration_seconds: 4,
        shots: [shot],
      },
    });
    replicaApi.getLatestProjectAnalysis.mockResolvedValue({
      id: "av-saved",
      payload: {
        analysis: {
          original_script: "保存的分析原文",
          shots: [shot],
        },
      },
    });
    replicaApi.getLatestGenerationPrompt.mockResolvedValue({
      stale: false,
      stale_reasons: [],
      version: {
        id: "prompt-saved",
        version_number: 4,
        payload: { prompt_text: "保存的复刻 Prompt" },
      },
    });
    replicaApi.getLatestScriptVersion.mockResolvedValue({
      stale: false,
      stale_reasons: [],
      version: {
        id: "script-saved",
        version_number: 3,
        payload: { full_text: "保存的二创终稿" },
      },
    });
  }

  it("返回复刻页时自动恢复分镜、脚本与 Prompt，不重新发起拆解", async () => {
    const value = replicaStudio(3);
    value.state = {
      ...value.state,
      draft: {
        ...value.state.draft,
        prompt: "",
        script: { ...value.state.draft.script, text: "", confirmed: false },
      },
    };
    mockSavedReplicaVersions();
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    await waitFor(() => expect(value.patchDraft).toHaveBeenCalled());
    expect(screen.getByLabelText("最终提示词")).toHaveValue(
      "保存的复刻 Prompt",
    );
    expect(screen.queryByLabelText("复刻单条时长")).toBeNull();
    expect(screen.queryByLabelText("复刻生成数量")).toBeNull();
    expect(value.patchDraft).toHaveBeenCalledWith(
      expect.objectContaining({
        prompt: "保存的复刻 Prompt",
        script: expect.objectContaining({
          id: "script-saved",
          text: "保存的二创终稿",
          version: 3,
        }),
      }),
    );
    expect(replicaApi.startVideoAnalysis).not.toHaveBeenCalled();
  });

  it("返回复刻页时保留当前项目未保存的工作区草稿", async () => {
    const value = replicaStudio(3);
    value.state = {
      ...value.state,
      draft: {
        ...value.state.draft,
        prompt: "尚未保存的 Prompt 编辑",
        promptEdited: true,
        script: {
          ...value.state.draft.script,
          text: "尚未保存的文案编辑",
          version: 7,
        },
        scriptEdited: true,
      },
    };
    mockSavedReplicaVersions();
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    await waitFor(() => expect(value.patchDraft).toHaveBeenCalled());
    expect(screen.getByLabelText("最终提示词")).toHaveValue(
      "尚未保存的 Prompt 编辑",
    );
    expect(value.patchDraft).toHaveBeenCalledWith(
      expect.objectContaining({
        prompt: "尚未保存的 Prompt 编辑",
        script: expect.objectContaining({
          text: "尚未保存的文案编辑",
          version: 7,
        }),
      }),
    );
  });

  it("StrictMode 重放 effect 后仍能完成首次版本恢复", async () => {
    const value = replicaStudio(3);
    value.state = {
      ...value.state,
      draft: {
        ...value.state.draft,
        prompt: "",
        script: { ...value.state.draft.script, text: "", confirmed: false },
      },
    };
    mockSavedReplicaVersions();
    useStudio.mockReturnValue(value);
    render(
      <StrictMode>
        <ReplicaPage />
      </StrictMode>,
    );

    await waitFor(() => expect(value.patchDraft).toHaveBeenCalled());
    expect(replicaApi.getLatestProjectShotCards).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/正在读取已保存/)).toBeNull();
  });

  it("恢复完成后主动清空 Prompt 保留空草稿", async () => {
    const value = replicaStudio(3);
    value.state = {
      ...value.state,
      draft: { ...value.state.draft, prompt: "" },
    };
    mockSavedReplicaVersions();
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    const textarea = await screen.findByLabelText("最终提示词");
    fireEvent.change(textarea, { target: { value: "加载期间的编辑" } });
    fireEvent.change(textarea, { target: { value: "" } });
    await waitFor(() =>
      expect(replicaApi.getLatestGenerationPrompt).toHaveBeenCalled(),
    );
    expect(textarea).toHaveValue("");
    expect(value.patchDraft).toHaveBeenLastCalledWith(
      expect.objectContaining({ prompt: "", promptEdited: true }),
    );
  });

  it("主动清空 Prompt 后离页再返回仍保留空草稿", async () => {
    const first = replicaStudio(3);
    mockSavedReplicaVersions();
    useStudio.mockReturnValue(first);
    const firstView = render(<ReplicaPage />);
    const textarea = await screen.findByLabelText("最终提示词");
    fireEvent.change(textarea, { target: { value: "准备清空" } });
    fireEvent.change(textarea, { target: { value: "" } });
    expect(first.patchDraft).toHaveBeenLastCalledWith({
      prompt: "",
      promptEdited: true,
    });
    firstView.unmount();

    const reopened = replicaStudio(3);
    reopened.state = {
      ...reopened.state,
      draft: {
        ...reopened.state.draft,
        prompt: "",
        promptEdited: true,
      },
    };
    mockSavedReplicaVersions();
    useStudio.mockReturnValue(reopened);
    render(<ReplicaPage />);

    await waitFor(() => expect(reopened.patchDraft).toHaveBeenCalled());
    expect(screen.getByLabelText("最终提示词")).toHaveValue("");
    expect(reopened.patchDraft).toHaveBeenCalledWith(
      expect.objectContaining({ prompt: "", promptEdited: true }),
    );
  });

  it("同项目本地文案保留标题、来源原文和主动清空内容", async () => {
    const value = replicaStudio();
    value.state = {
      ...value.state,
      draft: {
        ...value.state.draft,
        script: {
          ...value.state.draft.script,
          title: "本地改过的作品名",
          original: "音频提取的来源原文",
          text: "",
          version: 9,
        },
        scriptEdited: true,
      },
    };
    mockSavedReplicaVersions();
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    await waitFor(() => expect(value.patchDraft).toHaveBeenCalled());
    expect(value.patchDraft).toHaveBeenCalledWith(
      expect.objectContaining({
        script: expect.objectContaining({
          title: "本地改过的作品名",
          original: "音频提取的来源原文",
          text: "",
          version: 9,
        }),
        scriptEdited: true,
      }),
    );
  });

  it("空态挂载后云端编辑稿迟到，版本恢复不覆盖云端 Prompt", async () => {
    const initial = replicaStudio(3);
    initial.state = {
      ...initial.state,
      draft: { ...initial.state.draft, prompt: "", promptEdited: false },
    };
    let current = initial;
    let resolveShots:
      | ((
          value: Awaited<
            ReturnType<typeof replicaApi.getLatestProjectShotCards>
          >,
        ) => void)
      | undefined;
    mockSavedReplicaVersions();
    replicaApi.getLatestProjectShotCards.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveShots = resolve;
        }),
    );
    useStudio.mockImplementation(() => current);
    const view = render(<ReplicaPage />);

    current = {
      ...initial,
      state: {
        ...initial.state,
        draft: {
          ...initial.state.draft,
          prompt: "云端迟到的本地编辑稿",
          promptEdited: true,
        },
      },
    };
    view.rerender(<ReplicaPage />);
    resolveShots?.({
      id: "scv-cloud-late",
      payload: {
        source_analysis_version_id: "av-saved",
        duration_seconds: 8,
        shots: [shot],
      },
    });

    await waitFor(() => expect(current.patchDraft).toHaveBeenCalled());
    expect(screen.getByLabelText("最终提示词")).toHaveValue(
      "云端迟到的本地编辑稿",
    );
    expect(current.patchDraft).toHaveBeenCalledWith(
      expect.objectContaining({
        prompt: "云端迟到的本地编辑稿",
        promptEdited: true,
      }),
    );
  });

  it("项目尚无分析版本时按空态恢复，不显示读取失败", async () => {
    const value = replicaStudio();
    const notFound = Object.assign(
      new Error("Project has no analysis version."),
      {
        status: 404,
        code: "ANALYSIS_NOT_FOUND",
      },
    );
    replicaApi.getLatestProjectAnalysis.mockRejectedValue(notFound);
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    await waitFor(() =>
      expect(replicaApi.getLatestProjectAnalysis).toHaveBeenCalledWith(
        "project-1",
      ),
    );
    expect(screen.queryByRole("alert")).toBeNull();
    expect(
      screen.getByRole("button", { name: "启动 AI 拆解" }),
    ).toBeInTheDocument();
  });

  it("恢复失败时显示重试，重试成功后载入原版本", async () => {
    const value = replicaStudio();
    replicaApi.getLatestProjectShotCards
      .mockRejectedValueOnce(new Error("读取超时"))
      .mockResolvedValueOnce({
        id: "scv-retry",
        payload: {
          source_analysis_version_id: "av-retry",
          duration_seconds: 8,
          shots: [shot],
        },
      });
    replicaApi.getLatestProjectAnalysis.mockResolvedValue({
      id: "av-retry",
      payload: { analysis: { original_script: "重试恢复", shots: [shot] } },
    });
    replicaApi.getLatestGenerationPrompt.mockResolvedValue({
      stale: true,
      stale_reasons: ["shot cards changed"],
      version: null,
    });
    replicaApi.getLatestScriptVersion.mockResolvedValue({
      stale: true,
      stale_reasons: ["analysis changed"],
      version: null,
    });
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    expect(await screen.findByText(/读取超时/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试读取历史分镜" }));

    expect((await screen.findAllByDisplayValue(/院落/)).length).toBeGreaterThan(
      0,
    );
    expect(replicaApi.getLatestProjectShotCards).toHaveBeenCalledTimes(2);
    expect(replicaApi.startVideoAnalysis).not.toHaveBeenCalled();
  });

  async function openReplicaAndAnalyze(
    options: { existingShotCards?: boolean } = {},
  ) {
    const value = replicaStudio();
    mockAnalysisSuccess(options);
    useStudio.mockReturnValue(value);
    const view = render(<ReplicaPage />);
    fireEvent.click(screen.getByRole("button", { name: "启动 AI 拆解" }));
    await screen.findAllByDisplayValue(/院落/);
    view.rerender(<ReplicaPage />);
    return value;
  }

  it("上传参考视频后写入草稿的项目与来源", async () => {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "replica",
      draft: {
        ...value.state.draft,
        projectId: undefined,
        sourceId: undefined,
      },
    };
    value.data = { ...value.data, projects: [] };
    replicaLive.uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "project-upload-1",
      assetId: "asset-upload-1",
      project: {
        id: "project-upload-1",
        owner_user_id: "user-1",
        name: "a",
        status: "DRAFT",
        reference_asset_id: "asset-upload-1",
        reference_upload_status: "READY",
        analysis_status: "NOT_READY",
      },
      asset: {
        id: "asset-upload-1",
        name: "a · 来源视频",
        kind: "video",
        group: "a",
        source: "项目上传",
        saved: true,
      },
    });
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    fireEvent.click(screen.getByRole("button", { name: "上传参考视频" }));
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    expect(input).not.toBeNull();
    Object.defineProperty(input, "files", { value: [new File([], "a.mp4")] });
    fireEvent.change(input);

    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith(
        expect.objectContaining({
          projectId: "project-upload-1",
          sourceId: "asset-upload-1",
          sourceAssetId: "asset-upload-1",
          prompt: "",
          script: expect.objectContaining({ text: "" }),
        }),
      ),
    );
    expect(value.updateData).toHaveBeenCalledOnce();
    const update = vi.mocked(value.updateData).mock.calls[0][0];
    const updated = update(value.data);
    expect(updated.projects).toContainEqual(
      expect.objectContaining({ id: "project-upload-1" }),
    );
    expect(updated.assets).toContainEqual(
      expect.objectContaining({ id: "asset-upload-1" }),
    );
    expect(screen.queryByText("先导入参考视频")).toBeNull();
    expect(
      screen.getByRole("button", { name: "启动 AI 拆解" }),
    ).toBeInTheDocument();
  });

  it("上传 B 项目时清空 A 项目的 Prompt，并阻止重渲染重启 A 的恢复", async () => {
    const initial = replicaStudio();
    initial.state = {
      ...initial.state,
      draft: {
        ...initial.state.draft,
        prompt: "A 项目的 Prompt",
        script: { ...initial.state.draft.script, text: "A 项目的文案" },
      },
    };
    let current = initial;
    let resolveRestore:
      | ((
          value: Awaited<
            ReturnType<typeof replicaApi.getLatestProjectShotCards>
          >,
        ) => void)
      | undefined;
    let resolveUpload:
      | ((
          value: Awaited<
            ReturnType<typeof replicaLive.uploadWorkbenchSourceVideo>
          >,
        ) => void)
      | undefined;
    replicaApi.getLatestProjectShotCards.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRestore = resolve;
        }),
    );
    replicaLive.uploadWorkbenchSourceVideo.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve;
        }),
    );
    useStudio.mockImplementation(() => current);
    const view = render(<ReplicaPage />);
    expect(replicaApi.getLatestProjectShotCards).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "更换来源视频" }));
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    Object.defineProperty(input, "files", { value: [new File([], "b.mp4")] });
    fireEvent.change(input);

    current = { ...initial, patchDraft: vi.fn() };
    view.rerender(<ReplicaPage />);
    expect(replicaApi.getLatestProjectShotCards).toHaveBeenCalledTimes(1);

    resolveUpload?.({ projectId: "project-b", assetId: "asset-b" });
    await waitFor(() =>
      expect(initial.patchDraft).toHaveBeenCalledWith(
        expect.objectContaining({
          projectId: "project-b",
          prompt: "",
          script: expect.objectContaining({ text: "" }),
        }),
      ),
    );
    resolveRestore?.({
      id: "scv-a-late",
      payload: {
        source_analysis_version_id: "av-a",
        duration_seconds: 8,
        shots: [shot],
      },
    });
    await Promise.resolve();

    expect(current.patchDraft).not.toHaveBeenCalledWith(
      expect.objectContaining({ projectId: "project-1" }),
    );
    expect(initial.patchDraft).toHaveBeenCalledWith(
      expect.objectContaining({ prompt: "" }),
    );
  });

  it("选择已有项目后忽略仍在上传的旧来源", async () => {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "replica",
      draft: {
        ...value.state.draft,
        projectId: undefined,
        sourceId: undefined,
      },
    };
    value.data = {
      ...value.data,
      projects: [
        {
          id: "project-1",
          owner_user_id: "user-1",
          name: "已有项目",
          status: "READY",
          reference_asset_id: "asset-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        },
      ],
    };
    let resolveUpload:
      | ((value: { projectId: string; assetId: string }) => void)
      | undefined;
    replicaLive.uploadWorkbenchSourceVideo.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve;
        }),
    );
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    fireEvent.click(screen.getByRole("button", { name: "上传参考视频" }));
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    Object.defineProperty(input, "files", { value: [new File([], "a.mp4")] });
    fireEvent.change(input);
    fireEvent.change(screen.getByLabelText("选择已有项目"), {
      target: { value: "project-1" },
    });

    expect(
      replicaLive.uploadWorkbenchSourceVideo.mock.calls[0]?.[2]?.aborted,
    ).toBe(true);
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith(
        expect.objectContaining({
          projectId: "project-1",
          sourceId: "asset-1",
          sourceAssetId: "asset-1",
        }),
      ),
    );

    resolveUpload?.({ projectId: "late-project", assetId: "late-asset" });
    await Promise.resolve();
    expect(value.patchDraft).not.toHaveBeenCalledWith(
      expect.objectContaining({ projectId: "late-project" }),
    );
  });

  it("拆解只保存事实，最终提示词等待确认首帧和文案", async () => {
    await openReplicaAndAnalyze();
    expect(screen.getByLabelText("最终提示词")).toHaveValue("");
    expect(
      screen.getByRole("button", { name: "去 AI 视频创作" }),
    ).toBeDisabled();
    expect(replicaLive.runReplicaGeneration).not.toHaveBeenCalled();
  });

  it("可以复制拆解后的完整分镜表", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const value = await openReplicaAndAnalyze();

    fireEvent.click(screen.getByRole("button", { name: "复制分镜表" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledOnce());
    expect(writeText.mock.calls[0]?.[0]).toContain(
      "镜头编号\t开始(秒)\t结束(秒)",
    );
    expect(writeText.mock.calls[0]?.[0]).toContain("s1\t0\t8");
    expect(value.notify).toHaveBeenCalledWith(
      "分镜表已复制，可直接粘贴到 Excel 或在线表格。",
    );
  });

  it("编辑后的 Prompt 可保存为用户自定义提示词", async () => {
    const value = await openReplicaAndAnalyze();
    replicaApi.saveGenerationPrompt.mockResolvedValue({ id: "sp-1" });

    const textarea = screen.getByLabelText("最终提示词");
    fireEvent.change(textarea, { target: { value: "我改过的复刻提示词" } });
    fireEvent.click(screen.getByRole("button", { name: "保存为自定义提示词" }));
    fireEvent.change(screen.getByLabelText("自定义提示词名称"), {
      target: { value: "我的复刻" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

    await waitFor(() =>
      expect(replicaApi.saveGenerationPrompt).toHaveBeenCalledWith(
        "project-1",
        expect.objectContaining({
          name: "我的复刻",
          prompt_text: "我改过的复刻提示词",
        }),
      ),
    );
    expect(value.notify).toHaveBeenCalledWith(
      expect.stringContaining("我的提示词"),
    );
  });

  it("保存 A 期间继续编辑 B，A 的迟到响应不清除 B 的编辑标记", async () => {
    const value = replicaStudio(3);
    mockSavedReplicaVersions();
    let resolveSave: ((value: { id: string }) => void) | undefined;
    replicaApi.saveGenerationPrompt.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    const textarea = await screen.findByLabelText("最终提示词");
    fireEvent.change(textarea, { target: { value: "准备保存的 A" } });
    fireEvent.click(screen.getByRole("button", { name: "保存为自定义提示词" }));
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
    fireEvent.change(textarea, { target: { value: "继续编辑的 B" } });
    resolveSave?.({ id: "prompt-a" });

    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith(
        "提交时的 Prompt 已保存，当前修改仍需再次保存。",
      ),
    );
    expect(value.patchDraft).not.toHaveBeenCalledWith({ promptEdited: false });
    expect(textarea).toHaveValue("继续编辑的 B");
  });

  it("新 Prompt 保存成功后保留当前编辑内容", async () => {
    const value = replicaStudio(3);
    mockSavedReplicaVersions();
    replicaApi.saveGenerationPrompt.mockResolvedValue({ id: "prompt-new" });
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    const textarea = await screen.findByLabelText("最终提示词");
    fireEvent.change(textarea, { target: { value: "刚保存的新 Prompt" } });
    fireEvent.click(screen.getByRole("button", { name: "保存为自定义提示词" }));
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        prompt: "刚保存的新 Prompt",
        promptEdited: true,
      }),
    );

    await waitFor(() => expect(value.patchDraft).toHaveBeenCalled());
    expect(textarea).toHaveValue("刚保存的新 Prompt");
    expect(value.patchDraft).toHaveBeenLastCalledWith(
      expect.objectContaining({
        prompt: "刚保存的新 Prompt",
        promptEdited: true,
      }),
    );
    expect(screen.queryByText(/正在读取已保存/)).toBeNull();
  });

  it("保存响应在切换项目后返回，不清除新项目编辑标记", async () => {
    const value = replicaStudio(3);
    mockSavedReplicaVersions();
    let current = value;
    let resolveSave: ((value: { id: string }) => void) | undefined;
    replicaApi.saveGenerationPrompt.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );
    useStudio.mockImplementation(() => current);
    const view = render(<ReplicaPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "保存为自定义提示词" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
    current = {
      ...value,
      state: {
        ...value.state,
        draft: { ...value.state.draft, projectId: "project-2" },
      },
      data: {
        ...value.data,
        projects: [
          ...value.data.projects,
          {
            ...value.data.projects[0],
            id: "project-2",
            name: "第二项目",
            reference_asset_id: "asset-2",
          },
        ],
      },
    };
    view.rerender(<ReplicaPage />);
    resolveSave?.({ id: "prompt-a" });
    await Promise.resolve();

    expect(value.patchDraft).not.toHaveBeenCalledWith({ promptEdited: false });
  });

  it("保存响应在页面卸载后返回，不再修改工作区标记", async () => {
    const value = replicaStudio(3);
    mockSavedReplicaVersions();
    let resolveSave: ((value: { id: string }) => void) | undefined;
    replicaApi.saveGenerationPrompt.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );
    useStudio.mockReturnValue(value);
    const view = render(<ReplicaPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "保存为自定义提示词" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
    view.unmount();
    resolveSave?.({ id: "prompt-a" });
    await Promise.resolve();

    expect(value.patchDraft).not.toHaveBeenCalledWith({ promptEdited: false });
  });

  it("存入我的提示词：一键直存并使用默认命名", async () => {
    await openReplicaAndAnalyze();
    await prepareFinalReplica();
    replicaApi.saveGenerationPrompt.mockResolvedValue({ id: "sp-2" });

    fireEvent.click(screen.getByRole("button", { name: "存入我的提示词" }));

    await waitFor(() =>
      expect(replicaApi.saveGenerationPrompt).toHaveBeenCalledWith(
        "project-1",
        expect.objectContaining({
          name: expect.stringContaining("复刻提示词"),
          prompt_text: "最终新稿",
        }),
      ),
    );
  });

  it("去 AI 视频创作：带入最终提示词并切换页面", async () => {
    const value = await openReplicaAndAnalyze();
    await prepareFinalReplica();

    fireEvent.click(screen.getByRole("button", { name: "去 AI 视频创作" }));

    expect(value.patchDraft).toHaveBeenCalledWith(
      expect.objectContaining({ prompt: "最终新稿" }),
    );
    expect(value.navigate).toHaveBeenCalledWith("video");
  });

  it("审计员人物替换链路只读且不自动写入参考选择", async () => {
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "replacement",
      draft: { ...value.state.draft, projectId: "project-1" },
    };
    value.data = {
      ...value.data,
      projects: [
        {
          id: "project-1",
          name: "只读替换项目",
          owner_user_id: "auditor-1",
          status: "ACTIVE",
          reference_asset_id: "asset-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        },
      ],
    };
    value.user = {
      id: value.user.id,
      username: value.user.username,
      display_name: "审计员",
      role: "auditor",
    };
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    expect(
      screen.getByRole("button", { name: "stub-选择人物" }),
    ).toHaveAttribute("data-read-only", "true");
    expect(
      screen.getByRole("button", { name: "stub-确认源画面" }),
    ).toHaveAttribute("data-read-only", "true");
    vi.mocked(value.patchDraft).mockClear();
    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "stub-确认置换首帧" }),
    );
    expect(
      screen.getByRole("heading", { name: "3 文案与生成" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /下一步/ })).toBeNull();
    expect(value.patchDraft).not.toHaveBeenCalledWith(
      expect.objectContaining({ firstFrameId: expect.any(String) }),
    );
    expect(replicaApi.selectCharacterReferences).not.toHaveBeenCalled();
  });

  it("审计员不能从空人物替换页切换项目", () => {
    const value = studio({
      review: false,
      user: {
        id: "auditor-1",
        username: "auditor-1",
        display_name: "审计员",
        role: "auditor",
      },
    });
    value.state = {
      ...value.state,
      page: "replacement",
      draft: { ...value.state.draft, projectId: undefined },
    };
    value.data = {
      ...value.data,
      projects: [
        {
          id: "project-1",
          name: "可查看项目",
          owner_user_id: "auditor-1",
          status: "ACTIVE",
          reference_asset_id: "asset-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        },
      ],
    };
    useStudio.mockReturnValue(value);
    render(<ReplacementPage />);

    const selector = screen.getByLabelText("选择项目");
    expect(selector).toBeDisabled();
    fireEvent.change(selector, { target: { value: "project-1" } });
    expect(value.patchDraft).not.toHaveBeenCalled();
  });

  it("切换为审计员后保留已恢复的人物与首帧展示", async () => {
    replicaApi.selectCharacterReferences.mockResolvedValue({
      id: "reference-selection-1",
    });
    const value = studio({ review: false });
    value.state = {
      ...value.state,
      page: "replacement",
      draft: { ...value.state.draft, projectId: "project-1" },
    };
    value.data = {
      ...value.data,
      projects: [
        {
          id: "project-1",
          name: "角色切换项目",
          owner_user_id: "customer-1",
          status: "ACTIVE",
          reference_asset_id: "asset-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        },
      ],
    };
    useStudio.mockReturnValue(value);
    const view = render(<ReplacementPage />);
    fireEvent.click(screen.getByRole("button", { name: "stub-选择人物" }));
    fireEvent.click(screen.getByRole("button", { name: "stub-确认源画面" }));
    await screen.findByRole("button", { name: "stub-确认置换首帧" });

    value.user = {
      id: "auditor-1",
      username: "auditor-1",
      display_name: "审计员",
      role: "auditor",
    };
    vi.mocked(value.patchDraft).mockClear();
    replicaApi.selectCharacterReferences.mockClear();
    view.rerender(<ReplacementPage />);

    expect(
      screen.getByRole("button", { name: "stub-确认置换首帧" }),
    ).toHaveAttribute("data-read-only", "true");
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(replicaApi.selectCharacterReferences).not.toHaveBeenCalled();
  });
});
