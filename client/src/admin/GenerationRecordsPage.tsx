import { useCallback, useEffect, useRef, useState } from "react";

import {
  type AdminGenerationRecord,
  type AdminGenerationRecordSummary,
  getAdminGenerationRecordSummary,
  getAdminGenerationRecords,
  reconcileFirstFrameTask,
} from "../api.admin";
import { AnalysisDiagnosticPanel } from "./AnalysisDiagnosticPanel";
import { RecordCallsPanel } from "./RecordCallsPanel";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { StatusBadge } from "./ui/StatusBadge";
import { TabBar } from "./ui/TabBar";
import {
  FAILURE_PHASE_LABELS,
  formatDateTime,
  GENERATION_RECORD_TYPE_LABELS,
  GENERATION_STATUS_FILTERS,
  GENERATION_STATUS_LABELS,
  labelFrom,
  parseUtcTimestamp,
} from "./ui/vocabulary";

const PAGE_SIZE = 50;
// 只有 analysis_tasks 有失败阶段列，其他类型不该出现这个筛选项。
const ANALYSIS_RECORD_TYPE = "ANALYSIS";
// 首帧任务：只有"供应商提交结果未知"的行才可人工对账——服务端
// prepare_first_frame_reconcile 也只在 status='SUBMISSION_UNCERTAIN' 时放行，
// 其余状态一律 409 IMAGE_TASK_NOT_UNCERTAIN。前端按同一条件显示入口，
// 避免点开就是错。
const FIRST_FRAME_RECORD_TYPE = "FIRST_FRAME_IMAGE";
const SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN";

