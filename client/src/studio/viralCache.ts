import { convertFileSrc, invoke, isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useEffect, useRef, useState } from "react";
import { fetchViralVideoSource, type ViralPlatform } from "../api";

/**
 * 爆款视频本地缓存（P2）的桌面端桥接。
 *
 * 设计约束：
 * - §6.2 索引即文件系统，缓存状态由 Rust 侧按文件系统回答，这里不缓存业务字段。
 * - §13-2 Web 端降级：非桌面端不做缓存/播放，直接静默跳过（不能报错，否则
 *   Web 端浏览爆款列表会一路弹错）。
 * - 进度事件驱动、不轮询。
 */

export type ViralCacheState = "queued" | "downloading" | "cached" | "failed";

export type ViralCacheProgress = {
  platform: string;
  videoId: string;
  state: ViralCacheState;
  downloadedBytes: number;
  totalBytes: number | null;
  speedBytesPerSecond: number;
  error: string | null;
};

export type ViralCachedItem = {
  platform: string;
  videoId: string;
  bytes: number;
};

export type ViralCacheKeyInput = { platform: string; videoId: string };

/** 状态键：平台 + 视频 ID。视频号 id 可能含 `/`，因此不做路径拼接用途。 */
export function viralCacheKey(platform: string, videoId: string): string {
  return `${platform}:${videoId}`;
}

/**
 * 进度比例；总长度未知时返回 `null`。
 *
 * 返回 `null` 而不是 0：未知长度与「刚开始」在界面上是两回事，
 * 前者该画不确定态，后者该画 0%。
 */
export function cacheProgressRatio(
  progress: Pick<ViralCacheProgress, "downloadedBytes" | "totalBytes">,
): number | null {
  const total = progress.totalBytes;
  if (total === null || total <= 0) return null;
  const ratio = progress.downloadedBytes / total;
  return Math.max(0, Math.min(1, ratio));
}

/** 角标文案（决策 #16：排队中 / 缓存中 / 已缓存 / 缓存失败）。 */
export const CACHE_BADGE_LABELS: Record<ViralCacheState, string> = {
  queued: "排队中",
  downloading: "缓存中",
  cached: "已缓存",
  failed: "缓存失败（可重试）",
};

/** 只有桌面端有本地缓存能力（§13-2）。 */
export function cacheAvailable(): boolean {
  return isTauri();
}

/**
 * 入队下载。已在本地则 Rust 侧直接返回。
 *
 * `decodeKey` 必须与 `url` 来自同一次服务端响应（视频号密钥每次请求都会变），
 * 因此这里只做透传，绝不自行拼凑。
 */
export async function ensureViralCache(input: {
  platform: string;
  videoId: string;
  url: string;
  decodeKey?: string | null;
}): Promise<void> {
  if (!cacheAvailable()) return;
  await invoke("viral_cache_ensure", {
    platform: input.platform,
    videoId: input.videoId,
    url: input.url,
    decodeKey: input.decodeKey ?? null,
  });
}

/** 批量查缓存状态（页面角标用）。非桌面端一律返回空，界面按「不可用」处理。 */
export async function viralCacheStatus(
  items: ViralCacheKeyInput[],
): Promise<ViralCacheProgress[]> {
  if (!cacheAvailable() || items.length === 0) return [];
  return invoke<ViralCacheProgress[]>("viral_cache_status", { items });
}

export async function listViralCache(): Promise<ViralCachedItem[]> {
  if (!cacheAvailable()) return [];
  return invoke<ViralCachedItem[]>("viral_cache_list");
}

export async function deleteViralCache(
  platform: string,
  videoId: string,
): Promise<void> {
  if (!cacheAvailable()) return;
  await invoke("viral_cache_delete", { platform, videoId });
}

export async function clearViralCache(
  scope: "all" | "platform",
  platform?: string,
): Promise<void> {
  if (!cacheAvailable()) return;
  await invoke("viral_cache_clear", { scope, platform: platform ?? null });
}

export async function openViralCacheFolder(): Promise<void> {
  if (!cacheAvailable()) return;
  await invoke("viral_cache_open_folder");
}

/**
 * 订阅缓存进度事件，返回取消订阅函数。
 *
 * 非桌面端返回的取消函数是空操作，调用方无需分支。
 */
export async function listenViralCacheProgress(
  handler: (progress: ViralCacheProgress) => void,
): Promise<() => void> {
  if (!cacheAvailable()) return () => {};
  const unlisten = await listen<ViralCacheProgress>(
    "viral-cache-progress",
    (event) => handler(event.payload),
  );
  return unlisten;
}

