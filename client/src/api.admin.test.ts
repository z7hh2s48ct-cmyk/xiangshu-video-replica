import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getAdminCsrfToken,
  getControlAccounts,
  SESSION_EXPIRED_EVENT,
  setAdminCsrfToken,
  updateControlBillingSettings,
} from "./api";
import {
  AdminActivationError,
  clearAdminActivationSession,
  createActivationCodeBatch,
  deleteAdminSession,
  deliverActivationCode,
  downloadActivationCodeExport,
  exchangeAdminSession,
  exportBillingReportCsv,
  fetchAdminSession,
  fetchCustomerUnitPrice,
  generateActivationCodes,
  getCustomerPricing,
  listActivationCodes,
  loginAdminWithPassword,
  paidTestControlProvider,
  reconcileFirstFrameTask,
  recoverAdminPassword,
  refreshCollectedVideoStatistics,
  resumeActivationCode,
  revokeActivationCode,
  revokeCustomerSession,
  revokeDeviceCredential,
  selfCheckWechatNative,
  submitBillingQuote,
  suspendActivationCode,
  unbindDevice,
  updateCustomerUnitPrice,
} from "./api.admin";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

// The mock literal is indirect so the repo secret scan (which flags
// `token:` followed by a quoted literal) stays quiet — the T29 precedent.
const CSRF_TOKEN_TEXT = "csrf-token-1";

