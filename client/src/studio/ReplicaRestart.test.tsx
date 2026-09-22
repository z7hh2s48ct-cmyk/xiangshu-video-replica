/**
 * 复刻页「开始新的复刻」（LEFTOVER-ON-OPEN）。
 *
 * 在这之前，复刻页一旦绑上项目就再也回不到「先导入参考视频」的空态：
 * 空态的渲染条件是 `stage === "source" && !project`，而 project 由草稿里的
 * projectId 推出来，页面没有任何入口能把这个绑定解开。用户想重新开始只能
 * 「更换来源视频」——必须先有一个新文件。这里固化的是那条重来的出路。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { StudioContextValue } from "./types";

const { useStudio } = vi.hoisted(() => ({
  useStudio: vi.fn<() => StudioContextValue>(),
}));

vi.mock("./context", () => ({ useStudio }));

const api = vi.hoisted(() => ({
  listUserSavedPrompts: vi.fn(async () => []),
  getLatestProjectShotCards: vi.fn(),
  getLatestProjectAnalysis: vi.fn(),
  getLatestGenerationPrompt: vi.fn(),
  getLatestScriptVersion: vi.fn(),
  getLatestScriptRewriteTask: vi.fn(async () => null),
  getLatestProjectFirstFrameSelection: vi.fn(async () => null),
  cancelAnalysisTask: vi.fn(),
  startVideoAnalysis: vi.fn(),
  getAnalysisTask: vi.fn(),
  waitForAnalysisTask: vi.fn(),
  getAssetDownloadUrl: vi.fn(async (assetId: string) => ({
    url: `https://signed.example/${assetId}.mp4`,
  })),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...api,
}));

// 叶子组件打桩：这里只关心复刻页的阶段与绑定，不关心选择器实现。
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

function studio(overrides: Partial<StudioContextValue> = {}) {
  const value: StudioContextValue = {
    state: {
      page: "replica",
      draft: {
        id: "draft-1",
        sourceId: "asset-1",
        sourceAssetId: "asset-1",
        projectId: "project-1",
        selectedShotId: "",
        script: {
          id: "script-1",
          title: "上次的复刻项目",
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
      materials: [],
      projects: [
        {
          id: "project-1",
          name: "上次的复刻项目",
          owner_user_id: "employee_1",
          status: "ACTIVE",
          reference_asset_id: "asset-1",
          reference_upload_status: "READY",
          analysis_status: "READY",
        },
      ],
      errors: [],
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
  return value;
}

beforeEach(() => {
  useStudio.mockReset();
  api.getLatestProjectShotCards.mockReset().mockResolvedValue(null);
  api.getLatestProjectAnalysis.mockReset().mockResolvedValue(null);
  api.getLatestGenerationPrompt
    .mockReset()
    .mockResolvedValue({ version: null, stale: false });
  api.getLatestScriptVersion
    .mockReset()
    .mockResolvedValue({ version: null, stale: false });
  api.cancelAnalysisTask.mockReset();
  api.startVideoAnalysis.mockReset();
  api.getAnalysisTask.mockReset();
  api.waitForAnalysisTask.mockReset();
});

describe("复刻页重来入口", () => {
  it("点「开始新的复刻」解除项目绑定，页面回到先导入参考视频", async () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    const { rerender } = render(<ReplicaPage />);

    await screen.findByRole("button", { name: "开始新的复刻" });
    fireEvent.click(screen.getByRole("button", { name: "开始新的复刻" }));

    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith(
        expect.objectContaining({
          projectId: undefined,
          sourceId: undefined,
          sourceAssetId: undefined,
          firstFrameId: undefined,
          prompt: "",
          promptEdited: false,
        }),
      ),
    );

    // 宿主把清空后的草稿回灌，空态才真正出现。
    const cleared = studio();
    cleared.state.draft = {
      ...cleared.state.draft,
      projectId: undefined,
      sourceId: undefined,
      sourceAssetId: undefined,
    };
    useStudio.mockReturnValue(cleared);
    rerender(<ReplicaPage />);

    expect(await screen.findByText("先导入参考视频")).toBeInTheDocument();
  });

  it("同时清掉云端存的上次内容，否则重开软件又会被带回来", async () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "开始新的复刻" }),
    );

    await waitFor(() =>
      expect(value.discardSavedDraft).toHaveBeenCalledTimes(1),
    );
  });

  it("只读账号不给重来入口", async () => {
    const value = studio({
      user: {
        id: "auditor-1",
        username: "auditor-1",
        display_name: "审计员",
        role: "auditor",
      },
    });
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    await screen.findByRole("button", { name: "更换来源视频" });
    expect(screen.getByRole("button", { name: "开始新的复刻" })).toBeDisabled();
  });

  it("重来前取消旧的排队中拆解任务，否则它继续计费又看不到结果", async () => {
    const value = studio();
    value.state = {
      ...value.state,
      draft: {
        ...value.state.draft,
        analysisTaskId: "task-old",
        analysisTaskStatus: "PENDING",
      },
    };
    // S8：挂载即自动接回在途任务，重来是在这份忙碌态下发生的取消式出口。
    api.getAnalysisTask.mockResolvedValue({
      id: "task-old",
      status: "PENDING",
    });
    api.waitForAnalysisTask.mockImplementation(() => new Promise(() => {}));
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "开始新的复刻" }),
    );

    await waitFor(() =>
      expect(api.cancelAnalysisTask).toHaveBeenCalledWith("task-old"),
    );
    expect(value.patchDraft).toHaveBeenCalledWith(
      expect.objectContaining({
        analysisTaskId: undefined,
        analysisTaskStatus: undefined,
      }),
    );
  });

  it("旧任务已是终态时重来不调用取消", async () => {
    const value = studio();
    value.state = {
      ...value.state,
      draft: {
        ...value.state.draft,
        analysisTaskId: "task-done",
        analysisTaskStatus: "SUCCEEDED",
      },
    };
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "开始新的复刻" }),
    );

    await waitFor(() =>
      expect(value.discardSavedDraft).toHaveBeenCalledTimes(1),
    );
    expect(api.cancelAnalysisTask).not.toHaveBeenCalled();
  });

  it("取消失败不阻断清空", async () => {
    const value = studio();
    value.state = {
      ...value.state,
      draft: {
        ...value.state.draft,
        analysisTaskId: "task-old",
        analysisTaskStatus: "RUNNING",
      },
    };
    // S8：挂载即自动接回在途任务，重来是在这份忙碌态下发生的取消式出口。
    api.getAnalysisTask.mockResolvedValue({
      id: "task-old",
      status: "RUNNING",
    });
    api.waitForAnalysisTask.mockImplementation(() => new Promise(() => {}));
    api.cancelAnalysisTask.mockRejectedValue(new Error("网络错误"));
    useStudio.mockReturnValue(value);
    render(<ReplicaPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "开始新的复刻" }),
    );

    await waitFor(() =>
      expect(api.cancelAnalysisTask).toHaveBeenCalledWith("task-old"),
    );
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith(
        expect.objectContaining({
          projectId: undefined,
          analysisTaskId: undefined,
          analysisTaskStatus: undefined,
        }),
      ),
    );
  });
});