/** 抽出已缓存视频的音轨（m4a 字节），供文案转写上传。 */
export async function extractViralAudio(
  platform: string,
  videoId: string,
): Promise<Uint8Array> {
  if (!cacheAvailable()) {
    throw new Error("提取文案需要桌面端；Web 端暂不支持本地缓存。");
  }
  const bytes = await invoke<ArrayBuffer | number[]>("viral_extract_audio", {
    platform,
    videoId,
  });
  return bytes instanceof ArrayBuffer
    ? new Uint8Array(bytes)
    : Uint8Array.from(bytes);
}

/**
 * 等到本地缓存就绪，返回后 `extractViralAudio` 才拿得到文件。
 *
 * `viral_cache_ensure` 是**入队即返回**的（真正下载交给后台队列，界面靠事件更新
 * 进度），所以「先 ensure 再抽音轨」之间必须自己等一次；否则未缓存过的视频会在
 * 抽音轨那一步直接报「尚未缓存到本地」。进度角标由 `useViralCacheProgress` 的
 * 事件照常渲染，这里只负责不抢跑。
 */
export async function awaitViralCacheReady(
  platform: string,
  videoId: string,
  {
    timeoutMs = 300_000,
    intervalMs = 500,
  }: { timeoutMs?: number; intervalMs?: number } = {},
): Promise<void> {
  if (!cacheAvailable()) {
    throw new Error("提取文案需要桌面端；Web 端暂不支持本地缓存。");
  }
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const [status] = await viralCacheStatus([{ platform, videoId }]);
    if (status?.state === "cached") return;
    if (status?.state === "failed") {
      throw new Error(status.error || "视频缓存失败，请重试后再提取文案。");
    }
    if (Date.now() >= deadline) {
      throw new Error("视频尚未缓存完成，请等缓存结束后再提取文案。");
    }
    await new Promise((resolve) => window.setTimeout(resolve, intervalMs));
  }
}

/**
 * 已缓存视频的可播放地址（asset 协议）；未缓存返回 null。
 *
 * 走 asset 协议而不是直接把文件路径交给 `<video>`：WebView 读不了 `file://`。
 * 该协议与 scope 在 tauri.conf.json 里限定为 `$APPDATA/viral-cache/**`，因此
 * 即使前端拿到别的路径也放不了——播放能力被限制在缓存目录内。
 */
export async function viralCacheLocalUrl(
  platform: string,
  videoId: string,
): Promise<string | null> {
  if (!cacheAvailable()) return null;
  const path = await invoke<string | null>("viral_cache_local_path", {
    platform,
    videoId,
  });
  return path ? convertFileSrc(path) : null;
}

/**
 * 按需缓存：先向服务端取直链，再入队下载。
 *
 * 视频号没有随列表下发的直链，只有走这里才能缓存——所以这是「视频号缓存到本地」
 * 的唯一入口。取直链的端点**按次计费**，因此只能在用户明确动作（播放、提取文案、
 * 点缓存）时调用，绝不能放进列表渲染路径。
 */
export async function ensureViralSourceCache(
  platform: ViralPlatform,
  videoId: string,
): Promise<void> {
  if (!cacheAvailable()) return;
  const source = await fetchViralVideoSource(platform, videoId);
  await ensureViralCache({
    platform,
    videoId,
    url: source.fullUrl,
    decodeKey: source.decodeKey,
  });
}

/** 缓存占用的人类可读文本（面板用）。1 位小数足够，别在界面上堆精度。 */
export function formatCacheBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  // 字节数不带小数：`1.0 B` 读起来比 `1 B` 别扭。
  return unit === 0 ? `${value} B` : `${value.toFixed(1)} ${units[unit]}`;
}

/**
 * 按 LRU 把缓存压回上限内（§13-1），返回释放的字节数。
 *
 * `protectedKeys` 是当前收藏列表——收藏是服务端状态，Rust 侧看不到，必须由界面
 * 带进去。传空数组不会删收藏，但会把它们一并当普通条目参与淘汰，所以调用方
 * 应当尽量传全。
 */
export async function enforceViralCacheLimit(
  protectedKeys: ViralCacheKeyInput[] = [],
  limitBytes?: number,
): Promise<number> {
  if (!cacheAvailable()) return 0;
  return invoke<number>("viral_cache_enforce_limit", {
    protected: protectedKeys,
    limitBytes: limitBytes ?? null,
  });
}

