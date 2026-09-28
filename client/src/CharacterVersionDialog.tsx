import { useEffect, useRef, useState } from "react";
import {
  type CharacterAsset,
  type CharacterGenerationTask,
  type CharacterReviewDecision,
  type CharacterVersion,
  createCharacterVersion,
  generateCharacterAssets,
  getCachedCharacterAssetUrl,
  listCharacterAssets,
  listCharacterGenerationTasks,
  listCharacterVersions,
  publishCharacterVersion,
  type RequiredCharacterViewType,
  regenerateCharacterAsset,
  reviewCharacterAsset,
} from "./api";
import "./legacy-panels.css";

const VIEW_LABELS: Record<RequiredCharacterViewType, string> = {
  FRONT_FACE: "正脸近景",
  FRONT_HALF: "正面半身",
  FRONT_FULL: "正面全身",
  LEFT_45: "左 45°",
  LEFT_SIDE: "左侧面",
};

// 与服务端 REQUIRED_CHARACTER_VIEW_TYPES 同序；发布时必须五视角齐全。
const REQUIRED_VIEWS: RequiredCharacterViewType[] = [
  "FRONT_FACE",
  "FRONT_HALF",
  "FRONT_FULL",
  "LEFT_45",
  "LEFT_SIDE",
];

const VERSION_STATUS_LABELS: Record<CharacterVersion["status"], string> = {
  ARCHIVED: "已归档",
  DRAFT: "草稿",
  FAILED: "生成失败",
  GENERATING: "生成中",
  PUBLISHED: "已发布",
  REVIEWING: "待审核",
};

// 角色五视角按 gpt-image-2 竖版 1024x1536 生成：与简单人物上传链路的模型选型
// 一致，且该模型输出 PNG，满足发布链路「必需 PNG」的约束。供应商名称不出现在界面。
const CHARACTER_VERSION_PROVIDER = "apilio";
const CHARACTER_VERSION_MODEL = "gpt-image-2";
const POLL_INTERVAL_MS = 5000;

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function isActiveTask(task: CharacterGenerationTask): boolean {
  return task.status === "PENDING" || task.status === "RUNNING";
}

function reviewStatusLabel(status: CharacterAsset["review_status"]): string {
  if (status === "APPROVED") return "已批准";
  if (status === "REJECTED") return "已驳回";
  return "待审核";
}

