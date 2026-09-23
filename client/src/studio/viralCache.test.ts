import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  awaitViralCacheReady,
  CACHE_BADGE_LABELS,
  cacheAvailable,
  cacheProgressRatio,
  ensureViralCache,
  formatCacheBytes,
  listViralCache,
  listViralCacheTasks,
  pauseViralCache,
  resumeViralCache,
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
    it("覆盖决策 #16 的四个状态", () => {
      expect(Object.keys(CACHE_BADGE_LABELS).sort()).toEqual([
        "cached",
        "downloading",
        "failed",
        "paused",
        "queued",
      ]);
      expect(CACHE_BADGE_LABELS.failed).toContain("可重试");
      expect(CACHE_BADGE_LABELS.paused).toBe("已暂停");
    });
  });

  describe("下载管理命令", () => {
    beforeEach(() => {
      core.isTauri.mockReturnValue(true);
      core.invoke.mockReset();
    });
    afterEach(() => {
      core.isTauri.mockReturnValue(false);
    });

    it("读取任务快照并透传错误与速度", async () => {
      const failed = cacheProgress("failed", "源站返回 403");
      core.invoke.mockResolvedValue([failed]);
      await expect(listViralCacheTasks()).resolves.toEqual([failed]);
      expect(core.invoke).toHaveBeenCalledWith("viral_cache_tasks");
    });

    it("暂停与继续只传任务身份，不接触或刷新计费直链", async () => {
      core.invoke.mockResolvedValue(undefined);
      await pauseViralCache("wechat_channels", "opaque/id");
      await resumeViralCache("wechat_channels", "opaque/id");
      expect(core.invoke).toHaveBeenNthCalledWith(1, "viral_cache_pause", {
        platform: "wechat_channels",
        videoId: "opaque/id",
      });
      expect(core.invoke).toHaveBeenNthCalledWith(2, "viral_cache_resume", {
        platform: "wechat_channels",
        videoId: "opaque/id",
      });
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
