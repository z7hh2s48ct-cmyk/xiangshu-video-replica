import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useLayoutEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Project } from "../api";
import { createState, patchStudioDraft } from "./state";
import type {
  StudioAsset,
  StudioContextValue,
  StudioData,
  StudioTask,
  StudioVideo,
} from "./types";

type WorkbenchUploadResult = {
  projectId: string;
  assetId: string;
  project?: Project;
  asset?: StudioAsset;
};

const {
  useStudio,
  loadTaskPreview,
  saveTaskPreview,
  uploadWorkbenchSourceVideo,
  studioVideoFromViral,
  cancelStudioTask,
  downloadStudioTaskResult,
  retryStudioTask,
  renameStudioGenerationTask,
  removeStudioGenerationTask,
  loadMoreGenerationTasks,
  loadMoreOralTasks,
  loadStudioTaskDetail,
  createViralImportTask,
  resolveViralLink,
  getViralImportTask,
  getStudioNotificationPreferences,
  updateStudioNotificationPreferences,
  getPublishSummary,
  PUBLISH_PLATFORM_LABELS,
  connectPublishAccount,
  loadPublishAccounts,
  removePublishAccount,
  requestPublishAccountVerify,
} = vi.hoisted(() => ({
  useStudio: vi.fn<() => StudioContextValue>(),
  loadTaskPreview: vi.fn(),
  saveTaskPreview: vi.fn(),
  uploadWorkbenchSourceVideo:
    vi.fn<
      (
        file: File,
        onProgress: (percent: number) => void,
        signal?: AbortSignal,
      ) => Promise<WorkbenchUploadResult>
    >(),
  studioVideoFromViral: vi.fn((item: import("../api").ViralVideoItem) => ({
    id: `${item.platform}-${item.videoId}`,
    nativeId: item.videoId,
    platformKey: item.platform,
    title: item.title,
    author: item.author,
    platform: "抖音",
    category: item.category,
    duration: "00:30",
    likes: item.likes,
    collections: item.collects,
    shares: item.shares,
    description: item.sourceDescription ?? item.title,
  })),
  cancelStudioTask: vi.fn(),
  downloadStudioTaskResult: vi.fn(),
  retryStudioTask: vi.fn(),
  renameStudioGenerationTask: vi.fn(),
  removeStudioGenerationTask: vi.fn(),
  loadMoreGenerationTasks: vi.fn(),
  loadMoreOralTasks: vi.fn(),
  loadStudioTaskDetail: vi.fn(),
  createViralImportTask: vi.fn(),
  resolveViralLink: vi.fn(),
  getViralImportTask: vi.fn(),
  getStudioNotificationPreferences: vi.fn(),
  updateStudioNotificationPreferences: vi.fn(),
  getPublishSummary: vi.fn(),
  // C5 发布账号（第一阶段）：live.ts 的这几个导出在测试里由替身接管。
  // PUBLISH_PLATFORM_LABELS 是常量映射而非函数，给出同形状字面量即可。
  PUBLISH_PLATFORM_LABELS: { douyin: "抖音", wechat_channels: "视频号" },
  connectPublishAccount: vi.fn(),
  loadPublishAccounts: vi.fn(),
  removePublishAccount: vi.fn(),
  requestPublishAccountVerify: vi.fn(),
}));

vi.mock("./context", () => ({ useStudio }));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  createViralImportTask,
  resolveViralLink,
  getViralImportTask,
  getStudioNotificationPreferences,
  updateStudioNotificationPreferences,
  getPublishSummary,
}));
vi.mock("./live", () => ({
  loadTaskPreview,
  saveTaskPreview,
  uploadWorkbenchSourceVideo,
  studioVideoFromViral,
  cancelStudioTask,
  downloadStudioTaskResult,
  retryStudioTask,
  renameStudioGenerationTask,
  removeStudioGenerationTask,
  loadMoreGenerationTasks,
  loadMoreOralTasks,
  loadStudioTaskDetail,
  PUBLISH_PLATFORM_LABELS,
  connectPublishAccount,
  loadPublishAccounts,
  removePublishAccount,
  requestPublishAccountVerify,
}));

import {
  ProfilePage,
  TaskDetailPage,
  TasksPage,
  WorkbenchPage,
} from "./MainPages";
import { formatTaskTime } from "./ui";

const taskA: StudioTask = {
  id: "task-a",
  title: "张工建房预算",
  type: "数字人口播",
  status: "completed",
  submitted: "2026-09-05 10:00",
  batchId: "batch-a",
};

const taskB: StudioTask = {
  ...taskA,
  id: "task-b",
  title: "李总项目巡检",
  batchId: "batch-b",
};

const runningTask: StudioTask = {
  id: "batch-9",
  batchId: "batch-9",
  title: "张工 · 建房预算",
  type: "数字人口播",
  status: "running",
  progress: 68,
  submitted: "2026-09-06T09:30:00",
};

function data(
  tasks: StudioTask[] = [taskA],
  videos: StudioVideo[] = [],
): StudioData {
  return {
    people: [],
    assets: [],
    videos,
    homepageVideos: videos.filter((video) => video.homepageFeatured === true),
    tasks,
    projects: [],
    errors: [],
    materials: [],
    loading: false,
    stats: null,
    analytics7: null,
    analytics30: null,
  };
}