/**
 * 按需缓存指定视频（用户点播放/提取文案时的统一入口）。
 *
 * 抖音的直链随列表下发，直接入队；视频号没有直链，必须先取一次**计费**的
 * `/videos/source`。两者都不满足时静默返回，不抛错——调用方通常已经在给用户
 * 展示「尚未缓存」的说明，再弹一个错只会造成噪音。
 */
export async function ensureViralCacheForVideo(video: {
  platformKey?: string;
  nativeId?: string;
  playUrl?: string | null;
}): Promise<void> {
  if (!cacheAvailable()) return;
  const { platformKey, nativeId, playUrl } = video;
  if (!platformKey || !nativeId) return;
  if (playUrl) {
    await ensureViralCache({
      platform: platformKey,
      videoId: nativeId,
      url: playUrl,
    });
    return;
  }
  if (platformKey === "wechat_channels") {
    await ensureViralSourceCache(platformKey, nativeId);
  }
}

/** 自动缓存只关心这三个字段，因此不绑死到具体的视频类型上。 */
export type ViralCacheCandidate = {
  platformKey?: string;
  nativeId?: string;
  playUrl?: string | null;
};

/**
 * 把「已经拿到直链」的视频自动入队缓存（§6.2 翻页即缓存），重复渲染不会重复入队。
 *
 * **刻意只缓存已知直链的条目。** 抖音的 `playUrl` 随搜索结果一起下发，自动缓存
 * 不产生额外计费；而视频号没有直链，必须为每条单独调一次**计费的**
 * `/videos/source`。若照 §6.2 字面把「该页全部结果」都自动缓存，视频号翻几页
 * 就是几十次计费调用。因此视频号改为按需触发（用户点播放或提取文案时再取直链
 * 并缓存）——这是与设计稿字面的一处偏离，需要产品确认。
 */
export function useViralAutoCache(videos: ViralCacheCandidate[]): void {
  const queued = useRef(new Set<string>());
  useEffect(() => {
    if (!cacheAvailable()) return;
    for (const video of videos) {
      const platform = video.platformKey;
      const videoId = video.nativeId;
      const url = video.playUrl;
      if (!platform || !videoId || !url) continue;
      const key = viralCacheKey(platform, videoId);
      if (queued.current.has(key)) continue;
      queued.current.add(key);
      void ensureViralCache({ platform, videoId, url }).catch(() => {
        // 单条缓存失败不能打断浏览（角标会显示失败供重试）。
        // 从集合里移除，让下次进入页面还能再试。
        queued.current.delete(key);
      });
    }
  }, [videos]);
}

/**
 * 页面级缓存状态：先用 `viral_cache_status` 打底，再用进度事件增量覆盖。
 *
 * 只以「当前页视频键列表」的字符串为依赖，所以每次渲染新建数组不会重跑查询；
 * 最新列表走 ref，避免把 `items` 放进依赖数组。
 */
export function useViralCacheProgress(
  items: ViralCacheKeyInput[],
): Map<string, ViralCacheProgress> {
  const [progress, setProgress] = useState<Map<string, ViralCacheProgress>>(
    () => new Map(),
  );
  const key = items
    .map((item) => viralCacheKey(item.platform, item.videoId))
    .join(",");
  const itemsRef = useRef(items);
  itemsRef.current = items;

  useEffect(() => {
    if (!cacheAvailable() || !key) return;
    let cancelled = false;
    void viralCacheStatus(itemsRef.current)
      .then((list) => {
        if (cancelled) return;
        setProgress((previous) => {
          const next = new Map(previous);
          for (const item of list) {
            next.set(viralCacheKey(item.platform, item.videoId), item);
          }
          return next;
        });
      })
      .catch(() => {
        // 查状态失败不该影响浏览：事件到达后角标仍会补上。
      });
    return () => {
      cancelled = true;
    };
  }, [key]);

  useEffect(() => {
    let stop: (() => void) | undefined;
    let cancelled = false;
    void listenViralCacheProgress((item) => {
      setProgress((previous) =>
        new Map(previous).set(viralCacheKey(item.platform, item.videoId), item),
      );
    }).then((unlisten) => {
      // 订阅是异步建立的：期间若已卸载，必须立刻退订，否则会漏掉一个监听器。
      if (cancelled) unlisten();
      else stop = unlisten;
    });
    return () => {
      cancelled = true;
      stop?.();
    };
  }, []);

  return progress;
}
