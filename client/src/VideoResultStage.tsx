import { useEffect, useRef, useState } from "react";
import { formatDateTime as formatTimestamp } from "./admin/ui/vocabulary";
import {
  customerVisibleErrorMessage,
  type GenerationBatch,
  type GenerationTask,
  type GenerationTaskSummary,
  type VideoDownloadResult,
} from "./api";
import { VideoPreview } from "./VideoPreview";

// 生成结果舞台 = 客户视角的结果消费视图：大预览框 + 真实进度叙事 +
// 等待安抚内容 + 视频信息栏 + 轻量付费再次生成。运维操作（对账、安全
// 重试、确认未计费、整批重生成）保留在任务记录的运维视图，由
// onOpenOpsDetail 引导切换。

// H3 Provider 没有真实百分比进度接口，因此客户视图只展示后端可验证的
// 阶段与时间预估，不再用本地插值伪装成精确百分比。
const ESTIMATED_RENDER_SECONDS = 240;
const SLOW_RENDER_WARNING_FACTOR = 1.5;
const TICK_MS = 1_000;
// 超过此阈值提供排队提醒，不根据等待时间猜测服务端故障原因。
const QUEUE_STUCK_SECONDS = 180;

const STEP_LABELS = ["已提交", "排队中", "生成中", "完成"] as const;

const PHASE_MESSAGES: Record<string, string> = {
  PENDING: "正在准备你的生成任务…",
  SUBMITTING: "正在把首帧与 Prompt 提交给渲染引擎…",
  QUEUED: "已进入渲染队列，即将开始生成…",
  RUNNING: "AI 正在基于你的首帧渲染画面、动作与口型…",
  ARCHIVING: "视频已生成，正在获取播放地址…",
  SUCCEEDED: "视频已生成，正在获取播放地址…",
};

const REGENERATION_REASONS = [
  "画面质量不佳",
  "人物相似度不足",
  "动作不够自然",
  "口型与文案不同步",
  "其他原因",
] as const;

type VideoResultStageProps = {
  activeResultAction: string;
  activeTaskAction: string;
  batch: GenerationBatch;
  batchTitle: string;
  canOperate: boolean;
  isCustomerView?: boolean;
  onDownload: (task: GenerationTask) => void;
  onOpenOpsDetail: () => void;
  onPreviewSourceError: (task: GenerationTask) => void;
  onRegenerate: (task: GenerationTask, reason: string) => void;
  onRequestPreview: (task: GenerationTask) => void;
  previewUrls: Record<string, string>;
  resultErrors: Record<string, string>;
  downloadFeedback?: Record<string, VideoDownloadFeedback>;
  onOpenDownloadFolder?: (task: GenerationTask, downloadId: string) => void;
};

export type VideoDownloadFeedback = (
  | VideoDownloadResult
  | { status: "pending" }
  | { status: "error"; message: string }
) & { folderError?: string };

export function VideoDownloadNotice({
  feedback,
  onOpenFolder,
}: {
  feedback?: VideoDownloadFeedback;
  onOpenFolder: (downloadId: string) => void;
}) {
  if (!feedback) return null;
  return (
    <div className="video-download-feedback" role="status">
      {feedback.status === "saved" ? (
        <>
          <strong>下载成功</strong>
          <span className="video-download-path">{feedback.path}</span>
          <button
            className="secondary-button"
            onClick={() => onOpenFolder(feedback.downloadId)}
            type="button"
          >
            打开文件夹
          </button>
        </>
      ) : feedback.status === "cancelled" ? (
        "已取消下载"
      ) : feedback.status === "started" ? (
        "已交给浏览器下载，请查看浏览器下载列表。"
      ) : feedback.status === "error" ? (
        feedback.message
      ) : (
        "正在下载，请等待保存完成…"
      )}
      {feedback.folderError ? <span>{feedback.folderError}</span> : null}
    </div>
  );
}