export function GenerationRecordsPage({
  initialStatus = "",
  initialRecordType = "",
  readOnly = false,
}: {
  initialStatus?: string;
  initialRecordType?: string;
  readOnly?: boolean;
}) {
  const [items, setItems] = useState<AdminGenerationRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [summary, setSummary] = useState<AdminGenerationRecordSummary | null>(
    null,
  );
  const [username, setUsername] = useState("");
  const [status, setStatus] = useState(initialStatus);
  const [recordType, setRecordType] = useState(initialRecordType);
  const [failurePhase, setFailurePhase] = useState("");
  const [taskRef, setTaskRef] = useState("");
  const [createdFrom, setCreatedFrom] = useState("");
  const [createdTo, setCreatedTo] = useState("");
  const [filters, setFilters] = useState({
    username: "",
    status: initialStatus,
    recordType: initialRecordType,
    failurePhase: "",
    taskRef: "",
    createdFrom: "",
    createdTo: "",
  });
  // 合并页签：诊断是同一页面的第二视图，跳转时把任务编号一起带过去。
  const [view, setView] = useState<"records" | "diagnostics">("records");
  const [diagnosticTaskId, setDiagnosticTaskId] = useState("");
  const [notice, setNotice] = useState("");
  // 首帧对账：确认框里只放"待处理的那一行"，避免把行对象散进多个状态。
  const [pendingReconcile, setPendingReconcile] =
    useState<AdminGenerationRecord | null>(null);
  const [reconcileBusy, setReconcileBusy] = useState(false);
  const [reconcileError, setReconcileError] = useState("");
  // 展开过详情的记录行：第三方调用记录只在首次展开时懒加载，
  // 避免为一屏记录白拉一屏接口。
  const [openedDetails, setOpenedDetails] = useState<Set<string>>(new Set());
  const requestIdRef = useRef(0);

  const loadRecords = useCallback(async () => {
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    try {
      setLoading(true);
      setError("");
      setItems([]);
      const query = {
        username: filters.username || undefined,
        status: filters.status || undefined,
        recordType: filters.recordType || undefined,
        failurePhase: filters.failurePhase || undefined,
        taskRef: filters.taskRef || undefined,
        createdFrom: filters.createdFrom || undefined,
        createdTo: filters.createdTo || undefined,
      };
      const [response, nextSummary] = await Promise.all([
        getAdminGenerationRecords({ limit: PAGE_SIZE, offset, ...query }),
        // 聚合拉不到时不能让列表一起报错：降级为「无聚合」，绝不用 0 冒充。
        getAdminGenerationRecordSummary(query).catch(() => null),
      ]);
      if (requestId !== requestIdRef.current) {
        return;
      }
      setItems(response.items);
      setTotal(response.total);
      setSummary(nextSummary);
    } catch (cause) {
      if (requestId !== requestIdRef.current) {
        return;
      }
      setError(
        cause instanceof Error && cause.message
          ? `加载失败：${cause.message}`
          : "加载失败：未知错误",
      );
    } finally {
      if (requestId === requestIdRef.current) {
        setLoading(false);
      }
    }
  }, [filters, offset]);

  useEffect(() => {
    void loadRecords();
  }, [loadRecords]);

  async function submitReconcile(reason: string) {
    if (!pendingReconcile || reconcileBusy) return;
    const target = pendingReconcile;
    setReconcileBusy(true);
    setReconcileError("");
    setNotice("");
    try {
      const result = await reconcileFirstFrameTask(target.record_id, reason);
      // 服务端按供应商真实回执裁决，两种结局对运营的含义完全不同，分开说清。
      setNotice(
        result.result === "RESUMED"
          ? `已对账：任务 ${result.task_id} 供应商侧已受理，已回到生成队列继续处理。`
          : `已对账：任务 ${result.task_id} 供应商侧未成功，已置为失败并走计费退回。`,
      );
      setPendingReconcile(null);
      await loadRecords();
    } catch (cause) {
      setReconcileError(
        cause instanceof Error && cause.message
          ? cause.message
          : "首帧任务对账失败：未知错误",
      );
    } finally {
      setReconcileBusy(false);
    }
  }

  return (
    <section
      aria-label="生成记录"
      className="admin-panel admin-generation-records"
    >
      <TabBar
        active={view}
        ariaLabel="生成记录页签"
        items={[
          { id: "records", label: "生成记录" },
          { id: "diagnostics", label: "任务诊断" },
        ]}
        onChange={(id) =>
          setView(id === "diagnostics" ? "diagnostics" : "records")
        }
      />
      {view === "diagnostics" ? (
        <AnalysisDiagnosticPanel
          initialTaskId={diagnosticTaskId}
          key={`diagnostics:${diagnosticTaskId}`}
        />
      ) : (
        <>
          <div className="admin-actions">
            <button type="button" onClick={() => void loadRecords()}>
              {loading ? "刷新中…" : "刷新记录"}
            </button>
            <span>共 {total} 条</span>
          </div>

          <p className="admin-hint">
            记录视频、口播、图片和 AI
            评分调用。上游未返回精确成本时会明确标注，不以零成本代替。
          </p>
          <form
            className="admin-toolbar"
            onSubmit={(event) => {
              event.preventDefault();
              setOffset(0);
              setFilters({
                username,
                status,
                recordType,
                // 类型切走时草稿可能残留阶段值，不能让它泄漏进查询。
                failurePhase:
                  recordType === ANALYSIS_RECORD_TYPE ? failurePhase : "",
                taskRef,
                createdFrom,
                createdTo,
              });
            }}
          >
            <label>
              账号
              <input
                aria-label="生成账号"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
              />
            </label>
            <label>
              任务编号
              <input
                aria-label="生成任务编号"
                value={taskRef}
                onChange={(event) => setTaskRef(event.target.value)}
              />
            </label>
            <label>
              类型
              <select
                aria-label="生成类型"
                value={recordType}
                onChange={(event) => {
                  const nextType = event.target.value;
                  setRecordType(nextType);
                  if (nextType !== ANALYSIS_RECORD_TYPE) {
                    setFailurePhase("");
                  }
                }}
              >
                <option value="">全部类型</option>
                <option value="VIDEO">视频</option>
                <option value="ORAL_VIDEO">口播视频</option>
                <option value="FIRST_FRAME_IMAGE">首帧图片</option>
                <option value="CHARACTER_SHEET_IMAGE">人物表</option>
                <option value="CHARACTER_VIEW_IMAGE">人物视图</option>
                <option value="SOURCE_FRAME_PROCESS">素材处理</option>
                <option value="SOURCE_FRAME_AI_SCORE">AI 评分</option>
                <option value={ANALYSIS_RECORD_TYPE}>视频拆解</option>
              </select>
            </label>
            {recordType === ANALYSIS_RECORD_TYPE ? (
              <label>
                失败阶段
                <select
                  aria-label="失败阶段"
                  value={failurePhase}
                  onChange={(event) => setFailurePhase(event.target.value)}
                >
                  <option value="">全部阶段</option>
                  {Object.entries(FAILURE_PHASE_LABELS).map(
                    ([value, label]) => (
                      <option key={value} value={value}>
                        {label}
                      </option>
                    ),
                  )}
                </select>
              </label>
            ) : null}
            <label>
              状态
              <select
                aria-label="生成状态"
                value={status}
                onChange={(event) => setStatus(event.target.value)}
              >
                <option value="">全部状态</option>
                {GENERATION_STATUS_FILTERS.map(({ value, label }) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              起始时间
              <input
                aria-label="生成起始时间"
                type="date"
                value={createdFrom}
                onChange={(event) => setCreatedFrom(event.target.value)}
              />
            </label>
            <label>
              截止时间
              <input
                aria-label="生成截止时间"
                type="date"
                value={createdTo}
                onChange={(event) => setCreatedTo(event.target.value)}
              />
            </label>
            <button type="submit">查询</button>
          </form>

          {!loading && summary === null ? (
            <PageBanner tone="notice">
              记录聚合暂不可用；列表数据不受影响。
            </PageBanner>
          ) : null}

          {summary && summary.total > 0 ? (
            <section className="admin-panel" aria-label="记录聚合">
              <h2>当前筛选聚合</h2>
              <p>
                {summary.counts
                  .map(
                    (row) =>
                      `${labelFrom(GENERATION_RECORD_TYPE_LABELS, row.record_type)} ${labelFrom(
                        GENERATION_STATUS_LABELS,
                        row.status,
                      )} ${row.count}`,
                  )
                  .join("、")}
              </p>
              {summary.failure_reasons.length > 0 ? (
                <ul>
                  {summary.failure_reasons.map((reason) => (
                    <li
                      key={`${reason.error_code}-${reason.failure_phase}-${reason.reason}`}
                    >
                      <span>
                        {reason.failure_phase
                          ? labelFrom(
                              FAILURE_PHASE_LABELS,
                              reason.failure_phase,
                            )
                          : "未知阶段"}
                        {" · "}
                        {reason.error_code ?? "未记录错误码"}
                        {" · "}
                        {reason.retryable ? "可重试" : "不可重试"}
                        {" · "}
                        {reason.count} 条
                      </span>
                      {reason.reason ? (
                        <small>上游说明：{reason.reason}</small>
                      ) : null}
                      {reason.advice ? (
                        <small>修复建议：{reason.advice}</small>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : null}
            </section>
          ) : null}

          {error ? <PageBanner tone="error">{error}</PageBanner> : null}
          {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

          {!loading && items.length === 0 ? (
            <PageBanner tone="notice">暂无生成记录。</PageBanner>
          ) : (
            <DataTable
              ariaLabel="用户生成记录列表"
              headers={
                <>
                  <th>时间</th>
                  <th>用户</th>
                  <th>项目</th>
                  <th>生成类型</th>
                  <th>服务 / 模型</th>
                  <th>状态</th>
                  <th>耗时</th>
                  <th>扣减额度</th>
                  <th>上游成本（元）</th>
                  <th>结果 / 错误</th>
                </>
              }
            >
              {items.map((item) => (
                <tr key={`${item.record_type}-${item.record_id}`}>
                  <td>{formatDateTime(item.created_at)}</td>
                  <td>{item.username}</td>
                  <td>{item.project_name ?? "—"}</td>
                  <td>
                    {labelFrom(GENERATION_RECORD_TYPE_LABELS, item.record_type)}
                  </td>
                  <td>{formatProvider(item)}</td>
                  <td>
                    <StatusBadge
                      tone={
                        item.status === "SUCCEEDED"
                          ? "good"
                          : item.status === "FAILED"
                            ? "danger"
                            : "warn"
                      }
                    >
                      {labelFrom(GENERATION_STATUS_LABELS, item.status)}
                    </StatusBadge>
                  </td>
                  <td>{formatDuration(item.created_at, item.completed_at)}</td>
                  <td>
                    {item.charged_credits > 0
                      ? `${item.charged_credits} 积分`
                      : "0 积分"}
                  </td>
                  <td>{formatProviderCost(item)}</td>
                  <td>
                    <details>
                      {/* biome-ignore lint/a11y/noStaticElementInteractions: summary 是 details 的固有开关（原生可聚焦、可键盘切换），这里只借用它的点击做懒加载登记。 */}
                      <summary
                        onClick={(event) => {
                          // 懒加载只在「即将打开」时触发：关闭动作不清空已加载内容。
                          const container =
                            event.currentTarget.closest("details");
                          if (
                            container instanceof HTMLDetailsElement &&
                            container.open
                          ) {
                            return;
                          }
                          const key = recordKey(item);
                          setOpenedDetails((prev) => {
                            if (prev.has(key)) return prev;
                            const next = new Set(prev);
                            next.add(key);
                            return next;
                          });
                        }}
                      >
                        <span>查看详情</span>
                        <small>{formatResult(item)}</small>
                      </summary>
                      <dl>
                        <dt>记录编号</dt>
                        <dd>{item.record_id}</dd>
                        <dt>结果引用</dt>
                        <dd>{item.result_reference ?? "—"}</dd>
                        <dt>供应商任务凭证</dt>
                        <dd>{item.provider_reference ?? "—"}</dd>
                        <dt>错误码</dt>
                        <dd>{item.error_code ?? "—"}</dd>
                        <dt>错误说明</dt>
                        <dd>{item.error_message ?? "—"}</dd>
                        {item.provider_error_code ? (
                          <>
                            <dt>服务商错误码</dt>
                            <dd>{item.provider_error_code}</dd>
                          </>
                        ) : null}
                        {item.provider_message ? (
                          <>
                            <dt>服务商原话</dt>
                            <dd>{item.provider_message}</dd>
                          </>
                        ) : null}
                        {item.advice ? (
                          <>
                            <dt>修复建议</dt>
                            <dd>{item.advice}</dd>
                          </>
                        ) : null}
                        {item.failure_phase ? (
                          <>
                            <dt>失败阶段</dt>
                            <dd>
                              {labelFrom(
                                FAILURE_PHASE_LABELS,
                                item.failure_phase,
                              )}
                            </dd>
                          </>
                        ) : null}
                        {item.status === "FAILED" && item.retryable != null ? (
                          <>
                            <dt>可否重试</dt>
                            <dd>{item.retryable ? "可重试" : "不可重试"}</dd>
                          </>
                        ) : null}
                        {item.upstream_status != null ? (
                          <>
                            <dt>上游状态码</dt>
                            <dd>{item.upstream_status}</dd>
                          </>
                        ) : null}
                        {item.upstream_reason ? (
                          <>
                            <dt>上游说明</dt>
                            <dd>{item.upstream_reason}</dd>
                          </>
                        ) : null}
                        <dt>记录数据</dt>
                        <dd>
                          {item.record_data_status === "CORRUPTED"
                            ? "记录数据损坏"
                            : "正常"}
                        </dd>
                      </dl>
                      <RecordCallsPanel
                        active={openedDetails.has(recordKey(item))}
                        readOnly={readOnly}
                        recordId={item.record_id}
                        recordType={item.record_type}
                      />
                      {item.record_type === ANALYSIS_RECORD_TYPE ? (
                        <button
                          className="admin-diagnostics__jump"
                          type="button"
                          onClick={() => {
                            setDiagnosticTaskId(item.record_id);
                            setView("diagnostics");
                          }}
                        >
                          查看诊断
                        </button>
                      ) : null}
                      {!readOnly &&
                      item.record_type === FIRST_FRAME_RECORD_TYPE &&
                      item.status === SUBMISSION_UNCERTAIN ? (
                        <button
                          aria-label={`重新对账首帧任务 ${item.record_id}`}
                          className="admin-generation-records__reconcile"
                          type="button"
                          onClick={() => {
                            setPendingReconcile(item);
                            setReconcileError("");
                          }}
                        >
                          重新对账
                        </button>
                      ) : null}
                    </details>
                  </td>
                </tr>
              ))}
            </DataTable>
          )}

          <Pagination
            disabled={loading}
            limit={PAGE_SIZE}
            offset={offset}
            total={total}
            onPageChange={setOffset}
          />
        </>
      )}

      {/* 首帧对账：standard 级。该端点不接受也不消费 reason（服务端按供应商
          真实回执自行裁决并写审计），让运营手填一个被丢弃的原因只会制造
          "已经留痕"的错觉，因此这里不放原因输入，只把后果说明白。 */}
      <ConfirmDialog
        busy={reconcileBusy}
        confirmLabel="重新对账"
        description={
          pendingReconcile
            ? `将向图像供应商核对任务 ${pendingReconcile.record_id} 的真实提交结果：已受理则回到生成队列继续处理；未成功则置为失败并释放预扣额度。同一任务重复对账会被拒绝。`
            : undefined
        }
        error={reconcileError}
        level="standard"
        open={pendingReconcile !== null && !readOnly}
        title="重新对账首帧任务"
        onClose={() => {
          if (reconcileBusy) return;
          setPendingReconcile(null);
          setReconcileError("");
        }}
        onConfirm={(reason) => void submitReconcile(reason)}
      />
    </section>
  );
}

function formatProvider(item: AdminGenerationRecord): string {
  return [item.provider, item.model].filter(Boolean).join(" / ") || "本地处理";
}

function formatProviderCost(item: AdminGenerationRecord): string {
  if (item.provider_cost_status === "NOT_APPLICABLE") {
    return "无付费调用";
  }
  if (
    item.provider_cost_status === "UNAVAILABLE" ||
    item.provider_cost === null
  ) {
    return "上游未回传";
  }
  if (item.provider_cost_status === "ESTIMATED") {
    return `估算 ${item.provider_cost}`;
  }
  return String(item.provider_cost);
}

function formatResult(item: AdminGenerationRecord): string {
  if (item.record_data_status === "CORRUPTED") {
    return "记录数据损坏";
  }
  return item.error_code ?? item.result_reference ?? "—";
}

function formatDuration(createdAt: string, completedAt: string | null): string {
  if (!completedAt) return "进行中";
  const seconds = Math.max(
    0,
    Math.round(
      (parseUtcTimestamp(completedAt) - parseUtcTimestamp(createdAt)) / 1000,
    ),
  );
  if (!Number.isFinite(seconds)) return "—";
  const minutes = Math.floor(seconds / 60);
  return minutes > 0 ? `${minutes} 分 ${seconds % 60} 秒` : `${seconds} 秒`;
}

function recordKey(item: AdminGenerationRecord): string {
  return `${item.record_type}-${item.record_id}`;
}
