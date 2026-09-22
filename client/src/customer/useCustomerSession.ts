import { invoke } from "@tauri-apps/api/core";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import {
  CUSTOMER_SESSION_EXPIRED_EVENT,
  CUSTOMER_SESSION_REPLACED_EVENT,
  CUSTOMER_SESSION_REVOKED_EVENT,
  CustomerApiError,
  type CustomerDeviceCredential,
  clearCustomerBrowserCredentials,
  customerBrowserCredentials,
  customerGetProfile,
  customerHeartbeat,
  customerLogin,
  customerLogout,
  customerPasswordLogin,
  customerRegister,
  customerSwitch,
} from "../api";
import type { AccountAccessInput } from "./AccountAccessPage";
import {
  type CustomerScreen,
  customerScreenReducer,
  initialCustomerScreen,
} from "./customer-state";

/** The persistent-credential boundary for the customer lane (dev doc §14).
 *
 * The desktop build backs this with the Tauri DPAPI command bridge
 * (customer_credentials.rs); the browser adapter retains only CSRF handles
 * backed by HttpOnly cookies. Tests inject isolated stores. The hook never touches
 * localStorage/sessionStorage — §7/§10.2 forbid a plaintext secret in Web
 * Storage, and the injected store is the only place a credential survives.
 */
export interface CustomerCredentialStore {
  loadDeviceCredentialToken(): Promise<string | null>;
  loadSessionToken(): Promise<string | null>;
  /** 第三参可选：只有携带身份时才更新缓存（设备凭据恢复路径不传）。 */
  saveActivation(
    deviceToken: string,
    sessionToken: string,
    identity?: CustomerStoredIdentity,
  ): Promise<void>;
  saveSessionToken(sessionToken: string): Promise<void>;
  clearSessionToken(): Promise<void>;
  clearAllCredentials(): Promise<void>;
  /** The stable device fingerprint (§14: generated once, read forever). */
  deviceInstanceId(): Promise<string>;
  devicePlatform(): string;
  /** 「记住密码」：与会话令牌同住系统凭据库，桌面端之外一律不可用。 */
  loadRememberedLogin(): Promise<RememberedLogin | null>;
  saveRememberedLogin(login: RememberedLogin): Promise<void>;
  clearRememberedLogin(): Promise<void>;
  /** CW-062：随设备凭据缓存的账号身份。密码登录时写入，重启恢复会话时读出；
   * 桌面端持久化到系统凭据库，浏览器端仅内存（刷新后由 profile 端点补齐）。 */
  loadIdentity(): Promise<CustomerStoredIdentity | null>;
}

export type RememberedLogin = { username: string; password: string };

/** CW-062 子账号身份（与设备凭据同住）：母账号为 `MASTER` 且无 parent；
 * 子账号席位携带母账号 id 与显示名，工作台据此渲染身份徽章。 */
export type CustomerStoredIdentity = {
  accountType: "MASTER" | "SUB" | "SUB_ADMIN";
  parentUserId: string | null;
  parentDisplayName: string | null;
};

export type CustomerSessionConflict = {
  deviceNameMasked: string;
  slotNo: number;
  leaseExpiresAt: string;
};

/** Live session health for the workspace UI (A1 wiring of FE-04 / T31):
 * when the last heartbeat succeeded and when the server lease lapses.
 * Client-clock timestamps; `null` while no session is established. */
export type CustomerSessionRuntime = {
  connectivity: "reachable" | "unreachable";
  lastHeartbeatAt: string;
  leaseExpiresAt: string | null;
};

/** The determinate result of a logout (CW-017 DoD: a network or storage
 * failure must have an explicit outcome — never call a local-only logout a
 * server release). ``serverReleased`` is true only when the server confirmed
 * the release (2xx); a network/5xx failure leaves it false so the UI cannot
 * claim a release that did not happen. ``credentialCleared`` is false when the
 * local session-token vault write threw — a stale token stays on disk and the
 * next login overwrites it. */
export type CustomerLogoutOutcome = {
  serverReleased: boolean;
  credentialCleared: boolean;
};

export type CustomerActivationFormInput = {
  activationCode: string;
  deviceName: string;
};

/** The workspace identity for the customer lane. Activation returns the
 * username; a restart-restore login response carries only the user id (no
 * customer /me endpoint exists yet), so the username degrades to null.
 *
 * CW-062: `accountType`/`parentUserId`/`parentDisplayName` carry the
 * sub-account identity; `accountType` is null when a restored session could
 * not recover it (pre-upgrade credential vaults) — the badge stays hidden
 * until the next password login refreshes it. */
export type CustomerWorkspaceUser = {
  userId: string;
  username: string | null;
  accountType: "MASTER" | "SUB" | "SUB_ADMIN" | null;
  parentUserId: string | null;
  parentDisplayName: string | null;
};

const DEFAULT_HEARTBEAT_INTERVAL_MS = 30_000;

function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