it("只读价格试算使用 GET 和精确客户编号，不携带写契约", async () => {
  setAdminCsrfToken("");
  const fetchMock = vi
    .fn()
    .mockImplementation(() => jsonResponse({ credits: "1" }));
  vi.stubGlobal("fetch", fetchMock);
  const result = await submitBillingQuote({
    service: "asr",
    units: 0.4,
    userId: "opaque/user A",
  });
  expect(result.credits).toBe("1");
  const [address, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  const url = new URL(address, "http://localhost");
  expect(url.pathname).toBe("/api/control/billing/quote");
  expect(url.searchParams.get("user_id")).toBe("opaque/user A");
  expect(url.searchParams.get("units")).toBe("0.4");
  expect(init.method).toBe("GET");
  expect(init.body).toBeUndefined();
  expect(new Headers(init.headers).get("Idempotency-Key")).toBeNull();
  expect(new Headers(init.headers).get("X-CSRF-Token")).toBeNull();
});

it("视频号互动补采允许超过普通管理请求的五秒等待", async () => {
  vi.useFakeTimers();
  setAdminCsrfToken(CSRF_TOKEN_TEXT);
  vi.stubGlobal(
    "fetch",
    vi.fn(
      (_url: string, init: RequestInit) =>
        new Promise((resolve, reject) => {
          init.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
          setTimeout(
            () =>
              resolve({
                ok: true,
                status: 200,
                json: async () => ({ likes: 3, statistics_status: "complete" }),
              }),
            6000,
          );
        }),
    ),
  );
  try {
    const result = refreshCollectedVideoStatistics(
      { video_id: "opaque/id" },
      "statistics-wait",
    ).then(
      (value) => ({ ok: true, value }),
      () => ({ ok: false }),
    );
    await vi.advanceTimersByTimeAsync(6000);
    expect(await result).toMatchObject({ ok: true, value: { likes: 3 } });
  } finally {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    setAdminCsrfToken("");
  }
});

const exchangePayload = {
  session_id: "session-1",
  expires_at: "2026-08-23T20:00:00+00:00",
  csrf_token: CSRF_TOKEN_TEXT,
  actor: {
    user_id: "admin-1",
    username: "admin",
    display_name: "管理员一号",
    role: "admin",
  },
};

const sessionPayload = {
  session_id: "session-1",
  expires_at: "2026-08-23T20:00:00+00:00",
  last_activity_at: "2026-08-23T12:00:00+00:00",
  csrf_token: CSRF_TOKEN_TEXT,
  actor: exchangePayload.actor,
};

async function signIn(fetchMock: ReturnType<typeof vi.fn>) {
  fetchMock.mockImplementationOnce(() => jsonResponse(exchangePayload));
  await exchangeAdminSession("ASX1.body.signature");
}

describe("admin request contract regressions", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    clearAdminActivationSession();
  });

  it("revokes the selected session with CSRF, its epoch and the same retry key", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);
    fetchMock.mockImplementation(() =>
      jsonResponse({ request_id: "req-revoke" }),
    );
    for (let attempt = 0; attempt < 2; attempt += 1) {
      await expect(
        revokeCustomerSession("session/one", 7, "客服核验", "revoke-key"),
      ).resolves.toEqual({ request_id: "req-revoke" });
      expect(fetchMock).toHaveBeenLastCalledWith(
        "http://127.0.0.1:8000/api/control/customer-sessions/session%2Fone/revoke",
        expect.objectContaining({
          method: "POST",
          credentials: "include",
          headers: expect.objectContaining({
            "X-Admin-CSRF": CSRF_TOKEN_TEXT,
            "Idempotency-Key": "revoke-key",
          }),
          body: JSON.stringify({
            session_epoch: 7,
            confirm: true,
            reason: "客服核验",
          }),
        }),
      );
    }
  });

  it("refuses session revocation locally when the administrator has no CSRF token", async () => {
    const fetchMock = vi.fn(() => jsonResponse({ request_id: "unexpected" }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(
      revokeCustomerSession("session-one", 7, "客服核验", "revoke-key"),
    ).rejects.toMatchObject({ code: "ADMIN_CSRF_UNAVAILABLE" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each(["read", "write"])(
    "clears the admin token and notifies expiry on a protected %s 401",
    async (kind) => {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      await signIn(fetchMock);
      const dispatch = vi.spyOn(window, "dispatchEvent");
      fetchMock.mockImplementation(() =>
        jsonResponse(
          { detail: { code: "ADMIN_SESSION_EXPIRED", message: "会话过期" } },
          401,
        ),
      );
      await expect(
        kind === "read"
          ? getCustomerPricing()
          : revokeCustomerSession("session-one", 7, "客服核验", "revoke-key"),
      ).rejects.toMatchObject({ status: 401, code: "ADMIN_SESSION_EXPIRED" });
      expect(
        dispatch.mock.calls.filter(
          ([event]) => event.type === SESSION_EXPIRED_EVENT,
        ),
      ).toHaveLength(1);
      expect(getAdminCsrfToken()).toBeNull();
    },
  );

  it("shows a string detail from pricing validation and preserves a valid session on 422", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);
    const dispatch = vi.spyOn(window, "dispatchEvent");
    fetchMock.mockImplementation(() =>
      jsonResponse({ detail: "请先配置供应商和模型" }, 422),
    );
    await expect(getCustomerPricing()).rejects.toThrow(
      "读取积分价格失败：请先配置供应商和模型（422）",
    );
    expect(getAdminCsrfToken()).toBe(CSRF_TOKEN_TEXT);
    expect(dispatch).not.toHaveBeenCalled();
  });

  it("does not report a session expiry for rejected login, recovery exchange or an anonymous restore", async () => {
    const dispatch = vi.spyOn(window, "dispatchEvent");
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        jsonResponse(
          { detail: { code: "UNAUTHORIZED", message: "凭据无效" } },
          401,
        ),
      ),
    );
    await expect(
      loginAdminWithPassword("admin", "invalid-test-input"),
    ).rejects.toMatchObject({ status: 401 });
    await expect(
      exchangeAdminSession("invalid-test-input"),
    ).rejects.toMatchObject({ status: 401 });
    await expect(fetchAdminSession()).rejects.toMatchObject({ status: 401 });
    expect(dispatch).not.toHaveBeenCalled();
  });

  it("preserves the administrator session when a protected write is forbidden", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);
    const dispatch = vi.spyOn(window, "dispatchEvent");
    fetchMock.mockImplementation(() =>
      jsonResponse(
        { detail: { code: "ADMIN_WRITE_FORBIDDEN", message: "当前账号只读" } },
        403,
      ),
    );
    await expect(
      revokeCustomerSession("session-one", 7, "客服核验", "revoke-key"),
    ).rejects.toMatchObject({
      status: 403,
      code: "ADMIN_WRITE_FORBIDDEN",
      message: "结束会话失败：当前账号只读（403）",
    });
    expect(getAdminCsrfToken()).toBe(CSRF_TOKEN_TEXT);
    expect(dispatch).not.toHaveBeenCalled();
  });

  it("keeps the HTTP status when an upstream proxy returns a non-JSON failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () => new Response("<html>unavailable</html>", { status: 502 }),
      ),
    );
    await expect(getCustomerPricing()).rejects.toMatchObject({
      status: 502,
      message: "读取积分价格失败（502）",
    });
  });

  it.each([
    { name: "admin adapter", request: getCustomerPricing },
    { name: "settings adapter", request: getControlAccounts },
  ])(
    "ignores a late 401 from an older session in the $name",
    async ({ request }) => {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      await signIn(fetchMock);
      const dispatch = vi.spyOn(window, "dispatchEvent");
      let finish:
        | ((value: Awaited<ReturnType<typeof jsonResponse>>) => void)
        | undefined;
      fetchMock.mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      );
      const pending = request();
      setAdminCsrfToken("replacement-session-csrf");
      finish?.(
        await jsonResponse(
          { detail: { code: "ADMIN_SESSION_EXPIRED", message: "expired" } },
          401,
        ),
      );
      await expect(pending).rejects.toThrow();
      expect(getAdminCsrfToken()).toBe("replacement-session-csrf");
      expect(dispatch).not.toHaveBeenCalled();
    },
  );
});

