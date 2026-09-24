import {
  type FormEvent,
  type ReactNode,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  type CharacterSheetTask,
  createCharacterSceneLook,
  getCachedCharacterAssetUrl,
  getLatestSceneLookTask,
  listCharacterSceneLooks,
  type SimpleSceneLook,
  waitForCharacterSheetTask,
} from "./api";
import "./legacy-panels.css";

function sceneError(cause: unknown) {
  return cause instanceof Error
    ? cause.message
    : "场景造型暂不可用，请稍后重试。";
}

const viewLabels: Record<string, string> = {
  FRONT_FULL: "正面全身",
  FRONT_HALF: "正面半身",
  FRONT_FACE: "正面特写",
  LEFT_45: "左侧 45°",
  LEFT_SIDE: "左侧面",
};

/** 人物详情与人物管理共用同一场景列表、任务恢复和生成表单。 */
export function CharacterScenePanel({
  identityId,
  displayName,
  canManage,
  createRequest = 0,
  hideCreateButton = false,
  showResults = true,
  resultCount,
  onChanged,
  onRefresh,
  onActiveTask,
  renderActions,
}: {
  identityId: string;
  displayName: string;
  canManage: boolean;
  createRequest?: number;
  hideCreateButton?: boolean;
  showResults?: boolean;
  resultCount?: number;
  onChanged?: () => void;
  onRefresh?: () => void;
  onActiveTask?: () => void;
  renderActions?: (look: SimpleSceneLook, photoUrl?: string) => ReactNode;
}) {
  const [looks, setLooks] = useState<SimpleSceneLook[]>([]);
  const [urls, setUrls] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [taskChecked, setTaskChecked] = useState(false);
  const [revision, setRevision] = useState(0);
  const [formOpen, setFormOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [costume, setCostume] = useState("");
  const [generating, setGenerating] = useState(false);
  const [task, setTask] = useState<CharacterSheetTask | null>(null);
  const [submittedName, setSubmittedName] = useState("");
  const [expandedId, setExpandedId] = useState("");
  const [limit, setLimit] = useState(12);
  const scopeRef = useRef<object | null>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const callbacks = useRef({ onChanged, onActiveTask, onRefresh });
  callbacks.current = { onChanged, onActiveTask, onRefresh };
  const pending =
    generating || task?.status === "PENDING" || task?.status === "RUNNING";
  const uncertain = task?.status === "SUBMISSION_UNCERTAIN";

  useEffect(() => {
    if (createRequest > 0 && canManage) setFormOpen(true);
  }, [createRequest, canManage]);
  useEffect(() => {
    if (formOpen && createRequest > 0)
      formRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [formOpen, createRequest]);

  useEffect(() => {
    const scope = { revision };
    scopeRef.current = scope;
    const active = () => scopeRef.current === scope;
    setLoading(true);
    setTaskChecked(false);
    setError("");
    void (async () => {
      try {
        const [itemsResult, taskResult] = await Promise.allSettled([
          showResults
            ? listCharacterSceneLooks(identityId)
            : Promise.resolve([]),
          getLatestSceneLookTask(identityId),
        ]);
        if (!active()) return;
        if (itemsResult.status === "fulfilled") setLooks(itemsResult.value);
        else setError(sceneError(itemsResult.reason));
        if (taskResult.status === "rejected")
          throw new Error(
            "读取生成任务状态失败，请刷新场景后再生成，避免重复提交。",
          );
        const latest = taskResult.value;
        setTaskChecked(true);
        setTask(latest);
        setLoading(false);
        if (revision > 0) callbacks.current.onRefresh?.();
        if (latest?.status === "PENDING" || latest?.status === "RUNNING") {
          callbacks.current.onActiveTask?.();
          const result = await waitForCharacterSheetTask(latest.id, (next) => {
            if (active()) setTask(next);
          });
          if (!active()) return;
          const restored = result.result;
          if (restored && "scene_name" in restored) {
            setLooks((current) => [
              restored,
              ...current.filter(
                (look) => look.persona_id !== restored.persona_id,
              ),
            ]);
          } else {
            const refreshed = await listCharacterSceneLooks(identityId);
            if (active()) setLooks(refreshed);
          }
          if (active()) {
            setTask(result);
            callbacks.current.onChanged?.();
          }
        } else if (
          latest?.status === "FAILED" ||
          latest?.status === "SUBMISSION_UNCERTAIN"
        ) {
          setError(
            latest.error_message || "场景造型生成未完成，请查看任务状态。",
          );
        }
      } catch (cause) {
        if (active()) setError(sceneError(cause));
      } finally {
        if (active()) setLoading(false);
      }
    })();
    return () => {
      if (active()) scopeRef.current = null;
    };
  }, [identityId, revision, showResults]);

  const assetKey = (showResults ? looks : [])
    .slice(0, limit)
    .flatMap((look) => [
      look.contact_sheet_asset_id,
      ...look.views.map((view) => view.asset_id),
    ])
    .filter(Boolean)
    .join("\n");
  useEffect(() => {
    // Explicit refresh retries signed previews even when their IDs have not changed.
    void revision;
    let active = true;
    const ids = [...new Set(assetKey.split("\n").filter(Boolean))];
    void Promise.allSettled(
      ids.map(
        async (id) => [id, (await getCachedCharacterAssetUrl(id)).url] as const,
      ),
    ).then((results) => {
      if (!active) return;
      setUrls(
        Object.fromEntries(
          results.flatMap((result) =>
            result.status === "fulfilled" ? [result.value] : [],
          ),
        ),
      );
      if (results.some((result) => result.status === "rejected"))
        setError("部分图片暂时无法加载，可点击刷新场景重试。");
    });
    return () => {
      active = false;
    };
  }, [assetKey, revision]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canManage || pending || loading || !taskChecked || uncertain) return;
    const input = {
      scene_name: name.trim(),
      scene_description: description.trim(),
      costume_description: costume.trim(),
    };
    if (
      !input.scene_name ||
      !input.scene_description ||
      !input.costume_description
    ) {
      setError("请完整填写场景名称、场景描述和服装描述。");
      return;
    }
    const scope = scopeRef.current;
    const active = () => scope !== null && scopeRef.current === scope;
    setGenerating(true);
    setTask(null);
    setSubmittedName(input.scene_name);
    setError("");
    let observedTask = false;
    try {
      const created = await createCharacterSceneLook(
        identityId,
        input,
        (next) => {
          observedTask = true;
          if (active()) setTask(next);
        },
      );
      if (!active()) return;
      setLooks((current) => [
        created,
        ...current.filter((look) => look.persona_id !== created.persona_id),
      ]);
      setTask(null);
      setFormOpen(false);
      setName("");
      setDescription("");
      setCostume("");
      callbacks.current.onChanged?.();
    } catch (cause) {
      if (active()) {
        if (!observedTask) setTaskChecked(false);
        setError(
          observedTask
            ? sceneError(cause)
            : `${sceneError(cause)} 请先刷新场景核对提交状态，再决定是否重新生成。`,
        );
      }
    } finally {
      if (active()) setGenerating(false);
    }
  }

  return (
    <section className="character-scene-panel" aria-label="场景形象照">
      <div className="character-scene-panel__head">
        <div>
          <h3>场景形象照</h3>
          <p>每套包含一张五视图合成图，可展开查看单独视角。</p>
        </div>
        <div className="character-scene-panel__actions">
          <button
            className="secondary-button"
            type="button"
            disabled={generating || loading || (pending && !error)}
            onClick={() => setRevision((value) => value + 1)}
          >
            刷新场景
          </button>
          {canManage && (!hideCreateButton || formOpen) ? (
            <button
              className="primary-button"
              type="button"
              onClick={() => setFormOpen((value) => !value)}
            >
              {formOpen ? "收起" : "新增场景造型"}
            </button>
          ) : null}
        </div>
      </div>
      {pending ? (
        <div
          className="character-scene-progress"
          role="status"
          aria-label="场景造型生成进度"
          aria-live="polite"
        >
          <span
            className="character-scene-progress__indicator"
            aria-hidden="true"
          />
          <div>
            <strong>{submittedName || "场景造型"}</strong>
            <p>
              {task?.status === "PENDING"
                ? "已提交，等待开始生成…"
                : task?.status === "RUNNING"
                  ? "正在生成五视图，完成后会自动显示在这里。"
                  : "正在提交场景生成任务…"}
            </p>
            <small>
              可收起表单；返回此人物时会恢复任务状态，请勿重复提交。
            </small>
          </div>
        </div>
      ) : null}
      {uncertain ? (
        <p className="settings-error" role="alert">
          提交结果待核对，请先刷新场景确认，避免重复生成。
        </p>
      ) : null}
      {formOpen && canManage ? (
        <form ref={formRef} className="character-scene-form" onSubmit={submit}>
          <label>
            场景名称
            <input
              required
              maxLength={80}
              placeholder="例如：工地巡检"
              value={name}
              disabled={pending}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label>
            场景描述
            <textarea
              required
              rows={3}
              maxLength={600}
              placeholder="例如：乡村别墅施工现场，白天自然光"
              value={description}
              disabled={pending}
              onChange={(event) => setDescription(event.target.value)}
            />
          </label>
          <label>
            服装描述
            <textarea
              required
              rows={3}
              maxLength={600}
              placeholder="例如：黄色安全帽、深蓝色工装和反光背心"
              value={costume}
              disabled={pending}
              onChange={(event) => setCostume(event.target.value)}
            />
          </label>
          <p>生成完成后保存到当前人物，每次生成一张五视图合成图。</p>
          <button
            className="primary-button"
            disabled={pending || loading || !taskChecked || uncertain}
            type="submit"
          >
            {pending ? "正在生成，预计 1–3 分钟…" : "生成场景五视图"}
          </button>
        </form>
      ) : null}
      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}
      {loading && looks.length === 0 ? (
        <p className="status-note" role="status">
          正在读取场景造型…
        </p>
      ) : null}
      {(showResults || resultCount === 0) &&
      !loading &&
      !pending &&
      !formOpen &&
      looks.length === 0 &&
      !error ? (
        <div className="character-scene-empty">
          <strong>还没有场景形象照</strong>
          <p>基于这个人物创建不同场景、服装的形象，完成后会显示在这里。</p>
          {canManage && !formOpen ? (
            <button
              className="secondary-button"
              type="button"
              onClick={() => setFormOpen(true)}
            >
              创建第一套场景造型
            </button>
          ) : null}
        </div>
      ) : null}
      {showResults ? (
        <div className="character-scene-grid">
          {looks.slice(0, limit).map((look) => {
            const photo =
              look.views.find((view) => view.view_type === "FRONT_FACE") ??
              look.views[0];
            const expanded = expandedId === look.persona_id;
            const sheetUrl = urls[look.contact_sheet_asset_id];
            return (
              <article className="character-scene-card" key={look.persona_id}>
                <button
                  className="character-scene-card__image"
                  type="button"
                  aria-label={`查看${look.scene_name}五视图`}
                  onClick={() => setExpandedId(expanded ? "" : look.persona_id)}
                  aria-expanded={expanded}
                >
                  {sheetUrl ? (
                    <img
                      alt={`${displayName} ${look.scene_name}`}
                      src={sheetUrl}
                      loading="lazy"
                      onError={() =>
                        setError("部分图片暂时无法加载，可点击刷新场景重试。")
                      }
                    />
                  ) : (
                    <span className="source-frame-placeholder">
                      场景合成图暂不可用
                    </span>
                  )}
                </button>
                <div className="character-scene-card__body">
                  <strong>{look.scene_name}</strong>
                  <span>{look.scene_description}</span>
                  <small>{look.costume_description}</small>
                  <em>1 套 · 五视图已发布</em>
                  {renderActions?.(
                    look,
                    photo ? urls[photo.asset_id] : undefined,
                  )}
                </div>
                {expanded ? (
                  <section
                    className="character-scene-views"
                    aria-label={`${look.scene_name}五视图`}
                  >
                    <div className="character-contact-sheet">
                      {sheetUrl ? (
                        <img
                          alt={`${displayName} ${look.scene_name} 场景五视图`}
                          src={sheetUrl}
                        />
                      ) : (
                        <span>合成图暂不可用</span>
                      )}
                    </div>
                    <p>
                      以下为从合成图裁切或镜像的参考视角，可选择需要的单图。
                    </p>
                    <div className="character-preview-grid">
                      {look.views.map((view) => (
                        <figure key={view.asset_id}>
                          {urls[view.asset_id] ? (
                            <img
                              alt={`${look.scene_name} ${viewLabels[view.view_type] ?? view.view_type}`}
                              src={urls[view.asset_id]}
                              loading="lazy"
                            />
                          ) : (
                            <span>视角暂不可用</span>
                          )}
                          <figcaption>
                            {viewLabels[view.view_type] ?? view.view_type}
                          </figcaption>
                        </figure>
                      ))}
                    </div>
                    <button
                      className="secondary-button"
                      type="button"
                      onClick={() => setExpandedId("")}
                    >
                      收起五视图
                    </button>
                  </section>
                ) : null}
              </article>
            );
          })}
        </div>
      ) : null}
      {showResults && looks.length > limit ? (
        <button
          className="secondary-button"
          type="button"
          onClick={() => setLimit((value) => value + 12)}
        >
          加载更多场景（已显示 {limit} / {looks.length} 套）
        </button>
      ) : null}
    </section>
  );
}
