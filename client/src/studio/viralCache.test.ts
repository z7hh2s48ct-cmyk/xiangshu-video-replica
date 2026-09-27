import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  awaitViralCacheReady,
  CACHE_BADGE_LABELS,
  cacheAvailable,
  cacheProgressRatio,
  ensureViralArchiveCache,
  ensureViralCache,
  formatCacheBytes,
  isViralMaterialUnready,
  listViralCache,
  reclaimViralCache,
  useViralAutoCache,
  useViralCacheProgress,
  viralCacheKey,
} from "./viralCache";

// 桌面端判定默认仍是 false（jsdom 里就是如此），需要验证完整缓存轮询的用例自行打开。
// 只覆写真正碰 Tauri 的三个入口，其余沿用真实实现。
const core = vi.hoisted(() => ({
  isTauri: vi.fn(() => false),
  invoke: vi.fn<(...args: unknown[]) => Promise<unknown>>(
    async () => undefined,
  ),
}));
vi.mock("@tauri-apps/api/core", () => ({
  ...core,
  convertFileSrc: (path: string) => `asset://localhost/${path}`,
}));

// 只桩掉取归档地址这一个调用：入队与轮询走真实实现，才能验证「归档 -> 下载队列」的接线。
const api = vi.hoisted(() => ({
  fetchViralVideoMedia:
    vi.fn<
      (platform: string, videoId: string, kind?: string) => Promise<unknown>
    >(),
  fetchViralCacheReclaim:
    vi.fn<(platform: string, videoIds: string[]) => Promise<unknown>>(),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  ...api,
}));

function cacheProgress(state: string, error: string | null = null) {
  return {
    platform: "douyin",
    videoId: "v-1",
    state,
    downloadedBytes: 1,
    totalBytes: 10,
    speedBytesPerSecond: 1,
    error,
  };
}

describe("viralCache 桥接", () => {
  describe("viralCacheKey", () => {
    it("把平台与视频 ID 编成一个键", () => {
      expect(viralCacheKey("douyin", "v-1")).toBe("douyin:v-1");
    });

    it("同 ID 不同平台互不覆盖", () => {
      // 两个平台的 video_id 各自独立编号，只按 id 作键会让它们互相顶掉。
      expect(viralCacheKey("douyin", "1")).not.toBe(
        viralCacheKey("wechat_channels", "1"),
      );
    });
  });

  describe("cacheProgressRatio", () => {
    it("按总长度换算比例", () => {
      expect(cacheProgressRatio({ downloadedBytes: 50, totalBytes: 100 })).toBe(
        0.5,
      );
      expect(cacheProgressRatio({ downloadedBytes: 0, totalBytes: 100 })).toBe(
        0,
      );
      expect(
        cacheProgressRatio({ downloadedBytes: 100, totalBytes: 100 }),
      ).toBe(1);
    });

    it("总长度未知时返回 null 而不是 0", () => {
      // 「不知道总长度」和「刚开始下载」在界面上是两回事：前者画不确定态，
      // 后者画 0%。统一返回 0 会让进度条在拿到总长前一直假装没开始。
      expect(
        cacheProgressRatio({ downloadedBytes: 30, totalBytes: null }),
      ).toBeNull();
      expect(
        cacheProgressRatio({ downloadedBytes: 0, totalBytes: 0 }),
      ).toBeNull();
    });

    it("比例被夹在 [0,1]，源站给错总长也不画出溢出进度条", () => {
      expect(
        cacheProgressRatio({ downloadedBytes: 150, totalBytes: 100 }),
      ).toBe(1);
      expect(cacheProgressRatio({ downloadedBytes: 0, totalBytes: 100 })).toBe(
        0,
      );
    });
  });

  describe("角标文案", () => {
    it("覆盖决策 #16 的四个状态，外加前端专属的「素材未就绪」", () => {
      expect(Object.keys(CACHE_BADGE_LABELS).sort()).toEqual([
        "cached",
        "downloading",
        "failed",
        "queued",
        "unready",
      ]);
      expect(CACHE_BADGE_LABELS.failed).toContain("可重试");
      // 归档未就绪不是失败：文案不能带「失败」，否则又回到用户看到
      // 「缓存失败（可重试）」的那条老路上。
      expect(CACHE_BADGE_LABELS.unready).not.toContain("失败");
    });
  });

  describe("isViralMaterialUnready", () => {
    it("归档未就绪与存储不可用算「未就绪」，其余错误照旧算失败", () => {
      expect(
        isViralMaterialUnready({ code: "VIRAL_MEDIA_PREPARATION_BUSY" }),
      ).toBe(true);
      expect(
        isViralMaterialUnready({ code: "STORAGE_BACKEND_UNAVAILABLE" }),
      ).toBe(true);
      expect(isViralMaterialUnready({ code: "VIRAL_MEDIA_FORBIDDEN" })).toBe(
        false,
      );
      expect(isViralMaterialUnready(new Error("网络中断"))).toBe(false);
      expect(isViralMaterialUnready(undefined)).toBe(false);
    });
  });

  describe("非桌面端降级（§13-2）", () => {
    it("测试环境不在 Tauri 内，缓存能力应判定为不可用", () => {
      expect(cacheAvailable()).toBe(false);
    });

    it("Web 端调用缓存接口必须静默跳过而不是抛错", async () => {
      // Web 端能正常浏览爆款列表，只是不做缓存/播放；这里若抛错，
      // 浏览路径会一路弹错。
      await expect(
        ensureViralCache({
          platform: "douyin",
          videoId: "v-1",
          url: "https://cdn.example/a.mp4",
        }),
      ).resolves.toBeUndefined();
      await expect(listViralCache()).resolves.toEqual([]);
    });
  });

  describe("useViralCacheProgress", () => {
    it("非桌面端返回空状态且不抛错", () => {
      const { result } = renderHook(() =>
        useViralCacheProgress([{ platform: "douyin", videoId: "v-1" }]),
      );
      expect(result.current.size).toBe(0);
    });

    it("空列表也不得抛错（浏览列表为空是常态）", () => {
      const { result } = renderHook(() => useViralCacheProgress([]));
      expect(result.current.size).toBe(0);
    });
  });

  describe("formatCacheBytes", () => {
    it("按单位换算，字节不带小数、其余保留一位", () => {
      expect(formatCacheBytes(0)).toBe("0 B");
      expect(formatCacheBytes(512)).toBe("512 B");
      // 1024 就该进位成 KB，否则面板上会出现 "1024 B" 这种读起来别扭的值。
      expect(formatCacheBytes(1024)).toBe("1.0 KB");
      expect(formatCacheBytes(1536)).toBe("1.5 KB");
      expect(formatCacheBytes(20 * 1024 ** 3)).toBe("20.0 GB");
    });

    it("非法值一律显示 0 B，绝不把 NaN 摆到界面上", () => {
      expect(formatCacheBytes(-5)).toBe("0 B");
      expect(formatCacheBytes(Number.NaN)).toBe("0 B");
      expect(formatCacheBytes(Number.POSITIVE_INFINITY)).toBe("0 B");
    });
  });
});

