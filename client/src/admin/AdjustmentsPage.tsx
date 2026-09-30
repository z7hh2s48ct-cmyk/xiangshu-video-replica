import { useCallback, useEffect, useState } from "react";
import { downloadAdjustmentsCsv } from "../api";
import {
  type AdjustmentListItem,
  listAdminAdjustments,
  listAllAdminAdjustments,
} from "../api.admin";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { StatusBadge } from "./ui/StatusBadge";
import {
  ADJUSTMENT_SOURCE_LABELS,
  formatDateTime,
  formatFen,
  labelFrom,
  signedCredits,
} from "./ui/vocabulary";

/**
 * T33 — adjustment history for a specific customer.
 *
 * The page shows all admin adjustments (recharge orders) for a target user,
 * including source document type, reason, amount, credits, and timestamps.
 * All roles (admin/auditor) see the same read-only view (ADM-02).
 *
 * This page is typically accessed from the customer detail view; the userId
 * prop is passed by the parent component.
 */
// 页大小是常量，不是状态：原写法把它放进 useState 却从不改它，等于把常量
// 伪装成状态（2026-09-12 评审 P3 的「伪状态反模式」）。
const PAGE_SIZE = 20;

type Filters = {
  actorUsername: string;
  targetUsername: string;
  sourceType: string;
  createdFrom: string;
  createdTo: string;
};

const emptyFilters: Filters = {
  actorUsername: "",
  targetUsername: "",
  sourceType: "",
  createdFrom: "",
  createdTo: "",
};