/** The server closes the account type to MASTER/SUB/SUB_ADMIN; anything else
 * (an old server, a replayed envelope) degrades to the master default. */
function parseAccountType(
  value: string | null | undefined,
): "MASTER" | "SUB" | "SUB_ADMIN" {
  return value === "SUB" || value === "SUB_ADMIN" ? value : "MASTER";
}

/** The cached identity never blocks a session: a vault read failure degrades
 * to "unknown" and the badge simply stays hidden. */
async function loadStoredIdentity(
  store: CustomerCredentialStore,
): Promise<CustomerStoredIdentity | null> {
  try {
    return await store.loadIdentity();
  } catch {
    return null;
  }
}

/** A vault (credential-store) failure surfaced as a determinate customer
 * error: the UI gets a readable message instead of a swallowed rejection.
 * The credential store is local and opaque to the server; any I/O error is
 * treated as "unknown" transport kind rather than inferred from a status code.
 */
function credentialStoreError(cause: unknown): CustomerApiError {
  // The underlying I/O error is intentionally not surfaced verbatim: the
  // vault message may be OS-specific noise the customer cannot act on, and
  // the error path is already observable through the UI copy.
  void cause;
  return new CustomerApiError({
    message: "本机凭据读写失败，请重试或重新激活",
    transportKind: "unknown",
  });
}

/** CW-017: a logout whose server release or local credential clear failed must
 * leave a VISIBLE, determinate result — never let a local-only logout look like
 * a clean release. ``error`` is hook state, so it survives the workspace→login
 * transition and LoginPage renders it (DoD: 网络或存储失败有明确结果). */
function logoutFailureError(
  failure: "server-release" | "credential-clear",
): CustomerApiError {
  return new CustomerApiError({
    message:
      failure === "server-release"
        ? "本机已退出，但服务端未能确认释放会话，请检查网络后重试"
        : "本机已退出，但清理本机会话凭据失败，请重试",
    transportKind: "unknown",
  });
}

/**
 * The customer session orchestrator (FE-02): boots from the credential
 * store, drives login/logout, listens for the three lifecycle
 * events (§10.1), and keeps the lease alive with heartbeats while the
 * workspace is live. Screen transitions all flow through the
 * customer-state reducer — nothing here jumps screens directly.
 */
