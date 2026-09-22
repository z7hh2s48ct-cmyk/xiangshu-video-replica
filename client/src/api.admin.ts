// T32 — admin activation frontend adapter.
//
// The customer V3 control plane lives behind the T09 admin session
// (HttpOnly `admin_session` cookie on /api/control) plus the per-session CSRF
// token returned by the login/session response. The CSRF token is held in a
// module-level variable only: it never reaches localStorage, sessionStorage,
// or any other browser persistence. The server can deterministically restore
// it from the HttpOnly session cookie after a page refresh.
//
// Every write goes out with the dev-doc §15 admin write contract:
// `confirm: true`, a non-blank `reason`, and a unique `Idempotency-Key`
// header, alongside the `X-Admin-CSRF` header; responses carry the audit
// `request_id` that the pages surface to the operator.

import {
  type CustomerCreditConfig,
  type CustomerPricing,
  type CustomerRechargePackage,
  clearAdminCsrfToken,
  getAdminCsrfToken,
  notifyAdminSessionExpired,
  resolveApiBaseUrl,
  setAdminCsrfToken,
} from "./api";

export async function getCustomerPricing(): Promise<CustomerPricing> {
  const response = await requestControl(
    "/api/control/settings/customer-pricing",
    {},
  );
  if (!response.ok)
    throw await parseActivationError(response, "读取积分价格失败");
  return (await response.json()) as CustomerPricing;
}

export function updateCustomerPricing(
  config: CustomerCreditConfig,
  expectedVersion: number,
  reason: string,
  idempotencyKey: string,
): Promise<CustomerPricing> {
  return adminWrite<CustomerPricing>(
    "/api/control/settings/customer-pricing",
    { config, expected_version: expectedVersion },
    reason,
    "保存积分价格失败",
    idempotencyKey,
    "PUT",
  );
}

/** 充值套餐的写入草稿（管理端新建/更新共用；客户侧类型同名复用）。 */
export type RechargePackageDraft = {
  name: string;
  amount_fen: number;
  credits: number;
  /** 折扣率 4 位小数字符串（如 "0.9000"）；null = 无折扣档位。 */
  discount_rate: string | null;
  discount_interfaces: string[];
  sort_order: number;
  is_active: boolean;
};

/** 管理端套餐列表（含停用行；GET /api/control/settings/recharge-packages）。 */
export async function listRechargePackages(): Promise<
  CustomerRechargePackage[]
> {
  const body = await adminRead<{ items: CustomerRechargePackage[] }>(
    "/api/control/settings/recharge-packages",
    "读取充值套餐失败",
  );
  return body.items;
}

/** 新建套餐（POST；写契约 + 幂等快照 + 审计）。 */
export function createRechargePackage(
  draft: RechargePackageDraft,
  reason: string,
  idempotencyKey?: string,
): Promise<CustomerRechargePackage> {
  return adminWrite<CustomerRechargePackage>(
    "/api/control/settings/recharge-packages",
    { ...draft },
    reason,
    "新建充值套餐失败",
    idempotencyKey,
    "POST",
  );
}

/** 更新套餐（PUT；乐观锁 expected_version 不符 → 409）。 */
export function updateRechargePackage(
  packageId: string,
  draft: RechargePackageDraft,
  expectedVersion: number,
  reason: string,
  idempotencyKey?: string,
): Promise<CustomerRechargePackage> {
  return adminWrite<CustomerRechargePackage>(
    `/api/control/settings/recharge-packages/${encodeURIComponent(packageId)}`,
    { ...draft, expected_version: expectedVersion },
    reason,
    "更新充值套餐失败",
    idempotencyKey,
    "PUT",
  );
}

import type { BillingSettings, ControlSettings, CustomerApiKey } from "./api";
import type { components } from "./generated/api";

export type CustomerPaymentSettings = Pick<ControlSettings, "billing" | "zpay">;
export async function getCustomerPaymentSettings(): Promise<CustomerPaymentSettings> {
  const response = await requestControl(
    "/api/control/settings/customer-payments",
    {},
  );
  if (!response.ok)
    throw await parseActivationError(response, "读取支付配置失败");
  return response.json();
}
export type WechatSelfCheckResult = {
  ok: boolean;
  code: string | null;
  message: string;
  platform_certificates?: number;
};
/** 真实调一次微信 /v3/certificates 验证已保存的商户三件套（只读探测）。 */
export async function selfCheckWechatNative(): Promise<WechatSelfCheckResult> {
  const response = await requestControl(
    "/api/control/settings/customer-payments/wechat-native/self-check",
    { method: "POST" },
  );
  if (!response.ok) throw await parseActivationError(response, "凭据自检失败");
  return response.json();
}
export function updateCustomerPaymentBilling(
  input: Omit<BillingSettings, "charged_unit_price_fen">,
  reason: string,
  key?: string,
): Promise<BillingSettings> {
  return adminWrite(
    "/api/control/settings/customer-payments/billing",
    input,
    reason,
    "保存支付配置失败",
    key,
    "PATCH",
  );
}
export function updateCustomerPaymentZPay(
  input: {
    pid: string;
    key?: string;
    enabled_channels: Array<"alipay" | "wxpay">;
  },
  reason: string,
  key?: string,
): Promise<ControlSettings["zpay"]> {
  return adminWrite(
    "/api/control/settings/customer-payments/zpay",
    input,
    reason,
    "保存支付配置失败",
    key,
    "PATCH",
  );
}
export type AccountCreditSummary = {
  user_id: string;
  available_credits: number;
  reserved_credits: number;
  total_consumed_credits: number;
  software_consumed_credits: number;
  other_consumed_credits: number;
  tokens: CustomerApiKey[];
};
export function reconcileAccountRecharge(
  userId: string,
  orderNo: string,
  reason: string,
  key: string,
): Promise<unknown> {
  return adminWrite(
    `/api/control/customers/${encodeURIComponent(userId)}/recharge-orders/${encodeURIComponent(orderNo)}/reconcile`,
    {},
    reason,
    "充值订单核验失败",
    key,
  );
}
export async function getAccountCreditSummary(
  userId: string,
): Promise<AccountCreditSummary> {
  const response = await requestControl(
    `/api/control/customers/${encodeURIComponent(userId)}/account-summary`,
    {},
  );
  if (!response.ok)
    throw await parseActivationError(response, "读取账号查账失败");
  return response.json();
}

const DEFAULT_TIMEOUT_MS = 5_000;
const CSRF_HEADER = "X-Admin-CSRF";
const IDEMPOTENCY_KEY_HEADER = "Idempotency-Key";

// A8（2026-09-02 评估）：管理端错误类统一为一个实现。历史上五个域各复制
// 了同一个类（AdminCustomerError / AdminDeviceError / AdminAdjustmentError /
// AdminSessionError / AdminAuditError），语义完全相同；现在它们都是
// AdminControlError 的别名，`instanceof` 在所有调用点继续成立。
export class AdminControlError extends Error {
  readonly status: number | undefined;
  readonly code: string | undefined;

  constructor(message: string, status?: number, code?: string) {
    super(message);
    this.name = "AdminControlError";
    this.status = status;
    this.code = code;
  }
}

export class AdminActivationError extends AdminControlError {
  constructor(message: string, status?: number, code?: string) {
    super(message, status, code);
    this.name = "AdminActivationError";
  }
}

/** Drop the in-memory session state (logout, expiry, tests). */
export function clearAdminActivationSession(): void {
  clearAdminCsrfToken();
}

function requireCsrfToken(): string {
  const token = getAdminCsrfToken();
  if (!token) {
    throw new AdminActivationError(
      "管理登录令牌缺失，请重新登录后再执行写操作",
      401,
      "ADMIN_CSRF_UNAVAILABLE",
    );
  }
  return token;
}