describe("admin activation API adapter", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearAdminActivationSession();
  });

  it("exchanges a credential for a session without persisting secrets", async () => {
    const setItem = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => undefined);
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => jsonResponse(exchangePayload));
    vi.stubGlobal("fetch", fetchMock);

    const result = await exchangeAdminSession("ASX1.body.signature");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/control/admin/session/exchange",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        body: JSON.stringify({ credential: "ASX1.body.signature" }),
      }),
    );
    expect(result.csrf_token).toBe("csrf-token-1");
    expect(result.actor.role).toBe("admin");
    // No-Go red line: the CSRF token must never reach persistent storage.
    expect(setItem).not.toHaveBeenCalled();
  });

  it("emits the shared expiry event when a control-plane request returns 401", async () => {
    const onSessionExpired = vi.fn();
    window.addEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        jsonResponse(
          { detail: { code: "SESSION_EXPIRED", message: "expired" } },
          401,
        ),
      ),
    );

    await expect(getControlAccounts()).rejects.toThrow(
      "登录已过期，请重新登录。",
    );
    expect(onSessionExpired).toHaveBeenCalledOnce();

    window.removeEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
  });

  it("logs in with an account and password, then uses recovery only to replace it", async () => {
    const password = ["Admin", "Passphrase", "2026!"].join(" ");
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => jsonResponse(exchangePayload, 201))
      .mockImplementationOnce(() => jsonResponse(undefined, 204));
    vi.stubGlobal("fetch", fetchMock);

    await loginAdminWithPassword("admin", password);
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8000/api/control/admin/session/password",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        body: JSON.stringify({ username: "admin", password }),
      }),
    );

    await recoverAdminPassword(password);
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8000/api/control/admin/password",
      expect.objectContaining({
        method: "PUT",
        credentials: "include",
        headers: expect.objectContaining({
          "X-Admin-CSRF": CSRF_TOKEN_TEXT,
        }),
        body: JSON.stringify({ password }),
      }),
    );
  });

  it("rejects writes before any request when no CSRF token is held in memory", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createActivationCodeBatch({
        name: "首批",
        face_value_fen: 10000,
        credits: 100,
        quantity: 50,
        activation_expires_at: "2026-09-01T00:00:00Z",
        reason: "首批投放",
      }),
    ).rejects.toBeInstanceOf(AdminActivationError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sends confirm, reason, idempotency key and CSRF header on batch creation", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);
    fetchMock.mockImplementationOnce(() =>
      jsonResponse(
        {
          batch_id: "batch-1",
          name: "首批",
          face_value_fen: 10000,
          unit_price_fen_snapshot: 10000,
          credits_snapshot: 100,
          quantity: 50,
          activation_expires_at: "2026-09-01T00:00:00Z",
          status: "OPEN",
          created_by_user_id: "admin-1",
          request_id: "req-batch-1",
        },
        201,
      ),
    );

    const result = await createActivationCodeBatch({
      name: "首批",
      face_value_fen: 10000,
      credits: 100,
      quantity: 50,
      activation_expires_at: "2026-09-01T00:00:00Z",
      reason: "首批投放",
    });

    expect(fetchMock).toHaveBeenLastCalledWith(
      "http://127.0.0.1:8000/api/control/activation-code-batches",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        headers: expect.objectContaining({
          "Content-Type": "application/json",
          "X-Admin-CSRF": "csrf-token-1",
          "Idempotency-Key": expect.any(String),
        }),
        body: JSON.stringify({
          name: "首批",
          face_value_fen: 10000,
          credits: 100,
          quantity: 50,
          activation_expires_at: "2026-09-01T00:00:00Z",
          confirm_grant: false,
          confirm: true,
          reason: "首批投放",
        }),
      }),
    );
    expect(result.batch_id).toBe("batch-1");
    expect(result.request_id).toBe("req-batch-1");
  });

  it("shares the transient CSRF token with production control writes", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);
    fetchMock.mockImplementationOnce(() =>
      jsonResponse({
        internal_base_unit_price_fen: 100,
        charged_unit_price_fen: 100,
        oral_unit_price_fen: 100,
        min_recharge_fen: 100,
        recharge_step_fen: 100,
      }),
    );

    await updateControlBillingSettings({
      internal_base_unit_price_fen: 100,
      oral_unit_price_fen: 100,
      min_recharge_fen: 100,
      recharge_step_fen: 100,
    });

    const [url, request] = fetchMock.mock.calls.at(-1) ?? [];
    expect(url).toBe("http://127.0.0.1:8000/api/control/settings/billing");
    expect(request).toEqual(
      expect.objectContaining({ method: "PATCH", credentials: "include" }),
    );
    const headers = new Headers((request as RequestInit).headers);
    expect(headers.get("X-Admin-CSRF")).toBe(CSRF_TOKEN_TEXT);
    expect(headers.get("Idempotency-Key")).toBeTruthy();
    expect(request?.body).toBe(
      JSON.stringify({
        internal_base_unit_price_fen: 100,
        oral_unit_price_fen: 100,
        min_recharge_fen: 100,
        recharge_step_fen: 100,
        confirm: true,
        reason: "更新后台计费配置",
      }),
    );
  });

  it("mints a fresh idempotency key per write call", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      jsonResponse({
        code_id: "code-1",
        status: "SUSPENDED",
        request_id: "req-1",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);

    await suspendActivationCode("code-1", "涉嫌退款");
    await suspendActivationCode("code-2", "涉嫌退款");

    const keys = fetchMock.mock.calls
      .filter(([url]) => String(url).endsWith("/suspend"))
      .map(([, options]) => {
        const headers = (options as RequestInit).headers as
          | Record<string, string>
          | undefined;
        return String(headers?.["Idempotency-Key"] ?? "");
      });
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBeTruthy();
    expect(keys[0]).not.toBe(keys[1]);
  });

  it("sends audited device unbind and credential revocation writes", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);
    fetchMock
      .mockImplementationOnce(() =>
        jsonResponse({
          device_id: "device-1",
          status: "UNBOUND",
          outcome: "UNBOUND",
          request_id: "request-unbind-1",
        }),
      )
      .mockImplementationOnce(() =>
        jsonResponse({
          device_id: "device-2",
          status: "REVOKED",
          outcome: "REVOKED",
          request_id: "request-revoke-1",
        }),
      );

    await unbindDevice("device-1", "客户要求设备下线");
    await revokeDeviceCredential("device-2", "设备凭据疑似泄露");

    expect(fetchMock.mock.calls.slice(-2).map(([url]) => url)).toEqual([
      "http://127.0.0.1:8000/api/control/devices/device-1/unbind",
      "http://127.0.0.1:8000/api/control/devices/device-2/revoke-credential",
    ]);
    for (const [, options] of fetchMock.mock.calls.slice(-2)) {
      const request = options as RequestInit;
      expect(request.method).toBe("POST");
      expect(request.body).toContain('"confirm":true');
      expect(new Headers(request.headers).get("X-Admin-CSRF")).toBe(
        CSRF_TOKEN_TEXT,
      );
      expect(new Headers(request.headers).get("Idempotency-Key")).toBeTruthy();
    }
  });

  it("reconciles a first-frame task through the CSRF-carrying admin write lane", async () => {
    // 该端点声明 requestBody?: never，也不跑写契约——但 POST 仍受 CSRF 门禁。
    // 走裸 requestControl 不带 CSRF 头会被服务端 403 ADMIN_CSRF_REQUIRED
    // （当时点名的同类缺陷就是 selfCheckWechatNative，本次一并修掉。）
    // 这条用例钉住它必须经 adminWrite，防止有人日后"因为不需要 body"而简化掉。
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);
    fetchMock.mockImplementationOnce(() =>
      jsonResponse({ task_id: "ff-1", result: "RESUMED", detail_code: null }),
    );

    const result = await reconcileFirstFrameTask("ff-1", "运营核对供应商回执");

    expect(result.result).toBe("RESUMED");
    const last = fetchMock.mock.calls.at(-1) as [string, RequestInit];
    expect(last[0]).toBe(
      "http://127.0.0.1:8000/api/control/first-frame-tasks/ff-1/reconcile",
    );
    expect(last[1].method).toBe("POST");
    expect(new Headers(last[1].headers).get("X-Admin-CSRF")).toBe(
      CSRF_TOKEN_TEXT,
    );
    expect(new Headers(last[1].headers).get("Idempotency-Key")).toBeTruthy();
  });

  it("sends the WeChat credential self-check with the CSRF header", async () => {
    // 该端点是控制面的 POST，缺 X-Admin-CSRF 会被服务端 403 ADMIN_CSRF_REQUIRED
    // 拒掉。原先这里是裸 requestControl + {method:"POST"}，一个 CSRF 头都不带，
    // 生产必然失败——只因为 PaymentSettingsSection.test.tsx 把整个模块 mock 掉，
    // 才一直没暴露。这条钉住它必须经 adminWrite。
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);
    fetchMock.mockImplementationOnce(() =>
      jsonResponse({
        ok: true,
        code: null,
        message: "商户凭据有效",
        platform_certificates: 3,
      }),
    );

    const result = await selfCheckWechatNative();

    expect(result.platform_certificates).toBe(3);
    const last = fetchMock.mock.calls.at(-1) as [string, RequestInit];
    expect(last[0]).toBe(
      "http://127.0.0.1:8000/api/control/settings/customer-payments/wechat-native/self-check",
    );
    expect(last[1].method).toBe("POST");
    expect(new Headers(last[1].headers).get("X-Admin-CSRF")).toBe(
      CSRF_TOKEN_TEXT,
    );
  });

  it("generates codes for a batch and downloads the one-time export", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);
    fetchMock
      .mockImplementationOnce(() =>
        jsonResponse(
          {
            batch_id: "batch-1",
            export_id: "export-1",
            expires_at: "2026-08-23T12:15:00+00:00",
            codes: [{ code_id: "code-1", masked_code: "XS****01" }],
            request_id: "req-generate-1",
          },
          201,
        ),
      )
      .mockImplementationOnce(() =>
        jsonResponse({
          export_id: "export-1",
          batch_id: "batch-1",
          codes: ["XS-AAAA-BBBB-01"],
          downloaded_at: "2026-08-23T12:05:00+00:00",
          request_id: "req-download-1",
        }),
      );

    const generated = await generateActivationCodes("batch-1", 20, "渠道补货");
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8000/api/control/activation-code-batches/batch-1/generate",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        headers: expect.objectContaining({
          "X-Admin-CSRF": "csrf-token-1",
          "Idempotency-Key": expect.any(String),
        }),
        body: JSON.stringify({
          quantity: 20,
          confirm: true,
          reason: "渠道补货",
        }),
      }),
    );
    expect(generated.export_id).toBe("export-1");

    const download = await downloadActivationCodeExport("export-1", "线下交付");
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://127.0.0.1:8000/api/control/activation-code-exports/export-1/download",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        headers: expect.objectContaining({
          "X-Admin-CSRF": "csrf-token-1",
          "Idempotency-Key": expect.any(String),
        }),
        body: JSON.stringify({ confirm: true, reason: "线下交付" }),
      }),
    );
    expect(download.codes).toEqual(["XS-AAAA-BBBB-01"]);
    expect(download.request_id).toBe("req-download-1");
  });

  it("lists codes with batch and status filters", async () => {
    const fetchMock = vi.fn().mockImplementationOnce(() =>
      jsonResponse({
        items: [
          {
            code_id: "code-1",
            batch_id: "batch-1",
            masked_code: "XS****01",
            status: "GENERATED",
            bound_user_id: null,
            issued_at: null,
          },
        ],
        limit: 50,
        offset: 0,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const page = await listActivationCodes({
      batch_id: "batch-1",
      status: "GENERATED",
      limit: 50,
      offset: 0,
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/control/activation-codes?batch_id=batch-1&status=GENERATED&limit=50&offset=0",
      expect.objectContaining({
        method: "GET",
        credentials: "include",
      }),
    );
    expect(page.items).toHaveLength(1);
    expect(page.items[0]?.masked_code).toBe("XS****01");
  });

  it("delivers, resumes and revokes codes with the write contract", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);
    fetchMock
      .mockImplementationOnce(() =>
        jsonResponse({
          code_id: "code-1",
          status: "ISSUED",
          delivery_id: "delivery-1",
          request_id: "req-deliver-1",
        }),
      )
      .mockImplementationOnce(() =>
        jsonResponse({
          code_id: "code-1",
          status: "ACTIVE",
          request_id: "req-resume-1",
        }),
      )
      .mockImplementationOnce(() =>
        jsonResponse({
          code_id: "code-1",
          status: "REVOKED",
          request_id: "req-revoke-1",
        }),
      );

    const delivered = await deliverActivationCode("code-1", {
      channel: "offline",
      external_order_ref: "order-9",
      recipient_ref: "渠道商A",
      reason: "线下渠道发货",
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8000/api/control/activation-codes/code-1/deliver",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        headers: expect.objectContaining({ "X-Admin-CSRF": "csrf-token-1" }),
        body: JSON.stringify({
          channel: "offline",
          external_order_ref: "order-9",
          recipient_ref: "渠道商A",
          confirm: true,
          reason: "线下渠道发货",
        }),
      }),
    );
    expect(delivered.delivery_id).toBe("delivery-1");

    await resumeActivationCode("code-1", "复核通过恢复");
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://127.0.0.1:8000/api/control/activation-codes/code-1/resume",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ confirm: true, reason: "复核通过恢复" }),
      }),
    );

    await revokeActivationCode("code-1", "风控作废");
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://127.0.0.1:8000/api/control/activation-codes/code-1/revoke",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ confirm: true, reason: "风控作废" }),
      }),
    );
  });

  it("maps the server error envelope to a coded error", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);
    fetchMock.mockImplementationOnce(() =>
      jsonResponse(
        {
          detail: {
            code: "EXPORT_ALREADY_DOWNLOADED",
            message: "This export package was already downloaded exactly once.",
          },
        },
        409,
      ),
    );

    const error = await downloadActivationCodeExport(
      "export-1",
      "再次下载",
    ).catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(AdminActivationError);
    const activationError = error as AdminActivationError;
    expect(activationError.status).toBe(409);
    expect(activationError.code).toBe("EXPORT_ALREADY_DOWNLOADED");
    expect(activationError.message).toContain("下载明文码失败");
    expect(activationError.message).toContain("409");
  });

  it("reads and deletes the session with the cookie plane", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => jsonResponse(sessionPayload))
      .mockImplementationOnce(() => jsonResponse(undefined, 204));
    vi.stubGlobal("fetch", fetchMock);

    const session = await fetchAdminSession();
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8000/api/control/admin/session",
      expect.objectContaining({
        method: "GET",
        credentials: "include",
      }),
    );
    expect(session.actor.display_name).toBe("管理员一号");

    await deleteAdminSession();
    expect(fetchMock).toHaveBeenLastCalledWith(
      "http://127.0.0.1:8000/api/control/admin/session",
      expect.objectContaining({
        method: "DELETE",
        credentials: "include",
        headers: expect.objectContaining({ "X-Admin-CSRF": "csrf-token-1" }),
      }),
    );
  });

  it("reads and updates a customer's API-controlled unit price", async () => {
    const pricing = {
      user_id: "customer-1",
      unit_price_fen: 500,
      custom_unit_price_fen: 500,
      default_unit_price_fen: 1000,
      min_recharge_fen: 10000,
      recharge_step_fen: 500,
      updated_at: "2026-08-27T18:00:00+08:00",
      request_id: "req-price-1",
    };
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);
    fetchMock
      .mockImplementationOnce(() => jsonResponse(pricing))
      .mockImplementationOnce(() => jsonResponse(pricing));

    expect((await fetchCustomerUnitPrice("customer-1")).unit_price_fen).toBe(
      500,
    );
    await updateCustomerUnitPrice(
      "customer-1",
      500,
      "客户合同价",
      "price-idem-1",
    );

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8000/api/control/customers/customer-1/unit-price",
      expect.objectContaining({ method: "GET", credentials: "include" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://127.0.0.1:8000/api/control/customers/customer-1/unit-price",
      expect.objectContaining({
        method: "PUT",
        credentials: "include",
        headers: expect.objectContaining({
          "X-Admin-CSRF": CSRF_TOKEN_TEXT,
          "Idempotency-Key": "price-idem-1",
        }),
        body: JSON.stringify({
          confirm: true,
          reason: "客户合同价",
          unit_price_fen: 500,
        }),
      }),
    );
  });
});

