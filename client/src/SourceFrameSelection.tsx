import { useCallback, useEffect, useRef, useState } from "react";
import {
  type AnalysisVersion,
  cancelSourceFrameTask,
  confirmSourceFrame,
  extractSourceFrames,
  getAssetDownloadUrl,
  getLatestProjectSourceFrameSelection,
  getLatestProjectSourceFrames,
  getLatestProjectSourceFrameTask,
  readSourceFrameCandidates,
  type SourceFrameCandidate,
  type SourceFrameCharacterFeatures,
  type SourceFrameTask,
  SourceFrameTaskFailedError,
  waitForSourceFrameTask,
} from "./api";
import { VideoPreview } from "./VideoPreview";

export function SourceFrameSelection({
  candidatesAlwaysVisible = false,
  featureSuggestion = null,
  onBusyChange,
  onConfirmed,
  onSelectionChange,
  projectId,
  readOnly = false,
  referenceAssetId,
  simplified = false,
  videoDurationSeconds = null,
}: {
  // 复刻页第 2 节把源画面提为独立横向区域：候选帧与取帧工具常驻可见，
  // 不再收进折叠区（简化模式的默认折叠语义保持不变）。
  candidatesAlwaysVisible?: boolean;
  featureSuggestion?: SourceFrameCharacterFeatures | null;
  onBusyChange?: (isBusy: boolean) => void;
  onConfirmed?: () => void;
  onSelectionChange?: (selection: AnalysisVersion | null) => void;
  projectId: string;
  readOnly?: boolean;
  referenceAssetId: string | null;
  // 详情页默认只呈现自动处理状态；候选预览与更换入口收进低频操作区。
  simplified?: boolean;
  videoDurationSeconds?: number | null;
}) {
  const [candidates, setCandidates] = useState<SourceFrameCandidate[]>([]);
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});
  const [failedPreviewAssetIds, setFailedPreviewAssetIds] = useState<string[]>(
    [],
  );
  const [selectedAssetId, setSelectedAssetId] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const inputContext = `${projectId}\0${referenceAssetId ?? ""}`;
  const [loadedInputContext, setLoadedInputContext] = useState(inputContext);
  const [candidateInputContext, setCandidateInputContext] =
    useState(inputContext);
  const inputMismatch = loadedInputContext !== inputContext;
  const contextLoading = isLoading || inputMismatch;
  const materialReady =
    !contextLoading && candidateInputContext === inputContext;
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [manualConfirmationRequired, setManualConfirmationRequired] =
    useState(false);
  const [activeTask, setActiveTask] = useState<SourceFrameTask | null>(null);
  const [manualTimestamp, setManualTimestamp] = useState(() =>
    defaultManualTimestamp(videoDurationSeconds),
  );
  const loadRequestId = useRef(0);
  // P0-03-02：特征建议取最新值（latest-ref），避免建议变化触发候选重载；
  // 自动提取按项目去重，每项目仅自动一次，失败不自动重试（手动态仍可重提）。
  // onBusyChange 同样走 latest-ref：宿主链路（App 内联回调 + busy 翻转
  // 重渲染）会使其身份每次渲染变化，进依赖数组会导致无关重载清空用户
  // 已填特征、并吞掉自动提取失败错误（评审 M-1）。
  const featureSuggestionRef = useRef(featureSuggestion);
  featureSuggestionRef.current = featureSuggestion;
  const onBusyChangeRef = useRef(onBusyChange);
  onBusyChangeRef.current = onBusyChange;
  const busyOwnedRef = useRef(false);
  const setBusy = useCallback((busy: boolean) => {
    if (busyOwnedRef.current === busy) {
      return;
    }
    busyOwnedRef.current = busy;
    onBusyChangeRef.current?.(busy);
  }, []);
  const autoExtractProjectRef = useRef<string | null>(null);

  const loadCandidates = useCallback(async () => {
    const requestId = loadRequestId.current + 1;
    loadRequestId.current = requestId;
    const isCurrentRequest = () => requestId === loadRequestId.current;
    setIsLoading(true);
    setIsSubmitting(false);
    setActiveTask(null);
    setError("");
    setStatus("");
    try {
      const [version, selection, latestTask] = await Promise.all([
        getLatestProjectSourceFrames(projectId),
        getLatestProjectSourceFrameSelection(projectId),
        referenceAssetId
          ? getLatestProjectSourceFrameTask(projectId, referenceAssetId)
          : Promise.resolve(null),
      ]);
      if (!isCurrentRequest()) {
        return;
      }
      setLoadedInputContext(inputContext);
      setCandidateInputContext(inputContext);
      if (
        latestTask?.status === "PENDING" ||
        latestTask?.status === "RUNNING"
      ) {
        setCandidates([]);
        setPreviewUrls({});
        setFailedPreviewAssetIds([]);
        setSelectedAssetId("");
        setManualConfirmationRequired(false);
        onSelectionChange?.(null);
        setActiveTask(latestTask);
        setIsLoading(false);
        setIsSubmitting(true);
        setStatus(sourceFrameTaskStatus(latestTask));
        try {
          await waitForSourceFrameTask(latestTask.id);
          if (!isCurrentRequest()) {
            return;
          }
          setActiveTask(null);
          setStatus("候选源画面已提取，正在读取推荐结果。");
          await loadCandidates();
        } catch (requestError) {
          if (isCurrentRequest()) {
            if (requestError instanceof SourceFrameTaskFailedError) {
              setActiveTask(null);
            }
            setError(
              requestError instanceof Error
                ? requestError.message
                : "候选源画面提取失败。",
            );
          }
        } finally {
          if (isCurrentRequest()) {
            setIsSubmitting(false);
          }
        }
        return;
      }
      if (!version) {
        const defaultTimestamps =
          adaptiveSourceFrameTimestamps(videoDurationSeconds);
        setCandidates([]);
        setPreviewUrls({});
        setFailedPreviewAssetIds([]);
        setSelectedAssetId("");
        onSelectionChange?.(null);
        setStatus(selection.stale ? "候选已更新，正在自动选择源画面。" : "");
        if (latestTask?.status === "FAILED") {
          setActiveTask(null);
          setError(
            latestTask.error_message || "候选源画面提取失败，请重新提交。",
          );
          return;
        }
        if (latestTask?.status === "SUCCEEDED") {
          setActiveTask(null);
          setError("取帧任务已完成，但候选记录暂不可用，请刷新后重试。");
          return;
        }
        // P0-03-02：角色就绪且无候选时自动提取默认时间点（本地截帧无费用），
        // 候选生成后由下方流程自动选取并确认；readOnly 不触发写操作（契约红线 6）。
        if (
          !readOnly &&
          referenceAssetId &&
          autoExtractProjectRef.current !== projectId
        ) {
          autoExtractProjectRef.current = projectId;
          setBusy(true);
          setIsSubmitting(true);
          let enqueuePending = true;
          try {
            const task = await extractSourceFrames(
              projectId,
              referenceAssetId,
              defaultTimestamps,
            );
            if (!isCurrentRequest()) {
              return;
            }
            setActiveTask(task);
            enqueuePending = false;
            setBusy(false);
            setStatus(sourceFrameTaskStatus(task));
            await waitForSourceFrameTask(task.id);
            if (!isCurrentRequest()) {
              return;
            }
            setActiveTask(null);
            setStatus("已提取候选源画面，正在读取推荐结果。");
            await loadCandidates();
          } catch (requestError) {
            if (isCurrentRequest()) {
              if (requestError instanceof SourceFrameTaskFailedError) {
                setActiveTask(null);
              }
              setError(
                requestError instanceof Error
                  ? requestError.message
                  : "自动提取候选源画面失败。",
              );
            }
          } finally {
            if (isCurrentRequest()) {
              setIsSubmitting(false);
            }
            if (enqueuePending && isCurrentRequest()) {
              setBusy(false);
            }
          }
        }
        return;
      }
      const payload = readSourceFrameCandidates(version);
      if (!payload) {
        setCandidates([]);
        setPreviewUrls({});
        setFailedPreviewAssetIds([]);
        setSelectedAssetId("");
        setError("候选源画面数据格式无效，请重新提取。");
        return;
      }
      setCandidates(payload.candidates);
      setActiveTask(null);
      setManualConfirmationRequired(false);
      setPreviewUrls({});
      setFailedPreviewAssetIds([]);
      const confirmedAssetId = selection.version?.payload.source_frame_asset_id;
      if (typeof confirmedAssetId === "string" && !selection.stale) {
        setSelectedAssetId(confirmedAssetId);
        setStatus("已确认源画面，将保留原视频的构图与动作。");
        onSelectionChange?.(selection.version);
      } else {
        const preferredAssetId = preferredCandidateAssetId(payload.candidates);
        setSelectedAssetId(preferredAssetId);
        onSelectionChange?.(null);
        if (!readOnly && preferredAssetId) {
          setManualConfirmationRequired(true);
          const preferredIndex = payload.candidates.findIndex(
            (candidate) => candidate.asset_id === preferredAssetId,
          );
          setStatus(
            payload.semantic_quality_status === "VERIFIED"
              ? `已推荐画面 ${preferredIndex + 1}，请确认或更换源画面。`
              : "已推荐画面，请查看后确认。",
          );
        }
      }
      const previewResults = await Promise.allSettled(
        payload.candidates.map(async (candidate) => {
          const download = await getAssetDownloadUrl(candidate.asset_id);
          return [candidate.asset_id, download.url] as const;
        }),
      );
      const previewEntries = previewResults.flatMap((result) =>
        result.status === "fulfilled" ? [result.value] : [],
      );
      if (!isCurrentRequest()) {
        return;
      }
      setPreviewUrls(Object.fromEntries(previewEntries));
      setFailedPreviewAssetIds(
        previewResults.flatMap((result, index) =>
          result.status === "rejected"
            ? [payload.candidates[index].asset_id]
            : [],
        ),
      );
    } catch (requestError) {
      if (!isCurrentRequest()) {
        return;
      }
      setError(
        requestError instanceof Error
          ? requestError.message
          : "读取候选源画面失败。",
      );
      onSelectionChange?.(null);
    } finally {
      if (isCurrentRequest()) {
        setLoadedInputContext(inputContext);
        setIsLoading(false);
      }
    }
  }, [
    onSelectionChange,
    inputContext,
    projectId,
    readOnly,
    referenceAssetId,
    setBusy,
    videoDurationSeconds,
  ]);

  useEffect(() => {
    void loadCandidates();
    return () => {
      loadRequestId.current += 1;
      setBusy(false);
    };
  }, [loadCandidates, setBusy]);

  useEffect(() => {
    setManualTimestamp(defaultManualTimestamp(videoDurationSeconds));
  }, [videoDurationSeconds]);

  async function runExtraction(timestamps: number[]) {
    if (readOnly) {
      return;
    }
    if (!referenceAssetId) {
      setError("参考视频尚未就绪，不能提取源画面。");
      return;
    }
    const requestId = loadRequestId.current + 1;
    loadRequestId.current = requestId;
    setBusy(true);
    setIsSubmitting(true);
    setError("");
    setStatus("");
    let enqueuePending = true;
    try {
      const task = await extractSourceFrames(
        projectId,
        referenceAssetId,
        timestamps,
      );
      if (requestId !== loadRequestId.current) {
        return;
      }
      setActiveTask(task);
      enqueuePending = false;
      setBusy(false);
      setStatus(sourceFrameTaskStatus(task));
      await waitForSourceFrameTask(task.id);
      if (requestId !== loadRequestId.current) {
        return;
      }
      setSelectedAssetId("");
      setActiveTask(null);
      onSelectionChange?.(null);
      setStatus("候选源画面已更新，正在读取推荐结果。");
      await loadCandidates();
    } catch (requestError) {
      if (requestId !== loadRequestId.current) {
        return;
      }
      if (requestError instanceof SourceFrameTaskFailedError) {
        setActiveTask(null);
      }
      setError(
        requestError instanceof Error
          ? requestError.message
          : "提取候选源画面失败。",
      );
    } finally {
      if (requestId === loadRequestId.current) {
        setIsSubmitting(false);
      }
      if (enqueuePending && requestId === loadRequestId.current) {
        setBusy(false);
      }
    }
  }

  async function handleExtract() {
    await runExtraction(adaptiveSourceFrameTimestamps(videoDurationSeconds));
  }

  async function handleManualExtract() {
    if (
      !Number.isFinite(manualTimestamp) ||
      manualTimestamp < 0 ||
      (videoDurationSeconds !== null && manualTimestamp >= videoDurationSeconds)
    ) {
      setError("手动取帧时间必须位于视频时长范围内。");
      return;
    }
    await runExtraction([manualTimestamp]);
  }

  async function handleRestart() {
    const task = activeTask;
    loadRequestId.current += 1;
    setError("");
    if (task?.status === "RUNNING") {
      setError("任务正在执行，请等待完成或超时后再重新开始。");
      return;
    }
    if (task?.status === "PENDING") {
      try {
        await cancelSourceFrameTask(task.id);
      } catch (requestError) {
        setError(
          requestError instanceof Error
            ? requestError.message
            : "停止旧取帧任务失败。",
        );
        return;
      }
    }
    setActiveTask(null);
    await runExtraction(adaptiveSourceFrameTimestamps(videoDurationSeconds));
  }

  async function handleConfirm() {
    if (readOnly || contextLoading) {
      return;
    }
    if (!selectedAssetId || !previewUrls[selectedAssetId]) {
      setError("请先加载并查看候选源画面预览，再使用所选画面。");
      return;
    }
    const requestId = loadRequestId.current;
    setBusy(true);
    setIsSubmitting(true);
    setError("");
    try {
      const selection = await confirmSourceFrame(
        projectId,
        selectedAssetId,
        featureSuggestionRef.current,
      );
      if (requestId !== loadRequestId.current) {
        return;
      }
      const selectedIndex = candidates.findIndex(
        (candidate) => candidate.asset_id === selectedAssetId,
      );
      setManualConfirmationRequired(false);
      setStatus(`已确认源画面 ${selectedIndex + 1}，正在匹配人物参考。`);
      onSelectionChange?.(selection);
      onConfirmed?.();
    } catch (requestError) {
      if (requestId !== loadRequestId.current) {
        return;
      }
      setError(
        requestError instanceof Error
          ? requestError.message
          : "确认源画面失败。",
      );
    } finally {
      if (requestId === loadRequestId.current) {
        setIsSubmitting(false);
        setBusy(false);
      }
    }
  }

  const confirmButton = !readOnly ? (
    <button
      className="source-frame-confirm"
      disabled={
        !materialReady ||
        isSubmitting ||
        !selectedAssetId ||
        !previewUrls[selectedAssetId]
      }
      onClick={handleConfirm}
      type="button"
    >
      使用这张画面
    </button>
  ) : null;

  // 复刻页两栏形态（源画面行）：左栏源画面预览与确认按钮，右栏「查看或更换
  // 源画面」候选区；不再把预览、候选与取帧工具纵向堆叠成多层。
  const replicaRowLayout = candidatesAlwaysVisible;
  const currentFramePreview =
    materialReady &&
    simplified &&
    selectedAssetId &&
    previewUrls[selectedAssetId] ? (
      <VideoPreview
        className="source-frame-current"
        frameRatio="adaptive"
        alt="当前原画面"
        poster={previewUrls[selectedAssetId]}
        onPosterError={() => {
          setPreviewUrls((current) => {
            const next = { ...current };
            delete next[selectedAssetId];
            return next;
          });
          setError("画面预览加载失败，请重新取帧。");
        }}
      />
    ) : null;
  const rowConfirmButton =
    simplified && candidates.length > 0 ? confirmButton : null;
  const alternativesPanel =
    materialReady && candidates.length > 0 ? (
      <details
        className="source-frame-advanced"
        open={
          candidatesAlwaysVisible || (!simplified && manualConfirmationRequired)
        }
      >
        <summary>{readOnly ? "查看源画面记录" : "查看或更换源画面"}</summary>
        <div className="source-frame-advanced__body">
          {!readOnly ? (
            <div className="source-frame-toolbar">
              {/* 复刻页两栏形态删去这段长提示，取帧工具本身的含义已足够。 */}
              {replicaRowLayout ? null : (
                <p>
                  后段画面也能作为人物与构图参考；确认后请在最终提示词中填写开场衔接。
                </p>
              )}
              <button
                className="secondary-button"
                disabled={isSubmitting || !referenceAssetId}
                onClick={handleExtract}
                type="button"
              >
                {isSubmitting ? "正在处理" : "重新取帧"}
              </button>
              <label>
                取帧时间（秒）
                <input
                  disabled={isSubmitting}
                  max={
                    videoDurationSeconds === null
                      ? undefined
                      : Math.max(0, videoDurationSeconds - 0.1)
                  }
                  min="0"
                  onChange={(event) =>
                    setManualTimestamp(Number(event.target.value))
                  }
                  step="0.1"
                  type="number"
                  value={manualTimestamp}
                />
              </label>
              <button
                className="secondary-button"
                disabled={isSubmitting || !referenceAssetId}
                onClick={() => void handleManualExtract()}
                type="button"
              >
                取出画面
              </button>
            </div>
          ) : null}
          <fieldset className="source-frame-options">
            <legend>{readOnly ? "源画面记录" : "选择源画面"}</legend>
            {candidates.map((candidate, index) => (
              <label
                className={
                  selectedAssetId === candidate.asset_id
                    ? "source-frame-option source-frame-option--selected"
                    : "source-frame-option"
                }
                key={candidate.asset_id}
              >
                <input
                  checked={selectedAssetId === candidate.asset_id}
                  disabled={
                    readOnly || isSubmitting || !previewUrls[candidate.asset_id]
                  }
                  name="source-frame"
                  onChange={() => {
                    setSelectedAssetId(candidate.asset_id);
                    setManualConfirmationRequired(true);
                    setStatus("已选择其他源画面，点击下方按钮应用。");
                  }}
                  type="radio"
                  value={candidate.asset_id}
                />
                {previewUrls[candidate.asset_id] ? (
                  <VideoPreview
                    alt={`候选源画面 ${index + 1}`}
                    fitContainer
                    poster={previewUrls[candidate.asset_id]}
                  />
                ) : (
                  <span className="source-frame-placeholder">
                    {failedPreviewAssetIds.includes(candidate.asset_id)
                      ? "预览加载失败"
                      : readOnly
                        ? "预览不可用"
                        : "预览加载中"}
                  </span>
                )}
                <span>
                  <strong>
                    画面 {index + 1}
                    {candidate.asset_id ===
                    preferredCandidateAssetId(candidates)
                      ? " · 推荐"
                      : ""}
                  </strong>
                  <small>
                    {candidate.timestamp_seconds.toFixed(1)} 秒
                    {candidate.timestamp_seconds <= 0.25
                      ? " · 开场画面"
                      : " · 后段画面，需补开场衔接"}
                  </small>
                </span>
              </label>
            ))}
          </fieldset>
          {readOnly ? (
            <p className="status-note">只读身份不能更换源画面。</p>
          ) : !simplified ? (
            confirmButton
          ) : null}
        </div>
      </details>
    ) : null;

  return (
    <section
      className={
        simplified
          ? "source-frame-selection source-frame-selection--compact"
          : "source-frame-selection"
      }
      aria-labelledby="source-frame-title"
    >
      <div className="source-frame-summary">
        <div>
          <h3 id="source-frame-title">原视频画面</h3>
          {/* 复刻页两栏形态不再渲染整段说明，标题与状态徽章已足够表意。 */}
          {replicaRowLayout ? null : (
            <p>
              选择人物清晰、无遮挡的画面；开头有字幕、贴纸或多人遮挡时，可改用后段画面。
            </p>
          )}
        </div>
        <span
          className={
            !inputMismatch && error
              ? "source-frame-state source-frame-state--attention"
              : selectedAssetId && materialReady && !isSubmitting
                ? "source-frame-state source-frame-state--ready"
                : "source-frame-state"
          }
        >
          {!inputMismatch && error
            ? "需要处理"
            : activeTask
              ? "取帧中"
              : contextLoading
                ? "自动处理中"
                : isSubmitting
                  ? candidates.length > 0
                    ? "确认中"
                    : "自动处理中"
                  : manualConfirmationRequired
                    ? "待手动确认"
                    : selectedAssetId
                      ? "已确认"
                      : "自动处理中"}
        </span>
      </div>
      {contextLoading ? (
        <p className="status-note">正在读取候选源画面</p>
      ) : null}
      {!inputMismatch && error ? (
        <p className="settings-error">{error}</p>
      ) : null}
      {!inputMismatch && status ? (
        <p className="setup-success">{status}</p>
      ) : null}
      {!inputMismatch && !readOnly && activeTask ? (
        <div className="source-frame-toolbar">
          <p>
            {activeTask.status === "PENDING"
              ? "任务尚未开始，可停止后重新取帧。"
              : "正在取帧，请稍候。"}
          </p>
          <button
            className="secondary-button"
            onClick={() => void loadCandidates()}
            type="button"
          >
            刷新状态
          </button>
          {activeTask.status === "PENDING" ? (
            <button
              className="secondary-button"
              onClick={() => void handleRestart()}
              type="button"
            >
              停止并重新取帧
            </button>
          ) : null}
        </div>
      ) : null}
      {!inputMismatch &&
      !readOnly &&
      error &&
      (!materialReady || candidates.length === 0) &&
      !activeTask ? (
        <button
          className="secondary-button"
          disabled={isSubmitting || !referenceAssetId}
          onClick={() => void handleRestart()}
          type="button"
        >
          重新开始取帧
        </button>
      ) : null}
      {materialReady && !error && candidates.length === 0 ? (
        <p className="file-note">尚未提取候选源画面。</p>
      ) : null}
      {replicaRowLayout ? (
        <div className="source-frame-selection__row">
          <div className="source-frame-selection__current">
            {currentFramePreview}
            {rowConfirmButton}
          </div>
          {alternativesPanel}
        </div>
      ) : (
        <>
          {currentFramePreview}
          {rowConfirmButton}
          {alternativesPanel}
        </>
      )}
    </section>
  );
}