export function VideoResultStage({
  activeResultAction,
  activeTaskAction,
  batch,
  batchTitle,
  canOperate,
  isCustomerView = false,
  onDownload,
  onOpenOpsDetail,
  onPreviewSourceError,
  onRegenerate,
  onRequestPreview,
  previewUrls,
  resultErrors,
  downloadFeedback = {},
  onOpenDownloadFolder,
}: VideoResultStageProps) {
  const tasks = batch.tasks;
  const [activeTaskId, setActiveTaskId] = useState(() =>
    pickDefaultTaskId(tasks),
  );

  // 批次推进或重生成切换后校正选中任务（选中项被替代/消失时）。
  useEffect(() => {
    if (!tasks.some((task) => task.id === activeTaskId)) {
      setActiveTaskId(pickDefaultTaskId(tasks));
    }
  }, [tasks, activeTaskId]);

  const activeTask = tasks.find((task) => task.id === activeTaskId) ?? tasks[0];
  const batchInProgress = tasks.some(
    (task) => taskOutcome(task) === "in_progress",
  );

  // 本地秒级 tick 只更新时间；任务阶段仍以后端轮询为准。
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    if (!batchInProgress) {
      return;
    }
    const timer = window.setInterval(() => setNowMs(Date.now()), TICK_MS);
    return () => window.clearInterval(timer);
  }, [batchInProgress]);

  const previewUrl = activeTask ? previewUrls[activeTask.id] : undefined;
  const previewBusy = activeTask
    ? activeResultAction === `${activeTask.id}:preview`
    : false;
  const hasPreviewSource = activeTask
    ? hasGenerationResultSource(activeTask)
    : false;

  // 归档资产和直连结果都按任务归属取播放地址；直连 URL 不进入批次详情。
  const activePreviewError = activeTask ? resultErrors[activeTask.id] : "";
  useEffect(() => {
    if (!activeTask || !canOperate) {
      return;
    }
    if (!hasPreviewSource || previewUrl || previewBusy || activePreviewError) {
      return;
    }
    onRequestPreview(activeTask);
  }, [
    activeTask,
    canOperate,
    hasPreviewSource,
    previewUrl,
    previewBusy,
    activePreviewError,
    onRequestPreview,
  ]);

  const [isRegenerateOpen, setIsRegenerateOpen] = useState(false);
  const [regenerateReason, setRegenerateReason] = useState("");
  const [regenerateDetail, setRegenerateDetail] = useState("");
  const [regenerateConfirmed, setRegenerateConfirmed] = useState(false);
  // 无 submitted_at 的任务（尚未被 worker 提交）以首次观测时间为排队计时
  // 基准，避免 PENDING 任务永远显示“已用时 0 秒”。
  const firstSeenRef = useRef<Record<string, number>>({});

  if (!activeTask) {
    return null;
  }

  const outcome = taskOutcome(activeTask);
  const downloadBusy = downloadFeedback[activeTask.id]?.status === "pending";
  const visibleStatus = generationBatchDisplayStatus(batch);
  const stage = effectiveStage(activeTask);
  const stepIndex = stepIndexForStage(stage, outcome);
  const elapsed =
    outcome === "in_progress" ? elapsedSeconds(activeTask, nowMs) : 0;
  const remaining = Math.max(0, ESTIMATED_RENDER_SECONDS - elapsed);
  const showSlowWarning =
    outcome === "in_progress" &&
    stage === "RUNNING" &&
    elapsed > ESTIMATED_RENDER_SECONDS * SLOW_RENDER_WARNING_FACTOR;
  const preRenderStage =
    stage === "PENDING" || stage === "SUBMITTING" || stage === "QUEUED";
  const queueWait =
    outcome === "in_progress"
      ? queueWaitSeconds(activeTask, nowMs, firstSeenRef.current)
      : 0;
  const queueStuck =
    outcome === "in_progress" &&
    preRenderStage &&
    queueWait > QUEUE_STUCK_SECONDS;

  const canRegenerate =
    canOperate && activeTask.available_actions?.includes("REGENERATE");
  const regenerateBusy = Boolean(activeTaskAction);
  const rawResultError = resultErrors[activeTask.id];
  const resultError = rawResultError
    ? customerVisibleErrorMessage(
        rawResultError,
        "生成结果暂时无法加载，请稍后重试。",
      )
    : "";
  const showPlaybackRecovery = Boolean(
    resultError && !previewUrl && hasPreviewSource,
  );

  function handleRegenerateSubmit() {
    const detail = regenerateDetail.trim();
    const reason = detail ? `${regenerateReason}：${detail}` : regenerateReason;
    onRegenerate(activeTask, reason);
  }

  return (
    <section className="video-stage" aria-labelledby="video-stage-title">
      <header className="video-stage-header">
        <div className="video-stage-header__title">
          <div className="video-stage-title-row">
            <h2 id="video-stage-title" title={batchTitle}>
              {batchTitle}
            </h2>
            <span
              className={`batch-status batch-status--${visibleStatus.toLowerCase()}`}
            >
              {formatStatus(visibleStatus)}
            </span>
          </div>
          {tasks.length > 1 ? (
            <p className="video-stage-batch-note">
              任务已结束 {batch.progress.terminal_count} /{" "}
              {batch.progress.total_count}
              {batch.source_batch_id ? " · 冻结输入重生成批次" : ""}
            </p>
          ) : null}
        </div>
        <div className="video-stage-header__actions">
          {canOperate && hasPreviewSource ? (
            <button
              aria-label="下载 MP4"
              disabled={downloadBusy}
              onClick={() => onDownload(activeTask)}
              type="button"
            >
              {downloadBusy ? "正在下载…" : "下载视频"}
            </button>
          ) : null}
          {canRegenerate ? (
            <button
              className="secondary-button"
              disabled={regenerateBusy}
              onClick={() => {
                setIsRegenerateOpen(!isRegenerateOpen);
                setRegenerateReason("");
                setRegenerateDetail("");
                setRegenerateConfirmed(false);
              }}
              type="button"
            >
              {isRegenerateOpen ? "收起再次生成" : "再次生成"}
            </button>
          ) : null}
        </div>
      </header>

      <VideoDownloadNotice
        feedback={downloadFeedback[activeTask.id]}
        onOpenFolder={(downloadId) =>
          onOpenDownloadFolder?.(activeTask, downloadId)
        }
      />

      <div className="video-stage-result-grid">
        <div className="video-stage-player">
          {activeTask.provider === "fake_h3" ? (
            <p className="video-stage-provider-note" role="status">
              测试模式
            </p>
          ) : null}
          {previewUrl ? (
            <StageVideoPlayer
              onSourceError={() => onPreviewSourceError(activeTask)}
              src={previewUrl}
              taskLabel={`结果预览 ${activeTask.id}`}
            />
          ) : showPlaybackRecovery ? (
            <StagePlaybackRecovery
              canDownload={canOperate && hasPreviewSource}
              downloadBusy={downloadBusy}
              error={resultError}
              onDownload={() => onDownload(activeTask)}
              onOpenOpsDetail={onOpenOpsDetail}
              onRetry={() => onRequestPreview(activeTask)}
              retryBusy={previewBusy}
            />
          ) : outcome === "in_progress" ? (
            <StageProgressView
              elapsed={elapsed}
              onOpenOpsDetail={onOpenOpsDetail}
              phaseMessage={
                queueStuck
                  ? "任务仍在排队，请勿重复提交。"
                  : (PHASE_MESSAGES[stage] ?? "正在生成…")
              }
              queueStuck={queueStuck}
              queueWait={queueWait}
              remaining={remaining}
              showSlowWarning={showSlowWarning}
              stepIndex={stepIndex}
            />
          ) : !canOperate && hasPreviewSource ? (
            <p className="video-stage-player-note">
              审计只读，不可预览或下载结果
            </p>
          ) : hasPreviewSource ? (
            <p className="video-stage-player-note" role="status">
              {previewBusy ? "正在打开在线播放…" : "正在准备播放地址…"}
            </p>
          ) : (
            <StageOutcomeView outcome={outcome} task={activeTask} />
          )}
        </div>

        <StageSummary outcome={outcome} task={activeTask} />
      </div>

      {outcome === "in_progress" ? (
        <StageTimeline stepIndex={stepIndex} />
      ) : null}

      {resultError && !showPlaybackRecovery ? (
        <p className="task-error-summary" role="status">
          {resultError}
        </p>
      ) : null}

      {canRegenerate && isRegenerateOpen ? (
        <section
          aria-label={`再次生成 ${activeTask.id}`}
          className="video-stage-regenerate"
        >
          <strong>付费再次生成</strong>
          <p>
            只复用该任务的冻结 Prompt，新建一次付费生成；金额快照：
            {formatCost(activeTask.estimated_cost)}
          </p>
          <fieldset>
            <legend>再次生成原因</legend>
            {REGENERATION_REASONS.map((reason) => (
              <label key={reason}>
                <input
                  checked={regenerateReason === reason}
                  name={`regenerate-reason-${activeTask.id}`}
                  onChange={() => setRegenerateReason(reason)}
                  type="radio"
                  value={reason}
                />
                <span>{reason}</span>
              </label>
            ))}
          </fieldset>
          <label>
            <span>补充说明（可选）</span>
            <input
              aria-label={`再次生成补充说明 ${activeTask.id}`}
              disabled={regenerateBusy}
              maxLength={200}
              onChange={(event) => setRegenerateDetail(event.target.value)}
              value={regenerateDetail}
            />
          </label>
          <label className="paid-confirmation-check">
            <input
              aria-label={`确认为任务 ${activeTask.id} 新增一次付费生成`}
              checked={regenerateConfirmed}
              disabled={regenerateBusy}
              onChange={(event) => setRegenerateConfirmed(event.target.checked)}
              type="checkbox"
            />
            <span>我已确认本次将产生一次新的付费视频生成</span>
          </label>
          <button
            disabled={
              !regenerateReason || !regenerateConfirmed || regenerateBusy
            }
            onClick={handleRegenerateSubmit}
            type="button"
          >
            确认再次生成
          </button>
        </section>
      ) : null}

      <div className="video-stage-footer">
        {tasks.length > 1 ? (
          <nav aria-label="生成结果列表" className="video-stage-filmstrip">
            {tasks.map((task, index) => {
              const taskOutcomeValue = taskOutcome(task);
              const isActive = task.id === activeTask.id;
              const badge =
                taskOutcomeValue === "in_progress"
                  ? STEP_LABELS[
                      stepIndexForStage(effectiveStage(task), taskOutcomeValue)
                    ]
                  : outcomeBadgeLabel(taskOutcomeValue);
              return (
                <button
                  aria-label={`查看结果 ${index + 1}：${task.id}`}
                  aria-pressed={isActive}
                  className={
                    isActive
                      ? "video-stage-cell video-stage-cell--active"
                      : "video-stage-cell"
                  }
                  key={task.id}
                  onClick={() => setActiveTaskId(task.id)}
                  type="button"
                >
                  <span className="video-stage-cell__index">
                    结果 {index + 1}
                  </span>
                  <span
                    className={`video-stage-cell__badge video-stage-cell__badge--${taskOutcomeValue}`}
                  >
                    {badge}
                  </span>
                </button>
              );
            })}
          </nav>
        ) : null}
        <StageInfoBar
          batchId={batch.id}
          canOperate={
            canOperate &&
            (!isCustomerView ||
              Boolean(activeTask.available_actions?.length) ||
              outcome === "failed" ||
              outcome === "needs_attention" ||
              Boolean(resultError))
          }
          onOpenOpsDetail={onOpenOpsDetail}
          task={activeTask}
        />
      </div>
    </section>
  );
}