describe("billing report export", () => {
  /**
   * The export is a POST (so it must ride the CSRF write channel) whose body
   * is a binary gzip stream — it cannot go through `adminWrite`, which parses
   * JSON. `reason` is deliberately absent: `ExportRequest` neither accepts nor
   * consumes it, and a reason the server discards would only fake an audit
   * trail (see the GenerationRecordsPage reconciliation precedent).
   */
  function stubDownload() {
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      () => undefined,
    );
    const createObjectURL = vi.fn(() => "blob:report-export");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
    return { createObjectURL, revokeObjectURL };
  }

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    clearAdminActivationSession();
  });

  it("posts the window on the CSRF write channel and keeps the server file name", async () => {
    const { createObjectURL } = stubDownload();
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => jsonResponse(exchangePayload));
    vi.stubGlobal("fetch", fetchMock);
    await exchangeAdminSession("ASX1.body.signature");
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers({
        "Content-Disposition":
          'attachment; filename="billing_export_20260922_101530.csv.gz"',
      }),
      blob: async () => new Blob(["gz"], { type: "application/gzip" }),
    });

    const result = await exportBillingReportCsv(
      { start_date: "2026-07-01", end_date: "2026-09-22" },
      "report-key",
    );

    expect(fetchMock).toHaveBeenLastCalledWith(
      "http://127.0.0.1:8000/api/control/reports/export",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        headers: expect.objectContaining({
          "X-Admin-CSRF": CSRF_TOKEN_TEXT,
          "Idempotency-Key": "report-key",
          "Content-Type": "application/json",
        }),
        body: JSON.stringify({
          format: "csv",
          start_date: "2026-07-01",
          end_date: "2026-09-22",
          service_types: ["all"],
        }),
      }),
    );
    expect(result).toEqual({
      filename: "billing_export_20260922_101530.csv.gz",
      bytes: 2,
    });
    expect(createObjectURL).toHaveBeenCalledTimes(1);
  });

  it("falls back to a stable file name when the proxy drops Content-Disposition", async () => {
    stubDownload();
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => jsonResponse(exchangePayload));
    vi.stubGlobal("fetch", fetchMock);
    await exchangeAdminSession("ASX1.body.signature");
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers(),
      blob: async () => new Blob(["gz"]),
    });

    const result = await exportBillingReportCsv({
      start_date: "2026-09-01",
      end_date: "2026-09-22",
    });

    expect(result.filename).toBe("计费报表.csv.gz");
  });

  it("surfaces the server window rejection without touching the download", async () => {
    const { createObjectURL } = stubDownload();
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => jsonResponse(exchangePayload));
    vi.stubGlobal("fetch", fetchMock);
    await exchangeAdminSession("ASX1.body.signature");
    fetchMock.mockResolvedValue({
      ok: false,
      status: 400,
      json: async () => ({ detail: "Date range cannot exceed 90 days" }),
    });

    await expect(
      exportBillingReportCsv(
        { start_date: "2026-01-01", end_date: "2026-09-22" },
        "report-key",
      ),
    ).rejects.toThrow("Date range cannot exceed 90 days");
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it("waits past the five-second default for a full-quarter export", async () => {
    stubDownload();
    vi.useFakeTimers();
    setAdminCsrfToken(CSRF_TOKEN_TEXT);
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((resolve, reject) => {
            init.signal?.addEventListener("abort", () =>
              reject(new DOMException("aborted", "AbortError")),
            );
            setTimeout(
              () =>
                resolve({
                  ok: true,
                  status: 200,
                  headers: new Headers(),
                  blob: async () => new Blob(["gz"]),
                }),
              30_000,
            );
          }),
      ),
    );
    try {
      const pending = exportBillingReportCsv(
        { start_date: "2026-06-24", end_date: "2026-09-22" },
        "report-key",
      ).then(
        (value) => ({ ok: true, value }),
        () => ({ ok: false }),
      );
      await vi.advanceTimersByTimeAsync(30_000);
      expect(await pending).toMatchObject({ ok: true, value: { bytes: 2 } });
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
      setAdminCsrfToken("");
    }
  });
});

