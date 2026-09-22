import { lazy, Suspense, useEffect, useState } from "react";
// 客户 lane 基础与账户屏样式（F-01/P0-1 修复）：客户制品不含 styles.css，
// 全局 reset、:root 令牌与激活/登录/配对等屏样式必须随本入口加载。
import "./customer/customer-access.css";
import { AccountAccessPage } from "./customer/AccountAccessPage";
import { CustomerWelcomePage } from "./customer/CustomerWelcomePage";

import { LoginPage } from "./customer/LoginPage";
import { SessionConflictDialog } from "./customer/SessionConflictDialog";
import {
  type CustomerCredentialStore,
  customerCredentialStore,
  type RememberedLogin,
  useCustomerSession,
} from "./customer/useCustomerSession";

const CustomerWorkspace = lazy(() =>
  import("./customer/CustomerWorkspace").then((module) => ({
    default: module.CustomerWorkspace,
  })),
);

const ReviewWorkspace = import.meta.env.DEV
  ? lazy(() => import("./studio/ReviewWorkspace"))
  : null;

/** The single customer entry router (CW-013, refined by CW-019): every
 * browser path — the root, deep links, unmatched routes, and a historical
 * internal hash — converges on the customer state machine below, so the
 * internal `<App/>` fallback is deleted and the internal access-token shell
 * is structurally unreachable from here.
 *
 * CW-019: `/admin` is no longer a customer route. The management console now
 * ships as an independent build artifact (`client/dist-admin`, served by
 * nginx `location ^~ /admin/`), and this customer bundle must not contain any
 * admin code — the exclusion is enforced at build time by
 * `scripts/verify_customer_bundle.mjs` and at source level by
 * `entryContract.test.ts`. A request that still lands on the customer
 * `index.html` with `/admin` (misconfigured proxy, stale bookmark) degrades
 * to the customer shell rather than leaking admin UI. The only remaining
 * exception is the dev-only `/review/v1.4` review workspace, statically
 * eliminated in production builds.
 *
 * A fresh mount reads the real `window.location`, so refresh / back /
 * deep-link stay in the customer lane. The Tauri desktop customer build has
 * no admin lane at all and always mounts the customer shell. */
export function RootApp({
  path = window.location.pathname,
}: {
  path?: string;
}) {
  if (ReviewWorkspace && path === "/review/v1.4")
    return (
      <Suspense fallback={<p>正在加载 V1.4 审核工作区…</p>}>
        <ReviewWorkspace />
      </Suspense>
    );
  return <CustomerShell />;
}

/** The customer entry (FE-02): the customer screen state machine from dev
 * doc §4.1. It never renders the internal login shell, so the internal
 * access-token input is structurally not a customer entry. The workspace
 * screen reuses the shared shell under the customer identity.
 *
 * 任务书 B1：配对入口（原 `/customer/pairing` 的 CustomerPairingFlow）已摘除。
 * 新激活方案下不限设备台数、密码登录即用，不存在"添加已有账号设备"这一步。
 * 组件文件保留，实际删除归 B2。 */
function CustomerShell() {
  // The credential store owns session state, so its identity must survive
  // memo cache invalidation (including Fast Refresh). Browser reloads restore
  // CSRF handles from HttpOnly cookies; desktop credentials stay in the vault.
  const [store] = useState(customerCredentialStore);

  return <CustomerSessionShell store={store} />;
}