function studio(
  selectedTaskId = taskA.id,
  overrides: Partial<StudioContextValue> = {},
): StudioContextValue {
  return {
    state: { ...createState("task-detail"), selectedTaskId },
    data: data(),
    review: false,
    user: {} as StudioContextValue["user"],
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
    extractViralCopy: vi.fn(),
    refresh: vi.fn(),
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe("V1.4 任务详情真实成片预览", () => {
  beforeEach(() => {
    useStudio.mockReset();
    loadTaskPreview.mockReset();
    downloadStudioTaskResult.mockReset();
    retryStudioTask.mockReset();
    renameStudioGenerationTask.mockReset();
    loadStudioTaskDetail.mockReset();
  });

  it("首屏外任务按 URL 中的类型和后端 ID 读取，失败可受控重试", async () => {
    const loaded = {
      ...taskA,
      id: "oral-deep",
      backendKind: "oral_task" as const,
      backendId: "deep",
    };
    loadStudioTaskDetail
      .mockRejectedValueOnce(new Error("任务不存在或无权访问"))
      .mockResolvedValueOnce(loaded);
    const value = studio("oral-deep", {
      state: {
        ...createState("task-detail"),
        selectedTaskId: "oral-deep",
        selectedTaskKind: "oral_task",
        selectedTaskBackendId: "deep",
        returnTo: "analytics",
      },
      data: data([]),
      user: { id: "account-a" } as StudioContextValue["user"],
    });
    useStudio.mockReturnValue(value);
    render(<TaskDetailPage />);

    expect(await screen.findByText("任务详情暂不可用")).toBeInTheDocument();
    expect(screen.getByText("任务不存在或无权访问")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试读取任务详情" }));
    await waitFor(() => expect(loadStudioTaskDetail).toHaveBeenCalledTimes(2));
    expect(loadStudioTaskDetail).toHaveBeenLastCalledWith("oral_task", "deep");
    expect(value.updateData).toHaveBeenCalledOnce();
  });

  it("A到B再到A及账号切换时忽略所有旧详情响应", async () => {
    const oldA = deferred<StudioTask>();
    const oldB = deferred<StudioTask>();
    const currentA = deferred<StudioTask>();
    loadStudioTaskDetail
      .mockReturnValueOnce(oldA.promise)
      .mockReturnValueOnce(oldB.promise)
      .mockReturnValueOnce(currentA.promise);
    const updateData = vi.fn();
    const context = (
      id: string,
      account: string,
    ): Partial<StudioContextValue> => ({
      state: {
        ...createState("task-detail"),
        selectedTaskId: id,
        selectedTaskKind: "generation_batch",
        selectedTaskBackendId: id,
      },
      data: data([]),
      user: { id: account } as StudioContextValue["user"],
      updateData,
    });
    useStudio.mockReturnValue(studio("a", context("a", "account-a")));
    const view = render(<TaskDetailPage />);
    useStudio.mockReturnValue(studio("b", context("b", "account-a")));
    view.rerender(<TaskDetailPage />);
    useStudio.mockReturnValue(studio("a", context("a", "account-b")));
    view.rerender(<TaskDetailPage />);

    await act(async () => {
      oldA.resolve({ ...taskA, id: "a" });
      oldB.resolve({ ...taskB, id: "b" });
      await Promise.resolve();
    });
    expect(updateData).not.toHaveBeenCalled();
    await act(async () => {
      currentA.resolve({ ...taskA, id: "a" });
      await Promise.resolve();
    });
    expect(updateData).toHaveBeenCalledOnce();
  });

  it("同ID换账号时首屏隐藏旧详情且旧预览不得写入新账号", async () => {
    const preview = deferred<StudioAsset | undefined>();
    const detailB = deferred<StudioTask>();
    const updateData = vi.fn();
    const layoutSnapshots: string[] = [];
    let value = studio(taskA.id, {
      state: {
        ...createState("task-detail"),
        selectedTaskId: taskA.id,
        selectedTaskKind: "generation_batch",
        selectedTaskBackendId: taskA.id,
      },
      data: data([
        { ...taskA, backendKind: "generation_batch", backendId: taskA.id },
      ]),
      user: { id: "account-a" } as StudioContextValue["user"],
      updateData,
    });
    loadStudioTaskDetail
      .mockResolvedValueOnce({
        ...taskA,
        backendKind: "generation_batch",
        backendId: taskA.id,
      })
      .mockReturnValueOnce(detailB.promise);
    loadTaskPreview.mockReturnValue(preview.promise);
    useStudio.mockImplementation(() => value);
    function Probe({ account }: { account: string }) {
      useLayoutEffect(() => {
        layoutSnapshots.push(`${account}:${document.body.textContent ?? ""}`);
      }, [account]);
      return <TaskDetailPage />;
    }
    const view = render(<Probe account="account-a" />);
    expect(
      await screen.findByRole("heading", { name: taskA.title }),
    ).toBeInTheDocument();

    value = {
      ...value,
      user: { id: "account-b" } as StudioContextValue["user"],
    };
    view.rerender(<Probe account="account-b" />);
    expect(layoutSnapshots.at(-1)).not.toContain(taskA.title);
    expect(screen.getByText("正在读取任务详情")).toBeInTheDocument();
    await act(async () => {
      preview.resolve({
        id: "old-account-preview",
        name: "旧账号预览",
        kind: "video",
        url: "/old-account-preview",
        group: "任务结果",
        source: "任务中心",
        saved: true,
      });
      await Promise.resolve();
    });
    expect(updateData).toHaveBeenCalledTimes(1);
  });

  it("真实口播任务调整脚本直接带回实际正文，不落入旧任务面板", () => {
    const task = {
      ...taskA,
      backendKind: "oral_task" as const,
      backendId: "oral-1",
      driverMode: "text" as const,
      scriptText: "实际用于生成的口播稿",
      ipId: "person-a",
      avatarId: "avatar-a",
      voiceId: "voice-a",
    };
    const value = studio(task.id, { data: data([task]) });
    useStudio.mockReturnValue(value);
    render(<TaskDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "调整脚本" }));

    expect(value.patchState).toHaveBeenCalledWith({
      draft: expect.objectContaining({
        ipId: "person-a",
        avatarId: "avatar-a",
        voiceId: "voice-a",
        script: expect.objectContaining({
          text: task.scriptText,
          confirmed: false,
        }),
      }),
    });
    expect(value.navigate).toHaveBeenCalledWith("copy", {
      returnTo: "task-detail",
    });
    expect(value.openLive).not.toHaveBeenCalled();
  });

  it("真实音频口播再创作回到音频模式，未提供正文时禁用调整脚本", () => {
    const task = {
      ...taskA,
      backendKind: "oral_task" as const,
      backendId: "oral-audio-1",
      driverMode: "audio" as const,
      audioId: "audio-original",
      ipId: "person-a",
      avatarId: "avatar-a",
    };
    const value = studio(task.id, { data: data([task]) });
    useStudio.mockReturnValue(value);
    render(<TaskDetailPage />);

    expect(screen.getByRole("button", { name: "调整脚本" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "新建草稿" }));

    expect(value.navigate).toHaveBeenCalledWith("oral-audio", {
      returnTo: "task-detail",
    });
    expect(value.patchState).toHaveBeenCalledWith({
      draft: expect.objectContaining({ audioId: "audio-original" }),
    });
  });

  it("打开已完成任务立即加载成片，并只回填发起任务的结果", async () => {
    const pending = deferred<StudioAsset | undefined>();
    const value = studio(taskA.id, { data: data([taskA, taskB]) });
    useStudio.mockReturnValue(value);
    loadTaskPreview.mockReturnValue(pending.promise);
    render(<TaskDetailPage />);

    expect(loadTaskPreview).toHaveBeenCalledWith(taskA);
    expect(
      screen.getByRole("button", { name: "正在加载预览…" }),
    ).toBeDisabled();

    const asset: StudioAsset = {
      id: "result-a",
      name: "张工建房预算 · 首个可用结果",
      kind: "video",
      url: "/signed/result-a",
      group: "任务结果",
      source: "任务中心",
      saved: true,
    };
    pending.resolve(asset);

    await waitFor(() => expect(value.updateData).toHaveBeenCalledOnce());
    const update = vi.mocked(value.updateData).mock.calls[0][0];
    const next = update(value.data);
    expect(next.assets).toContainEqual(asset);
    expect(next.tasks.find((item) => item.id === taskA.id)?.resultId).toBe(
      asset.id,
    );
    expect(next.tasks.find((item) => item.id === taskB.id)?.resultId).toBe(
      undefined,
    );
  });

  it.each([false, true])(
    "任务轮询不清除直出预览，归档后才可发布（响应丢失=%s）",
    async (responseLost) => {
      const asset: StudioAsset = {
        id: "direct-task-result-a",
        name: "直出成片",
        kind: "video",
        url: "/signed/direct-a",
        group: "任务结果",
        source: "任务中心",
        saved: false,
        generationTaskId: "provider-result-a",
      };
      let value = studio();
      useStudio.mockImplementation(() => value);
      loadTaskPreview.mockResolvedValue(asset);
      const view = render(<TaskDetailPage />);
      await waitFor(() => expect(value.updateData).toHaveBeenCalledOnce());
      const update = vi.mocked(value.updateData).mock.calls[0][0];
      value = { ...value, data: update(value.data) };
      view.rerender(<TaskDetailPage />);
      const video = view.container.querySelector("video");
      expect(video).not.toBeNull();
      value = { ...value, data: { ...value.data, tasks: [{ ...taskA }] } };
      view.rerender(<TaskDetailPage />);
      expect(view.container.querySelector("video")).toBe(video);
      expect(screen.getByRole("button", { name: "查看素材" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "去发布管理" })).toBeDisabled();
      saveTaskPreview.mockResolvedValue({
        ...asset,
        id: "saved-result",
        saved: true,
      });
      if (responseLost) {
        saveTaskPreview.mockRejectedValueOnce(new Error("保存结果暂未确认"));
        loadTaskPreview.mockResolvedValueOnce({
          ...asset,
          id: "saved-result",
          saved: true,
        });
      }
      fireEvent.click(screen.getByRole("button", { name: "保存到素材库" }));
      expect(
        await screen.findByRole("button", { name: "正在保存成片…" }),
      ).toBeDisabled();
      await waitFor(() =>
        expect(
          screen.getByRole("button", { name: "去发布管理" }),
        ).toBeEnabled(),
      );
      expect(saveTaskPreview).toHaveBeenCalledWith(asset);
      fireEvent.click(screen.getByRole("button", { name: "去发布管理" }));
      expect(value.navigate).toHaveBeenCalledWith("publishing", {
        selectedAssetId: "saved-result",
      });
    },
  );

  it("后台归档后轮询得到的新资产取代已加载的供应商直链", async () => {
    const direct: StudioAsset = {
      id: "direct-task-old-result",
      name: "旧直链",
      kind: "video",
      url: "/signed/provider-original",
      group: "任务结果",
      source: "任务中心",
      saved: false,
      delivery: "direct",
      generationTaskId: "provider-result-a",
    };
    const archived: StudioAsset = {
      id: "normalized-result-asset",
      name: "已归档规范化成片",
      kind: "video",
      url: "/signed/archived-normalized",
      group: "任务结果",
      source: "任务中心",
      saved: true,
    };
    let value = studio();
    useStudio.mockImplementation(() => value);
    loadTaskPreview.mockResolvedValue(direct);
    const view = render(<TaskDetailPage />);
    await waitFor(() =>
      expect(view.container.querySelector("video")).toHaveAttribute(
        "src",
        direct.url,
      ),
    );
    // Background archive/normalization can finish independently of this page's
    // Save button. Polling now carries the authoritative physical result ID.
    value = {
      ...value,
      data: {
        ...value.data,
        tasks: [{ ...taskA, resultId: archived.id }],
        assets: [direct, archived],
      },
    };
    view.rerender(<TaskDetailPage />);
    expect(view.container.querySelector("video")).toHaveAttribute(
      "src",
      archived.url,
    );
    expect(screen.getByRole("button", { name: "查看素材" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "去发布管理" })).toBeEnabled();
  });

  it("轮询只返回新归档ID时忽略迟到直链并允许签发新资产", async () => {
    const pending = deferred<StudioAsset | undefined>();
    let value = studio();
    useStudio.mockImplementation(() => value);
    loadTaskPreview.mockReturnValueOnce(pending.promise).mockResolvedValueOnce({
      id: "new-archive-id",
      name: "归档成片",
      kind: "video",
      url: "/signed/new-archive",
      group: "任务结果",
      source: "任务中心",
      saved: true,
    });
    const view = render(<TaskDetailPage />);
    value = {
      ...value,
      data: {
        ...value.data,
        tasks: [{ ...taskA, resultId: "new-archive-id" }],
      },
    };
    view.rerender(<TaskDetailPage />);
    await act(async () => {
      pending.resolve({
        id: "direct-task-old",
        name: "迟到直链",
        kind: "video",
        url: "/signed/stale-direct",
        group: "任务结果",
        source: "任务中心",
        saved: false,
      });
    });
    await waitFor(() =>
      expect(view.container.querySelector("video")).toHaveAttribute(
        "src",
        "/signed/new-archive",
      ),
    );
  });

  it("成片签名URL播放失败后允许重新签发预览", async () => {
    const asset: StudioAsset = {
      id: "expiring-result",
      name: "已归档成片",
      kind: "video",
      url: "/signed/expired-result",
      group: "任务结果",
      source: "任务中心",
      saved: true,
    };
    useStudio.mockReturnValue(studio());
    loadTaskPreview
      .mockResolvedValueOnce(asset)
      .mockResolvedValueOnce({ ...asset, url: "/signed/refreshed-result" });
    const view = render(<TaskDetailPage />);
    const video = await waitFor(() => {
      const element = view.container.querySelector("video");
      expect(element).toHaveAttribute("src", asset.url);
      return element as HTMLVideoElement;
    });
    fireEvent.error(video);
    fireEvent.click(screen.getByRole("button", { name: "重试预览" }));
    await waitFor(() =>
      expect(view.container.querySelector("video")).toHaveAttribute(
        "src",
        "/signed/refreshed-result",
      ),
    );
    expect(loadTaskPreview).toHaveBeenCalledTimes(2);
  });

  it("显示无结果状态，并允许重新尝试", async () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    loadTaskPreview.mockResolvedValue(undefined);
    render(<TaskDetailPage />);

    expect(
      await screen.findByText("该批次暂时没有可预览的成功结果。"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新尝试" }));
    expect(loadTaskPreview).toHaveBeenCalledTimes(2);
  });

  it("失败后显示错误并可重试", async () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    loadTaskPreview.mockRejectedValue(new Error("preview unavailable"));
    render(<TaskDetailPage />);

    expect(
      await screen.findByText("预览加载失败，请重试。"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试预览" }));
    expect(loadTaskPreview).toHaveBeenCalledTimes(2);
  });

  it("切换任务后忽略旧请求的异步结果", async () => {
    const pending = deferred<StudioAsset | undefined>();
    const updateData = vi.fn();
    let value = studio(taskA.id, {
      data: data([taskA, taskB]),
      updateData,
    });
    useStudio.mockImplementation(() => value);
    loadTaskPreview
      .mockReturnValueOnce(pending.promise)
      .mockReturnValue(new Promise(() => {}));
    const view = render(<TaskDetailPage />);

    value = studio(taskB.id, {
      data: data([taskA, taskB]),
      updateData,
    });
    view.rerender(<TaskDetailPage />);
    expect(
      screen.getByRole("heading", { name: taskB.title }),
    ).toBeInTheDocument();

    pending.resolve({
      id: "stale-result",
      name: "旧任务结果",
      kind: "video",
      url: "/signed/stale",
      group: "任务结果",
      source: "任务中心",
      saved: true,
    });
    await pending.promise;
    await Promise.resolve();
    expect(updateData).not.toHaveBeenCalled();
  });

  it("审核示例不请求真实预览接口", () => {
    useStudio.mockReturnValue(studio(taskA.id, { review: true }));
    render(<TaskDetailPage />);

    expect(
      screen.queryByRole("button", { name: "预览成片" }),
    ).not.toBeInTheDocument();
    expect(loadTaskPreview).not.toHaveBeenCalled();
  });

  it("口播成片下载直接使用结果资产，不再打开旧任务面板", async () => {
    const oralTask: StudioTask = {
      ...taskA,
      id: "oral-visible-id",
      backendKind: "oral_task",
      backendId: "oral-backend-id",
      resultId: "oral-result-asset",
      batchId: undefined,
    };
    const value = studio(oralTask.id, { data: data([oralTask]) });
    useStudio.mockReturnValue(value);
    downloadStudioTaskResult.mockResolvedValue(undefined);
    render(<TaskDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "下载成片" }));

    await waitFor(() =>
      expect(downloadStudioTaskResult).toHaveBeenCalledWith(oralTask),
    );
    expect(value.openLive).not.toHaveBeenCalled();
  });

  it("提交结果不确定时只提供状态核对入口", () => {
    const uncertain: StudioTask = {
      ...taskA,
      id: "oral-uncertain",
      backendKind: "oral_task",
      backendId: "oral-uncertain",
      backendStatus: "SUBMISSION_UNCERTAIN",
      status: "uncertain",
      retryAction: undefined,
      resultId: undefined,
    };
    const value = studio(uncertain.id, { data: data([uncertain]) });
    useStudio.mockReturnValue(value);
    render(<TaskDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "核对任务状态" }));

    expect(value.openLive).toHaveBeenCalledWith("tasks");
    expect(retryStudioTask).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("button", { name: "重试提交" }),
    ).not.toBeInTheDocument();
  });
});

