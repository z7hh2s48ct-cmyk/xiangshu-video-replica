import { useEffect, useRef, useState } from "react";
import type { CustomerCreditConfig, CustomerPricing } from "../api";
import {
  AdminControlError,
  adminRead,
  getCustomerPricing,
  updateCustomerPricing,
} from "../api.admin";
import { formatDateTime } from "./ui/vocabulary";

/** 万分比折扣 → 折数回显（10000 → "10"，9500 → "9.5"，8750 → "8.75"）。 */
function foldsFromBasisPoints(basisPoints: number | undefined): string {
  if (!basisPoints || basisPoints >= 10_000) return "10";
  return String(Number((basisPoints / 1000).toFixed(3)));
}

function roundingLabel(value: string | undefined): string {
  if (value === "floor") return "向下取整";
  if (value === "ceil") return "向上取整";
  return "—";
}

type PricingHistoryItem = {
  version: number;
  config: Record<string, unknown> | null;
  source: "audit" | "current";
  current: boolean;
  actor_user_id: string | null;
  actor_username: string | null;
  reason: string | null;
  effective_at: string | null;
};
type PricingHistory = { current_version: number; items: PricingHistoryItem[] };
type PricingSnapshot = {
  points_per_yuan?: number;
  discount_basis_points?: number;
  consumption_rounding?: string;
};

