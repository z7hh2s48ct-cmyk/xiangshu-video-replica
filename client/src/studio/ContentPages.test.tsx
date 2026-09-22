import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useCallback, useLayoutEffect, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MaterialItem, ViralVideoItem } from "../api";
import type {
  StudioContextValue,
  StudioData,
  StudioPublishDraft,
  StudioState,
} from "./types";

const {
  useStudio,
  fetchViralVideoMedia,
  fetchViralVideoStatistics,
  fetchViralVideo,
  createViralImportTask,
  getViralImportTask,
  listViralFavorites,
  listViralVideos,
  saveViralFavorite,
  removeViralFavorite,
  listMaterials,
  listMaterialGroups,
  bulkUpdateMaterials,
  createMaterialUploadIntent,
  uploadMaterial,
  completeMaterialUpload,
  putMaterial,
  updateMaterial,
  hideMaterial,
  downloadMaterialAsset,
  getAssetDownloadUrl,
  getMaterialBatchPreviews,
  getMaterialCachedPreview,
  getMaterialCacheUsage,
  clearMaterialCache,
  evictMaterialCachedPreview,
  getStudioDraft,
  saveStudioDraft,
  resolveMaterials,
  createGenerationTaskPreviewUrl,
  createPublishRecord,
  listPublishRecords,
  cancelPublishRecord,
  retryPublishRecord,
  syncPublishRecord,
  deletePublishRecord,
} = vi.hoisted(() => ({
  useStudio: vi.fn(),
  fetchViralVideoMedia: vi.fn(),
  fetchViralVideoStatistics: vi.fn(),
  fetchViralVideo: vi.fn(),
  createViralImportTask: vi.fn(),
  getViralImportTask: vi.fn(),
  listViralFavorites: vi.fn(),
  listViralVideos: vi.fn(),
  saveViralFavorite: vi.fn(),
  removeViralFavorite: vi.fn(),
  listMaterials: vi.fn(),
  listMaterialGroups: vi.fn(
    async (): Promise<{ items: { name: string; count: number }[] }> => ({
      items: [],
    }),
  ),
  bulkUpdateMaterials: vi.fn(),
  createMaterialUploadIntent: vi.fn(),
  uploadMaterial: vi.fn(),
  completeMaterialUpload: vi.fn(),
  putMaterial: vi.fn(),
  updateMaterial: vi.fn(),
  hideMaterial: vi.fn(),
  downloadMaterialAsset: vi.fn(),
  getAssetDownloadUrl: vi.fn(),
  getMaterialBatchPreviews: vi.fn(),
  getMaterialCachedPreview: vi.fn(),
  getMaterialCacheUsage: vi.fn(),
  clearMaterialCache: vi.fn(),
  evictMaterialCachedPreview: vi.fn(),
  getStudioDraft: vi.fn(
    async (): Promise<{
      revision: number;
      payload: { drafts: StudioPublishDraft[] };
    }> => ({ revision: 1, payload: { drafts: [] } }),
  ),
  saveStudioDraft: vi.fn(),
  resolveMaterials: vi.fn(
    async (): Promise<{ items: MaterialItem[] }> => ({ items: [] }),
  ),
  createGenerationTaskPreviewUrl: vi.fn(),
  createPublishRecord: vi.fn(),
  listPublishRecords: vi.fn(async (): Promise<unknown[]> => []),
  cancelPublishRecord: vi.fn(),
  retryPublishRecord: vi.fn(),
  syncPublishRecord: vi.fn(),
  deletePublishRecord: vi.fn(),
}));
vi.mock("./context", () => ({ useStudio }));
vi.mock("../api", () => ({
  fetchViralVideoMedia,
  fetchViralVideoStatistics,
  fetchViralVideo,
  createViralImportTask,
  getViralImportTask,
  listViralFavorites,
  listViralVideos,
  saveViralFavorite,
  removeViralFavorite,
  listMaterials,
  listMaterialGroups,
  bulkUpdateMaterials,
  createMaterialUploadIntent,
  uploadMaterial,
  completeMaterialUpload,
  putMaterial,
  updateMaterial,
  hideMaterial,
  downloadMaterialAsset,
  getAssetDownloadUrl,
  getMaterialBatchPreviews,
  getMaterialCachedPreview,
  getMaterialCacheUsage,
  clearMaterialCache,
  evictMaterialCachedPreview,
  getStudioDraft,
  saveStudioDraft,
  resolveMaterials,
  createGenerationTaskPreviewUrl,
  createPublishRecord,
  listPublishRecords,
  cancelPublishRecord,
  retryPublishRecord,
  syncPublishRecord,
  deletePublishRecord,
}));
const publishAccounts = vi.hoisted(() => ({
  canUseLocalPublishAccounts: vi.fn(() => false),
  listCloudPublishAccounts: vi.fn(async () => [] as unknown[]),
  listLocalPublishAccounts: vi.fn(async () => [] as unknown[]),
  openLocalPublishAccount: vi.fn(),
}));
vi.mock("./localPublishAccounts", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  ...publishAccounts,
}));
// 组件现在统一走 putMaterial。默认实现沿用旧的「传输 → 完成」两步，
// 这样既有用例针对 uploadMaterial / completeMaterialUpload 打的桩仍然生效；
// 需要覆盖复用路径的用例可以直接给 putMaterial 打桩。
putMaterial.mockImplementation(async (intent, file, onProgress) => {
  await uploadMaterial(intent, file, onProgress);
  return completeMaterialUpload(intent.asset_id);
});

class IntersectionObserverStub {
  observe() {}
  disconnect() {}
  unobserve() {}
  takeRecords() {
    return [];
  }
}
vi.stubGlobal("IntersectionObserver", IntersectionObserverStub);

import {
  failMaterialPreview,
  MaterialsPage,
  PublishPage,
  ViralDetailPage,
  ViralFavoriteButton,
  ViralPage,
  visiblePageButtons,
} from "./ContentPages";