describe("V1.4 工作台正在进行行", () => {
  const clock = new Date("2026-09-06T10:00:00");

  beforeEach(() => {
    useStudio.mockReset();
    // Fake only the clock: waitFor relies on real timers.
    vi.useFakeTimers({ now: clock, toFake: ["Date"] });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function workbench(
    overrides: Partial<StudioContextValue> = {},
  ): StudioContextValue {
    return studio(undefined, {
      state: { ...createState("workbench") },
      data: data([runningTask]),
      ...overrides,
    });
  }

  it("爆款来源未上传时在工作台展示原视频上传引导", () => {
    const state = createState("workbench");
    state.draft.sourceId = "viral-source";
    const videos: StudioVideo[] = [
      {
        id: "viral-source",
        title: "建房预算参考",
        author: "作者",
        platform: "抖音",
        category: "建房预算",
        poster: "/studio/source-preview.jpg",
        duration: "00:30",
        likes: 0,
        collections: null,
        shares: null,
        description: "参考标题",
      },
    ];
    useStudio.mockReturnValue(workbench({ state, data: data([], videos) }));
    render(<WorkbenchPage />);

    expect(screen.getByText(/已选参考：建房预算参考/)).toBeInTheDocument();
    expect(
      screen.getByText(/请上传该视频的 MP4 或 MOV 文件/),
    ).toBeInTheDocument();
  });

  it("每行提供更多操作菜单：打开任务中心与复制任务编号", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    render(<WorkbenchPage />);

    const trigger = screen.getByRole("button", {
      name: "更多操作：张工 · 建房预算",
    });
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(screen.getByRole("menuitem", { name: "打开任务中心" }));
    expect(value.navigate).toHaveBeenCalledWith("tasks");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();

    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("menuitem", { name: "复制任务编号" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("batch-9"));
    expect(value.notify).toHaveBeenCalledWith("任务编号已复制");
  });

  it("复制失败时给出可感知的失败提示", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    render(<WorkbenchPage />);

    fireEvent.click(
      screen.getByRole("button", { name: "更多操作：张工 · 建房预算" }),
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "复制任务编号" }));
    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith(
        "复制失败，请手动复制任务编号。",
      ),
    );
    expect(writeText).toHaveBeenCalledWith("batch-9");
  });

  it("任务动态与正在进行行不显示原始 ISO 时间", () => {
    useStudio.mockReturnValue(workbench());
    render(<WorkbenchPage />);

    expect(screen.queryByText("2026-09-06T09:30:00")).not.toBeInTheDocument();
    expect(screen.getByText("今天 09:30")).toBeInTheDocument();
    expect(screen.getByText("68%")).toBeInTheDocument();
  });
});

describe("V1.4 工作台新版首页布局", () => {
  const videos: StudioVideo[] = Array.from({ length: 7 }, (_, index) => ({
    homepageFeatured: true,
    id: `video-${index + 1}`,
    title: `灵感视频 ${index + 1}`,
    author: `作者 ${index + 1}`,
    platform: index % 2 === 0 ? "抖音" : "视频号",
    category: "乡墅",
    poster: `/poster-${index + 1}.jpg`,
    duration: "00:56",
    likes: 128000 - index * 1000,
    collections: 100,
    shares: 20,
    description: "乡墅爆款案例",
  }));

  beforeEach(() => {
    window.sessionStorage.clear();
    createViralImportTask.mockReset();
    getViralImportTask.mockReset();
  });

  it("浏览普通周榜或解析链接后，回到首页仍只展示人工精选", () => {
    const value = studio(undefined, {
      state: createState("workbench"),
      data: data([], [videos[0]]),
    });
    useStudio.mockReturnValue(value);
    const view = render(<WorkbenchPage />);
    expect(screen.getByText("灵感视频 1")).toBeInTheDocument();
    value.data = {
      ...value.data,
      videos: [
        {
          ...videos[1],
          homepageFeatured: false,
          title: "周榜高点赞未精选",
          likes: 99999999,
        },
        {
          ...videos[2],
          homepageFeatured: undefined,
          title: "主动解析链接未精选",
          likes: 99999998,
        },
      ],
    };
    view.rerender(<WorkbenchPage />);
    expect(screen.getByText("灵感视频 1")).toBeInTheDocument();
    expect(screen.queryByText("周榜高点赞未精选")).not.toBeInTheDocument();
    expect(screen.queryByText("主动解析链接未精选")).not.toBeInTheDocument();
  });

  it("展示新版主标题、居中辅助文案、五个竖屏爆款与四个快捷入口", () => {
    const value = studio(undefined, {
      state: createState("workbench"),
      data: data([runningTask], videos),
    });
    useStudio.mockReturnValue(value);

    render(<WorkbenchPage />);

    expect(
      screen.getByRole("heading", {
        name: "粘贴一条爆款乡墅视频链接，快速生成它的原创视频",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText(/支持抖音、小红书的视频链接/)).toHaveClass(
      "studio-start-helper",
    );
    expect(
      screen.getByRole("heading", { name: "爆款视频精选" }),
    ).toBeInTheDocument();
    expect(screen.getByText("灵感视频 5")).toBeInTheDocument();
    expect(screen.queryByText("灵感视频 6")).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /提取文案：/ })).toHaveLength(
      5,
    );
    expect(screen.getAllByRole("button", { name: /查看详情：/ })).toHaveLength(
      5,
    );
    expect(
      screen.queryByRole("button", { name: /用它复刻/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "查看全部爆款" }),
    ).toBeInTheDocument();
    for (const name of ["文案工坊", "人物库", "素材库", "数据看板"]) {
      expect(
        screen.getByRole("button", { name: `快捷入口：${name}` }),
      ).toBeInTheDocument();
    }
  });

  it("审核首页从爆款卡片提取文案时复用既有草稿与导航流程", () => {
    const value = studio(undefined, {
      review: true,
      state: createState("workbench"),
      data: data([], videos),
    });
    useStudio.mockReturnValue(value);

    render(<WorkbenchPage />);
    fireEvent.click(
      screen.getByRole("button", { name: "提取文案：灵感视频 1" }),
    );

    expect(value.patchDraft).toHaveBeenCalledWith({ sourceId: "video-1" });
    expect(value.navigate).toHaveBeenCalledWith("copy", {
      selectedVideoId: "video-1",
      returnTo: "workbench",
    });
  });

  it("首页精选按热度和稳定 ID 确定排序，不依赖接口数组顺序", () => {
    const value = studio(undefined, {
      state: createState("workbench"),
      data: data([], [...videos].reverse()),
    });
    useStudio.mockReturnValue(value);

    render(<WorkbenchPage />);

    expect(screen.getByText("灵感视频 1")).toBeInTheDocument();
    expect(screen.getByText("灵感视频 5")).toBeInTheDocument();
    expect(screen.queryByText("灵感视频 6")).not.toBeInTheDocument();
  });

  it("首页精选先按跨平台置顶顺序展示，未置顶再按热度排序", () => {
    const rankedVideos: StudioVideo[] = [
      { ...videos[0], id: "douyin-hot", title: "抖音高热", likes: 999_999 },
      {
        ...videos[1],
        id: "wechat-pinned-second",
        title: "视频号置顶第二",
        likes: 1,
        homepageRank: 2,
      },
      {
        ...videos[2],
        id: "douyin-pinned-first",
        title: "抖音置顶第一",
        likes: 0,
        homepageRank: 1,
      },
      ...videos.slice(3),
    ];
    const value = studio(undefined, {
      state: createState("workbench"),
      data: data([], rankedVideos),
    });
    useStudio.mockReturnValue(value);

    const { container } = render(<WorkbenchPage />);
    const titles = Array.from(
      container.querySelectorAll(".studio-home-viral-card h3"),
      (heading) => heading.textContent,
    );

    expect(titles.slice(0, 3)).toEqual([
      "抖音置顶第一",
      "视频号置顶第二",
      "抖音高热",
    ]);
  });

  it("真实爆款从首页提取文案时先导入项目素材", async () => {
    createViralImportTask.mockResolvedValue({
      taskId: "import-home",
      status: "SUCCEEDED",
      projectId: "project-home",
      sourceAssetId: "asset-home",
      canTranscribe: true,
    });
    const source = {
      ...videos[0],
      platformKey: "douyin" as const,
      nativeId: "native-home",
    };
    const value = studio(undefined, {
      state: createState("workbench"),
      data: data([], [source]),
    });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);

    fireEvent.click(
      screen.getByRole("button", { name: "提取文案：灵感视频 1" }),
    );

    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "project-home",
        sourceId: "asset-home",
        sourceAssetId: "asset-home",
      }),
    );
    expect(createViralImportTask).toHaveBeenCalledWith(
      "douyin",
      "native-home",
      "copy",
      expect.any(String),
    );
    expect(value.extractScriptFromUpload).toHaveBeenCalledWith(
      "project-home",
      "asset-home",
      undefined,
      { platformKey: "douyin", nativeId: "native-home" },
    );
  });

  it("首页爆款永久失败后使用新幂等键重试", async () => {
    createViralImportTask
      .mockResolvedValueOnce({
        id: "home-expired",
        status: "FAILED",
        retryable: false,
        errorMessage: "首页来源已失效",
      })
      .mockResolvedValueOnce({
        id: "home-replacement",
        status: "SUCCEEDED",
        projectId: "home-project-new",
        sourceAssetId: "home-asset-new",
        canTranscribe: true,
      });
    const source = {
      ...videos[0],
      platformKey: "douyin" as const,
      nativeId: "native-home-expired",
    };
    const value = studio(undefined, {
      user: {
        id: "customer-home",
        username: "customer-home",
        display_name: "首页客户",
        role: "customer",
      },
      state: createState("workbench"),
      data: data([], [source]),
    });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);

    fireEvent.click(
      screen.getByRole("button", { name: "提取文案：灵感视频 1" }),
    );
    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith("首页来源已失效"),
    );
    const failedKey = createViralImportTask.mock.calls[0][3];
    fireEvent.click(
      screen.getByRole("button", { name: "提取文案：灵感视频 1" }),
    );
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));

    expect(createViralImportTask.mock.calls[1][3]).not.toBe(failedKey);
  });

  it("首页爆款畸形成功后不写草稿且使用新幂等键重试", async () => {
    createViralImportTask.mockResolvedValue({
      id: "home-malformed",
      status: "SUCCEEDED",
      projectId: "home-project",
      sourceAssetId: "home-audio",
      canTranscribe: false,
    });
    const source = {
      ...videos[0],
      platformKey: "douyin" as const,
      nativeId: "native-home-malformed",
    };
    const value = studio(undefined, {
      user: {
        id: "customer-home-malformed",
        username: "customer-home-malformed",
        display_name: "首页客户",
        role: "customer",
      },
      state: createState("workbench"),
      data: data([], [source]),
    });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);

    fireEvent.click(
      screen.getByRole("button", { name: "提取文案：灵感视频 1" }),
    );
    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith("该来源暂不支持提取文案"),
    );
    const malformedKey = createViralImportTask.mock.calls[0][3];
    fireEvent.click(
      screen.getByRole("button", { name: "提取文案：灵感视频 1" }),
    );
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));

    expect(createViralImportTask.mock.calls[1][3]).not.toBe(malformedKey);
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("正式首页爆款缺少平台原生 ID 时不创建伪文案项目", () => {
    const source = {
      ...videos[0],
      platformKey: undefined,
      nativeId: undefined,
    };
    const value = studio(undefined, {
      review: false,
      state: createState("workbench"),
      data: data([], [source]),
    });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);

    fireEvent.click(
      screen.getByRole("button", { name: "提取文案：灵感视频 1" }),
    );

    expect(value.notify).toHaveBeenCalledWith("该视频缺少可导入的平台标识");
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
  });
});