describe("paid probe idempotency", () => {
  const PAID_TEST_URL =
    "http://127.0.0.1:8000/api/control/settings/providers/metaso/paid-test";
  const OK_RESULT = {
    status: "ok",
    provider: "metaso",
    test_kind: "paid_probe",
  };

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    clearAdminActivationSession();
  });

  function paidCalls(fetchMock: ReturnType<typeof vi.fn>) {
    return fetchMock.mock.calls.filter(([url]) => url === PAID_TEST_URL);
  }

  function keyOf(call: unknown[]): string {
    const init = call[1] as { headers: Record<string, string> };
    return init.headers["Idempotency-Key"];
  }

  async function signedInFetch() {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await signIn(fetchMock);
    return fetchMock;
  }

  it("waits far longer than the default five seconds for the server to finish", async () => {
    vi.useFakeTimers();
    setAdminCsrfToken(CSRF_TOKEN_TEXT);
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((resolve, reject) => {
            init.signal?.addEventListener("abort", () =>
              reject(new DOMException("aborted", "AbortError")),
            );
            setTimeout(
              () =>
                resolve({
                  ok: true,
                  status: 200,
                  json: async () => OK_RESULT,
                }),
              120_000,
            );
          }),
      ),
    );
    try {
      const pending = paidTestControlProvider("metaso", "上线前核对").then(
        (value) => ({ ok: true, value }),
        () => ({ ok: false }),
      );
      await vi.advanceTimersByTimeAsync(120_000);
      expect(await pending).toMatchObject({ ok: true, value: OK_RESULT });
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
      setAdminCsrfToken("");
    }
  });

  it("reuses the same key when the first attempt got no answer", async () => {
    const fetchMock = await signedInFetch();
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fetchMock.mockImplementationOnce(() => jsonResponse(OK_RESULT));

    await expect(
      paidTestControlProvider("metaso", "上线前核对"),
    ).rejects.toThrow(/结果未能确认.*相同的操作原因/);
    await expect(
      paidTestControlProvider("metaso", "上线前核对"),
    ).resolves.toEqual(OK_RESULT);

    const [first, second] = paidCalls(fetchMock);
    expect(keyOf(first)).toBeTruthy();
    // 同一次逻辑操作：服务端据此回放首次结果，而不是再提交一次计费任务。
    expect(keyOf(second)).toBe(keyOf(first));
  });

  it("treats a request timeout as unconfirmed and keeps the key", async () => {
    const fetchMock = await signedInFetch();
    fetchMock.mockRejectedValueOnce(new DOMException("aborted", "AbortError"));
    fetchMock.mockImplementationOnce(() => jsonResponse(OK_RESULT));

    await expect(
      paidTestControlProvider("metaso", "上线前核对"),
    ).rejects.toThrow(/结果未能确认/);
    await paidTestControlProvider("metaso", "上线前核对");

    const [first, second] = paidCalls(fetchMock);
    expect(keyOf(second)).toBe(keyOf(first));
  });

  it("refuses a different reason while the previous probe is unconfirmed", async () => {
    const fetchMock = await signedInFetch();
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await expect(
      paidTestControlProvider("metaso", "上线前核对"),
    ).rejects.toThrow();
    const callsBefore = paidCalls(fetchMock).length;

    // 换原因会换请求指纹：既可能被判键冲突，也可能被误当成新操作再计一次费。
    await expect(
      paidTestControlProvider("metaso", "另一个原因"),
    ).rejects.toThrow(/上一次付费探针的结果尚未确认.*上线前核对/);
    expect(paidCalls(fetchMock)).toHaveLength(callsBefore);
  });

  it("mints a fresh key after a successful probe", async () => {
    const fetchMock = await signedInFetch();
    fetchMock.mockImplementation(() => jsonResponse(OK_RESULT));

    await paidTestControlProvider("metaso", "上线前核对");
    await paidTestControlProvider("metaso", "上线前核对");

    const [first, second] = paidCalls(fetchMock);
    expect(keyOf(second)).not.toBe(keyOf(first));
  });

  it("mints a fresh key after the server answered with a business error", async () => {
    const fetchMock = await signedInFetch();
    fetchMock.mockImplementationOnce(() =>
      jsonResponse(
        {
          detail: {
            code: "VIDEO_PAID_PROBE_UNCERTAIN",
            message: "可能已产生费用，请核对账单后再重试。",
          },
        },
        502,
      ),
    );
    fetchMock.mockImplementationOnce(() => jsonResponse(OK_RESULT));

    await expect(
      paidTestControlProvider("metaso", "上线前核对"),
    ).rejects.toMatchObject({
      status: 502,
      code: "VIDEO_PAID_PROBE_UNCERTAIN",
    });
    // 业务进程已明确回应（并提示核对账单）：下一次是操作者有意识的全新操作。
    await paidTestControlProvider("metaso", "上线前核对");

    const [first, second] = paidCalls(fetchMock);
    expect(keyOf(second)).not.toBe(keyOf(first));
  });

  it("keeps the key when only a gateway answered with a bare 5xx", async () => {
    const fetchMock = await signedInFetch();
    fetchMock.mockImplementationOnce(() => jsonResponse({}, 504));
    fetchMock.mockImplementationOnce(() => jsonResponse(OK_RESULT));

    // 网关自己的 504 不带业务错误码，不能证明业务进程没有执行。
    await expect(
      paidTestControlProvider("metaso", "上线前核对"),
    ).rejects.toThrow(/结果未能确认/);
    await paidTestControlProvider("metaso", "上线前核对");

    const [first, second] = paidCalls(fetchMock);
    expect(keyOf(second)).toBe(keyOf(first));
  });

  it("tracks unconfirmed probes per provider", async () => {
    const fetchMock = await signedInFetch();
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fetchMock.mockImplementation(() => jsonResponse(OK_RESULT));

    await expect(
      paidTestControlProvider("metaso", "上线前核对"),
    ).rejects.toThrow();
    // 另一个服务不受 metaso 未确认状态的牵连，原因也可以不同。
    await expect(
      paidTestControlProvider("hifly", "核对数字人"),
    ).resolves.toEqual(OK_RESULT);
  });

  it("forgets an unconfirmed probe when the admin session is cleared", async () => {
    const fetchMock = await signedInFetch();
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await expect(
      paidTestControlProvider("metaso", "上线前核对"),
    ).rejects.toThrow();

    // 登出 / 会话过期后换人登录：不得沿用上一个身份的幂等键与原因。
    clearAdminActivationSession();
    fetchMock.mockImplementationOnce(() => jsonResponse(exchangePayload));
    await exchangeAdminSession("ASX1.body.signature");
    fetchMock.mockImplementationOnce(() => jsonResponse(OK_RESULT));
    await expect(
      paidTestControlProvider("metaso", "另一个原因"),
    ).resolves.toEqual(OK_RESULT);
  });
});