// 舞台视频播放器：不自动播放、默认有声；自绘常驻深色控制条（播放/
// 暂停、进度与时间、音量开关与音量、全屏），点画面本身也可切换播放。
// 用自绘控制条替代原生 controls：原生条在 9:16 深色舞台里样式突兀，
// 且 autoPlay+muted 的旧组合让用户误以为「没有声音」。
function StageVideoPlayer({
  onSourceError,
  src,
  taskLabel,
}: {
  onSourceError: () => void;
  src: string;
  taskLabel: string;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [isMuted, setIsMuted] = useState(false);
  const [volume, setVolume] = useState(1);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);

  function togglePlay() {
    const video = videoRef.current;
    if (!video) {
      return;
    }
    if (video.paused) {
      if (video.ended) {
        video.currentTime = 0;
      }
      void video.play().catch(() => {
        setIsPlaying(false);
        onSourceError();
      });
    } else {
      video.pause();
    }
  }

  function toggleMute() {
    const video = videoRef.current;
    if (!video) {
      return;
    }
    video.muted = !video.muted;
    setIsMuted(video.muted);
  }

  function handleVolumeChange(nextVolume: number) {
    const video = videoRef.current;
    if (!video) {
      return;
    }
    video.volume = nextVolume;
    video.muted = nextVolume === 0;
    setVolume(nextVolume);
    setIsMuted(nextVolume === 0);
  }

  function handleSeek(nextTime: number) {
    const video = videoRef.current;
    if (!video) {
      return;
    }
    video.currentTime = nextTime;
    setCurrentTime(nextTime);
  }

  async function toggleFullscreen() {
    const container = containerRef.current;
    if (!container) {
      return;
    }
    try {
      if (document.fullscreenElement === container) {
        await document.exitFullscreen();
      } else {
        await container.requestFullscreen();
      }
    } catch {
      // 嵌入环境拒绝全屏时静默降级为原地播放。
    }
  }

  return (
    <div className="stage-video-player" ref={containerRef}>
      <VideoPreview
        aria-label={taskLabel}
        className="video-stage-surface"
        videoClassName="video-stage-video"
        onClick={togglePlay}
        onError={onSourceError}
        onEnded={() => setIsPlaying(false)}
        onLoadedMetadata={(event) => {
          setDuration(event.currentTarget.duration || 0);
        }}
        onPause={() => setIsPlaying(false)}
        onPlay={() => setIsPlaying(true)}
        onTimeUpdate={(event) => {
          setCurrentTime(event.currentTarget.currentTime);
        }}
        onVolumeChange={(event) => {
          setIsMuted(event.currentTarget.muted);
          setVolume(event.currentTarget.volume);
        }}
        playsInline
        preload="auto"
        ref={videoRef}
        src={src}
      />
      {!isPlaying ? (
        <button
          aria-label={`播放 ${taskLabel}`}
          className="stage-video-player__bigplay"
          onClick={togglePlay}
          type="button"
        >
          <span aria-hidden="true">▶</span>
        </button>
      ) : null}
      <div className="stage-video-player__controls">
        <button
          aria-label={isPlaying ? "暂停" : "播放"}
          onClick={togglePlay}
          type="button"
        >
          {isPlaying ? "暂停" : "播放"}
        </button>
        <span className="stage-video-player__time">
          {formatVideoTime(currentTime)} / {formatVideoTime(duration)}
        </span>
        <input
          aria-label="播放进度"
          className="stage-video-player__seek"
          max={duration || 0}
          min={0}
          onChange={(event) => handleSeek(Number(event.target.value))}
          step={0.1}
          type="range"
          value={Math.min(currentTime, duration || 0)}
        />
        <button
          aria-label={isMuted ? "取消静音" : "静音"}
          onClick={toggleMute}
          type="button"
        >
          {isMuted ? "已静音" : "有声"}
        </button>
        <input
          aria-label="音量"
          className="stage-video-player__volume"
          max={1}
          min={0}
          onChange={(event) => handleVolumeChange(Number(event.target.value))}
          step={0.05}
          type="range"
          value={isMuted ? 0 : volume}
        />
        <button onClick={() => void toggleFullscreen()} type="button">
          全屏
        </button>
      </div>
    </div>
  );
}

function formatVideoTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) {
    return "0:00";
  }
  const total = Math.floor(seconds);
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes}:${rest.toString().padStart(2, "0")}`;
}

function StageProgressView({
  elapsed,
  onOpenOpsDetail,
  phaseMessage,
  queueStuck,
  queueWait,
  remaining,
  showSlowWarning,
  stepIndex,
}: {
  elapsed: number;
  onOpenOpsDetail: () => void;
  phaseMessage: string;
  queueStuck: boolean;
  queueWait: number;
  remaining: number;
  showSlowWarning: boolean;
  stepIndex: number;
}) {
  return (
    <div className="video-stage-progress">
      <span className="video-stage-progress__eyebrow">当前阶段</span>
      <strong className="video-stage-progress__stage" role="status">
        {STEP_LABELS[stepIndex]}
      </strong>
      <p className="video-stage-phase" role="status">
        {phaseMessage}
      </p>
      <p className="video-stage-timing">
        {queueStuck
          ? `排队等待 ${formatClock(queueWait)}`
          : `已用时 ${formatClock(elapsed)}`}
        {!queueStuck && remaining > 0
          ? ` · 预计还需约 ${formatClock(remaining)}`
          : ""}
      </p>
      {queueStuck ? (
        <div className="video-stage-stuck-note" role="status">
          <p>排队时间较长，可查看任务状态或联系管理员。</p>
          <button
            className="link-button"
            onClick={onOpenOpsDetail}
            type="button"
          >
            查看运维详情
          </button>
        </div>
      ) : null}
      {showSlowWarning ? (
        <p className="video-stage-slow-note" role="status">
          渲染时间超过常规预估，任务仍在后台继续执行。
        </p>
      ) : null}
    </div>
  );
}

function StageTimeline({ stepIndex }: { stepIndex: number }) {
  return (
    <ol aria-label="生成阶段" className="video-stage-steps">
      {STEP_LABELS.map((label, index) => {
        const isDone = index < stepIndex;
        const isActive = index === stepIndex;
        return (
          <li
            className={
              isDone
                ? "video-stage-step video-stage-step--done"
                : isActive
                  ? "video-stage-step video-stage-step--active"
                  : "video-stage-step"
            }
            key={label}
          >
            <span className="video-stage-step__dot" aria-hidden="true" />
            <span>{label}</span>
          </li>
        );
      })}
    </ol>
  );
}

function StagePlaybackRecovery({
  canDownload,
  downloadBusy,
  error,
  onDownload,
  onOpenOpsDetail,
  onRetry,
  retryBusy,
}: {
  canDownload: boolean;
  downloadBusy: boolean;
  error: string;
  onDownload: () => void;
  onOpenOpsDetail: () => void;
  onRetry: () => void;
  retryBusy: boolean;
}) {
  return (
    <div className="video-stage-playback-error" role="status">
      <strong>暂时无法播放</strong>
      <p>{error}</p>
      <div className="video-stage-playback-error__actions">
        <button disabled={retryBusy} onClick={onRetry} type="button">
          {retryBusy ? "正在重新获取…" : "重新获取播放地址"}
        </button>
        {canDownload ? (
          <button
            className="secondary-button"
            disabled={downloadBusy}
            onClick={onDownload}
            type="button"
          >
            下载原文件
          </button>
        ) : null}
      </div>
      <button className="link-button" onClick={onOpenOpsDetail} type="button">
        查看技术详情
      </button>
    </div>
  );
}

function StageOutcomeView({
  outcome,
  task,
}: {
  outcome: ReturnType<typeof taskOutcome>;
  task: GenerationTask;
}) {
  const copy = outcomeCopy(outcome, task);
  return (
    <div className={`video-stage-outcome video-stage-outcome--${outcome}`}>
      <strong>{copy.title}</strong>
      <p>{copy.detail}</p>
    </div>
  );
}

function StageSummary({
  outcome,
  task,
}: {
  outcome: TaskOutcome;
  task: GenerationTask;
}) {
  const resolution = readSnapshotString(task, "resolution");
  const outputDuration = readSnapshotNumber(task, "output_duration_seconds");
  return (
    <aside className="video-stage-summary" aria-label="结果信息">
      <h3>结果信息</h3>
      <dl>
        <div>
          <dt>{outcome === "in_progress" ? "提交时间" : "完成时间"}</dt>
          <dd>
            {formatTimestamp(
              outcome === "in_progress" ? task.submitted_at : task.completed_at,
            )}
          </dd>
        </div>
        {resolution || outputDuration !== null ? (
          <div>
            <dt>成片规格</dt>
            <dd>
              {[
                resolution,
                outputDuration !== null ? `${outputDuration} 秒` : null,
              ]
                .filter(Boolean)
                .join(" · ")}
            </dd>
          </div>
        ) : null}
      </dl>
    </aside>
  );
}

function StageInfoBar({
  batchId,
  canOperate,
  onOpenOpsDetail,
  task,
}: {
  batchId: string;
  canOperate: boolean;
  onOpenOpsDetail: () => void;
  task: GenerationTask;
}) {
  return (
    <div className="video-stage-info">
      <details className="video-stage-tech">
        <summary>技术详情</summary>
        <dl>
          <div>
            <dt>批次 ID</dt>
            <dd>{batchId}</dd>
          </div>
          <div>
            <dt>任务 ID</dt>
            <dd>{task.id}</dd>
          </div>
          <div>
            <dt>服务商参考号</dt>
            <dd>{task.provider_task_id_tail ?? "未公开"}</dd>
          </div>
          <div>
            <dt>尝试次数</dt>
            <dd>
              {task.attempt ?? 0} 次 · 归档重试 {task.archive_retry_count ?? 0}{" "}
              次
            </dd>
          </div>
          <div>
            <dt>提交时间</dt>
            <dd>{formatTimestamp(task.submitted_at)}</dd>
          </div>
          <div>
            <dt>生成通道 / 费用</dt>
            <dd>
              {task.provider === "fake_h3" ? "测试模式" : "正式服务"} ·{" "}
              {formatCost(task.actual_cost ?? task.estimated_cost)}
            </dd>
          </div>
        </dl>
      </details>
      {canOperate ? (
        <button
          aria-label="运维详情"
          className="link-button video-stage-ops-entry"
          onClick={onOpenOpsDetail}
          type="button"
        >
          任务操作
        </button>
      ) : null}
    </div>
  );
}

type TaskOutcome =
  | "in_progress"
  | "completed"
  | "failed"
  | "needs_attention"
  | "superseded";

export function hasGenerationResultSource(
  task: Pick<
    GenerationTask,
    "direct_result_available" | "result_asset_id" | "archive_status"
  >,
): boolean {
  return Boolean(
    task.direct_result_available ||
      (task.result_asset_id && task.archive_status === "ARCHIVED"),
  );
}

// Legacy quality flags are audit facts, not failed deliveries. Only normalize
// a complete task snapshot; a truncated history page must keep server status.
export function generationBatchDisplayStatus(batch: {
  status: string;
  quantity: number;
  tasks: GenerationTaskSummary[];
}): string {
  const tasks = batch.tasks.filter((task) => !task.superseded_by_task_id);
  if (tasks.length !== batch.quantity || tasks.length === 0)
    return batch.status;
  if (
    tasks.every(
      (task) =>
        task.status === "SUCCEEDED" &&
        ["DIRECT", "ARCHIVED"].includes(task.archive_status) &&
        hasGenerationResultSource(task),
    )
  ) {
    return "SUCCEEDED";
  }
  return batch.status;
}

function taskOutcome(task: GenerationTask): TaskOutcome {
  const stage = effectiveStage(task);
  if (task.superseded_by_task_id) {
    return "superseded";
  }
  if (stage === "COMPLETED") {
    return "completed";
  }
  if (stage === "QUALITY_FAILED") {
    return "needs_attention";
  }
  if (stage === "FAILED" || stage === "CANCELLED") {
    return "failed";
  }
  if (stage === "SUBMISSION_UNCERTAIN" || stage === "ARCHIVE_FAILED") {
    return "needs_attention";
  }
  return "in_progress";
}

function effectiveStage(task: GenerationTask): string {
  if (["FAILED", "CANCELLED", "SUBMISSION_UNCERTAIN"].includes(task.status)) {
    return task.archive_status === "ARCHIVE_FAILED"
      ? "ARCHIVE_FAILED"
      : task.status;
  }
  if (task.archive_status === "ARCHIVE_FAILED") return "ARCHIVE_FAILED";
  if (
    task.status === "SUCCEEDED" &&
    ["DIRECT", "ARCHIVED"].includes(task.archive_status) &&
    hasGenerationResultSource(task)
  ) {
    return "COMPLETED";
  }
  if (task.stage) {
    return task.stage;
  }
  if (task.status === "SUCCEEDED" && task.archive_status === "ARCHIVED") {
    return "COMPLETED";
  }
  return task.status;
}

function pickDefaultTaskId(tasks: GenerationTask[]): string {
  const inProgress = tasks.find((task) => taskOutcome(task) === "in_progress");
  if (inProgress) {
    return inProgress.id;
  }
  const viewable = tasks.find(hasGenerationResultSource);
  return (viewable ?? tasks[0])?.id ?? "";
}

function stepIndexForStage(stage: string, outcome: TaskOutcome): number {
  if (outcome === "completed") {
    return 3;
  }
  switch (stage) {
    case "PENDING":
    case "SUBMITTING":
      return 0;
    case "QUEUED":
      return 1;
    case "RUNNING":
      return 2;
    case "ARCHIVING":
    case "SUCCEEDED":
    case "ARCHIVE_FAILED":
      return 2;
    case "FAILED":
    case "CANCELLED":
    case "SUBMISSION_UNCERTAIN":
      return 2;
    default:
      return 0;
  }
}

function outcomeCopy(outcome: TaskOutcome, task: GenerationTask) {
  switch (outcome) {
    case "failed":
      return {
        detail: customerVisibleErrorMessage(
          task.error_message_redacted,
          "本次生成未成功，可在运维详情查看原因。",
        ),
        title: "生成失败",
      };
    case "needs_attention":
      return {
        detail: "该任务需要人工处理（对账、归档重试或账单确认）后才能继续。",
        title: "需要处理",
      };
    case "superseded":
      return {
        detail: `已由任务 ${task.superseded_by_task_id} 替代；本记录仅保留历史事实。`,
        title: "已替代",
      };
    default:
      return {
        detail: "该结果暂不可用。",
        title: "暂不可用",
      };
  }
}

function outcomeBadgeLabel(outcome: TaskOutcome): string {
  switch (outcome) {
    case "completed":
      return "完成";
    case "failed":
      return "失败";
    case "needs_attention":
      return "需处理";
    case "superseded":
      return "已替代";
    default:
      return "生成中";
  }
}

function elapsedSeconds(task: GenerationTask, nowMs: number): number {
  const start =
    parseServerTime(task.started_at) ??
    parseServerTime(task.submitted_at) ??
    nowMs;
  return Math.max(0, Math.round((nowMs - start) / 1000));
}

function queueWaitSeconds(
  task: GenerationTask,
  nowMs: number,
  firstSeen: Record<string, number>,
): number {
  const serverStart =
    parseServerTime(task.submitted_at) ?? parseServerTime(task.started_at);
  if (serverStart === null && firstSeen[task.id] === undefined) {
    firstSeen[task.id] = nowMs;
  }
  const baseline = serverStart ?? firstSeen[task.id] ?? nowMs;
  return Math.max(0, Math.round((nowMs - baseline) / 1000));
}

function parseServerTime(value: string | null | undefined): number | null {
  if (!value) {
    return null;
  }
  const normalized = value.trim().replace(" ", "T");
  const withZone = /[Z+-]\d{2}:?\d{2}$/.test(normalized)
    ? normalized
    : `${normalized}Z`;
  const ms = Date.parse(withZone);
  return Number.isNaN(ms) ? null : ms;
}

export function readSnapshotString(
  task: GenerationTask,
  key: string,
): string | null {
  const snapshot = task.prompt_snapshot;
  if (!snapshot || typeof snapshot !== "object") {
    return null;
  }
  const value = (snapshot as Record<string, unknown>)[key];
  return typeof value === "string" && value ? value : null;
}

export function readSnapshotNumber(
  task: GenerationTask,
  key: string,
): number | null {
  const snapshot = task.prompt_snapshot;
  if (!snapshot || typeof snapshot !== "object") {
    return null;
  }
  const value = (snapshot as Record<string, unknown>)[key];
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  return null;
}

function formatClock(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes === 0) {
    return `${seconds} 秒`;
  }
  return `${minutes} 分 ${seconds} 秒`;
}

function formatCost(value: number | null | undefined) {
  return value === null || value === undefined
    ? "待回填"
    : `¥${value.toFixed(2)}`;
}

function formatStatus(status: string) {
  const labels: Record<string, string> = {
    CANCELLED: "已取消",
    COMPLETED_WITH_FAILURES: "部分失败",
    FAILED: "失败",
    NEEDS_ATTENTION: "需要处理",
    PENDING: "生成中",
    QUEUED: "生成中",
    SUCCEEDED: "已完成",
  };
  return labels[status] ?? status;
}
