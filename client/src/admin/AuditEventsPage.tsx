import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  type AuditLogItem,
  downloadAuditLogCsv,
  listAuditLog,
} from "../api.admin";
import {
  AUDIT_EVENT_LABELS,
  AUDIT_GROUP_MEMBERS,
  auditEventLabel,
  auditGroupLabel,
  BILLING_SERVICE_NAMES,
} from "./auditVocabulary";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { formatDateTime, formatFen } from "./ui/vocabulary";
import "./admin-audit.css";

/** 审计事件分组（方案 P1）：「分组 → 事件」两级筛选；键与服务端一致。 */
const EVENT_GROUP_OPTIONS = [
  ["", "全部分组"],
  ["funds", "资金"],
  ["pricing", "价格与套餐"],
  ["account", "账号与设备"],
  ["system", "系统配置"],
  ["content", "内容与采集"],
  ["secret_export", "密钥与导出"],
  ["login", "登录"],
] as const;

/** 分组包含的事件类型；与服务端 EVENT_GROUPS 同口径，用于过滤事件下拉。 */
const EVENT_GROUP_MEMBERS = AUDIT_GROUP_MEMBERS;

/** 计费科目 key → 业务名（方案 P1：科目不再直出英文 key）。 */
const SUBJECT_LABELS: Record<string, string> = {
  ...BILLING_SERVICE_NAMES,
  video_generation_768p: "视频生成 · 768P",
  video_generation_2k: "视频生成 · 2K",
  video_analysis_768p: "视频分析 · 768P",
  video_analysis_2k: "视频分析 · 2K",
  external_price_768p: "外部售价 · 768P",
  external_price_2k: "外部售价 · 2K",
  first_frame_image: "首帧图片",
  character_sheet_image: "人物五视图",
  image_generation: "图片生成",
  character_sheet: "人物表",
  character_view: "人物单视图",
  context_ir: "上下文改写",
  customer_unit_price: "客户单价",
};

function subjectLabel(subject: string | null | undefined): string {
  if (!subject) return "";
  return SUBJECT_LABELS[subject] ?? "业务配置项";
}

const EVENT_OPTIONS = [["", "全部事件"], ...Object.entries(AUDIT_EVENT_LABELS)];

// 方案 P0-3：同一张表里既有管理员动作也有客户工作台动作，默认只看管理员——
// 否则客户建项目、读素材淹没处置记录。客户维度的完整动作在客户详情的
// 「操作记录」里看（scope=customer + target_user_id）。
type AuditScope = "admin" | "customer" | "all";

function asAuditScope(value: string): AuditScope {
  return value === "customer" || value === "all" ? value : "admin";
}

/** 高敏动作：列表里标红，便于从一屏日志里先看到它们。 */
const SENSITIVE_EVENTS = new Set([
  "provider_settings.secret_reveal",
  "control.export",
  "customer_package.grant",
  "admin_session.exchange",
  "admin.activation_code.revealed",
  "payment.wechat.update",
  "payment.provider.update",
  "external_call.response_view",
]);

function eventTone(eventType: string) {
  if (SENSITIVE_EVENTS.has(eventType)) return "danger";
  if (/REVOK|DENIED|FAILED|FORCE/.test(eventType)) return "danger";
  if (eventType.includes("rate") || eventType.includes("price"))
    return "warning";
  if (eventType.includes("DEVICE") || eventType.includes("SESSION"))
    return "info";
  return "success";
}

function priceUnit(item: AuditLogItem) {
  if (item.event_type.startsWith("customer_unit_price.")) return "/秒";
  switch (item.change_subject) {
    case "video_generation_768p":
    case "video_generation_2k":
    case "video_analysis_768p":
    case "video_analysis_2k":
    case "external_price_768p":
    case "external_price_2k":
      return "/秒";
    case "first_frame_image":
    case "character_sheet_image":
    case "image_generation":
    case "character_sheet":
    case "character_view":
      return "/张";
    case "context_ir":
      return "/次";
    default:
      return "";
  }
}

/** 金额统一 ¥x.xx（方案 P1 词典：审计里不再出现裸「分」）。 */
function fenAmount(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return formatFen(Number(value));
}

function compactAmount(value?: string | null) {
  if (value === null || value === undefined || value === "") return "未配置";
  const [whole, fraction = ""] = String(value).split(".");
  const trimmed = fraction.replace(/0+$/, "");
  return trimmed ? `${whole}.${trimmed}` : whole;
}