function CustomerSessionShell({ store }: { store: CustomerCredentialStore }) {
  const session = useCustomerSession(store);
  const [accessOpen, setAccessOpen] = useState(
    () =>
      window.location.pathname === "/login" ||
      window.location.pathname === "/register" ||
      (window.location.hash.startsWith("#studio/") &&
        window.location.hash !== "#studio/workbench"),
  );
  const [accessMode, setAccessMode] = useState<"login" | "register">(
    window.location.pathname === "/register" ? "register" : "login",
  );
  // 「记住密码」只在这里落地：登录页保持纯展示，凭据的读写都收在这一处，
  // 免得口令散落到多个组件里。`undefined` 表示还没读完，用来推迟首帧渲染，
  // 否则输入框会先空一下再被填上。
  const [remembered, setRemembered] = useState<RememberedLogin | null>();
  useEffect(() => {
    let active = true;
    void store
      .loadRememberedLogin()
      .then((login) => {
        if (active) setRemembered(login);
      })
      // 读不出来就当没记住：不能让金库异常把用户挡在登录页外面。
      .catch(() => {
        if (active) setRemembered(null);
      });
    return () => {
      active = false;
    };
  }, [store]);
  useEffect(() => {
    function syncAccessRoute() {
      const path = window.location.pathname;
      setAccessMode(path === "/register" ? "register" : "login");
      setAccessOpen(
        path === "/login" ||
          path === "/register" ||
          (window.location.hash.startsWith("#studio/") &&
            window.location.hash !== "#studio/workbench"),
      );
    }
    window.addEventListener("popstate", syncAccessRoute);
    window.addEventListener("hashchange", syncAccessRoute);
    return () => {
      window.removeEventListener("popstate", syncAccessRoute);
      window.removeEventListener("hashchange", syncAccessRoute);
    };
  }, []);
  function openAccess(mode: "login" | "register") {
    window.history.pushState(null, "", `/${mode}`);
    setAccessMode(mode);
    setAccessOpen(true);
  }

  switch (session.screen) {
    case "checking":
      return (
        <main className="centered-shell">
          <section className="login-card" aria-live="polite">
            <span className="eyebrow">众墅之家 · AI 即创</span>
            <p className="login-hint">正在检查本机登录状态…</p>
          </section>
        </main>
      );
    case "login":
      return (
        <>
          {session.error && (
            <div className="customer-session-notice" role="alert">
              {session.error.message}
            </div>
          )}
          {accessOpen ? (
            // 等金库读完再挂载。登录页用 useState 初始化输入框，晚到的
            // remembered 不会再写进去——先渲染空表单就永远填不上了。这里返回
            // null 而不是退回欢迎页，否则读金库的这一瞬会闪出另一个屏。
            remembered === undefined ? null : (
              <AccountAccessPage
                initialMode={accessMode}
                onModeChange={openAccess}
                remembered={remembered}
                onSubmit={async (input) => {
                  await session.loginWithPassword(input);
                  // 只有登录成功才写入：登录失败时保存一份错口令，下次预填的就是
                  // 错的，反而更难用。
                  if (input.remember) {
                    await store.saveRememberedLogin({
                      username: input.username,
                      password: input.password,
                    });
                    setRemembered({
                      username: input.username,
                      password: input.password,
                    });
                  } else {
                    // 取消勾选等于撤回授权，必须当场清掉此前记住的口令。
                    await store.clearRememberedLogin();
                    setRemembered(null);
                  }
                  setAccessOpen(false);
                }}
                onHome={() => {
                  setAccessOpen(false);
                  window.history.replaceState(null, "", "/#studio/workbench");
                }}
              />
            )
          ) : (
            <CustomerWelcomePage onLogin={() => openAccess("login")} />
          )}
        </>
      );
    case "binding-conflict":
      // The conflict screen only exists with conflict metadata; the reducer
      // and this component dispatch together, so a null conflict here means
      // the dialog already cancelled and the screen fell back to login.
      return session.conflict === null ? (
        <LoginPage
          onRetryLogin={() => void session.retryLogin()}
          isBusy={session.isBusy}
          error={session.error}
          conflict={session.conflict}
        />
      ) : (
        <SessionConflictDialog
          conflict={session.conflict}
          // FE-03: a failed switch must be visible. Independent switchError —
          // the 409 that opened this screen also sits in session.error, and
          // reusing it would show "切换失败" before any switch attempt.
          error={session.switchError}
          onCancel={session.cancelSessionSwitch}
          // Return the switch promise: the dialog awaits onSwitch to keep
          // its buttons disabled, so a discarded promise would let a second
          // click start another switch with a new idempotency key.
          onSwitch={() => session.switchSession()}
        />
      );
    case "workspace":
      // The workspace screen is only reachable after activate/login set the
      // identity; the checking fallback below is unreachable in practice.
      return session.user === null ? null : (
        <Suspense fallback={<p role="status">正在加载工作台…</p>}>
          <CustomerWorkspace
            user={session.user}
            sessionRuntime={session.sessionRuntime}
            onManualHeartbeat={() => void session.sendHeartbeatNow()}
            onLogout={session.logout}
            store={store}
            // F-01 review (P0-3): the workspace calls this when it lost the
            // session locally (missing token / 401 without a lifecycle event).
            // restartAfterExpiry is a guarded no-op from the workspace screen —
            // the dedicated local expiry lands on the expired terminal instead.
            onSessionExpired={session.expireSessionLocally}
          />
        </Suspense>
      );
    case "session-expired":
      return (
        <CustomerTerminalScreen
          title="登录已过期"
          description="会话已过期，请重新登录。"
          actionLabel="重新登录"
          onAction={session.restartAfterExpiry}
        />
      );
    case "session-replaced":
      return (
        <CustomerTerminalScreen
          title="本设备已下线"
          description="本次登录凭据已失效，请重新登录。其他设备的登录不受影响。"
          actionLabel="重新登录"
          onAction={session.restartAfterExpiry}
        />
      );
    case "device-revoked":
      return (
        <CustomerTerminalScreen
          title="设备已被解绑"
          description="本设备已被解绑，请重新登录后使用。"
          actionLabel="重新登录"
          onAction={session.restartAfterRevocation}
        />
      );
  }
}

/** A terminal screen (§4.2): displaced/expired/revoked sessions are reported
 * as exactly what they are — never as a balance, network, or generic service
 * failure — with the single recovery action the state machine allows. */
function CustomerTerminalScreen({
  title,
  description,
  actionLabel,
  onAction,
}: {
  title: string;
  description: string;
  actionLabel: string;
  onAction(): void;
}) {
  return (
    <main className="centered-shell">
      <section className="login-card" aria-labelledby="customer-terminal-title">
        <span className="eyebrow">众墅之家 · AI 即创</span>
        <h1 id="customer-terminal-title">{title}</h1>
        <p className="login-hint">{description}</p>
        <button type="button" onClick={onAction}>
          {actionLabel}
        </button>
      </section>
    </main>
  );
}