export function useCustomerSession(
  store: CustomerCredentialStore,
  options?: { heartbeatIntervalMs?: number },
): {
  screen: CustomerScreen;
  isBusy: boolean;
  error: CustomerApiError | null;
  conflict: CustomerSessionConflict | null;
  user: CustomerWorkspaceUser | null;
  /** Heartbeat/lease health while a session is live; null otherwise. */
  sessionRuntime: CustomerSessionRuntime | null;
  loginWithPassword(input: AccountAccessInput): Promise<void>;
  retryLogin(): Promise<void>;
  /** The explicit takeover (FE-03): the user confirmed in the conflict dialog,
   * the server atomically displaces the other device's lease and mints a
   * fresh session token (§14). Never invoked without a prior
   * ``conflict-detected`` transition — no silent switching. */
  switchSession(): Promise<void>;
  /** Failure message of the last explicit switch (FE-03: visible failure);
   * null until a switch actually fails. */
  switchError: string | null;
  /** The user declined the takeover: back to the login screen, the other
   * device keeps the lease. */
  cancelSessionSwitch(): void;
  /** Manual lease renewal for the HeartbeatStatus / LeaseCountdown refresh
   * buttons. Failures stay silent — terminal outcomes arrive as the
   * lifecycle events, transient ones are retried by the next tick. */
  sendHeartbeatNow(): Promise<void>;
  /** 「登录已过期」终屏的定制说明；null = 显示通用文案（P2-2）。 */
  expiredNotice: string | null;
  logout(): Promise<CustomerLogoutOutcome>;
  restartAfterExpiry(): void;
  /** Local-only expiry: the workspace reports a lost session without a
   * transport lifecycle event (missing local token). Runs the same cleanup
   * as a transport expiry and lands on the expired terminal screen — never
   * a silent no-op from the workspace screen. The optional notice replaces
   * the generic「登录已过期」copy on the terminal screen (preflight P2-2:
   * 改密/退出所有设备成功后的分情况文案要活着落到终屏). */
  expireSessionLocally(notice?: string): void;
  restartAfterRevocation(): void;
} {
  const [screen, dispatch] = useReducer(
    customerScreenReducer,
    initialCustomerScreen,
  );
  // session-expired 终屏的定制说明（P2-2）：默认 null 显示通用文案；
  // 由 expireSessionLocally(notice) 写入，任何新会话建立时清空。
  const [expiredNotice, setExpiredNotice] = useState<string | null>(null);
  const [isBusy, setIsBusy] = useState(false);
  const [error, setError] = useState<CustomerApiError | null>(null);
  const [conflict, setConflict] = useState<CustomerSessionConflict | null>(
    null,
  );
  // The switch-failure message for the conflict dialog. Independent from the
  // global `error` — entering binding-conflict always leaves a 409 error in
  // `error`, and the dialog must not show "switch failed" before a switch.
  const [switchError, setSwitchError] = useState<string | null>(null);
  // The in-memory session token powers heartbeat/logout. The persistent
  // copy lives only in the injected store; this state is intentionally not
  // persisted anywhere else.
  const [sessionToken, setSessionToken] = useState<string | null>(null);
  const [user, setUser] = useState<CustomerWorkspaceUser | null>(null);
  const [sessionRuntime, setSessionRuntime] =
    useState<CustomerSessionRuntime | null>(null);
  const sessionTokenRef = useRef<string | null>(null);
  const sessionGenerationRef = useRef(0);
  const latestHeartbeatRequestIdRef = useRef(0);
  const bootstrappedRef = useRef(false);

  // Every established/renewed lease updates the runtime view: the heartbeat
  // timestamp is the client clock at success, the lease comes from the
  // server response.
  const noteLease = useCallback((leaseExpiresAt: string | null) => {
    setSessionRuntime({
      connectivity: "reachable",
      lastHeartbeatAt: new Date().toISOString(),
      leaseExpiresAt,
    });
  }, []);

  const noteHeartbeatFailure = useCallback(() => {
    setSessionRuntime((current) =>
      current ? { ...current, connectivity: "unreachable" } : current,
    );
  }, []);

  const sendHeartbeat = useCallback(
    async (token: string) => {
      const requestId = ++latestHeartbeatRequestIdRef.current;
      const sessionGeneration = sessionGenerationRef.current;
      const belongsToCurrentSession = () =>
        requestId === latestHeartbeatRequestIdRef.current &&
        sessionGeneration === sessionGenerationRef.current &&
        token === sessionTokenRef.current;
      try {
        const body = await customerHeartbeat(
          { kind: "session", token },
          { shouldDispatchLifecycle: belongsToCurrentSession },
        );
        if (belongsToCurrentSession()) {
          noteLease(body.lease_expires_at);
        }
      } catch {
        if (belongsToCurrentSession()) {
          noteHeartbeatFailure();
        }
      }
    },
    [noteHeartbeatFailure, noteLease],
  );

  useEffect(
    () => () => {
      sessionGenerationRef.current += 1;
      latestHeartbeatRequestIdRef.current += 1;
    },
    [],
  );

  const establishSession = useCallback(
    async (deviceToken: string) => {
      const credential: CustomerDeviceCredential = {
        kind: "device",
        token: deviceToken,
      };
      const previousSessionToken = await store.loadSessionToken();
      if (previousSessionToken?.startsWith("web-session:")) {
        const sessionCredential = {
          kind: "session" as const,
          token: previousSessionToken,
        };
        try {
          const lease = await customerHeartbeat(sessionCredential, {
            shouldDispatchLifecycle: () => false,
          });
          const profile = await customerGetProfile(sessionCredential);
          sessionTokenRef.current = previousSessionToken;
          sessionGenerationRef.current += 1;
          setSessionToken(previousSessionToken);
          setUser({
            userId: profile.user_id,
            username: profile.username,
            accountType: parseAccountType(profile.account_type),
            parentUserId: profile.parent_user_id ?? null,
            parentDisplayName: profile.parent_display_name ?? null,
          });
          noteLease(lease.lease_expires_at);
          return;
        } catch (cause) {
          // Only a genuinely expired lease can use normal device recovery.
          // Revocation/replacement and transport errors must not auto-relogin.
          if (
            !(cause instanceof CustomerApiError) ||
            cause.code !== "SESSION_EXPIRED"
          )
            throw cause;
        }
      }
      const result = await customerLogin(credential, {
        browserSession: store.devicePlatform() === "browser",
        idempotencyKey: newIdempotencyKey(),
        sessionToken: previousSessionToken ?? undefined,
      });
      try {
        if (result.session.device_token?.startsWith("web-device:")) {
          await store.saveActivation(
            result.session.device_token,
            result.session.session_token,
          );
        } else {
          await store.saveSessionToken(result.session.session_token);
        }
      } catch (cause) {
        throw credentialStoreError(cause);
      }
      sessionTokenRef.current = result.session.session_token;
      sessionGenerationRef.current += 1;
      setSessionToken(result.session.session_token);
      // CW-062：设备凭据恢复不带身份（响应里只有 user_id）；从凭据库缓存读回
      // 子账号身份。旧金库没有缓存时身份未知（null），徽章暂不可见，直到
      // 下一次密码登录刷新。
      const storedIdentity = await loadStoredIdentity(store);
      setUser({
        userId: result.session.user_id,
        username: null,
        accountType: storedIdentity?.accountType ?? null,
        parentUserId: storedIdentity?.parentUserId ?? null,
        parentDisplayName: storedIdentity?.parentDisplayName ?? null,
      });
      noteLease(result.session.session_lease_expires_at);
      return result;
    },
    [store, noteLease],
  );

  // Boot: check the credential store once, then (FE-02 restart gate) try to
  // restore a session automatically when a device credential exists. The
  // cleanup resets the guard for React 18 StrictMode double-mounting — the
  // customer lane would otherwise stay stuck on the checking screen forever.
  useEffect(() => {
    if (bootstrappedRef.current) {
      return;
    }
    bootstrappedRef.current = true;
    let cancelled = false;
    (async () => {
      let deviceToken: string | null = null;
      let credentialLoadFailed = false;
      try {
        deviceToken = await store.loadDeviceCredentialToken();
      } catch {
        // A missing/corrupt local envelope is precisely what the durable
        // machine-identity recovery lane repairs. If recovery is unavailable,
        // the activation page still gets a readable local-vault error.
        credentialLoadFailed = true;
      }
      if (cancelled) {
        return;
      }
      // CW-017: the unattended empty-code recovery lane is retired. The server
      // deliberately refuses fingerprint-only recovery (activation_code_routes.py
      // `if not code_digests: raise unavailable`; a leaked stable fingerprint is
      // not a second authentication factor), so the empty-code probe could never
      // succeed. A wiped install now falls through to the activation screen and
      // recovers with the full code, which the server binds to the same
      // fingerprint (recover-or-bind) — never an unattended boot probe.
      dispatch({
        type: "boot-check-completed",
        hasDeviceCredential: deviceToken !== null,
      });
      if (deviceToken === null) {
        if (credentialLoadFailed) {
          setError(credentialStoreError(null));
        }
        return;
      }
      setIsBusy(true);
      try {
        await establishSession(deviceToken);
        if (!cancelled) {
          dispatch({ type: "login-succeeded" });
        }
      } catch (cause) {
        if (cancelled) {
          return;
        }
        if (cause instanceof CustomerApiError) {
          setError(cause);
          if (cause.kind === "other-device-online") {
            setConflict({
              deviceNameMasked: cause.onlineDeviceNameMasked ?? "",
              slotNo: cause.onlineSlotNo ?? 0,
              leaseExpiresAt: cause.leaseExpiresAt ?? "",
            });
            dispatch({ type: "conflict-detected" });
          }
        } else {
          setError(credentialStoreError(cause));
        }
      } finally {
        if (!cancelled) {
          setIsBusy(false);
        }
      }
    })();
    return () => {
      cancelled = true;
      // StrictMode double-mounts the effect; the second mount must boot again
      // or the customer lane would sit on the checking screen forever.
      bootstrappedRef.current = false;
    };
  }, [establishSession, store]);

  // The three lifecycle events (§10.1) arrive on window — dispatched by the
  // customer transport on any 401 that ends the session. Each one also does
  // the right thing to the persisted credential: expired/replaced keep the
  // device credential (§13.2: back to the login screen), revoked clears
  // everything (recovery flow).
  const clearLifecycleCredentials = useCallback(
    (all: boolean) => {
      const generation = sessionGenerationRef.current;
      const clearing = all
        ? store.clearAllCredentials()
        : store.clearSessionToken();
      void clearing.catch(() => {
        if (sessionGenerationRef.current !== generation) return;
        setError(
          new CustomerApiError({
            code: "CREDENTIAL_CLEAR_FAILED",
            message: "旧登录状态清理失败，请检查网络后重新登录。",
            transportKind: "unknown",
          }),
        );
      });
    },
    [store],
  );
  useEffect(() => {
    const clearSession = () => {
      sessionTokenRef.current = null;
      sessionGenerationRef.current += 1;
      latestHeartbeatRequestIdRef.current += 1;
      setSessionToken(null);
      setUser(null);
      setConflict(null);
      setSessionRuntime(null);
      clearLifecycleCredentials(false);
    };
    const onExpired = () => {
      clearSession();
      // 传输层过期没有定制文案；清掉旧 notice 避免上一次「改密成功」的
      // 说明冒充本次过期原因（P2-2）。
      setExpiredNotice(null);
      dispatch({ type: "session-expired" });
    };
    const onReplaced = () => {
      clearSession();
      dispatch({ type: "session-replaced" });
    };
    const onRevoked = () => {
      sessionTokenRef.current = null;
      sessionGenerationRef.current += 1;
      latestHeartbeatRequestIdRef.current += 1;
      setSessionToken(null);
      setUser(null);
      setConflict(null);
      setSessionRuntime(null);
      clearLifecycleCredentials(true);
      dispatch({ type: "device-revoked" });
    };
    window.addEventListener(CUSTOMER_SESSION_EXPIRED_EVENT, onExpired);
    window.addEventListener(CUSTOMER_SESSION_REPLACED_EVENT, onReplaced);
    window.addEventListener(CUSTOMER_SESSION_REVOKED_EVENT, onRevoked);
    return () => {
      window.removeEventListener(CUSTOMER_SESSION_EXPIRED_EVENT, onExpired);
      window.removeEventListener(CUSTOMER_SESSION_REPLACED_EVENT, onReplaced);
      window.removeEventListener(CUSTOMER_SESSION_REVOKED_EVENT, onRevoked);
    };
  }, [clearLifecycleCredentials]);

  // Keep the lease alive while the workspace is live. A failing heartbeat
  // ends through the lifecycle events above (the transport dispatches them
  // on the terminal 401s), so errors here intentionally do not switch
  // screens — §4.2: a displaced/expired session must not masquerade as a
  // generic network/service failure either.
  const heartbeatIntervalMs =
    options?.heartbeatIntervalMs ?? DEFAULT_HEARTBEAT_INTERVAL_MS;
  useEffect(() => {
    if (screen !== "workspace" || sessionToken === null) {
      return;
    }
    const timer = window.setInterval(() => {
      // MATERIAL-PERF-D（P1-5）：页面隐藏时不发心跳（既省请求，也避免后台
      // 标签页维持在线表象）；恢复可见后由下一个 tick 续上。
      if (document.hidden) {
        return;
      }
      const token = sessionTokenRef.current;
      if (token === null) {
        return;
      }
      void sendHeartbeat(token);
    }, heartbeatIntervalMs);
    return () => {
      window.clearInterval(timer);
    };
  }, [heartbeatIntervalMs, screen, sessionToken, sendHeartbeat]);

  const passwordAttemptRef = useRef<{
    fingerprint: string;
    key: string;
  } | null>(null);
  const registeredUsernameRef = useRef<string | null>(null);
  const passwordPendingRef = useRef(false);
  const loginWithPassword = useCallback(
    async (input: AccountAccessInput) => {
      if (passwordPendingRef.current) return;
      passwordPendingRef.current = true;
      setIsBusy(true);
      setError(null);
      try {
        if (
          input.mode === "register" &&
          registeredUsernameRef.current !== input.username
        ) {
          await customerRegister(input.username, input.password);
          registeredUsernameRef.current = input.username;
        }
        const body = {
          username: input.username,
          password: input.password,
          device_fingerprint: await store.deviceInstanceId(),
          device_platform: store.devicePlatform(),
        };
        // Keep only a SHA-256 request fingerprint in the retry slot, never the password.
        const digest = await crypto.subtle.digest(
          "SHA-256",
          new TextEncoder().encode(JSON.stringify(body)),
        );
        const fingerprint = Array.from(new Uint8Array(digest), (b) =>
          b.toString(16).padStart(2, "0"),
        ).join("");
        if (passwordAttemptRef.current?.fingerprint !== fingerprint) {
          passwordAttemptRef.current = {
            fingerprint,
            key: newIdempotencyKey(),
          };
        }
        const response = await customerPasswordLogin(
          body,
          passwordAttemptRef.current.key,
        );
        // CW-062：登录响应携带子账号身份，随设备凭据一并落库，重启恢复会话
        // （设备凭据路径）不再需要第二次往返。
        const identity: CustomerStoredIdentity = {
          accountType: parseAccountType(response.account_type),
          parentUserId: response.parent_user_id ?? null,
          parentDisplayName: response.parent_display_name ?? null,
        };
        await store.saveActivation(
          response.device_token,
          response.session_token,
          identity,
        );
        passwordAttemptRef.current = null;
        registeredUsernameRef.current = null;
        sessionTokenRef.current = response.session_token;
        sessionGenerationRef.current += 1;
        setSessionToken(response.session_token);
        setUser({
          userId: response.user_id,
          username: response.username,
          accountType: identity.accountType,
          parentUserId: identity.parentUserId,
          parentDisplayName: identity.parentDisplayName,
        });
        setError(null);
        setConflict(null);
        noteLease(response.session_lease_expires_at);
        window.history.replaceState(null, "", "/#studio/workbench");
        dispatch({ type: "password-login-succeeded" });
      } catch (cause) {
        if (
          cause instanceof CustomerApiError &&
          cause.status &&
          cause.status < 500
        ) {
          passwordAttemptRef.current = null;
        }
        throw cause;
      } finally {
        passwordPendingRef.current = false;
        setIsBusy(false);
      }
    },
    [store, noteLease],
  );

  // A guard for concurrent retries: while a login attempt is in flight, further
  // clicks are ignored until the in-flight request completes. This avoids
  // duplicate login calls and keeps the UI state consistent.
  const retryInFlightRef = useRef(false);

  const retryLogin = useCallback(async () => {
    if (retryInFlightRef.current) {
      return;
    }
    retryInFlightRef.current = true;
    setError(null);
    setConflict(null);
    setIsBusy(true);
    try {
      let deviceToken: string | null = null;

      try {
        deviceToken = await store.loadDeviceCredentialToken();
      } catch (loadCause) {
        // Surface credential-load failures during manual login by transitioning
        // to credential-missing state with a readable error, matching the boot
        // path behavior. This avoids unhandled rejections with no recovery.
        const storeError = credentialStoreError(loadCause);
        setError(storeError);
        dispatch({ type: "credential-missing" });
        return;
      }

      if (deviceToken === null) {
        // The stored device credential vanished (vault cleared / I/O failure):
        // back to activation for recovery.
        dispatch({ type: "credential-missing" });
        return;
      }
      try {
        await establishSession(deviceToken);
        dispatch({ type: "login-succeeded" });
      } catch (cause) {
        if (cause instanceof CustomerApiError) {
          setError(cause);
          if (cause.kind === "other-device-online") {
            setConflict({
              deviceNameMasked: cause.onlineDeviceNameMasked ?? "",
              slotNo: cause.onlineSlotNo ?? 0,
              leaseExpiresAt: cause.leaseExpiresAt ?? "",
            });
            dispatch({ type: "conflict-detected" });
          }
        } else {
          setError(credentialStoreError(cause));
        }
      }
    } finally {
      retryInFlightRef.current = false;
      setIsBusy(false);
    }
  }, [establishSession, store]);

  // Explicit device switch (FE-03 / T30): the customer confirms the takeover
  // in the conflict dialog; the server atomically replaces the lease and
  // mints a fresh session token. The UI must never assume the switch
  // succeeded before the server confirms it — only this action transitions
  // to the workspace from the conflict screen.
  const switchSession = useCallback(async () => {
    setIsBusy(true);
    setError(null);
    setSwitchError(null);
    try {
      const deviceToken = await store.loadDeviceCredentialToken();
      if (deviceToken === null) {
        dispatch({ type: "credential-missing" });
        return;
      }
      const previousSessionToken = await store.loadSessionToken();
      const result = await customerSwitch(
        { kind: "device", token: deviceToken },
        {
          idempotencyKey: newIdempotencyKey(),
          sessionToken: previousSessionToken ?? undefined,
        },
      );
      try {
        await store.saveSessionToken(result.session.session_token);
      } catch (cause) {
        throw credentialStoreError(cause);
      }
      sessionTokenRef.current = result.session.session_token;
      sessionGenerationRef.current += 1;
      setSessionToken(result.session.session_token);
      // 显式切换设备仍是同一账号：身份从凭据库缓存延续（无缓存则未知）。
      const storedIdentity = await loadStoredIdentity(store);
      setUser({
        userId: result.session.user_id,
        username: null,
        accountType: storedIdentity?.accountType ?? null,
        parentUserId: storedIdentity?.parentUserId ?? null,
        parentDisplayName: storedIdentity?.parentDisplayName ?? null,
      });
      setConflict(null);
      noteLease(result.session.session_lease_expires_at);
      dispatch({ type: "login-succeeded" });
    } catch (cause) {
      setError(
        cause instanceof CustomerApiError ? cause : credentialStoreError(cause),
      );
      // F-01 review（FE-03）：切换失败必须可见。独立于全局 error——进入
      // binding-conflict 的两条路径（boot/retryLogin）本身会留下 409 的
      // error，若对话框复用它，用户没点切换就会先看到"切换失败"。
      setSwitchError(
        cause instanceof CustomerApiError
          ? cause.message
          : "本机凭据读写失败，请稍后重试。",
      );
    } finally {
      setIsBusy(false);
    }
  }, [store, noteLease]);

  const cancelSessionSwitch = useCallback(() => {
    setError(null);
    setSwitchError(null);
    setConflict(null);
    dispatch({ type: "conflict-cancelled" });
  }, []);

  const sendHeartbeatNow = useCallback(async () => {
    const token = sessionTokenRef.current;
    if (token === null) {
      return;
    }
    await sendHeartbeat(token);
  }, [sendHeartbeat]);

  const logoutInFlightRef = useRef(false);
  const logout = useCallback(async (): Promise<CustomerLogoutOutcome> => {
    if (logoutInFlightRef.current) {
      // A concurrent second click does not re-run the release; report that it
      // neither confirmed a server release nor cleared credentials itself.
      return { serverReleased: false, credentialCleared: false };
    }
    logoutInFlightRef.current = true;
    // Capture the generation this logout bumps to. If a new session is
    // established while the backend logout is still in flight (a late logout),
    // the tail below must not clobber it — DoD: a late logout never touches a
    // new session.
    const logoutGeneration = sessionGenerationRef.current + 1;
    try {
      const token = sessionTokenRef.current;
      sessionTokenRef.current = null;
      sessionGenerationRef.current = logoutGeneration;
      latestHeartbeatRequestIdRef.current += 1;
      setError(null);
      setConflict(null);
      // No live session token means there is nothing for the server to release;
      // treat that as released rather than a swallowed failure.
      let serverReleased = token === null;
      if (token !== null) {
        setIsBusy(true);
        try {
          await customerLogout(
            { kind: "session", token },
            { idempotencyKey: newIdempotencyKey() },
          );
          serverReleased = true;
        } catch {
          // The session is discarded locally regardless, but a failing logout
          // (network/timeout/5xx) must NOT be reported as a server release.
          serverReleased = false;
        } finally {
          setIsBusy(false);
        }
      }
      let credentialCleared = false;
      // Generation guard: only tear down local state when no newer session has
      // taken over since this logout began (a late logout must not clobber it).
      if (sessionGenerationRef.current === logoutGeneration) {
        try {
          if (store.devicePlatform() === "browser") {
            await store.clearAllCredentials();
          } else {
            await store.clearSessionToken();
          }
          credentialCleared = true;
        } catch {
          // A failing vault write still returns the user to the login screen;
          // the stale token stays on disk and the next login overwrites it.
          credentialCleared = false;
        }
        setSessionToken(null);
        setUser(null);
        setSessionRuntime(null);
        dispatch({ type: "logout" });
        // CW-017 DoD: a network or storage failure must have a VISIBLE result.
        // The user lands on the login screen; surface why this was not a clean
        // release so a local-only logout is never mistaken for a server one.
        // ``error`` is hook state, so it survives the workspace→login switch.
        if (!serverReleased) {
          setError(logoutFailureError("server-release"));
        } else if (!credentialCleared) {
          setError(logoutFailureError("credential-clear"));
        }
      }
      return { serverReleased, credentialCleared };
    } finally {
      logoutInFlightRef.current = false;
    }
  }, [store]);

  const restartAfterExpiry = useCallback(() => {
    setError((current) =>
      current?.code === "CREDENTIAL_CLEAR_FAILED" ? current : null,
    );
    setConflict(null);
    dispatch({ type: "restart-login" });
  }, []);

  // Local-only expiry path: the workspace lost its session (missing local
  // token, or a 401 whose lifecycle event never reached this hook) and would
  // otherwise stay on a silently dead workspace screen. Same cleanup as the
  // transport-driven expiry, then the §4.2 expired terminal screen offers the
  // deterministic recovery path.
  const expireSessionLocally = useCallback(
    (notice?: string) => {
      sessionTokenRef.current = null;
      sessionGenerationRef.current += 1;
      latestHeartbeatRequestIdRef.current += 1;
      setSessionToken(null);
      setUser(null);
      setConflict(null);
      setSessionRuntime(null);
      setError(null);
      setExpiredNotice(notice ?? null);
      clearLifecycleCredentials(false);
      dispatch({ type: "session-expired" });
    },
    [clearLifecycleCredentials],
  );

  const restartAfterRevocation = useCallback(() => {
    setError((current) =>
      current?.code === "CREDENTIAL_CLEAR_FAILED" ? current : null,
    );
    setConflict(null);
    dispatch({ type: "restart-login" });
  }, []);

  return {
    screen,
    expiredNotice,
    isBusy,
    error,
    conflict,
    user,
    sessionRuntime,
    loginWithPassword,
    retryLogin,
    switchSession,
    switchError,
    cancelSessionSwitch,
    sendHeartbeatNow,
    logout,
    restartAfterExpiry,
    expireSessionLocally,
    restartAfterRevocation,
  };
}

