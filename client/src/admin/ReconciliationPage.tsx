import { useCallback, useEffect, useRef, useState } from "react";

import {
  listReconciliationItems,
  type ReconciliationAnomaly,
  repairPaidRechargeLedger,
} from "../api";
import { AdminControlError, adminWrite } from "../api.admin";
import { CustomerLink } from "./CustomerLink";
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
    hint: "按已支付订单快照补记积分；钱包与已有流水不符时先人工核对",
  },
  {
    key: "charge_without_paid_order",
    label: "入账但订单未支付",
    hint: "账本记了充值入账但订单不是已支付状态",
  },
  {
    key: "wallet_mismatch",
    label: "钱包余额与流水不符",
    hint: "钱包当前余额对不上流水累计，记录核对结果后仍需处理差异",
  },
];

type Item = Record<string, unknown>;

const text = (item: Item, key: string) => {
  const value = item[key];
  return value === null || value === undefined ? "—" : String(value);
};

/** 资金中心·对账异常（方案 P1）：三类清单，每条一个处理动作，条数与汇总一致。 */
export function ReconciliationPage({
  readOnly = false,
  initialAnomaly = "paid_without_charge",
}: {
  readOnly?: boolean;
  initialAnomaly?: ReconciliationAnomaly;
}) {
  const [anomaly, setAnomaly] = useState<ReconciliationAnomaly>(initialAnomaly);
  const [items, setItems] = useState<Item[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [pendingSync, setPendingSync] = useState<Item | null>(null);
  const [pendingVerify, setPendingVerify] = useState<{
    item: Item;
    anomaly: ReconciliationAnomaly;
    key: string;
    reason?: string;
  } | null>(null);
  const [verifyBusy, setVerifyBusy] = useState(false);
  const [verifyError, setVerifyError] = useState("");
  const unresolvedVerifications = useRef(
    new Map<string, NonNullable<typeof pendingVerify>>(),
  );
  const verificationIdentity = (item: Item, kind: ReconciliationAnomaly) =>
    `${kind}:${item[kind === "wallet_mismatch" ? "user_id" : "transaction_id"]}:${item.snapshot}`;

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
    if (!pendingSync || readOnly) return;
    const orderNo = pendingSync.order_no;
    if (typeof orderNo !== "string" || !orderNo.trim()) {
      setError("订单号缺失，请刷新明细后重试。");
      setPendingSync(null);
      return;
    }
    try {
      const result = await repairPaidRechargeLedger(orderNo, reason);
      setNotice(
        result.outcome === "repaired"
          ? `订单 ${orderNo} 已补记 ${result.credits} 积分。`
          : `订单 ${orderNo} 已有入账流水，未重复增加积分。`,
      );
      setPendingSync(null);
      setOffset(0);
      await load();
    } catch (cause) {
      setPendingSync(null);
      setError(cause instanceof Error ? cause.message : "补记积分失败");
    }
  }

  async function confirmVerification(reason: string) {
    if (!pendingVerify || readOnly || verifyBusy) return;
    if (pendingVerify.reason && pendingVerify.reason !== reason) {
      setVerifyError("上次提交结果未确定，请保留原核对说明后重试。");
      return;
    }
    const pending = { ...pendingVerify, reason };
    const identity = verificationIdentity(pending.item, pending.anomaly);
    unresolvedVerifications.current.set(identity, pending);
    setPendingVerify(pending);
    setVerifyBusy(true);
    setVerifyError("");
    try {
      await adminWrite(
        "/api/control/billing-reconciliation/verify",
        {
          anomaly: pending.anomaly,
          entity_id:
            pending.item[
              pending.anomaly === "wallet_mismatch"
                ? "user_id"
                : "transaction_id"
            ],
          snapshot: pending.item.snapshot,
        },
        reason,
        "记录核对结果失败",
        pending.key,
      );
      setPendingVerify(null);
      unresolvedVerifications.current.delete(identity);
      setNotice("已记录核对说明，异常仍保留；请继续处理账务差异。");
      await load();
    } catch (cause) {
      if (
        cause instanceof AdminControlError &&
        cause.status &&
        cause.status < 500
      ) {
        setPendingVerify(null);
        unresolvedVerifications.current.delete(identity);
        setError(cause.message);
      } else {
        setVerifyError(
          "提交结果暂未确定，请保留核对说明并重试，避免重复记录。",
        );
      }
    } finally {
      setVerifyBusy(false);
    }
  }

  const customer = (item: Item) => (
    <CustomerLink
      userId={typeof item.user_id === "string" ? item.user_id : undefined}
      company={text(item, "display_name")}
      username={text(item, "username")}
    />
  );
  function verification(item: Item) {
    const record = item.verification as
      | { state: string; reason: string; operator: string; at: string }
      | null
      | undefined;
    return (
      <>
        <span>
          {record?.state === "verified"
            ? "已核对，待处理"
            : record
              ? "数据已变化，需重新核对"
              : "待人工核对"}
        </span>
        {record && (
          <details>
            <summary>查看核对记录</summary>
            <p>{record.reason}</p>
            <p>
              {record.operator} · {formatDateTime(record.at)}
            </p>
          </details>
        )}
        {!readOnly && (
          <button
            type="button"
            disabled={typeof item.snapshot !== "string"}
            onClick={() => {
              setVerifyError("");
              setPendingVerify(
                unresolvedVerifications.current.get(
                  verificationIdentity(item, anomaly),
                ) ?? { item, anomaly, key: crypto.randomUUID() },
              );
            }}
          >
            记录核对结果
          </button>
        )}
      </>
    );
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
                  <th>关联订单</th>
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
                  <td>{customer(item)}</td>
                  <td>
                    {text(item, "available_credits")} /{" "}
                    {text(item, "reserved_credits")} 积分
                  </td>
                  <td>
                    {text(item, "ledger_available_credits")} /{" "}
                    {text(item, "ledger_reserved_credits")} 积分
                  </td>
                  <td>{verification(item)}</td>
                </tr>
              ))
            : anomaly === "paid_without_charge"
              ? items.map((item) => (
                  <tr key={text(item, "order_id")}>
                    <td>
                      <code>{text(item, "order_no")}</code>
                    </td>
                    <td>{customer(item)}</td>
                    <td>{formatFen(Number(item.amount_fen ?? 0))}</td>
                    <td>{text(item, "credits")} 积分</td>
                    <td>{formatDateTime(text(item, "paid_at"))}</td>
                    <td>
                      {!readOnly && (
                        <button
                          type="button"
                          disabled={
                            typeof item.order_no !== "string" ||
                            !item.order_no.trim()
                          }
                          onClick={() => setPendingSync(item)}
                        >
                          补记积分
                        </button>
                      )}
                    </td>
                  </tr>
                ))
              : items.map((item) => (
                  <tr key={text(item, "transaction_id")}>
                    <td>
                      {typeof item.order_no === "string" ? (
                        <a
                          href={`#admin/funds?intent=order&orderNo=${encodeURIComponent(item.order_no)}&userId=${encodeURIComponent(String(item.user_id ?? ""))}`}
                        >
                          {item.order_no}
                        </a>
                      ) : (
                        "历史订单号未记录"
                      )}
                      <details>
                        <summary>查看流水编号</summary>
                        <code>{text(item, "transaction_id")}</code>
                      </details>
                    </td>
                    <td>{customer(item)}</td>
                    <td>{text(item, "available_delta")} 积分</td>
                    <td>{text(item, "order_status")}</td>
                    <td>{formatDateTime(text(item, "created_at"))}</td>
                    <td>{verification(item)}</td>
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
        open={!readOnly && pendingSync !== null}
        title={`补记积分 ${pendingSync ? text(pendingSync, "order_no") : ""}`}
        description="按本地已支付订单的积分快照补记缺失的充值流水和钱包余额。已有流水不会重复入账；钱包不符时拒绝补记。原因将写入审计日志。"
        confirmLabel="确认补记"
        onConfirm={(reason: string) => void confirmSync(reason)}
        onClose={() => setPendingSync(null)}
      />
      <ConfirmDialog
        level="reason"
        open={!readOnly && pendingVerify !== null}
        title="记录人工核对结果"
        description="记录当前差异的核对说明、操作人和时间。记录不会改动余额或隐藏异常；账务数据变化后须重新核对。"
        confirmLabel={verifyError ? "重试原核对记录" : "确认记录"}
        busy={verifyBusy}
        error={verifyError}
        onConfirm={(reason: string) => void confirmVerification(reason)}
        onClose={() => {
          if (!verifyBusy) setPendingVerify(null);
        }}
      />
    </div>
  );
}
