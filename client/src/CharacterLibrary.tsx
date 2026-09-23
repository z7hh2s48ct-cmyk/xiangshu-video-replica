import { useCallback, useEffect, useRef, useState } from "react";

import {
  type CharacterViewType,
  deleteSimpleCharacterIdentity,
  downloadCharacterAsset,
  getCachedCharacterAssetUrl,
  getLatestCharacterSheetTask,
  listSimpleCharacterLibraryPage,
  regenerateContactSheet,
  renamePersonIdentity,
  type SimpleCharacterView,
  type SimpleLibraryEntry,
  type UserRole,
  waitForCharacterSheetTask,
} from "./api";
import { CharacterScenePanel } from "./CharacterScenePanel";
import { SimpleCharacterUpload } from "./SimpleCharacterUpload";

const VIEW_LABELS: Record<CharacterViewType, string> = {
  FRONT_FACE: "正脸近景",
  FRONT_HALF: "正面半身",
  FRONT_FULL: "正面全身",
  LEFT_45: "左 45°",
  LEFT_SIDE: "左侧面",
};
const CHARACTER_PAGE_SIZE = 12;

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function entryAssetIds(entry: SimpleLibraryEntry): string[] {
  return [
    ...(entry.contact_sheet_asset_id ? [entry.contact_sheet_asset_id] : []),
    ...entry.views.map((view) => view.asset_id),
  ];
}

type PreviewStatus = "loading" | "ready" | "error";

export type PendingCharacterState = {
  displayName: string;
  progress: number;
  sourcePreviewUrl?: string;
  stage: string;
  status: "working" | "error";
};

// 卡片封面取正脸近景（辨识度最高）；没有正脸时退回首张视角图，
// 视角全缺（理论上不该出现）才用拼合图裁切兜底。拼合全图在灯箱看。
function coverAsset(
  entry: SimpleLibraryEntry,
): { assetId: string; label: string } | null {
  const view =
    entry.views.find((item) => item.view_type === "FRONT_FACE") ??
    (entry.views.length ? entry.views[0] : null);
  if (view) {
    return { assetId: view.asset_id, label: VIEW_LABELS[view.view_type] };
  }
  return entry.contact_sheet_asset_id
    ? { assetId: entry.contact_sheet_asset_id, label: "五视图拼合图" }
    : null;
}