describe("V1.4 工作台对齐网格", () => {
  const clock = new Date("2026-09-06T10:00:00");

  beforeEach(() => {
    useStudio.mockReset();
    vi.useFakeTimers({ now: clock, toFake: ["Date"] });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function workbench(
    overrides: Partial<StudioContextValue> = {},
  ): StudioContextValue {
    return studio(undefined, {
      state: { ...createState("workbench") },
      data: data([runningTask]),
      ...overrides,
    });
  }

  it("四区块锁定为两行等高网格，标题收进卡片内部", () => {
    useStudio.mockReturnValue(workbench());
    const { container } = render(<WorkbenchPage />);

    const grid = container.querySelector<HTMLElement>(".studio-home-grid");
    if (!grid) throw new Error("未找到 .studio-home-grid 网格容器");
    const areas = Array.from(grid.children).map(
      (cell) => (cell as HTMLElement).dataset.area,
    );
    expect(areas).toEqual(["running", "activity", "viral", "shortcuts"]);

    // 标题必须位于卡片内部（此前左栏 h2 悬在卡片外，行首高度受外部标题影响）
    const runningCell = grid.children[0] as HTMLElement;
    expect(runningCell.querySelector("h2")?.textContent).toBe("正在进行");
    const viralCell = grid.children[2] as HTMLElement;
    expect(viralCell.querySelector("h2")?.textContent).toBe("爆款视频精选");
  });

  it("任务动态最多展示 4 条，超出时提供进入任务中心出口", () => {
    const extraA = { ...taskA, id: "t-extra-1", title: "王宅庭院巡检" };
    const extraB = { ...taskA, id: "t-extra-2", title: "赵宅封顶记录" };
    const extraC = { ...taskA, id: "t-extra-3", title: "钱宅交付回访" };
    useStudio.mockReturnValue(
      workbench({
        data: data([runningTask, taskA, taskB, extraA, extraB, extraC]),
      }),
    );
    render(<WorkbenchPage />);

    expect(screen.getByText(/王宅庭院巡检/)).toBeInTheDocument();
    expect(screen.queryByText(/赵宅封顶记录/)).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "进入任务中心" }),
    ).toBeInTheDocument();
  });

  it("正在进行为空时保留空态卡片并给出上传入口", () => {
    const clickSpy = vi
      .spyOn(HTMLInputElement.prototype, "click")
      .mockImplementation(() => {});
    useStudio.mockReturnValue(workbench({ data: data([]) }));
    render(<WorkbenchPage />);

    const cta = screen.getByRole("button", {
      name: "上传视频，开始第一支创作",
    });
    fireEvent.click(cta);
    expect(clickSpy).toHaveBeenCalled();
    clickSpy.mockRestore();
  });

  it("任务动态为空时渲染带说明的空态", () => {
    useStudio.mockReturnValue(workbench({ data: data([]) }));
    render(<WorkbenchPage />);

    expect(screen.getByText("暂无任务动态")).toBeInTheDocument();
    expect(screen.getByText(/任务提交后这里会实时更新/)).toBeInTheDocument();
  });
});

describe("formatTaskTime", () => {
  it("PostgreSQL 微秒及短时区偏移与标准 ISO 时间一致", () => {
    const reference = new Date("2026-09-14T06:00:00Z");
    expect(formatTaskTime("2026-09-14 05:17:11.591133+00", reference)).toBe(
      formatTaskTime("2026-09-14T05:17:11.591Z", reference),
    );
  });
  const now = new Date("2026-09-06T10:00:00");

  it("当天显示“今天 HH:mm”", () => {
    expect(formatTaskTime("2026-09-06T09:30:00", now)).toBe("今天 09:30");
    expect(formatTaskTime("2026-09-06 00:05", now)).toBe("今天 00:05");
  });

  it("前一天显示“昨天 HH:mm”", () => {
    expect(formatTaskTime("2026-09-05T23:58:00", now)).toBe("昨天 23:58");
  });

  it("同年更早显示“MM-DD HH:mm”，跨年带年份", () => {
    expect(formatTaskTime("2026-09-03T14:00:00", now)).toBe("09-03 14:00");
    expect(formatTaskTime("2025-12-31T08:05:00", now)).toBe("2025-12-31 08:05");
  });

  it("无法解析或已是友好文案时原样返回", () => {
    expect(formatTaskTime("今天 09:30", now)).toBe("今天 09:30");
    expect(formatTaskTime("", now)).toBe("");
  });
});

