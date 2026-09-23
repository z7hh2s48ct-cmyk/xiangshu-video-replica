import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearScriptRewriteIdempotencyKey,
  resolvePendingRewrite,
  type ScriptRewriteScope,
  scriptRewriteIdempotencyKey,
  shouldClearScriptRewriteIdempotencyKey,
} from "./scriptRewrite";

const scope: ScriptRewriteScope = {
  accountId: "account-a",
  projectId: "project-a",
  sourceAssetId: "source-a",
  identityId: "identity-a",
  scriptId: "script-a",
  scriptVersion: 3,
  text: "已保存正文",
};

describe("云端与本地二创任务恢复", () => {
  const pending = { scopeKey: "scope", resultText: "原有结果" };
  it("旧云端记录不能遮蔽本地已受理任务", () => {
    const accepted = { ...pending, taskId: "task-new" };
    expect(resolvePendingRewrite("scope", pending, accepted)).toBe(accepted);
    expect(resolvePendingRewrite("scope", accepted, pending)).toBe(accepted);
  });
  it("新的请求不能被旧的已受理任务覆盖", () => {
    const old = { ...pending, taskId: "old", requestKey: "a", startedAt: 1 };
    const newer = { ...pending, requestKey: "b", startedAt: 2 };
    expect(resolvePendingRewrite("scope", newer, old)).toBe(newer);
    expect(resolvePendingRewrite("scope", old, newer)).toBe(newer);
  });
  it("同一请求优先受理状态，过滤其他来源或账号的scope", () => {
    const accepted = {
      ...pending,
      taskId: "task",
      requestKey: "a",
      startedAt: 1,
    };
    const retry = { ...pending, requestKey: "a", startedAt: 2 };
    expect(resolvePendingRewrite("scope", retry, accepted)).toBe(accepted);
    expect(resolvePendingRewrite("other", retry, accepted)).toBeUndefined();
    expect(
      resolvePendingRewrite(
        "scope",
        { ...pending, scopeKey: "other" },
        accepted,
      ),
    ).toBe(accepted);
  });
});

describe("文案改写幂等键生命周期", () => {
  beforeEach(() => window.sessionStorage.clear());

  it("同一账号和完整来源稿件范围跨重挂载复用，任一输入变化即换键", () => {
    const first = scriptRewriteIdempotencyKey(scope);
    expect(scriptRewriteIdempotencyKey({ ...scope })).toBe(first);
    expect(
      scriptRewriteIdempotencyKey({ ...scope, accountId: "account-b" }),
    ).not.toBe(first);
    expect(
      scriptRewriteIdempotencyKey({ ...scope, sourceAssetId: "source-b" }),
    ).not.toBe(first);
    expect(
      scriptRewriteIdempotencyKey({ ...scope, text: "另一篇正文" }),
    ).not.toBe(first);
    expect(
      scriptRewriteIdempotencyKey({ ...scope, scriptVersion: 4 }),
    ).not.toBe(first);
  });

  it("离开输入范围后即淘汰旧键，A到B再回A会创建第三把键", () => {
    const first = scriptRewriteIdempotencyKey(scope);
    const second = scriptRewriteIdempotencyKey({
      ...scope,
      sourceAssetId: "source-b",
    });
    const third = scriptRewriteIdempotencyKey(scope);

    expect(second).not.toBe(first);
    expect(third).not.toBe(first);
    expect(third).not.toBe(second);
  });

  it("sessionStorage拒绝访问时仍由账号内存状态复用并淘汰旧范围", () => {
    const getItem = vi
      .spyOn(Storage.prototype, "getItem")
      .mockImplementation(() => {
        throw new DOMException("blocked", "SecurityError");
      });
    const setItem = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new DOMException("blocked", "SecurityError");
      });
    try {
      const first = scriptRewriteIdempotencyKey(scope);
      expect(scriptRewriteIdempotencyKey({ ...scope })).toBe(first);
      const second = scriptRewriteIdempotencyKey({
        ...scope,
        sourceAssetId: "source-without-storage",
      });
      const third = scriptRewriteIdempotencyKey(scope);
      expect(second).not.toBe(first);
      expect(third).not.toBe(first);
      expect(third).not.toBe(second);
    } finally {
      getItem.mockRestore();
      setItem.mockRestore();
    }
  });

  it("只清理仍与终态请求匹配的键", () => {
    const first = scriptRewriteIdempotencyKey(scope);
    clearScriptRewriteIdempotencyKey(scope, first);
    const second = scriptRewriteIdempotencyKey(scope);
    expect(second).not.toBe(first);
    clearScriptRewriteIdempotencyKey(scope, first);
    expect(scriptRewriteIdempotencyKey(scope)).toBe(second);
  });

  it("网络、结果未知、卸载和可重试失败保留，同步永久4xx或终态永久失败清理", () => {
    expect(shouldClearScriptRewriteIdempotencyKey(new Error("network"))).toBe(
      false,
    );
    expect(
      shouldClearScriptRewriteIdempotencyKey({
        code: "SCRIPT_REWRITE_SUBMISSION_UNCERTAIN",
        retryable: false,
      }),
    ).toBe(false);
    expect(shouldClearScriptRewriteIdempotencyKey({ retryable: true })).toBe(
      false,
    );
    expect(shouldClearScriptRewriteIdempotencyKey({ status: 503 })).toBe(false);
    expect(shouldClearScriptRewriteIdempotencyKey({ status: 422 })).toBe(true);
    expect(shouldClearScriptRewriteIdempotencyKey({ retryable: false })).toBe(
      true,
    );
  });
});
