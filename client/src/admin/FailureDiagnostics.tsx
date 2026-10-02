import { useEffect, useState } from "react";
import {
  type AdminGenerationRecord,
  adminRead,
  getAdminGenerationRecords,
} from "../api.admin";
import { AnalysisDiagnosticPanel } from "./AnalysisDiagnosticPanel";
import { RecordCallsPanel } from "./RecordCallsPanel";
import { RecordStatusHistory } from "./RecordStatusHistory";
import { Pagination } from "./ui/Pagination";
import {
  FAILURE_CATEGORY_LABELS,
  FAILURE_OWNER_LABELS,
  formatDateTime,
  labelFrom,
  GENERATION_RECORD_TYPE_LABELS as RECORD_TYPE_LABELS,
} from "./ui/vocabulary";

export function FailureDiagnostics({
  initialTaskId = "",
  userId = "",
  readOnly = false,
}: {
  initialTaskId?: string;
  userId?: string;
  readOnly?: boolean;
}) {
  const [category, setCategory] = useState("");
  const [recordType, setRecordType] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<{
    items: AdminGenerationRecord[];
    total: number;
  } | null>(null);
  const [selected, setSelected] = useState<AdminGenerationRecord | null>(null);
  const [techOpen, setTechOpen] = useState(false);
  const [pendingCodes, setPendingCodes] = useState<
    Array<{
      provider: string;
      error_code: string | null;
      count: number;
      last_seen_at: string;
    }>
  >([]);
  const [pendingState, setPendingState] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const [draft, setDraft] = useState({ username: "", from: "", to: "" });
  const [filters, setFilters] = useState(draft);
  // biome-ignore lint/correctness/useExhaustiveDependencies: 手动刷新序号用于重新读取队列。
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    setPage(null);
    setSelected(null);
    setTechOpen(false);
    const from = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
    }).format(new Date(Date.now() - 6 * 86400000));
    void getAdminGenerationRecords({
      diagnostics: true,
      userId: userId || undefined,
      username: filters.username || undefined,
      failureCategory: category || undefined,
      recordType: recordType || undefined,
      taskRef: initialTaskId || undefined,
      createdFrom: filters.from || (initialTaskId ? undefined : from),
      createdTo: filters.to || undefined,
      offset,
      limit: 20,
    })
      .then((result) => {
        if (!active) return;
        setPage(result);
        if (initialTaskId && result.items.length === 1)
          setSelected(result.items[0]);
      })
      .catch((cause) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "读取诊断失败");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [category, recordType, offset, initialTaskId, reload, filters]);
  return (
    <section aria-label="全类型失败诊断">
      <h3>失败诊断与待归类队列</h3>
      <details>
        <summary>技术详情：待归类服务商错误码</summary>
        <button
          type="button"
          onClick={() => {
            setPendingState("正在读取…");
            void adminRead<typeof pendingCodes>(
              "/api/control/provider-errors/pending",
              "读取待归类错误码失败",
            )
              .then((items) => {
                setPendingCodes(items);
                setPendingState(items.length ? "" : "没有待归类错误码。");
              })
              .catch((cause) =>
                setPendingState(
                  cause instanceof Error ? cause.message : "读取失败",
                ),
              );
          }}
        >
          读取待归类错误码
        </button>
        {pendingState ? <p role="status">{pendingState}</p> : null}
        <ul>
          {pendingCodes.map((item) => (
            <li key={`${item.provider}:${item.error_code}`}>
              {item.provider} · {item.error_code ?? "未提供错误码"} ·{" "}
              {item.count} 次 · 最近 {formatDateTime(item.last_seen_at)}
            </li>
          ))}
        </ul>
      </details>
      <p>
        默认查看近7天创建的失败或待核对任务，优先按实测失败时间排序；旧记录回退到完成或创建时间，真实失败时刻未知。
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setOffset(0);
          setFilters(draft);
        }}
      >
        <label>
          诊断客户用户名
          <input
            value={draft.username}
            onChange={(event) =>
              setDraft({ ...draft, username: event.target.value })
            }
          />
        </label>
        <label>
          诊断创建开始日期
          <input
            type="date"
            value={draft.from}
            onChange={(event) =>
              setDraft({ ...draft, from: event.target.value })
            }
          />
        </label>
        <label>
          诊断创建结束日期
          <input
            type="date"
            value={draft.to}
            onChange={(event) => setDraft({ ...draft, to: event.target.value })}
          />
        </label>
        <button type="submit">查询诊断</button>
      </form>
      <label>
        诊断任务类型
        <select
          value={recordType}
          onChange={(event) => {
            setRecordType(event.target.value);
            setOffset(0);
          }}
        >
          <option value="">全部任务类型</option>
          {Object.entries(RECORD_TYPE_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <label>
        诊断原因分类
        <select
          value={category}
          onChange={(event) => {
            setCategory(event.target.value);
            setOffset(0);
          }}
        >
          <option value="">全部原因</option>
          {Object.entries(FAILURE_CATEGORY_LABELS)
            .filter(
              ([value]) =>
                value !== "NOT_A_FAILURE" && value !== "UNCLASSIFIED",
            )
            .map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          <option value="UNCLASSIFIED">待归类</option>
        </select>
      </label>
      <button type="button" onClick={() => setReload((value) => value + 1)}>
        刷新诊断队列
      </button>
      {loading ? <p role="status">正在读取诊断…</p> : null}
      {error ? <p role="alert">{error}</p> : null}
      {page?.items.length === 0 ? <p>当前条件没有待处理任务。</p> : null}
      {page?.items.map((item) => (
        <article key={`${item.record_type}:${item.record_id}`}>
          <h4>
            {labelFrom(RECORD_TYPE_LABELS, item.record_type)} ·{" "}
            {item.project_name ?? item.record_id}
          </h4>
          <p>
            {item.username} ·{" "}
            {formatDateTime(
              item.failed_at ?? item.completed_at ?? item.created_at,
            )}{" "}
            ·{" "}
            {item.failure_category
              ? labelFrom(FAILURE_CATEGORY_LABELS, item.failure_category)
              : "待归类"}
          </p>
          <p>
            {item.advice ?? "原因尚未归类，请交技术核对任务和本轮积分状态。"}
          </p>
          <button type="button" onClick={() => setSelected(item)}>
            查看诊断详情
          </button>
          <a
            href={`#admin/customersMgmt?userId=${encodeURIComponent(item.user_id)}`}
          >
            查看客户
          </a>
        </article>
      ))}
      {page ? (
        <Pagination
          total={page.total}
          offset={offset}
          limit={20}
          onPageChange={setOffset}
          noun="条"
        />
      ) : null}
      {!selected ? (
        <AnalysisDiagnosticPanel
          initialTaskId={initialTaskId}
          key={initialTaskId}
        />
      ) : null}
      {selected ? (
        <section aria-label="任务诊断详情">
          <h4>{selected.project_name ?? "任务诊断"}</h4>
          <p>
            处理建议：
            {selected.advice ?? "待归类，请保留问题编号交由技术核对。"}
          </p>
          <p>
            处理人：
            {selected.failure_owner
              ? labelFrom(FAILURE_OWNER_LABELS, selected.failure_owner)
              : "待归类"}
          </p>
          <details onToggle={(event) => setTechOpen(event.currentTarget.open)}>
            <summary>技术详情</summary>
            <p>
              任务编号：{selected.record_id} · 错误编号：
              {selected.short_ref ?? "未登记"}
            </p>
            <p>主记录：{selected.root_task_id ?? selected.record_id}</p>
            <p>错误码：{selected.error_code ?? "未记录"}</p>
            <p>
              上游原因：
              {selected.provider_message ??
                selected.upstream_reason ??
                "未记录"}
            </p>
            <RecordCallsPanel
              active={techOpen}
              readOnly={readOnly}
              recordId={selected.record_id}
              recordType={selected.record_type}
            />
            <RecordStatusHistory
              key={`${selected.record_type}:${selected.record_id}`}
              recordId={selected.record_id}
              recordType={selected.record_type}
            />
            {selected.record_type === "ANALYSIS" ? (
              <AnalysisDiagnosticPanel
                initialTaskId={selected.record_id}
                key={selected.record_id}
              />
            ) : null}
          </details>
        </section>
      ) : null}
    </section>
  );
}
