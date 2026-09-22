import { describe, expect, it } from "vitest";

import {
  forecastQuota,
  quotaBarData,
  quotaPercentUsed,
  quotaState,
  quotaVizMessage,
  type SubAccountQuotaInput,
  shanghaiMonthProgress,
  summarizeSubAccountQuotas,
} from "./quotaViz";

/** 最小子账号输入：只带额度可视化用到的字段。 */
function subAccount(
  overrides: Partial<SubAccountQuotaInput> & { id: string },
): SubAccountQuotaInput {
  return {
    display_name: `子账号 ${overrides.id}`,
    username: `sub_${overrides.id}`,
    monthly_quota_credits: null,
    quota_used_credits: 0,
    ...overrides,
  };
}

/** 固定上海时间工具：直接给 UTC 时刻，断言上海口径换算。 */
function shanghai(iso: string): Date {
  return new Date(iso);
}

describe("shanghaiMonthProgress", () => {
  it("折算上海自然月进度（9 月 22 日 → 30 天、剩 8 天）", () => {
    const progress = shanghaiMonthProgress(
      shanghai("2026-09-22T02:00:00Z"), // 北京 10:00
    );
    expect(progress).toEqual({
      year: 2026,
      month: 9,
      dayOfMonth: 22,
      daysInMonth: 30,
      daysLeft: 8,
    });
  });

  it("跨时区边界按上海日切（UTC 深夜 = 上海次日）", () => {
    const progress = shanghaiMonthProgress(
      shanghai("2026-09-21T20:00:00Z"), // 北京 09-22 04:00
    );
    expect(progress.dayOfMonth).toBe(22);
  });

  it("UTC 月末深夜在上海已是次月 1 日", () => {
    const progress = shanghaiMonthProgress(
      shanghai("2026-09-30T17:00:00Z"), // 北京 10-01 01:00
    );
    expect(progress.month).toBe(10);
    expect(progress.dayOfMonth).toBe(1);
    expect(progress.daysInMonth).toBe(31);
  });

  it("2 月天数按闰年规则（2026 平年 / 2028 闰年）", () => {
    expect(
      shanghaiMonthProgress(shanghai("2026-02-15T04:00:00Z")).daysInMonth,
    ).toBe(28);
    expect(
      shanghaiMonthProgress(shanghai("2028-02-15T04:00:00Z")).daysInMonth,
    ).toBe(29);
  });

  it("月末当天 daysLeft 为 0", () => {
    expect(
      shanghaiMonthProgress(shanghai("2026-09-30T04:00:00Z")).daysLeft,
    ).toBe(0);
  });
});

describe("quotaState", () => {
  it("无上限恒为不限（含大额已用）", () => {
    expect(quotaState(0, null)).toBe("unlimited");
    expect(quotaState(99_999, null)).toBe("unlimited");
  });

  it("80% 是邻近上限阈值（79 正常 / 80 预警）", () => {
    expect(quotaState(79, 100)).toBe("normal");
    expect(quotaState(80, 100)).toBe("warning");
    expect(quotaState(99, 100)).toBe("warning");
  });

  it("用尽含超限历史值与 0 上限", () => {
    expect(quotaState(100, 100)).toBe("exhausted");
    expect(quotaState(120, 100)).toBe("exhausted");
    // 上限 0 = 不允许消费，视为已用尽。
    expect(quotaState(0, 0)).toBe("exhausted");
  });

  it("负数已用量（跨月退回遗留）按 0 处理", () => {
    expect(quotaState(-5, 100)).toBe("normal");
    expect(quotaState(-5, 0)).toBe("exhausted");
  });
});

describe("quotaPercentUsed", () => {
  it("正常百分比四舍五入", () => {
    expect(quotaPercentUsed(1800, 5000)).toBe(36);
    expect(quotaPercentUsed(1, 3)).toBe(33);
  });

  it("不限额度返回 null；0 上限按 100% 展示", () => {
    expect(quotaPercentUsed(500, null)).toBeNull();
    expect(quotaPercentUsed(0, 0)).toBe(100);
  });

  it("超限用量返回大于 100 的整数（配合进度条 value 钳制展示）", () => {
    expect(quotaPercentUsed(1200, 1000)).toBe(120);
  });
});

describe("forecastQuota", () => {
  const progress = {
    year: 2026,
    month: 9,
    dayOfMonth: 10,
    daysInMonth: 30,
    daysLeft: 20,
  };

  it("按已过天数摊平时日均与月末外推", () => {
    expect(forecastQuota(500, 5000, progress)).toEqual({
      dailyAvg: 50,
      projectedMonthEnd: 1500,
      daysToExhaust: 90,
    });
  });

  it("月初第一天按 1 天摊平", () => {
    const first = { ...progress, dayOfMonth: 1, daysLeft: 29 };
    expect(forecastQuota(1000, 5000, first)).toEqual({
      dailyAvg: 1000,
      projectedMonthEnd: 30000,
      daysToExhaust: 4,
    });
  });

  it("未消费 / 已用尽 / 不限额度都不给预测", () => {
    expect(forecastQuota(0, 5000, progress)).toBeNull();
    expect(forecastQuota(5000, 5000, progress)).toBeNull();
    expect(forecastQuota(100, null, progress)).toBeNull();
    expect(forecastQuota(0, 0, progress)).toBeNull();
  });
});

