import { useCallback, useEffect, useRef, useState } from "react";

import {
  type AdminGenerationRecord,
  type AdminGenerationRecordSummary,
  createCustomerAdjustment,
  getAdminGenerationRecordSummary,
  getAdminGenerationRecords,
  getGenerationRecordContent,
  getGenerationRecordThumbnail,
  reconcileFirstFrameTask,
  retryGenerationRecord,
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
  formatFen,
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
// 一键重试仅支持视频任务（服务端 retry_generation_task 只覆盖 generation_tasks）。
const VIDEO_RECORD_TYPE = "VIDEO";
// 显示一键处理的失败终局：与服务端 _EXPLAINED_STATUSES 对齐——这些状态
// 会带失败分类/处理人/积分退回信息。
const FAILURE_STATUSES = new Set([
  "FAILED",
  SUBMISSION_UNCERTAIN,
  "UNKNOWN",
  "ARCHIVE_FAILED",
  "CANCELED",
  "CANCELLED",
]);
// 重试入口只给「可能被服务端放行」的两种终局：普通失败（PRE_PROVIDER 路径）
// 与提交结果待核对（点击即得「先核对再重试」的中文指引）。用户主动取消无需
// 重试，UNKNOWN 该先完成核对，都不给入口。
const RETRY_ENTRY_STATUSES = new Set(["FAILED", SUBMISSION_UNCERTAIN]);
// 有产物可预览的记录类型里，视频类用 <video> 播放成片，其余用 <img> 展示
// 生成图（与服务端媒体端点的「有内容可读」类型一致）。
const VIDEO_PREVIEW_TYPES = new Set([VIDEO_RECORD_TYPE, "ORAL_VIDEO"]);

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
  // 一键处理（方案 P1-4）：重试与补偿各自独立的确认框状态，互不干扰。
  const [pendingRetry, setPendingRetry] =
    useState<AdminGenerationRecord | null>(null);
  const [retryBusy, setRetryBusy] = useState(false);
  const [retryError, setRetryError] = useState("");
  const [pendingCompensation, setPendingCompensation] =
    useState<AdminGenerationRecord | null>(null);
  const [compensationBusy, setCompensationBusy] = useState(false);
  const [compensationError, setCompensationError] = useState("");
  const [compensationCredits, setCompensationCredits] = useState("");
  // 补偿的幂等键按「客户 + 记录 + 数量」指纹复用：内容没变的重试沿用同一键
  // （服务端重放不重复调账），运营改了数量则按新请求换键。
  const compensationRetryRef = useRef<{
    fingerprint: string;
    key: string;
  } | null>(null);
  // 展开过详情的记录行：第三方调用记录只在首次展开时懒加载，
  // 避免为一屏记录白拉一屏接口。
  const [openedDetails, setOpenedDetails] = useState<Set<string>>(new Set());
  const requestIdRef = useRef(0);
  // 预览层（P2-2「查看成片」）：content 是高敏读取（服务端每次查看都写
  // generation_record.content_view 审计），只在显式点击时拉取。url 为
  // object URL，关闭 / 切换记录 / 卸载时释放，避免泄漏。
  const [preview, setPreview] = useState<{
    record: AdminGenerationRecord;
    /** object URL；空串表示还在拉取中。 */
    url: string;
    /** 拉取失败的可读说明；非空即终态。 */
    error: string;
  } | null>(null);
  const previewGenerationRef = useRef(0);
  const previewUrlRef = useRef("");

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

  // Esc 关闭预览层：与 ConfirmDialog 同一键盘口径。
  // biome-ignore lint/correctness/useExhaustiveDependencies: closePreview 只读写 object URL ref 与 setState，引用等价稳定；开合由 preview 驱动。
  useEffect(() => {
    if (!preview) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closePreview();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [preview]);

  // 卸载（切走页签 / 退出管理端）时释放未关闭的预览 object URL。
  useEffect(() => {
    return () => {
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
    };
  }, []);

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

  async function submitRetry(reason: string) {
    if (!pendingRetry || retryBusy) return;
    const target = pendingRetry;
    setRetryBusy(true);
    setRetryError("");
    setNotice("");
    try {
      const result = await retryGenerationRecord(target.record_id, reason);
      // 两条重试路径的运营含义不同：重新入队（PENDING）会再次预扣积分，
      // 恢复存档（SUCCEEDED）沿用原有计费——分开说清，避免误读成重复扣费。
      setNotice(
        result.status === "PENDING"
          ? `已重新入队：任务 ${result.task_id} 将重新生成，并按规则再次预扣积分。`
          : `已恢复存档：任务 ${result.task_id} 将重试归档，沿用原有结果与计费。`,
      );
      setPendingRetry(null);
      await loadRecords();
    } catch (cause) {
      setRetryError(
        cause instanceof Error && cause.message
          ? cause.message
          : "重试生成任务失败：未知错误",
      );
    } finally {
      setRetryBusy(false);
    }
  }

  async function submitCompensation(reason: string) {
    if (!pendingCompensation || compensationBusy) return;
    const target = pendingCompensation;
    const credits = Number(compensationCredits.trim());
    if (!Number.isInteger(credits) || credits <= 0) {
      setCompensationError("补偿积分需为正整数");
      return;
    }
    const fingerprint = JSON.stringify({
      userId: target.user_id,
      recordId: target.record_id,
      credits,
    });
    if (compensationRetryRef.current?.fingerprint !== fingerprint) {
      compensationRetryRef.current = { fingerprint, key: crypto.randomUUID() };
    }
    setCompensationBusy(true);
    setCompensationError("");
    setNotice("");
    try {
      const result = await createCustomerAdjustment(
        target.user_id,
        {
          sourceDocumentType: "CREDIT_COMPENSATION",
          sourceDocumentRef: target.record_id,
          credits,
        },
        reason,
        compensationRetryRef.current.key,
      );
      setNotice(
        `已补偿 ${credits} 积分：客户余额现为 ${result.wallet_balance_after} 积分。`,
      );
      compensationRetryRef.current = null;
      setPendingCompensation(null);
      await loadRecords();
    } catch (cause) {
      setCompensationError(
        cause instanceof Error && cause.message
          ? cause.message
          : "积分补偿失败：未知错误",
      );
    } finally {
      setCompensationBusy(false);
    }
  }

  async function copyCustomerNote(item: AdminGenerationRecord) {
    setNotice("");
    try {
      await navigator.clipboard.writeText(customerNoteFor(item));
      setNotice(`已复制任务 ${item.record_id} 的客户说明，可直接转发。`);
    } catch {
      setNotice("复制失败：浏览器拒绝了剪贴板访问，请手动记录说明。");
    }
  }

  /** 释放当前预览的 object URL（若有）。 */
  function releasePreviewUrl() {
    if (!previewUrlRef.current) return;
    URL.revokeObjectURL(previewUrlRef.current);
    previewUrlRef.current = "";
  }

  /** 打开成片 / 生成图预览：先展示空壳，内容拉取成功后再点亮播放器。 */
  async function openPreview(item: AdminGenerationRecord) {
    const generation = ++previewGenerationRef.current;
    releasePreviewUrl();
    setPreview({ record: item, url: "", error: "" });
    try {
      const blob = await getGenerationRecordContent(
        item.record_type,
        item.record_id,
      );
      // 迟到的响应可能属于已关闭 / 已切换的预览：按代号丢弃，不点亮旧层。
      if (generation !== previewGenerationRef.current) return;
      const url = URL.createObjectURL(blob);
      previewUrlRef.current = url;
      setPreview({ record: item, url, error: "" });
    } catch (cause) {
      if (generation !== previewGenerationRef.current) return;
      setPreview({
        record: item,
        url: "",
        error:
          cause instanceof Error && cause.message
            ? cause.message
            : "读取生成内容失败：未知错误",
      });
    }
  }

  function closePreview() {
    // 作废在途请求：响应回来时不该再点亮已关闭的预览层。
    previewGenerationRef.current += 1;
    releasePreviewUrl();
    setPreview(null);
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
                {/* P2-1：类型下拉从词典生成——此前手写列表漏了口播分身/声音克隆，
                    且"首帧图片/人物表"与记录表里的类型列各说各话。 */}
                {Object.entries(GENERATION_RECORD_TYPE_LABELS).map(
                  ([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ),
                )}
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
                      key={`${reason.record_type}-${reason.error_code}-${reason.failure_phase}-${reason.reason}`}
                    >
                      <span>
                        {labelFrom(
                          GENERATION_RECORD_TYPE_LABELS,
                          reason.record_type,
                        )}
                        {" · "}
                        {reason.failure_phase
                          ? labelFrom(
                              FAILURE_PHASE_LABELS,
                              reason.failure_phase,
                            )
                          : "未知阶段"}
                        {" · "}
                        {reason.error_code ?? "未记录错误码"}
                        {/* retryable 缺失（如取帧/评分族无此语义）时省掉这段，
                            不能把「未判定」写成「不可重试」。 */}
                        {reason.retryable != null
                          ? ` · ${reason.retryable ? "可重试" : "不可重试"}`
                          : ""}
                        {" · "}
                        {reason.count} 条
                      </span>
                      {reason.category || reason.owner ? (
                        <small>
                          失败分类：{reason.category ?? "未分类"}
                          {reason.owner ? ` · 处理人：${reason.owner}` : ""}
                        </small>
                      ) : null}
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
                  <th>扣减积分</th>
                  <th>成本</th>
                  {/* 预览列随审计员整体隐藏：媒体端点对 auditor 一律 403。 */}
                  {!readOnly ? <th>结果预览</th> : null}
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
                  {!readOnly ? (
                    <td className="admin-generation-records__preview-cell">
                      {item.has_preview ? (
                        <button
                          aria-label={`${previewActionLabel(item)} ${item.record_id}`}
                          className="admin-generation-records__preview"
                          type="button"
                          onClick={() => void openPreview(item)}
                        >
                          <RecordThumbnail item={item} />
                          <small>{previewActionLabel(item)}</small>
                        </button>
                      ) : (
                        <span className="admin-generation-records__preview-empty">
                          无产物
                        </span>
                      )}
                    </td>
                  ) : null}
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
                        {item.failure_category ? (
                          <>
                            <dt>失败分类</dt>
                            <dd>{item.failure_category}</dd>
                          </>
                        ) : null}
                        {item.failure_owner ? (
                          <>
                            <dt>处理人</dt>
                            <dd>{item.failure_owner}</dd>
                          </>
                        ) : null}
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
                        {item.credits_refunded != null ? (
                          <>
                            <dt>积分退回</dt>
                            <dd>
                              {item.credits_refunded ? "已退回" : "未退回"}
                            </dd>
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
                      {!readOnly && FAILURE_STATUSES.has(item.status) ? (
                        <div className="admin-generation-records__actions">
                          {item.record_type === VIDEO_RECORD_TYPE &&
                          RETRY_ENTRY_STATUSES.has(item.status) ? (
                            <button
                              aria-label={`重试任务 ${item.record_id}`}
                              type="button"
                              onClick={() => {
                                setPendingRetry(item);
                                setRetryError("");
                              }}
                            >
                              重试任务
                            </button>
                          ) : null}
                          <button
                            aria-label={`补偿积分 ${item.record_id}`}
                            type="button"
                            onClick={() => {
                              setPendingCompensation(item);
                              setCompensationError("");
                              setCompensationCredits(
                                String(Math.max(0, item.charged_credits)),
                              );
                              compensationRetryRef.current = null;
                            }}
                          >
                            补偿积分
                          </button>
                          <button
                            aria-label={`复制客户说明 ${item.record_id}`}
                            type="button"
                            onClick={() => void copyCustomerNote(item)}
                          >
                            复制客户说明
                          </button>
                        </div>
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
            ? `将向图像供应商核对任务 ${pendingReconcile.record_id} 的真实提交结果：已受理则回到生成队列继续处理；未成功则置为失败并退回预扣积分。同一任务重复对账会被拒绝。`
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

      <ConfirmDialog
        busy={retryBusy}
        confirmLabel="重新入队"
        description={
          pendingRetry
            ? `将按现有规则尝试原地重试任务 ${pendingRetry.record_id}：未触达服务商的失败会重新生成并再次预扣积分；已付款结果会改为重试归档、不重新计费。服务端不满足条件时会拒绝并说明原因。`
            : undefined
        }
        error={retryError}
        level="reason"
        open={pendingRetry !== null && !readOnly}
        title="重试生成任务"
        onClose={() => {
          if (retryBusy) return;
          setPendingRetry(null);
          setRetryError("");
        }}
        onConfirm={(reason) => void submitRetry(reason)}
      />

      {/* 补偿积分走既有调账通道（CREDIT_COMPENSATION）：数量默认填该条被扣的
          积分、可改；确认后直接进客户钱包，因此是 reasonAndAck 级。 */}
      <ConfirmDialog
        busy={compensationBusy}
        confirmLabel="发放补偿"
        description={
          pendingCompensation
            ? `将向客户 ${pendingCompensation.username} 发放一笔积分补偿（来源单据：记录 ${pendingCompensation.record_id}）。`
            : undefined
        }
        error={compensationError}
        level="reasonAndAck"
        open={pendingCompensation !== null && !readOnly}
        title="补偿积分"
        onClose={() => {
          if (compensationBusy) return;
          setPendingCompensation(null);
          setCompensationError("");
        }}
        onConfirm={(reason) => void submitCompensation(reason)}
      >
        <label>
          补偿积分
          <input
            aria-label="补偿积分数量"
            inputMode="numeric"
            value={compensationCredits}
            onChange={(event) => setCompensationCredits(event.target.value)}
          />
        </label>
      </ConfirmDialog>

      {/* 成片 / 生成图预览（P2-2）：content 端点为高敏读取（服务端写审计），
          只在显式点击时拉取；遮罩点击或 Esc 关闭并释放 object URL。 */}
      {preview ? (
        // biome-ignore lint/a11y/noStaticElementInteractions: 遮罩是 aria-modal dialog 模式的标准视觉层，键盘经 Esc 与对话框内控件交互
        <div
          className="admin-dialog-overlay"
          onClick={closePreview}
          onKeyDown={(event) => {
            if (event.key === "Escape") closePreview();
          }}
          role="presentation"
        >
          <section
            aria-label={previewTitle(preview.record)}
            aria-modal="true"
            className="admin-dialog admin-generation-records__preview-dialog"
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => event.stopPropagation()}
            role="dialog"
          >
            <h2>{previewTitle(preview.record)}</h2>
            <p className="admin-hint admin-dialog__description">
              {`任务 ${preview.record.record_id} · ${preview.record.username} · ${labelFrom(
                GENERATION_RECORD_TYPE_LABELS,
                preview.record.record_type,
              )}`}
            </p>
            {preview.error ? (
              <p className="settings-error" role="alert">
                {preview.error}
              </p>
            ) : preview.url ? (
              VIDEO_PREVIEW_TYPES.has(preview.record.record_type) ? (
                <video
                  aria-label={`成片 ${preview.record.record_id}`}
                  className="admin-generation-records__preview-media"
                  controls
                  muted
                  src={preview.url}
                />
              ) : (
                <img
                  alt={`生成图 ${preview.record.record_id}`}
                  className="admin-generation-records__preview-media"
                  src={preview.url}
                />
              )
            ) : (
              <p className="admin-hint">正在读取生成结果…</p>
            )}
            <div className="admin-actions">
              <button type="button" onClick={closePreview}>
                关闭
              </button>
            </div>
          </section>
        </div>
      ) : null}
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
    return "成本待核对";
  }
  // provider_cost 是元（float），先换算成分为整数再走 formatFen，
  // 避免 1.25 * 100 = 125.00000000000001 这类浮点尾数。
  const fen = Math.round(item.provider_cost * 100);
  if (item.provider_cost_status === "ESTIMATED") {
    return `估算 ${formatFen(fen)}`;
  }
  return formatFen(fen);
}

function formatResult(item: AdminGenerationRecord): string {
  if (item.record_data_status === "CORRUPTED") {
    return "记录数据损坏";
  }
  return item.error_code ?? item.result_reference ?? "—";
}

/** 失败记录的客户说明文案（方案 P1-4「复制给客户的说明」）。
 *
 *  面向客户/客服的口吻：只说发生了什么、怎么处理、积分怎么算，不出现内部
 *  错误码与处理人角色——那些是运营自己看的。 */
function customerNoteFor(item: AdminGenerationRecord): string {
  const lines = [
    "【生成任务处理说明】",
    `任务编号：${item.record_id}`,
    `业务类型：${labelFrom(GENERATION_RECORD_TYPE_LABELS, item.record_type)}`,
  ];
  if (item.failure_category) {
    lines.push(`失败分类：${item.failure_category}`);
  }
  if (item.advice) {
    lines.push(`处理建议：${item.advice}`);
  }
  lines.push(
    item.credits_refunded === true
      ? "积分处理：本次消耗的积分已退回，请查收。"
      : item.credits_refunded === false
        ? "积分处理：本次消耗的积分将按流程退回或补偿，请留意后续通知。"
        : "积分处理：请与客服核对本次消耗的积分。",
  );
  return lines.join("\n");
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

/** 预览动作文案：视频类看的是成片，图片类看的是生成图。 */
function previewActionLabel(item: AdminGenerationRecord): string {
  return VIDEO_PREVIEW_TYPES.has(item.record_type) ? "查看成片" : "查看图片";
}

function previewTitle(item: AdminGenerationRecord): string {
  return VIDEO_PREVIEW_TYPES.has(item.record_type) ? "成片预览" : "生成图预览";
}

/** 行内缩略图（P2-2）：只有 has_preview 的记录才拉取；404 / 派生失败时降级
 *  为占位文案，不打断整行渲染，也不挡住「查看成片」入口（原件可能仍在）。 */
function RecordThumbnail({ item }: { item: AdminGenerationRecord }) {
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    let objectUrl = "";
    getGenerationRecordThumbnail(item.record_type, item.record_id)
      .then((blob) => {
        if (!active) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (active) setFailed(true);
      });
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
    // 行以 record_type + record_id 为键，两者变化即另一条记录。
  }, [item.record_id, item.record_type]);

  if (failed) {
    return (
      <span className="admin-generation-records__preview-empty">
        缩略图不可用
      </span>
    );
  }
  if (!url) {
    return (
      <span className="admin-generation-records__preview-empty">载入中…</span>
    );
  }
  return (
    <img
      alt={`任务 ${item.record_id} 缩略图`}
      className="admin-generation-records__thumb"
      src={url}
    />
  );
}