describe("awaitViralCacheReady", () => {
  beforeEach(() => {
    core.isTauri.mockReturnValue(true);
    core.invoke.mockReset();
  });
  afterEach(() => {
    core.isTauri.mockReturnValue(false);
  });

  it("已缓存时立即返回，不白等一轮", async () => {
    core.invoke.mockResolvedValue([cacheProgress("cached")]);
    await expect(
      awaitViralCacheReady("douyin", "v-1"),
    ).resolves.toBeUndefined();
    expect(core.invoke).toHaveBeenCalledWith("viral_cache_status", {
      items: [{ platform: "douyin", videoId: "v-1" }],
    });
  });

  it("缓存失败时抛出源站原因，而不是当成「还没好」继续等", async () => {
    core.invoke.mockResolvedValue([cacheProgress("failed", "源站返回 403")]);
    await expect(awaitViralCacheReady("douyin", "v-1")).rejects.toThrow(
      "源站返回 403",
    );
  });

  it("超时仍未缓存时给出可读提示，而不是一直挂着", async () => {
    core.invoke.mockResolvedValue([cacheProgress("downloading")]);
    await expect(
      awaitViralCacheReady("douyin", "v-1", { timeoutMs: 0 }),
    ).rejects.toThrow("尚未缓存完成");
  });

  it("非桌面端直接拒绝：Web 端没有本地缓存这条链路", async () => {
    core.isTauri.mockReturnValue(false);
    await expect(awaitViralCacheReady("douyin", "v-1")).rejects.toThrow(
      "需要桌面端",
    );
    expect(core.invoke).not.toHaveBeenCalled();
  });
});

