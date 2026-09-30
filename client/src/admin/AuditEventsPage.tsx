import { type FormEvent, useCallback, useEffect, useState } from "react";

import {
  type AuditLogItem,
  downloadAuditLogCsv,
  listAuditLog,
} from "../api.admin";
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
  ["secret_export", "密钥与导出"],
  ["login", "登录"],
] as const;

/** 分组包含的事件类型；与服务端 EVENT_GROUPS 同口径，用于过滤事件下拉。 */
const EVENT_GROUP_MEMBERS: Record<string, ReadonlySet<string>> = {
  funds: new Set(["ADMIN_ADJUSTMENT", "payment.sync"]),
  pricing: new Set([
    "operation_rate.update",
    "billing.tariff.update",
    "customer_pricing.update",
    "customer_unit_price.update",
    "customer_unit_price.reset",
    "recharge_package.create",
    "recharge_package.update",
    "customer_discount.create",
    "customer_discount.deactivate",
    "customer_package.grant",
  ]),
  account: new Set([
    "ADMIN_DEVICE_DISABLE",
    "ADMIN_DEVICE_UNBIND",
    "ADMIN_SESSION_LOGOUT",
    "h3.account.update",
    "admin.activation_code_batch.created",
    "ACTIVATION_CODE_ISSUED",
    "ACTIVATION_CODE_ARCHIVED",
    "ACTIVATION_CODE_DELIVERED",
  ]),
  system: new Set([
    "provider_settings.update",
    "provider_settings.paid_test",
    "runtime_settings.update",
    "payment.provider.update",
    "payment.wechat.update",
    "control.reconciliation.read",
  ]),
  secret_export: new Set([
    "provider_settings.secret_reveal",
    "admin.activation_code.revealed",
    "admin.activation_code.revealed_replay",
    "external_call.response_view",
    "control.export",
  ]),
  login: new Set(["admin_session.password_login", "admin_session.exchange"]),
};

/** 计费科目 key → 业务名（方案 P1：科目不再直出英文 key）。 */
const SUBJECT_LABELS: Record<string, string> = {
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
  return SUBJECT_LABELS[subject] ?? subject;
}

const EVENT_OPTIONS = [
  ["", "全部事件"],
  ["ADMIN_ADJUSTMENT", "管理员调账"],
  ["operation_rate.update", "费率调整"],
  ["billing.tariff.update", "API 成本与售价调整"],
  ["customer_pricing.update", "客户报价调整"],
  ["h3.account.update", "视频账号配置"],
  ["payment.provider.update", "默认支付方式调整"],
  ["payment.wechat.update", "微信商户配置"],
  ["customer_unit_price.update", "客户单价调整"],
  ["customer_unit_price.reset", "客户单价恢复默认"],
  ["runtime_settings.update", "运行参数调整"],
  // 激活码类是 audit_logs 里的 admin.activation_code.* 动作（archive/reveal
  // 走 audit_logs，suspend/resume/revoke 走 activation_code_events）。
  // 此前这里只有标签映射里的一个 CODE_REVEAL 分支，而后端从不产生该值，
  // 运营既选不到、reveal 行也只能显示成"系统操作"。
  ["admin.activation_code.revealed", "查看激活码明文"],
  ["admin.activation_code.revealed_replay", "查看激活码明文（幂等重放）"],
  ["admin.activation_code.archived", "归档激活码"],
  ["admin.activation_code_batch.created", "创建激活码批次"],
  // 管理员下线：customer_session_events 中 actor 非会话属主的行。
  ["ADMIN_SESSION_LOGOUT", "管理员下线"],
  // 方案 P0-4：服务端早已写这些 audit_logs 动作，但下拉里没有，运营筛不出来——
  // 其中查看密钥、数据导出、线下开通套餐都是高敏操作。
  ["admin_session.password_login", "管理员密码登录"],
  ["admin_session.exchange", "恢复凭据登录"],
  ["provider_settings.secret_reveal", "查看密钥明文"],
  ["provider_settings.update", "服务配置修改"],
  ["provider_settings.paid_test", "付费连接测试"],
  ["customer_package.grant", "开通套餐（线下收款）"],
  ["customer_discount.create", "设置专项折扣"],
  ["customer_discount.deactivate", "停用专项折扣"],
  ["recharge_package.create", "新建充值套餐"],
  ["recharge_package.update", "修改充值套餐"],
  ["payment.sync", "查单同步"],
  ["control.export", "数据导出"],
  // P0-9：记录详情里「查看原始响应」的高敏动作，每次读取都写审计；
  // 之前只能靠族回退标签显示成“系统操作”，运营筛不出来。
  ["external_call.response_view", "查看接口原始响应"],
] as const;

const EVENT_LABELS = new Map<string, string>(EVENT_OPTIONS);

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

function eventLabel(eventType: string) {
  if (EVENT_LABELS.has(eventType))
    return EVENT_LABELS.get(eventType) ?? "系统操作";
  if (eventType.startsWith("ADMIN_DEVICE_")) return "设备管理";
  if (eventType.startsWith("ADMIN_SESSION_")) return "管理员会话操作";
  if (eventType.startsWith("ACTIVATION_CODE_")) return "激活码操作";
  if (eventType.startsWith("admin.activation_code")) return "激活码操作";
  if (eventType.includes("reconciliation")) return "对账查询";
  return "系统操作";
}

function eventTone(eventType: string) {
  if (SENSITIVE_EVENTS.has(eventType)) return "danger";
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

export function AuditEventsPage() {
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

  const loadLog = useCallback(async () => {
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
  }, [filters, offset]);

  useEffect(() => {
    void loadLog();
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
          {/* 方案 P1：审计可导出，与列表同筛选口径（服务端 control.export 审计）。 */}
          <button
            className="secondary-button"
            disabled={loading}
            type="button"
            onClick={() => void exportCsv()}
          >
            导出 CSV
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
            <tr
              key={item.event_id}
              className={item.sensitive ? "audit-row--sensitive" : undefined}
              onClick={() => setDetail(item)}
            >
              <td>{formatDateTime(item.created_at)}</td>
              <td>
                <span
                  className={`audit-event-badge is-${eventTone(item.event_type)}`}
                  title={item.event_type}
                >
                  {eventLabel(item.event_type)}
                  {item.sensitive ? " ·高敏" : ""}
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
            {eventLabel(detail.event_type)}
            {detail.sensitive ? "（高敏）" : ""}
          </h3>
          <dl>
            <dt>时间</dt>
            <dd>{formatDateTime(detail.created_at)}</dd>
            <dt>操作人</dt>
            <dd>{detail.actor_username || detail.actor_user_id || "系统"}</dd>
            <dt>目标客户</dt>
            <dd>
              {detail.target_username || "—"}
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
