import { expect, test } from "vitest";
import { costInCredits, creditsToCost } from "./billingAmounts";

test("round-trips fractional costs through independently configured exchange ratios", () => {
  for (const rate of [1, 3, 100, 1000000])
    for (const amount of ["0", "0.000001", "0.000125", "12345678.123456"])
      expect(creditsToCost(costInCredits(amount, rate), rate)).toEqual({
        value: amount,
        exact: true,
      });
});

test("rounds non-terminating cost conversion only at the stored precision", () => {
  expect(costInCredits("12.5", 200)).toBe("25");
  expect(creditsToCost("1", 3)).toEqual({ value: "33.333333", exact: false });
  expect(creditsToCost("2", 3)).toEqual({ value: "66.666667", exact: false });
});

test("rejects invalid, too-small costs and unavailable conversion rates", () => {
  for (const input of ["-1", "NaN", "Infinity", "1e3", "0.000000000001"])
    expect(() => creditsToCost(input, 100)).toThrow();
  expect(() => creditsToCost("1", 0)).toThrow("充值换算");
  expect(costInCredits(null, 100)).toBe("");
  expect(costInCredits("1", null)).toBe("");
});
