import { readFileSync } from "node:fs";

import { describe, expect, test } from "vitest";

import type { CustomerRechargePackage } from "./api";
import {
  DISCOUNT_INTERFACE_LABELS,
  discountInterfaceLabel,
  discountSourceLabel,
  formatDiscountZhe,
  matchingPackagesForAmount,
  packageBelowMinimum,
  packageBenefitLabel,
  packageBonusCredits,
} from "./rechargePackageDisplay";

function stubPackage(
  overrides: Partial<CustomerRechargePackage> = {},
): CustomerRechargePackage {
  return {
    id: "pkg-stub",
    name: "存根档",
    amount_fen: 10000,
    credits: 10000,
    discount_rate: null,
    discount_interfaces: [],
    sort_order: 0,
    is_active: true,
    version: 1,
    created_at: null,
    updated_at: null,
    ...overrides,
  };
}

describe("formatDiscountZhe", () => {
  test("renders 4-decimal rates as Chinese zhe", () => {
    expect(formatDiscountZhe("0.9000")).toBe("9折");
    expect(formatDiscountZhe("0.9500")).toBe("9.5折");
    expect(formatDiscountZhe("0.8500")).toBe("8.5折");
    expect(formatDiscountZhe("1.0000")).toBe("10折");
  });

  test("treats empty and out-of-range rates as no discount", () => {
    expect(formatDiscountZhe(null)).toBeNull();
    expect(formatDiscountZhe(undefined)).toBeNull();
    expect(formatDiscountZhe("")).toBeNull();
    expect(formatDiscountZhe("0")).toBeNull();
    expect(formatDiscountZhe("1.5")).toBeNull();
    expect(formatDiscountZhe("abc")).toBeNull();
  });
});

describe("discountSourceLabel", () => {
  test("translates backend tokens and keeps unknown sources visible", () => {
    expect(discountSourceLabel("recharge_package")).toBe("充值套餐");
    expect(discountSourceLabel("manual")).toBe("专项优惠");
    expect(discountSourceLabel("future_source")).toBe("future_source");
    expect(discountSourceLabel(null)).toBeNull();
    expect(discountSourceLabel(undefined)).toBeNull();
    expect(discountSourceLabel("")).toBeNull();
  });
});

describe("discountInterfaceLabel", () => {
  test("maps known keys and falls back to the raw key", () => {
    expect(discountInterfaceLabel("video_generation")).toBe("视频生成");
    expect(discountInterfaceLabel("oral")).toBe("数字人口播");
    expect(discountInterfaceLabel("future_interface")).toBe("future_interface");
  });
});

describe("packageBenefitLabel", () => {
  test("summarises scoped and whole-catalogue discounts", () => {
    expect(
      packageBenefitLabel({
        discount_rate: "0.9000",
        discount_interfaces: ["video_generation"],
      }),
    ).toBe("视频生成 9折");
    expect(
      packageBenefitLabel({
        discount_rate: "0.9000",
        discount_interfaces: ["video_generation", "oral"],
      }),
    ).toBe("视频生成、数字人口播 9折");
    // 不勾选接口 = 全部消耗。
    expect(
      packageBenefitLabel({ discount_rate: "0.9000", discount_interfaces: [] }),
    ).toBe("全部消耗 9折");
    expect(
      packageBenefitLabel({
        discount_rate: "0.9500",
        discount_interfaces: ["future_interface"],
      }),
    ).toBe("future_interface 9.5折");
  });

  test("returns null for packages without discount", () => {
    expect(
      packageBenefitLabel({ discount_rate: null, discount_interfaces: [] }),
    ).toBeNull();
  });
});

describe("packageBonusCredits", () => {
  test("reports gifted credits against the configured exchange rate", () => {
    expect(
      packageBonusCredits(
        { points_per_yuan: 100, internal_unit_price_fen: 100 },
        { amount_fen: 20000, credits: 21000 },
      ),
    ).toBe(1000);
    // 到账 = 基础换算时没有赠送，不显示赠送行。
    expect(
      packageBonusCredits(
        { points_per_yuan: 100, internal_unit_price_fen: 100 },
        { amount_fen: 20000, credits: 20000 },
      ),
    ).toBeNull();
  });

  test("falls back to the internal unit price when the rate is absent", () => {
    expect(
      packageBonusCredits(
        { internal_unit_price_fen: 50 },
        { amount_fen: 5000, credits: 110 },
      ),
    ).toBe(10);
    expect(
      packageBonusCredits(
        { points_per_yuan: 0, internal_unit_price_fen: 50 },
        { amount_fen: 5000, credits: 110 },
      ),
    ).toBe(10);
  });

  test("returns null when the wallet snapshot is unavailable", () => {
    expect(
      packageBonusCredits(null, { amount_fen: 20000, credits: 21000 }),
    ).toBeNull();
  });
});

describe("matchingPackagesForAmount", () => {
  test("matches only exact package amounts", () => {
    const list = [
      stubPackage({ id: "pkg-100", amount_fen: 10000 }),
      stubPackage({ id: "pkg-1998", amount_fen: 199800 }),
    ];
    expect(matchingPackagesForAmount(list, 10000).map((pkg) => pkg.id)).toEqual(
      ["pkg-100"],
    );
    expect(
      matchingPackagesForAmount(list, 199800).map((pkg) => pkg.id),
    ).toEqual(["pkg-1998"]);
    expect(matchingPackagesForAmount(list, 30000)).toEqual([]);
  });
});

describe("packageBelowMinimum", () => {
  test("flags packages under the effective minimum recharge amount", () => {
    expect(
      packageBelowMinimum({ amount_fen: 5000 }, { min_recharge_fen: 10000 }),
    ).toBe(true);
    expect(
      packageBelowMinimum({ amount_fen: 10000 }, { min_recharge_fen: 10000 }),
    ).toBe(false);
    expect(
      packageBelowMinimum({ amount_fen: 20000 }, { min_recharge_fen: 10000 }),
    ).toBe(false);
    // 钱包快照未就绪时不误判禁用，等快照到位后再判定。
    expect(packageBelowMinimum({ amount_fen: 5000 }, null)).toBe(false);
  });
});

describe("backend INTERFACE_KEYS contract", () => {
  test("DISCOUNT_INTERFACE_LABELS covers every backend interface key", () => {
    // 路径参数用变量而非字面量：Vite 会把 `new URL("…", import.meta.url)`
    // 静态当作 asset 引用重写；变量形式才会在运行时按 file:// 解析（沿
    // entryContract.test.ts 先例）。
    const backendPath = "../../server/app/billing_catalog.py";
    const source = readFileSync(new URL(backendPath, import.meta.url), "utf8");
    const block = source.match(/INTERFACE_KEYS[^=]*=\s*\(([^)]*)\)/);
    expect(block).not.toBeNull();
    const keys = [...(block?.[1] ?? "").matchAll(/"([^"]+)"/g)].map(
      (match) => match[1],
    );
    expect(keys.length).toBeGreaterThan(0);
    // 双向相等：后端新增接口键必须同步中文标签，前端不得自造键。
    expect([...keys].sort()).toEqual(
      Object.keys(DISCOUNT_INTERFACE_LABELS).sort(),
    );
  });
});
