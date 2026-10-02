import { useCallback, useEffect, useRef, useState } from "react";

import { type AuditLogItem, listAuditLog } from "../api.admin";
import { auditEventLabel } from "./auditVocabulary";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { formatDateTime } from "./ui/vocabulary";

// 同一稳定客户编号归集日常行为与管理员处置，沿用服务端合并审计的分页口径。
const ACTIVITY_PAGE_SIZE = 20;

export function CustomerActivitySection({ userId }: { userId: string }) {
  const [items, setItems] = useState<AuditLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const requestIdRef = useRef(0);

  const load = useCallback(async () => {
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    try {
      setLoading(true);
      setError("");
      const response = await listAuditLog({
        scope: "all",
        targetUserId: userId,
        limit: ACTIVITY_PAGE_SIZE,
        offset,
      });
      if (requestId !== requestIdRef.current) return;
      setItems(response.items);
      setTotal(response.total);
    } catch (cause) {
      if (requestId !== requestIdRef.current) return;
      setError(
        cause instanceof Error && cause.message
          ? `加载操作记录失败：${cause.message}`
          : "加载操作记录失败：未知错误",
      );
    } finally {
      if (requestId === requestIdRef.current) {
        setLoading(false);
      }
    }
  }, [userId, offset]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section
      aria-label="客户操作记录"
      className="admin-panel customer-activity"
    >
      <header className="customer-activity__header">
        <h2>操作记录</h2>
        <small>{loading ? "加载中…" : `共 ${total} 条`}</small>
      </header>
      <p className="admin-hint">
        合并该客户的工作台行为与管理员处置，包含暂停、恢复、调账、退款扣减、设备和定价变更；仅显示已经记录的事实。
      </p>

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}

      {!loading && items.length === 0 && !error ? (
        <p className="admin-hint">该客户暂无操作记录。</p>
      ) : null}

      {items.length > 0 ? (
        <div className="admin-table-scroll">
          <table aria-label="客户操作记录列表" className="admin-data-table">
            <thead>
              <tr>
                <th scope="col">时间</th>
                <th scope="col">动作</th>
                <th scope="col">操作人</th>
                <th scope="col">操作对象</th>
                <th scope="col">原因</th>
                <th scope="col">变更</th>
                <th scope="col">详情</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.event_id}>
                  <td>{formatDateTime(item.created_at)}</td>
                  <td>{auditEventLabel(item)}</td>
                  <td>{item.actor_username || "系统"}</td>
                  <td>
                    {item.target_label ||
                      item.target_company_name ||
                      item.target_username ||
                      "该客户"}
                  </td>
                  <td>{item.reason || "—"}</td>
                  <td>{item.change_summary ?? activityChange(item)}</td>
                  <td>
                    <details>
                      <summary>技术详情</summary>
                      <p>事件：{item.event_type}</p>
                      <p>
                        来源单据：{item.source_document_type} /{" "}
                        {item.source_document_ref || "—"}
                      </p>
                      <p>请求编号：{item.request_id || "—"}</p>
                    </details>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      <Pagination
        disabled={loading}
        limit={ACTIVITY_PAGE_SIZE}
        offset={offset}
        total={total}
        onPageChange={setOffset}
      />
    </section>
  );
}

type PriceState = {
  mode: "DEFAULT" | "CUSTOM";
  effective_unit_price_fen: number | null;
};

function priceState(value: unknown): PriceState | null {
  if (!value || typeof value !== "object") return null;
  const state = value as Record<string, unknown>;
  if (state.mode !== "DEFAULT" && state.mode !== "CUSTOM") return null;
  if (
    state.effective_unit_price_fen !== null &&
    (typeof state.effective_unit_price_fen !== "number" ||
      !Number.isFinite(state.effective_unit_price_fen))
  )
    return null;
  return state as PriceState;
}

function priceLabel(state: PriceState): string {
  const amount =
    state.effective_unit_price_fen == null
      ? "历史金额未记录"
      : `¥${(state.effective_unit_price_fen / 100).toFixed(2)}`;
  return state.mode === "DEFAULT"
    ? `未配置自定义价（继承默认，${amount}）`
    : `自定义价 ${amount}`;
}

function activityChange(item: AuditLogItem): string {
  const credits = item.change_detail?.changes?.available_credits;
  if (typeof credits?.before === "number" && typeof credits.after === "number")
    return `${credits.before} → ${credits.after} 积分`;
  const prices = item.change_detail?.changes?.customer_unit_price;
  const before = priceState(prices?.before);
  const after = priceState(prices?.after);
  if (before && after) return `${priceLabel(before)} → ${priceLabel(after)}`;
  if (item.old_unit_price_fen != null && item.new_unit_price_fen != null)
    return `${item.old_unit_price_fen / 100} → ${item.new_unit_price_fen / 100} 元`;
  return "未记录前后值";
}