describe("V1.4 工作台上传与创作入口", () => {
  beforeEach(() => {
    useStudio.mockReset();
    uploadWorkbenchSourceVideo.mockReset();
    resolveViralLink.mockReset();
    createViralImportTask.mockReset();
    getViralImportTask.mockReset();
  });

  function workbench(
    overrides: Partial<StudioContextValue> = {},
  ): StudioContextValue {
    return studio(undefined, {
      state: { ...createState("workbench") },
      data: data([]),
      ...overrides,
    });
  }

  function changeFile(name: string) {
    const input = screen.getByLabelText("选择视频文件");
    fireEvent.change(input, {
      target: { files: [new File(["video"], name, { type: "video/mp4" })] },
    });
  }

  it("审核模式点击上传图标只提示，不进入真实上传", () => {
    const value = workbench({ review: true });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);

    fireEvent.click(screen.getByRole("button", { name: "上传视频" }));
    expect(value.notify).toHaveBeenCalledWith("审核示例不执行真实上传。");
    expect(uploadWorkbenchSourceVideo).not.toHaveBeenCalled();
  });

  it("非 MP4/MOV 文件被拒收并提示", () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);

    changeFile("相册导出.avi");
    expect(value.notify).toHaveBeenCalledWith(
      "目前仅支持 MP4 / MOV 视频文件。",
    );
    expect(uploadWorkbenchSourceVideo).not.toHaveBeenCalled();
  });

  it("上传成功：同步项目、来源资产与当前草稿", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    const pending: { resolve?: (value: WorkbenchUploadResult) => void } = {};
    let reportProgress: ((percent: number) => void) | undefined;
    uploadWorkbenchSourceVideo.mockImplementation(
      (_file: File, onProgress: (percent: number) => void) =>
        new Promise<{ projectId: string; assetId: string }>((resolve) => {
          pending.resolve = resolve;
          reportProgress = onProgress;
          onProgress(40);
        }),
    );
    render(<WorkbenchPage />);

    changeFile("乡墅案例.mp4");
    expect(uploadWorkbenchSourceVideo).toHaveBeenCalledOnce();
    expect(screen.getByText("正在上传 乡墅案例.mp4… 40%")).toBeInTheDocument();
    act(() => reportProgress?.(100));
    expect(screen.getByText("正在上传 乡墅案例.mp4… 100%")).toBeInTheDocument();
    expect(screen.queryByText("已上传：乡墅案例.mp4")).toBeNull();

    pending.resolve?.({
      projectId: "proj-1",
      assetId: "asset-1",
      project: {
        id: "proj-1",
        owner_user_id: "user-1",
        name: "乡墅案例",
        status: "DRAFT",
        reference_asset_id: "asset-1",
        reference_upload_status: "READY",
        analysis_status: "NOT_READY",
      },
      asset: {
        id: "asset-1",
        name: "乡墅案例 · 来源视频",
        kind: "video",
        group: "乡墅案例",
        source: "项目上传",
        saved: true,
      },
    });
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "proj-1",
        sourceId: "asset-1",
        sourceAssetId: "asset-1",
      }),
    );
    expect(value.updateData).toHaveBeenCalledOnce();
    const update = vi.mocked(value.updateData).mock.calls[0][0];
    expect(update(value.data)).toMatchObject({
      projects: [
        expect.objectContaining({
          id: "proj-1",
          reference_asset_id: "asset-1",
          reference_upload_status: "READY",
        }),
      ],
      assets: [
        expect.objectContaining({
          id: "asset-1",
          kind: "video",
          saved: true,
        }),
      ],
    });
    expect(screen.getByText("已上传：乡墅案例.mp4")).toBeInTheDocument();
    expect(value.notify).toHaveBeenCalledWith(
      "视频已上传，来源已加入当前创作。",
    );
  });

  it("A 项目编辑后从工作台上传 B，进入复刻前清除 A 的文本归属", async () => {
    const state = createState("workbench");
    state.draft = {
      ...state.draft,
      projectId: "project-a",
      prompt: "A Prompt",
      promptEdited: true,
      script: {
        ...state.draft.script,
        title: "A 标题",
        original: "A 原文",
        text: "A 文案",
      },
      scriptEdited: true,
    };
    const value = workbench({ state });
    uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "project-b",
      assetId: "asset-b",
    });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);

    changeFile("来源B.mp4");
    await waitFor(() => expect(value.patchDraft).toHaveBeenCalledOnce());
    const patch = vi.mocked(value.patchDraft).mock.calls[0][0];
    const next = patchStudioDraft(state.draft, patch);

    expect(next.projectId).toBe("project-b");
    expect(next.prompt).toBe("");
    expect(next.promptEdited).toBe(false);
    expect(next.script).toMatchObject({ title: "", original: "", text: "" });
    expect(next.scriptEdited).toBe(false);
  });

  it("忽略被后一次上传取代的迟到结果与错误", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    const first = deferred<WorkbenchUploadResult>();
    const second = deferred<WorkbenchUploadResult>();
    uploadWorkbenchSourceVideo
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    render(<WorkbenchPage />);

    changeFile("来源A.mp4");
    changeFile("来源B.mp4");
    expect(uploadWorkbenchSourceVideo.mock.calls[0]?.[2]?.aborted).toBe(true);

    second.resolve({ projectId: "project-b", assetId: "asset-b" });
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "project-b",
        sourceId: "asset-b",
        sourceAssetId: "asset-b",
      }),
    );

    first.reject(new Error("迟到失败"));
    await first.promise.catch(() => undefined);
    await Promise.resolve();
    expect(value.patchDraft).toHaveBeenCalledTimes(1);
    expect(screen.getByText("已上传：来源B.mp4")).toBeInTheDocument();
    expect(screen.queryByText(/迟到失败/)).toBeNull();
  });

  it("上传完成后开始复刻直接进分镜工作区，不再打开项目面板", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "proj-1",
      assetId: "asset-1",
    });
    render(<WorkbenchPage />);

    changeFile("乡墅案例.mp4");
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "proj-1",
        sourceId: "asset-1",
        sourceAssetId: "asset-1",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
    expect(value.navigate).toHaveBeenCalledWith("replica");
    expect(value.openLive).not.toHaveBeenCalled();
  });

  it("上传后的提取文案走 script-from-audio 管线，不再打开旧项目面板", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    uploadWorkbenchSourceVideo.mockResolvedValue({
      projectId: "proj-1",
      assetId: "asset-1",
    });
    render(<WorkbenchPage />);

    changeFile("乡墅案例.mp4");
    await waitFor(() =>
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "proj-1",
        sourceId: "asset-1",
        sourceAssetId: "asset-1",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    expect(value.extractScriptFromUpload).toHaveBeenCalledTimes(1);
    expect(value.openLive).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("已恢复草稿含来源时，提取文案同样走管线", () => {
    const state = createState("workbench");
    state.draft.sourceId = "proj-9";
    state.draft.projectId = "proj-9";
    const value = workbench({ state });
    useStudio.mockReturnValue(value);
    render(<WorkbenchPage />);

    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    expect(value.extractScriptFromUpload).toHaveBeenCalledTimes(1);
    expect(value.openLive).not.toHaveBeenCalled();
  });

  it("抖音链接解析后复用爆款导入状态流进入复刻且不重复提交", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    const resolution = deferred<import("../api").ViralLinkResolution>();
    resolveViralLink.mockReturnValue(resolution.promise);
    createViralImportTask.mockResolvedValue({
      id: "link-import-1",
      status: "SUCCEEDED",
      projectId: "link-project-1",
      sourceAssetId: "link-asset-1",
      canAnalyze: true,
    });
    render(<WorkbenchPage />);

    fireEvent.change(screen.getByLabelText("视频链接"), {
      target: {
        value: "3.28 复制打开抖音 https://v.douyin.com/shareCode/",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
    expect(
      screen.getByRole("button", { name: "正在解析链接…" }),
    ).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "正在解析链接…" }));
    expect(resolveViralLink).toHaveBeenCalledWith(
      "3.28 复制打开抖音 https://v.douyin.com/shareCode/",
      "replica",
      expect.any(String),
    );

    resolution.resolve({
      importIdempotencyKey: "stable-link-import-key",
      item: {
        platform: "douyin",
        videoId: "native-link-1",
        category: "链接导入",
        title: "链接乡墅案例",
        author: "作者",
        authorAvatar: null,
        verified: false,
        coverUrl: null,
        durationMs: 30_000,
        likes: 0,
        comments: null,
        shares: null,
        collects: null,
        publishedAt: null,
        publishedDisplay: null,
        likeDisplay: null,
        tags: [],
        hasPlayableAudio: true,
        playUrl: "https://cdn.example/video.mp4",
      },
    });

    await waitFor(() =>
      expect(createViralImportTask).toHaveBeenCalledWith(
        "douyin",
        "native-link-1",
        "replica",
        "stable-link-import-key",
      ),
    );
    expect(value.patchDraft).toHaveBeenCalledWith({
      projectId: "link-project-1",
      sourceId: "link-asset-1",
      sourceAssetId: "link-asset-1",
    });
    expect(value.navigate).toHaveBeenCalledWith("replica", {
      selectedVideoId: "douyin-native-link-1",
      returnTo: "workbench",
    });
    expect(value.refresh).toHaveBeenCalledTimes(1);
  });

  it("链接输入切换后忽略上一请求的迟到响应", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    const resolution = deferred<import("../api").ViralLinkResolution>();
    resolveViralLink.mockReturnValue(resolution.promise);
    render(<WorkbenchPage />);

    const input = screen.getByLabelText("视频链接");
    fireEvent.change(input, {
      target: { value: "https://v.douyin.com/old-link/" },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
    fireEvent.change(input, {
      target: { value: "https://v.douyin.com/new-link/" },
    });

    resolution.resolve({
      importIdempotencyKey: "stale-import-key",
      item: {
        platform: "douyin",
        videoId: "stale-link",
        category: "链接导入",
        title: "迟到响应",
        author: "作者",
        authorAvatar: null,
        verified: false,
        coverUrl: null,
        durationMs: 30_000,
        likes: 0,
        comments: null,
        shares: null,
        collects: null,
        publishedAt: null,
        publishedDisplay: null,
        likeDisplay: null,
        tags: [],
        hasPlayableAudio: true,
        playUrl: "https://cdn.example/stale.mp4",
      },
    });
    await act(async () => Promise.resolve());

    await waitFor(() => expect(resolveViralLink).toHaveBeenCalledOnce());
    expect(createViralImportTask).not.toHaveBeenCalled();
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("账号在 effect 前切换时阻断旧链接响应和后续导入", async () => {
    const accountA = workbench({
      user: { id: "customer-a" } as StudioContextValue["user"],
    });
    const accountB = workbench({
      user: { id: "customer-b" } as StudioContextValue["user"],
    });
    let current = accountA;
    useStudio.mockImplementation(() => current);
    const resolution = deferred<import("../api").ViralLinkResolution>();
    resolveViralLink.mockReturnValue(resolution.promise);
    const view = render(<WorkbenchPage />);

    fireEvent.change(screen.getByLabelText("视频链接"), {
      target: { value: "https://v.douyin.com/account-a/" },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
    current = accountB;
    view.rerender(<WorkbenchPage />);
    resolution.resolve({
      importIdempotencyKey: "account-a-import",
      item: {
        platform: "douyin",
        videoId: "7345678901234567890",
        category: "链接导入",
        title: "账号 A 链接",
        author: "作者",
        authorAvatar: null,
        verified: false,
        coverUrl: null,
        durationMs: 30_000,
        likes: 0,
        comments: null,
        shares: null,
        collects: null,
        publishedAt: null,
        publishedDisplay: null,
        likeDisplay: null,
        tags: [],
        hasPlayableAudio: true,
        playUrl: "https://cdn.example/account-a.mp4",
      },
    });
    await act(async () => Promise.resolve());

    await waitFor(() => expect(resolveViralLink).toHaveBeenCalledOnce());
    expect(createViralImportTask).not.toHaveBeenCalled();
    expect(accountA.updateData).not.toHaveBeenCalled();
    expect(accountB.updateData).not.toHaveBeenCalled();
    expect(accountA.notify).not.toHaveBeenCalled();
    expect(accountB.notify).not.toHaveBeenCalled();
  });

  it("切换账号后立即隐藏旧链接且不能以新账号提交", () => {
    const accountA = workbench({
      user: { id: "customer-a" } as StudioContextValue["user"],
    });
    const accountB = workbench({
      user: { id: "customer-b" } as StudioContextValue["user"],
    });
    let current = accountA;
    useStudio.mockImplementation(() => current);
    const view = render(<WorkbenchPage />);
    fireEvent.change(screen.getByLabelText("视频链接"), {
      target: { value: "https://v.douyin.com/account-a/" },
    });

    current = accountB;
    view.rerender(<WorkbenchPage />);
    expect(screen.getByLabelText("视频链接")).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));

    expect(resolveViralLink).not.toHaveBeenCalled();
    expect(accountB.navigate).not.toHaveBeenCalled();
    expect(accountB.extractScriptFromUpload).not.toHaveBeenCalled();
    expect(accountB.openLive).toHaveBeenCalledWith("projects");

    current = accountA;
    view.rerender(<WorkbenchPage />);
    expect(screen.getByLabelText("视频链接")).toHaveValue("");
  });

  it("切换账号后隐藏旧上传且迟到上传不会写入新账号", async () => {
    const accountA = workbench({
      user: { id: "customer-a" } as StudioContextValue["user"],
    });
    const accountB = workbench({
      user: { id: "customer-b" } as StudioContextValue["user"],
    });
    let current = accountA;
    useStudio.mockImplementation(() => current);
    const upload = deferred<WorkbenchUploadResult>();
    uploadWorkbenchSourceVideo.mockReturnValue(upload.promise);
    const view = render(<WorkbenchPage />);
    changeFile("账号A.mp4");
    expect(screen.getByText(/正在上传 账号A\.mp4/)).toBeInTheDocument();

    current = accountB;
    view.rerender(<WorkbenchPage />);
    expect(screen.queryByText(/账号A\.mp4/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    expect(accountB.extractScriptFromUpload).not.toHaveBeenCalled();
    expect(accountB.openLive).toHaveBeenCalledWith("projects");

    upload.resolve({ projectId: "project-a", assetId: "asset-a" });
    await act(async () => upload.promise);
    expect(accountA.patchDraft).not.toHaveBeenCalled();
    expect(accountB.patchDraft).not.toHaveBeenCalled();
    expect(accountA.notify).not.toHaveBeenCalled();
    expect(accountB.notify).not.toHaveBeenCalled();
  });

  it.each([
    "视频号链接暂不支持解析，请上传 MP4 或 MOV 文件。",
    "视频链接已过期，请重新复制链接或上传 MP4/MOV 文件。",
    "链接中没有可用视频，请上传 MP4 或 MOV 文件。",
  ])("链接失败显示上传回退且不创建导入任务：%s", async (message) => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    resolveViralLink.mockRejectedValue(new Error(message));
    render(<WorkbenchPage />);

    fireEvent.change(screen.getByLabelText("视频链接"), {
      target: { value: "https://channels.weixin.qq.com/example" },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(createViralImportTask).not.toHaveBeenCalled();
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("解析结果未知时保留同一幂等键供安全重放", async () => {
    useStudio.mockReturnValue(workbench());
    const error = Object.assign(new Error("视频链接解析结果未知"), {
      status: 503,
      code: "VIRAL_LINK_SUBMISSION_UNCERTAIN",
    });
    resolveViralLink.mockRejectedValue(error);
    render(<WorkbenchPage />);

    fireEvent.change(screen.getByLabelText("视频链接"), {
      target: { value: "https://v.douyin.com/retain-key/" },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
    await waitFor(() => expect(resolveViralLink).toHaveBeenCalledTimes(2));

    expect(resolveViralLink.mock.calls[1]?.[2]).toBe(
      resolveViralLink.mock.calls[0]?.[2],
    );
    expect(createViralImportTask).not.toHaveBeenCalled();
  });

  it("网络解析明确失败后由用户重试使用新请求，避免永久重放失败回执", async () => {
    useStudio.mockReturnValue(workbench());
    resolveViralLink.mockRejectedValue(
      Object.assign(new Error("请检查代理或 DNS 设置后重试"), {
        status: 503,
        code: "VIRAL_LINK_MEDIA_DNS_UNAVAILABLE",
      }),
    );
    render(<WorkbenchPage />);
    fireEvent.change(screen.getByLabelText("视频链接"), {
      target: {
        value: "https://www.douyin.com/jingxuan?modal_id=7672703482771972081",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await screen.findByRole("alert");
    expect(resolveViralLink).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(resolveViralLink).toHaveBeenCalledTimes(2));
    expect(resolveViralLink.mock.calls[1]?.[2]).not.toBe(
      resolveViralLink.mock.calls[0]?.[2],
    );
    expect(createViralImportTask).not.toHaveBeenCalled();
  });

  it("链接视频超过 15 秒上限时红标报错而非静默无反应", async () => {
    const value = workbench();
    useStudio.mockReturnValue(value);
    resolveViralLink.mockRejectedValue(
      Object.assign(
        new Error(
          "参考视频时长 81 秒，超过 15 秒上限，无法拆解；请上传 15 秒以内的视频。",
        ),
        { status: 422, code: "VIRAL_LINK_DURATION_EXCEEDED" },
      ),
    );
    render(<WorkbenchPage />);

    fireEvent.change(screen.getByLabelText("视频链接"), {
      target: {
        value: "https://www.douyin.com/jingxuan?modal_id=7672703482771972081",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/超过 15 秒上限/);
    expect(alert).toHaveClass("is-error");
    expect(createViralImportTask).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("工作台明确说明支持抖音、小红书与上传格式，不展示解析耗时", () => {
    useStudio.mockReturnValue(workbench());
    render(<WorkbenchPage />);

    expect(
      screen.getByText(
        /支持抖音、小红书的视频链接；其他平台请上传\s+MP4\/MOV\s+文件；视频复刻支持\s+4-15\s+秒、50\s+MB\s+以内的视频。/,
      ),
    ).toBeInTheDocument();
  });
});

describe("V1.4 任务中心列表", () => {
  const clock = new Date("2026-09-06T10:00:00");
  const queuedTask: StudioTask = {
    id: "t-queued",
    batchId: "b-queued",
    title: "三层新中式乡墅",
    type: "视频复刻",
    status: "queued",
    submitted: "2026-09-06T09:32:00",
  };
  const failedTask: StudioTask = {
    id: "t-failed",
    batchId: "b-failed",
    title: "张工 · 庭院讲解首帧",
    type: "人物置换",
    status: "failed",
    submitted: "2026-09-06T09:28:00",
  };
  const doneTask: StudioTask = {
    id: "t-done",
    batchId: "b-done",
    title: "张工 · 建房预算-已确认版",
    type: "数字人口播",
    status: "completed",
    submitted: "2026-09-06T09:25:00",
  };

  beforeEach(() => {
    useStudio.mockReset();
    cancelStudioTask.mockReset();
    removeStudioGenerationTask.mockReset();
    loadMoreGenerationTasks.mockReset();
    loadMoreOralTasks.mockReset();
    vi.useFakeTimers({ now: clock, toFake: ["Date"] });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function tasksPage(
    overrides: Partial<StudioContextValue> = {},
  ): StudioContextValue {
    return studio(undefined, {
      state: { ...createState("tasks") },
      data: data([runningTask, queuedTask, failedTask, doneTask]),
      ...overrides,
    });
  }

  it("状态页签展示进行中/待处理计数", () => {
    useStudio.mockReturnValue(tasksPage());
    render(<TasksPage />);

    expect(screen.getByRole("tab", { name: "进行中 2" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "待处理 1" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "已完成" })).toBeInTheDocument();
  });

  it("普通视频批次支持在任务中心重命名", async () => {
    const generationTask: StudioTask = {
      ...doneTask,
      id: "generation-name",
      backendId: "generation-name",
      backendKind: "generation_batch",
      title: "原视频名称",
      type: "视频生成",
    };
    const value = tasksPage({ data: data([generationTask]) });
    useStudio.mockReturnValue(value);
    renameStudioGenerationTask.mockResolvedValue({
      ...generationTask,
      title: "乡墅庭院成片",
    });
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "重命名" }));
    fireEvent.change(screen.getByLabelText("重命名 原视频名称"), {
      target: { value: "乡墅庭院成片" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存名称" }));

    await waitFor(() =>
      expect(renameStudioGenerationTask).toHaveBeenCalledWith(
        generationTask,
        "乡墅庭院成片",
      ),
    );
    expect(value.updateData).toHaveBeenCalled();
    expect(value.notify).toHaveBeenCalledWith("视频名称已更新。");
  });

  it("普通视频批次支持从任务列表删除，且需二次确认", async () => {
    const generationTask: StudioTask = {
      ...doneTask,
      id: "generation-remove",
      backendId: "generation-remove",
      backendKind: "generation_batch",
      title: "待移除的成片",
      type: "视频生成",
    };
    const value = tasksPage({ data: data([generationTask]) });
    useStudio.mockReturnValue(value);
    removeStudioGenerationTask.mockResolvedValue(undefined);
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    // 二次确认之前不得触达后端：这是账号级移除，但仍是用户不可自助撤销的操作。
    expect(removeStudioGenerationTask).not.toHaveBeenCalled();
    expect(
      await screen.findByText(/确定从任务列表移除「待移除的成片」吗/),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "确认移除" }));
    await waitFor(() =>
      expect(removeStudioGenerationTask).toHaveBeenCalledWith(generationTask),
    );
    expect(value.updateData).toHaveBeenCalled();
    expect(value.notify).toHaveBeenCalledWith("任务已从列表移除。");
  });

  it("确认弹窗说明不会取消任务也不退费，避免误解为撤单", async () => {
    const generationTask: StudioTask = {
      ...doneTask,
      id: "generation-copy",
      backendId: "generation-copy",
      backendKind: "generation_batch",
      title: "文案校验用成片",
      type: "视频生成",
    };
    useStudio.mockReturnValue(tasksPage({ data: data([generationTask]) }));
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    const dialog = await screen.findByText(/仅从本账号列表移除/);
    expect(dialog.textContent).toMatch(/不会取消/);
    expect(dialog.textContent).toMatch(/不影响已产生的费用/);
  });

  it("口播任务与普通批次一样提供删除入口并走确认移除", async () => {
    const oralTask: StudioTask = {
      ...doneTask,
      id: "oral-1",
      backendId: "oral-1",
      backendKind: "oral_task",
      backendStatus: "SUCCEEDED",
      title: "张工口播成片",
      type: "数字人口播",
    };
    const value = tasksPage({ data: data([oralTask]) });
    useStudio.mockReturnValue(value);
    removeStudioGenerationTask.mockResolvedValue(undefined);
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    expect(
      await screen.findByText(/确定从任务列表移除「张工口播成片」吗/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认移除" }));
    await waitFor(() =>
      expect(removeStudioGenerationTask).toHaveBeenCalledWith(oralTask),
    );
    expect(value.updateData).toHaveBeenCalled();
    expect(value.notify).toHaveBeenCalledWith("任务已从列表移除。");
  });

  it("审核模式删除只本地移除，不调用后端", async () => {
    const generationTask: StudioTask = {
      ...doneTask,
      id: "generation-review",
      backendId: "generation-review",
      backendKind: "generation_batch",
      title: "审核模式成片",
      type: "视频生成",
    };
    const value = tasksPage({
      data: data([generationTask]),
      review: true,
    });
    useStudio.mockReturnValue(value);
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认移除" }));

    await waitFor(() => expect(value.updateData).toHaveBeenCalled());
    expect(removeStudioGenerationTask).not.toHaveBeenCalled();
  });

  it("删除失败时保留任务并提示后端原因", async () => {
    const generationTask: StudioTask = {
      ...doneTask,
      id: "generation-fail",
      backendId: "generation-fail",
      backendKind: "generation_batch",
      title: "删除会失败的成片",
      type: "视频生成",
    };
    const value = tasksPage({ data: data([generationTask]) });
    useStudio.mockReturnValue(value);
    removeStudioGenerationTask.mockRejectedValue(
      new Error("任务不存在或无权限。"),
    );
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认移除" }));

    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith("任务不存在或无权限。"),
    );
    expect(value.updateData).not.toHaveBeenCalled();
  });

  it("普通批次和口播任务使用独立历史游标且按 id 去重追加", async () => {
    const value = tasksPage({
      data: {
        ...data([runningTask, { ...doneTask, backendKind: "oral_task" }]),
        pagination: {
          generationTasks: { nextCursor: "batch-next", total: 21 },
          oralTasks: { loaded: 20, total: 21 },
        },
      },
    });
    useStudio.mockReturnValue(value);
    loadMoreGenerationTasks.mockResolvedValue({
      items: [runningTask, queuedTask],
      nextCursor: null,
      total: 21,
    });
    loadMoreOralTasks.mockResolvedValue({
      items: [{ ...doneTask, id: "oral-deep", backendKind: "oral_task" }],
      loaded: 21,
      total: 21,
    });
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "加载更多普通批次" }));
    await waitFor(() =>
      expect(loadMoreGenerationTasks).toHaveBeenCalledWith("batch-next"),
    );
    const generationUpdate = vi.mocked(value.updateData).mock.calls[0]?.[0];
    const generationData = generationUpdate?.(value.data);
    expect(generationData?.tasks.map((task) => task.id)).toEqual([
      runningTask.id,
      doneTask.id,
      queuedTask.id,
    ]);

    fireEvent.click(screen.getByRole("button", { name: "加载更多口播任务" }));
    await waitFor(() => expect(loadMoreOralTasks).toHaveBeenCalledWith(20));
  });

  it("状态列带子文案：排队中→等待开始、待处理→生成失败", () => {
    useStudio.mockReturnValue(tasksPage());
    render(<TasksPage />);

    expect(screen.getByText("等待开始")).toBeInTheDocument();
    expect(screen.getByText("生成失败")).toBeInTheDocument();
  });

  it("提交时间本地化，不显示原始 ISO 串", () => {
    useStudio.mockReturnValue(tasksPage());
    render(<TasksPage />);

    expect(screen.getByText("今天 09:32")).toBeInTheDocument();
    expect(screen.queryByText("2026-09-06T09:32:00")).not.toBeInTheDocument();
  });

  it("排队行提供取消任务：成功后提示并刷新列表", async () => {
    const value = tasksPage();
    useStudio.mockReturnValue(value);
    cancelStudioTask.mockResolvedValue({ billingStatus: "PENDING" });
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "取消任务" }));
    expect(cancelStudioTask).toHaveBeenCalledWith(queuedTask);
    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith(
        "任务已取消，计费状态处理中，请稍后刷新核对。",
      ),
    );
    expect(value.refresh).toHaveBeenCalledOnce();
  });

  it("取消失败时给出可感知的错误提示", async () => {
    const value = tasksPage();
    useStudio.mockReturnValue(value);
    cancelStudioTask.mockRejectedValue(new Error("任务已在提交中"));
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "取消任务" }));
    await waitFor(() =>
      expect(value.notify).toHaveBeenCalledWith("任务已在提交中"),
    );
    expect(value.refresh).not.toHaveBeenCalled();
  });

  it("审核模式点击取消只提示，不调用接口", () => {
    const value = tasksPage({ review: true });
    useStudio.mockReturnValue(value);
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "取消任务" }));
    expect(cancelStudioTask).not.toHaveBeenCalled();
    expect(value.notify).toHaveBeenCalledWith("审核示例不执行真实取消。");
  });

  it("非排队行不出现取消入口", () => {
    useStudio.mockReturnValue(tasksPage());
    render(<TasksPage />);

    expect(screen.getAllByRole("button", { name: "取消任务" })).toHaveLength(1);
    expect(
      screen.getByRole("button", { name: "查看结果" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "查看详情" })).toHaveLength(2);
  });

  it("口播任务只有后端 QUEUED 状态可取消", () => {
    const submittingOral: StudioTask = {
      ...queuedTask,
      id: "oral-submitting",
      backendKind: "oral_task",
      backendId: "oral-submitting",
      backendStatus: "SUBMITTING",
      type: "数字人口播",
    };
    useStudio.mockReturnValue(
      tasksPage({ data: data([submittingOral, queuedTask]) }),
    );
    render(<TasksPage />);

    expect(screen.getAllByRole("button", { name: "取消任务" })).toHaveLength(1);
  });

  it("类型筛选收进单行下拉，菜单项计数与状态筛选联动", () => {
    useStudio.mockReturnValue(tasksPage());
    render(<TasksPage />);

    expect(
      screen.queryByRole("tab", { name: "视频复刻" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "类型：全部类型" }));
    expect(
      screen.getByRole("option", { name: "✓ 全部类型 4" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "数字人口播 2" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "视频复刻 1" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "人物置换 1" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "视频生成 0" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "进行中 2" }));
    expect(
      screen.getByRole("option", { name: "数字人口播 1" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "视频复刻 1" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "人物置换 0" }),
    ).toBeInTheDocument();
  });

  it("选择类型后过滤表格、高亮触发按钮并更新结果计数", () => {
    useStudio.mockReturnValue(tasksPage());
    render(<TasksPage />);

    expect(screen.getByText("共 4 条任务")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "类型：全部类型" }));
    fireEvent.click(screen.getByRole("option", { name: "数字人口播 2" }));

    const trigger = screen.getByRole("button", { name: "类型：数字人口播" });
    expect(trigger).toHaveClass("is-active");
    expect(screen.getByText("共 2 条任务")).toBeInTheDocument();
    expect(screen.getByText("张工 · 建房预算")).toBeInTheDocument();
    expect(screen.queryByText("三层新中式乡墅")).not.toBeInTheDocument();
  });

  it("筛选无结果时提供清除筛选出口，一键复位两个维度", () => {
    useStudio.mockReturnValue(tasksPage());
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("tab", { name: "待处理 1" }));
    fireEvent.click(screen.getByRole("button", { name: "类型：全部类型" }));
    fireEvent.click(screen.getByRole("option", { name: "视频复刻 0" }));

    expect(screen.getByText("当前筛选下暂无任务")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "清除筛选" }));

    expect(
      screen.getByRole("tab", { name: "全部", selected: true }),
    ).toBeInTheDocument();
    expect(screen.getByText("共 4 条任务")).toBeInTheDocument();
    expect(screen.getByText("三层新中式乡墅")).toBeInTheDocument();
  });

  it("类型下拉支持 Escape 与点选外部关闭", () => {
    useStudio.mockReturnValue(tasksPage());
    render(<TasksPage />);

    fireEvent.click(screen.getByRole("button", { name: "类型：全部类型" }));
    expect(screen.getByRole("listbox", { name: "类型" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(
      screen.queryByRole("listbox", { name: "类型" }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "类型：全部类型" }));
    fireEvent.pointerDown(screen.getByRole("heading", { name: "任务中心" }));
    expect(
      screen.queryByRole("listbox", { name: "类型" }),
    ).not.toBeInTheDocument();
  });
});

