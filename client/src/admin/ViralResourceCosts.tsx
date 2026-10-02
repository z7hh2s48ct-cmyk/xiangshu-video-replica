import { useCallback, useEffect, useRef, useState } from "react";
import {
  adminActivationErrorMessage,
  getViralResourceEvents,
  type ViralResourceRecord,
  type ViralResourceSummary,
  verifyViralResourceCost,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import {
  formatDateTime,
  formatFen,
  RESOURCE_STATE_LABELS,
  RESOURCE_UNIT_LABELS,
} from "./ui/vocabulary";

function yuanToFen(value: string): string | null {
  if (!/^\d{1,10}(\.\d{1,8})?$/.test(value)) return null;
  const [whole, fraction = ""] = value.split(".");
  const scaled = BigInt(whole) * 100000000n + BigInt(fraction.padEnd(8, "0"));
  const rest = String(scaled % 1000000n)
    .padStart(6, "0")
    .replace(/0+$/, "");
  return `${scaled / 1000000n}${rest ? `.${rest}` : ""}`;
}

export function ViralResourceCosts({
  platform,
  videoId,
  from,
  to,
  readOnly = false,
}: {
  platform?: string;
  videoId?: string;
  from?: string;
  to?: string;
  readOnly?: boolean;
}) {
  const [data, setData] = useState<ViralResourceSummary | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [amount, setAmount] = useState("");
  const [bill, setBill] = useState("");
  const generation = useRef(0);
  const [pending, setPending] = useState<{
    record: ViralResourceRecord;
    key: string;
    sent?: { cost: string; bill: string; reason: string };
  } | null>(null);
  const load = useCallback(async () => {
    const current = ++generation.current;
    const result = await getViralResourceEvents({
      platform,
      videoId,
      from,
      to,
      offset,
    });
    if (!Array.isArray(result.components) || !Array.isArray(result.records))
      throw new Error("计量响应缺少证据字段。");
    if (current === generation.current) setData(result);
  }, [platform, videoId, from, to, offset]);
  useEffect(() => {
    let active = true;
    void load().catch((cause) => {
      if (active)
        setError(adminActivationErrorMessage(cause, "读取素材计量失败"));
    });
    return () => {
      active = false;
      generation.current += 1;
    };
  }, [load]);
  return (
    <section aria-label="素材计量与账单核对">
      <h4>素材计量与账单核对</h4>
      {error && !pending && <PageBanner tone="error">{error}</PageBanner>}
      {data && (
        <>
          <p>
            已确认资源成本 {formatFen(data.knownCostFen)}；完整成本
            {data.costFen == null ? "待核对" : formatFen(data.costFen)}。
          </p>
          <p className="admin-hint">{data.countingRule}</p>
          <dl>
            {data.components.map((component) => (
              <div key={component.kind}>
                <dt>{component.label}</dt>
                <dd>
                  {component.events} 个事件 · 已知计量 {component.knownQuantity}{" "}
                  {RESOURCE_UNIT_LABELS[component.unit] ?? "未知单位"} ·{" "}
                  {component.unknownQuantityEvents} 项计量未知 ·{" "}
                  {component.unknownCostEvents} 项成本待核对
                </dd>
              </div>
            ))}
          </dl>
          <details>
            <summary>尚缺证据</summary>
            <ul>
              {data.missingEvidence.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </details>
          {data.records.map((record) => (
            <article key={record.id}>
              <strong>{record.label}</strong> ·{" "}
              {formatDateTime(record.created_at)} ·{" "}
              {RESOURCE_STATE_LABELS[record.state] ?? "未知状态"}
              <p>
                {record.quantity == null
                  ? "计量未知"
                  : `${record.quantity} ${RESOURCE_UNIT_LABELS[record.unit] ?? "未知单位"}`}{" "}
                · 成本{" "}
                {record.cost_fen == null
                  ? "待核对"
                  : formatFen(record.cost_fen)}
              </p>
              {record.evidence && (
                <p>
                  账单明细 {record.evidence.bill_reference} ·{" "}
                  {record.evidence.reason} ·{" "}
                  {formatDateTime(record.evidence.verified_at)}
                </p>
              )}
              {!readOnly && record.state !== "PENDING" && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setAmount("");
                    setBill("");
                    setError("");
                    setPending({ record, key: crypto.randomUUID() });
                  }}
                >
                  核对账单
                </button>
              )}
              <details>
                <summary>技术信息</summary>
                <p>
                  事件 {record.id} · 视频 {record.video_id}
                </p>
                {record.evidence && (
                  <p>核对账号 {record.evidence.operator_id}</p>
                )}
              </details>
            </article>
          ))}
          {data.records.length === 0 && (
            <p>本范围没有已记录事件；历史未观测不代表没有成本。</p>
          )}
          <Pagination
            offset={offset}
            limit={data.limit}
            total={data.total}
            disabled={busy}
            onPageChange={setOffset}
          />
        </>
      )}
      <ConfirmDialog
        open={pending !== null}
        title="核对素材账单"
        description="仅填写直接归属本事件的账单明细；不要录入共享账单总额。此操作不改客户余额。"
        busy={busy}
        error={error}
        confirmLabel="保存账单证据"
        onClose={() => {
          if (!busy) setPending(null);
        }}
        onConfirm={(reason) => {
          if (!pending || busy) return;
          const cost = yuanToFen(amount);
          if (cost === null || bill.trim().length < 3) {
            setError("请填写有效人民币金额和账单明细编号。");
            return;
          }
          const sent = pending.sent ?? { cost, bill: bill.trim(), reason };
          setPending({ ...pending, sent });
          setBusy(true);
          setError("");
          void verifyViralResourceCost(
            pending.record,
            sent.cost,
            sent.bill,
            sent.reason,
            pending.key,
          )
            .then(() => {
              setPending(null);
              return load();
            })
            .catch((cause) =>
              setError(
                adminActivationErrorMessage(
                  cause,
                  "核对未完成，请使用同一次提交重试或刷新核对证据。",
                ),
              ),
            )
            .finally(() => setBusy(false));
        }}
      >
        {pending?.sent && (
          <p>重试沿用首次提交的金额、账单明细和原因：{pending.sent.reason}</p>
        )}
        <label>
          账单金额（元）
          <input
            value={amount}
            inputMode="decimal"
            disabled={busy || Boolean(pending?.sent)}
            onChange={(event) => setAmount(event.target.value)}
          />
        </label>
        <label>
          账单编号与明细行
          <input
            value={bill}
            maxLength={200}
            disabled={busy || Boolean(pending?.sent)}
            onChange={(event) => setBill(event.target.value)}
          />
        </label>
      </ConfirmDialog>
    </section>
  );
}
