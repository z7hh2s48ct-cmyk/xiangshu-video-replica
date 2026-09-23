import type { StudioAsset } from "./types";

/** 参考素材预览解析（REFERENCE-MATERIAL-PREVIEW）：把参考列表的缩略图/播放地址
 * 从「谁手上有什么」收敛成一条可测的纯函数链，页面只负责渲染结果。
 *
 * 解析走同一条服务端授权通道 `POST /api/assets/download-urls`（批量、含属主校验
 * 与审计），一次请求同时拿到：
 * - `url`：素材本体签名地址（图片即图片，视频/音频用于弹窗播放）；
 * - `thumbnail_url`：视频首帧缩略图签名地址（MATERIAL-THUMBS-B，历史素材为空）。
 */

export type ReferencePreviewStatus = "loading" | "ready" | "error";

export type ReferencePreviewEntry = {
  status: ReferencePreviewStatus;
  /** 素材本体签名地址（图片直出/视频音频弹窗播放）。 */
  mediaUrl?: string;
  /** 视频首帧缩略图地址；服务端未签发（历史素材）时为空。 */
  poster?: string;
  /** 本机缓存 Blob URL 的释放回调；服务端签名地址不带。 */
  release?: () => void;
  /** 失败原因（页面直接展示，保持用户可读）。 */
  message?: string;
};

export type ReferencePreviewMap = Record<string, ReferencePreviewEntry>;

/** 批量解析结果的最小形状：与 api.getMaterialBatchPreviews 的返回结构对齐。 */
export type ReferencePreviewBatch = {
  previews: Record<string, { url?: string; release?: () => void }>;
  thumbnails: Record<string, string>;
};

/** 可解析的物理资产 id：与弹窗预览同口径（优先 assetId，回退自身 id）。 */
export function referencePreviewAssetId(asset: StudioAsset): string {
  return asset.assetId ?? asset.id;
}

/** 需要请求预览的参考素材 id：图片取本体、视频取本体+缩略图；音频列表不出图像，
 * 弹窗播放仍走既有按需解析，因此不进批量请求。 */
export function referencePreviewTargets(assets: StudioAsset[]): string[] {
  const ids: string[] = [];
  for (const asset of assets) {
    if (asset.kind === "audio") continue;
    const id = referencePreviewAssetId(asset);
    if (id && !ids.includes(id)) ids.push(id);
  }
  return ids;
}

/** 增量合并解析结果：本次请求覆盖到的 id 用新结果整体替换（避免残留上一轮的
 * 失败文案），未覆盖的 id 保留既有结果。追加素材或重进页面时不得整表替换，
 * 否则已显示的缩略图会整片消失。 */
export function mergeReferencePreviews(
  previous: ReferencePreviewMap,
  incoming: ReferencePreviewMap,
): ReferencePreviewMap {
  const merged: ReferencePreviewMap = { ...previous };
  for (const [id, entry] of Object.entries(incoming)) {
    if (!entry) continue;
    merged[id] = entry;
  }
  return merged;
}

/** 把一批 id 标记为加载中，保留已就绪/已失败结果。 */
export function markReferencePreviewsLoading(
  previous: ReferencePreviewMap,
  ids: string[],
): ReferencePreviewMap {
  const next: ReferencePreviewMap = { ...previous };
  for (const id of ids) next[id] = { status: "loading" };
  return next;
}

/** 清掉某个 id 的解析结果（重试用），并释放它持有的 Blob URL。 */
export function clearReferencePreview(
  previous: ReferencePreviewMap,
  assetId: string,
): ReferencePreviewMap {
  const entry = previous[assetId];
  entry?.release?.();
  const next: ReferencePreviewMap = { ...previous };
  delete next[assetId];
  return next;
}

/** 释放一批解析结果持有的本机 Blob URL（卸载或重新解析前调用）。 */
export function releaseReferencePreviews(entries: ReferencePreviewMap): void {
  for (const entry of Object.values(entries)) entry?.release?.();
}

/** 批量授权结果 → 每个 id 的解析结果。授权未覆盖或缺少地址的 id 记为失败，
 * 由页面给出可重试的可见状态，不再静默留空。 */
export function referencePreviewEntries(
  assets: StudioAsset[],
  ids: string[],
  batch: ReferencePreviewBatch,
): ReferencePreviewMap {
  const byId = new Map(
    assets.map((asset) => [referencePreviewAssetId(asset), asset]),
  );
  const entries: ReferencePreviewMap = {};
  for (const id of ids) {
    const asset = byId.get(id);
    if (!asset) continue;
    const preview = batch.previews[id];
    if (!preview?.url) {
      preview?.release?.();
      entries[id] = {
        status: "error",
        message: "缩略图暂不可用，请重试。",
      };
      continue;
    }
    entries[id] = {
      status: "ready",
      mediaUrl: preview.url,
      poster: asset.kind === "video" ? batch.thumbnails[id] : undefined,
      release: preview.release,
    };
  }
  return entries;
}

export type ReferencePreviewView = {
  /** 交给 Media 渲染的素材；为空表示走占位文案。 */
  asset?: StudioAsset;
  /** 占位文案（无地址/待加载/失败），页面渲染成元素。 */
  placeholder?: string;
};

/** 列表缩略图该渲染什么：视频优先用服务端首帧缩略图当图片展示；无缩略图的历史
 * 视频退回视频本体（浏览器首帧，`preload=metadata`）；音频固定走波形占位。 */
export function referencePreviewView(
  asset: StudioAsset,
  entry?: ReferencePreviewEntry,
): ReferencePreviewView {
  if (asset.kind === "audio") return { asset };
  if (asset.kind === "image") {
    const url = entry?.mediaUrl ?? asset.url;
    if (url) return { asset: { ...asset, url } };
    return entry?.status === "error"
      ? { placeholder: entry.message ?? "缩略图暂不可用，请重试。" }
      : { placeholder: "正在读取缩略图…" };
  }
  const poster = entry?.poster ?? asset.poster;
  if (poster) return { asset: { ...asset, kind: "image", url: poster } };
  if (entry?.status === "error")
    return { placeholder: entry.message ?? "缩略图暂不可用，请重试。" };
  if (entry?.mediaUrl) return { asset: { ...asset, url: entry.mediaUrl } };
  return { placeholder: "正在读取缩略图…" };
}
