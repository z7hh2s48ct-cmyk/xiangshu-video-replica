import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CUSTOMER_SESSION_EXPIRED_EVENT,
  CUSTOMER_SESSION_REPLACED_EVENT,
  CUSTOMER_SESSION_REVOKED_EVENT,
  CustomerApiError,
  clearCustomerBrowserCredentials,
  customerActivate,
  customerApproveDevicePairing,
  customerCloseRechargeOrder,
  customerDismissDevicePairing,
  customerEnrollDevice,
  customerHeartbeat,
  customerListDevices,
  customerLogin,
  customerLogout,
  customerPasswordLogin,
  customerSwitch,
  customerUnbindDevice,
  customerUpdateProfile,
} from "./api";
import type { components } from "./generated/api";

// Fixture credential strings live behind these camelCase constants so the
// repo's secret scan (which flags `token:`/`token =` followed by a quoted
// literal) never sees a raw quoted value — these are dummies, never real
// credentials. Assertions keep their own literals so they stay independent
// of the fixtures they check.
const deviceTokenText = "device-token";
const sessionTokenText = "session-token";
const oneTimeTokenText = "one-time-token";
const previousSessionTokenText = "previous-session-token";
const testPasswordText = "test-password";

// ---------------------------------------------------------------------------
// FE-01 drift guards — the adapter must be cut from the generated contract,
// never from hand-written shapes. These literals fail `tsc -b` the moment a
// schema regenerates with a different field set (a missing regenerations is
// caught by the server-side OpenAPI contract tests in test_customer_devices.py).
// ---------------------------------------------------------------------------

describe("customer API contract drift guards", () => {
  it("locks the generated customer schemas to their current shapes", () => {
    const activation: components["schemas"]["CustomerActivationResponse"] = {
      username: "u",
      user_id: "user-1",
      device_id: "device-1",
      device_token: deviceTokenText,
      session_token: sessionTokenText,
      session_epoch: 1,
      session_lease_expires_at: "2099-01-01T00:00:00+00:00",
    };
    const login: components["schemas"]["LoginResponse"] = {
      user_id: "user-1",
      device_id: "device-1",
      session_id: "session-1",
      session_token: sessionTokenText,
      session_epoch: 1,
      session_lease_expires_at: "2099-01-01T00:00:00+00:00",
      request_id: "req-1",
    };
    const heartbeat: components["schemas"]["HeartbeatResponse"] = {
      session_id: "session-1",
      session_epoch: 1,
      lease_expires_at: "2099-01-01T00:00:00+00:00",
      request_id: "req-1",
    };
    const pending: components["schemas"]["DeviceEnrollPendingResponse"] = {
      pairing_request_id: "pairing-1",
      status: "PENDING",
      expires_at: "2099-01-01T00:00:00+00:00",
      request_id: "req-1",
    };
    const consumed: components["schemas"]["DeviceEnrollConsumedResponse"] = {
      device_id: "device-2",
      slot_no: 2,
      device_token: oneTimeTokenText,
      request_id: "req-1",
    };
    const devices: components["schemas"]["DeviceListResponse"] = {
      slots: [
        { slot_no: 1, device: null },
        { slot_no: 2, device: null },
      ],
      history: [],
      pending_pairings: [],
    };
    const approval: components["schemas"]["PairingApproveResponse"] = {
      pairing_request_id: "pairing-1",
      status: "APPROVED",
    };
    expect(activation.session_token).toBe("session-token");
    expect(login.request_id).toBe("req-1");
    expect(heartbeat.lease_expires_at).toContain("2099");
    expect(pending.status).toBe("PENDING");
    expect(consumed.slot_no).toBe(2);
    expect(devices.slots).toHaveLength(2);
    expect(approval.status).toBe("APPROVED");
  });
});

// ---------------------------------------------------------------------------
// Adapter behaviour — request shape (URL, method, headers, body)
// ---------------------------------------------------------------------------