describe("V1.4 个人中心通知偏好（C10b）", () => {
  beforeEach(() => {
    useStudio.mockReset();
    getStudioNotificationPreferences.mockReset();
    updateStudioNotificationPreferences.mockReset();
  });

  it("生产模式拉取偏好并保存开关状态", async () => {
    getStudioNotificationPreferences.mockResolvedValue({ enabled: true });
    updateStudioNotificationPreferences.mockResolvedValue({ enabled: false });
    useStudio.mockReturnValue(studio());
    render(<ProfilePage />);

    const toggle = await screen.findByRole("button", { name: "通知偏好" });
    await waitFor(() => expect(toggle).toHaveTextContent("开启"));
    fireEvent.click(toggle);

    await waitFor(() =>
      expect(updateStudioNotificationPreferences).toHaveBeenCalledWith(false),
    );
    await waitFor(() => expect(toggle).toHaveTextContent("关闭"));
  });

  it("账户概览区分真实零余额、未知状态和读取失败", () => {
    getStudioNotificationPreferences.mockResolvedValue({ enabled: true });
    useStudio.mockReturnValue(
      studio(undefined, {
        user: {
          id: "customer-1",
          username: "customer-1",
          display_name: "客户一",
          role: "customer",
        },
      }),
    );
    const retryWallet = vi.fn();
    const { rerender } = render(
      <ProfilePage
        accountSummary={{
          walletStatus: "ready",
          availableCredits: 0,
          retryWallet,
          profile: null,
          profileLoadError: "",
        }}
      />,
    );
    expect(screen.getByText("0 积分")).toBeInTheDocument();

    rerender(
      <ProfilePage
        accountSummary={{
          walletStatus: "unknown",
          availableCredits: null,
          retryWallet,
          profile: null,
          profileLoadError: "",
        }}
      />,
    );
    expect(screen.getAllByText("未查询")).toHaveLength(2);

    rerender(
      <ProfilePage
        accountSummary={{
          walletStatus: "error",
          availableCredits: null,
          retryWallet,
          profile: null,
          profileLoadError: "资料读取失败",
        }}
      />,
    );
    expect(screen.getByText("读取失败")).toBeInTheDocument();
    expect(screen.getByText("资料读取失败")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试余额查询" }));
    expect(retryWallet).toHaveBeenCalledOnce();
  });

  it("资料读取失败可重试，并在资料刷新成功后移除错误", () => {
    getStudioNotificationPreferences.mockResolvedValue({ enabled: true });
    useStudio.mockReturnValue(studio());
    const retryProfile = vi.fn();
    const accountSummary = {
      walletStatus: "ready" as const,
      availableCredits: 8,
      retryWallet: vi.fn(),
      retryProfile,
      profile: null,
      profileLoadError: "账号资料加载失败，请稍后重试。",
    };
    const view = render(<ProfilePage accountSummary={accountSummary} />);

    fireEvent.click(screen.getByRole("button", { name: "重试资料查询" }));
    expect(retryProfile).toHaveBeenCalledOnce();

    view.rerender(
      <ProfilePage
        accountSummary={{ ...accountSummary, profileLoadError: "" }}
      />,
    );
    expect(screen.queryByText("账号资料加载失败，请稍后重试。")).toBeNull();
    expect(screen.queryByRole("button", { name: "重试资料查询" })).toBeNull();
  });

  it("保存失败时回退开关状态并提示", async () => {
    getStudioNotificationPreferences.mockResolvedValue({ enabled: true });
    updateStudioNotificationPreferences.mockRejectedValue(
      new Error("保存通知偏好失败"),
    );
    const value = studio();
    useStudio.mockReturnValue(value);
    render(<ProfilePage />);

    const toggle = await screen.findByRole("button", { name: "通知偏好" });
    await waitFor(() => expect(toggle).toHaveTextContent("开启"));
    fireEvent.click(toggle);

    await waitFor(() => expect(value.notify).toHaveBeenCalled());
    expect(toggle).toHaveTextContent("开启");
  });

  it("加载失败时开关置灰为 —，审核模式点击只提示不保存", async () => {
    getStudioNotificationPreferences.mockRejectedValue(new Error("网络错误"));
    useStudio.mockReturnValue(studio());
    render(<ProfilePage />);
    const toggle = await screen.findByRole("button", { name: "通知偏好" });
    await waitFor(() => expect(toggle).toHaveTextContent("—"));
    expect(toggle).toBeDisabled();

    useStudio.mockReturnValue(studio(undefined, { review: true }));
    const { unmount } = render(<ProfilePage />);
    const reviewToggle = screen
      .getAllByRole("button", { name: "通知偏好" })
      .at(-1);
    expect(reviewToggle).toBeDefined();
    if (!reviewToggle) throw new Error("审核模式缺少通知偏好开关");
    await waitFor(() => expect(reviewToggle).toHaveTextContent("开启"));
    fireEvent.click(reviewToggle);
    expect(updateStudioNotificationPreferences).not.toHaveBeenCalled();
    unmount();
  });
});

// CW-016：客户「使用记录」有两个入口——个人中心顶部「使用记录」标签，以及
// 账户概览里的「查看使用记录」按钮。两者都必须汇入同一个 live 钱包工作区
// （openLive("wallet")）；配合 LiveWorkspacePanel.test.tsx 的真实挂载锁，构成
// 「入口点击 → panel=wallet → CustomerWalletPanel」的完整链路回归保护。
describe("CW-016 两个客户钱包入口路由到 live 钱包工作区", () => {
  beforeEach(() => {
    useStudio.mockReset();
    getStudioNotificationPreferences.mockReset();
    getStudioNotificationPreferences.mockResolvedValue({ enabled: true });
  });

  it("个人中心「使用记录」标签入口调用 openLive(wallet)", () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    render(<ProfilePage />);

    fireEvent.click(screen.getByRole("tab", { name: "使用记录" }));

    expect(value.openLive).toHaveBeenCalledWith("wallet");
  });

  it("账户概览「查看使用记录」按钮入口调用 openLive(wallet)", () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    render(<ProfilePage />);

    fireEvent.click(screen.getByRole("button", { name: "查看使用记录" }));

    expect(value.openLive).toHaveBeenCalledWith("wallet");
  });
});

