/** 充值套餐的展示口径（客户充值页与管理端套餐管理共用）。
 *
 * 折扣接口键与后端 `billing_catalog.INTERFACE_KEYS` 对齐：管理端勾选的键必须能
 * 在这里翻译成中文；未知键（历史数据）原样展示，不隐藏配置事实。
 */

import type { CustomerRechargePackage } from "./api";

export const DISCOUNT_INTERFACE_LABELS: Record<string, string> = {
  video_generation: "视频生成",
  video_analysis: "视频分析",
  first_frame: "首帧生成",
  character: "人物形象",
  script_rewrite: "文案改写",
  oral: "数字人口播",
  asr: "语音转写",
  viral_extract: "爆款提取",
  link_resolution: "链接解析",
  prompt_optimize: "提示词优化",
};

export function discountInterfaceLabel(key: string): string {
  return DISCOUNT_INTERFACE_LABELS[key] ?? key;
}

/** 折扣率小数（"0.9000"）→ 中文折扣（"9折"）；非法/空 → null（无折扣）。 */
export function formatDiscountZhe(
  rateText: string | null | undefined,
): string | null {
  if (rateText === null || rateText === undefined || rateText === "") {
    return null;
  }
  const rate = Number(rateText);
  if (!Number.isFinite(rate) || rate <= 0 || rate > 1) {
    return null;
  }
  const zhe = Number((rate * 10).toFixed(4));
  return `${zhe}折`;
}

/** 套餐权益摘要（"视频生成 9折" / "全部消耗 9折"）；无折扣档位 → null。 */
export function packageBenefitLabel(
  pkg: Pick<CustomerRechargePackage, "discount_rate" | "discount_interfaces">,
): string | null {
  const zhe = formatDiscountZhe(pkg.discount_rate);
  if (zhe === null) {
    return null;
  }
  const names = pkg.discount_interfaces.map(discountInterfaceLabel);
  return names.length === 0 ? `全部消耗 ${zhe}` : `${names.join("、")} ${zhe}`;
}

/** 报价里的折扣来源是后端 token（recharge_package/manual），面向用户须说人话。 */
export function discountSourceLabel(
  source: string | null | undefined,
): string | null {
  if (!source) {
    return null;
  }
  if (source === "recharge_package") {
    return "充值套餐";
  }
  if (source === "manual") {
    return "专项优惠";
  }
  return source;
}

/** 套餐赠送积分：到账积分 − 基础汇率换算积分（无赠送或无法计算 → null）。 */
export function packageBonusCredits(
  wallet: {
    points_per_yuan?: number | null;
    internal_unit_price_fen: number;
  } | null,
  pkg: Pick<CustomerRechargePackage, "amount_fen" | "credits">,
): number | null {
  if (!wallet) {
    return null;
  }
  const base =
    wallet.points_per_yuan && wallet.points_per_yuan > 0
      ? Math.floor((pkg.amount_fen / 100) * wallet.points_per_yuan)
      : Math.floor(pkg.amount_fen / wallet.internal_unit_price_fen);
  const bonus = pkg.credits - base;
  return bonus > 0 ? bonus : null;
}

/** 该金额是否命中已配置套餐（自定义金额下单会失去套餐赠送/权益，需要提示）。 */
export function matchingPackagesForAmount(
  packages: readonly CustomerRechargePackage[],
  amountFen: number,
): CustomerRechargePackage[] {
  return packages.filter((pkg) => pkg.amount_fen === amountFen);
}

/** 套餐是否低于生效起充额（后端下单会 422 RECHARGE_PACKAGE_BELOW_MINIMUM）。
 *
 * 钱包快照未就绪（null）时不判禁用：等快照到位后按 ``min_recharge_fen`` 判定。
 */
export function packageBelowMinimum(
  pkg: Pick<CustomerRechargePackage, "amount_fen">,
  wallet: { min_recharge_fen: number } | null,
): boolean {
  return wallet !== null && pkg.amount_fen < wallet.min_recharge_fen;
}
