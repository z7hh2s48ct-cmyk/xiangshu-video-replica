import { useState } from "react";
import type { WalletTransaction } from "../api";
import {
  type CustomerSessionCredential,
  customerGetPriceVersion,
} from "../api";
import "./transaction-pricing.css";

const UNIT_TEXT: Record<string, string> = {
  second: "秒",
  image: "张",
  call: "次",
};

const FREE_REASON_TEXT: Record<string, string> = {
  platform_service: "平台承担，不向客户计费",
  unconfigured: "该科目尚未配置价目",
  disabled: "该科目已停用计费",
  zero_price: "按当前单价计算为 0 积分",
};

const ROUNDING_TEXT: Record<string, string> = {
  ceil: "向上取整",
  floor: "向下取整",
};

// 三行共享同一份提交时冻结的快照：行内变化才是真实资金移动。
const OPERATION_HINT: Record<string, string> = {
  RESERVE: "提交时按暂扣上限先扣留额度",
  SETTLE: "实际扣费按成功交付用量结算，差额同笔退回",
  RELEASE: "失败任务暂扣的额度已全额退回",
};

/** 冻结快照里的 numeric 文本带固定小数位（"2.000000"），界面去尾零后展示。 */
function trimDecimalText(text: string): string {
  const trimmed = text.trim();
  if (!trimmed.includes(".")) return trimmed;
  return trimmed.replace(/0+$/, "").replace(/\.$/, "");
}

/** Build a human summary for the historical price-version lookup result. */
export function priceVersionText(
  result: Awaited<ReturnType<typeof customerGetPriceVersion>>,
): string {
  const unit = UNIT_TEXT[result.unit] ?? result.unit;
  if (!result.found) {
    return `V${result.version} 的价目已不可回查（可能是系统上线前的历史数据）。`;
  }
  const credits = trimDecimalText(result.unit_credits ?? "0");
  const rounding =
    result.unit_rounding === "ceil" && unit
      ? `，不足 1 ${unit}按 1 ${unit}计`
      : "";
  const price = `V${result.version} 价目：${credits} 积分${unit ? `/${unit}` : ""}${rounding}`;
  const current = result.current
    ? "（当前版本）"
    : `（当前版本 V${result.current_version}）`;
  const effective = result.effective_at
    ? `，${result.effective_at.slice(0, 10)} 生效`
    : "";
  return `${price}${effective}${current}`;
}

/** 单项明细：单价 × 用量、折扣、取整、预扣上限，全部来自提交时的冻结快照。 */
export function pricingLines(transaction: WalletTransaction): string[] {
  const pricing = transaction.pricing;
  if (!pricing) return [];

  const lines: string[] = [];
  const unit = pricing.unit ? (UNIT_TEXT[pricing.unit] ?? pricing.unit) : "";
  const unitCredits = pricing.unit_credits
    ? trimDecimalText(pricing.unit_credits)
    : "";

  if ((pricing.credits ?? 0) > 0) {
    if (unitCredits) {
      // unit_rounding=ceil 表示用量先向上取整到 1 个计费单位再乘单价。
      const rounding =
        pricing.unit_rounding === "ceil" && unit
          ? `，不足 1 ${unit}按 1 ${unit}计`
          : "";
      lines.push(
        `单价：${unitCredits} 积分${unit ? `/${unit}` : ""}${rounding}`,
      );
    }
    if (pricing.units && unit) {
      // 快照记录的是提交用量，实际扣费按成功交付用量结算。
      lines.push(`提交用量：${trimDecimalText(pricing.units)} ${unit}`);
    }
    const basisPoints = pricing.discount_basis_points;
    if (basisPoints != null) {
      lines.push(
        `折扣：${
          basisPoints >= 10_000
            ? "无折扣（按价目表单价）"
            : `${Number((basisPoints / 100).toFixed(2))}%（${Number(
                (basisPoints / 1000).toFixed(3),
              )} 折）`
        }`,
      );
    }
    if (pricing.consumption_rounding) {
      lines.push(
        `消费取整：${
          ROUNDING_TEXT[pricing.consumption_rounding] ??
          pricing.consumption_rounding
        }（最低 1 积分）`,
      );
    }
    lines.push(`暂扣上限：${pricing.credits} 积分`);
    const hint = OPERATION_HINT[transaction.type];
    if (hint) lines.push(hint);
  } else {
    const reason = pricing.free_reason
      ? FREE_REASON_TEXT[pricing.free_reason]
      : undefined;
    lines.push(reason ? `未计费：${reason}` : "未计费");
  }

  if (transaction.billing_round != null && transaction.billing_round > 1) {
    lines.push(`计费轮次：第 ${transaction.billing_round} 轮`);
  }
  if (
    transaction.actor_user_id &&
    transaction.actor_user_id !== transaction.user_id
  ) {
    lines.push(
      `操作人：${transaction.actor_name || transaction.actor_user_id}`,
    );
  }
  return lines;
}

/** 扣费依据明细。快照缺失（充值与历史行）时什么都不渲染。 */
export function TransactionPricingBreakdown({
  transaction,
  credential,
}: {
  transaction: WalletTransaction;
  credential?: () => Promise<CustomerSessionCredential | null>;
}) {
  const [lookup, setLookup] =
    useState<Awaited<ReturnType<typeof customerGetPriceVersion>>>();
  const [lookupBusy, setLookupBusy] = useState(false);
  const [lookupError, setLookupError] = useState("");
  const service = transaction.service || transaction.pricing?.service;
  const version = transaction.pricing?.version ?? 0;
  const canLookup = Boolean(credential && service && version > 0);

  async function lookupVersion() {
    if (!credential || !service || version <= 0) return;
    setLookup(undefined);
    setLookupError("");
    setLookupBusy(true);
    try {
      const auth = await credential();
      if (!auth) throw new Error("登录状态已失效，请重新登录。");
      const result = await customerGetPriceVersion(auth, service, version);
      setLookup(result);
    } catch (cause) {
      setLookupError(
        cause instanceof Error ? cause.message : "查询失败，请稍后重试。",
      );
    } finally {
      setLookupBusy(false);
    }
  }

  const lines = pricingLines(transaction);
  if (!lines.length) return null;
  return (
    <details className="pricing-breakdown">
      <summary>
        {version > 0 ? `计费依据 · 费率 V${version}` : "计费依据"}
      </summary>
      <ul>
        {lines.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
      {canLookup && (
        <p className="pricing-breakdown__lookup">
          {!lookup && !lookupError && (
            <button
              type="button"
              disabled={lookupBusy}
              onClick={() => void lookupVersion()}
            >
              {lookupBusy ? "正在查询…" : "查当时价目"}
            </button>
          )}
          {lookupError && <span role="alert">{lookupError}</span>}
          {lookup && <span>{priceVersionText(lookup)}</span>}
        </p>
      )}
    </details>
  );
}
