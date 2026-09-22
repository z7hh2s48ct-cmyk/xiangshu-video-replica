import { useCallback, useEffect, useRef, useState } from "react";

import {
  type AdminAnalysisDiagnosticAttempt,
  type AdminAnalysisDiagnosticRecord,
  getAdminAnalysisDiagnostics,
} from "../api.admin";
import { PageBanner } from "./ui/PageBanner";
import { type BadgeTone, StatusBadge } from "./ui/StatusBadge";
import {
  ANALYSIS_ATTEMPT_STATUS_LABELS,
  FAILURE_PHASE_LABELS,
  formatDateTime,
  GENERATION_STATUS_LABELS,
  labelFrom,
} from "./ui/vocabulary";

// 语义色只表达「要不要管」，与生成记录列表同一口径。
function statusTone(status: string): BadgeTone {
  if (status === "SUCCEEDED") return "good";
  if (status === "FAILED") return "danger";
  return "warn";
}

const ATTEMPT_TONES: Record<string, BadgeTone> = {
  FAILED: "danger",
  INTERRUPTED: "warn",
  SUPERSEDED: "neutral",
};

/**
 * P1-8 — 失败诊断中心：按任务编号 / 问题编号回答「一个拆解任务经历了哪几次
 * 尝试、每次卡在哪、上游说了什么」。只读视图，不做全量日志浏览。
 * P2-2 — 每次结论额外给出 ``advice``（错误码 → 修复建议，来自
 * ``app.failure_runbook``）：管理端不必拿内部错误码去别处搜索。
 * 数据来自 GET /api/control/analysis-diagnostics。
 */
export function AnalysisDiagnosticPanel({
  initialTaskId = "",
}: {
  initialTaskId?: string;
}) {
  const [taskId, setTaskId] = useState(initialTaskId);
  const [requestId, setRequestId] = useState("");
  const [items, setItems] = useState<AdminAnalysisDiagnosticRecord[] | null>(
    null,
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const requestSeqRef = useRef(0);

  const search = useCallback(async () => {
    const trimmedTaskId = taskId.trim();
    const trimmedRequestId = requestId.trim();
    if (!trimmedTaskId && !trimmedRequestId) {
      return;
    }
    const seq = requestSeqRef.current + 1;
    requestSeqRef.current = seq;
    setLoading(true);
    setError("");
    setItems(null);
    try {
      const response = await getAdminAnalysisDiagnostics({
        taskId: trimmedTaskId || undefined,
        requestId: trimmedRequestId || undefined,
      });
      if (seq !== requestSeqRef.current) return;
      setItems(response.items);
    } catch (cause) {
      if (seq !== requestSeqRef.current) return;
      setError(
        cause instanceof Error && cause.message
          ? `诊断查询失败：${cause.message}`
          : "诊断查询失败：未知错误",
      );
    } finally {
      if (seq === requestSeqRef.current) {
        setLoading(false);
      }
    }
  }, [taskId, requestId]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: 移交的任务编号只在挂载时自动诊断一次，之后由查询按钮驱动。
  useEffect(() => {
    if (initialTaskId) void search();
  }, []);

  const canSearch = taskId.trim() !== "" || requestId.trim() !== "";

  return (
    <div className="admin-diagnostics">
      <p className="admin-hint">
        输入任务编号或失败卡片上的问题编号，查看该次拆解的重试历史与上游诊断。
        只读视图，不浏览全量日志。
      </p>
      <form
        className="admin-toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          void search();
        }}
      >
        <label>
          任务编号
          <input
            aria-label="诊断任务编号"
            value={taskId}
            onChange={(event) => setTaskId(event.target.value)}
          />
        </label>
        <label>
          问题编号
          <input
            aria-label="诊断问题编号"
            value={requestId}
            onChange={(event) => setRequestId(event.target.value)}
          />
        </label>
        <button disabled={loading || !canSearch} type="submit">
          {loading ? "查询中…" : "查询"}
        </button>
      </form>

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}

      {!error && !loading && items !== null && items.length === 0 ? (
        <PageBanner tone="notice">
          未找到匹配的任务记录，请核对编号。
        </PageBanner>
      ) : null}

      {items?.map((item) => (
        <DiagnosticRecordCard item={item} key={item.task_id} />
      ))}
    </div>
  );
}