export function CustomerPricingManager({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [data, setData] = useState<CustomerPricing | null>(null);
  const [pointsPerYuan, setPointsPerYuan] = useState("");
  const [discountFolds, setDiscountFolds] = useState("10");
  const [rounding, setRounding] = useState<"ceil" | "floor">("ceil");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [history, setHistory] = useState<PricingHistory>();
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const retry = useRef<{ fingerprint: string; key: string } | null>(null);
  const historyRequest = useRef(0);
  const saving = useRef(false);
  useEffect(() => {
    void refresh;
    let active = true;
    setData(null);
    setError("");
    void getCustomerPricing()
      .then((value) => {
        if (!active) return;
        setData(value);
        if (value.config) {
          setPointsPerYuan(String(value.config.points_per_yuan));
          setDiscountFolds(
            foldsFromBasisPoints(value.config.discount_basis_points),
          );
          setRounding(value.config.consumption_rounding ?? "ceil");
        }
      })
      .catch((cause) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "读取积分价格失败");
      });
    return () => {
      active = false;
    };
  }, [refresh]);
  // P2-3：配置版本序列同样以审计为源，便于对账历史充值换算与折扣。
  function loadHistory() {
    const token = historyRequest.current + 1;
    historyRequest.current = token;
    setHistoryOpen(true);
    setHistoryBusy(true);
    setHistoryError("");
    void adminRead<PricingHistory>(
      "/api/control/billing/pricing-history",
      "读取报价配置历史失败",
    )
      .then((result) => {
        if (historyRequest.current !== token) return;
        setHistory(result);
      })
      .catch((cause: unknown) => {
        if (historyRequest.current !== token) return;
        setHistoryError(cause instanceof Error ? cause.message : "读取失败");
      })
      .finally(() => {
        if (historyRequest.current === token) setHistoryBusy(false);
      });
  }
  function toggleHistory() {
    if (historyOpen) {
      historyRequest.current += 1;
      setHistoryOpen(false);
      return;
    }
    loadHistory();
  }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!data || readOnly || saving.current) return;
    const discountValue = Number(discountFolds.trim());
    if (
      !/^\d{1,2}(\.\d{1,3})?$/.test(discountFolds.trim()) ||
      discountValue <= 0 ||
      discountValue > 10
    ) {
      setError(
        "折扣请输入大于 0 且不超过 10 的折数，最多三位小数（10 折表示原价）。",
      );
      return;
    }
    const config: CustomerCreditConfig = {
      points_per_yuan: Number(pointsPerYuan),
      discount_basis_points: Math.round(discountValue * 1000),
      consumption_rounding: rounding,
    };
    if (
      !Number.isSafeInteger(config.points_per_yuan) ||
      config.points_per_yuan < 1 ||
      config.points_per_yuan > 1_000_000
    ) {
      setError("请输入 1–1000000 的整数积分。");
      return;
    }
    const reason = "更新客户报价配置（换算/折扣/取整）";
    const fingerprint = JSON.stringify([config, data.version, reason.trim()]);
    if (retry.current?.fingerprint !== fingerprint)
      retry.current = { fingerprint, key: crypto.randomUUID() };
    saving.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await updateCustomerPricing(
        config,
        data.version,
        reason.trim(),
        retry.current.key,
      );
      setData(result);
      retry.current = null;
      setNotice(
        "价格配置已保存：充值换算对新充值生效，折扣与取整对新受理任务生效。",
      );
      if (historyOpen) loadHistory();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存积分价格失败");
      if (
        cause instanceof AdminControlError &&
        cause.status &&
        cause.status < 500
      )
        retry.current = null;
    } finally {
      saving.current = false;
      setBusy(false);
    }
  }
  return (
    <section
      className="admin-panel admin-recharge-rate"
      aria-label="客户报价配置"
    >
      <h2>客户报价配置</h2>
      {error && (
        <div role="alert">
          {error}{" "}
          <button
            type="button"
            disabled={busy}
            onClick={() => setRefresh((v) => v + 1)}
          >
            刷新配置
          </button>
        </div>
      )}
      {notice && <p role="status">{notice}</p>}
      {!data ? (
        !error && <p role="status">正在读取积分配置…</p>
      ) : (
        <form onSubmit={save} className="admin-recharge-rate__form">
          <label>
            每 1 元充值获得积分
            <input
              type="number"
              min="1"
              max="1000000"
              step="1"
              required
              disabled={readOnly || busy}
              value={pointsPerYuan}
              placeholder="请输入积分数"
              onChange={(event) => setPointsPerYuan(event.target.value)}
            />
          </label>
          <label>
            全科目折扣（折）
            <input
              type="number"
              min="0.001"
              max="10"
              step="0.001"
              required
              disabled={readOnly || busy}
              value={discountFolds}
              placeholder="10 表示原价"
              onChange={(event) => setDiscountFolds(event.target.value)}
            />
          </label>
          <label>
            消费取整方式
            <select
              disabled={readOnly || busy}
              value={rounding}
              onChange={(event) =>
                setRounding(event.target.value as "ceil" | "floor")
              }
            >
              <option value="ceil">向上取整（不足 1 积分进位）</option>
              <option value="floor">向下取整（不足 1 积分舍去）</option>
            </select>
          </label>
          {!readOnly && (
            <button type="submit" disabled={busy}>
              {busy ? "正在保存…" : "保存价格配置"}
            </button>
          )}
          <p className="admin-recharge-rate__hint">
            折扣与取整影响所有科目的报价：报价 = 单价 ×
            折扣，带小数时按取整方式处理， 单次计费至少 1
            积分；已受理任务保留受理时价格。
          </p>
        </form>
      )}
      <p className="admin-recharge-rate__note">
        客户专项折扣与套餐权益已接入扣费链路：受理任务时与上表全局折扣取更优（折数更低者）计价，并随任务冻结，事后调整不影响已受理任务。
      </p>
      {data && (
        <div className="admin-recharge-rate__history">
          <button
            type="button"
            className="btn-secondary"
            aria-expanded={historyOpen}
            onClick={toggleHistory}
          >
            {historyOpen ? "收起配置历史" : "查看配置历史"}
          </button>
          {historyOpen && (
            <div>
              {historyBusy && <p role="status">正在读取配置历史…</p>}
              {historyError && <p role="alert">{historyError}</p>}
              {!historyBusy && !historyError && history && (
                <table
                  className="admin-data-table"
                  aria-label="报价配置版本历史"
                >
                  <thead>
                    <tr>
                      <th>版本</th>
                      <th>调整时间</th>
                      <th>每 1 元充值积分</th>
                      <th>全科目折扣</th>
                      <th>消费取整</th>
                      <th>操作人</th>
                      <th>调整原因</th>
                      <th>记录来源</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.items.map((item) => {
                      const snapshot = (item.config ?? {}) as PricingSnapshot;
                      return (
                        <tr key={item.version}>
                          <td>
                            V{item.version}
                            {item.current && <small>当前</small>}
                          </td>
                          <td>
                            {item.effective_at
                              ? formatDateTime(item.effective_at)
                              : "—"}
                          </td>
                          <td>{snapshot.points_per_yuan ?? "—"}</td>
                          <td>
                            {snapshot.discount_basis_points === undefined
                              ? "—"
                              : `${foldsFromBasisPoints(snapshot.discount_basis_points)} 折`}
                          </td>
                          <td>
                            {roundingLabel(snapshot.consumption_rounding)}
                          </td>
                          <td>{item.actor_username ?? "—"}</td>
                          <td>{item.reason ?? "—"}</td>
                          <td>
                            {item.source === "audit"
                              ? "审计记录"
                              : "当前行（无审计）"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