const nativeAccounts = vi.hoisted(() => ({
  focusLocalPublishLogin: vi.fn(),
  canUseLocalPublishAccounts: vi.fn(() => true),
  listLocalPublishAccounts: vi.fn(),
  startLocalPublishLogin: vi.fn(),
  checkLocalPublishLogin: vi.fn(),
  cancelLocalPublishLogin: vi.fn(),
  removeLocalPublishAccount: vi.fn(),
  // PUBLISH-DELIVERY-20260917: server-side copies used by the publish worker.
  listCloudPublishAccounts: vi.fn(),
  importCloudPublishAccount: vi.fn(),
  exportLocalPublishAccountState: vi.fn(),
  deleteCloudPublishAccount: vi.fn(),
}));
vi.mock("./localPublishAccounts", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  ...nativeAccounts,
}));
describe("发布账号官方扫码", () => {
  const account = {
    id: "local-1",
    platform: "xiaohongshu",
    platform_user_id: "platform-uid",
    username: "平台真实昵称",
    verified_at: 1,
  };
  function open() {
    const value = studio(undefined, { review: false });
    useStudio.mockReturnValue(value);
    const view = render(<ProfilePage />);
    fireEvent.click(screen.getByRole("tab", { name: "发布账号" }));
    return { value, view };
  }
  beforeEach(() => {
    Object.values(nativeAccounts).forEach((mock) => {
      mock.mockReset();
    });
    getStudioNotificationPreferences.mockResolvedValue({ enabled: true });
    nativeAccounts.canUseLocalPublishAccounts.mockReturnValue(true);
    nativeAccounts.listLocalPublishAccounts.mockResolvedValue([]);
    nativeAccounts.listCloudPublishAccounts.mockResolvedValue([]);
    nativeAccounts.importCloudPublishAccount.mockResolvedValue({
      ...account,
      id: "cloud-1",
      status: "connected",
      error_message: null,
      source: "desktop",
    });
    nativeAccounts.deleteCloudPublishAccount.mockResolvedValue(undefined);
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "loading",
      image: null,
      account: null,
    });
    nativeAccounts.cancelLocalPublishLogin.mockResolvedValue(undefined);
  });
  afterEach(() => {
    vi.useRealTimers();
  });
  it("自动检测暂停后打开官方窗口会恢复当前会话检测", async () => {
    vi.useFakeTimers();
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("recovery-login");
    nativeAccounts.focusLocalPublishLogin.mockResolvedValue(undefined);
    nativeAccounts.checkLocalPublishLogin.mockRejectedValue(
      new Error("暂时无法读取登录状态"),
    );
    const { value } = open();
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "抖音" }));
    await act(async () => {});

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_500);
    });
    expect(
      screen.getByText("自动检测已暂停，请重试检测或重新获取二维码。"),
    ).toBeInTheDocument();

    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "connected",
      image: null,
      account: { ...account, platform: "douyin" },
    });
    fireEvent.click(screen.getByRole("button", { name: "打开官方窗口" }));
    await act(async () => {});
    expect(nativeAccounts.focusLocalPublishLogin).toHaveBeenCalledWith(
      value.user.id,
      "recovery-login",
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(screen.getByText("抖音 · 平台真实昵称")).toBeInTheDocument();
    expect(screen.queryByLabelText("抖音扫码登录")).toBeNull();
  });
  it("取消扫码后迟到的官方窗口响应不会恢复已结束会话", async () => {
    vi.useFakeTimers();
    const focusRequest = deferred<void>();
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("cancelled-login");
    nativeAccounts.focusLocalPublishLogin.mockReturnValue(focusRequest.promise);
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "action_required",
      image: null,
      account: null,
    });
    open();
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "抖音" }));
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });

    fireEvent.click(screen.getByRole("button", { name: "打开官方窗口" }));
    fireEvent.click(screen.getByRole("button", { name: "取消扫码" }));
    await act(async () => {});
    expect(screen.queryByLabelText("抖音扫码登录")).toBeNull();

    focusRequest.resolve();
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(nativeAccounts.checkLocalPublishLogin).toHaveBeenCalledOnce();
    expect(screen.queryByLabelText("抖音扫码登录")).toBeNull();
  });
  it("旧会话的窗口响应不会重启新会话的轮询", async () => {
    vi.useFakeTimers();
    const oldFocusRequest = deferred<void>();
    nativeAccounts.startLocalPublishLogin
      .mockResolvedValueOnce("old-login")
      .mockResolvedValueOnce("new-login");
    nativeAccounts.focusLocalPublishLogin.mockReturnValue(
      oldFocusRequest.promise,
    );
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "action_required",
      image: null,
      account: null,
    });
    open();
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "抖音" }));
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });

    fireEvent.click(screen.getByRole("button", { name: "打开官方窗口" }));
    fireEvent.click(screen.getByRole("button", { name: "取消扫码" }));
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "小红书" }));
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(nativeAccounts.checkLocalPublishLogin).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText("小红书扫码登录")).toBeInTheDocument();

    oldFocusRequest.resolve();
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(nativeAccounts.checkLocalPublishLogin).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText("小红书扫码登录")).toBeInTheDocument();
  });
  it("扫码确认后把导出的登录状态加密同步到服务端，失败可重试", async () => {
    const storage = {
      cookies: [{ name: "sid", value: "s", domain: ".xiaohongshu.com" }],
      origins: [],
    };
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("login-1");
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "connected",
      image: null,
      account,
      storage_state: storage,
    });
    nativeAccounts.importCloudPublishAccount.mockRejectedValueOnce(
      new Error("服务端暂不可用"),
    );
    const { value } = open();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "小红书" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "小红书" }));
    await screen.findByText("小红书 · 平台真实昵称", {}, { timeout: 2500 });
    await waitFor(() =>
      expect(nativeAccounts.importCloudPublishAccount).toHaveBeenCalledWith(
        "xiaohongshu",
        {
          platform_user_id: "platform-uid",
          username: "平台真实昵称",
          avatar_url: null,
        },
        storage,
      ),
    );
    await screen.findByText("服务端暂不可用");
    expect(screen.getByText(/服务端未同步/)).toBeInTheDocument();
    // Retry re-exports from the hidden official window and uploads again.
    nativeAccounts.exportLocalPublishAccountState.mockResolvedValue({
      identity: { platform_user_id: "platform-uid", username: "平台真实昵称" },
      storage_state: storage,
    });
    fireEvent.click(screen.getByRole("button", { name: "同步到服务端" }));
    await waitFor(() =>
      expect(
        nativeAccounts.exportLocalPublishAccountState,
      ).toHaveBeenCalledWith(value.user.id, "local-1"),
    );
    await screen.findByText(/已同步服务端，可自动发布/);
    expect(value.notify).toHaveBeenCalledWith(
      "登录状态已同步到服务端，可用于自动发布",
    );
    expect(screen.queryByText("服务端暂不可用")).toBeNull();
  });
  it("解绑本机账号时同时删除服务端副本", async () => {
    nativeAccounts.listLocalPublishAccounts.mockResolvedValue([account]);
    nativeAccounts.listCloudPublishAccounts.mockResolvedValue([
      {
        ...account,
        id: "cloud-9",
        status: "connected",
        error_message: null,
        source: "desktop",
      },
    ]);
    nativeAccounts.removeLocalPublishAccount.mockResolvedValue(undefined);
    open();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "小红书" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "小红书" }));
    await screen.findByText(/已同步服务端，可自动发布/);
    fireEvent.click(screen.getByRole("button", { name: "解绑" }));
    fireEvent.click(screen.getByRole("button", { name: "确认解绑" }));
    await waitFor(() =>
      expect(nativeAccounts.deleteCloudPublishAccount).toHaveBeenCalledWith(
        "cloud-9",
      ),
    );
  });
  it("只从本机加载账号并显示官方用户名，不提供 Cookie 输入框", async () => {
    nativeAccounts.listLocalPublishAccounts.mockResolvedValue([account]);
    const { value } = open();
    await waitFor(() =>
      expect(nativeAccounts.listLocalPublishAccounts).toHaveBeenCalled(),
    );
    expect(screen.queryByText("小红书 · 平台真实昵称")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "小红书" }));
    await screen.findByText("小红书 · 平台真实昵称");
    expect(nativeAccounts.listLocalPublishAccounts).toHaveBeenCalledWith(
      value.user.id,
    );
    expect(
      screen.queryByPlaceholderText("粘贴从浏览器复制的整段 Cookie"),
    ).toBeNull();
    expect(screen.getByRole("button", { name: "抖音" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "视频号" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "小红书" })).toBeInTheDocument();
  });
  it("扫码添加以平台响应为准，未完成前不显示已连接", async () => {
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("login-1");
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "connected",
      image: null,
      account,
    });
    const { value } = open();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "扫码添加账号" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "小红书" }));
    await screen.findByRole("button", { name: "取消扫码" });
    expect(value.notify).not.toHaveBeenCalledWith(
      expect.stringContaining("已连接"),
    );
    nativeAccounts.listLocalPublishAccounts.mockResolvedValue([account]);
    await screen.findByText("小红书 · 平台真实昵称");
    expect(nativeAccounts.startLocalPublishLogin).toHaveBeenCalledWith(
      value.user.id,
      "xiaohongshu",
      undefined,
    );
    expect(value.notify).toHaveBeenCalledWith("已连接 小红书 · 平台真实昵称");
  });
  it("取消扫码关闭本次原生会话", async () => {
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("login-2");
    const { value } = open();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "扫码添加账号" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "扫码添加账号" }));
    fireEvent.click(await screen.findByRole("button", { name: "取消扫码" }));
    await waitFor(() =>
      expect(nativeAccounts.cancelLocalPublishLogin).toHaveBeenCalledWith(
        value.user.id,
        "login-2",
      ),
    );
  });
  it("解绑必须指向确认的本机账号，失败保留记录并显示错误", async () => {
    nativeAccounts.listLocalPublishAccounts.mockResolvedValue([account]);
    nativeAccounts.removeLocalPublishAccount.mockRejectedValue(
      new Error("请先关闭官方窗口"),
    );
    const { value } = open();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "小红书" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "小红书" }));
    await screen.findByText("小红书 · 平台真实昵称");
    fireEvent.click(screen.getByRole("button", { name: "解绑" }));
    expect(nativeAccounts.removeLocalPublishAccount).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认解绑" }));
    await screen.findByText("请先关闭官方窗口");
    expect(nativeAccounts.removeLocalPublishAccount).toHaveBeenCalledWith(
      value.user.id,
      account.id,
    );
    expect(screen.getByText("小红书 · 平台真实昵称")).toBeInTheDocument();
  });
  it("网页端读取云端账号并允许扫码添加", async () => {
    nativeAccounts.canUseLocalPublishAccounts.mockReturnValue(false);
    open();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "扫码添加账号" }),
      ).toBeEnabled(),
    );
    expect(nativeAccounts.listLocalPublishAccounts).toHaveBeenCalled();
    expect(
      screen.getByText("账号的登录状态加密保存在服务器，可在个人中心解绑。"),
    ).toBeInTheDocument();
  });
  it("非原生模式需要额外验证时不显示官方窗口入口", async () => {
    nativeAccounts.canUseLocalPublishAccounts.mockReturnValue(false);
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("cloud-login");
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "action_required",
      image: null,
      account: null,
    });
    open();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "抖音" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "抖音" }));
    await screen.findByText(
      "平台要求进一步验证，请按平台提示完成后重试。",
      {},
      { timeout: 2500 },
    );

    expect(screen.queryByRole("button", { name: "打开官方窗口" })).toBeNull();
    expect(nativeAccounts.focusLocalPublishLogin).not.toHaveBeenCalled();
  });
  it("个人中心显示二维码，过期后移除图片并可重新获取", async () => {
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("qr-1");
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "qr_ready",
      image: "data:image/png;base64,cXI=",
      account: null,
    });
    const { value } = open();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "扫码添加账号" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "扫码添加账号" }));
    await screen.findByAltText("抖音登录二维码", {}, { timeout: 2500 });
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "expired",
      image: null,
      account: null,
    });
    await screen.findByText(
      "二维码或本次连接已过期，请重新获取。",
      {},
      { timeout: 2500 },
    );
    expect(screen.queryByAltText("抖音登录二维码")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重新获取二维码" }));
    await waitFor(() =>
      expect(nativeAccounts.startLocalPublishLogin).toHaveBeenCalledTimes(2),
    );
    expect(nativeAccounts.cancelLocalPublishLogin).toHaveBeenCalledWith(
      value.user.id,
      "qr-1",
    );
  });
  it.each([
    ["douyin", "抖音"],
    ["wechat_channels", "视频号"],
    ["xiaohongshu", "小红书"],
  ])(
    "点击 %s 直接内嵌二维码，确认后在同一标签显示账号",
    async (platform, name) => {
      const connected = { ...account, platform, username: `${name}测试账号` };
      nativeAccounts.startLocalPublishLogin.mockResolvedValue("inline-login");
      nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
        phase: "qr_ready",
        image: "data:image/png;base64,cXI=",
        account: null,
      });
      const { value } = open();
      await waitFor(() =>
        expect(screen.getByRole("button", { name })).toBeEnabled(),
      );
      fireEvent.click(screen.getByRole("button", { name }));
      await screen.findByAltText(`${name}登录二维码`, {}, { timeout: 2500 });
      expect(
        nativeAccounts.startLocalPublishLogin,
      ).toHaveBeenCalledExactlyOnceWith(value.user.id, platform, undefined);
      expect(nativeAccounts.focusLocalPublishLogin).not.toHaveBeenCalled();
      expect(screen.queryByRole("button", { name: "打开官方窗口" })).toBeNull();
      // A list refresh may lag or fail: the verified account returned by login is authoritative.
      nativeAccounts.listLocalPublishAccounts.mockRejectedValue(
        new Error("列表暂不可用"),
      );
      nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
        phase: "connected",
        image: null,
        account: connected,
      });
      await screen.findByText(
        `${name} · ${name}测试账号`,
        {},
        { timeout: 2500 },
      );
      expect(screen.queryByAltText(`${name}登录二维码`)).toBeNull();
      const otherName = name === "抖音" ? "小红书" : "抖音";
      fireEvent.click(screen.getByRole("button", { name: otherName }));
      expect(screen.queryByText(`${name} · ${name}测试账号`)).toBeNull();
    },
  );
  it("已有账号的平台仅切换列表，添加更多账号仍可主动扫码", async () => {
    nativeAccounts.listLocalPublishAccounts.mockResolvedValue([account]);
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("second-login");
    open();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "小红书" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "小红书" }));
    await screen.findByText("小红书 · 平台真实昵称");
    expect(nativeAccounts.startLocalPublishLogin).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "扫码添加账号" }));
    await screen.findByRole("button", { name: "取消扫码" });
    expect(nativeAccounts.startLocalPublishLogin).toHaveBeenCalledOnce();
  });
  it("需要进一步验证时自动打开官方窗口，无需手动点击", async () => {
    vi.useFakeTimers();
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("verify-login");
    nativeAccounts.focusLocalPublishLogin.mockResolvedValue(undefined);
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "action_required",
      image: null,
      account: null,
    });
    const { value } = open();
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "抖音" }));
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    // 源码取码失败（action_required）即自动降级为可见官方窗口，只触发一次。
    expect(
      nativeAccounts.focusLocalPublishLogin,
    ).toHaveBeenCalledExactlyOnceWith(value.user.id, "verify-login");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(nativeAccounts.focusLocalPublishLogin).toHaveBeenCalledOnce();
  });
  it("隐藏窗口持续取不到二维码时自动降级打开官方窗口", async () => {
    vi.useFakeTimers();
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("slow-login");
    nativeAccounts.focusLocalPublishLogin.mockResolvedValue(undefined);
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "loading",
      image: null,
      account: null,
    });
    const { value } = open();
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "抖音" }));
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(nativeAccounts.focusLocalPublishLogin).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(
      nativeAccounts.focusLocalPublishLogin,
    ).toHaveBeenCalledExactlyOnceWith(value.user.id, "slow-login");
  });
  it("二维码就绪时不自动打开官方窗口，验证阶段仍可手动唤起", async () => {
    vi.useFakeTimers();
    nativeAccounts.startLocalPublishLogin.mockResolvedValue("ready-login");
    nativeAccounts.focusLocalPublishLogin.mockResolvedValue(undefined);
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "qr_ready",
      image: "data:image/png;base64,cXI=",
      account: null,
    });
    const { value } = open();
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "抖音" }));
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(35_000);
    });
    // 源码取码成功（qr_ready）时绝不自动弹官方窗口。
    expect(nativeAccounts.focusLocalPublishLogin).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "打开官方窗口" })).toBeNull();
    // 进入验证阶段后自动降级触发；手动按钮保留，便于窗口被最小化后重新唤起。
    nativeAccounts.checkLocalPublishLogin.mockResolvedValue({
      phase: "action_required",
      image: null,
      account: null,
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });
    expect(
      nativeAccounts.focusLocalPublishLogin,
    ).toHaveBeenCalledExactlyOnceWith(value.user.id, "ready-login");
    fireEvent.click(screen.getByRole("button", { name: "打开官方窗口" }));
    await act(async () => {});
    expect(nativeAccounts.focusLocalPublishLogin).toHaveBeenCalledTimes(2);
  });
});
