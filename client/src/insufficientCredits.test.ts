import { describe, expect, test, vi } from "vitest";
import {
  isInsufficientCredits,
  openWalletIfInsufficientCredits,
} from "./insufficientCredits";

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

describe("openWalletIfInsufficientCredits", () => {
  test("透传服务端文案并打开钱包侧栏", () => {
    const notify = vi.fn();
    const openWallet = vi.fn();
    const error = Object.assign(new Error("积分不足，本次需要 14 积分。"), {
      code: "INSUFFICIENT_CREDITS",
    });

    const handled = openWalletIfInsufficientCredits(error, {
      notify,
      openWallet,
    });

    expect(handled).toBe(true);
    expect(notify).toHaveBeenCalledWith("积分不足，本次需要 14 积分。");
    expect(openWallet).toHaveBeenCalledTimes(1);
  });

  test("其他错误不触发钱包侧栏，交回调用方的通用失败提示", () => {
    const notify = vi.fn();
    const openWallet = vi.fn();

    const handled = openWalletIfInsufficientCredits(new Error("网络连接中断"), {
      notify,
      openWallet,
    });

    expect(handled).toBe(false);
    expect(notify).not.toHaveBeenCalled();
    expect(openWallet).not.toHaveBeenCalled();
  });

  test("余额不足但错误缺 message 时使用兜底文案", () => {
    const notify = vi.fn();
    const openWallet = vi.fn();

    const handled = openWalletIfInsufficientCredits(
      { code: "INSUFFICIENT_CREDITS" },
      { notify, openWallet },
    );

    expect(handled).toBe(true);
    expect(notify).toHaveBeenCalledWith("余额不足，请先充值后再试。");
  });
});