describe("素材分页窗口化", () => {
  it("页数不超过 7 时完整展示页码", () => {
    expect(visiblePageButtons(1, 3)).toEqual([1, 2, 3]);
    expect(visiblePageButtons(3, 7)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  });

  it("页数较多时折叠为省略号并始终保留首末页与当前页邻域", () => {
    expect(visiblePageButtons(1, 13)).toEqual([1, 2, "…", 13]);
    expect(visiblePageButtons(2, 13)).toEqual([1, 2, 3, "…", 13]);
    expect(visiblePageButtons(7, 13)).toEqual([1, "…", 6, 7, 8, "…", 13]);
    expect(visiblePageButtons(13, 13)).toEqual([1, "…", 12, 13]);
  });
});

function studio(
  overrides: Partial<StudioContextValue> = {},
): StudioContextValue {
  return {
    state: {
      page: "viral",
      draft: {
        id: "draft-1",
        selectedShotId: "shot-1",
        script: {
          id: "script-1",
          title: "",
          original: "",
          text: "",
          version: 1,
          confirmed: false,
        },
        prompt: "",
        referenceIds: [],
        resolution: "768P",
        ratio: "16:9",
        duration: 4,
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
      assets: [
        {
          id: "audio-1",
          name: "张工讲预算.wav",
          kind: "audio",
          group: "口播音频",
          source: "素材库",
          saved: true,
          personId: "person-1",
        },
      ],
      videos: [
        {
          id: "dy-1",
          title: "农村建房预算，别只盯着主体",
          author: "乡墅建房笔记",
          platform: "抖音",
          category: "建房预算",
          poster: "/studio/demo.jpg",
          duration: "01:28",
          likes: 18000,
          collections: 842,
          shares: 326,
          description: "主体之外，门窗、水电、防水和庭院也要列进预算清单。",
          platformKey: "douyin",
          nativeId: "native-dy-1",
          verified: true,
          tags: ["农村自建房", "建房预算"],
          hasPlayableAudio: true,
        },
        {
          id: "wx-1",
          title: "新中式庭院的三个细节",
          author: "庭院设计老周",
          platform: "视频号",
          category: "庭院案例",
          poster: "/studio/demo-2.jpg",
          duration: "01:12",
          likes: 9800,
          collections: 430,
          shares: 92,
          description: "从动线、植物和夜景灯光说庭院。",
          platformKey: "wechat_channels",
          nativeId: "native-wx-1",
          publishedDisplay: "3天前",
          likeDisplay: "1.2万",
          tags: ["庭院案例", "别墅设计"],
          hasPlayableAudio: false,
        },
      ],
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
    refresh: vi.fn(),
    ...overrides,
  };
}

function material(
  id: string,
  overrides: Partial<MaterialItem> = {},
): MaterialItem {
  return {
    id: `asset:${id}`,
    owner_user_id: "employee_1",
    asset_id: id,
    generation_task_id: null,
    project_id: null,
    person_id: null,
    title: `${id}.png`,
    group: "我的上传",
    media_type: "image",
    source: "upload",
    status: "ready",
    delivery: "stored",
    content_type: "image/png",
    size_bytes: 1024,
    duration_seconds: null,
    created_at: "2026-09-08 10:00:00",
    hidden: false,
    saved: true,
    composite: false,
    allowed_uses: [],
    allowed_actions: ["preview", "download", "rename", "hide"],
    ...overrides,
  };
}

function StatefulViralPage({ value }: { value: StudioContextValue }) {
  const [data, setData] = useState(value.data);
  const updateData = useCallback(
    (update: (current: StudioData) => StudioData) =>
      setData((current) => update(current)),
    [],
  );
  useStudio.mockReturnValue({
    ...value,
    data,
    updateData,
  });
  return <ViralPage />;
}

function RenderWindowTrigger({ onLayout }: { onLayout?: () => void }) {
  useLayoutEffect(() => onLayout?.(), [onLayout]);
  return <ViralPage />;
}

function DetailRenderWindowTrigger({ onLayout }: { onLayout?: () => void }) {
  useLayoutEffect(() => onLayout?.(), [onLayout]);
  return <ViralDetailPage />;
}

function FavoriteRenderWindowTrigger({
  onLayout,
  onSavedChange,
}: {
  onLayout?: () => void;
  onSavedChange: (saved: boolean) => void;
}) {
  useLayoutEffect(() => onLayout?.(), [onLayout]);
  return (
    <ViralFavoriteButton
      video={studio().data.videos[0]}
      savedFromServer={false}
      onSavedChange={onSavedChange}
    />
  );
}

function viralItem(index: number, overrides: Partial<ViralVideoItem> = {}) {
  return {
    platform: "douyin" as const,
    videoId: `native-dy-${index}`,
    category: "建房预算",
    title: `乡墅参考 ${index}`,
    author: "乡墅建房笔记",
    authorAvatar: null,
    verified: false,
    coverUrl: null,
    durationMs: 30_000,
    likes: 100 - index,
    comments: 1,
    shares: 2,
    collects: 3,
    publishedAt: 1_788_600_000 - index,
    publishedDisplay: null,
    likeDisplay: String(100 - index),
    tags: [],
    hasPlayableAudio: true,
    playUrl: null,
    isFavorite: false,
    availability: "available" as const,
    ...overrides,
  };
}

describe("V1.4 内容与运营页面", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    useStudio.mockReset();
    fetchViralVideoMedia.mockReset();
    fetchViralVideoStatistics.mockReset();
    fetchViralVideo.mockReset();
    createViralImportTask.mockReset();
    getViralImportTask.mockReset();
    listViralFavorites.mockReset();
    listViralVideos.mockReset();
    saveViralFavorite.mockReset();
    removeViralFavorite.mockReset();
    listViralVideos.mockResolvedValue({
      platform: "douyin",
      sort: "hot",
      categories: [],
      items: [],
      fetchedAt: null,
      hasMore: false,
      nextCursor: null,
      total: 0,
    });
    listViralFavorites.mockResolvedValue({ items: [], total: 0 });
    saveViralFavorite.mockResolvedValue({ isFavorite: true });
    removeViralFavorite.mockResolvedValue({ isFavorite: false });
    createViralImportTask.mockResolvedValue({
      taskId: "import-1",
      status: "SUCCEEDED",
      projectId: "project-1",
      sourceAssetId: "asset-1",
      mediaKind: "video",
      canTranscribe: true,
      canAnalyze: true,
    });
    window.history.replaceState(null, "", "/");
    listMaterials.mockReset();
    listMaterialGroups.mockReset().mockResolvedValue({ items: [] });
    bulkUpdateMaterials.mockReset();
    createMaterialUploadIntent.mockReset();
    uploadMaterial.mockReset();
    completeMaterialUpload.mockReset();
    updateMaterial.mockReset();
    hideMaterial.mockReset();
    downloadMaterialAsset.mockReset();
    getAssetDownloadUrl.mockReset();
    getMaterialCachedPreview
      .mockReset()
      .mockImplementation(async (_userId, assetId) => ({
        ...(await getAssetDownloadUrl(assetId)),
        cached: false,
        release: vi.fn(),
      }));
    getMaterialBatchPreviews
      .mockReset()
      .mockImplementation(
        async (
          _userId: string,
          entries: { id: string; populate: boolean }[],
        ) => {
          const previews: Record<
            string,
            { url: string; cached: boolean; release: () => unknown }
          > = {};
          for (const entry of entries) {
            previews[entry.id] = {
              ...(await getAssetDownloadUrl(entry.id)),
              cached: false,
              release: vi.fn(),
            };
          }
          return { previews, thumbnails: {} };
        },
      );
    getMaterialCacheUsage.mockReset().mockResolvedValue({
      bytes: 0,
      limitBytes: 256 * 1024 * 1024,
      available: true,
    });
    clearMaterialCache.mockReset().mockResolvedValue(undefined);
    evictMaterialCachedPreview.mockReset().mockResolvedValue(undefined);
    createGenerationTaskPreviewUrl.mockReset();
    getAssetDownloadUrl.mockResolvedValue({
      url: "https://storage.test/material",
    });
    createGenerationTaskPreviewUrl.mockResolvedValue(
      "https://provider.test/direct-result.mp4",
    );
  });

  it("迟到的旧媒体错误不能覆盖已成功的新签名", () => {
    const current = {
      material: { status: "ready" as const, url: "https://storage.test/new" },
    };
    expect(
      failMaterialPreview(current, "material", "https://storage.test/old"),
    ).toBe(current);
    expect(
      failMaterialPreview(current, "material", "https://storage.test/new"),
    ).toEqual({ material: { status: "error" } });
  });

  it("爆款卡片主操作统一为详情和提取文案并保留收藏", () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    const view = render(<ViralPage />);
    fireEvent.click(screen.getByRole("tab", { name: "视频号 30" }));
    expect(screen.getByText("新中式庭院的三个细节")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "抖音 20" }));
    fireEvent.click(
      screen.getByRole("button", { name: "收藏 农村建房预算，别只盯着主体" }),
    );
    expect(value.patchState).toHaveBeenCalledWith({ favorites: ["dy-1"] });
    fireEvent.click(
      screen.getByRole("button", {
        name: "提取文案 农村建房预算，别只盯着主体",
      }),
    );
    expect(value.patchDraft).toHaveBeenCalledWith({ sourceId: "dy-1" });
    expect(value.navigate).toHaveBeenCalledWith("copy", {
      selectedVideoId: "dy-1",
      returnTo: "viral",
    });
    expect(
      screen.queryByRole("button", { name: /复刻/ }),
    ).not.toBeInTheDocument();
    for (const actions of view.container.querySelectorAll(
      ".content-card-actions",
    )) {
      expect(
        [...actions.querySelectorAll("button")].map(
          (button) => button.textContent,
        ),
      ).toEqual(["查看详情", "提取文案"]);
    }
  });

  it("我的收藏读取当前平台并通过真实素材导入提取文案", async () => {
    const base = studio();
    const value = studio({
      review: false,
      data: { ...base.data, videos: [] },
    });
    listViralFavorites.mockResolvedValue({
      items: [
        {
          platform: "douyin",
          videoId: "favorite-native-1",
          category: "庭院案例",
          title: "已收藏的庭院视频",
          author: "乡墅作者",
          authorAvatar: null,
          verified: false,
          coverUrl: null,
          durationMs: 16_000,
          likes: 123,
          comments: 4,
          shares: 5,
          collects: 6,
          publishedAt: 1_788_600_000,
          publishedDisplay: "1天前",
          likeDisplay: "123",
          tags: ["庭院"],
          hasPlayableAudio: true,
          playUrl: null,
          isFavorite: true,
          availability: "available",
        },
      ],
      total: 1,
      hasMore: false,
      nextCursor: null,
    });
    useStudio.mockReturnValue(value);
    const view = render(<ViralPage />);

    fireEvent.click(screen.getByRole("tab", { name: "我的收藏" }));

    await waitFor(() =>
      expect(listViralFavorites).toHaveBeenCalledWith({
        platform: "douyin",
        limit: 12,
      }),
    );
    const update = vi.mocked(value.updateData).mock.calls.at(-1)?.[0];
    expect(update?.(value.data).videos).toEqual([
      expect.objectContaining({
        nativeId: "favorite-native-1",
        title: "已收藏的庭院视频",
      }),
    ]);
    useStudio.mockReturnValue({
      ...value,
      data: update?.(value.data) ?? value.data,
    });
    view.rerender(<ViralPage />);
    expect(
      screen.queryByRole("button", { name: /复刻/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "收藏 已收藏的庭院视频" }),
    ).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(
      screen.getByRole("button", { name: "提取文案 已收藏的庭院视频" }),
    );
    await waitFor(() =>
      expect(value.extractScriptFromUpload).toHaveBeenCalledWith(
        "project-1",
        "asset-1",
      ),
    );
    expect(createViralImportTask).toHaveBeenCalledWith(
      "douyin",
      "favorite-native-1",
      "copy",
      expect.any(String),
    );
    expect(value.navigate).toHaveBeenCalledWith(
      "copy",
      expect.objectContaining({ returnTo: "viral" }),
    );
  });

  it("爆款详情导入来源项目后把真实资产交给文案工坊", async () => {
    const value = studio({
      state: {
        ...studio().state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    const view = render(<ViralDetailPage />);
    expect(
      screen.getByRole("heading", { name: "爆款视频 / 视频详情" }),
    ).toBeInTheDocument();
    expect(screen.getByText("乡墅建房笔记")).toBeInTheDocument();
    expect(screen.getByText("抖音 · 建房预算 · 有原声")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "播放 农村建房预算，别只盯着主体" }),
    ).toBeInTheDocument();
    expect(view.container.querySelector("main")).toBeNull();
    expect(screen.queryByText("内容浏览示例审核")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    expect(createViralImportTask).toHaveBeenCalledWith(
      "douyin",
      "native-dy-1",
      "copy",
      expect.any(String),
    );
    await waitFor(() => {
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "project-1",
        sourceId: "asset-1",
        sourceAssetId: "asset-1",
      });
      expect(value.extractScriptFromUpload).toHaveBeenCalledWith(
        "project-1",
        "asset-1",
      );
    });
    expect(value.navigate).toHaveBeenCalledWith("copy", {
      selectedVideoId: "dy-1",
      returnTo: "viral-detail",
    });
    expect(screen.getByText("参考素材已导入")).toBeInTheDocument();
  });

  it("同一爆款重挂载后复用幂等键，避免重复创建项目和任务", async () => {
    const value = studio({
      state: {
        ...studio().state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    const firstView = render(<ViralDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledOnce());
    const firstKey = createViralImportTask.mock.calls[0][3];
    firstView.unmount();
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));

    expect(createViralImportTask.mock.calls[1][3]).toBe(firstKey);
  });

  it("请求未完成时切换爆款来源会丢弃旧来源迟到结果", async () => {
    let resolveFirst: ((value: unknown) => void) | undefined;
    createViralImportTask.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveFirst = resolve;
        }),
    );
    const base = studio();
    let value = studio({
      state: { ...base.state, page: "viral-detail", selectedVideoId: "dy-1" },
    });
    useStudio.mockImplementation(() => value);
    const view = render(<ViralDetailPage />);
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));

    value = studio({
      state: { ...base.state, page: "viral-detail", selectedVideoId: "wx-1" },
    });
    view.rerender(<ViralDetailPage />);
    expect(screen.getByText("新中式庭院的三个细节")).toBeInTheDocument();

    await act(async () => {
      resolveFirst?.({
        taskId: "late-first",
        status: "SUCCEEDED",
        projectId: "project-first",
        sourceAssetId: "asset-first",
        mediaKind: "video",
        canTranscribe: true,
        canAnalyze: true,
      });
    });

    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
    expect(value.extractScriptFromUpload).not.toHaveBeenCalled();
  });

  it("切换账户会隔离操作键并丢弃上一账户的迟到结果", async () => {
    let resolveFirst: ((value: unknown) => void) | undefined;
    createViralImportTask.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveFirst = resolve;
        }),
    );
    const base = studio();
    const accountA = {
      id: "customer-a",
      username: "customer-a",
      display_name: "客户A",
      role: "customer" as const,
    };
    const accountB = {
      id: "customer-b",
      username: "customer-b",
      display_name: "客户B",
      role: "customer" as const,
    };
    let value = studio({
      user: accountA,
      state: { ...base.state, page: "viral-detail", selectedVideoId: "dy-1" },
    });
    useStudio.mockImplementation(() => value);
    const view = render(<ViralDetailPage />);
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    const accountAKey = createViralImportTask.mock.calls[0][3];

    value = studio({
      user: accountB,
      state: { ...base.state, page: "viral-detail", selectedVideoId: "dy-1" },
    });
    view.rerender(<ViralDetailPage />);
    await act(async () => {
      resolveFirst?.({
        id: "late-account-a",
        status: "SUCCEEDED",
        projectId: "project-a",
        sourceAssetId: "asset-a",
        canTranscribe: true,
        canAnalyze: true,
      });
    });
    expect(value.patchDraft).not.toHaveBeenCalled();

    createViralImportTask.mockResolvedValueOnce({
      id: "account-b-import",
      status: "SUCCEEDED",
      projectId: "project-b",
      sourceAssetId: "asset-b",
      canTranscribe: true,
      canAnalyze: true,
    });
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));
    expect(createViralImportTask.mock.calls[1][3]).not.toBe(accountAKey);

    value = studio({
      user: accountA,
      state: { ...base.state, page: "viral-detail", selectedVideoId: "dy-1" },
    });
    view.rerender(<ViralDetailPage />);
    createViralImportTask.mockResolvedValueOnce({
      id: "account-a-replay",
      status: "SUCCEEDED",
      projectId: "project-a",
      sourceAssetId: "asset-a",
      canTranscribe: true,
      canAnalyze: true,
    });
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(3));
    expect(createViralImportTask.mock.calls[2][3]).toBe(accountAKey);
  });

  it("视频号爆款使用平台与原生视频 ID 建立提取任务", async () => {
    const base = studio();
    const value = studio({
      state: { ...base.state, page: "viral-detail", selectedVideoId: "wx-1" },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));

    await waitFor(() =>
      expect(createViralImportTask).toHaveBeenCalledWith(
        "wechat_channels",
        "native-wx-1",
        "copy",
        expect.any(String),
      ),
    );
  });

  it("爆款详情移除复刻且仅音频来源可交给文案提取", async () => {
    createViralImportTask.mockResolvedValue({
      taskId: "audio-import",
      status: "SUCCEEDED",
      projectId: "audio-project",
      sourceAssetId: "audio-asset",
      mediaKind: "audio",
      canTranscribe: true,
      canAnalyze: false,
    });
    const value = studio({
      state: {
        ...studio().state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    expect(
      screen.queryByRole("button", { name: /复刻/ }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() =>
      expect(value.extractScriptFromUpload).toHaveBeenCalledWith(
        "audio-project",
        "audio-asset",
      ),
    );
    expect(createViralImportTask).toHaveBeenCalledWith(
      "douyin",
      "native-dy-1",
      "copy",
      expect.any(String),
    );
    expect(value.navigate).toHaveBeenCalledWith(
      "copy",
      expect.objectContaining({ returnTo: "viral-detail" }),
    );
  });

  it("不可转写的畸形成功不会写入文案草稿且再次点击使用新键", async () => {
    createViralImportTask.mockResolvedValue({
      taskId: "malformed-copy",
      status: "SUCCEEDED",
      projectId: "copy-project",
      sourceAssetId: "copy-asset",
      mediaKind: "video",
      canTranscribe: false,
      canAnalyze: true,
    });
    const value = studio({
      state: {
        ...studio().state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    expect(
      await screen.findByText("该来源暂不支持提取文案"),
    ).toBeInTheDocument();
    const malformedKey = createViralImportTask.mock.calls[0][3];
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));
    expect(createViralImportTask.mock.calls[1][3]).not.toBe(malformedKey);
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.extractScriptFromUpload).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("正式爆款卡片缺少平台原生 ID 时明确报错且不进入伪项目", () => {
    const base = studio();
    const malformed = {
      ...base.data.videos[0],
      platformKey: undefined,
      nativeId: undefined,
    };
    const value = studio({
      review: false,
      data: { ...base.data, videos: [malformed] },
    });
    useStudio.mockReturnValue(value);
    render(<ViralPage />);

    fireEvent.click(
      screen.getByRole("button", { name: `提取文案 ${malformed.title}` }),
    );

    expect(value.notify).toHaveBeenCalledWith("该视频缺少可导入的平台标识");
    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("导入排队时始终使用服务端返回的同一任务 ID 轮询", async () => {
    vi.useFakeTimers();
    try {
      createViralImportTask.mockResolvedValue({
        id: "server-import-1",
        status: "PENDING",
        projectId: "project-1",
        sourceAssetId: null,
        canTranscribe: false,
        canAnalyze: false,
      });
      getViralImportTask.mockResolvedValue({
        id: "server-import-1",
        status: "SUCCEEDED",
        projectId: "project-1",
        sourceAssetId: "asset-1",
        canTranscribe: true,
        canAnalyze: true,
      });
      const base = studio();
      const value = studio({
        state: {
          ...base.state,
          page: "viral-detail",
          selectedVideoId: "dy-1",
        },
      });
      useStudio.mockReturnValue(value);
      render(<ViralDetailPage />);

      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });

      expect(getViralImportTask).toHaveBeenCalledWith("server-import-1");
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "project-1",
        sourceId: "asset-1",
        sourceAssetId: "asset-1",
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("第120次轮询成功时只写入一次草稿并保留操作键", async () => {
    vi.useFakeTimers();
    try {
      let polls = 0;
      createViralImportTask.mockResolvedValue({
        id: "boundary-success",
        status: "PENDING",
        projectId: "boundary-project",
        sourceAssetId: null,
      });
      getViralImportTask.mockImplementation(async () => {
        polls += 1;
        return polls >= 120
          ? {
              id: "boundary-success",
              status: "SUCCEEDED",
              projectId: "boundary-project",
              sourceAssetId: "boundary-asset",
              canTranscribe: true,
              canAnalyze: true,
            }
          : {
              id: "boundary-success",
              status: "PENDING",
              projectId: "boundary-project",
              sourceAssetId: null,
            };
      });
      const value = studio({
        state: {
          ...studio().state,
          page: "viral-detail",
          selectedVideoId: "dy-1",
        },
      });
      useStudio.mockReturnValue(value);
      render(<ViralDetailPage />);

      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      const firstKey = createViralImportTask.mock.calls[0][3];
      await act(async () => {
        await vi.advanceTimersByTimeAsync(120_000);
      });

      expect(getViralImportTask).toHaveBeenCalledTimes(120);
      expect(value.patchDraft).toHaveBeenCalledOnce();
      expect(value.patchDraft).toHaveBeenCalledWith({
        projectId: "boundary-project",
        sourceId: "boundary-asset",
        sourceAssetId: "boundary-asset",
      });
      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      expect(createViralImportTask.mock.calls[1][3]).toBe(firstKey);
    } finally {
      vi.useRealTimers();
    }
  });

  it("第120次轮询永久失败时展示失败并清除操作键", async () => {
    vi.useFakeTimers();
    try {
      let polls = 0;
      createViralImportTask.mockResolvedValue({
        id: "boundary-failure",
        status: "PENDING",
        projectId: "boundary-project",
        sourceAssetId: null,
      });
      getViralImportTask.mockImplementation(async () => {
        polls += 1;
        return polls >= 120
          ? {
              id: "boundary-failure",
              status: "FAILED",
              projectId: "boundary-project",
              sourceAssetId: null,
              retryable: false,
              errorMessage: "来源归属已变化",
            }
          : {
              id: "boundary-failure",
              status: "PENDING",
              projectId: "boundary-project",
              sourceAssetId: null,
            };
      });
      const value = studio({
        state: {
          ...studio().state,
          page: "viral-detail",
          selectedVideoId: "dy-1",
        },
      });
      useStudio.mockReturnValue(value);
      render(<ViralDetailPage />);

      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      const firstKey = createViralImportTask.mock.calls[0][3];
      await act(async () => {
        await vi.advanceTimersByTimeAsync(120_000);
      });

      expect(getViralImportTask).toHaveBeenCalledTimes(120);
      expect(screen.getByText("来源归属已变化")).toBeInTheDocument();
      expect(value.patchDraft).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      expect(createViralImportTask.mock.calls[1][3]).not.toBe(firstKey);
    } finally {
      vi.useRealTimers();
    }
  });

  it("离开爆款详情后，迟到的来源导入不会替换当前草稿或跳转", async () => {
    let resolve: ((value: unknown) => void) | undefined;
    createViralImportTask.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const base = studio();
    const value = studio({
      state: { ...base.state, page: "viral-detail", selectedVideoId: "dy-1" },
    });
    useStudio.mockReturnValue(value);
    const view = render(<ViralDetailPage />);
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    const firstKey = createViralImportTask.mock.calls[0][3];
    view.unmount();

    await act(async () => {
      resolve?.({
        taskId: "late-import",
        status: "SUCCEEDED",
        projectId: "late-project",
        sourceAssetId: "late-asset",
        mediaKind: "video",
        canTranscribe: true,
        canAnalyze: true,
      });
    });

    expect(value.patchDraft).not.toHaveBeenCalled();
    expect(value.navigate).not.toHaveBeenCalled();
    expect(value.updateData).not.toHaveBeenCalled();

    createViralImportTask.mockResolvedValueOnce({
      taskId: "replayed-import",
      status: "SUCCEEDED",
      projectId: "replayed-project",
      sourceAssetId: "replayed-asset",
      mediaKind: "video",
      canTranscribe: true,
      canAnalyze: true,
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));
    expect(createViralImportTask.mock.calls[1][3]).toBe(firstKey);
  });

  it("导入轮询超时后清除旧键，允许用户发起新任务", async () => {
    vi.useFakeTimers();
    try {
      createViralImportTask.mockResolvedValue({
        id: "stalled-import",
        status: "PENDING",
        projectId: "stalled-project",
        sourceAssetId: null,
        retryable: true,
      });
      getViralImportTask.mockResolvedValue({
        id: "stalled-import",
        status: "RUNNING",
        projectId: "stalled-project",
        sourceAssetId: null,
        retryable: true,
      });
      const base = studio();
      const value = studio({
        user: {
          id: "customer-timeout",
          username: "customer-timeout",
          display_name: "超时客户",
          role: "customer",
        },
        state: {
          ...base.state,
          page: "viral-detail",
          selectedVideoId: "dy-1",
        },
      });
      useStudio.mockReturnValue(value);
      render(<ViralDetailPage />);

      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      const firstKey = createViralImportTask.mock.calls[0][3];
      await act(async () => {
        await vi.advanceTimersByTimeAsync(120_000);
      });
      expect(
        screen.getByText("导入任务等待超时，请稍后重试"),
      ).toBeInTheDocument();

      createViralImportTask.mockResolvedValueOnce({
        id: "new-import",
        status: "SUCCEEDED",
        projectId: "new-project",
        sourceAssetId: "new-asset",
        canTranscribe: true,
        canAnalyze: true,
      });
      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      await act(async () => {});

      expect(createViralImportTask).toHaveBeenCalledTimes(2);
      expect(createViralImportTask.mock.calls[1][3]).not.toBe(firstKey);
    } finally {
      vi.useRealTimers();
    }
  });

  it("爆款导入失败时保持详情页并展示错误", async () => {
    createViralImportTask.mockRejectedValue(new Error("素材暂时无法获取"));
    const value = studio({
      state: {
        ...studio().state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    const view = render(<ViralDetailPage />);
    expect(view.container.querySelector(".content-detail-grid")).toHaveClass(
      "content-detail-grid-viral",
    );
    expect(screen.getByText("▶ 播放")).toHaveClass("content-player-viral-play");
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => {
      expect(screen.getByText(/素材暂时无法获取/)).toBeInTheDocument();
    });
    const firstKey = createViralImportTask.mock.calls[0][3];
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));
    expect(createViralImportTask.mock.calls[1][3]).toBe(firstKey);
    expect(value.navigate).not.toHaveBeenCalled();
  });

  it("永久失败后清除旧幂等键，再次点击创建新操作", async () => {
    createViralImportTask
      .mockResolvedValueOnce({
        id: "permanent-failure",
        status: "FAILED",
        retryable: false,
        errorMessage: "来源已过期",
      })
      .mockResolvedValueOnce({
        id: "replacement",
        status: "SUCCEEDED",
        projectId: "project-new",
        sourceAssetId: "asset-new",
        canTranscribe: true,
        canAnalyze: true,
      });
    const value = studio({
      user: {
        id: "customer-a",
        username: "customer-a",
        display_name: "客户A",
        role: "customer",
      },
      state: {
        ...studio().state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await screen.findByText("来源已过期");
    const failedKey = createViralImportTask.mock.calls[0][3];
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));

    expect(createViralImportTask.mock.calls[1][3]).not.toBe(failedKey);
  });

  it("结果未知的网络异常保留幂等键供安全重放", async () => {
    createViralImportTask
      .mockRejectedValueOnce(new Error("network uncertain"))
      .mockResolvedValueOnce({
        id: "replayed",
        status: "SUCCEEDED",
        projectId: "project-replayed",
        sourceAssetId: "asset-replayed",
        canTranscribe: true,
        canAnalyze: true,
      });
    const value = studio({
      user: {
        id: "customer-a",
        username: "customer-a",
        display_name: "客户A",
        role: "customer",
      },
      state: {
        ...studio().state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await screen.findByText("network uncertain");
    const uncertainKey = createViralImportTask.mock.calls[0][3];
    fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
    await waitFor(() => expect(createViralImportTask).toHaveBeenCalledTimes(2));

    expect(createViralImportTask.mock.calls[1][3]).toBe(uncertainKey);
  });

  it("过期的视频选择显示空态，不回退到任意视频", () => {
    const base = studio();
    useStudio.mockReturnValue(
      studio({
        review: false,
        state: {
          ...base.state,
          page: "viral-detail",
          selectedVideoId: "removed-video",
        },
      }),
    );
    render(<ViralDetailPage />);

    expect(screen.getByText("暂未选择参考视频")).toBeInTheDocument();
    expect(screen.queryByText("农村建房预算，别只盯着主体")).toBeNull();
  });

  it("爆款列表不向用户展示采集参数后台提示", () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    render(<ViralPage />);

    expect(screen.queryByText("采集参数仅在管理后台配置。")).toBeNull();
    expect(screen.queryByText("内容浏览示例审核")).toBeNull();
  });

  it("爆款封面失败状态随封面地址更新而恢复", () => {
    const base = studio();
    const value = studio({
      data: { ...base.data, videos: [base.data.videos[0]] },
    });
    useStudio.mockReturnValue(value);
    const view = render(<ViralPage />);

    const brokenPoster = view.container.querySelector(
      ".viral-card-cover .video-preview__foreground",
    );
    expect(brokenPoster).not.toBeNull();
    if (!brokenPoster) return;
    fireEvent.error(brokenPoster);
    expect(
      view.container.querySelector(
        ".viral-card-cover .video-preview__foreground",
      ),
    ).toBeNull();

    const refreshed = {
      ...value,
      data: {
        ...value.data,
        videos: [{ ...value.data.videos[0], poster: "/studio/fresh.jpg" }],
      },
    };
    useStudio.mockReturnValue(refreshed);
    view.rerender(<ViralPage />);

    expect(
      view.container.querySelector(
        ".viral-card-cover .video-preview__foreground",
      ),
    ).toHaveAttribute("src", "/studio/fresh.jpg");
  });

  it("真实爆款首次点击即通过服务端媒体地址在原卡片播放", async () => {
    let resolveMedia: ((value: unknown) => void) | undefined;
    fetchViralVideoMedia.mockReturnValue(
      new Promise((resolve) => {
        resolveMedia = resolve;
      }),
    );
    const base = studio();
    const value = studio({
      data: {
        ...base.data,
        videos: [
          {
            ...base.data.videos[0],
            playUrl: "https://source.test/native-dy-1.mp4",
          },
        ],
      },
    });
    useStudio.mockReturnValue(value);
    const view = render(<ViralPage />);

    fireEvent.click(
      screen.getByRole("button", { name: "播放 农村建房预算，别只盯着主体" }),
    );
    expect(fetchViralVideoMedia).toHaveBeenCalledWith(
      "douyin",
      "native-dy-1",
      "video",
    );
    expect(screen.getByText("准备中…")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "播放 农村建房预算，别只盯着主体" }),
    );
    expect(fetchViralVideoMedia).toHaveBeenCalledOnce();

    resolveMedia?.({
      kind: "video",
      url: "https://storage.test/viral/douyin/native-dy-1.mp4",
      contentType: "video/mp4",
      cacheHit: false,
      video: null,
    });
    await waitFor(() => {
      expect(view.container.querySelector("video")).toHaveAttribute(
        "src",
        "https://storage.test/viral/douyin/native-dy-1.mp4",
      );
    });
    expect(view.container.querySelector(".viral-card-overlay")).toBeNull();
  });

  it("新卡片开始播放时暂停上一条并保留原播放器", async () => {
    const pause = vi
      .spyOn(HTMLMediaElement.prototype, "pause")
      .mockImplementation(() => {});
    fetchViralVideoMedia.mockImplementation((_platform, videoId) =>
      Promise.resolve({
        kind: "video",
        url: `https://storage.test/${videoId}.mp4`,
        contentType: "video/mp4",
        cacheHit: false,
        video: null,
      }),
    );
    const base = studio();
    const second = {
      ...base.data.videos[0],
      id: "dy-2",
      nativeId: "native-dy-2",
      title: "第二条乡墅参考",
    };
    useStudio.mockReturnValue(
      studio({ data: { ...base.data, videos: [base.data.videos[0], second] } }),
    );
    render(<ViralPage />);

    fireEvent.click(
      screen.getByRole("button", { name: "播放 农村建房预算，别只盯着主体" }),
    );
    const firstPlayer = await screen.findByTitle("农村建房预算，别只盯着主体");
    fireEvent.click(
      screen.getByRole("button", { name: "播放 第二条乡墅参考" }),
    );
    await screen.findByTitle("第二条乡墅参考");

    expect(pause).toHaveBeenCalledWith();
    expect(firstPlayer).toBeInTheDocument();
    pause.mockRestore();
  });

  it("快速切换卡片时旧请求晚返回不会抢占播放", async () => {
    const resolvers = new Map<string, (value: unknown) => void>();
    fetchViralVideoMedia.mockImplementation(
      (_platform, videoId) =>
        new Promise((resolve) => {
          resolvers.set(videoId, resolve);
        }),
    );
    const base = studio();
    const second = {
      ...base.data.videos[0],
      id: "dy-2",
      nativeId: "native-dy-2",
      title: "第二条乡墅参考",
    };
    useStudio.mockReturnValue(
      studio({ data: { ...base.data, videos: [base.data.videos[0], second] } }),
    );
    render(<ViralPage />);

    fireEvent.click(
      screen.getByRole("button", { name: "播放 农村建房预算，别只盯着主体" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "播放 第二条乡墅参考" }),
    );
    resolvers.get("native-dy-2")?.({
      kind: "video",
      url: "https://storage.test/native-dy-2.mp4",
      contentType: "video/mp4",
      cacheHit: false,
      video: null,
    });
    expect(await screen.findByTitle("第二条乡墅参考")).toHaveAttribute(
      "src",
      "https://storage.test/native-dy-2.mp4",
    );

    resolvers.get("native-dy-1")?.({
      kind: "video",
      url: "https://storage.test/native-dy-1.mp4",
      contentType: "video/mp4",
      cacheHit: false,
      video: null,
    });
    await waitFor(() => {
      expect(screen.queryByTitle("农村建房预算，别只盯着主体")).toBeNull();
    });
  });

  it("媒体响应携带补采统计时只更新对应爆款视频", async () => {
    const responseVideo = {
      platform: "douyin" as const,
      videoId: "native-dy-1",
      category: "建房预算",
      title: "农村建房预算，别只盯着主体",
      author: "乡墅建房笔记",
      authorAvatar: null,
      verified: true,
      coverUrl: "/studio/demo.jpg",
      durationMs: 88_000,
      likes: 19_001,
      comments: 321,
      shares: 654,
      collects: 987,
      publishedAt: null,
      publishedDisplay: null,
      likeDisplay: "1.9万",
      tags: [],
      hasPlayableAudio: true,
      playUrl: null,
    };
    fetchViralVideoMedia.mockResolvedValue({
      kind: "audio",
      url: "https://storage.test/viral/douyin/native-dy-1.mp3",
      contentType: "audio/mpeg",
      cacheHit: false,
      video: responseVideo,
    });
    const base = studio();
    const value = studio({
      state: {
        ...base.state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    fireEvent.click(
      screen.getByRole("button", {
        name: "播放 农村建房预算，别只盯着主体",
      }),
    );
    await waitFor(() => expect(value.updateData).toHaveBeenCalledOnce());
    const update = vi.mocked(value.updateData).mock.calls[0][0];
    const updated = update(value.data);

    expect(updated.videos[0]).toMatchObject({
      likes: 19_001,
      comments: 321,
      shares: 654,
      collections: 987,
      likeDisplay: "1.9万",
    });
    expect(updated.videos[1]).toBe(value.data.videos[1]);
  });

  it("未知爆款统计显示短横线", () => {
    const base = studio();
    useStudio.mockReturnValue(
      studio({
        data: {
          ...base.data,
          videos: [
            {
              ...base.data.videos[0],
              comments: null,
              shares: null,
              collections: null,
            },
          ],
        },
      }),
    );
    render(<ViralPage />);

    expect(screen.getByTitle("评论")).toHaveTextContent("—");
    expect(screen.getByTitle("转发")).toHaveTextContent("—");
    expect(screen.getByTitle("收藏")).toHaveTextContent("—");
  });

  it("非审核列表为当前可见的视频号完整统计查询缓存且每页仅一次", async () => {
    listViralVideos.mockReturnValue(new Promise(() => {}));
    const base = studio();
    const sourceVideo = {
      ...base.data.videos[1],
      comments: 10,
      shares: 20,
      collections: 30,
    };
    fetchViralVideoStatistics.mockResolvedValue({
      items: [
        {
          platform: "wechat_channels",
          videoId: "native-wx-1",
          category: "庭院案例",
          title: sourceVideo.title,
          author: sourceVideo.author,
          authorAvatar: null,
          verified: false,
          coverUrl: sourceVideo.poster,
          durationMs: 72_000,
          likes: 9_800,
          comments: 44,
          shares: 55,
          collects: 66,
          publishedAt: null,
          publishedDisplay: "3天前",
          likeDisplay: "1.2万",
          tags: [],
          hasPlayableAudio: false,
          playUrl: null,
        },
      ],
    });
    const value = studio({
      review: false,
      data: { ...base.data, videos: [sourceVideo] },
    });
    useStudio.mockReturnValue(value);
    const view = render(<ViralPage />);
    fireEvent.click(screen.getByRole("tab", { name: "视频号 1" }));

    await waitFor(() =>
      expect(fetchViralVideoStatistics).toHaveBeenCalledWith(["native-wx-1"]),
    );
    view.rerender(<ViralPage />);
    expect(fetchViralVideoStatistics).toHaveBeenCalledOnce();
    const update = vi.mocked(value.updateData).mock.calls[0][0];
    expect(update(value.data).videos[0]).toMatchObject({
      comments: 44,
      shares: 55,
      collections: 66,
    });
  });

  it("视频号详情直接打开时也查询当前完整统计缓存", async () => {
    const base = studio();
    fetchViralVideoStatistics.mockResolvedValue({ items: [] });
    const value = studio({
      review: false,
      state: {
        ...base.state,
        page: "viral-detail",
        selectedVideoId: "wx-1",
      },
    });
    useStudio.mockReturnValue(value);
    render(<ViralDetailPage />);

    await waitFor(() =>
      expect(fetchViralVideoStatistics).toHaveBeenCalledWith(["native-wx-1"]),
    );
  });

  it("爆款列表默认渲染前十二条并保留平台筛选", () => {
    const base = studio();
    const videos = Array.from({ length: 7 }, (_, index) => ({
      ...base.data.videos[0],
      id: `dy-${index + 1}`,
      title: `乡墅参考 ${index + 1}`,
    }));
    useStudio.mockReturnValue(studio({ data: { ...base.data, videos } }));
    render(<ViralPage />);
    expect(screen.getByText("乡墅参考 7")).toBeInTheDocument();
    expect(screen.getByText("乡墅参考 1")).toBeInTheDocument();
    expect(screen.queryByText("上拉加载更多…")).toBeNull();
  });

  it("爆款超出十二条时先渲染十二条并提供滚动加载占位", () => {
    const base = studio();
    const videos = Array.from({ length: 15 }, (_, index) => ({
      ...base.data.videos[0],
      id: `dy-${index + 1}`,
      title: `乡墅参考 ${index + 1}`,
    }));
    useStudio.mockReturnValue(studio({ data: { ...base.data, videos } }));
    render(<ViralPage />);
    expect(screen.getByText("乡墅参考 12")).toBeInTheDocument();
    expect(screen.queryByText("乡墅参考 13")).toBeNull();
    expect(screen.getByText("上拉加载更多…")).toBeInTheDocument();
  });

  it("爆款视频按热度或发布时间排序", () => {
    const base = studio();
    const videos = Array.from({ length: 7 }, (_, index) => ({
      ...base.data.videos[0],
      id: `dy-${index + 1}`,
      title: `排序参考 ${index + 1}`,
      likes: (index + 1) * 100,
      publishedAt: 1788600000 - index,
    }));
    useStudio.mockReturnValue(studio({ data: { ...base.data, videos } }));
    const view = render(<ViralPage />);

    const titles = () =>
      [...view.container.querySelectorAll(".viral-card-body h3")].map(
        (node) => node.textContent,
      );
    expect(titles()[0]).toBe("排序参考 7");
    fireEvent.change(screen.getByLabelText("排序方式"), {
      target: { value: "最新" },
    });
    expect(titles()[0]).toBe("排序参考 1");
  });

  it("切换最新排序时按当前平台重新读取列表", async () => {
    const value = studio({ review: false });
    useStudio.mockReturnValue(value);
    render(<ViralPage />);

    fireEvent.change(screen.getByLabelText("排序方式"), {
      target: { value: "最新" },
    });

    await waitFor(() =>
      expect(listViralVideos).toHaveBeenCalledWith("douyin", "latest", {
        limit: 12,
      }),
    );
  });

  it("非审核列表使用服务端游标分页，并提供可键盘触发的加载入口", async () => {
    const base = studio();
    const item = {
      platform: "douyin" as const,
      videoId: "native-dy-1",
      category: "建房预算",
      title: "农村建房预算，别只盯着主体",
      author: "乡墅建房笔记",
      authorAvatar: null,
      verified: true,
      coverUrl: "/studio/demo.jpg",
      durationMs: 88_000,
      likes: 18_000,
      comments: 10,
      shares: 20,
      collects: 30,
      publishedAt: 1_788_600_000,
      publishedDisplay: null,
      likeDisplay: "1.8万",
      tags: [],
      hasPlayableAudio: true,
      playUrl: null,
      isFavorite: false,
      availability: "available" as const,
    };
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: [item],
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "cursor-2",
        total: 13,
      })
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: [{ ...item, videoId: "native-dy-2", title: "第二页视频" }],
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: false,
        nextCursor: null,
        total: 13,
      });
    useStudio.mockReturnValue(
      studio({ review: false, data: { ...base.data, videos: [] } }),
    );
    render(<ViralPage />);

    await waitFor(() =>
      expect(listViralVideos).toHaveBeenCalledWith("douyin", "hot", {
        limit: 12,
      }),
    );
    const loadMore = await screen.findByRole("button", {
      name: "加载更多视频",
    });
    fireEvent.keyDown(loadMore, { key: "Enter" });
    await waitFor(() =>
      expect(listViralVideos).toHaveBeenCalledWith("douyin", "hot", {
        limit: 12,
        cursor: "cursor-2",
      }),
    );
  });

  it("连续加载至少三十条并去除跨页重复项", async () => {
    const base = studio({ review: false });
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: Array.from({ length: 12 }, (_, index) => viralItem(index + 1)),
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "cursor-2",
        total: 31,
      })
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: [
          viralItem(12),
          ...Array.from({ length: 11 }, (_, index) => viralItem(index + 13)),
        ],
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "cursor-3",
        total: 31,
      })
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: Array.from({ length: 8 }, (_, index) => viralItem(index + 24)),
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: false,
        nextCursor: null,
        total: 31,
      });

    render(
      <StatefulViralPage
        value={{ ...base, data: { ...base.data, videos: [] } }}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "加载更多视频" }),
    );
    expect(await screen.findByText("乡墅参考 23")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "加载更多视频" }));

    expect(await screen.findByText("乡墅参考 31")).toBeInTheDocument();
    expect(screen.getAllByText("乡墅参考 12")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "加载更多视频" })).toBeNull();
  });

  it("分页请求在途时忽略连续触发，失败后保留同一游标供重试", async () => {
    const base = studio({ review: false });
    let resolvePage:
      | ((value: Awaited<ReturnType<typeof listViralVideos>>) => void)
      | undefined;
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: Array.from({ length: 12 }, (_, index) => viralItem(index + 1)),
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "cursor-2",
        total: 13,
      })
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolvePage = resolve;
        }),
      )
      .mockRejectedValueOnce(new Error("temporary failure"))
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: [viralItem(13)],
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: false,
        nextCursor: null,
        total: 13,
      });
    render(
      <StatefulViralPage
        value={{ ...base, data: { ...base.data, videos: [] } }}
      />,
    );

    const loadMore = await screen.findByRole("button", {
      name: "加载更多视频",
    });
    fireEvent.click(loadMore);
    fireEvent.keyDown(loadMore, { key: "Enter" });
    expect(listViralVideos).toHaveBeenCalledTimes(2);

    resolvePage?.({
      platform: "douyin",
      sort: "hot",
      categories: ["建房预算"],
      items: [],
      fetchedAt: "2026-09-07T12:00:00Z",
      hasMore: true,
      nextCursor: "cursor-2",
      total: 13,
    });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "加载更多视频" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "加载更多视频" }));
    expect(await screen.findByText("加载更多失败，请重试")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "加载更多视频" }));

    expect(await screen.findByText("乡墅参考 13")).toBeInTheDocument();
    expect(listViralVideos).toHaveBeenLastCalledWith("douyin", "hot", {
      limit: 12,
      cursor: "cursor-2",
    });
  });

  it("游标失效时清除当前上下文快照并自动恢复首屏", async () => {
    const base = studio({ review: false });
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: Array.from({ length: 12 }, (_, index) => viralItem(index + 1)),
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "expired-cursor",
        total: 13,
      })
      .mockRejectedValueOnce(
        Object.assign(new Error("分页游标无效，请刷新列表"), {
          code: "VIRAL_CURSOR_INVALID",
        }),
      )
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: [viralItem(31, { title: "恢复后的首屏" })],
        fetchedAt: "2026-09-07T12:01:00Z",
        hasMore: false,
        nextCursor: null,
        total: 1,
      });
    render(
      <StatefulViralPage
        value={{ ...base, data: { ...base.data, videos: [] } }}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "加载更多视频" }),
    );

    expect(await screen.findByText("恢复后的首屏")).toBeInTheDocument();
    expect(listViralVideos).toHaveBeenNthCalledWith(2, "douyin", "hot", {
      limit: 12,
      cursor: "expired-cursor",
    });
    expect(listViralVideos).toHaveBeenNthCalledWith(3, "douyin", "hot", {
      limit: 12,
    });
    expect(listViralVideos).toHaveBeenCalledTimes(3);
  });

  it("切换分类会从首屏重置分页并丢弃旧分类的迟到结果", async () => {
    const base = studio({ review: false });
    let resolveOldPage:
      | ((value: Awaited<ReturnType<typeof listViralVideos>>) => void)
      | undefined;
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算", "庭院案例"],
        items: Array.from({ length: 12 }, (_, index) => viralItem(index + 1)),
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "cursor-2",
        total: 13,
      })
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveOldPage = resolve;
        }),
      )
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算", "庭院案例"],
        items: [viralItem(21, { category: "庭院案例" })],
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: false,
        nextCursor: null,
        total: 13,
      });
    render(
      <StatefulViralPage
        value={{ ...base, data: { ...base.data, videos: [] } }}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "加载更多视频" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "庭院案例" }));

    await waitFor(() =>
      expect(listViralVideos).toHaveBeenLastCalledWith("douyin", "hot", {
        limit: 12,
      }),
    );
    resolveOldPage?.({
      platform: "douyin",
      sort: "hot",
      categories: ["建房预算", "庭院案例"],
      items: [viralItem(99, { category: "庭院案例" })],
      fetchedAt: "2026-09-07T12:00:00Z",
      hasMore: false,
      nextCursor: null,
      total: 13,
    });

    expect(await screen.findByText("乡墅参考 21")).toBeInTheDocument();
    expect(screen.queryByText("乡墅参考 99")).toBeNull();
  });

  it("本地搜索保留已加载第二页且不发起新网络请求", async () => {
    const base = studio({ review: false });
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: Array.from({ length: 12 }, (_, index) => viralItem(index + 1)),
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "cursor-2",
        total: 13,
      })
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: [viralItem(13, { title: "只在第二页的庭院案例" })],
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: false,
        nextCursor: null,
        total: 13,
      });
    render(
      <StatefulViralPage
        value={{ ...base, data: { ...base.data, videos: [] } }}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "加载更多视频" }),
    );
    expect(await screen.findByText("只在第二页的庭院案例")).toBeInTheDocument();
    expect(listViralVideos).toHaveBeenCalledTimes(2);

    fireEvent.change(screen.getByRole("textbox", { name: "搜索视频标题" }), {
      target: { value: "庭院" },
    });

    expect(screen.getByText("只在第二页的庭院案例")).toBeInTheDocument();
    expect(listViralVideos).toHaveBeenCalledTimes(2);
  });

  it("清空本地搜索后恢复已确认游标并继续下一页", async () => {
    const base = studio({ review: false });
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: Array.from({ length: 12 }, (_, index) => viralItem(index + 1)),
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "cursor-2",
        total: 25,
      })
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: [
          viralItem(12),
          ...Array.from({ length: 11 }, (_, index) =>
            viralItem(index + 13, {
              title: index === 0 ? "第二页庭院案例" : `乡墅参考 ${index + 13}`,
            }),
          ),
        ],
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "cursor-3",
        total: 25,
      })
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: [viralItem(24), viralItem(25)],
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: false,
        nextCursor: null,
        total: 25,
      });
    render(
      <StatefulViralPage
        value={{ ...base, data: { ...base.data, videos: [] } }}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "加载更多视频" }),
    );
    expect(await screen.findByText("第二页庭院案例")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "搜索视频标题" }), {
      target: { value: "庭院" },
    });
    expect(screen.getByText("第二页庭院案例")).toBeInTheDocument();
    expect(listViralVideos).toHaveBeenCalledTimes(2);
    fireEvent.change(screen.getByRole("textbox", { name: "搜索视频标题" }), {
      target: { value: "参考" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "搜索视频标题" }), {
      target: { value: "庭院" },
    });
    expect(screen.getByText("第二页庭院案例")).toBeInTheDocument();
    expect(listViralVideos).toHaveBeenCalledTimes(2);

    fireEvent.change(screen.getByRole("textbox", { name: "搜索视频标题" }), {
      target: { value: "" },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: "加载更多视频" }),
    );

    expect(await screen.findByText("乡墅参考 25")).toBeInTheDocument();
    expect(listViralVideos).toHaveBeenLastCalledWith("douyin", "hot", {
      limit: 12,
      cursor: "cursor-3",
    });
    expect(screen.getAllByText("乡墅参考 12")).toHaveLength(1);
  });

  it("搜索期间切换平台只恢复新平台的已确认游标", async () => {
    const base = studio({ review: false });
    fetchViralVideoStatistics.mockResolvedValue({ items: [] });
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: Array.from({ length: 12 }, (_, index) => viralItem(index + 1)),
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "douyin-cursor-2",
        total: 24,
      })
      .mockResolvedValueOnce({
        platform: "wechat_channels",
        sort: "hot",
        categories: ["庭院案例"],
        items: [
          viralItem(41, {
            platform: "wechat_channels",
            category: "庭院案例",
            title: "视频号庭院首屏",
          }),
        ],
        fetchedAt: "2026-09-07T12:00:01Z",
        hasMore: true,
        nextCursor: "wechat-cursor-2",
        total: 2,
      })
      .mockResolvedValueOnce({
        platform: "wechat_channels",
        sort: "hot",
        categories: ["庭院案例"],
        items: [
          viralItem(42, {
            platform: "wechat_channels",
            category: "庭院案例",
            title: "视频号庭院第二页",
          }),
        ],
        fetchedAt: "2026-09-07T12:00:01Z",
        hasMore: false,
        nextCursor: null,
        total: 2,
      });
    render(
      <StatefulViralPage
        value={{ ...base, data: { ...base.data, videos: [] } }}
      />,
    );

    await screen.findByRole("button", { name: "加载更多视频" });
    fireEvent.change(screen.getByRole("textbox", { name: "搜索视频标题" }), {
      target: { value: "庭院" },
    });
    fireEvent.click(screen.getByRole("tab", { name: /视频号/ }));
    expect(await screen.findByText("视频号庭院首屏")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "搜索视频标题" }), {
      target: { value: "" },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: "加载更多视频" }),
    );

    expect(await screen.findByText("视频号庭院第二页")).toBeInTheDocument();
    expect(listViralVideos).toHaveBeenLastCalledWith("wechat_channels", "hot", {
      limit: 12,
      cursor: "wechat-cursor-2",
    });
    expect(listViralVideos).not.toHaveBeenCalledWith("douyin", "hot", {
      limit: 12,
      cursor: "douyin-cursor-2",
    });
  });

  it("修改本地搜索词会丢弃挂起旧页且不继续旧游标", async () => {
    const base = studio({ review: false });
    let resolveOldPage:
      | ((value: Awaited<ReturnType<typeof listViralVideos>>) => void)
      | undefined;
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: Array.from({ length: 12 }, (_, index) => viralItem(index + 1)),
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "cursor-2",
        total: 13,
      })
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveOldPage = resolve;
        }),
      );
    render(
      <StatefulViralPage
        value={{ ...base, data: { ...base.data, videos: [] } }}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "加载更多视频" }),
    );
    fireEvent.change(screen.getByRole("textbox", { name: "搜索视频标题" }), {
      target: { value: "庭院" },
    });
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "正在加载…" })).toBeNull(),
    );
    resolveOldPage?.({
      platform: "douyin",
      sort: "hot",
      categories: ["建房预算"],
      items: [viralItem(99, { title: "庭院旧页" })],
      fetchedAt: "2026-09-07T12:00:00Z",
      hasMore: true,
      nextCursor: "cursor-3",
      total: 13,
    });
    await Promise.resolve();

    expect(screen.queryByText("庭院旧页")).toBeNull();
    expect(screen.queryByRole("button", { name: "加载更多视频" })).toBeNull();
    expect(listViralVideos).toHaveBeenCalledTimes(2);
    expect(listViralVideos).not.toHaveBeenCalledWith("douyin", "hot", {
      limit: 12,
      cursor: "cursor-3",
    });
  });

  it("切换平台后丢弃上一平台迟到的分页结果", async () => {
    const base = studio();
    const updateData = vi.fn();
    const item = {
      platform: "douyin" as const,
      videoId: "native-dy-1",
      category: "建房预算",
      title: "抖音首页视频",
      author: "作者",
      authorAvatar: null,
      verified: false,
      coverUrl: "/studio/demo.jpg",
      durationMs: 30_000,
      likes: 100,
      comments: 1,
      shares: 2,
      collects: 3,
      publishedAt: 1_788_600_000,
      publishedDisplay: null,
      likeDisplay: "100",
      tags: [],
      hasPlayableAudio: true,
      playUrl: null,
      isFavorite: false,
      availability: "available" as const,
    };
    let resolveOldPage:
      | ((value: {
          platform: "douyin";
          sort: "hot";
          categories: string[];
          items: Array<typeof item>;
          fetchedAt: string;
          hasMore: boolean;
          nextCursor: null;
          total: number;
        }) => void)
      | undefined;
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: ["建房预算"],
        items: [item],
        fetchedAt: "2026-09-07T12:00:00Z",
        hasMore: true,
        nextCursor: "cursor-2",
        total: 2,
      })
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveOldPage = resolve;
        }),
      )
      .mockResolvedValueOnce({
        platform: "wechat_channels",
        sort: "hot",
        categories: [],
        items: [],
        fetchedAt: "2026-09-07T12:00:01Z",
        hasMore: false,
        nextCursor: null,
        total: 0,
      });
    useStudio.mockReturnValue(
      studio({
        review: false,
        data: { ...base.data, videos: [] },
        updateData,
      }),
    );
    render(<ViralPage />);

    const loadMore = await screen.findByRole("button", {
      name: "加载更多视频",
    });
    updateData.mockClear();
    fireEvent.click(loadMore);
    fireEvent.click(screen.getByRole("tab", { name: /视频号/ }));
    await waitFor(() =>
      expect(listViralVideos).toHaveBeenCalledWith("wechat_channels", "hot", {
        limit: 12,
      }),
    );

    resolveOldPage?.({
      platform: "douyin",
      sort: "hot",
      categories: ["建房预算"],
      items: [{ ...item, videoId: "native-dy-late", title: "迟到的抖音视频" }],
      fetchedAt: "2026-09-07T12:00:00Z",
      hasMore: false,
      nextCursor: null,
      total: 2,
    });
    await Promise.resolve();

    expect(updateData).toHaveBeenCalledTimes(1);
  });

  it("冷库刷新时保持页面可用并显示采集状态", async () => {
    const base = studio();
    listViralVideos.mockResolvedValue({
      platform: "douyin",
      sort: "hot",
      categories: [],
      items: [],
      fetchedAt: null,
      stale: true,
      refreshing: true,
      refreshError: null,
      hasMore: false,
      nextCursor: null,
      total: 0,
    });
    useStudio.mockReturnValue(
      studio({ review: false, data: { ...base.data, videos: [] } }),
    );

    render(<ViralPage />);

    expect(
      await screen.findAllByText("正在采集爆款视频，当前先展示已缓存内容…"),
    ).not.toHaveLength(0);
    expect(screen.getByText("暂无爆款视频")).toBeInTheDocument();
  });

  it("全部列表首屏在搜索变化后迟到失败不污染当前页面", async () => {
    const base = studio({ review: false });
    useStudio.mockReturnValue(base);
    let rejectOldRequest: ((reason: Error) => void) | undefined;
    listViralVideos.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectOldRequest = reject;
      }),
    );
    render(<ViralPage />);

    fireEvent.change(screen.getByRole("textbox", { name: "搜索视频标题" }), {
      target: { value: "建房" },
    });
    await act(async () => {
      rejectOldRequest?.(new Error("old all request failed"));
      await Promise.resolve();
    });

    expect(screen.getByText(base.data.videos[0].title)).toBeInTheDocument();
    expect(
      screen.queryByText("视频列表暂时无法更新，已保留当前内容"),
    ).toBeNull();
  });

  it("收藏首屏在搜索变化后迟到失败不污染当前页面", async () => {
    const base = studio({ review: false });
    useStudio.mockReturnValue(base);
    let rejectOldRequest: ((reason: Error) => void) | undefined;
    listViralFavorites.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectOldRequest = reject;
      }),
    );
    render(<ViralPage />);
    fireEvent.click(screen.getByRole("tab", { name: "我的收藏" }));
    await waitFor(() => expect(listViralFavorites).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByRole("textbox", { name: "搜索视频标题" }), {
      target: { value: "庭院" },
    });
    await act(async () => {
      rejectOldRequest?.(new Error("old favorite request failed"));
      await Promise.resolve();
    });

    expect(screen.queryByText("收藏列表暂时无法更新")).toBeNull();
  });

  it("切换账号后立即清除旧收藏并忽略上一账号的迟到响应", async () => {
    let current = studio({
      review: false,
      user: { id: "user-a" } as StudioContextValue["user"],
    });
    useStudio.mockImplementation(() => current);
    let resolveUserA!: (value: {
      platform: "douyin";
      sort: "hot";
      categories: string[];
      items: ViralVideoItem[];
      fetchedAt: null;
      hasMore: false;
      nextCursor: null;
      total: number;
    }) => void;
    listViralVideos.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveUserA = resolve;
      }),
    );
    const view = render(<ViralPage />);

    current = {
      ...current,
      user: { id: "user-b" } as StudioContextValue["user"],
    };
    listViralVideos.mockResolvedValueOnce({
      platform: "douyin",
      sort: "hot",
      categories: [],
      items: [viralItem(1, { isFavorite: false })],
      fetchedAt: null,
      hasMore: false,
      nextCursor: null,
      total: 1,
    });
    view.rerender(<ViralPage />);

    await waitFor(() => expect(listViralVideos).toHaveBeenCalledTimes(2));
    resolveUserA({
      platform: "douyin",
      sort: "hot",
      categories: [],
      items: [viralItem(1, { isFavorite: true })],
      fetchedAt: null,
      hasMore: false,
      nextCursor: null,
      total: 1,
    });
    await act(async () => Promise.resolve());

    expect(
      screen.getByRole("button", {
        name: "收藏 农村建房预算，别只盯着主体",
      }),
    ).toHaveAttribute("aria-pressed", "false");
  });

  it.each([
    ["全部", "成功"],
    ["全部", "失败"],
    ["收藏", "成功"],
    ["收藏", "失败"],
  ] as const)("%s深页在切换账号后忽略迟到%s", async (scope, outcome) => {
    let resolvePage!: (
      value: Awaited<ReturnType<typeof listViralVideos>>,
    ) => void;
    let rejectPage!: (reason: Error) => void;
    const deferred = new Promise<Awaited<ReturnType<typeof listViralVideos>>>(
      (resolve, reject) => {
        resolvePage = resolve;
        rejectPage = reject;
      },
    );
    const firstPage = {
      platform: "douyin" as const,
      sort: "hot" as const,
      categories: [],
      items: [viralItem(1, { isFavorite: scope === "收藏" })],
      fetchedAt: null,
      hasMore: true,
      nextCursor: "cursor-2",
      total: 2,
    };
    if (scope === "全部") {
      listViralVideos
        .mockResolvedValueOnce(firstPage)
        .mockReturnValueOnce(deferred)
        .mockReturnValueOnce(new Promise(() => undefined));
    } else {
      listViralFavorites
        .mockResolvedValueOnce(firstPage)
        .mockReturnValueOnce(deferred)
        .mockReturnValueOnce(new Promise(() => undefined));
    }
    const updateData = vi.fn();
    let current = studio({
      review: false,
      user: { id: "user-a" } as StudioContextValue["user"],
      updateData,
    });
    useStudio.mockImplementation(() => current);
    const view = render(<RenderWindowTrigger />);
    if (scope === "收藏") {
      fireEvent.click(screen.getByRole("tab", { name: "我的收藏" }));
    }
    fireEvent.click(
      await screen.findByRole("button", { name: "加载更多视频" }),
    );
    updateData.mockClear();

    current = {
      ...current,
      user: { id: "user-b" } as StudioContextValue["user"],
    };
    const settle = () => {
      if (outcome === "成功") {
        resolvePage({
          ...firstPage,
          items: [viralItem(2, { title: "旧账号迟到深页" })],
          hasMore: false,
          nextCursor: null,
        });
      } else {
        rejectPage(new Error("旧账号深页失败"));
      }
    };
    view.rerender(<RenderWindowTrigger onLayout={settle} />);
    await act(async () => Promise.resolve());

    expect(updateData).not.toHaveBeenCalled();
    expect(screen.queryByText("旧账号迟到深页")).toBeNull();
    expect(screen.queryByText("加载更多失败，请重试")).toBeNull();
  });

  it("新账号首屏尚未启动时不能沿用旧账号游标加载深页", async () => {
    listViralVideos
      .mockResolvedValueOnce({
        platform: "douyin",
        sort: "hot",
        categories: [],
        items: [viralItem(1)],
        fetchedAt: null,
        hasMore: true,
        nextCursor: "user-a-cursor",
        total: 2,
      })
      .mockReturnValueOnce(new Promise(() => undefined));
    let current = studio({
      review: false,
      user: { id: "user-a" } as StudioContextValue["user"],
    });
    useStudio.mockImplementation(() => current);
    const view = render(<RenderWindowTrigger />);
    await screen.findByRole("button", { name: "加载更多视频" });

    current = {
      ...current,
      user: { id: "user-b" } as StudioContextValue["user"],
    };
    view.rerender(
      <RenderWindowTrigger
        onLayout={() =>
          screen.getByRole("button", { name: "加载更多视频" }).click()
        }
      />,
    );

    expect(
      listViralVideos.mock.calls.filter(([, , options]) => options?.cursor),
    ).toHaveLength(0);
  });

  it("收藏使用持久化接口并在保存失败时回滚乐观状态", async () => {
    saveViralFavorite.mockRejectedValueOnce(new Error("收藏失败"));
    const value = studio({ review: false });
    useStudio.mockReturnValue(value);
    render(<ViralPage />);

    const button = screen.getByRole("button", {
      name: "收藏 农村建房预算，别只盯着主体",
    });
    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-pressed", "true");
    await waitFor(() =>
      expect(button).toHaveAttribute("aria-pressed", "false"),
    );
    expect(saveViralFavorite).toHaveBeenCalledWith("douyin", "native-dy-1");
    expect(value.notify).toHaveBeenCalledWith("收藏失败，已恢复原状态");
  });

  it("取消收藏失败时恢复收藏状态且请求未完成期间不重复提交", async () => {
    let rejectRemoval!: (reason: Error) => void;
    removeViralFavorite.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectRemoval = reject;
      }),
    );
    listViralVideos.mockResolvedValueOnce({
      platform: "douyin",
      sort: "hot",
      categories: [],
      items: [viralItem(1, { isFavorite: true })],
      fetchedAt: null,
      hasMore: false,
      nextCursor: null,
      total: 1,
    });
    const value = studio({ review: false });
    useStudio.mockReturnValue(value);
    render(<ViralPage />);

    const button = screen.getByRole("button", {
      name: "收藏 农村建房预算，别只盯着主体",
    });
    await waitFor(() => expect(button).toHaveAttribute("aria-pressed", "true"));
    fireEvent.click(button);
    fireEvent.click(button);
    expect(removeViralFavorite).toHaveBeenCalledTimes(1);
    expect(button).toHaveAttribute("aria-pressed", "false");

    rejectRemoval(new Error("取消失败"));
    await waitFor(() => expect(button).toHaveAttribute("aria-pressed", "true"));
    expect(value.notify).toHaveBeenCalledWith("收藏失败，已恢复原状态");
  });

  it("账号渲染完成但被动 effect 尚未执行时仍回滚新账号收藏失败", async () => {
    saveViralFavorite.mockImplementationOnce(() => {
      throw new Error("新账号收藏失败");
    });
    let current = studio({
      review: false,
      user: { id: "user-a" } as StudioContextValue["user"],
    });
    useStudio.mockImplementation(() => current);
    const view = render(<RenderWindowTrigger />);

    vi.mocked(current.patchState).mockClear();
    vi.mocked(current.notify).mockClear();
    current = {
      ...current,
      user: { id: "user-b" } as StudioContextValue["user"],
    };
    view.rerender(
      <RenderWindowTrigger
        onLayout={() =>
          screen
            .getByRole("button", {
              name: "收藏 农村建房预算，别只盯着主体",
            })
            .click()
        }
      />,
    );
    await act(async () => Promise.resolve());

    expect(
      screen.getByRole("button", {
        name: "收藏 农村建房预算，别只盯着主体",
      }),
    ).toHaveAttribute("aria-pressed", "false");
    expect(current.notify).toHaveBeenCalledWith("收藏失败，已恢复原状态");
  });

  it("A到B再回A时旧收藏结算保持静默且不会永久禁用", async () => {
    let rejectUserA!: (reason: Error) => void;
    saveViralFavorite.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectUserA = reject;
      }),
    );
    const userA = studio({
      review: false,
      user: { id: "user-a" } as StudioContextValue["user"],
    });
    let current = userA;
    useStudio.mockImplementation(() => current);
    const onSavedChange = vi.fn();
    const view = render(
      <FavoriteRenderWindowTrigger onSavedChange={onSavedChange} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /收藏/ }));
    expect(saveViralFavorite).toHaveBeenCalledOnce();

    onSavedChange.mockClear();
    vi.mocked(userA.patchState).mockClear();
    vi.mocked(userA.notify).mockClear();
    current = {
      ...userA,
      user: { id: "user-b" } as StudioContextValue["user"],
    };
    view.rerender(
      <FavoriteRenderWindowTrigger onSavedChange={onSavedChange} />,
    );
    current = userA;
    view.rerender(
      <FavoriteRenderWindowTrigger
        onSavedChange={onSavedChange}
        onLayout={() => rejectUserA(new Error("用户 A 的旧请求失败"))}
      />,
    );
    await act(async () => Promise.resolve());

    expect(screen.getByRole("button", { name: /收藏/ })).toBeEnabled();
    expect(onSavedChange).not.toHaveBeenCalled();
    expect(userA.patchState).not.toHaveBeenCalled();
    expect(userA.notify).not.toHaveBeenCalled();
  });

  it("收藏按钮卸载后旧请求结算不再回滚或通知", async () => {
    let rejectRequest!: (reason: Error) => void;
    saveViralFavorite.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectRequest = reject;
      }),
    );
    const value = studio({ review: false });
    useStudio.mockReturnValue(value);
    const onSavedChange = vi.fn();
    const view = render(
      <FavoriteRenderWindowTrigger onSavedChange={onSavedChange} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /收藏/ }));
    onSavedChange.mockClear();
    vi.mocked(value.patchState).mockClear();
    vi.mocked(value.notify).mockClear();

    view.unmount();
    rejectRequest(new Error("卸载后的旧请求失败"));
    await act(async () => Promise.resolve());

    expect(onSavedChange).not.toHaveBeenCalled();
    expect(value.patchState).not.toHaveBeenCalled();
    expect(value.notify).not.toHaveBeenCalled();
  });

  it("详情地址保留平台和视频 ID，刷新后可从单条接口恢复", async () => {
    const base = studio();
    const item = {
      platform: "douyin" as const,
      videoId: "native-dy-1",
      category: "建房预算",
      title: "刷新恢复的视频",
      author: "乡墅建房笔记",
      authorAvatar: null,
      verified: true,
      coverUrl: "/studio/demo.jpg",
      durationMs: 88_000,
      likes: 18_000,
      comments: 10,
      shares: 20,
      collects: 30,
      publishedAt: 1_788_600_000,
      publishedDisplay: null,
      likeDisplay: "1.8万",
      tags: [],
      hasPlayableAudio: true,
      playUrl: null,
      isFavorite: false,
      availability: "available" as const,
    };
    fetchViralVideo.mockResolvedValue({ item });
    window.history.replaceState(
      null,
      "",
      "/?viralPlatform=douyin&viralVideoId=native-dy-1#studio/viral-detail",
    );
    useStudio.mockReturnValue(
      studio({
        review: false,
        data: { ...base.data, videos: [] },
        state: { ...base.state, page: "viral-detail" },
      }),
    );
    render(<ViralDetailPage />);

    expect(screen.getByText("正在读取视频详情…")).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "刷新恢复的视频" }),
    ).toBeInTheDocument();
    expect(fetchViralVideo).toHaveBeenCalledWith("douyin", "native-dy-1");
  });

  it("详情页切换账号后重新读取当前账号的收藏状态", async () => {
    window.history.replaceState(
      null,
      "",
      "/?viralPlatform=douyin&viralVideoId=native-dy-1#studio/viral-detail",
    );
    const initial = studio();
    let current = studio({
      review: false,
      user: { id: "user-a" } as StudioContextValue["user"],
      state: {
        ...initial.state,
        page: "viral-detail",
        selectedVideoId: "dy-1",
      },
    });
    useStudio.mockImplementation(() => current);
    fetchViralVideo
      .mockResolvedValueOnce({
        item: viralItem(1, {
          title: "农村建房预算，别只盯着主体",
          isFavorite: true,
        }),
      })
      .mockResolvedValueOnce({
        item: viralItem(1, {
          title: "农村建房预算，别只盯着主体",
          isFavorite: false,
        }),
      });
    const view = render(<ViralDetailPage />);

    const button = screen.getByRole("button", {
      name: "收藏 农村建房预算，别只盯着主体",
    });
    await waitFor(() => expect(button).toHaveAttribute("aria-pressed", "true"));
    current = {
      ...current,
      user: { id: "user-b" } as StudioContextValue["user"],
    };
    view.rerender(<ViralDetailPage />);

    await waitFor(() => expect(fetchViralVideo).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(button).toHaveAttribute("aria-pressed", "false"),
    );
  });

  it("详情页账号已渲染时同步拒绝旧账号迟到成功", async () => {
    window.history.replaceState(
      null,
      "",
      "/?viralPlatform=douyin&viralVideoId=native-dy-1#studio/viral-detail",
    );
    let resolveUserA!: (value: { item: ViralVideoItem }) => void;
    fetchViralVideo
      .mockReturnValueOnce({
        // biome-ignore lint/suspicious/noThenProperty: 同步 thenable 用于复现 render 到 passive effect 之间的响应窗口。
        then(resolve: (value: { item: ViralVideoItem }) => void) {
          resolveUserA = resolve;
          return { catch: () => undefined };
        },
      })
      .mockReturnValueOnce(new Promise(() => undefined));
    const initial = studio();
    let current = studio({
      review: false,
      user: { id: "user-a" } as StudioContextValue["user"],
      data: { ...initial.data, videos: [] },
      state: { ...initial.state, page: "viral-detail" },
    });
    useStudio.mockImplementation(() => current);
    const view = render(<DetailRenderWindowTrigger />);
    await waitFor(() => expect(fetchViralVideo).toHaveBeenCalledTimes(1));

    vi.mocked(current.updateData).mockClear();
    current = {
      ...current,
      user: { id: "user-b" } as StudioContextValue["user"],
    };
    view.rerender(
      <DetailRenderWindowTrigger
        onLayout={() =>
          resolveUserA({
            item: viralItem(1, { title: "用户 A 的迟到详情" }),
          })
        }
      />,
    );

    expect(current.updateData).not.toHaveBeenCalled();
    expect(screen.queryByText("用户 A 的迟到详情")).toBeNull();
  });

  it("非审核工作区不把示例三十条当作真实采集数据", () => {
    useStudio.mockReturnValue(
      studio({ review: false, data: { ...studio().data, videos: [] } }),
    );
    render(<ViralPage />);
    expect(screen.getByRole("tab", { name: "抖音 0" })).toBeInTheDocument();
    expect(screen.getByText("暂无爆款视频")).toBeInTheDocument();
    expect(
      screen.getByText("数据源尚未配置或最近 7 天暂无内容，配置后自动展示。"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("采集参数仅在管理后台配置。"),
    ).not.toBeInTheDocument();
  });

  it("素材页用统一选择器，音频仅交给音频口播并带 IP", () => {
    const value = studio();
    useStudio.mockReturnValue(value);
    const view = render(<MaterialsPage />);
    fireEvent.click(
      screen.getByRole("button", { name: "选择素材 张工讲预算.wav" }),
    );
    expect(value.patchState).toHaveBeenCalledWith({
      selectedAssetId: "audio-1",
    });
    const selectedValue = studio({
      state: { ...value.state, selectedAssetId: "audio-1" },
    });
    useStudio.mockReturnValue(selectedValue);
    view.rerender(<MaterialsPage />);
    fireEvent.click(screen.getByRole("button", { name: "用于音频口播" }));
    expect(selectedValue.patchDraft).toHaveBeenCalledWith({
      audioId: "audio-1",
      ipId: "person-1",
      voiceId: undefined,
    });
    expect(selectedValue.navigate).toHaveBeenCalledWith("oral-audio", {
      returnTo: "materials",
    });
  });

  it("素材图片固定在卡片缩略图区并保留完整名称与状态", () => {
    const value = studio();
    value.state = { ...value.state, page: "materials" };
    value.data.assets = [
      {
        id: "portrait-1",
        name: "乡墅工程师施工现场人物竖版场景形象图.png",
        kind: "image",
        url: "/portrait.png",
        group: "场景形象照",
        source: "人物库",
        saved: true,
      },
    ];
    useStudio.mockReturnValue(value);

    const rendered = render(<MaterialsPage />);

    const card = screen.getByRole("button", {
      name: "选择素材 乡墅工程师施工现场人物竖版场景形象图.png",
    });
    expect(card).toHaveClass("content-asset--image");
    expect(card.querySelector(".studio-media")).not.toHaveAttribute("style");
    expect(card.querySelector("strong")).toHaveAttribute(
      "title",
      "乡墅工程师施工现场人物竖版场景形象图.png",
    );
    expect(card.querySelector(".content-asset__status")).toHaveTextContent(
      "永久保存",
    );
    expect(
      rendered.container.querySelector(".content-asset__kind"),
    ).toHaveTextContent("图片");
  });

  it("五视图按套展示正面封面，切换视角后首帧使用对应单图", async () => {
    listMaterials.mockResolvedValue({
      items: [
        {
          ...material("sheet"),
          composite: true,
          person_id: "person-1",
          preview_asset_id: "front",
          character_views: [
            { asset_id: "front", view_type: "FRONT_FULL" },
            { asset_id: "left", view_type: "LEFT_SIDE" },
          ],
          allowed_uses: ["reference"],
        },
      ],
      page: 1,
      page_size: 6,
      total: 1,
    });
    getAssetDownloadUrl.mockImplementation(async (id: string) => ({
      url: `https://storage.test/${id}`,
    }));
    const value = studio({ review: false });
    useStudio.mockReturnValue(value);
    render(<MaterialsPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: "选择素材 sheet.png" }),
    );
    expect(
      await screen.findByRole("button", { name: "正面全身" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(getAssetDownloadUrl).toHaveBeenCalledWith("front");
    fireEvent.click(screen.getByRole("button", { name: "左侧面" }));
    await waitFor(() =>
      expect(getAssetDownloadUrl).toHaveBeenCalledWith("left"),
    );
    fireEvent.click(screen.getByRole("button", { name: "用作首帧" }));
    expect(value.patchDraft).toHaveBeenCalledWith({ firstFrameId: "left" });
    expect(value.updateData).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "下载素材" }));
    await waitFor(() =>
      expect(downloadMaterialAsset).toHaveBeenCalledWith(
        "left",
        "sheet.png · 左侧面",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "合成图" }));
    await waitFor(() =>
      expect(getAssetDownloadUrl).toHaveBeenCalledWith("sheet"),
    );
    expect(screen.queryByRole("button", { name: "用作首帧" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "下载素材" }));
    await waitFor(() =>
      expect(downloadMaterialAsset).toHaveBeenCalledWith("sheet", "sheet.png"),
    );
    expect(screen.getAllByRole("button", { name: /选择素材 / })).toHaveLength(
      1,
    );
  });

  it.each([false, true])(
    "清理五视图缓存保留就绪图片并恢复未完成预览（ready=%s）",
    async (ready) => {
      listMaterials.mockResolvedValue({
        items: [
          {
            ...material("sheet"),
            composite: true,
            preview_asset_id: "front",
            character_views: [
              { asset_id: "front", view_type: "FRONT_FULL" },
              { asset_id: "left", view_type: "LEFT_SIDE" },
            ],
            allowed_uses: ["reference"],
          },
        ],
        page: 1,
        page_size: 6,
        total: 1,
      });
      const release = vi.fn();
      let rejectPending: ((reason: unknown) => void) | undefined;
      getMaterialCachedPreview.mockImplementation(
        async (_user, id, options) => {
          if (id === "left" && options?.populate) {
            if (!ready)
              return new Promise((_resolve, reject) => {
                rejectPending = reject;
              });
            return { url: "blob:left-ready", cached: true, release };
          }
          return {
            url: `https://storage.test/${id}`,
            cached: false,
            release: vi.fn(),
          };
        },
      );
      clearMaterialCache.mockImplementation(async () => {
        rejectPending?.(new DOMException("素材缓存已清理", "AbortError"));
      });
      useStudio.mockReturnValue(studio({ review: false }));
      const rendered = render(<MaterialsPage />);
      fireEvent.click(
        await screen.findByRole("button", { name: "选择素材 sheet.png" }),
      );
      fireEvent.click(await screen.findByRole("button", { name: "左侧面" }));
      await waitFor(() =>
        expect(
          getMaterialCachedPreview.mock.calls.some(
            (call) => call[1] === "left" && call[2]?.populate === true,
          ),
        ).toBe(true),
      );
      if (ready)
        await waitFor(() =>
          expect(
            screen.getByRole("img", {
              name: "sheet.png 左侧面",
            }),
          ).toHaveAttribute("src", "blob:left-ready"),
        );
      fireEvent.click(screen.getByRole("button", { name: "清理本机缓存" }));
      await screen.findByText(
        "本机缓存已清理，云端素材保留。当前播放不受影响。",
      );
      await waitFor(() =>
        expect(
          screen.getByRole("img", {
            name: "sheet.png 左侧面",
          }),
        ).toHaveAttribute(
          "src",
          ready ? "blob:left-ready" : "https://storage.test/left",
        ),
      );
      expect(
        getMaterialCachedPreview.mock.calls.filter(
          (call) => call[1] === "left" && call[2]?.populate === true,
        ),
      ).toHaveLength(1);
      expect(release).not.toHaveBeenCalled();
      if (!ready)
        expect(
          getMaterialCachedPreview.mock.calls.some(
            (call) => call[1] === "left" && call[2]?.populate === false,
          ),
        ).toBe(true);
      rendered.unmount();
      if (ready) expect(release).toHaveBeenCalledOnce();
    },
  );

  it("素材库二十四条分页，初始定位已选素材且筛选后保留右侧选择", () => {
    const base = studio();
    const assets = Array.from({ length: 30 }, (_, index) => ({
      id: `image-${index + 1}`,
      name: `乡墅素材 ${index + 1}`,
      kind: "image" as const,
      group: "人物素材",
      source: "人物库",
      saved: true,
      ...(index === 29 ? { composite: true } : {}),
    }));
    useStudio.mockReturnValue(
      studio({
        data: { ...base.data, assets },
        state: { ...base.state, selectedAssetId: "image-30" },
      }),
    );
    render(<MaterialsPage />);

    expect(
      screen.getByRole("button", { name: "选择素材 乡墅素材 30" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "选择素材 乡墅素材 1" }),
    ).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "图片" }));
    expect(
      screen.getByRole("button", { name: "选择素材 乡墅素材 1" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 2, name: "乡墅素材 30" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(
      screen.getByRole("button", { name: "选择素材 乡墅素材 30" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "上一页" }));
    expect(
      screen.getByRole("button", { name: "选择素材 乡墅素材 1" }),
    ).toBeInTheDocument();
  });

  it("非审核素材页读取服务端素材并把真实资产 ID 带入口播草稿", async () => {
    listMaterials.mockResolvedValue({
      items: [
        {
          id: "asset:audio-cloud-1",
          owner_user_id: "employee_1",
          asset_id: "audio-cloud-1",
          generation_task_id: null,
          project_id: null,
          person_id: "person-1",
          title: "云端讲解.mp3",
          group: "口播素材",
          media_type: "audio",
          source: "upload",
          status: "ready",
          delivery: "stored",
          content_type: "audio/mpeg",
          size_bytes: 1024,
          duration_seconds: 18,
          created_at: "2026-09-06 10:00:00",
          hidden: false,
          saved: true,
          allowed_uses: ["oral_audio", "reference"],
          allowed_actions: ["preview", "download", "rename", "hide"],
        },
      ],
      page: 1,
      page_size: 6,
      total: 1,
    });
    const value = studio({
      review: false,
      data: { ...studio().data, assets: [] },
    });
    useStudio.mockReturnValue(value);
    render(<MaterialsPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "选择素材 云端讲解.mp3" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "用于音频口播" }));

    expect(value.patchDraft).toHaveBeenCalledWith({
      audioId: "audio-cloud-1",
      ipId: "person-1",
      voiceId: undefined,
    });
    expect(value.updateData).toHaveBeenCalled();
    expect(value.navigate).toHaveBeenCalledWith("oral-audio", {
      returnTo: "materials",
    });
  });

  it("素材页完成上传、重命名和移除的服务端闭环", async () => {
    listMaterials.mockResolvedValue({
      items: [],
      page: 1,
      page_size: 6,
      total: 0,
    });
    createMaterialUploadIntent.mockResolvedValue({
      material_id: "asset:image-cloud-1",
      asset_id: "image-cloud-1",
      storage_key: "materials/employee_1/image-cloud-1/original.png",
      method: "PUT",
      url: "https://storage.test/upload",
      headers: { "Content-Type": "image/png" },
      expires_at: "2026-09-06T10:10:00Z",
    });
    uploadMaterial.mockImplementation(
      (_intent, _file, onProgress: (progress: number) => void) => {
        onProgress(100);
        return Promise.resolve();
      },
    );
    const uploaded = {
      id: "asset:image-cloud-1",
      owner_user_id: "employee_1",
      asset_id: "image-cloud-1",
      generation_task_id: null,
      project_id: null,
      person_id: null,
      title: "庭院.png",
      group: "我的上传",
      media_type: "image",
      source: "upload",
      status: "ready",
      delivery: "stored",
      content_type: "image/png",
      size_bytes: 8,
      duration_seconds: null,
      created_at: "2026-09-06 10:00:00",
      hidden: false,
      saved: true,
      allowed_uses: [
        "original_frame",
        "first_frame",
        "tail_frame",
        "reference",
      ],
      allowed_actions: ["preview", "download", "rename", "hide"],
    } as const;
    completeMaterialUpload.mockResolvedValue(uploaded);
    updateMaterial.mockResolvedValue({ ...uploaded, title: "新庭院首帧" });
    hideMaterial.mockResolvedValue(undefined);
    const value = studio({
      review: false,
      data: { ...studio().data, assets: [] },
    });
    useStudio.mockReturnValue(value);
    render(<MaterialsPage />);

    const file = new File([new Uint8Array(8)], "庭院.png", {
      type: "image/png",
    });
    fireEvent.change(screen.getByLabelText("选择上传素材"), {
      target: { files: [file] },
    });
    await waitFor(() =>
      expect(completeMaterialUpload).toHaveBeenCalledWith("image-cloud-1"),
    );
    expect(
      screen.getByRole("heading", { name: "庭院.png" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "用作尾帧" }));
    expect(value.patchDraft).toHaveBeenCalledWith({
      tailFrameId: "image-cloud-1",
    });
    expect(value.navigate).toHaveBeenCalledWith("video", {
      returnTo: "materials",
    });

    // Upload completion selects the asset, then an effect initializes its
    // controlled fields. Wait for the editable form, not just the API call,
    // before entering a new name (CI may render the heading first).
    await waitFor(() =>
      expect(screen.getByLabelText("素材名称")).toHaveValue("庭院.png"),
    );
    fireEvent.change(screen.getByLabelText("素材名称"), {
      target: { value: "新庭院首帧" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存名称" }));
    await waitFor(() =>
      expect(updateMaterial).toHaveBeenCalledWith("asset:image-cloud-1", {
        title: "新庭院首帧",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "从素材库移除" }));
    await waitFor(() =>
      expect(hideMaterial).toHaveBeenCalledWith("asset:image-cloud-1"),
    );
  });

  it("复用素材时用最新名称和分组更新已有草稿缓存", async () => {
    const item = material("renamed-video", {
      title: "最新成片名称",
      group: "联合调试验收",
      media_type: "video",
      content_type: "video/mp4",
      duration_seconds: 4,
      allowed_uses: ["reference"],
    });
    listMaterials.mockResolvedValue({
      items: [item],
      page: 1,
      page_size: 6,
      total: 1,
    });
    const base = studio();
    const value = studio({
      review: false,
      data: {
        ...base.data,
        assets: [
          {
            id: "renamed-video",
            name: "旧名称",
            kind: "video",
            group: "旧分组",
            source: "任务中心",
            saved: true,
          },
        ],
      },
    });
    useStudio.mockReturnValue(value);
    render(<MaterialsPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: "选择素材 最新成片名称" }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "用于参考生视频" }),
    );
    const update = vi.mocked(value.updateData).mock.calls.at(-1)?.[0];
    expect(update).toBeTypeOf("function");
    const next = (update as (data: StudioData) => StudioData)(value.data);
    expect(next.assets.filter((asset) => asset.id === "renamed-video")).toEqual(
      [
        expect.objectContaining({
          name: "最新成片名称",
          group: "联合调试验收",
        }),
      ],
    );
    expect(value.patchDraft).toHaveBeenCalledWith({
      referenceIds: ["renamed-video"],
    });
  });

  it("视频播放才后台填充缓存且不替换正在播放的地址", async () => {
    listMaterials.mockResolvedValue({
      items: [
        material("cache-video", { media_type: "video", title: "缓存视频.mp4" }),
      ],
      page: 1,
      total: 1,
    });
    const release = vi.fn();
    // 网格预览走批量通道（P0-2）；播放后的后台填充仍走单资产预览（populate）。
    getMaterialBatchPreviews.mockResolvedValue({
      previews: {
        "cache-video": {
          url: "https://storage.test/video",
          cached: false,
          release: vi.fn(),
        },
      },
      thumbnails: {},
    });
    getMaterialCachedPreview.mockImplementation(
      async (_userId, _assetId, options) =>
        options?.populate
          ? { url: "blob:cached-video", cached: true, release }
          : {
              url: "https://storage.test/video",
              cached: false,
              release: vi.fn(),
            },
    );
    useStudio.mockReturnValue(
      studio({
        review: false,
        user: { id: "cache-user" } as StudioContextValue["user"],
      }),
    );
    render(<MaterialsPage />);
    const video = await screen.findByLabelText("缓存视频.mp4");
    await waitFor(() =>
      expect(video).toHaveAttribute("src", "https://storage.test/video"),
    );
    expect(
      getMaterialCachedPreview.mock.calls.some((call) => call[2]?.populate),
    ).toBe(false);
    fireEvent.play(video);
    await waitFor(() => expect(release).toHaveBeenCalledOnce());
    expect(getMaterialCachedPreview).toHaveBeenLastCalledWith(
      "cache-user",
      "cache-video",
      expect.objectContaining({ populate: true }),
    );
    expect(video).toHaveAttribute("src", "https://storage.test/video");
  });

  it("缓存命中在卸载时释放，清理只删除本机缓存", async () => {
    listMaterials.mockResolvedValue({
      items: [material("cache-image")],
      page: 1,
      total: 1,
    });
    const release = vi.fn();
    getMaterialBatchPreviews.mockResolvedValue({
      previews: {
        "cache-image": {
          url: "blob:cached-image",
          cached: true,
          release,
        },
      },
      thumbnails: {},
    });
    getMaterialCacheUsage.mockResolvedValue({
      bytes: 1048576,
      limitBytes: 268435456,
      available: true,
    });
    useStudio.mockReturnValue(
      studio({
        review: false,
        user: { id: "cache-user" } as StudioContextValue["user"],
      }),
    );
    const view = render(<MaterialsPage />);
    await screen.findByRole("img", { name: "cache-image.png" });
    fireEvent.click(screen.getByRole("button", { name: "清理本机缓存" }));
    await waitFor(() =>
      expect(clearMaterialCache).toHaveBeenCalledWith("cache-user"),
    );
    expect(hideMaterial).not.toHaveBeenCalled();
    view.unmount();
    expect(release).toHaveBeenCalled();
  });

  it("切换账号丢弃旧缓存预览并释放迟到的Blob", async () => {
    listMaterials.mockResolvedValue({
      items: [material("cache-image")],
      page: 1,
      total: 1,
    });
    let resolveOld!: (value: unknown) => void;
    const releaseOld = vi.fn();
    getMaterialBatchPreviews.mockImplementation((userId) =>
      userId === "old-user"
        ? new Promise((resolve) => {
            resolveOld = resolve;
          })
        : Promise.resolve({
            previews: {
              "cache-image": {
                url: "blob:new-user",
                cached: true,
                release: vi.fn(),
              },
            },
            thumbnails: {},
          }),
    );
    const context = studio({
      review: false,
      user: { id: "old-user" } as StudioContextValue["user"],
    });
    useStudio.mockReturnValue(context);
    const view = render(<MaterialsPage />);
    await waitFor(() => expect(resolveOld).toBeTypeOf("function"));
    useStudio.mockReturnValue({
      ...context,
      user: { ...context.user, id: "new-user" },
    });
    view.rerender(<MaterialsPage />);
    await waitFor(() =>
      expect(
        screen.getByRole("img", { name: "cache-image.png" }),
      ).toHaveAttribute("src", "blob:new-user"),
    );
    await act(async () => {
      resolveOld({
        previews: {
          "cache-image": {
            url: "blob:old-user",
            cached: true,
            release: releaseOld,
          },
        },
        thumbnails: {},
      });
    });
    expect(releaseOld).toHaveBeenCalledOnce();
    expect(
      screen.getByRole("img", { name: "cache-image.png" }),
    ).toHaveAttribute("src", "blob:new-user");
  });

  it("非审核素材页把搜索和来源筛选交给服务端", async () => {
    listMaterials.mockResolvedValue({
      items: [],
      page: 1,
      page_size: 6,
      total: 0,
    });
    useStudio.mockReturnValue(
      studio({ review: false, data: { ...studio().data, assets: [] } }),
    );
    render(<MaterialsPage />);
    await waitFor(() => expect(listMaterials).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText("搜索素材"), {
      target: { value: "庭院" },
    });
    fireEvent.change(screen.getByLabelText("素材来源"), {
      target: { value: "upload" },
    });
    fireEvent.submit(screen.getByRole("form", { name: "素材筛选" }));

    await waitFor(() =>
      expect(listMaterials).toHaveBeenLastCalledWith({
        mediaType: undefined,
        source: "upload",
        query: "庭院",
        group: undefined,
        page: 1,
        pageSize: 24,
      }),
    );
  });

  it("素材页一次批量签名当前页并在翻页后加载下一页", async () => {
    const firstPage = [
      material("image-1"),
      material("video-2", {
        title: "video-2.mp4",
        media_type: "video",
        content_type: "video/mp4",
      }),
      material("audio-3", {
        title: "audio-3.mp3",
        media_type: "audio",
        content_type: "audio/mpeg",
      }),
      ...Array.from({ length: 21 }, (_, index) =>
        material(`image-${index + 4}`),
      ),
    ];
    listMaterials.mockImplementation(({ page }: { page: number }) =>
      Promise.resolve({
        items: page === 1 ? firstPage : [material("image-25")],
        page,
        page_size: 24,
        total: 25,
      }),
    );
    getAssetDownloadUrl.mockImplementation((id: string) =>
      Promise.resolve({ url: `https://storage.test/${id}` }),
    );
    useStudio.mockReturnValue(studio({ review: false }));
    render(<MaterialsPage />);

    // 整页可见素材只发一次批量授权（P0-2），不再逐瓦片请求。
    await waitFor(() =>
      expect(getMaterialBatchPreviews).toHaveBeenCalledTimes(1),
    );
    const firstEntries = getMaterialBatchPreviews.mock.calls[0][1];
    expect(firstEntries).toHaveLength(24);
    expect(
      firstEntries.slice(0, 3).map((entry: { id: string }) => entry.id),
    ).toEqual(["image-1", "video-2", "audio-3"]);
    // 首屏只读已有缓存或签在线地址，不等待 22 张原图全部下载到本机。
    const populateIds = firstEntries
      .filter((entry: { populate: boolean }) => entry.populate)
      .map((entry: { id: string }) => entry.id);
    expect(populateIds).toEqual([]);
    expect(
      await screen.findByRole("img", { name: "image-1.png" }),
    ).toHaveAttribute("src", "https://storage.test/image-1");
    expect(screen.getByLabelText("video-2.mp4")).toHaveAttribute(
      "src",
      "https://storage.test/video-2",
    );
    expect(screen.getByLabelText("audio-3.mp3")).toHaveAttribute(
      "src",
      "https://storage.test/audio-3",
    );

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() =>
      expect(getMaterialBatchPreviews).toHaveBeenCalledTimes(2),
    );
    expect(
      getMaterialBatchPreviews.mock.calls[1][1].map(
        (entry: { id: string }) => entry.id,
      ),
    ).toEqual(["image-25"]);
    fireEvent.click(
      await screen.findByRole("button", { name: "选择素材 image-25.png" }),
    );
    expect(
      screen.getByRole("heading", { level: 2, name: "image-25.png" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: "image-25.png" })).toHaveLength(
      2,
    );
  });

  it("批量授权失败退回逐条并自动重试，旧页迟到响应不能覆盖当前页", async () => {
    let resolveOld: ((value: { url: string }) => void) | undefined;
    listMaterials.mockImplementation(({ page }: { page: number }) =>
      Promise.resolve({
        items:
          page === 1
            ? [
                material("old-1"),
                ...Array.from({ length: 23 }, (_, index) =>
                  material(`old-${index + 2}`),
                ),
              ]
            : [material("current-7")],
        page,
        page_size: 24,
        total: 25,
      }),
    );
    // 第 1 页批量授权成功；第 2 页批量整体超时 → 退回逐条路径。
    getMaterialBatchPreviews
      .mockImplementationOnce(async (_userId, entries) => {
        const previews: Record<
          string,
          { url: string; cached: boolean; release: () => unknown }
        > = {};
        for (const entry of entries) {
          previews[entry.id] = {
            ...(await getAssetDownloadUrl(entry.id)),
            cached: false,
            release: vi.fn(),
          };
        }
        return { previews, thumbnails: {} };
      })
      .mockRejectedValueOnce(new Error("批量授权超时"));
    getAssetDownloadUrl.mockImplementation((id: string) => {
      if (id === "old-1")
        return new Promise((resolve) => {
          resolveOld = resolve;
        });
      if (
        id === "current-7" &&
        getAssetDownloadUrl.mock.calls.filter(([assetId]) => assetId === id)
          .length === 1
      )
        return Promise.reject(new Error("签名服务超时"));
      return Promise.resolve({ url: `https://storage.test/${id}` });
    });
    useStudio.mockReturnValue(studio({ review: false }));
    render(<MaterialsPage />);

    await screen.findByRole("button", { name: "选择素材 old-1.png" });
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(
      await screen.findByText("预览加载失败，点击重试"),
    ).toBeInTheDocument();
    resolveOld?.({ url: "https://storage.test/stale-old-1" });
    fireEvent.click(
      screen.getByRole("button", { name: "选择素材 current-7.png" }),
    );
    // 首次签名失败后 1 秒自动重试（P0-4），无需用户再次操作。
    await waitFor(
      () =>
        expect(
          screen.getAllByRole("img", { name: "current-7.png" }),
        ).toHaveLength(2),
      { timeout: 4000 },
    );
    for (const image of screen.getAllByRole("img", { name: "current-7.png" }))
      expect(image).toHaveAttribute("src", "https://storage.test/current-7");
    expect(screen.queryByText("stale-old-1")).toBeNull();
  });

  it("预览失败自动重试最多两次后停止，不形成无限重试", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      listMaterials.mockResolvedValue({
        items: [material("doomed-image")],
        page: 1,
        page_size: 24,
        total: 1,
      });
      getAssetDownloadUrl.mockRejectedValue(new Error("签名服务不可用"));
      useStudio.mockReturnValue(studio({ review: false }));
      render(<MaterialsPage />);

      await screen.findByRole("button", { name: "选择素材 doomed-image.png" });
      expect(
        await screen.findByText("预览加载失败，点击重试"),
      ).toBeInTheDocument();
      // 批量授权 1 次 + 逐条回退 1 次 + 自动重试 2 次 = 4 次，之后不再重试。
      await vi.advanceTimersByTimeAsync(5_000);
      expect(getAssetDownloadUrl).toHaveBeenCalledTimes(4);
      expect(screen.getByText("预览加载失败，点击重试")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("签名地址过期后自动重签取得新地址并同步详情", async () => {
    listMaterials.mockResolvedValue({
      items: [material("expiring-image")],
      page: 1,
      page_size: 24,
      total: 1,
    });
    getAssetDownloadUrl
      .mockResolvedValueOnce({ url: "https://storage.test/expired" })
      .mockResolvedValueOnce({ url: "https://storage.test/refreshed" });
    useStudio.mockReturnValue(studio({ review: false }));
    render(<MaterialsPage />);

    const expiredImage = await screen.findByRole("img", {
      name: "expiring-image.png",
    });
    fireEvent.error(expiredImage);
    expect(
      await screen.findByText("预览加载失败，点击重试"),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "选择素材 expiring-image.png" }),
    );
    // 过期不再永久置灰：约 1 秒后自动重签并恢复（P0-4）。
    await waitFor(
      () =>
        expect(
          screen.getAllByRole("img", { name: "expiring-image.png" }),
        ).toHaveLength(2),
      { timeout: 4000 },
    );
    for (const image of screen.getAllByRole("img", {
      name: "expiring-image.png",
    }))
      expect(image).toHaveAttribute("src", "https://storage.test/refreshed");
    expect(getAssetDownloadUrl).toHaveBeenCalledTimes(2);
  });

  it.each([
    ["image", "image/png", "img"],
    ["video", "video/mp4", "video"],
    ["audio", "audio/mpeg", "audio"],
  ] as const)(
    "%s 媒体错误后单次重签并让详情复用新地址",
    async (mediaType, contentType, tagName) => {
      const item = material(`retry-${mediaType}`, {
        title: `retry-${mediaType}.${mediaType === "image" ? "png" : mediaType === "video" ? "mp4" : "mp3"}`,
        media_type: mediaType,
        content_type: contentType,
      });
      listMaterials.mockResolvedValue({
        items: [item],
        page: 1,
        page_size: 24,
        total: 1,
      });
      getAssetDownloadUrl
        .mockResolvedValueOnce({ url: `https://storage.test/${mediaType}-old` })
        .mockResolvedValueOnce({
          url: `https://storage.test/${mediaType}-new`,
        });
      useStudio.mockReturnValue(studio({ review: false }));
      render(<MaterialsPage />);

      const card = await screen.findByRole("button", {
        name: `选择素材 ${item.title}`,
      });
      const oldMedia = await waitFor(() => {
        const element = card.querySelector(tagName);
        expect(element).toHaveAttribute(
          "src",
          `https://storage.test/${mediaType}-old`,
        );
        return element as HTMLElement;
      });
      fireEvent.error(oldMedia);
      fireEvent.click(card);
      // 媒体错误触发单次自动重签（P0-4），详情复用新地址。
      await waitFor(
        () =>
          expect(card.querySelector(tagName)).toHaveAttribute(
            "src",
            `https://storage.test/${mediaType}-new`,
          ),
        { timeout: 4000 },
      );
      fireEvent.error(oldMedia);

      expect(card.querySelector(tagName)).toHaveAttribute(
        "src",
        `https://storage.test/${mediaType}-new`,
      );
      expect(screen.queryByText("预览加载失败，点击重试")).toBeNull();
      expect(
        screen.getByRole("heading", { name: item.title }),
      ).toBeInTheDocument();
      expect(
        screen
          .getByRole("heading", { name: item.title })
          .parentElement?.querySelector(tagName),
      ).toHaveAttribute("src", `https://storage.test/${mediaType}-new`);
      expect(getAssetDownloadUrl).toHaveBeenCalledTimes(2);
    },
  );

  it("带封面的视频瓦片用缩略图懒加载展示，详情仍可播放原视频", async () => {
    listMaterials.mockResolvedValue({
      items: [
        material("thumb-video", {
          title: "thumb-video.mp4",
          media_type: "video",
          content_type: "video/mp4",
        }),
      ],
      page: 1,
      page_size: 24,
      total: 1,
    });
    getMaterialBatchPreviews.mockResolvedValue({
      previews: {
        "thumb-video": {
          url: "https://storage.test/video",
          cached: false,
          release: vi.fn(),
        },
      },
      thumbnails: { "thumb-video": "https://media.test/thumb.jpg" },
    });
    useStudio.mockReturnValue(studio({ review: false }));
    render(<MaterialsPage />);

    const card = await screen.findByRole("button", {
      name: "选择素材 thumb-video.mp4",
    });
    // 网格展示缩略图 img（懒加载），不再经服务端代理流式拉原视频。
    const thumb = await waitFor(() => {
      const element = card.querySelector("img");
      expect(element).toHaveAttribute("src", "https://media.test/thumb.jpg");
      expect(element).toHaveAttribute("loading", "lazy");
      return element as HTMLElement;
    });
    expect(card.querySelector("video")).toBeNull();
    expect(thumb).toBeInTheDocument();
    // 点开详情：先见封面海报，随后加载可播放视频。
    fireEvent.click(card);
    const heading = await screen.findByRole("heading", {
      name: "thumb-video.mp4",
    });
    expect(heading.parentElement?.querySelector("img")).toHaveAttribute(
      "src",
      "https://media.test/thumb.jpg",
    );
    await waitFor(() =>
      expect(heading.parentElement?.querySelector("video")).toHaveAttribute(
        "src",
        "https://storage.test/video",
      ),
    );
  });

  it("缩略图地址失效时自动重新授权，不把瓦片永久置灰", async () => {
    // 客户版签名绑定 session_epoch：会话一换，页面 state 里缓存的缩略图地址
    // 立即 403。既有自动重签的判据是 previewStates.url === failedUrl，而缩略图
    // 失败报上来的是 poster，匹配不上就直接 return——表现为「用着用着图没了，
    // 刷新一下又好」。
    listMaterials.mockResolvedValue({
      items: [
        material("stale-thumb", {
          title: "stale-thumb.mp4",
          media_type: "video",
          content_type: "video/mp4",
        }),
      ],
      page: 1,
      page_size: 24,
      total: 1,
    });
    getMaterialBatchPreviews
      .mockResolvedValueOnce({
        previews: {
          "stale-thumb": {
            url: "https://storage.test/video",
            cached: false,
            release: vi.fn(),
          },
        },
        thumbnails: { "stale-thumb": "https://media.test/stale.jpg" },
      })
      .mockResolvedValue({
        previews: {
          "stale-thumb": {
            url: "https://storage.test/video",
            cached: false,
            release: vi.fn(),
          },
        },
        thumbnails: { "stale-thumb": "https://media.test/fresh.jpg" },
      });
    useStudio.mockReturnValue(studio({ review: false }));
    render(<MaterialsPage />);

    const card = await screen.findByRole("button", {
      name: "选择素材 stale-thumb.mp4",
    });
    const stale = await waitFor(() => {
      const element = card.querySelector("img");
      expect(element).toHaveAttribute("src", "https://media.test/stale.jpg");
      return element as HTMLElement;
    });

    fireEvent.error(stale);

    await waitFor(() =>
      expect(card.querySelector("img")).toHaveAttribute(
        "src",
        "https://media.test/fresh.jpg",
      ),
    );
  });

  it("缩略图反复失效时有限次重签后停手，不陷入死循环", async () => {
    // 抽帧确实失败的视频，每次重新授权都会签出一条新地址却依旧 404。没有上限
    // 就是「重签 → 404 → 重签」的无限请求。
    listMaterials.mockResolvedValue({
      items: [
        material("broken-thumb", {
          title: "broken-thumb.mp4",
          media_type: "video",
          content_type: "video/mp4",
        }),
      ],
      page: 1,
      page_size: 24,
      total: 1,
    });
    let issued = 0;
    getMaterialBatchPreviews.mockImplementation(async () => {
      issued += 1;
      return {
        previews: {
          "broken-thumb": {
            url: "https://storage.test/video",
            cached: false,
            release: vi.fn(),
          },
        },
        thumbnails: {
          "broken-thumb": `https://media.test/broken-${issued}.jpg`,
        },
      };
    });
    useStudio.mockReturnValue(studio({ review: false }));
    render(<MaterialsPage />);

    const card = await screen.findByRole("button", {
      name: "选择素材 broken-thumb.mp4",
    });
    for (let round = 0; round < 6; round += 1) {
      const image = card.querySelector("img");
      if (!image) break;
      fireEvent.error(image);
      await waitFor(() => expect(getMaterialBatchPreviews).toHaveBeenCalled());
    }
    // 首次授权 + 至多两次重签。
    expect(issued).toBeLessThanOrEqual(3);
  });

  it("无封面的历史视频瓦片保持原视频预览行为", async () => {
    listMaterials.mockResolvedValue({
      items: [
        material("legacy-video", {
          title: "legacy-video.mp4",
          media_type: "video",
          content_type: "video/mp4",
        }),
      ],
      page: 1,
      page_size: 24,
      total: 1,
    });
    useStudio.mockReturnValue(studio({ review: false }));
    render(<MaterialsPage />);

    const card = await screen.findByRole("button", {
      name: "选择素材 legacy-video.mp4",
    });
    // 默认批量夹具不含缩略图 → 瓦片回退为 video 预览（beforeEach 默认 URL）。
    await waitFor(() =>
      expect(card.querySelector("video")).toHaveAttribute(
        "src",
        "https://storage.test/material",
      ),
    );
    expect(card.querySelector("img")).toBeNull();
  });

  it("直出素材通过 generation_task_id 获取预览且详情复用", async () => {
    const direct = material("direct-task", {
      id: "generation:direct-task",
      asset_id: null,
      generation_task_id: "direct-task",
      title: "直出成片.mp4",
      media_type: "video",
      content_type: "video/mp4",
      source: "generation",
      delivery: "direct",
      saved: false,
    });
    listMaterials.mockResolvedValue({
      items: [direct],
      page: 1,
      page_size: 6,
      total: 1,
    });
    createGenerationTaskPreviewUrl.mockResolvedValue(
      "https://provider.test/direct-task.mp4",
    );
    useStudio.mockReturnValue(studio({ review: false }));
    render(<MaterialsPage />);

    const card = await screen.findByRole("button", {
      name: "选择素材 直出成片.mp4",
    });
    await waitFor(() =>
      expect(card.querySelector("video")).toHaveAttribute(
        "src",
        "https://provider.test/direct-task.mp4",
      ),
    );
    fireEvent.click(card);
    expect(createGenerationTaskPreviewUrl).toHaveBeenCalledOnce();
    expect(getAssetDownloadUrl).not.toHaveBeenCalled();
    expect(screen.getAllByLabelText("直出成片.mp4")).toHaveLength(2);
  });

  it("移除末页唯一素材后回到有效页且不形成空白死页", async () => {
    const firstPage = Array.from({ length: 24 }, (_, index) =>
      material(`item-${index + 1}`),
    );
    let total = 25;
    listMaterials.mockImplementation(({ page }: { page: number }) =>
      Promise.resolve({
        items: page === 1 ? firstPage : [material("last-item")],
        page,
        page_size: 24,
        total,
      }),
    );
    hideMaterial.mockImplementation(() => {
      total = 6;
      return Promise.resolve();
    });
    useStudio.mockReturnValue(studio({ review: false }));
    render(<MaterialsPage />);

    await screen.findByRole("button", { name: "选择素材 item-1.png" });
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "选择素材 last-item.png" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "从素材库移除" }));

    await screen.findByRole("button", { name: "选择素材 item-1.png" });
    expect(listMaterials).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 1 }),
    );
    expect(screen.queryByText("暂无素材")).toBeNull();
  });

  it("已归档素材支持修改分组和直接下载", async () => {
    const material = {
      id: "asset:image-cloud-2",
      owner_user_id: "employee_1",
      asset_id: "image-cloud-2",
      generation_task_id: null,
      project_id: null,
      person_id: null,
      title: "院门.png",
      group: "我的上传",
      media_type: "image",
      source: "upload",
      status: "ready",
      delivery: "stored",
      content_type: "image/png",
      size_bytes: 8,
      duration_seconds: null,
      created_at: "2026-09-06 10:00:00",
      hidden: false,
      saved: true,
      allowed_uses: ["reference"],
      allowed_actions: ["preview", "download", "rename", "hide"],
    } as const;
    listMaterials.mockResolvedValue({
      items: [material],
      page: 1,
      page_size: 6,
      total: 1,
    });
    listMaterialGroups.mockResolvedValue({
      items: [
        { name: "我的上传", count: 1 },
        { name: "庭院案例", count: 2 },
      ],
    });
    updateMaterial.mockResolvedValue({ ...material, group: "庭院案例" });
    downloadMaterialAsset.mockResolvedValue(undefined);
    useStudio.mockReturnValue(
      studio({ review: false, data: { ...studio().data, assets: [] } }),
    );
    render(<MaterialsPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "选择素材 院门.png" }),
    );
    fireEvent.change(screen.getByLabelText("素材分组"), {
      target: { value: "庭院案例" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存分组" }));
    await waitFor(() =>
      expect(updateMaterial).toHaveBeenCalledWith("asset:image-cloud-2", {
        group: "庭院案例",
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: "下载素材" }));
    await waitFor(() =>
      expect(downloadMaterialAsset).toHaveBeenCalledWith(
        "image-cloud-2",
        "院门.png",
      ),
    );
  });

  it("分组导航按分组筛选，整理模式可跨卡片批量移动", async () => {
    listMaterials.mockResolvedValue({
      items: [material("gate", { group: "庭院案例" }), material("door")],
      page: 1,
      page_size: 24,
      total: 2,
    });
    listMaterialGroups.mockResolvedValue({
      items: [
        { name: "我的上传", count: 1 },
        { name: "庭院案例", count: 1 },
      ],
    });
    bulkUpdateMaterials.mockResolvedValue({ updated: 1, skipped: 0 });
    useStudio.mockReturnValue(studio({ review: false }));
    render(<MaterialsPage />);

    await screen.findByRole("button", { name: "选择素材 gate.png" });
    fireEvent.click(screen.getByRole("button", { name: "筛选分组 庭院案例" }));
    await waitFor(() =>
      expect(listMaterials).toHaveBeenLastCalledWith(
        expect.objectContaining({ group: "庭院案例", page: 1 }),
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "整理素材" }));
    fireEvent.click(screen.getByRole("button", { name: "选择素材 gate.png" }));
    expect(screen.getByText("已选 1 项")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("批量目标分组"), {
      target: { value: "我的上传" },
    });
    fireEvent.click(screen.getByRole("button", { name: "移入分组" }));
    await waitFor(() =>
      expect(bulkUpdateMaterials).toHaveBeenCalledWith({
        material_ids: ["asset:gate"],
        update: { group: "我的上传" },
      }),
    );
    expect(
      screen.getByRole("button", { name: "筛选分组 庭院案例" }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  it("详情面板可把素材改入新建分组", async () => {
    const item = material("gate");
    listMaterials.mockResolvedValue({
      items: [item],
      page: 1,
      page_size: 24,
      total: 1,
    });
    listMaterialGroups.mockResolvedValue({
      items: [{ name: "我的上传", count: 1 }],
    });
    updateMaterial.mockResolvedValue({ ...item, group: "门头专题" });
    useStudio.mockReturnValue(studio({ review: false }));
    render(<MaterialsPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "选择素材 gate.png" }),
    );
    fireEvent.change(screen.getByLabelText("素材分组"), {
      target: { value: "__new__" },
    });
    fireEvent.change(screen.getByLabelText("新分组名称"), {
      target: { value: "门头专题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存分组" }));
    await waitFor(() =>
      expect(updateMaterial).toHaveBeenCalledWith("asset:gate", {
        group: "门头专题",
      }),
    );
  });

  it("审核示例发布草稿独立保存，不覆盖口播脚本", () => {
    const base = studio();
    const value = studio({
      state: {
        ...base.state,
        selectedAssetId: "completed-video",
        draft: {
          ...base.state.draft,
          script: {
            ...base.state.draft.script,
            text: "口播终稿不得被发布表单覆盖",
          },
        },
      },
      data: {
        ...base.data,
        assets: [
          ...base.data.assets,
          {
            id: "other-video",
            name: "不应被随机选作预览的视频.mp4",
            kind: "video",
            poster: "/studio/other.jpg",
            group: "成片",
            source: "任务中心",
            saved: true,
          },
          {
            id: "completed-video",
            name: "张工 · 建房预算确认版.mp4",
            kind: "video",
            poster: "/studio/completed.jpg",
            group: "成片",
            source: "任务中心",
            saved: true,
          },
        ],
        tasks: [
          {
            id: "task-completed",
            title: "张工 · 建房预算确认版",
            type: "数字人口播",
            status: "completed",
            submitted: "今天 09:27",
            resultId: "completed-video",
          },
        ],
      },
    });
    useStudio.mockReturnValue(value);
    const view = render(<PublishPage />);
    expect(
      screen.getByAltText("张工 · 建房预算确认版.mp4 视频预览"),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("发布标题"), {
      target: { value: "乡墅建房预算避坑清单" },
    });
    fireEvent.change(screen.getByLabelText("添加标签"), {
      target: { value: " 建房避坑 " },
    });
    fireEvent.keyDown(screen.getByLabelText("添加标签"), { key: "Enter" });
    expect(
      screen.getByRole("button", { name: "移除标签 建房避坑" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("添加标签")).toHaveAttribute(
      "placeholder",
      "输入标签后按 Enter",
    );
    fireEvent.change(screen.getByLabelText("添加标签"), {
      target: { value: "建房避坑" },
    });
    fireEvent.keyDown(screen.getByLabelText("添加标签"), { key: "Enter" });
    expect(
      screen.getAllByRole("button", { name: "移除标签 建房避坑" }),
    ).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(value.saveDraft).not.toHaveBeenCalled();
    expect(value.state.draft.script.text).toBe("口播终稿不得被发布表单覆盖");
    expect(
      screen.getByText("已保存审核示例草稿，未同步到云端。"),
    ).toBeInTheDocument();
    expect(value.patchState).toHaveBeenCalledWith({
      publishDrafts: [
        expect.objectContaining({
          assetId: "completed-video",
          coverId: "completed-video",
          description: "主体之外，门窗、水电、防水和庭院，也要提前规划。",
          platform: "抖音",
          tags: ["农村自建房", "建房预算", "建房避坑"],
          title: "乡墅建房预算避坑清单",
        }),
      ],
    });
    const savedState = (
      value.patchState as unknown as {
        mock: { calls: Array<[Partial<StudioState>]> };
      }
    ).mock.calls.at(-1)?.[0];
    view.unmount();
    useStudio.mockReturnValue(
      studio({
        ...value,
        state: { ...value.state, ...savedState },
      }),
    );
    render(<PublishPage />);
    expect(screen.getByLabelText("发布标题")).toHaveValue(
      "乡墅建房预算避坑清单",
    );
    expect(screen.getByLabelText("发布描述")).toHaveValue(
      "主体之外，门窗、水电、防水和庭院，也要提前规划。",
    );
    expect(
      screen.getByRole("button", { name: "移除标签 农村自建房" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "移除标签 建房避坑" }),
    ).toBeInTheDocument();
    expect(value.state.draft.script.text).toBe("口播终稿不得被发布表单覆盖");
    // Review mode never submits a real delivery.
    expect(screen.getByRole("button", { name: "立即发布" })).toBeDisabled();
  });

  it("发布草稿使用云端版本保存，失败保留表单且不提示成功", async () => {
    const base = studio();
    const value = studio({
      review: false,
      state: { ...base.state, selectedAssetId: "ready-video" },
      data: {
        ...base.data,
        tasks: [],
        assets: [
          {
            id: "ready-video",
            assetId: "ready-video",
            name: "成片",
            kind: "video",
            group: "成片",
            source: "任务中心",
            saved: true,
          },
        ],
      },
    });
    getStudioDraft.mockResolvedValueOnce({
      revision: 7,
      payload: { drafts: [] },
    });
    saveStudioDraft.mockRejectedValueOnce(
      new Error("云端草稿已在其他窗口更新"),
    );
    useStudio.mockReturnValue(value);
    render(<PublishPage />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "保存草稿" })).toBeEnabled(),
    );
    fireEvent.change(screen.getByLabelText("发布标题"), {
      target: { value: "新标题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await screen.findByText("云端草稿已在其他窗口更新");
    expect(saveStudioDraft).toHaveBeenCalledWith(
      "publishing",
      {
        drafts: [
          expect.objectContaining({ title: "新标题", assetId: "ready-video" }),
        ],
      },
      false,
      7,
    );
    expect(screen.getByLabelText("发布标题")).toHaveValue("新标题");
    expect(screen.queryByText("已保存到云端，可在刷新后继续编辑。")).toBeNull();
    saveStudioDraft.mockResolvedValueOnce({ revision: 8 });
    fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await screen.findByText("已保存到云端，可在刷新后继续编辑。");
    expect(value.patchState).toHaveBeenLastCalledWith({
      publishDrafts: [expect.objectContaining({ title: "新标题" })],
    });
  });

  it.each([
    [true, true],
    [false, true],
    [true, false],
    [false, false],
  ])(
    "历史发布成片按素材接口确认权限（可访问：%s，已有任务：%s）",
    async (available, hasRecentTask) => {
      const base = studio();
      const value = studio({
        review: false,
        state: { ...base.state, selectedAssetId: "historical-video" },
        data: {
          ...base.data,
          tasks: hasRecentTask
            ? [
                {
                  id: "new-task",
                  title: "近期成片",
                  type: "数字人口播",
                  status: "completed",
                  submitted: "今天",
                  resultId: "new-video",
                },
              ]
            : [],
          assets: [
            {
              id: "historical-video",
              name: "历史成片",
              kind: "video",
              group: "成片",
              source: "任务中心",
              saved: true,
              allowedActions: ["download"],
            },
          ],
        },
      });
      getStudioDraft.mockResolvedValueOnce({
        revision: 3,
        payload: {
          drafts: [
            {
              id: "old-draft",
              assetId: "historical-video",
              coverId: "asset:historical-cover",
              title: "旧发布草稿",
              description: "正文",
              account: "",
              platform: "抖音",
              tags: [],
            },
          ],
        },
      });
      resolveMaterials.mockResolvedValueOnce({
        items: available
          ? [
              material("historical-video", {
                media_type: "video",
                content_type: "video/mp4",
              }),
            ]
          : [],
      });
      useStudio.mockReturnValue(value);
      render(<PublishPage />);
      await waitFor(() =>
        expect(value.patchState).toHaveBeenCalledWith({
          publishDrafts: [expect.objectContaining({ id: "old-draft" })],
        }),
      );
      expect(resolveMaterials).toHaveBeenCalledWith([
        "asset:historical-video",
        "asset:historical-cover",
      ]);
      if (available) {
        const update = vi.mocked(value.updateData).mock.calls.at(-1)?.[0];
        const next = (update as (data: StudioData) => StudioData)(value.data);
        expect(
          next.assets.find((asset) => asset.id === "historical-video")?.url,
        ).toBe("https://storage.test/material");
      }
      const save = screen.getByRole("button", { name: "保存草稿" });
      if (available) expect(save).toBeEnabled();
      else expect(save).toBeDisabled();
    },
  );

  it("正式模式不伪造已发布数量或已连接账号", async () => {
    const base = studio();
    const value = studio({
      review: false,
      state: { ...base.state, selectedAssetId: "completed-video" },
      data: {
        ...base.data,
        assets: [
          ...base.data.assets,
          {
            id: "completed-video",
            name: "乡墅建房预算确认版.mp4",
            kind: "video",
            poster: "/studio/completed.jpg",
            group: "成片",
            source: "任务中心",
            saved: true,
          },
        ],
        tasks: [
          {
            id: "task-completed",
            title: "乡墅建房预算确认版",
            type: "数字人口播",
            status: "completed",
            submitted: "今天 09:27",
            resultId: "completed-video",
          },
        ],
      },
    });
    useStudio.mockReturnValue(value);
    render(<PublishPage />);

    expect(
      screen.getByText(
        "选择成片与账号，立即发布或定时发布；发布结果在下方记录中回收",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("草稿保存在云端，可反复编辑")).toBeInTheDocument();
    expect(screen.queryByText(/待发布\s*0/)).not.toBeInTheDocument();
    expect(screen.queryByText(/已发布\s*0/)).not.toBeInTheDocument();
    await screen.findByText("尚未连接该平台账号，请先扫码连接");
    await screen.findByText("暂无发布记录");
    expect(screen.getByRole("button", { name: "立即发布" })).toBeDisabled();
    fireEvent.click(
      screen.getByRole("button", { name: "前往用户档案管理账号" }),
    );
    expect(value.navigate).toHaveBeenCalledWith("profile");
  });

  it("选择服务端账号后可立即发布或按时区换算的定时发布", async () => {
    createPublishRecord.mockReset();
    const base = studio();
    const value = studio({
      review: false,
      state: { ...base.state, selectedAssetId: "ready-video" },
      data: {
        ...base.data,
        tasks: [],
        assets: [
          {
            id: "ready-video",
            assetId: "ready-video",
            materialId: "asset:ready-video",
            name: "成片",
            kind: "video",
            group: "成片",
            source: "任务中心",
            saved: true,
          },
        ],
      },
    });
    publishAccounts.listCloudPublishAccounts.mockResolvedValue([
      {
        id: "cloud-douyin",
        platform: "douyin",
        platform_user_id: "uid-1",
        username: "张工说乡墅",
        verified_at: 1,
        status: "connected",
        error_message: null,
        source: "cloud",
      },
      {
        id: "cloud-dead",
        platform: "douyin",
        platform_user_id: "uid-2",
        username: "失效账号",
        verified_at: 1,
        status: "invalid",
        error_message: "登录过期",
        source: "desktop",
      },
    ]);
    getStudioDraft.mockResolvedValueOnce({
      revision: 3,
      payload: { drafts: [] },
    });
    createPublishRecord.mockResolvedValue({
      id: "rec-1",
      platform: "douyin",
      scheduled_at: null,
      status: "queued",
    });
    useStudio.mockReturnValue(value);
    render(<PublishPage />);
    const select = await screen.findByLabelText("选择发布账号");
    fireEvent.change(select, { target: { value: "cloud-dead" } });
    expect(
      screen.getByText("该账号登录态已失效，请在用户档案中重新扫码后再发布。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "立即发布" })).toBeDisabled();
    fireEvent.change(select, { target: { value: "cloud-douyin" } });
    fireEvent.change(screen.getByLabelText("发布标题"), {
      target: { value: "立即发的标题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "立即发布" }));
    await waitFor(() =>
      expect(createPublishRecord).toHaveBeenCalledWith({
        account_id: "cloud-douyin",
        video_material_id: "asset:ready-video",
        cover_material_id: null,
        title: "立即发的标题",
        description: "",
        tags: [],
        scheduled_at: null,
      }),
    );
    expect(value.notify).toHaveBeenCalledWith("已提交发布 · 抖音");
    expect(listPublishRecords).toHaveBeenCalled();

    // Scheduled: the datetime-local value is local wall-clock time → ISO instant.
    fireEvent.click(screen.getByRole("button", { name: "定时" }));
    const timeInput = screen.getByLabelText("定时发布时间");
    fireEvent.change(timeInput, { target: { value: "2020-01-01T10:00" } });
    fireEvent.click(screen.getByRole("button", { name: "定时发布" }));
    await screen.findByText("定时发布至少需要提前 2 分钟。");
    expect(createPublishRecord).toHaveBeenCalledTimes(1);
    const future = new Date(Date.now() + 3 * 60 * 60 * 1000);
    future.setSeconds(0, 0);
    const pad = (n: number) => String(n).padStart(2, "0");
    const local = `${future.getFullYear()}-${pad(future.getMonth() + 1)}-${pad(future.getDate())}T${pad(future.getHours())}:${pad(future.getMinutes())}`;
    fireEvent.change(timeInput, { target: { value: local } });
    createPublishRecord.mockResolvedValueOnce({
      id: "rec-2",
      platform: "douyin",
      scheduled_at: future.toISOString(),
      status: "queued",
    });
    fireEvent.click(screen.getByRole("button", { name: "定时发布" }));
    await waitFor(() => expect(createPublishRecord).toHaveBeenCalledTimes(2));
    expect(createPublishRecord.mock.calls[1][0].scheduled_at).toBe(
      future.toISOString(),
    );
    expect(value.notify).toHaveBeenCalledWith("已加入定时发布队列 · 抖音");
  });

  it("小红书账号只能保存草稿与前往官方发布，不提交自动发布", async () => {
    createPublishRecord.mockReset();
    const base = studio();
    const value = studio({
      review: false,
      state: { ...base.state, selectedAssetId: "ready-video" },
      data: {
        ...base.data,
        tasks: [],
        assets: [
          {
            id: "ready-video",
            assetId: "ready-video",
            name: "成片",
            kind: "video",
            group: "成片",
            source: "任务中心",
            saved: true,
          },
        ],
      },
    });
    publishAccounts.listCloudPublishAccounts.mockResolvedValue([
      {
        id: "cloud-xhs",
        platform: "xiaohongshu",
        platform_user_id: "uid-x",
        username: "小红书号",
        verified_at: 1,
        status: "connected",
        error_message: null,
        source: "cloud",
      },
    ]);
    getStudioDraft.mockResolvedValueOnce({
      revision: 1,
      payload: { drafts: [] },
    });
    useStudio.mockReturnValue(value);
    render(<PublishPage />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "保存草稿" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: /小红书/ }));
    const select = await screen.findByLabelText("选择发布账号");
    fireEvent.change(select, { target: { value: "cloud-xhs" } });
    expect(screen.getByText(/小红书自动发布即将上线/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "立即发布" })).toBeDisabled();
    expect(createPublishRecord).not.toHaveBeenCalled();
  });

  it("直接进入发布页但未选择完成视频时不随机回退预览", () => {
    const base = studio();
    useStudio.mockReturnValue(
      studio({
        state: { ...base.state, selectedAssetId: "other-video" },
        data: {
          ...base.data,
          assets: [
            ...base.data.assets,
            {
              id: "other-video",
              name: "仍在生成的视频.mp4",
              kind: "video",
              poster: "/studio/other.jpg",
              group: "成片",
              source: "任务中心",
              saved: true,
            },
            {
              id: "completed-video",
              name: "不可随机回退的完成视频.mp4",
              kind: "video",
              poster: "/studio/completed.jpg",
              group: "成片",
              source: "任务中心",
              saved: true,
            },
          ],
          tasks: [
            {
              id: "task-completed",
              title: "不可随机回退的完成视频",
              type: "数字人口播",
              status: "completed",
              submitted: "今天 09:27",
              resultId: "completed-video",
            },
          ],
        },
      }),
    );
    render(<PublishPage />);

    expect(screen.getByText("暂无可发布成片")).toBeInTheDocument();
    expect(
      screen.queryByAltText("不可随机回退的完成视频.mp4 视频预览"),
    ).toBeNull();
    expect(screen.getByRole("button", { name: "保存草稿" })).toBeDisabled();
  });
});
