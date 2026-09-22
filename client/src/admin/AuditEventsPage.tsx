import { type FormEvent, useCallback, useEffect, useState } from "react";

import { type AuditLogItem, listAuditLog } from "../api.admin";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { formatDateTime } from "./ui/vocabulary";
import "./admin-audit.css";

const EVENT_OPTIONS = [
  ["", "全部事件"],
  ["ADMIN_ADJUSTMENT", "管理员调账"],
  ["operation_rate.update", "费率调整"],
  ["billing.tariff.update", "API 成本与售价调整"],
  ["customer_pricing.update", "客户报价调整"],
  ["h3.account.update", "视频账号配置"],
  ["payment.provider.update", "默认支付通道调整"],
  ["payment.wechat.update", "微信商户配置"],
  ["customer_unit_price.update", "客户单价调整"],
  ["customer_unit_price.reset", "客户单价恢复默认"],
  ["runtime_settings.update", "运行参数调整"],
] as const;

const EVENT_LABELS = new Map<string, string>(EVENT_OPTIONS);

function eventLabel(eventType: string) {
  if (EVENT_LABELS.has(eventType))
    return EVENT_LABELS.get(eventType) ?? "系统操作";
  if (eventType.startsWith("ADMIN_DEVICE_")) return "设备管理";
  if (eventType.startsWith("ACTIVATION_CODE_")) return "激活码操作";
  if (eventType === "CODE_REVEAL") return "查看激活码明文";
  if (eventType.includes("reconciliation")) return "对账查询";
  return "系统操作";
}

function eventTone(eventType: string) {
  if (/REVOK|DENIED|FAILED|FORCE/.test(eventType)) return "danger";
  if (eventType.includes("rate") || eventType.includes("price"))
    return "warning";
  if (eventType.includes("DEVICE") || eventType.includes("SESSION"))
    return "info";
  return "success";
}

function compactText(value: string, maxLength = 20) {
  if (!value) return "—";
  if (value.length <= maxLength) return value;
  return `${value.slice(0, 10)}…${value.slice(-6)}`;
}

function priceUnit(item: AuditLogItem) {
  if (item.event_type.startsWith("customer_unit_price.")) return "分/秒";
  switch (item.change_subject) {
    case "video_generation_768p":
    case "video_generation_2k":
    case "video_analysis_768p":
    case "video_analysis_2k":
    case "external_price_768p":
    case "external_price_2k":
      return "分/秒";
    case "first_frame_image":
    case "character_sheet_image":
    case "image_generation":
    case "character_sheet":
    case "character_view":
      return "分/张";
    case "context_ir":
      return "分/次";
    default:
      return "分";
  }
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
      parts.push(`成本 ${compactAmount(after.unit_cost_fen)} 分`);
  } else {
    if ((before.unit_credits ?? null) !== (after?.unit_credits ?? null))
      parts.push(
        `售价 ${compactAmount(before.unit_credits)} → ${compactAmount(after?.unit_credits)} 积分`,
      );
    if ((before.unit_cost_fen ?? null) !== (after?.unit_cost_fen ?? null))
      parts.push(
        `成本 ${compactAmount(before.unit_cost_fen)} → ${compactAmount(after?.unit_cost_fen)} 分`,
      );
    if (Boolean(before.enabled) !== Boolean(after?.enabled))
      parts.push(after?.enabled ? "启用用户扣费" : "停用用户扣费");
  }
  if (parts.length === 0) return "未变更";
  const prefix = item.change_subject ? `${item.change_subject}：` : "";
  return `${prefix}${parts.join(" · ")}`;
}