// ---------------------------------------------------------------------------
// Credential-store adapters (dev doc §14: the desktop and browser lanes stay
// separate). The desktop lane talks to the Tauri DPAPI vault
// (customer_credentials.rs); browser credentials remain in HttpOnly cookies,
// while this adapter caches only CSRF handles. No secret enters Web Storage.
// ---------------------------------------------------------------------------

export function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

type StoredCustomerIdentity = {
  account_type: string;
  parent_user_id: string | null;
  parent_display_name: string | null;
};

type StoredCustomerCredentials = {
  device_token: string;
  session_token: string | null;
  remembered_login?: { username: string; password: string } | null;
  /** CW-062：老金库记录里没有这个字段，升级后必须还能读出来，故可选。 */
  identity?: StoredCustomerIdentity | null;
};

function tauriCustomerCredentialStore(): CustomerCredentialStore {
  return {
    async loadDeviceCredentialToken() {
      const stored = await invoke<StoredCustomerCredentials | null>(
        "customer_load_credentials",
      );
      // 空串按「没有」处理，与下面的 session_token 一致：金库记录可能只为
      // 存「记住密码」而建（设备令牌尚未写入），此时不能让引导流程把空串
      // 当成一个有效的设备凭据。
      return stored?.device_token || null;
    },
    async loadSessionToken() {
      const stored = await invoke<StoredCustomerCredentials | null>(
        "customer_load_credentials",
      );
      const sessionToken = stored?.session_token?.trim();
      return sessionToken || null;
    },
    async saveActivation(deviceToken, sessionToken, identity) {
      await invoke("customer_save_credentials", {
        deviceToken,
        sessionToken,
        // CW-062：无身份信息（设备凭据恢复）时传 null，Rust 侧保留已存身份。
        identity: identity
          ? {
              account_type: identity.accountType,
              parent_user_id: identity.parentUserId,
              parent_display_name: identity.parentDisplayName,
            }
          : null,
      });
    },
    async loadIdentity() {
      const stored = await invoke<StoredCustomerCredentials | null>(
        "customer_load_credentials",
      );
      const identity = stored?.identity;
      if (!identity) return null;
      return {
        accountType: parseAccountType(identity.account_type),
        parentUserId: identity.parent_user_id ?? null,
        parentDisplayName: identity.parent_display_name ?? null,
      };
    },
    async saveSessionToken(sessionToken) {
      const stored = await invoke<StoredCustomerCredentials | null>(
        "customer_load_credentials",
      );
      if (stored === null) {
        throw new Error("cannot renew a session without a stored device token");
      }
      await invoke("customer_save_credentials", {
        deviceToken: stored.device_token,
        sessionToken,
      });
    },
    async clearSessionToken() {
      await invoke("customer_clear_session_token");
    },
    async clearAllCredentials() {
      await invoke("customer_clear_all_credentials");
    },
    async deviceInstanceId() {
      return invoke<string>("customer_device_instance_id");
    },
    devicePlatform() {
      // The customer desktop build ships Windows-only (DESK-03 NSIS x64).
      return "windows";
    },
    async loadRememberedLogin() {
      const stored = await invoke<StoredCustomerCredentials | null>(
        "customer_load_credentials",
      );
      const remembered = stored?.remembered_login;
      if (!remembered?.username.trim() || !remembered.password) return null;
      return { username: remembered.username, password: remembered.password };
    },
    async saveRememberedLogin(login) {
      await invoke("customer_save_remembered_login", {
        username: login.username,
        password: login.password,
      });
    },
    async clearRememberedLogin() {
      await invoke("customer_clear_remembered_login");
    },
  };
}

