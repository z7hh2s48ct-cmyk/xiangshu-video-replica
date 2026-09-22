/**
 * 批次1（CW-062 方案 B 差距 1 剩余项）：额度可视化纯函数。
 *
 * 与后端 `sub_account_quota.py` 的上海自然月口径对齐——月度边界按
 * Asia/Shanghai 计算。前端只用这里推演展示态（三态 / 占比 / 日均 / 预测），
 * 不作任何写入或放行决策；权威校验仍在服务端预扣入口（403 拦截）。
 *
 * 所有时间相关函数显式接受 `now`（或 `progress`），便于测试注入固定时间。
 */

import type { CustomerSubAccount } from "../api";

/** 额度展示四态：不限 / 正常 / 邻近上限（≥80%） / 已用尽（≥100%）。 */
export type QuotaState = "unlimited" | "normal" | "warning" | "exhausted";

/** 额度可视化所需的子账号字段（其余字段不参与计算）。 */
export type SubAccountQuotaInput = Pick<
  CustomerSubAccount,
  | "id"
  | "display_name"
  | "username"
  | "monthly_quota_credits"
  | "quota_used_credits"
>;

/** 上海自然月的进度快照（月度额度的分摊基准）。 */
export interface ShanghaiMonthProgress {
  year: number;
  /** 1-12。 */
  month: number;
  /** 上海时区今天是当月第几天（1-based）。 */
  dayOfMonth: number;
  /** 当月总天数（28-31）。 */
  daysInMonth: number;
  /** 距离月末还有多少天（不含今天）。 */
  daysLeft: number;
}

const SHANGHAI_PARTS = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

/** 上海自然月进度：与后端 `shanghai_month_start_iso` 的月末边界同源。 */
export function shanghaiMonthProgress(now: Date): ShanghaiMonthProgress {
  const parts = SHANGHAI_PARTS.formatToParts(now);
  const pick = (type: "year" | "month" | "day"): number =>
    Number(parts.find((part) => part.type === type)?.value ?? "1");
  const year = pick("year");
  const month = pick("month");
  const dayOfMonth = pick("day");
  // Date.UTC(y, m, 0)：第 m 月（1-based）的最后一天。
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return {
    year,
    month,
    dayOfMonth,
    daysInMonth,
    daysLeft: daysInMonth - dayOfMonth,
  };
}

/** 负数已用量（历史跨月退回遗留）统一钳到 0，与 Phase 3a 展示口径一致。 */
function normalizeUsed(used: number): number {
  return Math.max(0, used);
}

/** 三态判定：不限 / 正常 / 邻近上限（≥80%） / 已用尽（≥100% 或上限为 0）。 */
export function quotaState(used: number, quota: number | null): QuotaState {
  if (quota === null) {
    return "unlimited";
  }
  const safeUsed = normalizeUsed(used);
  if (quota <= 0 || safeUsed >= quota) {
    return "exhausted";
  }
  return safeUsed / quota >= 0.8 ? "warning" : "normal";
}

/** 已用百分比（整数；常态 0-100，超限时 >100）；不限额度返回 null。 */
export function quotaPercentUsed(
  used: number,
  quota: number | null,
): number | null {
  if (quota === null) {
    return null;
  }
  if (quota <= 0) {
    return 100;
  }
  return Math.round((normalizeUsed(used) / quota) * 100);
}

/** 预测结果：日均 / 预计月末用量 / 预计用尽天数。 */
export interface QuotaForecast {
  /** 当月日均用量（按已过天数摊平，含今天，保守估计）。 */
  dailyAvg: number;
  /** 按当前速率外推的月末累计用量（向上取整）。 */
  projectedMonthEnd: number;
  /** 预计还能用多少天（向上取整）。 */
  daysToExhaust: number;
}

/**
 * 预测仅在有上限、有用量且未用尽时给值；其余（不限 / 未消费 / 已用尽）
 * 返回 null——避免对无信息场景编造趋势。
 */
export function forecastQuota(
  used: number,
  quota: number | null,
  progress: ShanghaiMonthProgress,
): QuotaForecast | null {
  const safeUsed = normalizeUsed(used);
  if (quota === null || quota <= 0 || safeUsed <= 0 || safeUsed >= quota) {
    return null;
  }
  const daysElapsed = Math.max(1, progress.dayOfMonth);
  const dailyAvg = safeUsed / daysElapsed;
  return {
    dailyAvg,
    projectedMonthEnd: Math.ceil(dailyAvg * progress.daysInMonth),
    daysToExhaust: Math.ceil((quota - safeUsed) / dailyAvg),
  };
}

/** 子账号管理页 KPI 行的聚合结果。 */
export interface QuotaOverview {
  /** 子账号总数。 */
  count: number;
  /** 本月总消费（所有子账号合计，含不限额者）。 */
  totalUsed: number;
  /** 剩余额度总和（仅设限子账号合计）。 */
  remainingTotal: number;
  /** 设限子账号数量。 */
  cappedCount: number;
  /** 额度已用尽（含上限为 0）的子账号数量。 */
  exhaustedCount: number;
}

export function summarizeSubAccountQuotas(
  items: readonly SubAccountQuotaInput[],
): QuotaOverview {
  let totalUsed = 0;
  let remainingTotal = 0;
  let cappedCount = 0;
  let exhaustedCount = 0;
  for (const item of items) {
    const used = normalizeUsed(item.quota_used_credits);
    totalUsed += used;
    const quota = item.monthly_quota_credits;
    if (quota === null) {
      continue;
    }
    cappedCount += 1;
    remainingTotal += Math.max(0, quota - used);
    if (quotaState(used, quota) === "exhausted") {
      exhaustedCount += 1;
    }
  }
  return {
    count: items.length,
    totalUsed,
    remainingTotal,
    cappedCount,
    exhaustedCount,
  };
}

/** 消费占比条形图的一行。 */
export interface QuotaBarDatum {
  id: string;
  label: string;
  used: number;
  /** 占本月总消费的百分比（0-100 整数；全体无消费时全 0）。 */
  percent: number;
  state: QuotaState;
}

/** 按本月消费降序的占比数据（含不限额子账号；同额保持列表原序）。 */
export function quotaBarData(
  items: readonly SubAccountQuotaInput[],
): QuotaBarDatum[] {
  const totalUsed = items.reduce(
    (sum, item) => sum + normalizeUsed(item.quota_used_credits),
    0,
  );
  return items
    .map((item) => {
      const used = normalizeUsed(item.quota_used_credits);
      return {
        id: item.id,
        label: item.display_name,
        used,
        percent: totalUsed > 0 ? Math.round((used / totalUsed) * 100) : 0,
        state: quotaState(item.quota_used_credits, item.monthly_quota_credits),
      };
    })
    .sort((left, right) => right.used - left.used);
}

/**
 * 卡片脚注的预算提示：无提示时返回空串。
 * - 已用尽：给出恢复路径（调高额度 / 等下月重置）。
 * - 邻近上限且预计在月末前用尽：给出剩余天数预警；撑得到月末则保持安静。
 */
export function quotaVizMessage(
  used: number,
  quota: number | null,
  progress: ShanghaiMonthProgress,
): string {
  const state = quotaState(used, quota);
  if (state === "exhausted") {
    return "额度已用尽；调高月度额度或等下月 1 日重置后恢复消费。";
  }
  if (state !== "warning") {
    return "";
  }
  const forecast = forecastQuota(used, quota, progress);
  if (forecast === null || forecast.daysToExhaust > progress.daysLeft) {
    return "";
  }
  return `预计 ${forecast.daysToExhaust} 天后额度用完，建议提前调整。`;
}