function priceChange(item: AuditLogItem) {
  if (item.event_type === "billing.tariff.update") {
    const change = tariffChange(item);
    if (change) return change;
  }
  const oldPrice = item.old_unit_price_fen;
  const newPrice = item.new_unit_price_fen;
  const unit = priceUnit(item);
  if (typeof oldPrice === "number" && typeof newPrice === "number") {
    return `${oldPrice} → ${newPrice} ${unit}`;
  }
  if (typeof newPrice === "number") return `设置为 ${newPrice} ${unit}`;
  if (
    item.event_type === "customer_unit_price.reset" &&
    typeof oldPrice === "number"
  ) {
    return `恢复默认（原 ${oldPrice} ${unit}）`;
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
export function AuditEventsPage() {
  const [items, setItems] = useState<AuditLogItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [pageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [actorDraft, setActorDraft] = useState("");
  const [targetDraft, setTargetDraft] = useState("");
  const [typeDraft, setTypeDraft] = useState("");
  const [fromDraft, setFromDraft] = useState("");
  const [toDraft, setToDraft] = useState("");
  const [filters, setFilters] = useState<{
    actor: string;
    target: string;
    eventType: string;
    from: string;
    to: string;
  }>({ actor: "", target: "", eventType: "", from: "", to: "" });

  const loadLog = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = await listAuditLog({
        actorUsername: filters.actor || undefined,
        targetUsername: filters.target || undefined,
        eventType: filters.eventType || undefined,
        createdFrom: filters.from || undefined,
        createdTo: filters.to || undefined,
        limit: pageSize,
        offset,
      });
      setItems(response.items);
      setTotal(response.total);
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? `加载失败：${cause.message}`
          : "加载失败：未知错误",
      );
    } finally {
      setLoading(false);
    }
  }, [filters, offset, pageSize]);

  useEffect(() => {
    void loadLog();
  }, [loadLog]);

  function handleFilter(event: FormEvent) {
    event.preventDefault();
    // offset 归零与筛选词提交合入同一批次，effect 只会跑一次。
    setOffset(0);
    setFilters({
      actor: actorDraft.trim(),
      target: targetDraft.trim(),
      eventType: typeDraft.trim(),
      from: fromDraft.trim(),
      to: toDraft.trim(),
    });
  }

  function handleReset() {
    setActorDraft("");
    setTargetDraft("");
    setTypeDraft("");
    setFromDraft("");
    setToDraft("");
    setOffset(0);
    setFilters({ actor: "", target: "", eventType: "", from: "", to: "" });
  }

  return (
    <section aria-label="审计事件" className="admin-panel">
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}

      <form
        className="admin-form admin-filter-grid audit-filter-grid"
        onSubmit={handleFilter}
      >
        <label>
          事件类型
          <select
            aria-label="事件类型"
            value={typeDraft}
            onChange={(event) => setTypeDraft(event.target.value)}
          >
            {EVENT_OPTIONS.map(([value, label]) => (
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
              <th>类型</th>
              <th>操作人</th>
              <th>目标客户</th>
              <th>变更明细</th>
              <th>来源单</th>
              <th>原因</th>
              <th>request id</th>
            </>
          }
        >
          {items.map((item) => (
            <tr key={item.event_id}>
              <td>{formatDateTime(item.created_at)}</td>
              <td>
                <span
                  className={`audit-event-badge is-${eventTone(item.event_type)}`}
                  title={item.event_type}
                >
                  {eventLabel(item.event_type)}
                </span>
              </td>
              <td>{item.actor_username || item.actor_user_id}</td>
              <td>
                {item.target_username || "—"}
                <br />
                <small>{item.target_user_id}</small>
              </td>
              <td className="audit-change-detail">{priceChange(item)}</td>
              <td>
                <span
                  title={`${item.source_document_type} / ${item.source_document_ref}`}
                >
                  {compactText(
                    `${item.source_document_type} / ${item.source_document_ref}`,
                  )}
                </span>
              </td>
              <td>{item.reason}</td>
              <td>
                <span title={item.request_id}>
                  {compactText(item.request_id)}
                </span>
              </td>
            </tr>
          ))}
        </DataTable>
      )}

      <Pagination
        disabled={loading}
        limit={pageSize}
        offset={offset}
        total={total}
        onPageChange={setOffset}
      />
    </section>
  );
}
