import { expect, test } from "vitest";
import { CustomerApiError } from "../api";
import { customerErrorHint } from "./customerErrorHint";

const error = (init: ConstructorParameters<typeof CustomerApiError>[0]) =>
  new CustomerApiError(init);

test("transport 失败按 kind 判网络/超时（不能用文案关键词判——生产里文案是中文）", () => {
  const offline = customerErrorHint(
    error({ message: "网络连接失败，请检查网络", transportKind: "network" }),
  );
  expect(offline.category).toBe("network");
  expect(offline.code).toBe("NET-NOCONN");
  expect(offline.retryable).toBe(true);

  const slow = customerErrorHint(
    error({ message: "请求超时", transportKind: "timeout" }),
  );
  expect(slow.category).toBe("network");
  expect(slow.code).toBe("NET-TIMEOUT");
});

test("会话类不给重试按钮：过期/顶替/凭据吊销/账号停用都归到 AUTH-401", () => {
  for (const code of [
    "SESSION_REQUIRED",
    "SESSION_EXPIRED",
    "SESSION_REPLACED",
    "INVALID_CREDENTIALS",
  ]) {
    const hint = customerErrorHint(error({ message: "x", status: 401, code }));
    expect(hint.category).toBe("session");
    expect(hint.code).toBe("AUTH-401");
    expect(hint.retryable).toBe(false);
  }
});

test("限流与权限：429 可重试、403 不可", () => {
  const limited = customerErrorHint(
    error({ message: "x", status: 429, code: "RATE_LIMITED" }),
  );
  expect(limited.category).toBe("rate-limit");
  expect(limited.code).toBe("LIMIT-429");
  expect(limited.retryable).toBe(true);

  const forbidden = customerErrorHint(error({ message: "x", status: 403 }));
  expect(forbidden.category).toBe("permission");
  expect(forbidden.code).toBe("AUTH-403");
  expect(forbidden.retryable).toBe(false);
});

test("503 判服务端，可重试并提示带上问题编号", () => {
  const hint = customerErrorHint(error({ message: "x", status: 503 }));
  expect(hint.category).toBe("server");
  expect(hint.code).toBe("SVC-503");
  expect(hint.retryable).toBe(true);
  expect(hint.hint).toContain("问题编号");
});

test("非 CustomerApiError（浏览器 TypeError）走兜底：文案里带网络关键词仍判网络", () => {
  expect(customerErrorHint(new TypeError("Failed to fetch")).category).toBe(
    "network",
  );
  expect(customerErrorHint(new TypeError("网络不可用")).category).toBe(
    "network",
  );
});

test("kind 表覆盖 api.ts 的每一种 kind，不留未分类的口子", () => {
  // 与 api.ts 的 CustomerApiErrorKind 逐一对齐；新增 kind 时这里会红，
  // 同时 KIND_HINTS 的 Record<CustomerApiErrorKind, …> 类型也会编译不过。
  //
  // 注意用**裸对象**而不是 CustomerApiError：后者的构造函数收的是 `transportKind`，
  // 传 `kind` 会被忽略，getter 再从 status/code 推回 `unknown`——那样这轮遍历会
  // 全部命中兜底却依然全绿（本条测试第一版就是这么假绿的）。
  const everyKind = [
    "session-expired",
    "session-replaced",
    "credential-revoked",
    "credential-invalid",
    "code-suspended",
    "code-revoked",
    "other-device-online",
    "idempotency-conflict",
    "rate-limited",
    "bad-request",
    "unauthorized",
    "forbidden",
    "not-found",
    "conflict",
    "service-unavailable",
    "network",
    "timeout",
    "unknown",
  ];
  for (const kind of everyKind) {
    const hint = customerErrorHint({ message: "x", kind });
    expect(hint.hint).not.toBe("");
    // 码是「分类前缀-后缀」：网络类是 NET-NOCONN / NET-TIMEOUT，其余是状态号。
    expect(hint.code).toMatch(/^[A-Z]+-[A-Z0-9]+$/);
    expect(typeof hint.retryable).toBe("boolean");
  }
  // 表里没有的 kind 才落到兜底（这里 status 也缺，所以是 APP-000）。
  expect(customerErrorHint({ message: "x", kind: "not-a-kind" }).code).toBe(
    "APP-000",
  );
  expect(customerErrorHint({ message: "x", kind: "constructor" }).code).toBe(
    "APP-000",
  );
});

test("请求不合格/目标不存在不给重试，冲突类可以重试", () => {
  for (const kind of ["bad-request", "not-found"]) {
    expect(customerErrorHint({ message: "x", kind }).retryable).toBe(false);
  }
  expect(customerErrorHint({ message: "x", kind: "conflict" }).retryable).toBe(
    true,
  );
});

test("认不出的错误退到通用提示，且不抛出", () => {
  expect(customerErrorHint(null).category).toBe("unknown");
  expect(customerErrorHint(new Error("boom")).category).toBe("unknown");
  expect(customerErrorHint("字符串").category).toBe("unknown");
  expect(customerErrorHint(error({ message: "x", status: 418 })).code).toBe(
    "APP-418",
  );
});
