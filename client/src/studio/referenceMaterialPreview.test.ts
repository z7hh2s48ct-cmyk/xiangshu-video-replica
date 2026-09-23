import { describe, expect, it, vi } from "vitest";
import {
  clearReferencePreview,
  markReferencePreviewsLoading,
  mergeReferencePreviews,
  referencePreviewAssetId,
  referencePreviewEntries,
  referencePreviewTargets,
  referencePreviewView,
  releaseReferencePreviews,
} from "./referenceMaterialPreview";
import type { StudioAsset } from "./types";

function asset(overrides: Partial<StudioAsset> = {}): StudioAsset {
  return {
    id: "asset-1",
    name: "乡墅外观.jpg",
    kind: "image",
    group: "参考素材",
    source: "素材库",
    saved: true,
    ...overrides,
  };
}

describe("参考素材预览解析", () => {
  it("解析 id 优先物理资产 id，回退自身 id", () => {
    expect(referencePreviewAssetId(asset({ assetId: "asset:9" }))).toBe(
      "asset:9",
    );
    expect(referencePreviewAssetId(asset({ id: "mat-3" }))).toBe("mat-3");
  });

  it("只请求图片与视频，并按 id 去重（音频不出图像）", () => {
    expect(
      referencePreviewTargets([
        asset({ assetId: "a" }),
        asset({ assetId: "a" }),
        asset({ assetId: "v", kind: "video" }),
        asset({ assetId: "s", kind: "audio" }),
      ]),
    ).toEqual(["a", "v"]);
  });

  it("增量合并：追加素材时已解析结果保留，不被空值覆盖", () => {
    const merged = mergeReferencePreviews(
      {
        a: { status: "ready", mediaUrl: "https://media.example/a.jpg" },
        b: { status: "error", message: "缩略图暂不可用，请重试。" },
      },
      { b: { status: "ready", mediaUrl: "https://media.example/b.jpg" } },
    );
    expect(merged.a).toEqual({
      status: "ready",
      mediaUrl: "https://media.example/a.jpg",
    });
    expect(merged.b).toEqual({
      status: "ready",
      mediaUrl: "https://media.example/b.jpg",
    });
  });

  it("加载中标记不清掉其它 id 的结果", () => {
    const next = markReferencePreviewsLoading(
      { a: { status: "ready", mediaUrl: "https://media.example/a.jpg" } },
      ["b"],
    );
    expect(next.a.status).toBe("ready");
    expect(next.b).toEqual({ status: "loading" });
  });

  it("清理单个 id 时释放它持有的 Blob URL", () => {
    const release = vi.fn();
    const next = clearReferencePreview(
      { a: { status: "ready", mediaUrl: "blob:x", release } },
      "a",
    );
    expect(release).toHaveBeenCalledTimes(1);
    expect(next.a).toBeUndefined();
  });

  it("批量释放所有持有的 Blob URL", () => {
    const first = vi.fn();
    const second = vi.fn();
    releaseReferencePreviews({
      a: { status: "ready", release: first },
      b: { status: "ready", release: second },
    });
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("批量结果：视频带首帧缩略图，图片不带；缺地址记为可重试失败", () => {
    const release = vi.fn();
    const entries = referencePreviewEntries(
      [
        asset({ assetId: "v", kind: "video" }),
        asset({ assetId: "i" }),
        asset({ assetId: "missing" }),
      ],
      ["v", "i", "missing"],
      {
        previews: {
          v: { url: "https://media.example/v.mp4", release },
          i: { url: "https://media.example/i.jpg" },
        },
        thumbnails: { v: "https://media.example/v.thumb.jpg" },
      },
    );
    expect(entries.v).toEqual({
      status: "ready",
      mediaUrl: "https://media.example/v.mp4",
      poster: "https://media.example/v.thumb.jpg",
      release,
    });
    expect(entries.i).toEqual({
      status: "ready",
      mediaUrl: "https://media.example/i.jpg",
      poster: undefined,
      release: undefined,
    });
    expect(entries.missing).toEqual({
      status: "error",
      message: "缩略图暂不可用，请重试。",
    });
  });

  it("列表缩略图：视频用首帧缩略图当图片展示", () => {
    const view = referencePreviewView(
      asset({ assetId: "v", kind: "video", name: "庭院运镜.mp4" }),
      {
        status: "ready",
        mediaUrl: "https://media.example/v.mp4",
        poster: "https://media.example/v.thumb.jpg",
      },
    );
    expect(view.asset).toMatchObject({
      id: "asset-1",
      kind: "image",
      url: "https://media.example/v.thumb.jpg",
    });
  });

  it("列表缩略图：历史视频无缩略图时退回视频本体，失败与待加载给出占位文案", () => {
    const video = asset({ assetId: "v", kind: "video" });
    expect(
      referencePreviewView(video, {
        status: "ready",
        mediaUrl: "https://media.example/v.mp4",
      }).asset,
    ).toMatchObject({ kind: "video", url: "https://media.example/v.mp4" });
    expect(referencePreviewView(video, { status: "loading" })).toEqual({
      placeholder: "正在读取缩略图…",
    });
    expect(
      referencePreviewView(video, {
        status: "error",
        message: "缩略图暂不可用，请重试。",
      }),
    ).toEqual({ placeholder: "缩略图暂不可用，请重试。" });
  });

  it("列表缩略图：图片优先用解析地址，无地址时用素材自带地址", () => {
    expect(
      referencePreviewView(asset({ url: "https://media.example/raw.jpg" }), {
        status: "loading",
      }).asset,
    ).toMatchObject({ url: "https://media.example/raw.jpg" });
    expect(
      referencePreviewView(asset(), {
        status: "ready",
        mediaUrl: "https://media.example/i.jpg",
      }).asset,
    ).toMatchObject({ url: "https://media.example/i.jpg" });
  });

  it("音频不做图像请求，直接给波形占位素材", () => {
    const audio = asset({ kind: "audio", name: "环境声.wav" });
    expect(referencePreviewView(audio, undefined).asset).toBe(audio);
    expect(referencePreviewView(audio, { status: "error" }).asset).toBe(audio);
  });
});
