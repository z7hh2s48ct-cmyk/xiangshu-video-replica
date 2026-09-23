/**
 * 客户侧报错的分类提示（审计 P1 清单 #8 的「错误分类」与「错误码」两半）。
 *
 * 目标不是给每个错误编一个独家码（那要先有一套码表），而是让客户一眼知道**该做什么**：
 * 检查网络、稍后重试、重新登录，还是别再点、先找客服。
 *
 * **分类以 `CustomerApiError.kind` 为准**：`api.ts` 的 FE-01 已经把每个
 * 401/403/409/429 解析成唯一状态（注释原文「the UI never has to parse a raw status
 * line」）。早先这版按 message 里的 ASCII 关键词判网络，而真实的 transport 错误带的是
 * 中文文案 + `kind: "network"`，结果网络错误在生产里全落到「未知」。
 */
import type { CustomerApiErrorKind } from "../api";

export type CustomerErrorCategory =
  | "network"
  | "session"
  | "permission"
  | "rate-limit"
  | "server"
  | "unknown";

export type CustomerErrorHint = {
  category: CustomerErrorCategory;
  /** 一句话，直接显示给客户。 */
  hint: string;
  /** 值不值得给「重试」按钮——网络/服务端/限流值得，权限类重试没用。 */
  retryable: boolean;
  /**
   * 给客户看的错误码（分类前缀 + 状态，如 `NET-NOCONN` / `AUTH-401`）。
   *
   * **纯前端映射**：服务端的内部 `code`（`RATE_LIMITED` 这类）不动，也不新增后端码表。
   * 客户真正需要的是「这错我自己能解决还是要找客服」——分类提示回答了；码只是让客服与
   * 工单对号入座的短标签，精确定位仍靠问题编号（`requestId`）。
   */
  code: string;
};

type ErrorLike = {
  status?: number | null;
  code?: string | null;
  kind?: string | null;
  message?: string;
};

const SERVICE_UNAVAILABLE: CustomerErrorHint = {
  category: "server",
  code: "SVC-503",
  hint: "平台这边出了点问题，稍后重试即可；若持续出现请把问题编号发给客服。",
  retryable: true,
};

const NETWORK_HINT: CustomerErrorHint = {
  category: "network",
  code: "NET-NOCONN",
  hint: "网络请求没有成功发出，请检查网络连接后重试。",
  retryable: true,
};

/**
 * kind → 分类结果。
 *
 * 类型写成 `Record<…>` 而不是 `Record<string, …>`：`api.ts` 新增一个 kind 时这里
 * **编译不过**，逼着补一行，不会静默落回「未知」。这正是本条评审意见的教训——上一版
 * 按 message 里的 ASCII 关键词猜网络，真实的 transport 错误带的是中文文案 + `kind`，
 * 于是网络错误在生产里全落到「未知」而单测全绿。
 *
 * `unknown` 故意**不在表里**（所以是 `Exclude<…, "unknown">`）：它就是兜底本身，由
 * `fallbackHint` 处理——那里还能从 `status` 里挤出 `APP-418` 这种更具体的码，静态写
 * 一条 `APP-000` 反而丢信息。
 */
const KIND_HINTS: Record<
  Exclude<CustomerApiErrorKind, "unknown">,
  CustomerErrorHint
> = {
  network: NETWORK_HINT,
  timeout: {
    category: "network",
    code: "NET-TIMEOUT",
    hint: "请求超时了，请检查网络后重试。",
    retryable: true,
  },
  "rate-limited": {
    category: "rate-limit",
    code: "LIMIT-429",
    hint: "操作有点频繁，稍等一会儿再试。",
    retryable: true,
  },
  // 会话类：重试没用，得重新登录。凭据被吊销/账号被停用对客户是同一件事。
  "session-expired": {
    category: "session",
    code: "AUTH-401",
    hint: "登录状态已失效，请重新登录。",
    retryable: false,
  },
  "session-replaced": {
    category: "session",
    code: "AUTH-401",
    hint: "账号在另一台设备上登录了，请重新登录。",
    retryable: false,
  },
  "credential-revoked": {
    category: "session",
    code: "AUTH-401",
    hint: "账号登录已被管理员撤销，请联系管理员。",
    retryable: false,
  },
  "credential-invalid": {
    category: "session",
    code: "AUTH-401",
    hint: "登录凭据无效，请重新登录。",
    retryable: false,
  },
  "code-suspended": {
    category: "session",
    code: "AUTH-401",
    hint: "账号已被暂停，请联系管理员。",
    retryable: false,
  },
  "code-revoked": {
    category: "session",
    code: "AUTH-401",
    hint: "账号已被停用，请联系管理员。",
    retryable: false,
  },
  unauthorized: {
    category: "session",
    code: "AUTH-401",
    hint: "登录状态已失效，请重新登录。",
    retryable: false,
  },
  forbidden: {
    category: "permission",
    code: "AUTH-403",
    hint: "当前账号没有这项权限，如需开通请联系管理员。",
    retryable: false,
  },
  "service-unavailable": SERVICE_UNAVAILABLE,
  "idempotency-conflict": {
    category: "unknown",
    code: "APP-409",
    hint: "这次提交的内容已经变化，请重新提交。",
    retryable: true,
  },
  "other-device-online": {
    category: "unknown",
    code: "APP-409",
    hint: "另一台设备正在使用这个账号，切换会把那台挤下线。",
    retryable: false,
  },
  // 请求本身不合格/目标不存在：重试同样的内容不会有不同结果，不给重试按钮。
  "bad-request": {
    category: "unknown",
    code: "APP-400",
    hint: "这次提交没有通过校验，请检查填写的内容后重新提交。",
    retryable: false,
  },
  "not-found": {
    category: "unknown",
    code: "APP-404",
    hint: "要找的内容不存在或已被删除，请刷新后重试。",
    retryable: false,
  },
  conflict: {
    category: "unknown",
    code: "APP-409",
    hint: "这条数据已经变了，请刷新后重新操作。",
    retryable: true,
  },
};

/** 没有 `kind` 时的兜底（非 CustomerApiError：浏览器抛的 TypeError 等）。 */
function fallbackHint(
  candidate: ErrorLike,
  status: number | null,
): CustomerErrorHint {
  const message = candidate.message ?? "";
  if (
    status === 0 ||
    (!status && /failed to fetch|network|load failed|网络/i.test(message))
  ) {
    return NETWORK_HINT;
  }
  if (candidate.code === "RATE_LIMITED") {
    return KIND_HINTS["rate-limited"];
  }
  if (status !== null && status >= 500) {
    return { ...SERVICE_UNAVAILABLE, code: `SVC-${status}` };
  }
  return {
    category: "unknown",
    code: status === null ? "APP-000" : `APP-${status}`,
    hint: "操作没有完成，请重试；若持续出现请联系客服。",
    retryable: true,
  };
}

export function customerErrorHint(error: unknown): CustomerErrorHint {
  const candidate = (error ?? {}) as ErrorLike;
  const kind = typeof candidate.kind === "string" ? candidate.kind : "";
  const status = typeof candidate.status === "number" ? candidate.status : null;
  // 只有确实是表里的 kind 才用表——任意字符串都不该把 `Object.hasOwn` 之外的路径
  // 当成命中（`kind` 来自运行时对象，不是编译期类型）。
  const mapped = Object.hasOwn(KIND_HINTS, kind)
    ? KIND_HINTS[kind as Exclude<CustomerApiErrorKind, "unknown">]
    : undefined;
  return mapped ?? fallbackHint(candidate, status);
}