// 优先从真实开场复刻；旧候选没有开场帧时沿用画质推荐。
function preferredCandidateAssetId(candidates: SourceFrameCandidate[]): string {
  if (candidates.length === 0) {
    return "";
  }
  const opening = candidates.find(
    (candidate) => candidate.timestamp_seconds === 0,
  );
  if (opening) return opening.asset_id;
  return candidates.reduce((best, candidate) =>
    (candidate.score ?? -1) > (best.score ?? -1) ? candidate : best,
  ).asset_id;
}

function adaptiveSourceFrameTimestamps(
  durationSeconds: number | null,
): number[] {
  if (
    typeof durationSeconds !== "number" ||
    !Number.isFinite(durationSeconds) ||
    durationSeconds <= 0
  ) {
    return [0, 1.5, 2.5];
  }
  return [0, 0.3, 0.5, 0.7, 0.9].map((ratio) =>
    Number((durationSeconds * ratio).toFixed(3)),
  );
}

function defaultManualTimestamp(durationSeconds: number | null): number {
  if (
    typeof durationSeconds !== "number" ||
    !Number.isFinite(durationSeconds) ||
    durationSeconds <= 0
  ) {
    return 0.5;
  }
  return Number((durationSeconds / 2).toFixed(1));
}

function sourceFrameTaskStatus(task: SourceFrameTask): string {
  return task.status === "RUNNING"
    ? "正在从原视频批量提取候选画面。"
    : "已进入处理队列，后台即将开始取帧。";
}
