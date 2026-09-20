/**
 * 视频复刻（模块①）流程与布局缺陷的回归测试。
 *
 * 本文件只测试、不改源码：A1/A2/A3/B2/C1/C5 六项缺陷的期望行为在此固化。
 * 对应修复均已落地，用例全绿，这些断言的作用是防止缺陷回潮。
 *
 * 隔离策略：只部分 mock `../api` 与 `./live`——
 *  - `./live` 的 `uploadWorkbenchSourceVideo` 保持真实实现（C1 测的是它本体，
 *    mock 掉就等于测 mock），它的底层 api 依赖（createProject /
 *    createVideoUploadIntent / getAssetDownloadUrl）在 `../api` 侧打桩，
 *    并以 `upload_required: false` 跳过真实 PUT，使整条链路确定性、无网络。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { StudioContextValue } from "./types";

const { useStudio } = vi.hoisted(() => ({
  useStudio: vi.fn<() => StudioContextValue>(),
}));

vi.mock("./context", () => ({ useStudio }));

const api = vi.hoisted(() => ({
  createProject: vi.fn(),
  createVideoUploadIntent: vi.fn(),
  uploadReferenceVideo: vi.fn(),
  completeVideoUpload: vi.fn(),
  getAssetDownloadUrl: vi.fn(),
  listUserSavedPrompts: vi.fn(async () => []),
  selectCharacterReferences: vi.fn(),
  startVideoAnalysis: vi.fn(),
  waitForAnalysisTask: vi.fn(),
  getAnalysisTask: vi.fn(),
  getLatestProjectShotCards: vi.fn(),
  getLatestProjectAnalysis: vi.fn(),
  getLatestGenerationPrompt: vi.fn(),
  getLatestScriptVersion: vi.fn(),
  getLatestScriptRewriteTask: vi.fn(async () => null),
  getScriptRewriteTask: vi.fn(),
  rewriteProjectScript: vi.fn(),
  waitForScriptRewriteTask: vi.fn(),
  getLatestProjectFirstFrameSelection: vi.fn(async () => null),
  getGenerationPriceQuote: vi.fn(),
  saveGenerationPrompt: vi.fn(),
  createScriptVersion: vi.fn(),
  compileGenerationPrompt: vi.fn(),
  saveShotCards: vi.fn(),
}));

// 注意：这里不含 uploadWorkbenchSourceVideo——它必须保持真实实现。
const live = vi.hoisted(() => ({
  loadSavedScriptList: vi.fn(async () => []),
  readVideoDuration: vi.fn(async () => 8),
  readAudioDuration: vi.fn(async () => 42),
  uploadReferenceAudioMaterial: vi.fn(),
  uploadVideoMaterial: vi.fn(),
  runReplicaGeneration: vi.fn(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...api,
}));
vi.mock("./live", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...live,
}));

// 叶子组件打桩：把组合层（ReplicaPage）与选择器实现隔离。
vi.mock("../CharacterSelection", () => ({
  CharacterSelection: () => <span>stub-人物选择</span>,
}));
vi.mock("../SourceFrameSelection", () => ({
  SourceFrameSelection: () => <span>stub-源画面选择</span>,
}));
vi.mock("../FirstFrameSelection", () => ({
  FirstFrameSelection: () => <span>stub-置换首帧选择</span>,
}));

import { ReplicaPage } from "./CreationPages";
import { uploadWorkbenchSourceVideo } from "./live";

/**
 * 这些断言等的是异步恢复链路（草稿读取、素材注册）落地，用 4s 上限：
 * 正常在毫秒级返回，上限只用来兜住偶发的渲染排队。
 */
const RED_TIMEOUT = { timeout: 4000 };

