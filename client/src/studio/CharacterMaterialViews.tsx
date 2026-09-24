import { useEffect, useRef, useState } from "react";
import { evictMaterialCachedPreview, getMaterialCachedPreview } from "../api";
import type { StudioAsset } from "./types";
import { Button, Media } from "./ui";

const labels: Record<string, string> = {
  FRONT_FULL: "正面全身",
  FRONT_HALF: "正面半身",
  FRONT_FACE: "正面近景",
  LEFT_45: "左侧 45°",
  LEFT_SIDE: "左侧面",
};

/** One material set, with explicit single-image selection for downstream creation. */
export function CharacterMaterialViews({
  asset,
  userId,
  cacheRevision = 0,
  cacheClearing = false,
  cachePopulateAllowed = true,
  onSelected,
}: {
  asset: StudioAsset;
  userId: string;
  cacheRevision?: number;
  cacheClearing?: boolean;
  cachePopulateAllowed?: boolean;
  onSelected: (value: StudioAsset | undefined) => void;
}) {
  const views = asset.characterViews ?? [];
  const [viewId, setViewId] = useState(
    asset.previewAssetId ?? views[0]?.assetId ?? asset.id,
  );
  const [preview, setPreview] = useState<{ id: string; url: string }>();
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const resourceRef = useRef<
    | {
        userId: string;
        viewId: string;
        revision: number;
        release: () => void;
      }
    | undefined
  >(undefined);
  const isSheet = viewId === asset.id;
  const view = views.find((item) => item.assetId === viewId);
  const label = isSheet
    ? "五视图合成图"
    : (labels[view?.viewType ?? ""] ?? "人物视角");
  const url = preview?.id === viewId ? preview.url : undefined;

  useEffect(
    () => () => {
      resourceRef.current?.release();
      resourceRef.current = undefined;
    },
    [],
  );

  useEffect(() => {
    void cacheRevision;
    const resource = resourceRef.current;
    if (
      resource &&
      resource.userId === userId &&
      resource.viewId === viewId &&
      resource.revision === revision
    )
      return; // Clearing persistent bytes must not revoke an already displayed image.
    resource?.release();
    resourceRef.current = undefined;
    setPreview(undefined);
    if (cacheClearing) return;
    let active = true;
    const controller = new AbortController();
    setError("");
    void getMaterialCachedPreview(userId, viewId, {
      populate: cachePopulateAllowed,
      signal: controller.signal,
    })
      .then((result) => {
        if (active) {
          resourceRef.current = {
            userId,
            viewId,
            revision,
            release: result.release,
          };
          setPreview({ id: viewId, url: result.url });
        } else result.release();
      })
      .catch(() => {
        if (active) setError("当前视角图片加载失败，请重试。");
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [
    userId,
    viewId,
    revision,
    cacheRevision,
    cacheClearing,
    cachePopulateAllowed,
  ]);

  useEffect(() => {
    onSelected(
      isSheet
        ? { ...asset, contactSheetId: asset.id, url }
        : !view
          ? undefined
          : {
              ...asset,
              id: viewId,
              assetId: viewId,
              materialId: `asset:${viewId}`,
              contactSheetId: asset.id,
              previewAssetId: undefined,
              characterViews: undefined,
              name: `${asset.name} · ${label}`,
              url,
              composite: false,
              allowedUses: [
                "original_frame",
                "first_frame",
                "tail_frame",
                "reference",
              ],
            },
    );
  }, [asset, view, viewId, isSheet, label, url, onSelected]);

  return (
    <section className="character-material-views" aria-label="五视图单图选择">
      <div className="character-material-choices">
        {views.map((item) => (
          <button
            key={item.assetId}
            type="button"
            aria-pressed={viewId === item.assetId}
            onClick={() => setViewId(item.assetId)}
          >
            {labels[item.viewType] ?? item.viewType}
          </button>
        ))}
        <button
          type="button"
          aria-pressed={isSheet}
          onClick={() => setViewId(asset.id)}
        >
          合成图
        </button>
      </div>
      <Media
        key={`${viewId}:${revision}`}
        asset={{ ...asset, id: viewId, url, composite: isSheet }}
        alt={`${asset.name} ${label}`}
        className={`character-material-preview ${isSheet ? "is-sheet" : ""}`}
        onError={() => {
          setError("当前视角图片加载失败，请重试。");
          if (url?.startsWith("blob:"))
            void evictMaterialCachedPreview(userId, viewId).catch(() => {});
        }}
      />
      <p>
        当前选中：{label}。
        {isSheet
          ? "合成图可用于整体参考，请选择单独视角用作首尾帧。"
          : "下方创作操作使用这张单图。"}
      </p>
      {error ? (
        <div role="alert">
          <p>{error}</p>
          <Button
            variant="outline"
            onClick={() => {
              setPreview(undefined);
              setRevision((value) => value + 1);
            }}
          >
            重试当前视角
          </Button>
        </div>
      ) : null}
    </section>
  );
}