function browserCookieCredentialStore(): CustomerCredentialStore {
  // These values are CSRF handles, not bearer tokens. A new page reconstructs
  // them from the same-origin endpoint, which never exposes its HttpOnly cookies.
  let deviceToken: string | null = null;
  let sessionToken: string | null = null;
  // CW-062：浏览器端没有系统凭据库，身份缓存只在内存里活到刷新为止；
  // 刷新后由 profile 端点（account_type/parent_*）补齐。绝不落 Web Storage。
  let identity: CustomerStoredIdentity | null = null;
  const instanceId = newIdempotencyKey();
  let initialized = false;
  let pending: Promise<void> | null = null;
  let revision = 0;
  const load = async () => {
    if (initialized) return;
    if (!pending) {
      const started = revision;
      pending = customerBrowserCredentials()
        .then((credentials) => {
          if (revision !== started) return;
          deviceToken = credentials.device_token?.startsWith("web-device:")
            ? credentials.device_token
            : null;
          sessionToken = credentials.session_token?.startsWith("web-session:")
            ? credentials.session_token
            : null;
          initialized = true;
        })
        .finally(() => {
          pending = null;
        });
    }
    await pending;
  };
  return {
    async loadDeviceCredentialToken() {
      await load();
      return deviceToken;
    },
    async loadSessionToken() {
      await load();
      return sessionToken;
    },
    async saveActivation(nextDeviceToken, nextSessionToken, nextIdentity) {
      revision += 1;
      initialized = true;
      deviceToken = nextDeviceToken;
      sessionToken = nextSessionToken.trim() || null;
      if (nextIdentity) identity = nextIdentity;
    },
    async saveSessionToken(nextSessionToken) {
      revision += 1;
      sessionToken = nextSessionToken;
    },
    async clearSessionToken() {
      const started = revision;
      if (sessionToken)
        await clearCustomerBrowserCredentials({
          kind: "session",
          token: sessionToken,
        });
      if (revision !== started) return;
      revision += 1;
      sessionToken = null;
    },
    async clearAllCredentials() {
      const started = revision;
      const token = deviceToken ?? sessionToken;
      if (token)
        await clearCustomerBrowserCredentials({
          kind: deviceToken ? "device" : "session",
          token,
        });
      if (revision !== started) return;
      revision += 1;
      deviceToken = null;
      sessionToken = null;
      identity = null;
    },
    async deviceInstanceId() {
      return instanceId;
    },
    devicePlatform() {
      return "browser";
    },
    async loadIdentity() {
      return identity;
    },
    // 浏览器没有系统凭据库。这里刻意不降级到 localStorage/sessionStorage——
    // 那等于把账号口令明文留在磁盘上，是 §10.2 明令禁止的。读恒为空，写是
    // 静默 no-op，于是「记住密码」在网页端就是不可用，而不是不安全地可用。
    async loadRememberedLogin() {
      return null;
    },
    async saveRememberedLogin() {},
    async clearRememberedLogin() {},
  };
}

/** The production credential store for the current runtime: the Tauri DPAPI
 * vault on desktop, HttpOnly Cookie transport in browsers. Tests inject
 * their own store instead. */
export function customerCredentialStore(): CustomerCredentialStore {
  return isTauriRuntime()
    ? tauriCustomerCredentialStore()
    : browserCookieCredentialStore();
}