// 角色版本工作流：创建版本 → 生成五个标准视角 → 逐图审核（含重生成）→
// 全部批准后发布。发布前关闭对话框不中断服务端任务；重新打开按任务状态续传。
export function CharacterVersionDialog({
  personaId,
  displayName,
  onClose,
  onPublished,
}: {
  personaId: string;
  displayName: string;
  onClose: () => void;
  onPublished?: () => void;
}) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [version, setVersion] = useState<CharacterVersion | null>(null);
  const [tasks, setTasks] = useState<CharacterGenerationTask[]>([]);
  const [assets, setAssets] = useState<CharacterAsset[]>([]);
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});
  const [selection, setSelection] = useState<
    Partial<Record<RequiredCharacterViewType, string>>
  >({});
  const [busy, setBusy] = useState("");
  const [revision, setRevision] = useState(0);
  const pollTimerRef = useRef(0);
  const submissionRef = useRef<{ fingerprint: string; key: string } | null>(
    null,
  );
  const hasActiveTasks = tasks.some(isActiveTask);

  // 读取版本、任务与候选；有活跃任务时自排下一次轮询（revision 触发重读）。
  // biome-ignore lint/correctness/useExhaustiveDependencies: revision 是刻意的重载触发器——轮询定时器与「刷新」按钮都自增它，effect 体内不需要读值。
  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const versions = await listCharacterVersions(personaId);
        if (!active) return;
        const target =
          [...versions].sort(
            (a, b) => b.version_number - a.version_number,
          )[0] ?? null;
        setVersion(target);
        if (!target) {
          setTasks([]);
          setAssets([]);
          return;
        }
        const [nextTasks, nextAssets] = await Promise.all([
          listCharacterGenerationTasks(target.id),
          listCharacterAssets(target.id),
        ]);
        if (!active) return;
        setTasks(nextTasks);
        setAssets(nextAssets);
        setError("");
        if (nextTasks.some(isActiveTask)) {
          pollTimerRef.current = window.setTimeout(
            () => setRevision((value) => value + 1),
            POLL_INTERVAL_MS,
          );
        }
      } catch (cause) {
        if (active) {
          setError(errorMessage(cause, "角色版本暂不可用，请重试。"));
        }
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
      window.clearTimeout(pollTimerRef.current);
    };
  }, [personaId, revision]);

  const assetKey = assets
    .flatMap((asset) => (asset.asset_id ? [asset.asset_id] : []))
    .join("\n");
  useEffect(() => {
    let active = true;
    const ids = [...new Set(assetKey.split("\n").filter(Boolean))];
    if (ids.length === 0) {
      return;
    }
    void Promise.allSettled(
      ids.map(
        async (assetId) =>
          [assetId, (await getCachedCharacterAssetUrl(assetId)).url] as const,
      ),
    ).then((results) => {
      if (!active) return;
      setPreviewUrls(
        Object.fromEntries(
          results.flatMap((result) =>
            result.status === "fulfilled" ? [result.value] : [],
          ),
        ),
      );
    });
    return () => {
      active = false;
    };
  }, [assetKey]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  // 同一次提交在未确认结果前复用幂等键：网络超时重试不会重复生成候选；
  // 一旦本轮返回成功就换新键，下一次点击是真正的新一轮生成。
  function idempotencyKeyFor(fingerprint: string): string {
    if (submissionRef.current?.fingerprint !== fingerprint) {
      submissionRef.current = { fingerprint, key: crypto.randomUUID() };
    }
    return submissionRef.current.key;
  }

  async function startGeneration(
    versionId: string,
    viewTypes?: RequiredCharacterViewType[],
  ) {
    const views = viewTypes ?? REQUIRED_VIEWS;
    const fingerprint = `generate:${versionId}:${views.join(",")}`;
    setBusy("generate");
    setError("");
    setMessage("");
    try {
      await generateCharacterAssets(versionId, {
        idempotency_key: idempotencyKeyFor(fingerprint),
        candidates_per_view: 1,
        ...(viewTypes ? { view_types: viewTypes } : {}),
      });
      submissionRef.current = null;
      setMessage(`已提交 ${views.length} 个视角生成，完成后会自动刷新。`);
      setRevision((value) => value + 1);
    } catch (cause) {
      setError(errorMessage(cause, "启动视角生成失败，请重试。"));
    } finally {
      setBusy("");
    }
  }

  async function createVersionAndGenerate() {
    setBusy("create");
    setError("");
    setMessage("");
    try {
      const created = await createCharacterVersion(personaId, {
        provider: CHARACTER_VERSION_PROVIDER,
        model: CHARACTER_VERSION_MODEL,
        generation_params_json: {},
      });
      setVersion(created);
      setMessage(`已创建角色版本 v${created.version_number}。`);
      await startGeneration(created.id);
    } catch (cause) {
      setError(errorMessage(cause, "创建角色版本失败，请重试。"));
    } finally {
      setBusy("");
    }
  }

  async function review(
    asset: CharacterAsset,
    decision: CharacterReviewDecision,
  ) {
    setBusy(`review:${asset.id}`);
    setError("");
    setMessage("");
    try {
      await reviewCharacterAsset(asset.id, decision, "");
      setMessage(
        decision === "APPROVED" ? "已批准该候选图。" : "已驳回该候选图。",
      );
      setRevision((value) => value + 1);
    } catch (cause) {
      setError(
        errorMessage(
          cause,
          decision === "APPROVED"
            ? "批准候选图失败，请重试。"
            : "驳回候选图失败，请重试。",
        ),
      );
    } finally {
      setBusy("");
    }
  }

  async function regenerate(asset: CharacterAsset) {
    const fingerprint = `regenerate:${asset.id}`;
    setBusy(`regenerate:${asset.id}`);
    setError("");
    setMessage("");
    try {
      await regenerateCharacterAsset(asset.id, idempotencyKeyFor(fingerprint));
      submissionRef.current = null;
      setMessage("已提交该视角的重新生成，完成后会自动刷新。");
      setRevision((value) => value + 1);
    } catch (cause) {
      setError(errorMessage(cause, "重新生成该视角失败，请重试。"));
    } finally {
      setBusy("");
    }
  }

  async function publish() {
    const selected = effectiveSelection();
    if (!version || !selected) return;
    setBusy("publish");
    setError("");
    setMessage("");
    try {
      const published = await publishCharacterVersion(version.id, selected);
      setVersion(published);
      setMessage(
        `角色版本 v${published.version_number} 已发布，五个视角已冻结。`,
      );
      onPublished?.();
      setRevision((value) => value + 1);
    } catch (cause) {
      setError(errorMessage(cause, "发布角色版本失败，请重试。"));
    } finally {
      setBusy("");
    }
  }

  function approvedByView(view: RequiredCharacterViewType): CharacterAsset[] {
    return assets
      .filter(
        (asset) =>
          asset.view_type === view && asset.review_status === "APPROVED",
      )
      .sort((a, b) => a.candidate_number - b.candidate_number);
  }

  // 发布选择：显式选择仍为已批准时沿用，否则回退到该视角最新批准的候选。
  function effectiveSelection(): Record<
    RequiredCharacterViewType,
    string
  > | null {
    const result = {} as Record<RequiredCharacterViewType, string>;
    for (const view of REQUIRED_VIEWS) {
      const approved = approvedByView(view);
      if (approved.length === 0) return null;
      const chosen =
        approved.find((asset) => asset.id === selection[view]) ??
        approved[approved.length - 1];
      result[view] = chosen.id;
    }
    return result;
  }

  function renderViewSection(view: RequiredCharacterViewType) {
    if (!version) return null;
    const viewAssets = assets
      .filter((asset) => asset.view_type === view)
      .sort((a, b) => a.candidate_number - b.candidate_number);
    const viewTasks = tasks.filter((task) => task.view_type === view);
    const active = viewTasks.some(isActiveTask);
    const latestTask = viewTasks.length
      ? viewTasks[viewTasks.length - 1]
      : null;
    const taskFailed = latestTask?.status === "FAILED";
    const latestAsset = viewAssets.length
      ? viewAssets[viewAssets.length - 1]
      : null;
    const viewApproved = approvedByView(view);
    const effectiveForView =
      viewApproved.find((asset) => asset.id === selection[view]) ??
      viewApproved[viewApproved.length - 1];
    return (
      <section
        aria-label={`${VIEW_LABELS[view]}候选`}
        className="character-scene-views"
        key={view}
      >
        <div className="character-scene-views__head">
          <span>{VIEW_LABELS[view]}</span>
          <div className="character-scene-panel__actions">
            {active ? (
              <span className="status-badge status-badge--warning">生成中</span>
            ) : null}
            {!active && taskFailed && viewAssets.length === 0 ? (
              <button
                className="secondary-button"
                disabled={busy !== ""}
                onClick={() => void startGeneration(version.id, [view])}
                type="button"
              >
                重试该视角
              </button>
            ) : null}
            {!active && latestAsset ? (
              <button
                className="secondary-button"
                disabled={busy !== ""}
                onClick={() => void regenerate(latestAsset)}
                type="button"
              >
                {busy === `regenerate:${latestAsset.id}`
                  ? "正在提交…"
                  : "重新生成该视角"}
              </button>
            ) : null}
          </div>
        </div>
        {viewAssets.length ? (
          <div className="character-preview-grid">
            {viewAssets.map((asset) => {
              const url = asset.asset_id
                ? previewUrls[asset.asset_id]
                : undefined;
              return (
                <figure className="character-preview-item" key={asset.id}>
                  {url ? (
                    <img
                      alt={`${displayName} ${VIEW_LABELS[view]} 候选 ${asset.candidate_number}`}
                      loading="lazy"
                      src={url}
                    />
                  ) : (
                    <span className="source-frame-placeholder">
                      图片加载中…
                    </span>
                  )}
                  <figcaption>
                    <span>候选 {asset.candidate_number}</span>
                    <span
                      className={
                        asset.review_status === "REJECTED"
                          ? "status-badge status-badge--warning"
                          : "status-badge"
                      }
                    >
                      {reviewStatusLabel(asset.review_status)}
                    </span>
                  </figcaption>
                  <div className="character-version-candidate-actions">
                    <button
                      className="secondary-button"
                      disabled={
                        busy !== "" || asset.review_status === "APPROVED"
                      }
                      onClick={() => void review(asset, "APPROVED")}
                      type="button"
                    >
                      {busy === `review:${asset.id}` ? "处理中…" : "批准"}
                    </button>
                    <button
                      className="secondary-button"
                      disabled={
                        busy !== "" || asset.review_status === "REJECTED"
                      }
                      onClick={() => void review(asset, "REJECTED")}
                      type="button"
                    >
                      驳回
                    </button>
                  </div>
                  {asset.review_status === "APPROVED" ? (
                    <label className="character-version-select">
                      <input
                        checked={effectiveForView?.id === asset.id}
                        disabled={busy !== ""}
                        name={`character-publish-${view}`}
                        onChange={() =>
                          setSelection((current) => ({
                            ...current,
                            [view]: asset.id,
                          }))
                        }
                        type="radio"
                      />
                      选为发布版本
                    </label>
                  ) : null}
                </figure>
              );
            })}
          </div>
        ) : active ? (
          <p className="status-note">该视角正在生成，完成后自动显示候选图。</p>
        ) : taskFailed ? (
          <p className="status-note">
            该视角生成未成功
            {latestTask?.error_code ? `（${latestTask.error_code}）` : ""}
            ，可点击「重试该视角」重新提交。
          </p>
        ) : viewTasks.length === 0 ? (
          <p className="status-note">该视角尚未提交生成。</p>
        ) : (
          <p className="status-note">该视角暂无候选图。</p>
        )}
      </section>
    );
  }

  const missingViews = REQUIRED_VIEWS.filter(
    (view) => !assets.some((asset) => asset.view_type === view),
  );
  const viewsWithAssets = REQUIRED_VIEWS.length - missingViews.length;
  const readyCount = REQUIRED_VIEWS.filter((view) =>
    assets.some(
      (asset) => asset.view_type === view && asset.review_status === "APPROVED",
    ),
  ).length;
  const selectionComplete = readyCount === REQUIRED_VIEWS.length;
  const versionOpen = Boolean(
    version && version.status !== "PUBLISHED" && version.status !== "ARCHIVED",
  );
  const canGenerate = Boolean(
    versionOpen && missingViews.length > 0 && !hasActiveTasks && busy === "",
  );
  const canPublish = Boolean(
    version?.status === "REVIEWING" &&
      !hasActiveTasks &&
      selectionComplete &&
      busy === "",
  );

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: 点遮罩关闭是鼠标增强；键盘关闭走全局 Esc 监听。
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
        aria-label={`角色版本工作流 ${displayName}`}
        aria-modal="true"
        className="character-lightbox__dialog"
        role="dialog"
      >
        <div className="character-lightbox__head">
          <div>
            <span className="character-detail__eyebrow">角色版本工作流</span>
            <h3 className="character-lightbox__title">{displayName}</h3>
            <p>
              创建版本 → 生成五个标准视角 → 逐图审核（可重生成）→
              全部批准后发布。
            </p>
          </div>
          <div className="character-scene-panel__actions">
            <button
              className="secondary-button"
              disabled={loading}
              onClick={() => setRevision((value) => value + 1)}
              type="button"
            >
              刷新
            </button>
            <button
              aria-label="关闭角色版本工作流"
              className="secondary-button"
              onClick={onClose}
              type="button"
            >
              关闭
            </button>
          </div>
        </div>

        {error ? (
          <p className="settings-error" role="alert">
            {error}
          </p>
        ) : null}
        {message ? (
          <p className="setup-success" role="status">
            {message}
          </p>
        ) : null}
        {loading && !version && !error ? (
          <p className="status-note">正在读取角色版本…</p>
        ) : null}

        {!loading && !version ? (
          <div className="character-scene-empty">
            <strong>还没有角色版本</strong>
            <p>
              创建后按正脸近景、正面半身、正面全身、左
              45°、左侧面五个视角各生成一张候选图； 全部批准后才能发布。
            </p>
            <button
              className="primary-button"
              disabled={busy !== ""}
              onClick={() => void createVersionAndGenerate()}
              type="button"
            >
              {busy === "create" ? "正在创建…" : "创建版本并生成五视图"}
            </button>
          </div>
        ) : null}

        {version ? (
          <div className="character-version-meta">
            <strong>v{version.version_number}</strong>
            <span className="status-badge">
              {VERSION_STATUS_LABELS[version.status]}
            </span>
            <p>
              {version.status === "PUBLISHED"
                ? "五个视角已冻结，如需调整请创建新版本。"
                : `已出图 ${viewsWithAssets}/5 · 已批准 ${readyCount}/5`}
            </p>
          </div>
        ) : null}

        {version && hasActiveTasks ? (
          <div
            aria-label="角色五视图生成进度"
            aria-live="polite"
            className="character-scene-progress"
            role="status"
          >
            <span
              aria-hidden="true"
              className="character-scene-progress__indicator"
            />
            <div>
              <strong>正在生成视角候选</strong>
              <p>{viewsWithAssets}/5 个视角已出图，完成后会自动刷新。</p>
              <small>
                可以关闭对话框，任务会在后台继续；重新打开会自动恢复进度。
              </small>
            </div>
          </div>
        ) : null}

        {version && canGenerate ? (
          <div className="character-scene-panel__actions">
            <button
              className="primary-button"
              disabled={busy !== ""}
              onClick={() =>
                void startGeneration(
                  version.id,
                  missingViews.length === REQUIRED_VIEWS.length
                    ? undefined
                    : missingViews,
                )
              }
              type="button"
            >
              {busy === "generate"
                ? "正在提交…"
                : missingViews.length === REQUIRED_VIEWS.length
                  ? "生成五视图"
                  : `补生成 ${missingViews.length} 个视角`}
            </button>
          </div>
        ) : null}

        {versionOpen && version
          ? REQUIRED_VIEWS.map((view) => renderViewSection(view))
          : null}

        {version && versionOpen ? (
          <div
            className={`character-version-publish${
              selectionComplete ? " character-version-publish--ready" : ""
            }`}
          >
            <button
              className="primary-button"
              disabled={!canPublish}
              onClick={() => void publish()}
              type="button"
            >
              {busy === "publish" ? "正在发布…" : "发布角色版本"}
            </button>
            <p>
              {selectionComplete
                ? version.status === "REVIEWING" && !hasActiveTasks
                  ? "五个视角均已批准，可以发布；发布后本版本选择将冻结。"
                  : "五个视角均已批准，等生成任务全部结束后即可发布。"
                : `发布前需为五个视角各批准并选择一张候选（已批准 ${readyCount}/5）。`}
            </p>
          </div>
        ) : null}

        {version?.status === "PUBLISHED" ? (
          <>
            <div className="character-preview-grid">
              {REQUIRED_VIEWS.map((view) => {
                const asset = assets.find(
                  (item) =>
                    item.is_published_selection && item.view_type === view,
                );
                const url = asset?.asset_id
                  ? previewUrls[asset.asset_id]
                  : undefined;
                return (
                  <figure className="character-preview-item" key={view}>
                    {url ? (
                      <img
                        alt={`${displayName} ${VIEW_LABELS[view]}`}
                        src={url}
                      />
                    ) : (
                      <span className="source-frame-placeholder">
                        已发布视角
                      </span>
                    )}
                    <figcaption>
                      <span>{VIEW_LABELS[view]}</span>
                      <span className="status-badge">已发布</span>
                    </figcaption>
                  </figure>
                );
              })}
            </div>
            <div className="character-version-publish">
              <button
                className="primary-button"
                disabled={busy !== ""}
                onClick={() => void createVersionAndGenerate()}
                type="button"
              >
                {busy === "create" ? "正在创建…" : "创建新版本"}
              </button>
              <p>
                新版本会基于同一人物人设重新生成五个视角，已发布版本保持不变。
              </p>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}
