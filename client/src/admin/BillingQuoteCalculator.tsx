import { type FormEvent, useCallback, useEffect, useState } from "react";

import {
  adminActivationErrorMessage,
  adminRead,
  type BillingQuoteResult,
  type CustomerListItem,
  listCustomers,
  submitBillingQuote,
} from "../api.admin";
import { type BillingService } from "./billingTypes";
import { PageBanner } from "./ui/PageBanner";
import { formatFen } from "./ui/vocabulary";

const UNIT_LABELS: Record<string, string> = {
  second: "秒",
  image: "张",
  call: "次",
};

/**
 * 价格试算器（方案 P1「价格与套餐」）：输入客户（可选）、业务、数量，
 * 输出原价、命中的折扣、取整后实扣积分、折合金额、我方成本与毛利。
 * 计算调用后端同一套计价函数（/billing/quote 复用结算快照），前端不复刻
 * 公式——验收口径是「试算与实际扣费一致」。
 */
export function BillingQuoteCalculator({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [catalog, setCatalog] = useState<BillingService[]>([]);
  const [service, setService] = useState("");
  const [units, setUnits] = useState("10");
  const [customerKeyword, setCustomerKeyword] = useState("");
  const [candidates, setCandidates] = useState<CustomerListItem[]>([]);
  const [picked, setPicked] = useState<CustomerListItem | null>(null);
  const [result, setResult] = useState<BillingQuoteResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void adminRead<{ services: BillingService[] }>(
      "/api/control/billing/catalog",
      "读取业务失败",
    )
      .then((payload) => {
        if (active) setCatalog(payload.services);
      })
      .catch((cause: unknown) => {
        if (active)
          setError(adminActivationErrorMessage(cause, "读取业务失败"));
      });
    return () => {
      active = false;
    };
  }, []);

  const searchCustomers = useCallback(async () => {
    const keyword = customerKeyword.trim();
    if (!keyword) {
      setCandidates([]);
      return;
    }
    try {
      const page = await listCustomers({ username_filter: keyword, limit: 5 });
      setCandidates(page.items);
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "搜索客户失败"));
    }
  }, [customerKeyword]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const unitsNumber = Number(units);
    if (!service || !Number.isFinite(unitsNumber) || unitsNumber <= 0) {
      setError("请选择业务并填写大于 0 的数量。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      setResult(
        await submitBillingQuote({
          service,
          units: unitsNumber,
          userId: picked?.user_id,
        }),
      );
    } catch (cause) {
      setResult(null);
      setError(adminActivationErrorMessage(cause, "试算失败"));
    } finally {
      setBusy(false);
    }
  }

  const selected = catalog.find((item) => item.service === service);
  const unitLabel = selected
    ? (UNIT_LABELS[selected.unit] ?? selected.unit)
    : "";
  const discountLabel =
    result?.discount_basis_points != null
      ? `${result.discount_basis_points / 1000} 折${
          result.discount_source ? "（客户权益）" : ""
        }`
      : "未命中折扣";

  return (
    <section className="admin-panel" aria-label="价格试算">
      <h3>价格试算</h3>
      <p className="admin-hint">
        输入客户（可选）、业务与数量，按当前生效的价格与折扣试算实扣积分、
        折合金额、我方成本与毛利；计算与实际扣费同一套计价逻辑。
      </p>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      <form className="billing-economics-filters" onSubmit={submit}>
        <label>
          客户（可选）
          {picked ? (
            <span className="admin-hint">
              已选：{picked.display_name || picked.username}（{picked.username}
              ）
              <button
                type="button"
                onClick={() => {
                  setPicked(null);
                  setCandidates([]);
                }}
              >
                清除
              </button>
            </span>
          ) : null}
          <input
            placeholder="用户名或公司名"
            value={customerKeyword}
            onChange={(event) => setCustomerKeyword(event.target.value)}
          />
          <button type="button" onClick={() => void searchCustomers()}>
            搜客户
          </button>
          {candidates.length > 0 && !picked ? (
            <ul>
              {candidates.map((candidate) => (
                <li key={candidate.user_id}>
                  <button
                    type="button"
                    onClick={() => {
                      setPicked(candidate);
                      setCandidates([]);
                    }}
                  >
                    {candidate.display_name || candidate.username}（
                    {candidate.username}）
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </label>
        <label>
          业务
          <select
            value={service}
            onChange={(event) => setService(event.target.value)}
            required
          >
            <option value="">请选择业务</option>
            {catalog.map((item) => (
              <option key={item.service} value={item.service}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          数量{unitLabel ? `（${unitLabel}）` : ""}
          <input
            type="number"
            min="1"
            step="1"
            value={units}
            onChange={(event) => setUnits(event.target.value)}
            required
          />
        </label>
        <button type="submit" disabled={busy || readOnly}>
          {busy ? "试算中…" : "试算"}
        </button>
      </form>
      {result ? (
        <div className="admin-table-scroll">
          <table className="admin-data-table" aria-label="试算结果">
            <tbody>
              <tr>
                <th scope="row">业务</th>
                <td>{result.label}</td>
              </tr>
              <tr>
                <th scope="row">数量</th>
                <td>
                  {result.units} {UNIT_LABELS[result.unit] ?? result.unit}
                </td>
              </tr>
              <tr>
                <th scope="row">原价</th>
                <td>
                  {result.unit_credits} 积分 /{" "}
                  {UNIT_LABELS[result.unit] ?? result.unit}
                </td>
              </tr>
              <tr>
                <th scope="row">命中折扣</th>
                <td>{discountLabel}</td>
              </tr>
              <tr>
                <th scope="row">实扣积分</th>
                <td>{result.credits} 积分</td>
              </tr>
              <tr>
                <th scope="row">折合金额</th>
                <td>
                  {result.nominal_fen == null
                    ? "—"
                    : formatFen(Number(result.nominal_fen))}
                </td>
              </tr>
              <tr>
                <th scope="row">我方成本</th>
                <td>
                  {result.cost_fen == null
                    ? "待核对（未配置成本单价）"
                    : formatFen(Number(result.cost_fen))}
                </td>
              </tr>
              <tr>
                <th scope="row">毛利</th>
                <td>
                  {result.gross_fen == null
                    ? "—"
                    : formatFen(Number(result.gross_fen))}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}