export function AdjustmentsPage({
  userId,
  readOnly = false,
}: {
  userId?: string;
  readOnly?: boolean;
}) {
  const [adjustments, setAdjustments] = useState<AdjustmentListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  // 输入草稿与已提交筛选分开：点「查询」才发请求（方案 P1：逐键请求既刷屏
  // 也浪费配额），分页翻页则沿用已提交的筛选。
  const [draft, setDraft] = useState<Filters>(emptyFilters);
  const [filters, setFilters] = useState<Filters>(emptyFilters);

  const loadAdjustments = useCallback(async () => {
    try {
      setLoading(true);
      setError("");
      const response = userId
        ? await listAdminAdjustments(userId, { limit: PAGE_SIZE, offset })
        : await listAllAdminAdjustments({
            actorUsername: filters.actorUsername || undefined,
            targetUsername: filters.targetUsername || undefined,
            sourceDocumentType: filters.sourceType || undefined,
            createdFrom: filters.createdFrom || undefined,
            createdTo: filters.createdTo || undefined,
            limit: PAGE_SIZE,
            offset,
          });
      setAdjustments(response.items);
      setTotal(response.total);
    } catch (err) {
      setError(
        err instanceof Error && err.message
          ? `加载失败：${err.message}`
          : "加载失败：未知错误",
      );
    } finally {
      setLoading(false);
    }
  }, [filters, offset, userId]);

  useEffect(() => {
    loadAdjustments();
  }, [loadAdjustments]);

  async function exportCsv() {
    try {
      setNotice("");
      setError("");
      await downloadAdjustmentsCsv({
        actorUsername: filters.actorUsername || undefined,
        targetUsername: filters.targetUsername || undefined,
        sourceDocumentType: filters.sourceType || undefined,
        createdFrom: filters.createdFrom || undefined,
        createdTo: filters.createdTo || undefined,
      });
      setNotice("已导出当前筛选的人工调整 CSV。");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "导出失败");
    }
  }

  return (
    <div className="adjustments-page">
      <header>
        {userId ? <p className="user-info">用户 ID: {userId}</p> : null}
      </header>
      {!userId ? (
        <form
          className="admin-toolbar"
          onSubmit={(event) => {
            event.preventDefault();
            setOffset(0);
            setFilters(draft);
          }}
        >
          <label>
            操作人
            <input
              aria-label="调账操作人"
              value={draft.actorUsername}
              onChange={(event) =>
                setDraft({ ...draft, actorUsername: event.target.value })
              }
            />
          </label>
          <label>
            目标客户
            <input
              aria-label="调账目标客户"
              value={draft.targetUsername}
              onChange={(event) =>
                setDraft({ ...draft, targetUsername: event.target.value })
              }
            />
          </label>
          <label>
            来源类型
            <select
              aria-label="调账来源类型"
              value={draft.sourceType}
              onChange={(event) =>
                setDraft({ ...draft, sourceType: event.target.value })
              }
            >
              <option value="">全部来源</option>
              <option value="CS_TICKET">客服工单</option>
              <option value="REFUND_APPROVAL">退款审批</option>
              <option value="COMPENSATION_APPROVAL">补偿审批</option>
              <option value="LEDGER_CORRECTION">账本修正</option>
              <option value="FREE_GRANT">积分赠送</option>
              <option value="CREDIT_COMPENSATION">积分补偿</option>
              <option value="OFFLINE_PAYMENT">线下收款开通套餐</option>
            </select>
          </label>
          <label>
            开始日期
            <input
              type="date"
              aria-label="调账开始日期"
              value={draft.createdFrom}
              onChange={(event) =>
                setDraft({ ...draft, createdFrom: event.target.value })
              }
            />
          </label>
          <label>
            结束日期
            <input
              type="date"
              aria-label="调账结束日期"
              value={draft.createdTo}
              onChange={(event) =>
                setDraft({ ...draft, createdTo: event.target.value })
              }
            />
          </label>
          <button type="submit">查询</button>
          {/* 整表导出是写级动作（服务端 AdminWriter）：只读角色不渲染入口。 */}
          {readOnly ? null : (
            <button type="button" onClick={() => void exportCsv()}>
              导出 CSV
            </button>
          )}
        </form>
      ) : null}

      {loading && <div className="loading">加载中...</div>}

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

      {!loading && !error && adjustments.length === 0 && (
        <div className="empty-state">暂无调账记录</div>
      )}

      {!loading && !error && adjustments.length > 0 && (
        <>
          <DataTable
            ariaLabel="调账历史列表"
            headers={
              <>
                <th>来源单类型</th>
                {!userId ? <th>操作人 / 客户</th> : null}
                <th>来源单编号</th>
                <th>原因</th>
                <th>金额</th>
                <th>积分</th>
                <th>调整前后余额</th>
                <th>时间</th>
              </>
            }
          >
            {adjustments.map((adj) => (
              <tr key={adj.adjustment_id}>
                <td>
                  <StatusBadge tone="info">
                    {labelFrom(
                      ADJUSTMENT_SOURCE_LABELS,
                      adj.source_document_type,
                    )}
                  </StatusBadge>
                </td>
                {!userId ? (
                  <td>
                    {adj.admin_username} → {adj.target_username}
                  </td>
                ) : null}
                <td>
                  <code>{adj.source_document_ref}</code>
                </td>
                <td>{adj.reason}</td>
                <td className="amount">
                  {/* B1：反向调账在系统内不产生资金流水（不建充值单），金额列
                      不编造一个不存在的数字；实际退付在支付通道后台，以来源单号对齐。 */}
                  {adj.credits < 0 ? "—" : formatFen(adj.amount_fen)}
                </td>
                <td>
                  {signedCredits(adj.credits)}
                  {adj.credits < 0 ? (
                    <span className="admin-hint">
                      账本反向记账，实际退付在支付通道后台办理
                    </span>
                  ) : null}
                </td>
                <td>
                  {adj.balance_before === null ||
                  adj.balance_before === undefined ||
                  adj.balance_after === null ||
                  adj.balance_after === undefined
                    ? "历史未记录"
                    : `${adj.balance_before} → ${adj.balance_after} 积分`}
                </td>
                <td>{formatDateTime(adj.created_at)}</td>
              </tr>
            ))}
          </DataTable>

          <Pagination
            limit={PAGE_SIZE}
            offset={offset}
            total={total}
            onPageChange={setOffset}
          />
        </>
      )}
    </div>
  );
}