function studio(
  overrides: Partial<StudioContextValue> = {},
): StudioContextValue {
  return {
    state: {
      page: "replica",
      draft: {
        id: "draft-1",
        ipId: "person-1",
        sourceId: "asset-1",
        sourceAssetId: "asset-1",
        projectId: "project-1",
        selectedShotId: "",
        script: {
          id: "script-1",
          title: "复刻测试项目",
          original: "",
          text: "",
          version: 1,
          confirmed: false,
        },
        prompt: "",
        referenceIds: [],
        resolution: "768P",
        ratio: "16:9",
        duration: 8,
        count: 1,
        frameConfirmed: false,
        style: "standard",
        subtitles: false,
        quoteRevision: 1,
      },
      savedScripts: [],
      favorites: [],
    },
    data: {
      people: [],
      assets: [],
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
    review: false,
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

/** 复刻页已挂载、已选定来源视频的项目态。 */
function replicaStudio(): StudioContextValue {
  const value = studio();
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

function mockAnalysisSuccess() {
  api.startVideoAnalysis.mockResolvedValue({
    id: "task-1",
    status: "RUNNING",
  });
  api.waitForAnalysisTask.mockResolvedValue({
    id: "task-1",
    status: "SUCCEEDED",
  });
  api.getLatestProjectShotCards.mockResolvedValue({
    id: "scv-1",
    payload: {
      source_analysis_version_id: "av-1",
      duration_seconds: 8,
      shots: [shot],
    },
  });
  api.getLatestProjectAnalysis.mockResolvedValue({
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
}

/** 拆解按钮在不同阶段文案不同，用语义正则定位。 */
function analysisButton() {
  return screen.getByRole("button", { name: /拆解/ });
}

beforeEach(() => {
  useStudio.mockReset();
  api.createProject.mockReset();
  api.createVideoUploadIntent.mockReset();
  api.uploadReferenceVideo.mockReset();
  api.completeVideoUpload.mockReset();
  api.getAssetDownloadUrl
    .mockReset()
    .mockImplementation(async (assetId: string) => ({
      url: `https://signed.example/${assetId}.mp4`,
    }));
  api.listUserSavedPrompts.mockReset().mockResolvedValue([]);
  api.startVideoAnalysis.mockReset();
  api.waitForAnalysisTask.mockReset();
  api.getAnalysisTask.mockReset();
  api.getLatestProjectShotCards.mockReset().mockResolvedValue(null);
  api.getLatestProjectAnalysis
    .mockReset()
    .mockResolvedValue({ id: "av-empty", payload: {} });
  api.getLatestGenerationPrompt.mockReset().mockResolvedValue({
    stale: false,
    stale_reasons: [],
    version: null,
  });
  api.getLatestScriptVersion.mockReset().mockResolvedValue({
    stale: false,
    stale_reasons: [],
    version: null,
  });
  api.getLatestProjectFirstFrameSelection.mockReset().mockResolvedValue(null);
  api.getGenerationPriceQuote.mockReset().mockResolvedValue({
    resolution: "768P",
    duration_seconds: 8,
    quantity: 1,
    unit_price_fen_per_second: 120,
    estimated_seconds: 8,
    estimated_price_fen: 960,
  });
  api.saveShotCards.mockReset();
  api.saveGenerationPrompt.mockReset();
  api.compileGenerationPrompt.mockReset();
  api.createScriptVersion.mockReset();
  api.selectCharacterReferences.mockReset();
  live.loadSavedScriptList.mockReset().mockResolvedValue([]);
  live.runReplicaGeneration.mockReset();
});

describe("复刻页 A 类：上传与拆解流程", () => {
  it("A1 上传参考视频成功后跳转到复刻页", async () => {
    const value = replicaStudio();
    // 非复刻页触发上传：期望上传完成后跳回复刻页。
    value.state = { ...value.state, page: "copy" };
    api.createProject.mockResolvedValue({
      id: "project-new",
      owner_user_id: "employee_1",
      name: "新上传项目",
      status: "ACTIVE",
      reference_asset_id: null,
      reference_upload_status: "NOT_STARTED",
      analysis_status: "NOT_STARTED",
    });
    api.createVideoUploadIntent.mockResolvedValue({
      asset_id: "asset-new",
      project_id: "project-new",
      method: null,
      url: "",
      headers: {},
      expires_at: "2026-01-01T00:00:00Z",
      upload_required: false,
    });
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    fireEvent.change(screen.getByLabelText("上传参考视频"), {
      target: {
        files: [new File(["x"], "参考视频.mp4", { type: "video/mp4" })],
      },
    });

    await waitFor(
      () => expect(value.navigate).toHaveBeenCalledWith("replica"),
      {
        timeout: 4000,
      },
    );
  });

  it("A2 等待期间来源项目被切换后，拆解按钮复位为可点击", async () => {
    const value = replicaStudio();
    mockAnalysisSuccess();
    // 拆解停在「等待回执」：resolve 由测试手动触发，精确制造逃逸路径。
    let finish!: (result: unknown) => void;
    api.waitForAnalysisTask.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    useStudio.mockReturnValue(value);
    const view = render(<ReplicaPage />);
    await screen.findByRole("button", { name: /拆解/ });
    fireEvent.click(analysisButton());
    await waitFor(() => expect(finish).toBeDefined());

    // 用户在等待期间切到另一个项目 → isCurrentAnalysis() 之后必然失败。
    const switched = replicaStudio();
    switched.state = {
      ...switched.state,
      draft: { ...switched.state.draft, projectId: "project-2" },
    };
    switched.data = {
      ...switched.data,
      projects: [
        ...switched.data.projects,
        {
          ...switched.data.projects[0],
          id: "project-2",
          name: "另一个项目",
        },
      ],
    };
    useStudio.mockReturnValue(switched);
    view.rerender(<ReplicaPage />);

    // 迟到回执到达：走 `if (!isCurrentAnalysis()) return;` 逃逸分支。
    finish({ id: "task-1", status: "SUCCEEDED" });
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    await waitFor(() => expect(analysisButton()).toBeEnabled(), RED_TIMEOUT);
  });
});

describe("复刻页 A 类：第 3 节渲染条件", () => {
  it("A3 尚未拆解时第 3 节仍渲染口播文案输入框", async () => {
    const value = replicaStudio();
    // 有项目、无分镜：期望仍给出文案输入，而不是只有一句 Hint。
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    await screen.findByRole("button", { name: /拆解/ });
    expect(
      await screen.findByPlaceholderText(
        "完成拆解后，原视频口播文案会出现在这里。",
        {},
        RED_TIMEOUT,
      ),
    ).toBeInTheDocument();
  });
});

describe("复刻页 B 类：拆解结果回填写入", () => {
  it("B2-a 草稿内容未被改动过时，即使 scriptEdited 已置位也要回填新拆解原文", async () => {
    const value = replicaStudio();
    // 真实污染源：scriptEdited 随云端草稿持久化，一次编辑后永久为 true，
    // 于是后续拆解结果再也回填不进草稿（文案永远停在旧项目原文）。
    value.state = {
      ...value.state,
      draft: {
        ...value.state.draft,
        script: {
          ...value.state.draft.script,
          original: "旧项目的原文",
          text: "旧项目的原文",
          confirmed: false,
        },
        scriptEdited: true,
      },
    };
    mockAnalysisSuccess();
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    // 挂载时的历史恢复也会写一次 patchDraft，先等它落地再清账。
    await waitFor(() => expect(value.patchDraft).toHaveBeenCalled());
    vi.mocked(value.patchDraft).mockClear();

    fireEvent.click(analysisButton());

    await waitFor(
      () =>
        expect(value.patchDraft).toHaveBeenCalledWith(
          expect.objectContaining({
            script: expect.objectContaining({
              original: "这栋房子的采光设计非常好",
              text: "这栋房子的采光设计非常好",
            }),
          }),
        ),
      RED_TIMEOUT,
    );
  });

  it("B2-b 用户手改过文案时不回填，保护用户编辑", async () => {
    const value = replicaStudio();
    value.state = {
      ...value.state,
      draft: {
        ...value.state.draft,
        script: {
          ...value.state.draft.script,
          original: "这栋房子的采光设计非常好",
          text: "用户手改过的终稿",
          confirmed: false,
        },
        scriptEdited: true,
      },
    };
    mockAnalysisSuccess();
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    await waitFor(() => expect(value.patchDraft).toHaveBeenCalled());
    vi.mocked(value.patchDraft).mockClear();

    fireEvent.click(analysisButton());

    // 等拆解收尾信号，确保回填判断已经执行过。
    await waitFor(
      () =>
        expect(value.notify).toHaveBeenCalledWith(
          "拆解完成。请确认文案和置换首帧，再合成最终提示词。",
        ),
      RED_TIMEOUT,
    );
    expect(value.patchDraft).not.toHaveBeenCalledWith(
      expect.objectContaining({ script: expect.anything() }),
    );
  });
});

describe("复刻页 C 类：素材签名与提示词渲染", () => {
  it("C1 上传返回的来源素材带签名 URL", async () => {
    api.createProject.mockResolvedValue({
      id: "project-c1",
      owner_user_id: "employee_1",
      name: "C1 来源项目",
      status: "ACTIVE",
      reference_asset_id: null,
      reference_upload_status: "NOT_STARTED",
      analysis_status: "NOT_STARTED",
    });
    api.createVideoUploadIntent.mockResolvedValue({
      asset_id: "asset-c1",
      project_id: "project-c1",
      method: null,
      url: "",
      headers: {},
      expires_at: "2026-01-01T00:00:00Z",
      upload_required: false,
    });

    const result = await uploadWorkbenchSourceVideo(
      new File(["x"], "来源.mp4", { type: "video/mp4" }),
      () => {},
    );

    expect(result.asset?.id).toBe("asset-c1");
    expect(result.asset?.url).toBe("https://signed.example/asset-c1.mp4");
  });

  it("C5 拆解后的提示词在单一文本框中直接展示与编辑", async () => {
    const value = replicaStudio();
    value.state = {
      ...value.state,
      draft: {
        ...value.state.draft,
        firstFrameId: undefined,
        prompt: "[0.0-2.5s] 近景，镜头缓推庭院\n[2.5-5.0s] 中景，人物入画",
        promptEdited: true,
      },
    };
    mockAnalysisSuccess();
    useStudio.mockReturnValue(value);
    const _view = render(<ReplicaPage />);

    await screen.findByRole("button", { name: /拆解/ });
    fireEvent.click(analysisButton());
    await waitFor(
      () =>
        expect(value.notify).toHaveBeenCalledWith(
          "拆解完成。请确认文案和置换首帧，再合成最终提示词。",
        ),
      RED_TIMEOUT,
    );

    // 单一文本框契约：提示词正文直接在可编辑框中展示，不再有第二渲染体。
    await waitFor(() => {
      const box = screen.getByLabelText("最终提示词") as HTMLTextAreaElement;
      expect(box.value.includes("镜头缓推庭院")).toBe(true);
    }, RED_TIMEOUT);
    expect(
      screen.getByRole("button", { name: "去 AI 视频创作" }),
    ).toBeEnabled();
  });
});
