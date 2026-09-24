import { beforeEach, describe, expect, it } from "vitest";
import {
  clearViralImportIdempotencyKey,
  shouldClearViralImportIdempotencyKey,
  viralImportIdempotencyKey,
} from "./viralImport";

describe("爆款导入幂等键生命周期", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it("按账户和用途隔离，同一操作重挂载复用", () => {
    const copyA = viralImportIdempotencyKey(
      "customer-a",
      "douyin:native-1:copy",
    );

    expect(
      viralImportIdempotencyKey("customer-a", "douyin:native-1:copy"),
    ).toBe(copyA);
    expect(
      viralImportIdempotencyKey("customer-b", "douyin:native-1:copy"),
    ).not.toBe(copyA);
    expect(
      viralImportIdempotencyKey("customer-a", "douyin:native-1:replica"),
    ).not.toBe(copyA);
  });

  it("只清除仍匹配当前请求的键，迟到请求不能删除新操作", () => {
    const action = "wechat_channels:native-2:replica";
    const first = viralImportIdempotencyKey("customer-a", action);
    clearViralImportIdempotencyKey("customer-a", action, first);
    const second = viralImportIdempotencyKey("customer-a", action);
    expect(second).not.toBe(first);

    clearViralImportIdempotencyKey("customer-a", action, first);
    expect(viralImportIdempotencyKey("customer-a", action)).toBe(second);
  });

  it("只把明确永久或客户端失效错误判为需要新键", () => {
    expect(shouldClearViralImportIdempotencyKey({ retryable: false })).toBe(
      true,
    );
    expect(shouldClearViralImportIdempotencyKey({ status: 404 })).toBe(true);
    expect(shouldClearViralImportIdempotencyKey({ retryable: true })).toBe(
      false,
    );
    expect(shouldClearViralImportIdempotencyKey(new Error("network"))).toBe(
      false,
    );
  });
});