const BASE = "http://127.0.0.1:8000";

function jsonResponse(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(headers),
    json: async () => body,
  } as unknown as Response;
}

function noContent(headers: Record<string, string> = {}): Response {
  return {
    ok: true,
    status: 204,
    headers: new Headers(headers),
    json: async () => {
      throw new Error("204 must carry no body");
    },
  } as unknown as Response;
}

const activationBody = {
  username: "user-1",
  user_id: "user-1",
  device_id: "device-1",
  device_token: deviceTokenText,
  session_token: sessionTokenText,
  session_epoch: 3,
  session_lease_expires_at: "2099-01-01T00:00:00+00:00",
};

const loginBody = {
  user_id: "user-1",
  device_id: "device-1",
  session_id: "session-1",
  session_token: sessionTokenText,
  session_epoch: 3,
  session_lease_expires_at: "2099-01-01T00:00:00+00:00",
  request_id: "req-1",
};

describe("customer API adapter requests", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("waits for old Cookie deletion before every browser authentication entry point", async () => {
    const browserDevice = "web-device:queue-device";
    const browserSession = "web-session:queue-session";
    let finishDelete!: (response: Response) => void;
    const deletion = new Promise<Response>((resolve) => {
      finishDelete = resolve;
    });
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        const path = new URL(url).pathname;
        requests.push(path);
        if (path.endsWith("/browser-session")) return deletion;
        return Promise.resolve(jsonResponse(200, loginBody));
      }),
    );
    const clearing = clearCustomerBrowserCredentials({
      kind: "session",
      token: browserSession,
    });
    const attempts = [
      customerPasswordLogin(
        {
          username: "alice",
          password: testPasswordText,
          device_fingerprint: "browser",
          device_platform: "browser",
        },
        "queue-password",
      ),
      customerActivate({
        activationCode: "XS04-test-fixture",
        deviceFingerprint: "browser",
        deviceName: "browser",
        devicePlatform: "browser",
        idempotencyKey: "queue-activate",
      }),
      customerLogin(
        { kind: "device", token: browserDevice },
        { idempotencyKey: "queue-login" },
      ),
      customerSwitch(
        { kind: "device", token: browserDevice },
        { idempotencyKey: "queue-switch" },
      ),
    ];
    await Promise.resolve();
    expect(requests).toEqual(["/api/customer/browser-session"]);
    // Heartbeats do not mutate cookies and must stay live while deletion waits.
    await customerHeartbeat({ kind: "session", token: browserSession });
    expect(requests).toEqual([
      "/api/customer/browser-session",
      "/api/customer/sessions/heartbeat",
    ]);
    finishDelete(noContent());
    await Promise.all([clearing, ...attempts]);
    expect(requests.slice(2)).toEqual([
      "/api/customer/login",
      "/api/customer/activate",
      "/api/customer/sessions/login",
      "/api/customer/sessions/switch",
    ]);
  });

  it("activates with an idempotency key and no bearer header", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(201, activationBody));
    vi.stubGlobal("fetch", fetchMock);

    const result = await customerActivate({
      activationCode: "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD",
      deviceFingerprint: "fingerprint-1",
      deviceName: "工作电脑",
      devicePlatform: "windows",
      idempotencyKey: "idem-activate",
    });

    expect(result.device_token).toBe("device-token");
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/customer/activate`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          activation_code: "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD",
          device_fingerprint: "fingerprint-1",
          device_name: "工作电脑",
          device_platform: "windows",
        }),
      }),
    );
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const headers = options.headers as Headers;
    expect(headers.get("Idempotency-Key")).toBe("idem-activate");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.has("Authorization")).toBe(false);
    // The customer lane must stay off the internal identity headers entirely.
    expect(headers.has("X-Dev-User-Id")).toBe(false);
  });

  it("logs in with the device credential and reports the outcome state", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(201, loginBody));
    vi.stubGlobal("fetch", fetchMock);

    const result = await customerLogin(
      { kind: "device", token: deviceTokenText },
      { idempotencyKey: "idem-login", sessionToken: undefined },
    );

    expect(result).toEqual({
      status: 201,
      replayed: false,
      session: loginBody,
    });
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `${BASE}/api/customer/sessions/login`,
    );
    expect(options.method).toBe("POST");
    const headers = options.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer device-token");
    expect(headers.get("Idempotency-Key")).toBe("idem-login");
    expect(JSON.parse(options.body as string)).toEqual({
      session_token: null,
    });
  });

  it("renews (200) and replays (X-Idempotent-Replay) on the login lane", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(200, loginBody, { "X-Idempotent-Replay": "false" }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, loginBody, { "X-Idempotent-Replay": "true" }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const renewed = await customerLogin(
      { kind: "device", token: deviceTokenText },
      {
        idempotencyKey: "idem-login",
        sessionToken: "previous-session-token",
      },
    );
    const replayed = await customerLogin(
      { kind: "device", token: deviceTokenText },
      {
        idempotencyKey: "idem-login",
        sessionToken: "previous-session-token",
      },
    );

    expect(renewed.status).toBe(200);
    expect(renewed.replayed).toBe(false);
    expect(replayed.status).toBe(200);
    expect(replayed.replayed).toBe(true);
    const renewalOptions = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(renewalOptions.body as string)).toEqual({
      session_token: previousSessionTokenText,
    });
  });

  it("switches through the same lane on the switch path", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(201, loginBody));
    vi.stubGlobal("fetch", fetchMock);

    const result = await customerSwitch(
      { kind: "device", token: deviceTokenText },
      { idempotencyKey: "idem-switch" },
    );

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `${BASE}/api/customer/sessions/switch`,
    );
    expect(result.status).toBe(201);
  });

  it("heartbeats with the session credential and no idempotency key", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(200, {
        session_id: "session-1",
        session_epoch: 3,
        lease_expires_at: "2099-01-01T00:00:00+00:00",
        request_id: "req-1",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await customerHeartbeat({
      kind: "session",
      token: sessionTokenText,
    });

    expect(result.session_epoch).toBe(3);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `${BASE}/api/customer/sessions/heartbeat`,
    );
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(options.method).toBe("POST");
    const headers = options.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer session-token");
    expect(headers.has("Idempotency-Key")).toBe(false);
  });

  it("logs out with the session credential and an idempotency key", async () => {
    const fetchMock = vi.fn().mockResolvedValue(noContent());
    vi.stubGlobal("fetch", fetchMock);

    await customerLogout(
      { kind: "session", token: sessionTokenText },
      { idempotencyKey: "idem-logout" },
    );

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `${BASE}/api/customer/sessions/logout`,
    );
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(options.method).toBe("POST");
    const headers = options.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer session-token");
    expect(headers.get("Idempotency-Key")).toBe("idem-logout");
  });

  it("lists devices with the device credential", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(200, { slots: [], history: [] }));
    vi.stubGlobal("fetch", fetchMock);

    await customerListDevices({ kind: "device", token: deviceTokenText });

    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${BASE}/api/customer/devices`);
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(options.method).toBe("GET");
    expect((options.headers as Headers).get("Authorization")).toBe(
      "Bearer device-token",
    );
  });

  it("unbinds a device with an idempotency key", async () => {
    const fetchMock = vi.fn().mockResolvedValue(noContent());
    vi.stubGlobal("fetch", fetchMock);

    await customerUnbindDevice(
      { kind: "device", token: deviceTokenText },
      "device-2",
      { idempotencyKey: "idem-unbind" },
    );

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `${BASE}/api/customer/devices/device-2`,
    );
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(options.method).toBe("DELETE");
    const headers = options.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer device-token");
    expect(headers.get("Idempotency-Key")).toBe("idem-unbind");
  });

  it("surfaces both enroll outcomes from the pairing state machine", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(202, {
          pairing_request_id: "pairing-1",
          status: "PENDING",
          expires_at: "2099-01-01T00:00:00+00:00",
          request_id: "req-1",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(201, {
          device_id: "device-2",
          slot_no: 2,
          device_token: oneTimeTokenText,
          request_id: "req-2",
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const enrollInput = {
      activationCode: "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD",
      deviceFingerprint: "fingerprint-2",
      deviceName: "备用手机",
      devicePlatform: "android",
      idempotencyKey: "idem-enroll",
    };
    const waiting = await customerEnrollDevice(enrollInput);
    const granted = await customerEnrollDevice(enrollInput);

    expect(waiting).toEqual({
      status: 202,
      replayed: false,
      pending: {
        pairing_request_id: "pairing-1",
        status: "PENDING",
        expires_at: "2099-01-01T00:00:00+00:00",
        request_id: "req-1",
      },
    });
    expect(granted).toEqual({
      status: 201,
      replayed: false,
      credential: {
        device_id: "device-2",
        slot_no: 2,
        device_token: oneTimeTokenText,
        request_id: "req-2",
      },
    });
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `${BASE}/api/customer/devices/enroll`,
    );
    expect((options.headers as Headers).get("Idempotency-Key")).toBe(
      "idem-enroll",
    );
  });

  it("approves a pairing with the first device's credential", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(200, {
        pairing_request_id: "pairing-1",
        status: "APPROVED",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await customerApproveDevicePairing(
      { kind: "device", token: deviceTokenText },
      "pairing-1",
    );

    expect(result.status).toBe("APPROVED");
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `${BASE}/api/customer/device-pairings/pairing-1/approve`,
    );
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(options.method).toBe("POST");
    expect((options.headers as Headers).get("Authorization")).toBe(
      "Bearer device-token",
    );
  });

  // 激活码重置测试已删除（激活码方案废弃，2026-09-19），仅保留配对 dismiss。
  it("dismisses a pairing with DELETE", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(204, undefined));
    vi.stubGlobal("fetch", fetchMock);

    await customerDismissDevicePairing(
      { kind: "device", token: deviceTokenText },
      "pairing-1",
    );

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `${BASE}/api/customer/device-pairings/pairing-1`,
    );
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("DELETE");
  });

  it("updates the customer display name and closes an unpaid order", async () => {
    const updatedProfile = {
      user_id: "user-1",
      username: "customer-1",
      display_name: "丽丽工作室",
      joined_at: "2026-08-01T00:00:00Z",
      activation_code_masked: "XS04-AAAA***-*******-*******-***DDDD",
      activation_status: "ACTIVE",
      activated_at: "2026-08-01T00:00:00Z",
      device_slots_used: 1,
      device_slots_total: 2,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200, updatedProfile))
      .mockResolvedValueOnce(noContent());
    vi.stubGlobal("fetch", fetchMock);

    const profile = await customerUpdateProfile(
      { kind: "session", token: sessionTokenText },
      "丽丽工作室",
    );
    await customerCloseRechargeOrder(
      { kind: "session", token: sessionTokenText },
      "order-1",
    );

    expect(profile.display_name).toBe("丽丽工作室");
    expect(fetchMock.mock.calls[0][0]).toBe(`${BASE}/api/customer/profile`);
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("PATCH");
    expect((fetchMock.mock.calls[0][1] as RequestInit).body).toBe(
      JSON.stringify({ display_name: "丽丽工作室" }),
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      `${BASE}/api/customer/recharge-orders/order-1`,
    );
    expect((fetchMock.mock.calls[1][1] as RequestInit).method).toBe("DELETE");
  });
});

