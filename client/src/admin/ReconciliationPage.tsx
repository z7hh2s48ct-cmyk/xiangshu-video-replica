import { useCallback, useEffect, useState } from "react";

import {
  listReconciliationItems,
  type ReconciliationAnomaly,
  syncControlRechargeOrder,
} from "../api";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { formatDateTime, formatFen } from "./ui/vocabulary";

const PAGE_SIZE = 20;

const anomalyTabs: {
  key: ReconciliationAnomaly;
  label: string;
  hint: string;
}[] = [
  {
    key: "paid_without_charge",
    label: "已支付未入账",
    hint: "客户已付款但积分没有到账，可查单补单",
  },
  {
    key: "charge_without_paid_order",
    label: "入账但订单未支付",
    hint: "账本记了充值入账但订单不是已支付状态",
  },
  {
    key: "wallet_mismatch",
    label: "钱包余额与流水不符",
    hint: "钱包当前余额对不上流水累计，需技术核对",
  },
];

type Item = Record<string, unknown>;

const text = (item: Item, key: string) => {
  const value = item[key];
  return value === null || value === undefined ? "—" : String(value);
};

/** 资金中心·对账异常（方案 P1）：三类清单，每条一个处理动作，条数与汇总一致。 */
export function ReconciliationPage() {
  const [anomaly, setAnomaly] = useState<ReconciliationAnomaly>(
    "paid_without_charge",
  );
  const [items, setItems] = useState<Item[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [pendingSync, setPendingSync] = useState<Item | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const result = await listReconciliationItems(anomaly, {
        limit: PAGE_SIZE,
        offset,
      });
      setItems(result.items);
      setTotal(result.total);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取对账异常明细失败");
    } finally {
      setBusy(false);
    }
  }, [anomaly, offset]);

  useEffect(() => {
    void load();
  }, [load]);

  const activeHint = anomalyTabs.find((tab) => tab.key === anomaly)?.hint ?? "";

  async function confirmSync(reason: string) {
    if (!pendingSync) return;
    try {
      await syncControlRechargeOrder(text(pendingSync, "order_no"), reason);
      setNotice(`订单 ${text(pendingSync, "order_no")} 已查单补单。`);
      setPendingSync(null);
      setOffset(0);
      await load();
    } catch (cause) {
      setPendingSync(null);
      setError(cause instanceof Error ? cause.message : "查单补单失败");
    }
  }

  return (
    <div className="reconciliation-page">
      <div role="tablist" aria-label="对账异常类型">
        {anomalyTabs.map((tab) => (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={anomaly === tab.key}
            onClick={() => {
              setAnomaly(tab.key);
              setOffset(0);
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <p className="admin-hint">{activeHint}</p>
      {error && <PageBanner tone="error">{error}</PageBanner>}
      {notice && <PageBanner tone="notice">{notice}</PageBanner>}
      {busy && <p role="status">正在加载…</p>}
      {!busy && !error && items.length === 0 && (
        <div className="empty-state">这类异常当前没有记录。</div>
      )}
      {!busy && items.length > 0 && (
        <DataTable
          ariaLabel="对账异常清单"
          headers={
            <>
              {anomaly === "wallet_mismatch" ? (
                <>
                  <th>客户</th>
                  <th>钱包余额（可用 / 冻结）</th>
                  <th>流水累计（可用 / 冻结）</th>
                </>
              ) : anomaly === "paid_without_charge" ? (
                <>
                  <th>订单号</th>
                  <th>客户</th>
                  <th>金额</th>
                  <th>积分</th>
                  <th>支付时间</th>
                </>
              ) : (
                <>
                  <th>流水编号</th>
                  <th>客户</th>
                  <th>入账积分</th>
                  <th>订单状态</th>
                  <th>时间</th>
                </>
              )}
              <th>操作</th>
            </>
          }
        >
          {anomaly === "wallet_mismatch"
            ? items.map((item) => (
                <tr key={text(item, "user_id")}>
                  <td>
                    {text(item, "display_name")}
                    <small>（{text(item, "username")}）</small>
                  </td>
                  <td>
                    {text(item, "available_credits")} /{" "}
                    {text(item, "reserved_credits")} 积分
                  </td>
                  <td>
                    {text(item, "ledger_available_credits")} /{" "}
                    {text(item, "ledger_reserved_credits")} 积分
                  </td>
                  <td>
                    <span className="admin-hint">需技术核对</span>
                  </td>
                </tr>
              ))
            : anomaly === "paid_without_charge"
              ? items.map((item) => (
                  <tr key={text(item, "order_id")}>
                    <td>
                      <code>{text(item, "order_id")}</code>
                    </td>
                    <td>
                      {text(item, "display_name")}
                      <small>（{text(item, "username")}）</small>
                    </td>
                    <td>{formatFen(Number(item.amount_fen ?? 0))}</td>
                    <td>{text(item, "credits")} 积分</td>
                    <td>{formatDateTime(text(item, "paid_at"))}</td>
                    <td>
                      <button
                        type="button"
                        onClick={() => setPendingSync(item)}
                      >
                        查单补单
                      </button>
                    </td>
                  </tr>
                ))
              : items.map((item) => (
                  <tr key={text(item, "transaction_id")}>
                    <td>
                      <code>{text(item, "transaction_id")}</code>
                    </td>
                    <td>
                      {text(item, "display_name")}
                      <small>（{text(item, "username")}）</small>
                    </td>
                    <td>{text(item, "available_delta")} 积分</td>
                    <td>{text(item, "order_status")}</td>
                    <td>{formatDateTime(text(item, "created_at"))}</td>
                    <td>
                      <span className="admin-hint">需技术核对</span>
                    </td>
                  </tr>
                ))}
        </DataTable>
      )}
      <Pagination
        limit={PAGE_SIZE}
        offset={offset}
        total={total}
        onPageChange={setOffset}
      />
      <ConfirmDialog
        level="reason"
        open={pendingSync !== null}
        title={`查单补单 ${pendingSync ? text(pendingSync, "order_no") : ""}`}
        description="向支付通道查询该订单的最新状态；已确认支付的会立即入账。原因将写入审计日志。"
        confirmLabel="确认查单"
        onConfirm={(reason: string) => void confirmSync(reason)}
        onClose={() => setPendingSync(null)}
      />
    </div>
  );
}
