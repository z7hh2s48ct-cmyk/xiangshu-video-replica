import { convertFileSrc, invoke, isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useEffect, useRef, useState } from "react";
import {
  fetchViralCacheReclaim,
  fetchViralVideoMedia,
  type ViralPlatform,
} from "../api";

/**
 * 爆款视频本地缓存（P2）的桌面端桥接。
 *
 * 设计约束：
 * - §6.2 索引即文件系统，缓存状态由 Rust 侧按文件系统回答，这里不缓存业务字段。
 * - §13-2 Web 端降级：非桌面端不做缓存/播放，直接静默跳过（不能报错，否则
 *   Web 端浏览爆款列表会一路弹错）。
 * - 缓存数据源统一为**云端归档**（`/videos/media`）：源站直链会过期，链接导入
 *   与采集条目都靠归档才能稳定缓存，且该端点不计费。
 * - 进度事件驱动、不轮询。
 */

/**
 * 缓存条目在界面上的状态。
 *
 * 前四种与 Rust `CacheState` 一一对应；`unready` 是**前端专属**状态，用于
 * 「云端归档尚未就绪」这类还没轮到下载队列就先失败的条目——如实说明，不谎报
 * 「缓存失败」。
 */
export type ViralCacheState =
  | "queued"
  | "downloading"
  | "cached"
  | "failed"
  | "unready";

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
  unready: "素材未就绪",
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

/** 归档素材未就绪（服务端仍在校验/转存，或存储后端临时不可用）。 */
export function isViralMaterialUnready(error: unknown): boolean {
  const code =
    error && typeof error === "object" && "code" in error
      ? (error as { code?: unknown }).code
      : undefined;
  return (
    code === "VIRAL_MEDIA_PREPARATION_BUSY" ||
    code === "STORAGE_BACKEND_UNAVAILABLE" ||
    code === "VIRAL_MEDIA_URL_UNSUPPORTED"
  );
}

/**
 * 按需缓存：取**云端归档**素材地址，再入队下载。
 *
 * 归档件是平台自己的对象（采集与链接导入在入库时就已转存），因此：
 * - 不再依赖会过期的源站直链，链接导入与采集条目走同一条缓存路径；
 * - 视频号在入云前就完成了解密，这里不需要 `decodeKey`；
 * - `/videos/media` 不计费，翻页自动缓存不会产生「查看详情」以外的费用。
 *
 * 归档尚未就绪时服务端返回 503，调用方用 `isViralMaterialUnready` 区分。
 */
export async function ensureViralArchiveCache(
  platform: ViralPlatform,
  videoId: string,
): Promise<void> {
  if (!cacheAvailable()) return;
  const media = await fetchViralVideoMedia(platform, videoId, "video");
  await ensureViralCache({ platform, videoId, url: media.url });
}

/** 合成一条前端口径的失败状态：仅用于归档未就绪/取地址失败时的角标。 */
export function fallbackCacheProgress(
  platform: string,
  videoId: string,
  error: unknown,
): ViralCacheProgress {
  const unready = isViralMaterialUnready(error);
  return {
    platform,
    videoId,
    state: unready ? "unready" : "failed",
    downloadedBytes: 0,
    totalBytes: null,
    speedBytesPerSecond: 0,
    error: unready
      ? "云端素材尚未就绪，稍后会自动重试"
      : error instanceof Error
        ? error.message
        : "缓存失败，请重试",
  };
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
 * 与自动缓存共用归档数据源；缺少平台标识时静默返回、不抛错——调用方通常已经在
 * 给用户展示「尚未缓存」的说明，再弹一个错只会造成噪音。
 */
export async function ensureViralCacheForVideo(video: {
  platformKey?: string;
  nativeId?: string;
}): Promise<void> {
  if (!cacheAvailable()) return;
  const { platformKey, nativeId } = video;
  if (!platformKey || !nativeId) return;
  await ensureViralArchiveCache(platformKey as ViralPlatform, nativeId);
}

/** 与服务端单批上限一致（`ViralCacheReclaimRequest` 的 200 条）。 */
const VIRAL_CACHE_RECLAIM_BATCH = 200;

/**
 * 回收「本地有、服务端却已不再下发」的条目（D5：删除与下架都要连带清本地缓存）。
 *
 * 判据必须问服务端：客户端的列表是分页的，只按「本次下发的条目」判断会把仍然有效
 * 的缓存误删。`retain` 传客户端已知还需要保留的条目（本次下发的、收藏、素材等
 * 非首页条目），服务端再叠加它自己的收藏与可见性判断，返回的才是可安全回收的集合。
 *
 * 返回真正被回收的 videoId；失败一律静默——回收是尽力而为，下次刷新会再试一次，
 * 为此弹错只会打断浏览。
 */
export async function reclaimViralCache(
  platform: string,
  retain: ViralCacheKeyInput[] = [],
): Promise<string[]> {
  if (!cacheAvailable()) return [];
  try {
    const local = await listViralCache();
    const retained = new Set(retain.map((item) => item.videoId));
    const candidates = local
      .filter(
        (item) => item.platform === platform && !retained.has(item.videoId),
      )
      .map((item) => item.videoId);
    const reclaimed: string[] = [];
    for (
      let start = 0;
      start < candidates.length;
      start += VIRAL_CACHE_RECLAIM_BATCH
    ) {
      const response = await fetchViralCacheReclaim(
        platform as ViralPlatform,
        candidates.slice(start, start + VIRAL_CACHE_RECLAIM_BATCH),
      );
      await Promise.all(
        response.reclaim.map((videoId) => deleteViralCache(platform, videoId)),
      );
      reclaimed.push(...response.reclaim);
    }
    return reclaimed;
  } catch {
    return [];
  }
}

/** 自动缓存只关心这两个字段，因此不绑死到具体的视频类型上。 */
export type ViralCacheCandidate = {
  platformKey?: string;
  nativeId?: string;
};

/**
 * 把当前页的全部条目按「翻页即缓存」（§6.2）自动入队，重复渲染不会重复入队。
 *
 * 数据源是云端归档且不计费，因此不必再像过去那样只挑「已知直链」的条目缓存；
 * 归档未就绪的条目返回一条合成的失败状态（`unready`），由界面如实提示。
 */
export function useViralAutoCache(
  videos: ViralCacheCandidate[],
): Map<string, ViralCacheProgress> {
  const queued = useRef(new Set<string>());
  const [fallback, setFallback] = useState<Map<string, ViralCacheProgress>>(
    () => new Map(),
  );
  useEffect(() => {
    if (!cacheAvailable()) return;
    for (const video of videos) {
      const platform = video.platformKey;
      const videoId = video.nativeId;
      if (!platform || !videoId) continue;
      const key = viralCacheKey(platform, videoId);
      if (queued.current.has(key)) continue;
      queued.current.add(key);
      void ensureViralArchiveCache(platform as ViralPlatform, videoId)
        .then(() => {
          // 重试成功后清掉上一次的合成状态，让位于真实的下载进度。
          setFallback((previous) => {
            if (!previous.has(key)) return previous;
            const next = new Map(previous);
            next.delete(key);
            return next;
          });
        })
        .catch((error: unknown) => {
          // 单条取地址失败不能打断浏览（角标会显示失败供重试）。
          // 从集合里移除，让下次进入页面还能再试。
          queued.current.delete(key);
          setFallback((previous) =>
            new Map(previous).set(
              key,
              fallbackCacheProgress(platform, videoId, error),
            ),
          );
        });
    }
  }, [videos]);
  return fallback;
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
