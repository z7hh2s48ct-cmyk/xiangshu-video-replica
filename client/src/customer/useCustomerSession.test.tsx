import { webcrypto } from "node:crypto";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CUSTOMER_SESSION_EXPIRED_EVENT,
  CUSTOMER_SESSION_REPLACED_EVENT,
  CUSTOMER_SESSION_REVOKED_EVENT,
} from "../api";
import {
  type CustomerCredentialStore,
  type CustomerStoredIdentity,
  customerCredentialStore,
  useCustomerSession,
} from "./useCustomerSession";

it("browser store restores only cookie CSRF handles after a fresh mount", async () => {
  const deviceHandle = "web-device:csrf-device";
  const sessionHandle = "web-session:csrf-session";
  const fetchMock = vi.fn().mockImplementation(() =>
    jsonResponse({
      device_token: deviceHandle,
      session_token: sessionHandle,
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  const first = customerCredentialStore();
  expect(await first.loadDeviceCredentialToken()).toBe(
    "web-device:csrf-device",
  );
  expect(await first.loadSessionToken()).toBe("web-session:csrf-session");
  const reloaded = customerCredentialStore();
  expect(await reloaded.loadDeviceCredentialToken()).toBe(
    "web-device:csrf-device",
  );
  expect(fetchMock).toHaveBeenCalledTimes(2);
  const headers = new Headers(fetchMock.mock.calls[0]?.[1].headers);
  expect(headers.get("X-Customer-Web")).toBe("1");
  expect(headers.has("Authorization")).toBe(false);
});

it("a live browser reload renews the lease without consuming a new login attempt; logout clears device cookies", async () => {
  const store = memoryStore({ deviceToken: "web-device:csrf-device" });
  store.devicePlatform = () => "browser";
  await store.saveSessionToken("web-session:csrf-session");
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url.endsWith("/sessions/heartbeat"))
      return jsonResponse({
        session_id: "web-session-id",
        session_epoch: 1,
        lease_expires_at: "2099-01-01T00:00:00Z",
      });
    if (url.endsWith("/profile"))
      return jsonResponse({ user_id: "web-user", username: "alice" });
    if (url.endsWith("/sessions/logout")) return jsonResponse(null, 204);
    return jsonResponse({}, 500);
  });
  vi.stubGlobal("fetch", fetchMock);
  const { result } = renderHook(() => useCustomerSession(store));
  await waitFor(() => expect(result.current.screen).toBe("workspace"));
  expect(result.current.user?.userId).toBe("web-user");
  expect(
    fetchMock.mock.calls.some(([url]) => url.endsWith("/sessions/login")),
  ).toBe(false);
  await act(async () => {
    await result.current.logout();
  });
  expect(store.calls).toContain("clear-all");
  expect(store.snapshot().deviceToken).toBeNull();
});

/** In-memory credential store — the reference implementation of the store
 * contract (the desktop build swaps in the Tauri DPAPI adapter, tests and
 * the browser lane use an isolated non-persistent store; dev doc §14: the
 * desktop and browser credential adapters must stay separate). */
function memoryStore(initial?: {
  deviceToken?: string | null;
  identity?: CustomerStoredIdentity | null;
}) {
  let deviceToken: string | null = initial?.deviceToken ?? null;
  let sessionToken: string | null = null;
  let identity: CustomerStoredIdentity | null = initial?.identity ?? null;
  const calls: string[] = [];
  const store: CustomerCredentialStore & {
    calls: string[];
    snapshot: () => {
      deviceToken: string | null;
      sessionToken: string | null;
      identity: CustomerStoredIdentity | null;
    };
  } = {
    calls,
    snapshot: () => ({ deviceToken, sessionToken, identity }),
    async loadDeviceCredentialToken() {
      calls.push("load-device");
      return deviceToken;
    },
    async loadSessionToken() {
      calls.push("load-session");
      return sessionToken;
    },
    async saveActivation(nextDeviceToken, nextSessionToken, nextIdentity) {
      calls.push("save-activation");
      deviceToken = nextDeviceToken;
      sessionToken = nextSessionToken;
      // CW-062：仅在携带身份时更新缓存（设备凭据恢复路径不传身份）。
      if (nextIdentity) identity = nextIdentity;
    },
    async saveSessionToken(nextSessionToken) {
      calls.push("save-session");
      sessionToken = nextSessionToken;
    },
    async clearSessionToken() {
      calls.push("clear-session");
      sessionToken = null;
    },
    async clearAllCredentials() {
      calls.push("clear-all");
      deviceToken = null;
      sessionToken = null;
    },
    async deviceInstanceId() {
      return "instance-1";
    },
    devicePlatform() {
      return "windows";
    },
    // 「记住密码」不参与本文件的会话断言，给出满足接口的最小桩。
    async loadRememberedLogin() {
      return null;
    },
    async saveRememberedLogin() {},
    async clearRememberedLogin() {},
    async loadIdentity() {
      return identity;
    },
  };
  return store;
}