// 人物库 = 「上传 → 五视图拼合图预览 → 下载」的主动线：上方一键上传，
// 下方 4 列人物卡（正脸近景封面）；点封面开灯箱看拼合大图并下载。
export function CharacterLibrary({
  userRole,
  userId,
  initialIdentityId,
  initialTab = "base",
  onChanged,
  onOpenProfile,
}: {
  userRole: UserRole;
  userId: string;
  initialIdentityId?: string;
  initialTab?: "base" | "scenes";
  onChanged?: () => void;
  onOpenProfile?: (identityId: string) => void;
}) {
  const canManage = userRole !== "auditor";
  const [entries, setEntries] = useState<SimpleLibraryEntry[]>([]);
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});
  const [previewStatuses, setPreviewStatuses] = useState<
    Record<string, PreviewStatus>
  >({});
  const [pendingCharacter, setPendingCharacter] =
    useState<PendingCharacterState | null>(null);
  const pendingPreviewUrlRef = useRef("");
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busyDownloadKey, setBusyDownloadKey] = useState("");
  const [editingId, setEditingId] = useState("");
  const [editingName, setEditingName] = useState("");
  const [busyRenameId, setBusyRenameId] = useState("");
  const [busyDeleteId, setBusyDeleteId] = useState("");
  const [busyRegenerateId, setBusyRegenerateId] = useState("");
  const [lightboxId, setLightboxId] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const libraryRequestIdRef = useRef(0);
  const openedInitialIdentityRef = useRef<string | undefined>(undefined);
  const normalizedQuery = searchQuery.trim();
  const shownAssetKey = entries.flatMap(entryAssetIds).join("\n");

  const loadPreviewUrls = useCallback(
    async (assetIds: string[], retry = false) => {
      const uniqueAssetIds = [...new Set(assetIds)];
      setPreviewStatuses((current) => ({
        ...current,
        ...Object.fromEntries(
          uniqueAssetIds.map((assetId) => [assetId, "loading"]),
        ),
      }));
      const results = await Promise.allSettled(
        uniqueAssetIds.map(async (assetId) => {
          const download = await getCachedCharacterAssetUrl(assetId);
          const retrySeparator = download.url.includes("?") ? "&" : "?";
          return [
            assetId,
            retry
              ? `${download.url}${retrySeparator}preview_retry=${Date.now()}`
              : download.url,
          ] as const;
        }),
      );
      const fulfilled = results.flatMap((result) =>
        result.status === "fulfilled" ? [result.value] : [],
      );
      const failedIds = results.flatMap((result, index) =>
        result.status === "rejected" ? [uniqueAssetIds[index]] : [],
      );
      setPreviewUrls((current) => ({
        ...current,
        ...Object.fromEntries(fulfilled),
      }));
      setPreviewStatuses((current) => ({
        ...current,
        ...Object.fromEntries(fulfilled.map(([assetId]) => [assetId, "ready"])),
        ...Object.fromEntries(failedIds.map((assetId) => [assetId, "error"])),
      }));
    },
    [],
  );

  const markPreviewError = useCallback((assetId: string) => {
    setPreviewStatuses((current) => ({ ...current, [assetId]: "error" }));
  }, []);

  const loadLibrary = useCallback(async () => {
    const requestId = ++libraryRequestIdRef.current;
    setIsLoading(true);
    setIsLoadingMore(false);
    try {
      const result = await listSimpleCharacterLibraryPage({
        limit: CHARACTER_PAGE_SIZE,
        query: normalizedQuery,
      });
      if (requestId !== libraryRequestIdRef.current) {
        return;
      }
      const ownedEntries =
        userRole === "admin" || userRole === "auditor"
          ? result.items
          : result.items.filter((entry) => entry.owner_user_id === userId);
      // A detail-page handoff may target an older person outside the first page.
      // Read pages only until that target is found; do not load previews for every person.
      if (
        initialIdentityId &&
        openedInitialIdentityRef.current !== initialIdentityId &&
        !ownedEntries.some((entry) => entry.identity_id === initialIdentityId)
      ) {
        let cursor = result.next_cursor;
        const visited = new Set<string>();
        while (cursor && !visited.has(cursor)) {
          visited.add(cursor);
          const page = await listSimpleCharacterLibraryPage({
            limit: CHARACTER_PAGE_SIZE,
            cursor,
          });
          if (requestId !== libraryRequestIdRef.current) return;
          const target = page.items.find(
            (entry) =>
              entry.identity_id === initialIdentityId &&
              (userRole === "admin" ||
                userRole === "auditor" ||
                entry.owner_user_id === userId),
          );
          if (target) {
            ownedEntries.unshift(target);
            break;
          }
          cursor = page.next_cursor;
        }
      }
      setEntries(ownedEntries);
      setNextCursor(result.next_cursor);
      setError("");
    } catch (loadError) {
      if (requestId === libraryRequestIdRef.current) {
        setError(errorMessage(loadError, "人物库暂不可用，请重试。"));
      }
    } finally {
      if (requestId === libraryRequestIdRef.current) {
        setIsLoading(false);
      }
    }
  }, [normalizedQuery, userId, userRole, initialIdentityId]);

  async function loadMoreCharacters() {
    if (!nextCursor || isLoadingMore) {
      return;
    }
    const requestId = ++libraryRequestIdRef.current;
    setIsLoadingMore(true);
    try {
      const result = await listSimpleCharacterLibraryPage({
        limit: CHARACTER_PAGE_SIZE,
        cursor: nextCursor,
        query: normalizedQuery,
      });
      if (requestId !== libraryRequestIdRef.current) {
        return;
      }
      const allowedItems =
        userRole === "admin" || userRole === "auditor"
          ? result.items
          : result.items.filter((entry) => entry.owner_user_id === userId);
      setEntries((current) => {
        const knownIds = new Set(current.map((entry) => entry.identity_id));
        return [
          ...current,
          ...allowedItems.filter((entry) => !knownIds.has(entry.identity_id)),
        ];
      });
      setNextCursor(result.next_cursor);
      setError("");
    } catch (loadError) {
      if (requestId === libraryRequestIdRef.current) {
        setError(errorMessage(loadError, "加载更多人物失败，请重试。"));
      }
    } finally {
      if (requestId === libraryRequestIdRef.current) {
        setIsLoadingMore(false);
      }
    }
  }

  const releasePendingPreview = useCallback(() => {
    if (pendingPreviewUrlRef.current) {
      URL.revokeObjectURL(pendingPreviewUrlRef.current);
      pendingPreviewUrlRef.current = "";
    }
  }, []);

  const handleGenerationFailed = useCallback((generationError: string) => {
    setPendingCharacter((current) =>
      current
        ? {
            ...current,
            stage: generationError,
            status: "error",
          }
        : current,
    );
  }, []);

  const clearPendingGeneration = useCallback(() => {
    releasePendingPreview();
    setPendingCharacter(null);
  }, [releasePendingPreview]);

  useEffect(() => {
    void loadLibrary();
  }, [loadLibrary]);

  useEffect(() => {
    if (shownAssetKey) {
      void loadPreviewUrls(shownAssetKey.split("\n"));
    }
  }, [loadPreviewUrls, shownAssetKey]);

  useEffect(() => {
    if (
      initialIdentityId &&
      openedInitialIdentityRef.current !== initialIdentityId &&
      entries.some((entry) => entry.identity_id === initialIdentityId)
    ) {
      openedInitialIdentityRef.current = initialIdentityId;
      setLightboxId(initialIdentityId);
    }
  }, [entries, initialIdentityId]);

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const task = await getLatestCharacterSheetTask();
        if (!active || !task) {
          return;
        }
        // 恢复接口不区分任务类型：场景造型任务产出的是场景五视图，
        // 场景五视图不能冒充人物基准五视图。
        const taskNoun = task.operation === "SCENE" ? "场景造型" : "人物五视图";
        if (task.status === "SUCCEEDED") {
          clearPendingGeneration();
          setMessage(
            task.operation === "SCENE"
              ? `场景造型“${task.display_name}”已生成，请在人物卡片的“场景造型”页查看。`
              : `人物“${task.display_name}”五视图已生成。`,
          );
          // The first load may race the worker's final commit. Always reload after
          // observing SUCCEEDED so a completed character cannot stay invisible.
          await loadLibrary();
          return;
        }
        if (
          task.status === "FAILED" ||
          task.status === "SUBMISSION_UNCERTAIN"
        ) {
          setPendingCharacter({
            displayName: task.display_name,
            progress: task.status === "FAILED" ? 100 : 72,
            stage:
              task.error_message ??
              (task.status === "SUBMISSION_UNCERTAIN"
                ? "云端提交结果暂时无法确认，请稍后重试。"
                : `${taskNoun}生成失败，请重新提交。`),
            status: "error",
          });
          return;
        }
        setPendingCharacter({
          displayName: task.display_name,
          progress: task.status === "RUNNING" ? 58 : 18,
          stage:
            task.status === "RUNNING"
              ? task.operation === "SCENE"
                ? "正在云端生成场景造型五视图"
                : "正在云端生成五视图拼合图"
              : "已进入云端生成队列",
          status: "working",
        });
        await waitForCharacterSheetTask(task.id);
        if (!active) {
          return;
        }
        clearPendingGeneration();
        setMessage(
          task.operation === "SCENE"
            ? `场景造型“${task.display_name}”已生成，请在人物卡片的“场景造型”页查看。`
            : `人物“${task.display_name}”五视图已生成。`,
        );
        await loadLibrary();
      } catch (recoveryError) {
        if (active) {
          handleGenerationFailed(
            errorMessage(recoveryError, "人物生成失败，请重新提交。"),
          );
        }
      }
    })();
    return () => {
      active = false;
    };
  }, [clearPendingGeneration, handleGenerationFailed, loadLibrary]);

  useEffect(
    () => () => {
      if (pendingPreviewUrlRef.current) {
        URL.revokeObjectURL(pendingPreviewUrlRef.current);
      }
    },
    [],
  );

  function handleGenerationStarted(file: File, displayName: string) {
    releasePendingPreview();
    const sourcePreviewUrl = URL.createObjectURL(file);
    pendingPreviewUrlRef.current = sourcePreviewUrl;
    setPendingCharacter({
      displayName,
      progress: 8,
      sourcePreviewUrl,
      stage: "正在上传授权图片",
      status: "working",
    });
  }

  function handleGenerationProgress(progress: number, stage: string) {
    setPendingCharacter((current) =>
      current ? { ...current, progress, stage, status: "working" } : current,
    );
  }

  // 上传成功后立刻把新人物置顶展示，无需等待整表刷新。
  function handleCreated(newEntry: SimpleLibraryEntry) {
    // A slower initial/search request must not erase the newly created card.
    libraryRequestIdRef.current += 1;
    setIsLoading(false);
    setIsLoadingMore(false);
    clearPendingGeneration();
    setEntries((current) => [
      newEntry,
      ...current.filter((item) => item.identity_id !== newEntry.identity_id),
    ]);
    void loadPreviewUrls(entryAssetIds(newEntry));
    onChanged?.();
  }

  function viewFileName(entryName: string, view: SimpleCharacterView): string {
    return `${entryName}-${VIEW_LABELS[view.view_type]}.png`;
  }

  async function handleDownloadSheet(entry: SimpleLibraryEntry) {
    if (!entry.contact_sheet_asset_id) {
      return;
    }
    setBusyDownloadKey(entry.contact_sheet_asset_id);
    setError("");
    try {
      await downloadCharacterAsset(
        entry.contact_sheet_asset_id,
        `${entry.display_name}-五视图拼合图.png`,
      );
      setMessage(`人物“${entry.display_name}”的五视图拼合图已开始下载。`);
    } catch (downloadError) {
      setError(errorMessage(downloadError, "下载拼合图失败，请稍后重试。"));
    } finally {
      setBusyDownloadKey("");
    }
  }

  async function handleDownloadView(
    entry: SimpleLibraryEntry,
    view: SimpleCharacterView,
  ) {
    setBusyDownloadKey(view.asset_id);
    setError("");
    try {
      await downloadCharacterAsset(
        view.asset_id,
        viewFileName(entry.display_name, view),
      );
    } catch (downloadError) {
      setError(errorMessage(downloadError, "下载人物视角图失败，请稍后重试。"));
    } finally {
      setBusyDownloadKey("");
    }
  }

  async function handleDownloadAll(entry: SimpleLibraryEntry) {
    setBusyDownloadKey(entry.identity_id);
    setError("");
    try {
      for (const view of entry.views) {
        await downloadCharacterAsset(
          view.asset_id,
          viewFileName(entry.display_name, view),
        );
      }
      setMessage(
        `人物“${entry.display_name}”的 ${entry.views.length} 张视角图已开始下载。`,
      );
    } catch (downloadError) {
      setError(errorMessage(downloadError, "下载人物视角图失败，请稍后重试。"));
    } finally {
      setBusyDownloadKey("");
    }
  }

  function canRename(entry: SimpleLibraryEntry): boolean {
    if (!canManage || entry.status === "ARCHIVED") {
      return false;
    }
    return userRole === "admin" || entry.owner_user_id === userId;
  }

  function canDelete(entry: SimpleLibraryEntry): boolean {
    if (!canManage) {
      return false;
    }
    return userRole === "admin" || entry.owner_user_id === userId;
  }

  // 重新生成五视图 = 用授权原图重跑单图版 identity-preserve 生成；
  // 新结果作为新版本发布并自动成为人物库预览，已选用旧版本的项目不受影响。
  async function handleRegenerate(entry: SimpleLibraryEntry) {
    setBusyRegenerateId(entry.identity_id);
    setError("");
    setMessage("");
    try {
      const result = await regenerateContactSheet(entry.identity_id);
      setMessage(
        `人物“${entry.display_name}”的五视图已重新生成（V${result.version_number}）。`,
      );
      await loadLibrary();
      onChanged?.();
    } catch (regenerateError) {
      setError(
        errorMessage(regenerateError, "重新生成五视图失败，请稍后重试。"),
      );
    } finally {
      setBusyRegenerateId("");
    }
  }

  async function handleDelete(entry: SimpleLibraryEntry) {
    const confirmed = window.confirm(
      `删除“${entry.display_name}”？该人物的授权图片、五视图拼合图与全部视角图将一并删除，且无法恢复。`,
    );
    if (!confirmed) {
      return;
    }
    setBusyDeleteId(entry.identity_id);
    setError("");
    try {
      await deleteSimpleCharacterIdentity(entry.identity_id);
      setEntries((current) =>
        current.filter((item) => item.identity_id !== entry.identity_id),
      );
      setMessage(`人物“${entry.display_name}”已删除。`);
      onChanged?.();
    } catch (deleteError) {
      setError(errorMessage(deleteError, "删除人物失败，请稍后重试。"));
    } finally {
      setBusyDeleteId("");
    }
  }

  function startRename(entry: SimpleLibraryEntry) {
    setEditingId(entry.identity_id);
    setEditingName(entry.display_name);
    setError("");
    setMessage("");
  }

  function cancelRename() {
    setEditingId("");
    setEditingName("");
    setBusyRenameId("");
  }

  async function saveRename(entry: SimpleLibraryEntry) {
    const name = editingName.trim();
    if (!name) {
      setError("人物名称不能为空。");
      return;
    }
    if (name === entry.display_name) {
      cancelRename();
      return;
    }
    setBusyRenameId(entry.identity_id);
    setError("");
    try {
      const updated = await renamePersonIdentity(entry.identity_id, name);
      setEntries((current) =>
        current.map((item) =>
          item.identity_id === updated.id
            ? { ...item, display_name: updated.display_name }
            : item,
        ),
      );
      setMessage(`人物名称已更新为“${updated.display_name}”。`);
      onChanged?.();
      cancelRename();
    } catch (renameError) {
      setError(errorMessage(renameError, "修改人物名称失败，请重试。"));
    } finally {
      setBusyRenameId("");
    }
  }

  const lightboxEntry =
    entries.find((entry) => entry.identity_id === lightboxId) ?? null;

  return (
    <section aria-label="人物库" className="character-library-simple">
      {canManage ? (
        <SimpleCharacterUpload
          onGenerationFailed={handleGenerationFailed}
          onGenerationProgress={handleGenerationProgress}
          onGenerationStarted={handleGenerationStarted}
          onCreated={(result, displayName) =>
            handleCreated({
              identity_id: result.identity_id,
              persona_id: result.persona_id,
              version_number: 1,
              display_name: displayName,
              role: "",
              service_scope: "",
              target_audience: "",
              expression_style: "",
              owner_user_id: userId,
              status: "ACTIVE",
              contact_sheet_asset_id: result.contact_sheet_asset_id,
              generation_source: result.generation_source,
              scene_look_count: 0,
              views: result.views,
            })
          }
        />
      ) : (
        <p className="status-note">
          审计身份只读，如需创建或改名请联系管理员。
        </p>
      )}
      {error ? (
        <div className="inline-error-actions">
          <p className="settings-error" role="alert">
            {error}
          </p>
          <button
            className="secondary-button"
            disabled={isLoading}
            onClick={() => void loadLibrary()}
            type="button"
          >
            重新读取人物库
          </button>
        </div>
      ) : null}
      {message ? <p className="setup-success">{message}</p> : null}
      {entries.length > 0 || normalizedQuery ? (
        <div className="character-library-search">
          <input
            aria-label="搜索人物"
            onChange={(event) => setSearchQuery(event.target.value)}
            placeholder="搜索人物名称、角色或服务范围"
            type="search"
            value={searchQuery}
          />
          <span>
            {normalizedQuery
              ? `已找到 ${entries.length} 位人物`
              : `已加载 ${entries.length} 位人物`}
          </span>
        </div>
      ) : null}
      {isLoading && !pendingCharacter ? (
        <p className="status-note">正在读取人物库…</p>
      ) : entries.length === 0 && normalizedQuery && !pendingCharacter ? (
        <p className="status-note">未找到匹配人物，请更换搜索词。</p>
      ) : entries.length === 0 && !pendingCharacter ? (
        <p className="status-note">还没有人物，上传一张图片开始创建。</p>
      ) : (
        <>
          <ul className="character-preview-list">
            {pendingCharacter ? (
              <PendingCharacterCard
                character={pendingCharacter}
                onClear={clearPendingGeneration}
              />
            ) : null}
            {entries.map((entry) => {
              const isEditing = editingId === entry.identity_id;
              const isRenaming = busyRenameId === entry.identity_id;
              const cover = coverAsset(entry);
              const coverUrl = cover ? previewUrls[cover.assetId] : undefined;
              const coverStatus = cover
                ? (previewStatuses[cover.assetId] ?? "loading")
                : "error";
              return (
                <li className="character-preview-card" key={entry.identity_id}>
                  <button
                    aria-label={`查看人物 ${entry.display_name} 大图`}
                    className="character-preview-card__cover"
                    onClick={() => {
                      if (cover && coverStatus === "error") {
                        void loadPreviewUrls([cover.assetId], true);
                        return;
                      }
                      setLightboxId(entry.identity_id);
                    }}
                    type="button"
                  >
                    {coverUrl && cover && coverStatus === "ready" ? (
                      <img
                        alt={`${entry.display_name} ${cover.label}`}
                        loading="lazy"
                        onError={() => markPreviewError(cover.assetId)}
                        src={coverUrl}
                      />
                    ) : coverStatus === "error" ? (
                      <span className="source-frame-placeholder source-frame-placeholder--error">
                        <strong>预览加载失败</strong>
                        <small>点击重新加载</small>
                      </span>
                    ) : (
                      <span className="source-frame-placeholder">
                        预览加载中…
                      </span>
                    )}
                  </button>
                  <div className="character-preview-card__body">
                    {isEditing ? (
                      <div className="character-preview-card__edit">
                        <input
                          aria-label="修改人物名称"
                          onChange={(event) =>
                            setEditingName(event.target.value)
                          }
                          type="text"
                          value={editingName}
                        />
                        <button
                          disabled={isRenaming}
                          onClick={() => void saveRename(entry)}
                          type="button"
                        >
                          {isRenaming ? "正在保存…" : "保存名称"}
                        </button>
                        <button
                          className="secondary-button"
                          disabled={isRenaming}
                          onClick={cancelRename}
                          type="button"
                        >
                          取消
                        </button>
                      </div>
                    ) : (
                      <>
                        <div className="character-preview-card__title">
                          <span className="character-preview-card__name">
                            {entry.display_name}
                          </span>
                          {entry.status === "ARCHIVED" ? (
                            <span className="status-badge">已归档</span>
                          ) : null}
                          {entry.generation_source === "local_placeholder" ? (
                            <span className="status-badge status-badge--warning">
                              本地占位结果
                            </span>
                          ) : null}
                        </div>
                        <div className="character-preview-card__actions">
                          {onOpenProfile ? (
                            <button
                              className="secondary-button"
                              onClick={() => onOpenProfile(entry.identity_id)}
                              type="button"
                            >
                              完整档案
                            </button>
                          ) : null}
                          {canRename(entry) ? (
                            <button
                              className="secondary-button"
                              onClick={() => startRename(entry)}
                              type="button"
                            >
                              改名
                            </button>
                          ) : null}
                          {canRename(entry) ? (
                            <button
                              aria-label={`重新生成人物 ${entry.display_name} 的五视图`}
                              className="secondary-button"
                              disabled={busyRegenerateId !== ""}
                              onClick={() => void handleRegenerate(entry)}
                              type="button"
                            >
                              {busyRegenerateId === entry.identity_id
                                ? "正在重新生成…"
                                : "重新生成五视图"}
                            </button>
                          ) : null}
                          {canDelete(entry) ? (
                            <button
                              aria-label={`删除人物 ${entry.display_name}`}
                              className="secondary-button"
                              disabled={busyDeleteId !== ""}
                              onClick={() => void handleDelete(entry)}
                              type="button"
                            >
                              {busyDeleteId === entry.identity_id
                                ? "正在删除…"
                                : "删除"}
                            </button>
                          ) : null}
                        </div>
                        <small>场景造型 {entry.scene_look_count} 套</small>
                      </>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
          {nextCursor ? (
            <button
              className="secondary-button"
              disabled={isLoadingMore}
              onClick={() => void loadMoreCharacters()}
              type="button"
            >
              {isLoadingMore ? "正在加载…" : "加载更多人物"}
            </button>
          ) : null}
        </>
      )}
      {lightboxEntry ? (
        <CharacterLightbox
          busyDownloadKey={busyDownloadKey}
          canManage={canManage}
          entry={lightboxEntry}
          initialTab={initialTab}
          onClose={() => setLightboxId("")}
          onDownloadSheet={handleDownloadSheet}
          onDownloadView={handleDownloadView}
          onDownloadAll={handleDownloadAll}
          onSceneCreated={onChanged}
          previewUrls={previewUrls}
        />
      ) : null}
    </section>
  );
}

export function PendingCharacterCard({
  character,
  onClear,
}: {
  character: PendingCharacterState;
  onClear: () => void;
}) {
  return (
    <li
      aria-label={`人物 ${character.displayName} 生成进度`}
      className="character-preview-card character-preview-card--generating"
    >
      <div className="character-preview-card__cover character-generation-card__cover">
        {character.sourcePreviewUrl ? (
          <img
            alt={`${character.displayName} 授权原图`}
            src={character.sourcePreviewUrl}
          />
        ) : (
          <span className="source-frame-placeholder">云端任务已恢复</span>
        )}
        <div className="character-generation-card__overlay">
          <strong>
            {character.status === "error"
              ? "生成未完成"
              : `${character.progress}%`}
          </strong>
          <span>{character.stage}</span>
        </div>
      </div>
      <div className="character-preview-card__body">
        <div className="character-preview-card__title">
          <span className="character-preview-card__name">
            {character.displayName}
          </span>
          <span className="status-badge">
            {character.status === "error" ? "失败" : "生成中"}
          </span>
        </div>
        <progress
          aria-label={`${character.displayName} 预计生成进度`}
          max={100}
          value={character.progress}
        />
        <p className="character-generation-card__note">
          {character.status === "error"
            ? "请检查提示后重新提交。"
            : "预计需要 1–3 分钟，可离开当前页面，任务会在后台继续。"}
        </p>
        {character.status === "error" ? (
          <button className="secondary-button" onClick={onClear} type="button">
            移除失败任务
          </button>
        ) : null}
      </div>
    </li>
  );
}

// 人物灯箱：点卡片封面放大查看——有拼合图看拼合全图，无则退回视角
// 网格；下载拼合图/单视角/全部集中在这里，卡片操作行只留管理动作。
// 关闭方式：Esc、点遮罩、右上「关闭」。
function CharacterLightbox({
  busyDownloadKey,
  canManage,
  entry,
  initialTab,
  onClose,
  onDownloadSheet,
  onDownloadView,
  onDownloadAll,
  onSceneCreated,
  previewUrls,
}: {
  busyDownloadKey: string;
  canManage: boolean;
  entry: SimpleLibraryEntry;
  initialTab: "base" | "scenes";
  onClose: () => void;
  onDownloadSheet: (entry: SimpleLibraryEntry) => Promise<void>;
  onDownloadView: (
    entry: SimpleLibraryEntry,
    view: SimpleCharacterView,
  ) => Promise<void>;
  onDownloadAll: (entry: SimpleLibraryEntry) => Promise<void>;
  onSceneCreated?: () => void;
  previewUrls: Record<string, string>;
}) {
  const [activeTab, setActiveTab] = useState<"base" | "scenes">(initialTab);
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const sheetUrl = entry.contact_sheet_asset_id
    ? previewUrls[entry.contact_sheet_asset_id]
    : undefined;
  const isDownloadingAll = busyDownloadKey === entry.identity_id;

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: 点遮罩关闭是鼠标增强；键盘用户由下方 Esc 监听兜底。
    // biome-ignore lint/a11y/useKeyWithClickEvents: 键盘关闭走全局 Esc 监听（见上方 useEffect）。
    <div
      className="character-lightbox"
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div
        aria-label={`人物预览 ${entry.display_name}`}
        aria-modal="true"
        className="character-lightbox__dialog"
        role="dialog"
      >
        <div className="character-lightbox__head">
          <div>
            <span className="character-detail__eyebrow">人物形象</span>
            <h3 className="character-lightbox__title">{entry.display_name}</h3>
            <p>人物身份保持不变，不同造型只调整场景、服装与视觉氛围。</p>
          </div>
          <button
            aria-label="关闭人物预览"
            className="secondary-button"
            onClick={onClose}
            type="button"
          >
            关闭
          </button>
        </div>
        <div
          aria-label="人物形象类型"
          className="character-detail__tabs"
          role="tablist"
        >
          <button
            aria-selected={activeTab === "base"}
            className={activeTab === "base" ? "is-active" : undefined}
            onClick={() => setActiveTab("base")}
            role="tab"
            type="button"
          >
            人物基准
          </button>
          <button
            aria-selected={activeTab === "scenes"}
            className={activeTab === "scenes" ? "is-active" : undefined}
            onClick={() => setActiveTab("scenes")}
            role="tab"
            type="button"
          >
            场景造型
          </button>
        </div>
        <div hidden={activeTab !== "scenes"}>
          <CharacterScenePanel
            key={entry.identity_id}
            identityId={entry.identity_id}
            displayName={entry.display_name}
            canManage={canManage}
            onChanged={onSceneCreated}
            onActiveTask={() => setActiveTab("scenes")}
          />
        </div>
        {activeTab !== "base" ? null : entry.contact_sheet_asset_id ? (
          <div className="character-contact-sheet">
            {sheetUrl ? (
              <img alt={`${entry.display_name} 五视图拼合图`} src={sheetUrl} />
            ) : (
              <span className="source-frame-placeholder">拼合图加载中…</span>
            )}
            {canManage ? (
              <div className="character-contact-sheet__bar">
                <span className="character-contact-sheet__label">
                  五视图拼合图
                </span>
                <button
                  className="secondary-button"
                  disabled={Boolean(busyDownloadKey) || !sheetUrl}
                  onClick={() => void onDownloadSheet(entry)}
                  type="button"
                >
                  {busyDownloadKey === entry.contact_sheet_asset_id
                    ? "下载中…"
                    : "下载拼合图"}
                </button>
              </div>
            ) : null}
          </div>
        ) : entry.views.length ? (
          <div className="character-lightbox__views">
            <div className="character-contact-sheet__bar">
              <span className="character-contact-sheet__label">
                视角图（{entry.views.length} 张）
              </span>
              {canManage ? (
                <button
                  className="secondary-button"
                  disabled={Boolean(busyDownloadKey)}
                  onClick={() => void onDownloadAll(entry)}
                  type="button"
                >
                  {isDownloadingAll
                    ? "正在下载全部…"
                    : `下载全部（${entry.views.length} 张）`}
                </button>
              ) : null}
            </div>
            <div className="character-preview-grid">
              {entry.views.map((view) => {
                const previewUrl = previewUrls[view.asset_id];
                const isDownloadingView = busyDownloadKey === view.asset_id;
                return (
                  <figure
                    className="character-preview-item"
                    key={view.asset_id}
                  >
                    {previewUrl ? (
                      <img
                        alt={`${entry.display_name} ${VIEW_LABELS[view.view_type]}`}
                        src={previewUrl}
                      />
                    ) : (
                      <span className="source-frame-placeholder">
                        预览加载中…
                      </span>
                    )}
                    <figcaption>
                      <span>{VIEW_LABELS[view.view_type]}</span>
                      {canManage ? (
                        <button
                          className="secondary-button"
                          disabled={Boolean(busyDownloadKey) || !previewUrl}
                          onClick={() => void onDownloadView(entry, view)}
                          type="button"
                        >
                          {isDownloadingView ? "下载中…" : "下载"}
                        </button>
                      ) : null}
                    </figcaption>
                  </figure>
                );
              })}
            </div>
          </div>
        ) : (
          <p className="status-note">该人物暂无已发布的视角图。</p>
        )}
      </div>
    </div>
  );
}