/** 费率审计的派生明细：售价（积分）与成本（分）的前后值，启用状态变化单独提。 */
function tariffChange(item: AuditLogItem) {
  const detail = item.change_detail;
  if (!detail) return null;
  const before = detail.old ?? null;
  const after = detail.new ?? null;
  const parts: string[] = [];
  if (!before) {
    parts.push("初始配置");
    if (after?.unit_credits)
      parts.push(`售价 ${compactAmount(after.unit_credits)} 积分`);
    if (after?.unit_cost_fen)
      parts.push(`成本 ${fenAmount(after.unit_cost_fen)}`);
  } else {
    if ((before.unit_credits ?? null) !== (after?.unit_credits ?? null))
      parts.push(
        `售价 ${compactAmount(before.unit_credits)} → ${compactAmount(after?.unit_credits)} 积分`,
      );
    if ((before.unit_cost_fen ?? null) !== (after?.unit_cost_fen ?? null))
      parts.push(
        `成本 ${fenAmount(before.unit_cost_fen)} → ${fenAmount(after?.unit_cost_fen)}`,
      );
    if (Boolean(before.enabled) !== Boolean(after?.enabled))
      parts.push(after?.enabled ? "启用用户扣费" : "停用用户扣费");
  }
  if (parts.length === 0) return "未变更";
  const label = subjectLabel(item.change_subject);
  const prefix = label ? `${label}：` : "";
  return `${prefix}${parts.join(" · ")}`;
}

function priceChange(item: AuditLogItem) {
  if (item.change_summary) return item.change_summary;
  if (item.event_type === "viral_runtime.update") {
    const changes = item.change_detail?.changes;
    if (!changes) return "历史记录未保存变更值";
    const labels: Record<string, string> = {
      collection_enabled: "定时采集",
      import_enabled: "客户链接导入",
      keywords: "关键词",
      per_keyword_limit: "默认每词条数",
      collection_interval_days: "采集间隔（天）",
      collection_time: "上海执行时刻",
      quality_min_likes: "最低点赞",
      quality_duration_min_ms: "最短时长",
      quality_duration_max_ms: "最长时长",
      quality_exclude_words: "排除词",
      monthly_budget_fen: "月度预算",
    };
    const valueLabel = (field: string, value: unknown): string => {
      if (value == null) return field === "collection_time" ? "未指定" : "不限";
      if (field === "monthly_budget_fen" && typeof value === "number")
        return fenAmount(value);
      if (field.endsWith("_ms") && typeof value === "number")
        return `${value / 1000} 秒`;
      if (field.endsWith("_enabled")) return value ? "开启" : "暂停";
      if (field === "keywords" && Array.isArray(value))
        return `${value.length} 个`;
      if (Array.isArray(value)) return value.map(String).join("、") || "不排除";
      return String(value);
    };
    return (
      Object.entries(changes)
        .filter(([field]) => field in labels)
        .map(
          ([field, change]) =>
            `${labels[field]}：${valueLabel(field, change.before)} → ${valueLabel(field, change.after)}`,
        )
        .join(" · ") || "未变更"
    );
  }
  if (item.event_type === "billing.tariff.update") {
    const change = tariffChange(item);
    if (change) return change;
  }
  const oldPrice = item.old_unit_price_fen;
  const newPrice = item.new_unit_price_fen;
  const unit = priceUnit(item);
  if (typeof oldPrice === "number" && typeof newPrice === "number") {
    return `${fenAmount(oldPrice)} → ${fenAmount(newPrice)} ${unit}`;
  }
  if (typeof newPrice === "number")
    return `设置为 ${fenAmount(newPrice)} ${unit}`;
  if (
    item.event_type === "customer_unit_price.reset" &&
    typeof oldPrice === "number"
  ) {
    return `恢复默认（原 ${fenAmount(oldPrice)} ${unit}）`;
  }
  if (item.change_subject) return "历史记录未保存变更值";
  return "—";
}

/**
 * T34 / ADM-02 — audit event log view.
 *
 * Lists the aggregated audit events (currently ADMIN_ADJUSTMENT rows from
 * the 039 ledger) with pagination and an actor filter. Both admin and
 * auditor roles read the same view (read-only).
 *
 * 筛选只在提交时生效：输入框改动不触发请求（整改清单 评估登记 5 的
 * "输入即加载 + 点击再发一次"重复请求问题在此收口）。
 */
// 页大小是常量，不是状态：原写法把它放进 useState 却从不改它，等于把常量
// 伪装成状态（2026-09-12 评审 P3 的「伪状态反模式」）。
const PAGE_SIZE = 20;

