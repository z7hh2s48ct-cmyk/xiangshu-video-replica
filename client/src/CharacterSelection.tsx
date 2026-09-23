import { useCallback, useEffect, useRef, useState } from "react";

import {
  chooseProjectMainCharacterVersion,
  getAssetDownloadUrl,
  getProjectMainCharacter,
  listProjectCharacterVersions,
  type ProjectCharacterAssetOption,
  type ProjectCharacterVersionOption,
  type ProjectMainCharacter,
  type SimpleCharacterResult,
} from "./api";
import { SimpleCharacterUpload } from "./SimpleCharacterUpload";

const VIEW_LABELS: Record<ProjectCharacterAssetOption["view_type"], string> = {
  FRONT_FACE: "正脸近景",
  FRONT_HALF: "正面半身",
  FRONT_FULL: "正面全身",
  LEFT_45: "左 45°",
  LEFT_SIDE: "左侧面",
};

export function CharacterSelection({
  banded = false,
  onAspectRatioChange,
  onBusyChange,
  onSelectionChange,
  onVersionChange,
  projectId,
  readOnly = false,
  variant = "full",
  sceneOnly = false,
}: {
  /** 三带布局（复刻页第 2 节）：控制带 / 媒体带 / 操作带，供左右栏媒体框对齐。 */
  banded?: boolean;
  /** 场景预览图加载完成后的宽高比（宽 / 高）。复刻页要用它给左侧媒体框定比例。 */
  onAspectRatioChange?: (ratio: number) => void;
  onBusyChange?: (isBusy: boolean) => void;
  onSelectionChange?: (hasSelection: boolean) => void;
  onVersionChange?: (selection: ProjectMainCharacter | null) => void;
  projectId: string;
  readOnly?: boolean;
  // inline：详情页第二段区头的内联下拉形态（选择即落库，仅完整五类
  // 资产的版本可选）；full：旧工作台的面板形态（radio 列表 + 确认）。
  variant?: "full" | "inline";
  sceneOnly?: boolean;
}) {
  const [versions, setVersions] = useState<ProjectCharacterVersionOption[]>([]);
  const [currentSelection, setCurrentSelection] =
    useState<ProjectMainCharacter | null>(null);
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [isOpen, setIsOpen] = useState(false);
  const [isRestoring, setIsRestoring] = useState(true);
  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [isUploadOpen, setIsUploadOpen] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [autoSelected, setAutoSelected] = useState(false);
  const [guidance, setGuidance] = useState("");
  const [restoredEmpty, setRestoredEmpty] = useState(false);
  const [isAutoSelecting, setIsAutoSelecting] = useState(false);
  const [scenePreview, setScenePreview] = useState<{
    contextKey: string;
    requestId: number;
    url: string;
  } | null>(null);
  const [scenePreviewErrorContext, setScenePreviewErrorContext] = useState<
    string | null
  >(null);
  const autoSelectProjectRef = useRef<string | null>(null);
  // 回调走 ref 转发：生产接线中 busy 上报会让父级重渲染并产生新的
  // 回调引用，若进入自动选择 effect 的依赖面会触发 cleanup 中止
  // in-flight 落库（评审 Critical 1），ref 保持依赖面纯净。
  const onBusyChangeRef = useRef(onBusyChange);
  const onSelectionChangeRef = useRef(onSelectionChange);
  const onVersionChangeRef = useRef(onVersionChange);
  const scenePreviewRequestIdRef = useRef(0);
  const scenePreviewContextRef = useRef("");

  useEffect(() => {
    onBusyChangeRef.current = onBusyChange;
    onSelectionChangeRef.current = onSelectionChange;
    onVersionChangeRef.current = onVersionChange;
  });

  useEffect(() => {
    let active = true;
    setIsRestoring(true);
    setCurrentSelection(null);
    setSelectedVersionId("");
    setError("");
    setMessage("");
    setAutoSelected(false);
    setGuidance("");
    setRestoredEmpty(false);
    getProjectMainCharacter(projectId)
      .then((selection) => {
        if (!active) {
          return;
        }
        const restoredSelection =
          selectionSummary(selection) &&
          (!sceneOnly ||
            isSceneSnapshot(
              selection?.character_snapshot.persona_snapshot_json,
            ))
            ? selection
            : null;
        setCurrentSelection(restoredSelection);
        setSelectedVersionId(restoredSelection?.character_version_id ?? "");
        // 仅「restore 成功且无快照」才允许自动预选：restore 失败时快照
        // 状态未知，自动改绑可能覆盖用户既有选择（评审 Major 2）。
        setRestoredEmpty(restoredSelection === null);
        onSelectionChange?.(restoredSelection !== null);
        onVersionChange?.(restoredSelection);
      })
      .catch((requestError) => {
        if (!active) {
          return;
        }
        setError(
          requestError instanceof Error
            ? requestError.message
            : "读取当前角色版本失败。",
        );
        onSelectionChange?.(false);
        onVersionChange?.(null);
      })
      .finally(() => {
        if (active) {
          setIsRestoring(false);
        }
      });
    return () => {
      active = false;
    };
  }, [onSelectionChange, onVersionChange, projectId, sceneOnly]);

  // P0-03-01：项目无角色快照进入时，自动预选最近发布的可用版本并落库
  //（choose 服务端原子复用快照，重复选择幂等）；仅每个项目自动一次，
  // 只读身份、restore 失败、空列表与写入失败都静默转手动态选择。
  useEffect(() => {
    if (
      sceneOnly ||
      isRestoring ||
      !restoredEmpty ||
      readOnly ||
      currentSelection ||
      autoSelectProjectRef.current === projectId
    ) {
      return;
    }
    autoSelectProjectRef.current = projectId;
    let active = true;
    void (async () => {
      setIsAutoSelecting(true);
      onBusyChangeRef.current?.(true);
      try {
        const availableVersions = await listProjectCharacterVersions(projectId);
        if (!active) {
          return;
        }
        if (!availableVersions.length) {
          setGuidance(
            "暂无可选角色版本，请先在人物库发布角色，或使用一键上传人物。",
          );
          return;
        }
        const baseVersions = availableVersions.filter(
          (version) => !isSceneAppearance(version),
        );
        const automaticCandidates = baseVersions.length
          ? baseVersions
          : availableVersions;
        const latestVersion = automaticCandidates.reduce((latest, current) =>
          Date.parse(current.published_at) > Date.parse(latest.published_at)
            ? current
            : latest,
        );
        const selection = await chooseProjectMainCharacterVersion(
          projectId,
          latestVersion.character_version_id,
        );
        if (!active) {
          return;
        }
        setCurrentSelection(selection);
        setSelectedVersionId(selection.character_version_id ?? "");
        setAutoSelected(true);
        onSelectionChangeRef.current?.(true);
        onVersionChangeRef.current?.(selection);
      } catch {
        if (active) {
          setGuidance("未自动选择角色版本，请手动选择角色版本。");
        }
      } finally {
        setIsAutoSelecting(false);
        onBusyChangeRef.current?.(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [
    currentSelection,
    isRestoring,
    projectId,
    readOnly,
    restoredEmpty,
    sceneOnly,
  ]);

  // inline 下拉需要常驻版本列表：restore 完成后即拉取，失败只降级为
  // 空列表提示，不阻断主流程。
  useEffect(() => {
    if (variant !== "inline" || isRestoring) {
      return;
    }
    let active = true;
    listProjectCharacterVersions(projectId)
      .then((availableVersions) => {
        if (active) {
          setVersions(availableVersions);
        }
      })
      .catch(() => {
        if (active) {
          setGuidance("读取可用角色版本失败，请稍后重试。");
        }
      });
    return () => {
      active = false;
    };
  }, [isRestoring, projectId, variant]);

  async function openSelection() {
    setIsOpen(true);
    setIsLoading(true);
    setError("");
    setMessage("");
    try {
      const availableVersions = await listProjectCharacterVersions(projectId);
      setVersions(availableVersions);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "读取可用角色版本失败。",
      );
    } finally {
      setIsLoading(false);
    }
  }

  async function refreshVersions() {
    try {
      const availableVersions = await listProjectCharacterVersions(projectId);
      setVersions(availableVersions);
    } catch {
      // Keep the previously loaded list; the next openSelection will retry.
    }
  }

  async function handleSimpleCharacterCreated(result: SimpleCharacterResult) {
    await refreshVersions();
    setSelectedVersionId(result.character_version_id);
    setIsUploadOpen(false);
    setMessage("新人物已发布并自动选中，确认后保存即可作为项目角色版本。");
  }

  async function saveSelection() {
    // 自动预选写入在途时跳过手动保存，避免双 PUT 竞态改写落库结果
    //（评审 Minor 4）；窗口极短，按钮同步禁用防采。
    if (readOnly || !selectedVersionId || isAutoSelecting) {
      return;
    }
    onBusyChange?.(true);
    setIsSaving(true);
    setError("");
    setMessage("");
    try {
      const selection = await chooseProjectMainCharacterVersion(
        projectId,
        selectedVersionId,
      );
      setCurrentSelection(selection);
      setAutoSelected(false);
      onSelectionChange?.(true);
      onVersionChange?.(selection);
      const summary = selectionSummary(selection);
      setMessage(
        summary
          ? `已选择角色“${summary.identityName} · ${summary.personaName} V${summary.versionNumber}”。`
          : "角色版本已保存。",
      );
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "选择角色版本失败。",
      );
    } finally {
      setIsSaving(false);
      onBusyChange?.(false);
    }
  }

  // inline 形态：下拉选择即落库（服务端原子幂等），失败回退显示原选择。
  async function handleInlineChange(versionId: string) {
    if (readOnly || !versionId || isAutoSelecting) {
      return;
    }
    onBusyChangeRef.current?.(true);
    setIsSaving(true);
    setError("");
    setGuidance("");
    try {
      const selection = await chooseProjectMainCharacterVersion(
        projectId,
        versionId,
      );
      setCurrentSelection(selection);
      setSelectedVersionId(selection.character_version_id ?? "");
      setAutoSelected(false);
      onSelectionChangeRef.current?.(true);
      onVersionChangeRef.current?.(selection);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "选择角色版本失败。",
      );
      setSelectedVersionId(currentSelection?.character_version_id ?? "");
    } finally {
      setIsSaving(false);
      onBusyChangeRef.current?.(false);
    }
  }

  const currentSummary = selectionSummary(currentSelection);
  const selectedOption = versions.find(
    (version) => version.character_version_id === selectedVersionId,
  );
  const sceneAssets =
    selectedOption?.assets ??
    currentSelection?.character_snapshot.published_assets;
  const scenePreviewAssetId =
    sceneAssets?.find((asset) => asset.view_type === "FRONT_FULL")?.asset_id ??
    sceneAssets?.[0]?.asset_id;
  const scenePreviewContext = `${projectId}\0${scenePreviewAssetId ?? ""}`;
  scenePreviewContextRef.current = scenePreviewContext;
  const loadScenePreview = useCallback(() => {
    const requestId = scenePreviewRequestIdRef.current + 1;
    scenePreviewRequestIdRef.current = requestId;
    const requestedContext = scenePreviewContext;
    if (!sceneOnly || !scenePreviewAssetId) {
      setScenePreview(null);
      setScenePreviewErrorContext(null);
      return;
    }
    setScenePreview(null);
    setScenePreviewErrorContext(null);
    void getAssetDownloadUrl(scenePreviewAssetId)
      .then((result) => {
        if (
          scenePreviewRequestIdRef.current === requestId &&
          scenePreviewContextRef.current === requestedContext
        ) {
          setScenePreview({
            contextKey: requestedContext,
            requestId,
            url: result.url,
          });
        }
      })
      .catch(() => {
        if (
          scenePreviewRequestIdRef.current === requestId &&
          scenePreviewContextRef.current === requestedContext
        ) {
          setScenePreviewErrorContext(requestedContext);
        }
      });
  }, [sceneOnly, scenePreviewAssetId, scenePreviewContext]);
  useEffect(() => {
    loadScenePreview();
    return () => {
      scenePreviewRequestIdRef.current += 1;
    };
  }, [loadScenePreview]);
  const currentIdentityId = currentSelection?.character_snapshot.identity?.id;
  const currentPersonaId = currentSelection?.character_snapshot.persona_id;
  const currentVersionNumber =
    currentSelection?.character_snapshot.character_version_number;
  const newerVersion =
    currentIdentityId &&
    currentPersonaId &&
    typeof currentVersionNumber === "number"
      ? versions
          .filter(
            (version) =>
              version.identity_id === currentIdentityId &&
              version.persona_id === currentPersonaId &&
              version.version_number > currentVersionNumber,
          )
          .reduce<ProjectCharacterVersionOption | null>(
            (latest, version) =>
              !latest || version.version_number > latest.version_number
                ? version
                : latest,
            null,
          )
      : null;

  // 详情页第二段区头形态：一行「角色：<下拉>」，选择即生效。
  if (variant === "inline") {
    const inlineVersions = versions.filter(
      (version) =>
        version.assets.length === Object.keys(VIEW_LABELS).length &&
        (!sceneOnly || isSceneAppearance(version)),
    );
    const hasCurrentOption = inlineVersions.some(
      (version) => version.character_version_id === selectedVersionId,
    );
    const visibleScenePreview =
      scenePreview?.contextKey === scenePreviewContext ? scenePreview : null;
    const scenePreviewError = scenePreviewErrorContext === scenePreviewContext;
    // 上报场景图宽高比（可选）：onLoad 之外补一次挂载检查——缓存命中的图片
    // 可能不再派发 load（与 VideoPreview 对缓存视频的兜底同因），漏报会让
    // 上游拿不到当前素材的真实比例。
    const reportSceneRatio = (image: HTMLImageElement | null) => {
      if (!image?.complete) {
        return;
      }
      if (image.naturalWidth <= 0 || image.naturalHeight <= 0) {
        return;
      }
      onAspectRatioChange?.(image.naturalWidth / image.naturalHeight);
    };
    const scenePreviewImage =
      sceneOnly && visibleScenePreview ? (
        <img
          key={`${visibleScenePreview.contextKey}\0${visibleScenePreview.url}`}
          className="flow-character-row__preview"
          src={visibleScenePreview.url}
          alt="已选场景图"
          ref={(node) => reportSceneRatio(node)}
          onLoad={(event) => {
            reportSceneRatio(event.currentTarget);
          }}
          onError={() => {
            if (
              scenePreviewRequestIdRef.current !==
                visibleScenePreview.requestId ||
              scenePreviewContextRef.current !== visibleScenePreview.contextKey
            ) {
              return;
            }
            scenePreviewRequestIdRef.current += 1;
            setScenePreview(null);
            setScenePreviewErrorContext(visibleScenePreview.contextKey);
          }}
        />
      ) : null;
    const versionSelect = (
      <label>
        {sceneOnly ? "人物场景" : "角色"}
        <select
          aria-label={sceneOnly ? "人物场景形象" : "角色版本"}
          disabled={readOnly || isRestoring || isSaving || isAutoSelecting}
          onChange={(event) => void handleInlineChange(event.target.value)}
          value={selectedVersionId}
        >
          {sceneOnly && !isRestoring && inlineVersions.length > 0 ? (
            <option value="" disabled>
              请选择场景形象
            </option>
          ) : null}
          {isRestoring || (!hasCurrentOption && !inlineVersions.length) ? (
            <option value="">
              {isRestoring
                ? "恢复中…"
                : sceneOnly
                  ? "暂无可用场景形象"
                  : "暂无可选角色"}
            </option>
          ) : null}
          {selectedVersionId && !hasCurrentOption ? (
            <option value={selectedVersionId}>
              {currentSummary
                ? `${currentSummary.identityName} · V${currentSummary.versionNumber}`
                : "当前角色"}
            </option>
          ) : null}
          {inlineVersions.map((version) => (
            <option
              key={version.character_version_id}
              value={version.character_version_id}
            >
              {version.identity_name} · {versionAppearanceLabel(version)} · V
              {version.version_number}
            </option>
          ))}
        </select>
      </label>
    );
    const inlineNotices = (
      <>
        {sceneOnly && !isRestoring && inlineVersions.length === 0 ? (
          <span className="status-note" role="status">
            请到人物库创建并发布人物场景形象。
          </span>
        ) : null}
        {sceneOnly && scenePreviewError ? (
          <span className="settings-error" role="alert">
            场景图预览加载失败。
            <button
              className="secondary-button"
              onClick={() => loadScenePreview()}
              type="button"
            >
              重试预览
            </button>
          </span>
        ) : null}
        {newerVersion && typeof currentVersionNumber === "number" ? (
          <span className="character-version-update" role="status">
            <span>
              人物库已有新版本 V{newerVersion.version_number}，当前项目仍使用 V
              {currentVersionNumber}。
            </span>
            {!readOnly ? (
              <button
                className="secondary-button"
                disabled={isSaving || isAutoSelecting}
                onClick={() =>
                  void handleInlineChange(newerVersion.character_version_id)
                }
                type="button"
              >
                切换到 V{newerVersion.version_number}
              </button>
            ) : null}
          </span>
        ) : null}
        {error ? (
          <span className="settings-error" role="alert">
            {error}
          </span>
        ) : null}
        {!error && guidance ? (
          <span className="status-note" role="status">
            {guidance}
          </span>
        ) : null}
      </>
    );
    // 三带布局（复刻页第 2 节）：下拉进控制带、预览图进媒体带、操作带留空，
    // 与左栏「源画面」的媒体带等位，两栏媒体框因此同尺寸且上下边对齐。
    if (banded) {
      return (
        <section
          className="flow-character-row flow-character-row--banded"
          aria-label="角色"
        >
          <div className="band-ctrl">{versionSelect}</div>
          <div className="media-frame">
            {scenePreviewImage ?? (
              <p className="file-note">请先选择场景形象。</p>
            )}
          </div>
          <div className="band-act" />
          {inlineNotices}
        </section>
      );
    }
    return (
      <section className="flow-character-row" aria-label="角色">
        {scenePreviewImage}
        {versionSelect}
        {inlineNotices}
      </section>
    );
  }

  return (
    <section className="character-selection" aria-labelledby="character-title">
      <div className="section-heading">
        <div>
          <h3 id="character-title">角色版本</h3>
          {isRestoring ? (
            <p>恢复角色版本中…</p>
          ) : currentSummary ? (
            <div className="current-character-summary">
              <strong>当前角色：{currentSummary.identityName}</strong>
              <span>
                {currentSummary.personaName} · V{currentSummary.versionNumber}
              </span>
              <small>
                {authorizationLabel(currentSummary.authorizationExpiresAt)}
              </small>
            </div>
          ) : (
            <p>选择一个已发布且授权有效的角色版本，用于后续人物参考匹配。</p>
          )}
        </div>
        <button
          className="secondary-button"
          disabled={isRestoring}
          onClick={openSelection}
          type="button"
        >
          {readOnly ? "查看角色版本" : "选择角色版本"}
        </button>
      </div>
      {autoSelected && currentSummary ? (
        <div className="auto-selection-notice" role="status">
          <span>
            已自动选择角色版本 {currentSummary.identityName} · V
            {currentSummary.versionNumber}
          </span>
          <button onClick={openSelection} type="button">
            更换
          </button>
        </div>
      ) : null}
      {guidance ? (
        <p className="status-note" role="status">
          {guidance}
        </p>
      ) : null}
      {isOpen ? (
        <div className="character-selection-panel">
          <div className="panel-header">
            <div>
              {!readOnly ? (
                <button
                  className="secondary-button"
                  onClick={() => setIsUploadOpen((open) => !open)}
                  type="button"
                >
                  {isUploadOpen ? "收起一键上传" : "一键上传人物"}
                </button>
              ) : null}
            </div>
            <button
              type="button"
              className="secondary-button"
              onClick={() => setIsOpen(false)}
            >
              关闭
            </button>
          </div>
          {!readOnly && isUploadOpen ? (
            <SimpleCharacterUpload
              onCreated={handleSimpleCharacterCreated}
              projectId={projectId}
            />
          ) : null}
          {isLoading ? (
            <p className="status-note">正在读取可用角色版本</p>
          ) : null}
          {error ? <p className="settings-error">{error}</p> : null}
          {!isLoading && !error && !versions.length ? (
            <p className="status-note">
              当前没有具备有效授权与完整七类资产的已发布角色版本。
            </p>
          ) : null}
          {!isLoading && !error && versions.length ? (
            <fieldset className="character-options">
              <legend>选择一个不可变角色版本</legend>
              {versions.map((version) => {
                const isScene = isSceneAppearance(version);
                const occupation = stringValue(
                  version.persona_snapshot_json.occupation,
                  "未填写职业",
                );
                const sceneDescription = stringValue(
                  version.persona_snapshot_json.scene_description,
                  "未填写场景描述",
                );
                const costumeDescription = stringValue(
                  version.persona_snapshot_json.costume_description,
                  "未填写服装描述",
                );
                const isSelected =
                  selectedVersionId === version.character_version_id;
                return (
                  <label
                    className={
                      isSelected ? "character-option--selected" : undefined
                    }
                    key={version.character_version_id}
                  >
                    <input
                      checked={isSelected}
                      disabled={readOnly}
                      name="main-character-version"
                      onChange={() => {
                        setSelectedVersionId(version.character_version_id);
                        setMessage("");
                      }}
                      type="radio"
                      value={version.character_version_id}
                    />
                    <span className="character-option-copy">
                      <strong>{version.identity_name}</strong>
                      <span>
                        {versionAppearanceLabel(version)} · V
                        {version.version_number}
                      </span>
                      {isScene ? (
                        <>
                          <small>{sceneDescription}</small>
                          <small>服装：{costumeDescription}</small>
                        </>
                      ) : (
                        <small>{occupation}</small>
                      )}
                      <small>
                        {authorizationLabel(version.authorization_expires_at)}
                      </small>
                    </span>
                    {isSelected ? (
                      <ul
                        aria-label="五类已发布资产"
                        className="published-view-list"
                      >
                        {version.assets.map((asset) => (
                          <li key={asset.character_asset_id}>
                            <span>{VIEW_LABELS[asset.view_type]}</span>
                            <small>已发布</small>
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </label>
                );
              })}
            </fieldset>
          ) : null}
          {selectedOption &&
          selectedOption.assets.length !== Object.keys(VIEW_LABELS).length ? (
            <p className="settings-error">
              当前版本缺少完整五类已发布资产，不能选择。
            </p>
          ) : null}
          {message ? <p className="setup-success">{message}</p> : null}
          {readOnly ? (
            <p className="status-note">只读身份不能更改项目角色版本。</p>
          ) : (
            <button
              disabled={
                isLoading ||
                isSaving ||
                isAutoSelecting ||
                !selectedVersionId ||
                selectedOption?.assets.length !==
                  Object.keys(VIEW_LABELS).length
              }
              onClick={saveSelection}
              type="button"
            >
              {isSaving ? "正在保存" : "确认角色版本"}
            </button>
          )}
        </div>
      ) : null}
    </section>
  );
}

type SelectionSummary = {
  identityName: string;
  personaName: string;
  versionNumber: number;
  authorizationExpiresAt: string | null;
};

function selectionSummary(
  selection: ProjectMainCharacter | null,
): SelectionSummary | null {
  if (!selection) {
    return null;
  }
  const snapshot = selection.character_snapshot;
  if (!snapshot) {
    return null;
  }
  if (
    snapshot.schema_version === "project-character-selection.v1" &&
    snapshot.identity?.display_name &&
    typeof snapshot.character_version_number === "number"
  ) {
    return {
      identityName: snapshot.identity.display_name,
      personaName: stringValue(
        snapshot.persona_snapshot_json?.name,
        "未命名人设",
      ),
      versionNumber: snapshot.character_version_number,
      authorizationExpiresAt:
        snapshot.identity.authorization_expires_at ?? null,
    };
  }
  if (snapshot.name) {
    return {
      identityName: snapshot.name,
      personaName: "历史兼容人物",
      versionNumber: selection.version_number,
      authorizationExpiresAt: null,
    };
  }
  return null;
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() ? value : fallback;
}

function isSceneAppearance(version: ProjectCharacterVersionOption): boolean {
  const constraints = version.persona_snapshot_json.appearance_constraints_json;
  return (
    typeof constraints === "object" &&
    constraints !== null &&
    "appearance_type" in constraints &&
    constraints.appearance_type === "scene"
  );
}

function versionAppearanceLabel(
  version: ProjectCharacterVersionOption,
): string {
  const name = stringValue(
    version.persona_snapshot_json.name,
    isSceneAppearance(version) ? "未命名场景" : "未命名人设",
  );
  return isSceneAppearance(version) ? `场景：${name}` : `人物基准：${name}`;
}

function authorizationLabel(value: string | null): string {
  if (!value) {
    return "授权长期有效";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "授权期限记录无效";
  }
  const parts = new Intl.DateTimeFormat("zh-CN", {
    day: "2-digit",
    month: "2-digit",
    timeZone: "Asia/Shanghai",
    year: "numeric",
  }).formatToParts(date);
  const part = (type: "day" | "month" | "year") =>
    parts.find((item) => item.type === type)?.value ?? "";
  return `授权有效至 ${part("year")}-${part("month")}-${part("day")}`;
}

function isSceneSnapshot(persona: unknown): boolean {
  if (
    typeof persona !== "object" ||
    persona === null ||
    !("appearance_constraints_json" in persona)
  )
    return false;
  const constraints = persona.appearance_constraints_json;
  return (
    typeof constraints === "object" &&
    constraints !== null &&
    "appearance_type" in constraints &&
    constraints.appearance_type === "scene"
  );
}