describe("归档数据源（缓存不再依赖会过期的源站直链）", () => {
  beforeEach(() => {
    core.invoke.mockReset();
    core.invoke.mockResolvedValue(undefined);
    api.fetchViralVideoMedia.mockReset();
    api.fetchViralVideoMedia.mockResolvedValue({
      kind: "video",
      url: "https://archive.test/viral/douyin/native-1.mp4?sign=abc",
      contentType: "video/mp4",
      cacheHit: true,
    });
    core.isTauri.mockReturnValue(true);
  });
  afterEach(() => {
    core.isTauri.mockReturnValue(false);
  });

  it("先取归档地址再入队，视频号同样走归档（无需密钥、无需计费直链）", async () => {
    await ensureViralArchiveCache("wechat_channels", "wx/1");
    // 只有 kind=video 的归档件是播放/抽音轨要用的原件。
    expect(api.fetchViralVideoMedia).toHaveBeenCalledWith(
      "wechat_channels",
      "wx/1",
      "video",
    );
    // 归档件在入云前就完成了解密，因此这里不能带 decodeKey（带了反而会被误解为加密流）。
    expect(core.invoke).toHaveBeenCalledWith("viral_cache_ensure", {
      platform: "wechat_channels",
      videoId: "wx/1",
      url: "https://archive.test/viral/douyin/native-1.mp4?sign=abc",
      decodeKey: null,
    });
  });

  it("归档未就绪时把服务端错误原样抛出，交给上层判定是否「未就绪」", async () => {
    api.fetchViralVideoMedia.mockRejectedValue({
      code: "VIRAL_MEDIA_PREPARATION_BUSY",
    });
    await expect(
      ensureViralArchiveCache("douyin", "native-1"),
    ).rejects.toMatchObject({ code: "VIRAL_MEDIA_PREPARATION_BUSY" });
    expect(core.invoke).not.toHaveBeenCalled();
  });

  it("非桌面端一次外呼都不发（Web 端只浏览）", async () => {
    core.isTauri.mockReturnValue(false);
    await ensureViralArchiveCache("douyin", "native-1");
    expect(api.fetchViralVideoMedia).not.toHaveBeenCalled();
    expect(core.invoke).not.toHaveBeenCalled();
  });
});

describe("useViralAutoCache（翻页即缓存）", () => {
  beforeEach(() => {
    core.invoke.mockReset();
    core.invoke.mockResolvedValue(undefined);
    api.fetchViralVideoMedia.mockReset();
    core.isTauri.mockReturnValue(true);
  });
  afterEach(() => {
    core.isTauri.mockReturnValue(false);
  });

  it("本页每个条目都按归档入队，视频号不再因为「没有直链」被跳过", async () => {
    api.fetchViralVideoMedia.mockImplementation(async (platform, videoId) => ({
      kind: "video",
      url: `https://archive.test/${platform}/${videoId}.mp4`,
      contentType: "video/mp4",
      cacheHit: true,
    }));
    const { result } = renderHook(() =>
      useViralAutoCache([
        { platformKey: "douyin", nativeId: "native-1" },
        { platformKey: "wechat_channels", nativeId: "wx-1" },
      ]),
    );
    await waitFor(() =>
      expect(api.fetchViralVideoMedia).toHaveBeenCalledTimes(2),
    );
    expect(result.current.size).toBe(0);
    expect(core.invoke).toHaveBeenCalledWith("viral_cache_ensure", {
      platform: "wechat_channels",
      videoId: "wx-1",
      url: "https://archive.test/wechat_channels/wx-1.mp4",
      decodeKey: null,
    });
  });

  it("归档未就绪时给出「素材未就绪」而不是「缓存失败」，并允许下次重试", async () => {
    api.fetchViralVideoMedia
      .mockRejectedValueOnce({ code: "VIRAL_MEDIA_PREPARATION_BUSY" })
      .mockResolvedValueOnce({
        kind: "video",
        url: "https://archive.test/douyin/native-1.mp4",
        contentType: "video/mp4",
        cacheHit: true,
      });
    const videos = [{ platformKey: "douyin", nativeId: "native-1" }];
    const { result, rerender } = renderHook(
      ({ items }) => useViralAutoCache(items),
      { initialProps: { items: videos } },
    );
    await waitFor(() =>
      expect(result.current.get("douyin:native-1")?.state).toBe("unready"),
    );
    expect(result.current.get("douyin:native-1")?.error).toContain("尚未就绪");

    // 再进一次页面（列表引用变化）会用新地址重试，成功后合成状态让位给真实进度。
    rerender({ items: [...videos] });
    await waitFor(() =>
      expect(result.current.has("douyin:native-1")).toBe(false),
    );
  });

  it("取地址失败但仍算失败时照旧标「缓存失败（可重试）」", async () => {
    api.fetchViralVideoMedia.mockRejectedValue({
      code: "VIRAL_MEDIA_FORBIDDEN",
    });
    const { result } = renderHook(() =>
      useViralAutoCache([{ platformKey: "douyin", nativeId: "native-1" }]),
    );
    await waitFor(() =>
      expect(result.current.get("douyin:native-1")?.state).toBe("failed"),
    );
  });
});