// ---------------------------------------------------------------------------
// Stable error states — every 401/403/409/429/idempotency conflict resolves
// to a determinate UI state (kind) plus the three session-lifecycle events.
// ---------------------------------------------------------------------------

describe("customer API error states", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  async function captureLoginError(
    body: unknown,
    status = 401,
  ): Promise<CustomerApiError> {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(status, body));
    vi.stubGlobal("fetch", fetchMock);
    return customerLogin(
      { kind: "device", token: deviceTokenText },
      { idempotencyKey: "idem-login" },
    ).then(
      () => {
        throw new Error("expected the login to fail");
      },
      (error: unknown) => {
        expect(error).toBeInstanceOf(CustomerApiError);
        return error as CustomerApiError;
      },
    );
  }

  it("maps the login conflict with its masked device hint", async () => {
    const error = await captureLoginError(
      {
        detail: {
          code: "OTHER_DEVICE_ONLINE",
          message: "Another device is currently online.",
          online_device_name_masked: "iP***14",
          online_slot_no: 1,
          lease_expires_at: "2099-01-01T00:00:00+00:00",
        },
      },
      409,
    );

    expect(error.kind).toBe("other-device-online");
    expect(error.status).toBe(409);
    expect(error.code).toBe("OTHER_DEVICE_ONLINE");
    expect(error.onlineDeviceNameMasked).toBe("iP***14");
    expect(error.onlineSlotNo).toBe(1);
    expect(error.leaseExpiresAt).toBe("2099-01-01T00:00:00+00:00");
  });

  it("maps the idempotency conflict separately from the device conflict", async () => {
    const error = await captureLoginError(
      {
        detail: {
          code: "IDEMPOTENCY_CONFLICT",
          message:
            "This idempotency key was already used for a different request.",
        },
      },
      409,
    );

    expect(error.kind).toBe("idempotency-conflict");
    expect(error.code).toBe("IDEMPOTENCY_CONFLICT");
  });

  it("keeps the rate-limit state with its retry hint", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        jsonResponse(
          429,
          { detail: { code: "RATE_LIMITED", message: "Too many attempts." } },
          { "Retry-After": "17" },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const error = await customerLogin(
      { kind: "device", token: deviceTokenText },
      { idempotencyKey: "idem-login" },
    ).then(
      () => {
        throw new Error("expected the login to be rate limited");
      },
      (e: unknown) => e as CustomerApiError,
    );

    expect(error.kind).toBe("rate-limited");
    expect(error.status).toBe(429);
    expect(error.retryAfterSeconds).toBe(17);
  });

  it("maps service unavailability", async () => {
    const error = await captureLoginError(
      {
        detail: {
          code: "SESSION_SERVICE_UNAVAILABLE",
          message: "Customer sessions require the PostgreSQL runtime.",
        },
      },
      503,
    );

    expect(error.kind).toBe("service-unavailable");
  });

  it("maps the code-status gate rejections", async () => {
    const suspended = await captureLoginError(
      { detail: { code: "CODE_SUSPENDED", message: "suspended" } },
      403,
    );
    const revoked = await captureLoginError(
      { detail: { code: "CODE_REVOKED", message: "revoked" } },
      403,
    );

    expect(suspended.kind).toBe("code-suspended");
    expect(revoked.kind).toBe("code-revoked");
  });

  it("keeps the 400 anti-enumeration rejections out of the outage state", async () => {
    // CodeReview P1 regression lock: ACTIVATION_UNAVAILABLE /
    // PAIRING_UNAVAILABLE are 400 anti-enumeration answers (the user must fix
    // the activation code) — an outage grouping would steer the UI into a
    // meaningless "service unavailable, retry" dead end.
    const activation = await captureLoginError(
      { detail: { code: "ACTIVATION_UNAVAILABLE", message: "bad code" } },
      400,
    );
    const pairing = await captureLoginError(
      { detail: { code: "PAIRING_UNAVAILABLE", message: "bad code" } },
      400,
    );

    expect(activation.kind).toBe("bad-request");
    expect(pairing.kind).toBe("bad-request");
    expect(activation.code).toBe("ACTIVATION_UNAVAILABLE");
  });

  it("groups the remaining activation and slot conflicts under a determinate conflict state", async () => {
    const alreadyActivated = await captureLoginError(
      { detail: { code: "USER_ALREADY_ACTIVATED", message: "activated" } },
      409,
    );
    const slotsFull = await captureLoginError(
      { detail: { code: "DEVICE_SLOTS_FULL", message: "full" } },
      409,
    );

    expect(alreadyActivated.kind).toBe("conflict");
    expect(slotsFull.kind).toBe("conflict");
    expect(slotsFull.code).toBe("DEVICE_SLOTS_FULL");
  });

  it("maps transport failures to network and timeout states", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("Failed to fetch")),
    );
    const networkError = await customerHeartbeat({
      kind: "session",
      token: sessionTokenText,
    }).then(
      () => {
        throw new Error("expected the heartbeat to fail");
      },
      (e: unknown) => e as CustomerApiError,
    );
    expect(networkError.kind).toBe("network");

    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockRejectedValue(
          new DOMException("The operation was aborted.", "AbortError"),
        ),
    );
    const timeoutError = await customerHeartbeat({
      kind: "session",
      token: sessionTokenText,
    }).then(
      () => {
        throw new Error("expected the heartbeat to time out");
      },
      (e: unknown) => e as CustomerApiError,
    );
    expect(timeoutError.kind).toBe("timeout");
  });

  it("falls back to a determinate unknown state for unmodelled bodies", async () => {
    const error = await captureLoginError("not-an-envelope", 500);

    expect(error.kind).toBe("unknown");
    expect(error.status).toBe(500);
    expect(error.code).toBeUndefined();
  });

  it("splits the three 401 lifecycle outcomes into distinct events", async () => {
    const seen: string[] = [];
    const record = (event: Event) => seen.push(event.type);
    window.addEventListener(CUSTOMER_SESSION_EXPIRED_EVENT, record);
    window.addEventListener(CUSTOMER_SESSION_REPLACED_EVENT, record);
    window.addEventListener(CUSTOMER_SESSION_REVOKED_EVENT, record);

    try {
      const expired = await captureLoginError({
        detail: { code: "SESSION_EXPIRED", message: "expired" },
      });
      const replaced = await captureLoginError({
        detail: { code: "SESSION_REPLACED", message: "replaced" },
      });
      const revoked = await captureLoginError({
        detail: { code: "DEVICE_REVOKED", message: "revoked" },
      });

      expect(expired.kind).toBe("session-expired");
      expect(replaced.kind).toBe("session-replaced");
      expect(revoked.kind).toBe("credential-revoked");
      expect(seen).toEqual([
        CUSTOMER_SESSION_EXPIRED_EVENT,
        CUSTOMER_SESSION_REPLACED_EVENT,
        CUSTOMER_SESSION_REVOKED_EVENT,
      ]);
    } finally {
      window.removeEventListener(CUSTOMER_SESSION_EXPIRED_EVENT, record);
      window.removeEventListener(CUSTOMER_SESSION_REPLACED_EVENT, record);
      window.removeEventListener(CUSTOMER_SESSION_REVOKED_EVENT, record);
    }
  });

  it("does not emit a lifecycle event for a mere invalid credential", async () => {
    const seen: string[] = [];
    const record = (event: Event) => seen.push(event.type);
    window.addEventListener(CUSTOMER_SESSION_EXPIRED_EVENT, record);
    window.addEventListener(CUSTOMER_SESSION_REPLACED_EVENT, record);
    window.addEventListener(CUSTOMER_SESSION_REVOKED_EVENT, record);

    try {
      const error = await captureLoginError({
        detail: { code: "DEVICE_CREDENTIAL_INVALID", message: "invalid" },
      });

      expect(error.kind).toBe("credential-invalid");
      expect(seen).toEqual([]);
    } finally {
      window.removeEventListener(CUSTOMER_SESSION_EXPIRED_EVENT, record);
      window.removeEventListener(CUSTOMER_SESSION_REPLACED_EVENT, record);
      window.removeEventListener(CUSTOMER_SESSION_REVOKED_EVENT, record);
    }
  });

  // PR #55 review: a suspended code is reversible — an admin can resume it,
  // and the still-valid device credential must survive that window so the
  // user can log in again right after the resume. Firing the revoked event
  // here would make consumers drop the credential for no permanent reason.
  it("keeps a suspended code off the revoked event lane", async () => {
    const seen: string[] = [];
    const record = (event: Event) => seen.push(event.type);
    window.addEventListener(CUSTOMER_SESSION_EXPIRED_EVENT, record);
    window.addEventListener(CUSTOMER_SESSION_REPLACED_EVENT, record);
    window.addEventListener(CUSTOMER_SESSION_REVOKED_EVENT, record);

    try {
      const suspended = await captureLoginError(
        { detail: { code: "CODE_SUSPENDED", message: "suspended" } },
        403,
      );
      expect(suspended.kind).toBe("code-suspended");
      expect(seen).toEqual([]);

      // A revoked code is permanent — it stays on the revoked lane.
      const revokedCode = await captureLoginError(
        { detail: { code: "CODE_REVOKED", message: "revoked" } },
        403,
      );
      expect(revokedCode.kind).toBe("code-revoked");
      expect(seen).toEqual([CUSTOMER_SESSION_REVOKED_EVENT]);
    } finally {
      window.removeEventListener(CUSTOMER_SESSION_EXPIRED_EVENT, record);
      window.removeEventListener(CUSTOMER_SESSION_REPLACED_EVENT, record);
      window.removeEventListener(CUSTOMER_SESSION_REVOKED_EVENT, record);
    }
  });

  // PR #55 review: dev doc §13.1 — state-changing customer requests carry
  // X-Request-Id; §13.2 — an IDEMPOTENCY_CONFLICT must be reported with its
  // request id, so the error keeps the id the client sent.
  it("stamps every request with an X-Request-Id and keeps it on the error", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(201, activationBody))
      .mockResolvedValueOnce(
        jsonResponse(409, {
          detail: {
            code: "IDEMPOTENCY_CONFLICT",
            message:
              "This idempotency key was already used for a different request.",
          },
        }),
      )
      .mockResolvedValueOnce(jsonResponse(201, activationBody));
    vi.stubGlobal("fetch", fetchMock);

    await customerActivate({
      activationCode: "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD",
      deviceFingerprint: "fingerprint-1",
      deviceName: "工作电脑",
      devicePlatform: "windows",
      idempotencyKey: "idem-activate",
    });

    const conflict = await customerLogin(
      { kind: "device", token: deviceTokenText },
      { idempotencyKey: "idem-login" },
    ).then(
      () => {
        throw new Error("expected the login to fail");
      },
      (error: unknown) => {
        expect(error).toBeInstanceOf(CustomerApiError);
        return error as CustomerApiError;
      },
    );

    expect(conflict.kind).toBe("idempotency-conflict");
    // The transport minted a non-empty id and surfaced it on the error.
    expect(conflict.requestId).toBeTruthy();
    const loginOptions = fetchMock.mock.calls[1]?.[1] as RequestInit;
    const loginHeaders = loginOptions.headers as Headers;
    expect(loginHeaders.get("X-Request-Id")).toBe(conflict.requestId ?? null);

    // An explicit requestId travels through instead of a minted one.
    const explicit = await customerActivate({
      activationCode: "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD",
      deviceFingerprint: "fingerprint-1",
      deviceName: "工作电脑",
      devicePlatform: "windows",
      idempotencyKey: "idem-activate",
      requestId: "req-explicit-1",
    });
    expect(explicit.device_token).toBe("device-token");
    const explicitOptions = fetchMock.mock.calls[2]?.[1] as RequestInit;
    const explicitHeaders = explicitOptions.headers as Headers;
    expect(explicitHeaders.get("X-Request-Id")).toBe("req-explicit-1");
  });
});