function DiagnosticRecordCard({
  item,
}: {
  item: AdminAnalysisDiagnosticRecord;
}) {
  return (
    <article
      aria-label={`任务 ${item.task_id}`}
      className="admin-diagnostics__card"
    >
      <header className="admin-diagnostics__head">
        <div>
          <h3>{item.project_name ?? "未关联项目"}</h3>
          <small>{item.display_name || item.username}</small>
        </div>
        <StatusBadge tone={statusTone(item.status)}>
          {labelFrom(GENERATION_STATUS_LABELS, item.status)}
        </StatusBadge>
      </header>
      <dl className="admin-diagnostics__facts">
        <dt>任务编号</dt>
        <dd>{item.task_id}</dd>
        <dt>关联问题编号</dt>
        <dd>{item.request_id ?? "—"}</dd>
        <dt>当前尝试</dt>
        <dd>第 {item.attempt} 次</dd>
        <dt>创建时间</dt>
        <dd>{formatDateTime(item.created_at)}</dd>
        <dt>完成时间</dt>
        <dd>{formatDateTime(item.completed_at)}</dd>
        <dt>错误码</dt>
        <dd>{item.error_code ?? "—"}</dd>
        <dt>错误说明</dt>
        <dd>{item.error_message ?? "—"}</dd>
        <dt>失败阶段</dt>
        <dd>
          {item.failure_phase
            ? labelFrom(FAILURE_PHASE_LABELS, item.failure_phase)
            : "—"}
        </dd>
        {item.retryable != null ? (
          <>
            <dt>可否重试</dt>
            <dd>{item.retryable ? "可重试" : "不可重试"}</dd>
          </>
        ) : null}
        <dt>上游状态码</dt>
        <dd>{item.upstream_status ?? "—"}</dd>
        <dt>上游说明</dt>
        <dd>{item.upstream_reason ?? "—"}</dd>
        {item.advice ? (
          <>
            <dt>修复建议</dt>
            <dd>{item.advice}</dd>
          </>
        ) : null}
      </dl>
      {item.attempts.length > 0 ? (
        <section aria-label="重试历史" className="admin-diagnostics__history">
          <h4>重试历史</h4>
          <ol className="admin-diagnostics__attempts">
            {item.attempts.map((attempt) => (
              <AttemptItem attempt={attempt} key={attempt.attempt} />
            ))}
          </ol>
        </section>
      ) : null}
    </article>
  );
}

function AttemptItem({ attempt }: { attempt: AdminAnalysisDiagnosticAttempt }) {
  return (
    <li className="admin-diagnostics__attempt">
      <div className="admin-diagnostics__attempt-head">
        <h5>第 {attempt.attempt} 次尝试</h5>
        <StatusBadge tone={ATTEMPT_TONES[attempt.status] ?? "neutral"}>
          {labelFrom(ANALYSIS_ATTEMPT_STATUS_LABELS, attempt.status)}
        </StatusBadge>
      </div>
      <dl className="admin-diagnostics__facts">
        <dt>错误码</dt>
        <dd>{attempt.error_code ?? "—"}</dd>
        <dt>错误说明</dt>
        <dd>{attempt.error_message ?? "—"}</dd>
        <dt>失败阶段</dt>
        <dd>
          {attempt.failure_phase
            ? labelFrom(FAILURE_PHASE_LABELS, attempt.failure_phase)
            : "—"}
        </dd>
        <dt>可否重试</dt>
        <dd>{attempt.retryable ? "可重试" : "不可重试"}</dd>
        <dt>上游状态码</dt>
        <dd>{attempt.upstream_status ?? "—"}</dd>
        <dt>上游说明</dt>
        <dd>{attempt.upstream_reason ?? "—"}</dd>
        {attempt.advice ? (
          <>
            <dt>修复建议</dt>
            <dd>{attempt.advice}</dd>
          </>
        ) : null}
        {attempt.request_id ? (
          <>
            <dt>问题编号</dt>
            <dd>{attempt.request_id}</dd>
          </>
        ) : null}
        <dt>完成时间</dt>
        <dd>{formatDateTime(attempt.completed_at)}</dd>
      </dl>
    </li>
  );
}