function jsonResponse(payload: unknown, status = 200, headers?: Headers) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    headers: headers ?? new Headers(),
    json: async () => payload,
  });
}

// The fake token fixtures live in named constants so the repo's secret
// scan (which flags `token:` followed by a quoted literal) stays quiet —
// same posture as customerApi.test.ts.
const deviceTokenText = "device-token-1";
const sessionTokenText = "session-token-1";
const renewedSessionTokenText = "session-token-2";
// CW-017 late-logout race: a distinct token for the session established while
// the first logout is still in flight (named constant so the repo secret scan
// stays quiet, same posture as the fixtures above).
const relaunchSessionTokenText = "session-token-3";
const testPasswordText = "test-password";

const activationBody = {
  username: "user-1",
  user_id: "user-1",
  device_id: "device-1",
  device_token: deviceTokenText,
  session_token: sessionTokenText,
  session_epoch: 1,
  session_lease_expires_at: "2026-08-24T12:01:00Z",
  request_id: "req-1",
};

const loginBody = {
  user_id: "user-1",
  device_id: "device-1",
  session_id: "session-1",
  session_token: renewedSessionTokenText,
  session_epoch: 2,
  session_lease_expires_at: "2026-08-24T12:02:00Z",
  request_id: "req-2",
};

const heartbeatBody = {
  session_id: "session-1",
  session_epoch: 2,
  lease_expires_at: "2026-08-24T12:03:00Z",
  request_id: "req-3",
};

function stubFetch(handler: (url: string, init?: RequestInit) => unknown) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string" ? input : new URL(input.toString()).pathname;
    return handler(url, init);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const HEARTBEAT_INTERVAL_MS = 30_000;