function newIdempotencyKey(): string {
  const cryptoRef = globalThis.crypto;
  if (cryptoRef && typeof cryptoRef.randomUUID === "function") {
    return cryptoRef.randomUUID();
  }
  return `idem-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
}

/**
 * Mint an idempotency key for one logical submission.
 *
 * The server (T12) keeps a replay snapshot keyed by (actor, route, key digest)
 * so that a retry after an ambiguous failure (timeout / network error) replays
 * the original response instead of creating a second batch. Pages therefore
 * mint a key on the first submit attempt and reuse it for retries of that
 * same submission; a fresh key is minted only after the previous submission
 * reached a definitive outcome.
 */
export function createIdempotencyKey(): string {
  return newIdempotencyKey();
}

function apiBaseUrl(): string {
  return resolveApiBaseUrl(
    import.meta.env.VITE_API_BASE_URL,
    import.meta.env.PROD,
    window.location,
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function parseActivationError(
  response: Response,
  fallback: string,
): Promise<AdminActivationError> {
  let code: string | undefined;
  let message: string | undefined;
  try {
    const payload: unknown = await response.json();
    if (isRecord(payload) && isRecord(payload.detail)) {
      code =
        typeof payload.detail.code === "string"
          ? payload.detail.code
          : undefined;
      message =
        typeof payload.detail.message === "string"
          ? payload.detail.message
          : undefined;
    } else if (isRecord(payload) && typeof payload.detail === "string") {
      message = payload.detail;
    }
  } catch {
    // A non-JSON body must not hide the HTTP status.
  }
  const text = message?.trim()
    ? `${fallback}：${message}（${response.status}）`
    : `${fallback}（${response.status}）`;
  return new AdminActivationError(text, response.status, code);
}

async function requestControl(
  path: string,
  init: RequestInit & { headers?: Record<string, string> },
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<Response> {
  const csrfAtStart = getAdminCsrfToken();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  const headers: Record<string, string> = { ...(init.headers ?? {}) };
  if (init.body && !(init.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  try {
    const response = await fetch(`${apiBaseUrl()}${path}`, {
      ...init,
      headers,
      credentials: "include",
      signal: controller.signal,
    });
    // Authentication failures are handled by the login/restore form itself.
    // Protected reads and writes must also close the active management UI.
    if (
      response.status === 401 &&
      path !== "/api/control/admin/session/password" &&
      path !== "/api/control/admin/session/exchange" &&
      !(path === "/api/control/admin/session" && init.method === "GET")
    ) {
      notifyAdminSessionExpired(csrfAtStart);
    }
    return response;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new AdminActivationError("请求超时，请重试");
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function adminWrite<T>(
  path: string,
  fields: Record<string, unknown>,
  reason: string,
  fallback: string,
  idempotencyKey?: string,
  method: "POST" | "PATCH" | "PUT" | "DELETE" = "POST",
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const csrf = requireCsrfToken();
  const response = await requestControl(
    path,
    {
      method,
      headers: {
        [CSRF_HEADER]: csrf,
        [IDEMPOTENCY_KEY_HEADER]: idempotencyKey ?? newIdempotencyKey(),
      },
      body: JSON.stringify({ ...fields, confirm: true, reason }),
    },
    timeoutMs,
  );
  if (!response.ok) {
    throw await parseActivationError(response, fallback);
  }
  return (await response.json()) as T;
}

export async function adminRead<T>(path: string, fallback: string): Promise<T> {
  const response = await requestControl(path, { method: "GET" });
  if (!response.ok) {
    throw await parseActivationError(response, fallback);
  }
  return response.json() as Promise<T>;
}

export interface AdminRechargeOrder {
  id: string;
  user_id: string;
  username: string;
  display_name?: string;
  order_no: string;
  status: "PENDING" | "PAID" | "FAILED" | "CLOSED";
  amount_fen: number;
  credits: number;
  channel: string;
  provider: string;
  /** ZPay settles here; a WeChat Native order keeps it null by constraint. */
  provider_trade_no: string | null;
  /** WeChat Native's trade reference; null for every other provider. */
  transaction_id: string | null;
  created_at: string;
  paid_at: string | null;
}

export type AdminWalletTransaction =
  components["schemas"]["ControlWalletTransaction"];

interface AdminListPage<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

// Sub-account related types
export interface SubAccountListItem {
  id: string;
  username: string;
  display_name: string;
  account_type: "SUB";
  parent_user_id: string;
  is_active: boolean;
  created_at: string;
  updated_at: string | null;
}

export interface CreateSubAccountRequest {
  username: string;
  display_name: string;
  parent_user_id: string;
  reason: string;
}

export interface UpdateSubAccountRequest {
  display_name?: string;
  is_active?: boolean;
  reason: string;
  request_id?: string;
}

export interface SubAccountListResponse {
  sub_accounts: SubAccountListItem[];
  total_count: number;
}

export async function listMasterAccounts(limit = 100, offset = 0) {
  return adminRead<{
    items: CustomerListItem[];
    total: number;
    limit: number;
    offset: number;
  }>(
    `/api/admin/customers?limit=${limit}&offset=${offset}`,
    "读取母账号列表失败",
  );
}

export async function listSubAccounts(
  parentUserId: string,
): Promise<SubAccountListResponse> {
  return adminRead<SubAccountListResponse>(
    `/api/admin/sub-accounts?parent_user_id=${encodeURIComponent(parentUserId)}`,
    "读取子账号列表失败",
  );
}

export async function createSubAccount(
  input: CreateSubAccountRequest,
): Promise<SubAccountListItem> {
  return adminWrite<SubAccountListItem>(
    "/api/admin/sub-accounts",
    {
      username: input.username,
      display_name: input.display_name,
      parent_user_id: input.parent_user_id,
      confirm: true,
      reason: input.reason,
    },
    input.reason || "创建子账号",
    "创建子账号失败",
    undefined,
    "POST",
  );
}

export async function updateSubAccount(
  subAccountId: string,
  input: Partial<UpdateSubAccountRequest>,
  reason: string = input.reason || "编辑子账号信息",
): Promise<SubAccountListItem> {
  const fields: Record<string, unknown> = {};
  if (input.display_name !== undefined)
    fields.display_name = input.display_name;
  if (input.is_active !== undefined) fields.is_active = input.is_active;
  if (input.reason !== undefined) {
    fields.reason = input.reason;
  }

  return adminWrite<SubAccountListItem>(
    `/api/admin/sub-accounts/${encodeURIComponent(subAccountId)}`,
    fields,
    reason,
    "保存子账号信息失败",
    undefined,
    "PATCH",
  );
}

export async function deleteSubAccount(
  subAccountId: string,
): Promise<{ deleted: boolean }> {
  return adminWrite(
    `/api/admin/sub-accounts/${encodeURIComponent(subAccountId)}`,
    {},
    "删除子账号",
    "删除子账号失败",
    undefined,
    "DELETE",
  );
}

export async function listAdminRechargeOrders(
  options: {
    status?: string;
    userId?: string;
    username?: string;
    channel?: string;
    createdFrom?: string;
    createdTo?: string;
    limit?: number;
    offset?: number;
  } = {},
): Promise<AdminListPage<AdminRechargeOrder>> {
  const params = new URLSearchParams({
    limit: String(options.limit ?? 50),
    offset: String(options.offset ?? 0),
  });
  if (options.status) params.set("status", options.status);
  if (options.userId) params.set("user_id", options.userId);
  if (options.username) params.set("username", options.username);
  if (options.channel) params.set("channel", options.channel);
  if (options.createdFrom) params.set("created_from", options.createdFrom);
  if (options.createdTo) params.set("created_to", options.createdTo);
  return adminRead(
    options.userId
      ? `/api/control/customers/${encodeURIComponent(options.userId)}/recharge-orders?${params}`
      : `/api/control/recharge-orders?${params}`,
    "读取充值订单失败",
  );
}

export async function listAdminWalletTransactions(
  options: {
    userId?: string;
    username?: string;
    type?: string;
    createdFrom?: string;
    createdTo?: string;
    limit?: number;
    offset?: number;
  } = {},
): Promise<AdminListPage<AdminWalletTransaction>> {
  const params = new URLSearchParams({
    limit: String(options.limit ?? 50),
    offset: String(options.offset ?? 0),
  });
  if (options.userId) params.set("user_id", options.userId);
  if (options.username) params.set("username", options.username);
  if (options.type) params.set("type", options.type);
  if (options.createdFrom) params.set("created_from", options.createdFrom);
  if (options.createdTo) params.set("created_to", options.createdTo);
  return adminRead(
    options.userId
      ? `/api/control/customers/${encodeURIComponent(options.userId)}/wallet-transactions?${params}`
      : `/api/control/wallet-transactions?${params}`,
    "读取额度流水失败",
  );
}

// ---------------------------------------------------------------------------
// Session (T09)
// ---------------------------------------------------------------------------

export type AdminActorInfo = {
  user_id: string;
  username: string;
  display_name: string;
  role: string;
};

export type AdminExchangeResult = {
  session_id: string;
  expires_at: string;
  csrf_token: string;
  actor: AdminActorInfo;
  auth_method: "exchange" | "password";
};

export type AdminSessionInfo = {
  session_id: string;
  expires_at: string;
  last_activity_at: string;
  csrf_token: string | null;
  actor: AdminActorInfo;
  auth_method: "exchange" | "password";
};

export async function loginAdminWithPassword(
  username: string,
  password: string,
): Promise<AdminExchangeResult> {
  const response = await requestControl("/api/control/admin/session/password", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) {
    throw await parseActivationError(response, "后台登录失败");
  }
  const payload = (await response.json()) as AdminExchangeResult;
  setAdminCsrfToken(payload.csrf_token);
  return payload;
}

export async function exchangeAdminSession(
  credential: string,
): Promise<AdminExchangeResult> {
  const response = await requestControl("/api/control/admin/session/exchange", {
    method: "POST",
    body: JSON.stringify({ credential }),
  });
  if (!response.ok) {
    throw await parseActivationError(response, "管理登录失败");
  }
  const payload = (await response.json()) as AdminExchangeResult;
  // Memory only — never persisted (No-Go red line).
  setAdminCsrfToken(payload.csrf_token);
  return payload;
}

export async function fetchAdminSession(): Promise<AdminSessionInfo> {
  const response = await requestControl("/api/control/admin/session", {
    method: "GET",
  });
  if (!response.ok) {
    throw await parseActivationError(response, "读取管理会话失败");
  }
  const payload = (await response.json()) as AdminSessionInfo;
  if (payload.csrf_token) {
    setAdminCsrfToken(payload.csrf_token);
  } else {
    clearAdminCsrfToken();
  }
  return payload;
}

export async function recoverAdminPassword(password: string): Promise<void> {
  const csrf = requireCsrfToken();
  const response = await requestControl("/api/control/admin/password", {
    method: "PUT",
    headers: { [CSRF_HEADER]: csrf },
    body: JSON.stringify({ password }),
  });
  if (!response.ok) {
    throw await parseActivationError(response, "设置管理员密码失败");
  }
  clearAdminCsrfToken();
}

export async function deleteAdminSession(): Promise<void> {
  const csrf = requireCsrfToken();
  const response = await requestControl("/api/control/admin/session", {
    method: "DELETE",
    headers: { [CSRF_HEADER]: csrf },
  });
  if (!response.ok) {
    throw await parseActivationError(response, "退出管理登录失败");
  }
  clearAdminCsrfToken();
}

// ---------------------------------------------------------------------------
// Activation codes (T12)
// ---------------------------------------------------------------------------

export type ActivationCodeListItem = {
  code_id: string;
  batch_id: string;
  masked_code: string;
  status: string;
  bound_user_id: string | null;
  bound_username: string | null;
  issued_at: string | null;
  expires_at?: string;
  created_at?: string;
  archived_at: string | null;
  devices: ActivationCodeDevice[];
  pending_pairings: ActivationCodePendingPairing[];
};

export type ActivationCodePendingPairing = {
  pairing_request_id: string;
  display_name: string;
  platform: string;
  status: "PENDING" | "APPROVED";
  created_at: string;
  expires_at: string;
};

export type ActivationCodeDevice = {
  device_id: string;
  slot_no: number;
  display_name: string | null;
  platform: string;
  status: string;
  bound_at: string | null;
  last_active_at: string | null;
  unbound_at: string | null;
  revoked_at: string | null;
};

export type ActivationCodePage = {
  items: ActivationCodeListItem[];
  total: number;
  limit: number;
  offset: number;
};

export type ActivationBatchResult = {
  batch_id: string;
  name: string;
  face_value_fen: number;
  unit_price_fen_snapshot: number;
  credits_snapshot: number;
  quantity: number;
  activation_expires_at: string;
  status: string;
  created_by_user_id: string;
  request_id: string;
};

export type ActivationGenerateResult = {
  batch_id: string;
  export_id: string;
  expires_at: string;
  codes: Array<{ code_id: string; masked_code: string }>;
  request_id: string;
};

export type ActivationDownloadResult = {
  export_id: string;
  batch_id: string;
  codes: string[];
  downloaded_at: string;
  request_id: string;
};

export type ActivationDeliverResult = {
  code_id: string;
  status: string;
  delivery_id: string;
  request_id: string;
};

export type ActivationCodeMutationResult = {
  code_id: string;
  status: string;
  request_id: string;
};

export type ActivationCodeArchiveResult = {
  code_id: string;
  archived_at: string;
  request_id: string;
};

export type PairingAdminResult = {
  pairing_id: string;
  status: string;
  outcome?: string;
  replaced_device_id?: string;
  request_id: string;
};

export type ActivationCodeRevealResult = {
  code_id: string;
  activation_code: string;
  masked_code: string;
  request_id: string;
};

export async function createActivationCodeBatch(
  input: {
    name: string;
    face_value_fen: number;
    credits: number;
    quantity: number;
    activation_expires_at: string;
    confirm_grant?: boolean;
    reason: string;
  },
  idempotencyKey?: string,
): Promise<ActivationBatchResult> {
  return adminWrite<ActivationBatchResult>(
    "/api/control/activation-code-batches",
    {
      name: input.name,
      face_value_fen: input.face_value_fen,
      credits: input.credits,
      quantity: input.quantity,
      activation_expires_at: input.activation_expires_at,
      confirm_grant: input.confirm_grant ?? false,
    },
    input.reason,
    "创建激活码批次失败",
    idempotencyKey,
  );
}

export async function generateActivationCodes(
  batchId: string,
  quantity: number,
  reason: string,
  idempotencyKey?: string,
  autoIssue = false,
): Promise<ActivationGenerateResult> {
  return adminWrite<ActivationGenerateResult>(
    `/api/control/activation-code-batches/${encodeURIComponent(batchId)}/generate`,
    autoIssue ? { quantity, auto_issue: true } : { quantity },
    reason,
    "生成激活码失败",
    idempotencyKey,
  );
}

export async function downloadActivationCodeExport(
  exportId: string,
  reason: string,
  idempotencyKey?: string,
): Promise<ActivationDownloadResult> {
  return adminWrite<ActivationDownloadResult>(
    `/api/control/activation-code-exports/${encodeURIComponent(exportId)}/download`,
    {},
    reason,
    "下载明文码失败",
    idempotencyKey,
  );
}

export async function listActivationCodes({
  batch_id,
  status,
  search,
  include_archived,
  limit = 50,
  offset = 0,
}: {
  batch_id?: string;
  status?: string;
  /** 服务端搜索：匹配掩码码或绑定用户名（A9）。 */
  search?: string;
  /** C7：true 时回看已归档码（归档只隐藏，不删史）。 */
  include_archived?: boolean;
  limit?: number;
  offset?: number;
} = {}): Promise<ActivationCodePage> {
  const query = new URLSearchParams();
  if (batch_id) {
    query.set("batch_id", batch_id);
  }
  if (status) {
    query.set("status", status);
  }
  if (search) {
    query.set("search", search);
  }
  if (include_archived) {
    query.set("include_archived", "true");
  }
  query.set("limit", String(limit));
  query.set("offset", String(offset));
  const response = await requestControl(
    `/api/control/activation-codes?${query}`,
    { method: "GET" },
  );
  if (!response.ok) {
    throw await parseActivationError(response, "读取激活码列表失败");
  }
  return (await response.json()) as ActivationCodePage;
}

export async function revealActivationCode(
  codeId: string,
  reason: string,
  idempotencyKey?: string,
): Promise<ActivationCodeRevealResult> {
  return adminWrite<ActivationCodeRevealResult>(
    `/api/control/activation-codes/${encodeURIComponent(codeId)}/reveal`,
    {},
    reason,
    "读取激活码失败",
    idempotencyKey,
  );
}

export async function deliverActivationCode(
  codeId: string,
  input: {
    channel: string;
    external_order_ref?: string;
    recipient_ref?: string;
    reason: string;
  },
  idempotencyKey?: string,
): Promise<ActivationDeliverResult> {
  return adminWrite<ActivationDeliverResult>(
    `/api/control/activation-codes/${encodeURIComponent(codeId)}/deliver`,
    {
      channel: input.channel,
      external_order_ref: input.external_order_ref || undefined,
      recipient_ref: input.recipient_ref || undefined,
    },
    input.reason,
    "发放激活码失败",
    idempotencyKey,
  );
}

export async function suspendActivationCode(
  codeId: string,
  reason: string,
  idempotencyKey?: string,
): Promise<ActivationCodeMutationResult> {
  return adminWrite<ActivationCodeMutationResult>(
    `/api/control/activation-codes/${encodeURIComponent(codeId)}/suspend`,
    {},
    reason,
    "暂停激活码失败",
    idempotencyKey,
  );
}

export async function resumeActivationCode(
  codeId: string,
  reason: string,
  idempotencyKey?: string,
): Promise<ActivationCodeMutationResult> {
  return adminWrite<ActivationCodeMutationResult>(
    `/api/control/activation-codes/${encodeURIComponent(codeId)}/resume`,
    {},
    reason,
    "恢复激活码失败",
    idempotencyKey,
  );
}

export async function revokeActivationCode(
  codeId: string,
  reason: string,
  idempotencyKey?: string,
): Promise<ActivationCodeMutationResult> {
  return adminWrite<ActivationCodeMutationResult>(
    `/api/control/activation-codes/${encodeURIComponent(codeId)}/revoke`,
    {},
    reason,
    "作废激活码失败",
    idempotencyKey,
  );
}

export async function archiveActivationCode(
  codeId: string,
  reason: string,
  idempotencyKey?: string,
): Promise<ActivationCodeArchiveResult> {
  return adminWrite<ActivationCodeArchiveResult>(
    `/api/control/activation-codes/${encodeURIComponent(codeId)}/archive`,
    {},
    reason,
    "删除激活码失败",
    idempotencyKey,
  );
}

export async function approveDevicePairing(
  pairingId: string,
  reason: string,
  idempotencyKey?: string,
): Promise<PairingAdminResult> {
  return adminWrite<PairingAdminResult>(
    `/api/control/device-pairings/${encodeURIComponent(pairingId)}/approve`,
    {},
    reason,
    "批准设备配对失败",
    idempotencyKey,
  );
}

export async function replaceDeviceForPairing(
  pairingId: string,
  replaceDeviceId: string,
  reason: string,
  idempotencyKey?: string,
): Promise<PairingAdminResult> {
  return adminWrite<PairingAdminResult>(
    `/api/control/device-pairings/${encodeURIComponent(pairingId)}/replace-device`,
    { replace_device_id: replaceDeviceId },
    reason,
    "更换设备失败",
    idempotencyKey,
  );
}

// ---------------------------------------------------------------------------
// Deterministic operator-facing messages
// ---------------------------------------------------------------------------

const CODE_MESSAGES: Record<string, string> = {
  EXCHANGE_CREDENTIAL_INVALID: "交换凭据无效或已过期，请重新获取",
  EXCHANGE_CREDENTIAL_REUSED: "交换凭据已被使用，请重新获取",
  ADMIN_ACTOR_INVALID: "操作员账号不可用",
  ADMIN_ROLE_REQUIRED: "仅管理员或审计员可登录管理端",
  ADMIN_LOGIN_INVALID: "管理员账号或密码错误",
  ADMIN_PASSWORD_INVALID: "密码需为 12 至 128 个字符",
  ADMIN_PASSWORD_RECOVERY_REQUIRED: "请先使用一次性恢复凭据验证身份",
  ADMIN_PASSWORD_RECOVERY_ONLY: "请先设置密码，再使用账号和密码登录后台",
  ADMIN_SESSION_INVALID: "会话已失效，请重新登录",
  ADMIN_SESSION_EXPIRED: "会话已过期，请重新登录",
  ADMIN_SESSIONS_UNAVAILABLE: "管理会话服务暂不可用，请稍后重试",
  ADMIN_CSRF_REQUIRED: "缺少 CSRF 令牌，请重新登录",
  ADMIN_CSRF_INVALID: "CSRF 令牌不匹配，请重新登录",
  ADMIN_CSRF_UNAVAILABLE: "管理登录令牌缺失，请重新登录后再执行写操作",
  AUDITOR_READ_ONLY: "审计员只读，无法执行写操作",
  IDEMPOTENCY_KEY_REQUIRED: "缺少幂等键，请重试",
  IDEMPOTENCY_CONFLICT: "幂等键冲突：该键已被其他请求使用",
  CONFIRMATION_REQUIRED: "服务端要求显式确认，请勾选确认后重试",
  REASON_REQUIRED: "服务端要求填写操作原因，请补充后重试",
  BATCH_VALIDATION_FAILED: "批次参数校验失败，请检查后重试",
  BATCH_NOT_FOUND: "批次不存在",
  BATCH_NOT_OPEN: "批次已关闭，无法生成激活码",
  BATCH_BUDGET_EXCEEDED: "生成数量超出批次预算",
  ACTIVATION_KEYS_UNAVAILABLE: "激活码密钥未配置，请联系运维",
  ACTIVATION_DETAILS_UNAVAILABLE: "激活码详情暂时无法读取，请稍后重试",
  ACTIVATION_SERVICE_UNAVAILABLE: "激活码服务暂不可用，请稍后重试",
  EXPORT_NOT_FOUND: "导出包不存在",
  EXPORT_ALREADY_DOWNLOADED: "该导出包已被下载过，无法再次下载",
  EXPORT_EXPIRED: "该导出包已过期，请重新生成",
  CODE_NOT_FOUND: "激活码不存在",
  CODE_TRANSITION_INVALID: "当前状态不允许该操作",
  CODE_NOT_ACTIVATED: "仅已激活的激活码可以恢复",
  DELIVERY_VALIDATION_FAILED: "发放参数校验失败，请检查后重试",
};

export function adminActivationErrorMessage(
  cause: unknown,
  fallback: string,
  overrides: Record<string, string> = {},
): string {
  if (cause instanceof AdminActivationError) {
    const code = cause.code ?? "";
    const mapped = overrides[code] ?? CODE_MESSAGES[code];
    if (mapped) {
      return mapped;
    }
    return cause.message.trim() || fallback;
  }
  if (cause instanceof Error && cause.message.trim()) {
    return cause.message;
  }
  return fallback;
}

// ---------------------------------------------------------------------------
// T33 — Customer management APIs (ADM-02)
// ---------------------------------------------------------------------------

export const AdminCustomerError = AdminControlError;

export interface CustomerListItem {
  user_id: string;
  username: string;
  display_name?: string;
  created_at: string;
  activation_code_id?: string;
  activation_code: string;
  status: string;
  available_credits?: number;
  reserved_credits?: number;
  device_slots_used?: number;
  device_slots_total?: number | null;
  generation_total?: number;
  generation_succeeded?: number;
  generation_failed?: number;
  generation_in_progress?: number;
  generation_attention?: number;
  credits_spent?: number;
}

export interface CustomerListResponse {
  items: CustomerListItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface CustomerListOptions {
  limit?: number;
  offset?: number;
  username_filter?: string;
  status?: string;
  createdFrom?: string;
  createdTo?: string;
  balanceMin?: number;
  balanceMax?: number;
}

/**
 * Fetch the customer list with the management-wide limit/offset contract (A5).
 *
 * GET /api/control/customers?limit=&offset=&username=
 */
export async function listCustomers(
  options: CustomerListOptions = {},
): Promise<CustomerListResponse> {
  const params = new URLSearchParams();
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined)
    params.set("offset", String(options.offset));
  if (options.username_filter) params.set("username", options.username_filter);
  if (options.status) params.set("status", options.status);
  if (options.createdFrom) params.set("created_from", options.createdFrom);
  if (options.createdTo) params.set("created_to", options.createdTo);
  if (options.balanceMin !== undefined)
    params.set("balance_min", String(options.balanceMin));
  if (options.balanceMax !== undefined)
    params.set("balance_max", String(options.balanceMax));

  const response = await requestControl(
    `/api/control/customers?${params.toString()}`,
    { method: "GET" },
  );

  if (!response.ok) {
    throw await parseActivationError(response, "读取客户列表失败");
  }

  return response.json() as Promise<CustomerListResponse>;
}

export interface CustomerUnitPrice {
  user_id: string;
  unit_price_fen: number;
  custom_unit_price_fen: number | null;
  default_unit_price_fen: number;
  min_recharge_fen: number;
  recharge_step_fen: number;
  updated_at: string | null;
  request_id: string | null;
}

/** Read the effective sale price for one activated customer. */
export async function fetchCustomerUnitPrice(
  userId: string,
): Promise<CustomerUnitPrice> {
  const response = await requestControl(
    `/api/control/customers/${encodeURIComponent(userId)}/unit-price`,
    { method: "GET" },
  );
  if (!response.ok) {
    throw await parseActivationError(response, "读取客户单价失败");
  }
  return response.json() as Promise<CustomerUnitPrice>;
}

/**
 * Set a customer's sale price in fen, or pass null to restore the global
 * default. The internal cost/base price is deliberately not consulted.
 */
export async function updateCustomerUnitPrice(
  userId: string,
  unitPriceFen: number | null,
  reason: string,
  idempotencyKey: string = newIdempotencyKey(),
): Promise<CustomerUnitPrice> {
  const csrf = requireCsrfToken();
  const response = await requestControl(
    `/api/control/customers/${encodeURIComponent(userId)}/unit-price`,
    {
      method: "PUT",
      headers: {
        [CSRF_HEADER]: csrf,
        [IDEMPOTENCY_KEY_HEADER]: idempotencyKey,
      },
      body: JSON.stringify({
        confirm: true,
        reason,
        unit_price_fen: unitPriceFen,
      }),
    },
  );
  if (!response.ok) {
    throw await parseActivationError(response, "保存客户单价失败");
  }
  return response.json() as Promise<CustomerUnitPrice>;
}

// ---------------------------------------------------------------------------
// T33 — Device management APIs (ADM-02)
// ---------------------------------------------------------------------------

export const AdminDeviceError = AdminControlError;

export interface DeviceListItem {
  device_id: string;
  activation_code_id: string | null;
  user_id: string;
  slot_no: number;
  display_name: string | null;
  platform: string;
  status: string;
  bound_at: string | null;
  unbound_at: string | null;
  revoked_at: string | null;
  username?: string;
  activation_code?: string;
  last_heartbeat_at?: string | null;
  online?: boolean;
}

export interface DeviceSummary {
  bound: number;
  online: number;
  revoked_today: number;
  unbound: number;
}

export interface DeviceListResponse {
  items: DeviceListItem[];
  total: number;
  limit: number;
  offset: number;
  summary?: DeviceSummary;
}

export interface DeviceListOptions {
  status?: string;
  platform?: string;
  userId?: string;
  limit?: number;
  offset?: number;
}

/**
 * Fetch the device list with offset-based pagination.
 *
 * GET /api/control/devices?status=&limit=&offset=
 */
export async function listDevices(
  options: DeviceListOptions = {},
): Promise<DeviceListResponse> {
  const params = new URLSearchParams();
  if (options.status) params.set("status", options.status);
  if (options.platform) params.set("platform", options.platform);
  if (options.userId) params.set("user_id", options.userId);
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined)
    params.set("offset", String(options.offset));

  const response = await requestControl(
    `/api/control/devices?${params.toString()}`,
    { method: "GET" },
  );

  if (!response.ok) {
    throw await parseActivationError(response, "读取设备列表失败");
  }

  return response.json() as Promise<DeviceListResponse>;
}

export interface DeviceOperationResult {
  device_id: string;
  status: "UNBOUND" | "REVOKED";
  outcome: string;
  request_id: string;
}

export async function unbindDevice(
  deviceId: string,
  reason: string,
): Promise<DeviceOperationResult> {
  return adminWrite<DeviceOperationResult>(
    `/api/control/devices/${encodeURIComponent(deviceId)}/unbind`,
    {},
    reason,
    "设备下线失败",
  );
}

export async function revokeDeviceCredential(
  deviceId: string,
  reason: string,
): Promise<DeviceOperationResult> {
  return adminWrite<DeviceOperationResult>(
    `/api/control/devices/${encodeURIComponent(deviceId)}/revoke-credential`,
    {},
    reason,
    "撤销设备凭据失败",
  );
}

// ---------------------------------------------------------------------------
// T33 — Adjustment history APIs (ADM-02)
// ---------------------------------------------------------------------------

export const AdminAdjustmentError = AdminControlError;

export interface AdjustmentListItem {
  adjustment_id: string;
  order_id: string;
  admin_user_id: string;
  source_document_type: string;
  source_document_ref: string;
  reason: string;
  request_id: string;
  created_at: string;
  amount_fen: number;
  credits: number;
  pricing_scope: string;
  status: string;
  admin_username?: string;
  target_user_id?: string;
  target_username?: string;
  balance_before?: number | null;
  balance_after?: number | null;
}

export interface GlobalAdjustmentListOptions extends AdjustmentListOptions {
  actorUsername?: string;
  targetUsername?: string;
  sourceDocumentType?: string;
  createdFrom?: string;
  createdTo?: string;
}

export async function listAllAdminAdjustments(
  options: GlobalAdjustmentListOptions = {},
): Promise<AdjustmentListResponse> {
  const params = new URLSearchParams();
  if (options.actorUsername)
    params.set("actor_username", options.actorUsername);
  if (options.targetUsername)
    params.set("target_username", options.targetUsername);
  if (options.sourceDocumentType)
    params.set("source_document_type", options.sourceDocumentType);
  if (options.createdFrom) params.set("created_from", options.createdFrom);
  if (options.createdTo) params.set("created_to", options.createdTo);
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined)
    params.set("offset", String(options.offset));
  return adminRead<AdjustmentListResponse>(
    `/api/control/adjustments?${params.toString()}`,
    "读取调账记录失败",
  );
}

export interface AdjustmentListResponse {
  items: AdjustmentListItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface AdjustmentListOptions {
  limit?: number;
  offset?: number;
  sort?: "asc" | "desc";
}

/**
 * Fetch the adjustment history for a specific user with offset-based pagination.
 *
 * GET /api/control/customers/{user_id}/adjustments?limit=&offset=
 */
export async function listAdminAdjustments(
  userId: string,
  options: AdjustmentListOptions = {},
): Promise<AdjustmentListResponse> {
  const params = new URLSearchParams();
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined)
    params.set("offset", String(options.offset));
  if (options.sort !== undefined) params.set("sort", options.sort);

  const response = await requestControl(
    `/api/control/customers/${encodeURIComponent(userId)}/adjustments?${params.toString()}`,
    { method: "GET" },
  );

  if (!response.ok) {
    let detail = "读取调账历史失败";
    try {
      const body: unknown = await response.json();
      if (typeof body === "object" && body !== null && !Array.isArray(body)) {
        const d = (body as Record<string, unknown>).detail;
        if (typeof d === "object" && d !== null) {
          const msg = (d as Record<string, unknown>).message;
          if (typeof msg === "string" && msg.trim()) {
            detail = `读取调账历史失败：${msg}（${response.status}）`;
          }
        }
      }
    } catch {
      /* non-JSON body */
    }
    throw new AdminAdjustmentError(detail, response.status);
  }

  return response.json() as Promise<AdjustmentListResponse>;
}

// ---------------------------------------------------------------------------
// T34 — Customer session API (ADM-02)
// ---------------------------------------------------------------------------

export const AdminSessionError = AdminControlError;

export interface CustomerSessionListItem {
  session_id: string;
  user_id: string;
  username: string;
  device_id: string;
  session_epoch: number;
  lease_until: string;
  last_heartbeat_at: string;
  created_at: string;
  updated_at: string;
  device_name: string;
  platform: string;
  slot_no: number;
  device_status: string;
}

export interface CustomerSessionListResponse {
  items: CustomerSessionListItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface CustomerSessionListOptions {
  status?: string;
  limit?: number;
  offset?: number;
}

/**
 * Overview of every currently live customer session (A11).
 *
 * GET /api/control/customer-sessions/live?limit=&offset=
 */
export async function listLiveSessions(
  options: { limit?: number; offset?: number } = {},
): Promise<CustomerSessionListResponse> {
  const params = new URLSearchParams();
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined)
    params.set("offset", String(options.offset));

  const response = await requestControl(
    `/api/control/customer-sessions/live?${params.toString()}`,
    { method: "GET" },
  );

  if (!response.ok) {
    throw await parseActivationError(response, "读取在线会话失败");
  }

  return response.json() as Promise<CustomerSessionListResponse>;
}

/**
 * Fetch the live customer session state (the 029 one-row-per-user model).
 *
 * GET /api/control/customers/{user_id}/sessions?status=&limit=&offset=
 */
export async function listCustomerSessions(
  userId: string,
  options: CustomerSessionListOptions = {},
): Promise<CustomerSessionListResponse> {
  const params = new URLSearchParams();
  if (options.status) params.set("status", options.status);
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined)
    params.set("offset", String(options.offset));

  const response = await requestControl(
    `/api/control/customers/${encodeURIComponent(userId)}/sessions?${params.toString()}`,
    { method: "GET" },
  );

  if (!response.ok) {
    throw await parseActivationError(response, "读取客户会话失败");
  }

  return response.json() as Promise<CustomerSessionListResponse>;
}

export async function revokeCustomerSession(
  sessionId: string,
  sessionEpoch: number,
  reason: string,
  idempotencyKey: string,
): Promise<{ request_id: string }> {
  return adminWrite<{ request_id: string }>(
    `/api/control/customer-sessions/${encodeURIComponent(sessionId)}/revoke`,
    { session_epoch: sessionEpoch },
    reason,
    "结束会话失败",
    idempotencyKey,
  );
}

// ---------------------------------------------------------------------------
// T34 — Audit log API (ADM-02)
// ---------------------------------------------------------------------------

export const AdminAuditError = AdminControlError;

export interface AuditTariffSnapshot {
  enabled?: boolean;
  unit_credits?: string | null;
  unit_cost_fen?: string | null;
  unit_rounding?: string | null;
  version?: number;
}

export interface AuditLogItem {
  event_id: string;
  event_type: string;
  actor_user_id: string;
  actor_username: string;
  target_user_id: string;
  target_username?: string;
  source_document_type: string;
  source_document_ref: string;
  reason: string;
  request_id: string;
  created_at: string;
  change_subject?: string | null;
  old_unit_price_fen?: number | null;
  new_unit_price_fen?: number | null;
  /** 仅 billing.tariff.update：服务端派生的费率变更前后快照（白名单字段）。 */
  change_detail?: {
    old?: AuditTariffSnapshot | null;
    new?: AuditTariffSnapshot | null;
  } | null;
}

export interface AuditLogResponse {
  items: AuditLogItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface AuditLogOptions {
  eventType?: string;
  actorUserId?: string;
  targetUserId?: string;
  actorUsername?: string;
  targetUsername?: string;
  createdFrom?: string;
  createdTo?: string;
  limit?: number;
  offset?: number;
}

/**
 * Fetch the unified audit trail (A10: five audited surfaces UNIONed) with
 * pagination and combined filters.
 *
 * GET /api/control/audit-log?event_type=&actor_user_id=&target_user_id=
 *   &created_from=&created_to=&limit=&offset=
 */
export async function listAuditLog(
  options: AuditLogOptions = {},
): Promise<AuditLogResponse> {
  const params = new URLSearchParams();
  if (options.eventType) params.set("event_type", options.eventType);
  if (options.actorUserId) params.set("actor_user_id", options.actorUserId);
  if (options.targetUserId) params.set("target_user_id", options.targetUserId);
  if (options.actorUsername)
    params.set("actor_username", options.actorUsername);
  if (options.targetUsername)
    params.set("target_username", options.targetUsername);
  if (options.createdFrom) params.set("created_from", options.createdFrom);
  if (options.createdTo) params.set("created_to", options.createdTo);
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.offset !== undefined)
    params.set("offset", String(options.offset));

  const response = await requestControl(
    `/api/control/audit-log?${params.toString()}`,
    { method: "GET" },
  );

  if (!response.ok) {
    throw await parseActivationError(response, "读取审计日志失败");
  }

  return response.json() as Promise<AuditLogResponse>;
}

// ---------------------------------------------------------------------------
// Generation records — paid media and AI-operation traceability
// ---------------------------------------------------------------------------

export type AdminGenerationRecord =
  components["schemas"]["ControlGenerationRecord"];
export type AdminGenerationRecordPage =
  components["schemas"]["ControlGenerationRecordPage"];
export type AdminGenerationRecordSummary =
  components["schemas"]["ControlGenerationRecordSummary"];

export async function getAdminGenerationRecords(
  options: {
    limit?: number;
    offset?: number;
    username?: string;
    status?: string;
    recordType?: string;
    failurePhase?: string;
    createdFrom?: string;
    createdTo?: string;
  } = {},
): Promise<AdminGenerationRecordPage> {
  const params = new URLSearchParams({
    limit: String(options.limit ?? 50),
    offset: String(options.offset ?? 0),
  });
  if (options.username) params.set("username", options.username);
  if (options.status) params.set("status", options.status);
  if (options.recordType) params.set("record_type", options.recordType);
  if (options.failurePhase) params.set("failure_phase", options.failurePhase);
  if (options.createdFrom) params.set("created_from", options.createdFrom);
  if (options.createdTo) params.set("created_to", options.createdTo);
  const response = await requestControl(
    `/api/control/generation-records?${params.toString()}`,
    { method: "GET" },
  );
  if (!response.ok) {
    throw await parseActivationError(response, "读取生成记录失败");
  }
  return response.json() as Promise<AdminGenerationRecordPage>;
}

export async function getAdminGenerationRecordSummary(
  options: {
    username?: string;
    status?: string;
    recordType?: string;
    failurePhase?: string;
    createdFrom?: string;
    createdTo?: string;
  } = {},
): Promise<AdminGenerationRecordSummary> {
  const params = new URLSearchParams();
  if (options.username) params.set("username", options.username);
  if (options.status) params.set("status", options.status);
  if (options.recordType) params.set("record_type", options.recordType);
  if (options.failurePhase) params.set("failure_phase", options.failurePhase);
  if (options.createdFrom) params.set("created_from", options.createdFrom);
  if (options.createdTo) params.set("created_to", options.createdTo);
  const query = params.toString();
  const response = await requestControl(
    `/api/control/generation-records/summary${query ? `?${query}` : ""}`,
    { method: "GET" },
  );
  if (!response.ok) {
    throw await parseActivationError(response, "读取生成记录聚合失败");
  }
  return response.json() as Promise<AdminGenerationRecordSummary>;
}

export interface FirstFrameReconcileResult {
  task_id: string;
  result: "RESUMED" | "FAILED";
  detail_code: string | null;
}

/**
 * Reconcile a first-frame task stuck in SUBMISSION_UNCERTAIN.
 *
 * `POST /api/control/first-frame-tasks/{task_id}/reconcile` — the operator
 * entry for a task whose provider submission outcome is unknown. The server
 * asks the provider what really happened (never a second paid submission)
 * and answers RESUMED (requeued, status back to PENDING) or FAILED.
 *
 * The route consumes no body: its OpenAPI operation declares
 * `requestBody?: never`, it does not run the write contract, and a repeat
 * answers 409 `IMAGE_TASK_NOT_UNCERTAIN` rather than replaying — so there is
 * nothing for an idempotency key to do. It is still sent through `adminWrite`
 * because a POST on this lane is CSRF-gated: a bare `requestControl` POST
 * carries no CSRF header and the server answers 403 `ADMIN_CSRF_REQUIRED`.
 * The reason travels for shape consistency with every other admin write; the
 * server derives the audit wording from the reconcile decision instead.
 */
export async function reconcileFirstFrameTask(
  taskId: string,
  reason: string,
): Promise<FirstFrameReconcileResult> {
  return adminWrite<FirstFrameReconcileResult>(
    `/api/control/first-frame-tasks/${encodeURIComponent(taskId)}/reconcile`,
    {},
    reason,
    "首帧任务对账失败",
  );
}

export type AdminAnalysisDiagnosticAttempt =
  components["schemas"]["AnalysisDiagnosticAttempt"];
export type AdminAnalysisDiagnosticRecord =
  components["schemas"]["AnalysisDiagnosticRecord"];
export type AdminAnalysisDiagnostics =
  components["schemas"]["AnalysisDiagnosticsResponse"];

/** P1-8 失败诊断：按任务编号或问题编号取单个拆解任务的重试历史。 */
export async function getAdminAnalysisDiagnostics(
  options: { taskId?: string; requestId?: string } = {},
): Promise<AdminAnalysisDiagnostics> {
  const params = new URLSearchParams();
  if (options.taskId) params.set("task_id", options.taskId);
  if (options.requestId) params.set("request_id", options.requestId);
  const query = params.toString();
  const response = await requestControl(
    `/api/control/analysis-diagnostics${query ? `?${query}` : ""}`,
    { method: "GET" },
  );
  if (!response.ok) {
    throw await parseActivationError(response, "查询任务诊断失败");
  }
  return response.json() as Promise<AdminAnalysisDiagnostics>;
}

// ---------------------------------------------------------------------------
// T23 — Admin adjustment write (ADM-02)
// ---------------------------------------------------------------------------

export interface AdjustmentWriteInput {
  sourceDocumentType: string;
  sourceDocumentRef: string;
  credits: number;
}

export interface AdjustmentWriteResult {
  adjustment_id: string;
  order_id: string;
  credits: string;
  amount_fen: string;
  pricing_scope: string;
  wallet_balance_after: number;
  source_document_type: string;
  source_document_ref: string;
  request_id: string;
}

/**
 * Create an admin adjustment: PAID order + CHARGE + wallet + audit row.
 * The write contract (confirm + reason + idempotency key) rides the shared
 * adminWrite path, and the key is preserved across ambiguous retries.
 *
 * POST /api/control/customers/{user_id}/adjustments
 */
export async function createCustomerAdjustment(
  userId: string,
  input: AdjustmentWriteInput,
  reason: string,
  idempotencyKey?: string,
): Promise<AdjustmentWriteResult> {
  return adminWrite<AdjustmentWriteResult>(
    `/api/control/customers/${encodeURIComponent(userId)}/adjustments`,
    {
      source_document_type: input.sourceDocumentType,
      source_document_ref: input.sourceDocumentRef,
      credits: input.credits,
    },
    reason,
    "创建后台调账失败",
    idempotencyKey,
  );
}

// ---------------------------------------------------------------------------
// Queue-mode switch (M4/M5 review M2 follow-up, PR #68 Codex P1): the
// production control-plane read/write for the fair-queue rollout switch.
// ---------------------------------------------------------------------------

export async function fetchQueueMode(): Promise<boolean> {
  const response = await requestControl("/api/control/settings/queue-mode", {
    method: "GET",
  });
  if (!response.ok) {
    throw await parseActivationError(response, "读取队列模式失败");
  }
  const payload = (await response.json()) as { fair_queue_enabled: boolean };
  return payload.fair_queue_enabled;
}

export async function updateQueueMode(
  enabled: boolean,
  reason: string,
  idempotencyKey?: string,
): Promise<boolean> {
  const payload = await adminWrite<{ fair_queue_enabled: boolean }>(
    "/api/control/settings/queue-mode",
    { fair_queue_enabled: enabled },
    reason,
    "切换队列模式失败",
    idempotencyKey,
    "PATCH",
  );
  return payload.fair_queue_enabled;
}

// ---------------------------------------------------------------------------
// H3 extended-modes switch (CW-063 / 缺口 4b): the production control-plane
// read/write for runtime_settings.h3_extended_modes_enabled — the gate on real
// paid T2V / R2V / last_frame submissions. Mirrors the queue-mode helpers and
// rides the same shared admin write contract (reason + Idempotency-Key).
// ---------------------------------------------------------------------------

export async function fetchH3ExtendedModes(): Promise<boolean> {
  const response = await requestControl(
    "/api/control/settings/h3-extended-modes",
    {
      method: "GET",
    },
  );
  if (!response.ok) {
    throw await parseActivationError(response, "读取扩展模式开关失败");
  }
  const payload = (await response.json()) as {
    h3_extended_modes_enabled: boolean;
  };
  return payload.h3_extended_modes_enabled;
}

export async function updateH3ExtendedModes(
  enabled: boolean,
  reason: string,
  idempotencyKey?: string,
): Promise<boolean> {
  const payload = await adminWrite<{ h3_extended_modes_enabled: boolean }>(
    "/api/control/settings/h3-extended-modes",
    { h3_extended_modes_enabled: enabled },
    reason,
    "切换扩展模式失败",
    idempotencyKey,
    "PATCH",
  );
  return payload.h3_extended_modes_enabled;
}

export type ViralRuntimeControls = {
  collection_enabled: boolean;
  import_enabled: boolean;
  pending_imports: number;
  running_imports: number;
  failed_imports: number;
  pending_refreshes: number;
  running_refreshes: number;
  failed_refreshes: number;
  source_configured: boolean;
  keywords?: Array<{
    platform: "douyin" | "wechat_channels";
    category: string;
    keyword: string;
  }>;
  per_keyword_limit?: number;
  next_collection_at?: string | null;
  collection_interval_days?: number;
  platforms: Array<{
    platform: "douyin" | "wechat_channels";
    cached_videos: number;
    last_fetched_at: string | null;
    refresh_status:
      | "not_configured"
      | "configured_only"
      | "refreshing"
      | "ok"
      | "error";
    last_refresh_error: string | null;
  }>;
};

export async function fetchViralRuntimeControls(): Promise<ViralRuntimeControls> {
  const response = await requestControl("/api/control/settings/viral", {
    method: "GET",
  });
  if (!response.ok) {
    throw await parseActivationError(response, "读取爆款视频运行状态失败");
  }
  return (await response.json()) as ViralRuntimeControls;
}

export async function updateViralRuntimeControls(
  controls: Pick<
    ViralRuntimeControls,
    | "collection_enabled"
    | "import_enabled"
    | "keywords"
    | "per_keyword_limit"
    | "collection_interval_days"
  >,
  reason: string,
  idempotencyKey?: string,
): Promise<ViralRuntimeControls> {
  return adminWrite<ViralRuntimeControls>(
    "/api/control/settings/viral",
    controls,
    reason,
    "更新爆款视频运行开关失败",
    idempotencyKey,
    "PATCH",
  );
}

export async function updateViralVideoAvailability(
  platform: "douyin" | "wechat_channels",
  videoId: string,
  status: "AVAILABLE" | "HIDDEN" | "UNAVAILABLE",
  reason: string,
  idempotencyKey?: string,
): Promise<{ platform: string; video_id: string; status: string }> {
  return adminWrite(
    `/api/control/viral/videos/${encodeURIComponent(platform)}/${encodeURIComponent(videoId)}/availability`,
    { status },
    reason,
    "更新爆款视频可用状态失败",
    idempotencyKey,
    "PATCH",
  );
}

// ---------------------------------------------------------------------------
// Operation rates (W10 — 费率管理：上游成本费率与对外售价)
// ---------------------------------------------------------------------------

export type CollectedViralVideo = {
  archive_status?: string | null;
  archive_error?: string | null;
  statistics_checked_at?: string | null;
  statistics_retry_at?: string | null;
  cover_required?: boolean;
  cover_key?: string | null;
  platform: "douyin" | "wechat_channels";
  video_id: string;
  category: string;
  title: string;
  author: string;
  duration_ms: number;
  likes: number;
  comments: number | null;
  shares: number | null;
  collects: number | null;
  published_at: number | null;
  created_at: string;
  homepage_featured: boolean;
  collection_published: boolean;
  media_status: string;
  storage_uri: string | null;
};

export async function listCollectedViralVideos(options: {
  platform?: string;
  query?: string;
  offset?: number;
}): Promise<{ items: CollectedViralVideo[]; total: number }> {
  const query = new URLSearchParams({
    limit: "25",
    offset: String(options.offset ?? 0),
  });
  if (options.platform) query.set("platform", options.platform);
  if (options.query) query.set("query", options.query);
  const response = await requestControl(
    `/api/control/viral/videos?${query}`,
    {},
  );
  if (!response.ok)
    throw await parseActivationError(response, "读取采集视频失败");
  return response.json();
}

export function archiveCollectedViralVideo(
  video: CollectedViralVideo,
  reason: string,
  idempotencyKey: string,
) {
  return adminWrite(
    `/api/control/viral/videos/${encodeURIComponent(video.platform)}/${encodeURIComponent(video.video_id)}/archive`,
    {},
    reason,
    "提交转存失败",
    idempotencyKey,
    "POST",
  );
}

export function curateViralVideo(
  video: CollectedViralVideo,
  action: "feature" | "unfeature" | "delete",
  reason: string,
  idempotencyKey: string,
) {
  return adminWrite(
    `/api/control/viral/videos/${encodeURIComponent(video.platform)}/${encodeURIComponent(video.video_id)}/curation`,
    { action },
    reason,
    "更新爆款视频失败",
    idempotencyKey,
    "PATCH",
  );
}

export function refreshCollectedVideoStatistics(
  video: Pick<CollectedViralVideo, "video_id">,
  key: string,
) {
  return adminWrite<
    Pick<
      CollectedViralVideo,
      | "likes"
      | "comments"
      | "shares"
      | "collects"
      | "statistics_checked_at"
      | "statistics_retry_at"
    > & { statistics_status: "complete" | "partial" | "failure" }
  >(
    `/api/control/viral/videos/wechat_channels/${encodeURIComponent(video.video_id)}/statistics`,
    {},
    "管理端补齐视频号互动数据",
    "获取互动数据失败",
    key,
    "POST",
    45_000,
  );
}

export async function previewCollectedViralVideo(
  video: CollectedViralVideo,
): Promise<{ url: string }> {
  const response = await requestControl(
    `/api/control/viral/videos/${encodeURIComponent(video.platform)}/${encodeURIComponent(video.video_id)}/preview`,
    {},
  );
  if (!response.ok)
    throw await parseActivationError(response, "读取视频预览失败");
  return response.json();
}

// ---------------------------------------------------------------------------
// Dashboard summary (W15 — 总览仪表盘)
// ---------------------------------------------------------------------------

export type DashboardTrendPoint = {
  day: string;
  succeeded: number;
  failed: number;
};

export type DashboardSummary = {
  today: {
    generation_count: number;
    succeeded: number;
    success_rate_pct?: number | null;
    output_seconds?: number;
    cost_fen?: number | null;
    revenue_fen?: number | null;
    gross_fen?: number | null;
    margin_pct?: number | null;
    pending_operations?: number;
    unknown_revenue_operations?: number;
    legacy_cost_records?: number;
    legacy_settlements?: number;
    active_customers: number;
    recharge_fen: number;
    recharge_orders?: number;
  };
  trend: Array<
    DashboardTrendPoint & {
      cost_fen?: number | null;
      legacy_cost_records?: number;
    }
  >;
  todos: {
    failed_tasks_7d: number;
    /** 视频拆解失败（7 天）；与 failed_tasks_7d 分开统计：「生成」口径不含拆解。 */
    analysis_failures_7d?: number;
    /** 拆解失败原因聚合（最多 3 条），回答「上游为什么拒绝、能不能重试」。 */
    analysis_failure_reasons?: Array<{
      error_code: string | null;
      failure_phase: string | null;
      reason: string | null;
      count: number;
    }>;
    reconciliation_problems: number;
    unconfigured_rates?: number;
    unknown_cost_records?: number;
  };
};

export async function getDashboardSummary(): Promise<DashboardSummary> {
  const response = await requestControl("/api/control/dashboard/summary", {});
  if (!response.ok) {
    throw await parseActivationError(response, "读取仪表盘失败");
  }
  return (await response.json()) as DashboardSummary;
}
export type LegacyCreditPolicy = {
  version: number;
  mode: "keep" | "convert";
  numerator: number;
  denominator: number;
};
export type LegacyCreditConversion = {
  user_id: string;
  before_credits: number;
  after_credits: number;
  converted: boolean;
  policy: LegacyCreditPolicy;
};
export async function getLegacyCreditPolicy(): Promise<LegacyCreditPolicy> {
  const response = await requestControl(
    "/api/control/settings/legacy-credit-policy",
    {},
  );
  if (!response.ok)
    throw await parseActivationError(response, "历史积分策略加载失败");
  return response.json();
}
export function saveLegacyCreditPolicy(
  policy: Omit<LegacyCreditPolicy, "version"> & { expected_version: number },
  reason: string,
  key: string,
): Promise<LegacyCreditPolicy> {
  return adminWrite(
    "/api/control/settings/legacy-credit-policy",
    policy,
    reason,
    "保存历史积分策略失败",
    key,
    "PUT",
  );
}
export async function getLegacyCreditConversion(
  userId: string,
): Promise<LegacyCreditConversion> {
  const response = await requestControl(
    `/api/control/customers/${encodeURIComponent(userId)}/credit-conversion`,
    {},
  );
  if (!response.ok)
    throw await parseActivationError(response, "转换预览不可用");
  return response.json();
}
export function applyLegacyCreditConversion(
  userId: string,
  preview: LegacyCreditConversion,
  reason: string,
  key: string,
): Promise<LegacyCreditConversion> {
  return adminWrite(
    `/api/control/customers/${encodeURIComponent(userId)}/credit-conversion`,
    {
      expected_version: preview.policy.version,
      expected_balance: preview.before_credits,
    },
    reason,
    "历史积分转换失败",
    key,
  );
}

export async function downloadBillingCsv(query: string): Promise<string> {
  const response = await requestControl(
    `/api/control/billing/export?${query}`,
    {},
  );
  if (!response.ok)
    throw await parseActivationError(response, "导出经营明细失败");
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "逐项经营明细.csv";
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return response.headers.get("X-Export-Truncated") === "true"
    ? "已导出前 5000 条；请缩小日期范围后分批导出。"
    : "明细已导出。";
}
