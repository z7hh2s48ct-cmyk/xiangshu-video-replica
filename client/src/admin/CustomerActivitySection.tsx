import { useCallback, useEffect, useRef, useState } from "react";

import { type AuditLogItem, listAuditLog } from "../api.admin";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { formatDateTime } from "./ui/vocabulary";

// 方案 P0-3：客户详情「操作记录」——查看这位客户自己在工作台的日常动作
// （建项目、读素材等）。这些行一直写在 audit_logs 里，但审计页默认
// scope=admin 看不到；服务端 docstring 承诺的入口（scope=customer +
// target_user_id）就在这里补上。管理员对该客户的处置仍在审计页查看。
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
        scope: "customer",
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
        该客户在工作台的日常动作（建项目、读素材等）。管理员对该客户的处置请到
        「审计事件」页查看。
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
                <th scope="col">来源单据</th>
                <th scope="col">原因</th>
                <th scope="col">问题编号</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.event_id}>
                  <td>{formatDateTime(item.created_at)}</td>
                  <td>
                    {/* 客户动作没有逐项中文词典（族回退会把它们全说成“系统
                        操作”），这里直接给原始动作名 + 悬浮完整值。 */}
                    <span title={item.event_type}>{item.event_type}</span>
                  </td>
                  <td>
                    {item.source_document_ref ? (
                      <span
                        title={`${item.source_document_type} / ${item.source_document_ref}`}
                      >
                        {item.source_document_ref}
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>{item.reason || "—"}</td>
                  <td>
                    {item.request_id ? (
                      <span title={item.request_id}>
                        {compactRef(item.request_id)}
                      </span>
                    ) : (
                      "—"
                    )}
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

function compactRef(value: string): string {
  return value.length <= 24
    ? value
    : `${value.slice(0, 12)}…${value.slice(-6)}`;
}