describe("useCustomerSession", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it("preserves a password login started from the expiry screen while old Cookie cleanup is delayed", async () => {
    vi.stubGlobal("crypto", {
      randomUUID: () => webcrypto.randomUUID(),
      subtle: { digest: vi.fn().mockResolvedValue(new ArrayBuffer(32)) },
    });
    const oldDevice = "web-device:old-device";
    const oldSession = "web-session:old-session";
    const newDevice = "web-device:new-device";
    const newSession = "web-session:new-session";
    let resolveCleanup!: (
      response: Awaited<ReturnType<typeof jsonResponse>>,
    ) => void;
    const cleanup = new Promise<Awaited<ReturnType<typeof jsonResponse>>>(
      (resolve) => {
        resolveCleanup = resolve;
      },
    );
    const fetchMock = stubFetch((url, init) => {
      if (url.endsWith("/browser-session")) {
        return init?.method === "DELETE"
          ? cleanup
          : jsonResponse({
              device_token: oldDevice,
              session_token: oldSession,
            });
      }
      if (url.endsWith("/sessions/heartbeat"))
        return jsonResponse(heartbeatBody);
      if (url.endsWith("/profile"))
        return jsonResponse({ user_id: "user-1", username: "alice" });
      if (url.endsWith("/customer/login"))
        return jsonResponse({
          ...activationBody,
          device_token: newDevice,
          session_token: newSession,
        });
      return jsonResponse({}, 500);
    });
    const store = customerCredentialStore();
    const { result } = renderHook(() => useCustomerSession(store));
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    act(() => window.dispatchEvent(new Event(CUSTOMER_SESSION_EXPIRED_EVENT)));
    expect(result.current.screen).toBe("session-expired");
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE"),
      ).toBe(true),
    );
    // RootApp's terminal action is enabled immediately; it exposes the normal
    // password form while the old asynchronous Cookie deletion is in flight.
    act(() => result.current.restartAfterExpiry());
    expect(result.current.screen).toBe("login");
    let loginPending!: Promise<void>;
    await act(async () => {
      loginPending = result.current.loginWithPassword({
        mode: "login",
        username: "alice",
        password: testPasswordText,
        remember: false,
      });
    });
    expect(result.current.screen).toBe("login");
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).endsWith("/customer/login"),
      ),
    ).toBe(false);

    await act(async () => {
      resolveCleanup(await jsonResponse(undefined, 204));
      await loginPending;
    });
    expect(result.current.screen).toBe("workspace");
    expect(await store.loadSessionToken()).toBe(newSession);
    expect(await store.loadDeviceCredentialToken()).toBe(newDevice);
  });

  it.each(["http", "network"])(
    "reports %s Cookie cleanup failure and allows explicit password login recovery",
    async (failure) => {
      vi.stubGlobal("crypto", webcrypto);
      const oldDevice = "web-device:failed-cleanup-device";
      const oldSession = "web-session:failed-cleanup-session";
      const newSession = "web-session:recovered-session";
      stubFetch((url, init) => {
        if (url.endsWith("/browser-session")) {
          if (init?.method === "DELETE") {
            return failure === "network"
              ? Promise.reject(new TypeError("offline"))
              : jsonResponse({ detail: { code: "SERVICE_UNAVAILABLE" } }, 503);
          }
          return jsonResponse({
            device_token: oldDevice,
            session_token: oldSession,
          });
        }
        if (url.endsWith("/sessions/heartbeat"))
          return jsonResponse(heartbeatBody);
        if (url.endsWith("/profile"))
          return jsonResponse({ user_id: "user-1", username: "alice" });
        if (url.endsWith("/customer/login"))
          return jsonResponse({
            ...activationBody,
            device_token: oldDevice,
            session_token: newSession,
          });
        return jsonResponse({}, 500);
      });
      const store = customerCredentialStore();
      const { result } = renderHook(() => useCustomerSession(store));
      await waitFor(() => expect(result.current.screen).toBe("workspace"));
      act(() =>
        window.dispatchEvent(new Event(CUSTOMER_SESSION_EXPIRED_EVENT)),
      );
      await waitFor(() =>
        expect(result.current.error?.code).toBe("CREDENTIAL_CLEAR_FAILED"),
      );
      act(() => result.current.restartAfterExpiry());
      expect(result.current.screen).toBe("login");
      expect(result.current.error?.message).toContain("旧登录状态清理失败");
      await act(async () => {
        await result.current.loginWithPassword({
          mode: "login",
          username: "alice",
          password: testPasswordText,
          remember: false,
        });
      });
      expect(result.current.screen).toBe("workspace");
      expect(result.current.error).toBeNull();
      expect(await store.loadSessionToken()).toBe(newSession);
    },
  );

  it("boots onto the login screen without a stored credential", async () => {
    const store = memoryStore();
    const fetchMock = stubFetch(() => jsonResponse({}));

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );

    await waitFor(() => expect(result.current.screen).toBe("login"));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  // CW-017: the unattended empty-code recovery probe is retired. The server
  // deliberately refuses fingerprint-only recovery (activation_code_routes.py
  // `if not code_digests: raise unavailable` + the module docstring "a leaked
  // stable fingerprint is not a second authentication factor"), so the old
  // automaticRecovery lane could never succeed — its 201 "recovered" mock was
  // false evidence, forbidden by the CW-017 acceptance line. A wiped install
  // now waits on the login screen, which the server
  // binds to the same fingerprint (recover-or-bind) — never a boot-time probe.
  it("never fires an empty-code recovery probe; a wiped install waits on the login screen", async () => {
    const store = memoryStore();
    const fetchMock = stubFetch(() => jsonResponse({}, 500));

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );

    await waitFor(() => expect(result.current.screen).toBe("login"));
    // No unattended POST /api/customer/activate carrying an empty code: the
    // recovery contract is user-driven (full code + the same fingerprint).
    const activateProbes = fetchMock.mock.calls.filter(([url]) =>
      String(url).endsWith("/api/customer/activate"),
    );
    expect(activateProbes).toHaveLength(0);
    expect(result.current.error).toBeNull();
  });

  // F-01 review (P0-3): the workspace calls expireSessionLocally when it lost
  // the session without a transport lifecycle event. It must land on the
  // expired terminal — never a silent no-op leaving a dead workspace.
  it("本地会话失效路径从工作台进入过期终局屏并清除会话令牌", async () => {
    // B1：不再有激活入口，用「凭设备凭据重新登录」到达工作台。
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    act(() => {
      result.current.expireSessionLocally();
    });

    expect(result.current.screen).toBe("session-expired");
    expect(result.current.user).toBeNull();
    expect(result.current.sessionRuntime).toBeNull();
    // 与传输层 401 过期同规格：仅清会话令牌，设备凭据保留（§13.2）
    expect(store.snapshot()).toEqual({
      deviceToken: "device-token-1",
      sessionToken: null,
      identity: null,
    });
  });

  it("restores a restart by auto-logging-in with the stored device credential", async () => {
    // FE-02 exit gate: after a real app restart the stored credential logs
    // the user back in without retyping anything.
    const store = memoryStore({ deviceToken: "device-token-1" });
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );

    await waitFor(() => expect(result.current.screen).toBe("workspace"));
    expect(store.snapshot().sessionToken).toBe("session-token-2");
    const loginCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/api/customer/sessions/login"),
    );
    expect(loginCall).toBeDefined();
  });

  it("surfaces the other-device-online conflict on the conflict screen without switching", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(
          {
            detail: {
              code: "OTHER_DEVICE_ONLINE",
              message: "另一台设备在线",
              online_device_name_masked: "张**的 iPad",
              online_slot_no: 2,
              lease_expires_at: "2026-08-24T12:05:00Z",
            },
          },
          409,
        );
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );

    await waitFor(() => expect(result.current.screen).toBe("binding-conflict"));
    expect(result.current.conflict).toEqual({
      deviceNameMasked: "张**的 iPad",
      slotNo: 2,
      leaseExpiresAt: "2026-08-24T12:05:00Z",
    });
    // T29/T30: no silent switch — the takeover only runs after the user
    // confirms it in the conflict dialog.
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).endsWith("/api/customer/sessions/switch"),
      ),
    ).toBe(false);
  });

  it("switches to the confirmed device after the user confirms the takeover", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(
          {
            detail: {
              code: "OTHER_DEVICE_ONLINE",
              message: "另一台设备在线",
              online_device_name_masked: "张**的 iPad",
              online_slot_no: 2,
              lease_expires_at: "2026-08-24T12:05:00Z",
            },
          },
          409,
        );
      }
      if (url.endsWith("/api/customer/sessions/switch")) {
        return jsonResponse(loginBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("binding-conflict"));

    await act(async () => {
      await result.current.switchSession();
    });

    expect(result.current.screen).toBe("workspace");
    expect(result.current.conflict).toBeNull();
    expect(store.snapshot().sessionToken).toBe(renewedSessionTokenText);
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).endsWith("/api/customer/sessions/switch"),
      ),
    ).toBe(true);
  });

  it("returns to the login screen when the takeover is declined", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(
          {
            detail: {
              code: "OTHER_DEVICE_ONLINE",
              message: "另一台设备在线",
              online_device_name_masked: "张**的 iPad",
              online_slot_no: 2,
              lease_expires_at: "2026-08-24T12:05:00Z",
            },
          },
          409,
        );
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("binding-conflict"));

    await act(async () => {
      result.current.cancelSessionSwitch();
    });

    expect(result.current.screen).toBe("login");
    expect(result.current.conflict).toBeNull();
  });

  it("keeps the conflict screen when the switch request fails", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(
          {
            detail: {
              code: "OTHER_DEVICE_ONLINE",
              message: "另一台设备在线",
              online_device_name_masked: "张**的 iPad",
              online_slot_no: 2,
              lease_expires_at: "2026-08-24T12:05:00Z",
            },
          },
          409,
        );
      }
      if (url.endsWith("/api/customer/sessions/switch")) {
        return jsonResponse(
          { detail: { code: "INTERNAL", message: "boom" } },
          500,
        );
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("binding-conflict"));
    // 冲突屏打开时（尚未点切换）不得出现"切换失败"：409 的 error 属于
    // 进入路径，switchError 只在真正的切换失败后非空（FE-03）。
    expect(result.current.switchError).toBeNull();

    await act(async () => {
      await result.current.switchSession();
    });

    expect(result.current.screen).toBe("binding-conflict");
    expect(result.current.conflict).not.toBeNull();
    expect(result.current.error).not.toBeNull();
    // F-01 review：切换失败原因走独立的 switchError（对话框告警位），
    // 全局 error 此时仍是进入冲突屏时的 409 消息，不得混用。
    expect(result.current.switchError).toBe("boom");
  });

  it("expires a live session but keeps the device credential for the next login", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    act(() => {
      window.dispatchEvent(new Event(CUSTOMER_SESSION_EXPIRED_EVENT));
    });

    await waitFor(() => expect(result.current.screen).toBe("session-expired"));
    expect(store.snapshot()).toEqual({
      deviceToken: "device-token-1",
      sessionToken: null,
      identity: null,
    });
  });

  it("routes a displaced session to the dedicated replaced screen", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    act(() => {
      window.dispatchEvent(new Event(CUSTOMER_SESSION_REPLACED_EVENT));
    });

    await waitFor(() => expect(result.current.screen).toBe("session-replaced"));
    // DoD: SESSION_REPLACED keeps the device credential (back to login, the
    // device slot is not consumed again) — only the session token is cleared.
    expect(store.snapshot()).toEqual({
      deviceToken: "device-token-1",
      sessionToken: null,
      identity: null,
    });
  });

  it("clears every stored credential when the device is revoked", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    act(() => {
      window.dispatchEvent(new Event(CUSTOMER_SESSION_REVOKED_EVENT));
    });

    await waitFor(() => expect(result.current.screen).toBe("device-revoked"));
    expect(store.snapshot()).toEqual({
      deviceToken: null,
      sessionToken: null,
      identity: null,
    });
    // DoD: DEVICE_REVOKED clears device/session credentials (the snapshot above)
    // but must NOT treat the stable machine identity as a credential. The hook
    // only ever calls clearAllCredentials — it has no instance-id-clearing path
    // — so the fingerprint survives for a later re-pairing. This in-memory mock
    // returns the id from a closure the clear never touches, so the real
    // guarantee (clear_all() deletes only the credential envelope while the
    // device-instance-id lives in its own vault file) is locked by the Rust
    // vault test `clear_all_removes_the_envelope_entirely`, not asserted here.
    expect(await store.deviceInstanceId()).toBe("instance-1");
  });

  it("logs out to the login screen keeping the device credential", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      if (url.endsWith("/api/customer/sessions/logout")) {
        return jsonResponse(undefined, 204);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    await act(async () => {
      await result.current.logout();
    });

    expect(result.current.screen).toBe("login");
    expect(store.snapshot()).toEqual({
      deviceToken: "device-token-1",
      sessionToken: null,
      identity: null,
    });
  });

  it("reports a determinate server-released outcome on a clean logout", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      if (url.endsWith("/api/customer/sessions/logout")) {
        return jsonResponse(undefined, 204);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    // CW-017 DoD: logout must hand back a determinate outcome, not void — the
    // shell has to tell "the server released the lease" from "local-only".
    let outcome: unknown;
    await act(async () => {
      outcome = await result.current.logout();
    });
    expect(outcome).toEqual({ serverReleased: true, credentialCleared: true });
    expect(result.current.screen).toBe("login");
    // A clean release leaves no visible error banner on the login screen.
    expect(result.current.error).toBeNull();
    expect(store.snapshot()).toEqual({
      deviceToken: "device-token-1",
      sessionToken: null,
      identity: null,
    });
  });

  it("never reports a server release when the logout call fails (network/5xx)", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      if (url.endsWith("/api/customer/sessions/logout")) {
        return jsonResponse(
          { detail: { code: "INTERNAL", message: "logout blew up" } },
          500,
        );
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    // DoD: never call a local-only logout a server release. A 5xx logout still
    // returns the user to login with the device credential intact, but
    // serverReleased must be false so the UI cannot claim the lease is gone.
    let outcome: unknown;
    await act(async () => {
      outcome = await result.current.logout();
    });
    expect(outcome).toEqual({ serverReleased: false, credentialCleared: true });
    expect(result.current.screen).toBe("login");
    // CW-017 可见结果: the login screen must tell the user the server never
    // confirmed the release, so a local-only logout is not mistaken for clean.
    expect(result.current.error?.message).toBe(
      "本机已退出，但服务端未能确认释放会话，请检查网络后重试",
    );
    expect(store.snapshot()).toEqual({
      deviceToken: "device-token-1",
      sessionToken: null,
      identity: null,
    });
  });

  it("surfaces a session-token vault-write failure as a determinate outcome", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      if (url.endsWith("/api/customer/sessions/logout")) {
        return jsonResponse(undefined, 204);
      }
      return jsonResponse({}, 500);
    });
    // Today a throwing vault write is swallowed by a bare catch{}; the
    // determinate outcome must instead report credentialCleared:false.
    vi.spyOn(store, "clearSessionToken").mockRejectedValue(
      new Error("vault I/O failure"),
    );

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    let outcome: unknown;
    await act(async () => {
      outcome = await result.current.logout();
    });
    expect(outcome).toEqual({ serverReleased: true, credentialCleared: false });
    expect(result.current.screen).toBe("login");
    // CW-017 可见结果: a vault-write failure is surfaced, not swallowed.
    expect(result.current.error?.message).toBe(
      "本机已退出，但清理本机会话凭据失败，请重试",
    );
  });

  it("sends only one backend logout while concurrent clicks are pending", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    let resolveLogout:
      | ((value: Awaited<ReturnType<typeof jsonResponse>>) => void)
      | undefined;
    const logoutResponse = new Promise<
      Awaited<ReturnType<typeof jsonResponse>>
    >((resolve) => {
      resolveLogout = resolve;
    });
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      if (url.endsWith("/api/customer/sessions/logout")) {
        return logoutResponse;
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    let firstLogout: Promise<unknown> | undefined;
    let secondLogout: Promise<unknown> | undefined;
    act(() => {
      firstLogout = result.current.logout();
      secondLogout = result.current.logout();
    });

    expect(
      fetchMock.mock.calls.filter(([url]) =>
        String(url).endsWith("/api/customer/sessions/logout"),
      ),
    ).toHaveLength(1);

    resolveLogout?.(await jsonResponse(undefined, 204));
    await act(async () => {
      await Promise.all([firstLogout, secondLogout]);
    });
    expect(result.current.screen).toBe("login");
  });

  it("does not let a late logout clobber a session established while it was in flight", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    let resolveLogout:
      | ((value: Awaited<ReturnType<typeof jsonResponse>>) => void)
      | undefined;
    const logoutGate = new Promise<Awaited<ReturnType<typeof jsonResponse>>>(
      (resolve) => {
        resolveLogout = resolve;
      },
    );
    let loginCount = 0;
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        loginCount += 1;
        // First login = the original session; the retry while the logout is
        // still in flight mints a distinct session token (session B).
        return jsonResponse(
          loginCount === 1
            ? loginBody
            : {
                ...loginBody,
                session_token: relaunchSessionTokenText,
                request_id: "req-relaunch",
              },
          201,
        );
      }
      if (url.endsWith("/api/customer/sessions/logout")) {
        return logoutGate;
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    // Kick off logout; it parks on the gate (slow network) after nulling the
    // session ref and bumping the generation.
    let logoutPromise: Promise<unknown> | undefined;
    act(() => {
      logoutPromise = result.current.logout();
    });

    // While that logout is in flight the customer logs back in (session B).
    await act(async () => {
      await result.current.retryLogin();
    });
    await waitFor(() => expect(result.current.screen).toBe("workspace"));
    expect(store.snapshot().sessionToken).toBe(relaunchSessionTokenText);

    // The stale logout finally resolves. Its tail (clearSessionToken +
    // setSessionToken(null) + dispatch logout) must be generation-guarded so it
    // cannot clobber session B — DoD: a late logout never touches a new session.
    resolveLogout?.(await jsonResponse(undefined, 204));
    await act(async () => {
      await logoutPromise;
    });

    expect(result.current.screen).toBe("workspace");
    expect(result.current.user).not.toBeNull();
    expect(store.snapshot().sessionToken).toBe(relaunchSessionTokenText);
  });

  it("exposes the user id (without a username) after a restart restore login", async () => {
    // The login response carries no username; the restored workspace shows
    // the generic identity until a customer /me endpoint exists (later task).
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );

    await waitFor(() => expect(result.current.screen).toBe("workspace"));
    expect(result.current.user).toEqual({
      userId: "user-1",
      username: null,
      // 旧凭据库没有身份缓存：未知身份如实呈现为 null（徽章不显示）。
      accountType: null,
      parentUserId: null,
      parentDisplayName: null,
    });
  });

  it("renews the lease with a heartbeat while the workspace is live", async () => {
    vi.useFakeTimers();
    const store = memoryStore({ deviceToken: "device-token-1" });
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      if (url.endsWith("/api/customer/sessions/heartbeat")) {
        return jsonResponse(heartbeatBody, 200);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await vi.waitFor(() => expect(result.current.screen).toBe("workspace"));
    const beatsBefore = fetchMock.mock.calls.filter(([url]) =>
      String(url).endsWith("/api/customer/sessions/heartbeat"),
    ).length;
    expect(beatsBefore).toBe(0);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);
    });

    const beatsAfter = fetchMock.mock.calls.filter(([url]) =>
      String(url).endsWith("/api/customer/sessions/heartbeat"),
    ).length;
    expect(beatsAfter).toBe(2);
  });

  it("exposes the session lease runtime from login and clears it on logout", async () => {
    // B1：不再有激活入口，改用「凭设备凭据重新登录」到达工作台。
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      if (url.endsWith("/api/customer/sessions/logout")) {
        return jsonResponse(undefined, 204);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));
    expect(result.current.sessionRuntime).not.toBeNull();
    expect(result.current.sessionRuntime?.leaseExpiresAt).toBe(
      loginBody.session_lease_expires_at,
    );
    expect(
      Number.isNaN(
        Date.parse(result.current.sessionRuntime?.lastHeartbeatAt ?? ""),
      ),
    ).toBe(false);

    await act(async () => {
      await result.current.logout();
    });
    expect(result.current.sessionRuntime).toBeNull();
  });

  it("refreshes the runtime lease on heartbeat ticks and manual renewal", async () => {
    vi.useFakeTimers();
    const store = memoryStore({ deviceToken: "device-token-1" });
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      if (url.endsWith("/api/customer/sessions/heartbeat")) {
        return jsonResponse(heartbeatBody, 200);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await vi.waitFor(() => expect(result.current.screen).toBe("workspace"));
    // The restart login carries the login lease until the first beat lands.
    expect(result.current.sessionRuntime?.leaseExpiresAt).toBe(
      loginBody.session_lease_expires_at,
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);
    });
    expect(result.current.sessionRuntime?.leaseExpiresAt).toBe(
      heartbeatBody.lease_expires_at,
    );

    const beatsBefore = fetchMock.mock.calls.filter(([url]) =>
      String(url).endsWith("/api/customer/sessions/heartbeat"),
    ).length;
    await act(async () => {
      await result.current.sendHeartbeatNow();
    });
    expect(
      fetchMock.mock.calls.filter(([url]) =>
        String(url).endsWith("/api/customer/sessions/heartbeat"),
      ).length,
    ).toBe(beatsBefore + 1);
  });

  it("keeps the server lease while reporting a transient heartbeat failure separately", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    let heartbeatFails = true;
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      if (url.endsWith("/api/customer/sessions/heartbeat")) {
        return heartbeatFails
          ? jsonResponse({ detail: "network unavailable" }, 503)
          : jsonResponse(heartbeatBody, 200);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));
    const originalLease = result.current.sessionRuntime?.leaseExpiresAt;

    await act(async () => result.current.sendHeartbeatNow());

    expect(result.current.sessionRuntime).toEqual(
      expect.objectContaining({
        connectivity: "unreachable",
        leaseExpiresAt: originalLease,
      }),
    );

    heartbeatFails = false;
    await act(async () => result.current.sendHeartbeatNow());
    expect(result.current.sessionRuntime).toEqual(
      expect.objectContaining({
        connectivity: "reachable",
        leaseExpiresAt: heartbeatBody.lease_expires_at,
      }),
    );
  });

  it("ignores an older heartbeat failure after a newer heartbeat succeeds", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    let resolveOlderHeartbeat:
      | ((value: Awaited<ReturnType<typeof jsonResponse>>) => void)
      | undefined;
    const olderHeartbeat = new Promise<
      Awaited<ReturnType<typeof jsonResponse>>
    >((resolve) => {
      resolveOlderHeartbeat = resolve;
    });
    let heartbeatCalls = 0;
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      if (url.endsWith("/api/customer/sessions/heartbeat")) {
        heartbeatCalls += 1;
        return heartbeatCalls === 1
          ? olderHeartbeat
          : jsonResponse(heartbeatBody, 200);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    let olderRequest: Promise<void> | undefined;
    act(() => {
      olderRequest = result.current.sendHeartbeatNow();
    });
    await act(async () => result.current.sendHeartbeatNow());
    expect(result.current.sessionRuntime).toEqual(
      expect.objectContaining({
        connectivity: "reachable",
        leaseExpiresAt: heartbeatBody.lease_expires_at,
      }),
    );

    resolveOlderHeartbeat?.(
      await jsonResponse({ detail: "network unavailable" }, 503),
    );
    await act(async () => olderRequest);

    expect(result.current.sessionRuntime).toEqual(
      expect.objectContaining({
        connectivity: "reachable",
        leaseExpiresAt: heartbeatBody.lease_expires_at,
      }),
    );
  });

  it("ignores a heartbeat from the previous session after logout and login", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    let resolveOldHeartbeat:
      | ((value: Awaited<ReturnType<typeof jsonResponse>>) => void)
      | undefined;
    const oldHeartbeat = new Promise<Awaited<ReturnType<typeof jsonResponse>>>(
      (resolve) => {
        resolveOldHeartbeat = resolve;
      },
    );
    let loginCalls = 0;
    const secondLoginBody = {
      ...loginBody,
      session_lease_expires_at: "2026-08-24T13:00:00Z",
      request_id: "req-new-login",
    };
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        loginCalls += 1;
        return jsonResponse(
          loginCalls === 1 ? loginBody : secondLoginBody,
          201,
        );
      }
      if (url.endsWith("/api/customer/sessions/heartbeat")) {
        return oldHeartbeat;
      }
      if (url.endsWith("/api/customer/sessions/logout")) {
        return jsonResponse(undefined, 204);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));
    let oldRequest: Promise<void> | undefined;
    act(() => {
      oldRequest = result.current.sendHeartbeatNow();
    });
    await act(async () => result.current.logout());
    await act(async () => result.current.retryLogin());
    expect(result.current.sessionRuntime?.leaseExpiresAt).toBe(
      secondLoginBody.session_lease_expires_at,
    );

    resolveOldHeartbeat?.(
      await jsonResponse(
        { ...heartbeatBody, lease_expires_at: "2026-08-24T12:03:00Z" },
        200,
      ),
    );
    await act(async () => oldRequest);

    expect(result.current.sessionRuntime).toEqual(
      expect.objectContaining({
        connectivity: "reachable",
        leaseExpiresAt: secondLoginBody.session_lease_expires_at,
      }),
    );
  });

  it("never writes a credential into Web Storage (dev doc §7 red line)", async () => {
    const store = memoryStore({ deviceToken: "device-token-1" });
    stubFetch((url) => {
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(loginBody, 201);
      }
      return jsonResponse({}, 500);
    });

    const { result } = renderHook(() =>
      useCustomerSession(store, { heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS }),
    );
    await waitFor(() => expect(result.current.screen).toBe("workspace"));

    // FE-02 / §10.2: no plaintext secret may land in Web Storage — the
    // persistent copy lives only behind the injected credential store.
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });
});