describe("reclaimViralCache（删除 / 下架的条目连带清本地缓存）", () => {
  function cacheItem(platform: string, videoId: string) {
    return { platform, videoId, bytes: 1024 };
  }

  beforeEach(() => {
    core.isTauri.mockReturnValue(true);
    core.invoke.mockReset();
    core.invoke.mockResolvedValue(undefined);
    api.fetchViralVideoMedia.mockReset();
    api.fetchViralCacheReclaim.mockReset();
    api.fetchViralCacheReclaim.mockResolvedValue({
      platform: "douyin",
      reclaim: [],
    });
  });

  afterEach(() => {
    core.isTauri.mockReturnValue(false);
  });

  function listReturns(items: unknown[]) {
    // invoke 的桩签名是 (...args: unknown[])，这里按实参取命令名，避免窄化形参。
    core.invoke.mockImplementation(async (...args: unknown[]) =>
      args[0] === "viral_cache_list" ? items : undefined,
    );
  }

  it("只上报不在保留集里的条目，并按服务端判定删掉本地文件", async () => {
    listReturns([
      cacheItem("douyin", "keep"),
      cacheItem("douyin", "gone"),
      // 别的平台不参与本次回收：平台之间 ID 各自编号，混着报会误删。
      cacheItem("wechat_channels", "other-platform"),
    ]);
    api.fetchViralCacheReclaim.mockResolvedValue({
      platform: "douyin",
      reclaim: ["gone"],
    });
    const reclaimed = await reclaimViralCache("douyin", [
      { platform: "douyin", videoId: "keep" },
    ]);
    expect(api.fetchViralCacheReclaim).toHaveBeenCalledWith("douyin", ["gone"]);
    expect(reclaimed).toEqual(["gone"]);
    expect(
      core.invoke.mock.calls.filter(
        ([command]) => command === "viral_cache_delete",
      ),
    ).toEqual([
      ["viral_cache_delete", { platform: "douyin", videoId: "gone" }],
    ]);
  });

  it("服务端判为保留的条目不删（判据在服务端，客户端不自行决定）", async () => {
    listReturns([cacheItem("douyin", "still-live")]);
    api.fetchViralCacheReclaim.mockResolvedValue({
      platform: "douyin",
      reclaim: [],
    });
    await expect(reclaimViralCache("douyin")).resolves.toEqual([]);
    expect(
      core.invoke.mock.calls.filter(
        ([command]) => command === "viral_cache_delete",
      ),
    ).toEqual([]);
  });

  it("没有候选条目时不发请求（空列表刷新是常态）", async () => {
    listReturns([]);
    await expect(reclaimViralCache("douyin", [])).resolves.toEqual([]);
    expect(api.fetchViralCacheReclaim).not.toHaveBeenCalled();
  });

  it("超过服务端单批上限时自行分批，不把超长请求打过去", async () => {
    listReturns(
      Array.from({ length: 201 }, (_, index) =>
        cacheItem("douyin", `v-${index}`),
      ),
    );
    await reclaimViralCache("douyin");
    expect(api.fetchViralCacheReclaim).toHaveBeenCalledTimes(2);
    expect(api.fetchViralCacheReclaim.mock.calls[0][1]).toHaveLength(200);
    expect(api.fetchViralCacheReclaim.mock.calls[1][1]).toHaveLength(1);
  });

  it("回收失败与本地清单读取失败都静默：清理不该打断浏览", async () => {
    listReturns([cacheItem("douyin", "gone")]);
    api.fetchViralCacheReclaim.mockRejectedValue(new Error("网络中断"));
    await expect(reclaimViralCache("douyin")).resolves.toEqual([]);
    core.invoke.mockRejectedValue(new Error("缓存目录不可读"));
    await expect(reclaimViralCache("douyin")).resolves.toEqual([]);
  });

  it("非桌面端一次外呼都不发（Web 端没有本地缓存）", async () => {
    core.isTauri.mockReturnValue(false);
    await expect(reclaimViralCache("douyin")).resolves.toEqual([]);
    expect(core.invoke).not.toHaveBeenCalled();
    expect(api.fetchViralCacheReclaim).not.toHaveBeenCalled();
  });
});
