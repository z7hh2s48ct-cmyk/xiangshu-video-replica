import { describe, expect, test } from "vitest";
import { isInsufficientCredits } from "./insufficientCredits";

describe("isInsufficientCredits", () => {
  test("recognises the server's 402 contract", () => {
    expect(isInsufficientCredits({ code: "INSUFFICIENT_CREDITS" })).toBe(true);
    const error = Object.assign(new Error("积分不足，本次需要 14 积分。"), {
      code: "INSUFFICIENT_CREDITS",
    });
    expect(isInsufficientCredits(error)).toBe(true);
  });

  test("does not fire on other billing rejections", () => {
    // These reach the same call sites and must keep their own handling.
    expect(isInsufficientCredits({ code: "BILLING_REQUEST_CONFLICT" })).toBe(
      false,
    );
    expect(isInsufficientCredits({ code: "BILLABLE_DURATION_REQUIRED" })).toBe(
      false,
    );
  });

  test("tolerates the shapes a rejected request can actually take", () => {
    expect(isInsufficientCredits(null)).toBe(false);
    expect(isInsufficientCredits(undefined)).toBe(false);
    expect(isInsufficientCredits(new Error("network down"))).toBe(false);
    expect(isInsufficientCredits("INSUFFICIENT_CREDITS")).toBe(false);
    expect(isInsufficientCredits({ code: 402 })).toBe(false);
  });
});