describe("summarizeSubAccountQuotas", () => {
  it("空列表返回全 0", () => {
    expect(summarizeSubAccountQuotas([])).toEqual({
      count: 0,
      totalUsed: 0,
      remainingTotal: 0,
      cappedCount: 0,
      exhaustedCount: 0,
    });
  });

  it("聚合含不限额项的消费、仅设限项的剩余额度与用尽数", () => {
    const overview = summarizeSubAccountQuotas([
      subAccount({ id: "a", quota_used_credits: 300 }),
      subAccount({
        id: "b",
        monthly_quota_credits: 5000,
        quota_used_credits: 1800,
      }),
      subAccount({
        id: "c",
        monthly_quota_credits: 500,
        quota_used_credits: 400,
      }),
      subAccount({
        id: "d",
        monthly_quota_credits: 5000,
        quota_used_credits: 5000,
      }),
    ]);
    expect(overview).toEqual({
      count: 4,
      totalUsed: 7500,
      remainingTotal: 3200 + 100 + 0,
      cappedCount: 3,
      exhaustedCount: 1,
    });
  });

  it("超限用量不使剩余额度总和变负", () => {
    const overview = summarizeSubAccountQuotas([
      subAccount({
        id: "over",
        monthly_quota_credits: 100,
        quota_used_credits: 250,
      }),
    ]);
    expect(overview.remainingTotal).toBe(0);
    expect(overview.exhaustedCount).toBe(1);
  });
});

describe("quotaBarData", () => {
  it("按消费降序并折算占比（百分比为全局占比）", () => {
    const bars = quotaBarData([
      subAccount({
        id: "zhang",
        display_name: "张三",
        quota_used_credits: 1800,
      }),
      subAccount({ id: "zhao", display_name: "赵六", quota_used_credits: 200 }),
      subAccount({ id: "li", display_name: "李四", quota_used_credits: 400 }),
    ]);
    expect(bars.map((bar) => bar.label)).toEqual(["张三", "李四", "赵六"]);
    expect(bars.map((bar) => bar.percent)).toEqual([75, 17, 8]);
    expect(bars.every((bar) => bar.state === "unlimited")).toBe(true);
  });

  it("全体无消费时占比全 0 且保持原序", () => {
    const bars = quotaBarData([
      subAccount({ id: "a", quota_used_credits: 0 }),
      subAccount({ id: "b", quota_used_credits: 0 }),
    ]);
    expect(bars.map((bar) => bar.percent)).toEqual([0, 0]);
  });

  it("标记已用尽行（供条形图红色区分）", () => {
    const bars = quotaBarData([
      subAccount({
        id: "done",
        monthly_quota_credits: 5000,
        quota_used_credits: 5000,
      }),
    ]);
    expect(bars[0].state).toBe("exhausted");
  });
});

describe("quotaVizMessage", () => {
  // 9 月 20 日：30 天，剩 10 天。
  const progress = {
    year: 2026,
    month: 9,
    dayOfMonth: 20,
    daysInMonth: 30,
    daysLeft: 10,
  };

  it("已用尽给出恢复路径", () => {
    expect(quotaVizMessage(5000, 5000, progress)).toBe(
      "额度已用尽；调高月度额度或等下月 1 日重置后恢复消费。",
    );
  });

  it("邻近上限且预计月末前用尽 → 给出天数预警", () => {
    // 4400/5000（88%），日均 220，剩余 600 → 3 天用尽 ≤ 剩 10 天。
    expect(quotaVizMessage(4400, 5000, progress)).toBe(
      "预计 3 天后额度用完，建议提前调整。",
    );
  });

  it("邻近上限但撑得到月末 → 保持安静", () => {
    // 8100/10000（81%），28 日时日均约 289，剩余 1900 → 7 天 > 剩 2 天。
    const lateMonth = {
      year: 2026,
      month: 9,
      dayOfMonth: 28,
      daysInMonth: 30,
      daysLeft: 2,
    };
    expect(quotaVizMessage(8100, 10000, lateMonth)).toBe("");
  });

  it("正常 / 不限额度不提示", () => {
    expect(quotaVizMessage(1000, 5000, progress)).toBe("");
    expect(quotaVizMessage(9999, null, progress)).toBe("");
  });
});