export function AuditEventsPage({ readOnly = false }: { readOnly?: boolean }) {
  const [items, setItems] = useState<AuditLogItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [actorDraft, setActorDraft] = useState("");
  const [targetDraft, setTargetDraft] = useState("");
  const [groupDraft, setGroupDraft] = useState("");
  const [typeDraft, setTypeDraft] = useState("");
  const [scopeDraft, setScopeDraft] = useState<AuditScope>("admin");
  const [fromDraft, setFromDraft] = useState("");
  const [toDraft, setToDraft] = useState("");
  const [detail, setDetail] = useState<AuditLogItem | null>(null);
  const [filters, setFilters] = useState<{
    scope: AuditScope;
    actor: string;
    target: string;
    eventGroup: string;
    eventType: string;
    from: string;
    to: string;
  }>({
    scope: "admin",
    actor: "",
    target: "",
    eventGroup: "",
    eventType: "",
    from: "",
    to: "",
  });

  const request = useRef(0);
  const loadLog = useCallback(async () => {
    const id = ++request.current;
    try {
      setLoading(true);
      setError("");
      const response = await listAuditLog({
        scope: filters.scope,
        actorUsername: filters.actor || undefined,
        targetUsername: filters.target || undefined,
        eventGroup: filters.eventGroup || undefined,
        eventType: filters.eventType || undefined,
        createdFrom: filters.from || undefined,
        createdTo: filters.to || undefined,
        limit: PAGE_SIZE,
        offset,
      });
      if (id !== request.current) return;
      setItems(response.items);
      setTotal(response.total);
    } catch (cause) {
      if (id !== request.current) return;
      setError(
        cause instanceof Error && cause.message
          ? `加载失败：${cause.message}`
          : "加载失败：未知错误",
      );
    } finally {
      if (id === request.current) setLoading(false);
    }
  }, [filters, offset]);

  useEffect(() => {
    setDetail(null);
    void loadLog();
    return () => {
      request.current += 1;
    };
  }, [loadLog]);

  function handleFilter(event: FormEvent) {
    event.preventDefault();
    // offset 归零与筛选词提交合入同一批次，effect 只会跑一次。
    setOffset(0);
    setFilters({
      scope: scopeDraft,
      actor: actorDraft.trim(),
      target: targetDraft.trim(),
      eventGroup: groupDraft,
      eventType: typeDraft.trim(),
      from: fromDraft.trim(),
      to: toDraft.trim(),
    });
  }

  function handleReset() {
    setActorDraft("");
    setTargetDraft("");
    setGroupDraft("");
    setTypeDraft("");
    setScopeDraft("admin");
    setFromDraft("");
    setToDraft("");
    setOffset(0);
    setFilters({
      scope: "admin",
      actor: "",
      target: "",
      eventGroup: "",
      eventType: "",
      from: "",
      to: "",
    });
  }

  async function exportCsv() {
    try {
      await downloadAuditLogCsv({
        scope: filters.scope,
        actorUsername: filters.actor || undefined,
        targetUsername: filters.target || undefined,
        eventGroup: filters.eventGroup || undefined,
        eventType: filters.eventType || undefined,
        createdFrom: filters.from || undefined,
        createdTo: filters.to || undefined,
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "导出失败");
    }
  }

  return (
    <section aria-label="审计事件" className="admin-panel">
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}

      <form
        className="admin-form admin-filter-grid audit-filter-grid"
        onSubmit={handleFilter}
      >
        <label>
          范围
          <select
            aria-label="审计范围"
            value={scopeDraft}
            onChange={(event) =>
              setScopeDraft(asAuditScope(event.target.value))
            }
          >
            <option value="admin">管理员操作</option>
            <option value="customer">客户操作</option>
            <option value="all">全部动作</option>
          </select>
        </label>
        <label>
          事件分组
          <select
            aria-label="事件分组"
            value={groupDraft}
            onChange={(event) => {
              // 切组后事件下拉只留该组事件；已选事件不属于新组时一并清空。
              const nextGroup = event.target.value;
              setGroupDraft(nextGroup);
              const members = EVENT_GROUP_MEMBERS[nextGroup];
              if (members && typeDraft && !members.has(typeDraft)) {
                setTypeDraft("");
              }
            }}
          >
            {EVENT_GROUP_OPTIONS.map(([value, label]) => (
              <option key={value || "all"} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          事件类型
          <select
            aria-label="事件类型"
            value={typeDraft}
            onChange={(event) => setTypeDraft(event.target.value)}
          >
            {EVENT_OPTIONS.filter(([value]) => {
              const members = EVENT_GROUP_MEMBERS[groupDraft];
              return !value || !members || members.has(value);
            }).map(([value, label]) => (
              <option key={value || "all"} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          操作人用户名
          <input
            aria-label="操作人用户名"
            placeholder="例如：admin_u"
            value={actorDraft}
            onChange={(event) => setActorDraft(event.target.value)}
          />
        </label>
        <label>
          目标客户用户名
          <input
            aria-label="目标客户用户名"
            placeholder="例如：customer_u"
            value={targetDraft}
            onChange={(event) => setTargetDraft(event.target.value)}
          />
        </label>
        <label>
          起始时间
          <input
            type="date"
            value={fromDraft}
            onChange={(event) => setFromDraft(event.target.value)}
          />
        </label>
        <label>
          截止时间
          <input
            type="date"
            value={toDraft}
            onChange={(event) => setToDraft(event.target.value)}
          />
        </label>
        <div className="audit-filter-actions">
          <button disabled={loading} type="submit">
            {loading ? "加载中…" : "筛选"}
          </button>
          <button
            className="secondary-button"
            disabled={loading}
            type="button"
            onClick={handleReset}
          >
            重置
          </button>
          {/* 方案 P1：审计可导出，与列表同筛选口径（服务端 control.export 审计）。
              整表导出是数据出境动作，服务端只放行写级角色；只读角色（auditor）
              不渲染入口，免得点了才收到 403。 */}
          {readOnly ? null : (
            <button
              className="secondary-button"
              disabled={loading}
              type="button"
              onClick={() => void exportCsv()}
            >
              导出 CSV
            </button>
          )}
        </div>
      </form>

      {items.length === 0 && !loading ? (
        <PageBanner tone="notice">暂无审计事件。</PageBanner>
      ) : (
        <DataTable
          ariaLabel="审计事件列表"
          headers={
            <>
              <th>时间</th>
              <th>操作人</th>
              <th>分组与事件</th>
              <th>操作对象</th>
              <th>变更摘要</th>
              <th>原因</th>
              <th>详情</th>
            </>
          }
        >
          {items.map((item) => (
            <tr
              key={item.event_id}
              className={item.sensitive ? "audit-row--sensitive" : undefined}
              onClick={() => setDetail(item)}
            >
              <td>{formatDateTime(item.created_at)}</td>
              <td>{item.actor_username || "系统"}</td>
              <td>
                <small>{auditGroupLabel(item)}</small>
                <span
                  className={`audit-event-badge is-${item.sensitive ? "danger" : eventTone(item.event_type)}`}
                >
                  {auditEventLabel(item)}
                  {item.sensitive ? " ·高敏" : ""}
                </span>
              </td>
              <td>
                {item.target_label ||
                  item.target_company_name ||
                  item.target_username ||
                  subjectLabel(item.change_subject) ||
                  "业务配置项"}
              </td>
              <td className="audit-change-detail">{priceChange(item)}</td>
              <td>{item.reason || "—"}</td>
              <td>
                <button
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation();
                    setDetail(item);
                  }}
                >
                  查看详情
                </button>
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

      {detail && (
        <aside className="audit-detail-drawer" aria-label="审计事件详情">
          <button
            type="button"
            className="secondary-button"
            onClick={() => setDetail(null)}
          >
            关闭详情
          </button>
          <h3>
            {auditEventLabel(detail)}
            {detail.sensitive ? "（高敏）" : ""}
          </h3>
          <dl>
            <dt>时间</dt>
            <dd>{formatDateTime(detail.created_at)}</dd>
            <dt>操作人</dt>
            <dd>{detail.actor_username || detail.actor_user_id || "系统"}</dd>
            <dt>目标客户</dt>
            <dd>
              {detail.target_label || detail.target_username || "业务配置项"}
              <small>
                {detail.target_user_id ? `（${detail.target_user_id}）` : ""}
              </small>
            </dd>
            <dt>来源单</dt>
            <dd>
              {detail.source_document_type} /{" "}
              {detail.source_document_ref || "—"}
            </dd>
            <dt>变更明细</dt>
            <dd>{priceChange(detail)}</dd>
            <dt>原因</dt>
            <dd>{detail.reason || "—"}</dd>
            {detail.event_type === "viral_runtime.update" ? (
              <>
                <dt>变更内容</dt>
                <dd>{priceChange(detail)}</dd>
              </>
            ) : null}
            <dt>技术事件名</dt>
            <dd>
              <code>{detail.event_type}</code>
            </dd>
            <dt>请求编号</dt>
            <dd>
              <code>{detail.request_id || "—"}</code>
            </dd>
          </dl>
        </aside>
      )}
    </section>
  );
}
