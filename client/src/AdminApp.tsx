import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { AnalyticsPage } from "./admin/AnalyticsPage";
import { AuditCenterPage } from "./admin/AuditCenterPage";
import { CustomersManagementPage } from "./admin/CustomersManagementPage";
import { FundsPage } from "./admin/FundsPage";
import { GenerationRecordsPage } from "./admin/GenerationRecordsPage";
import { OverviewPage } from "./admin/OverviewPage";
import { SessionsPage } from "./admin/SessionsPage";
import { SubAccountsPage } from "./admin/SubAccountsPage";
import { SystemSettingsPage } from "./admin/SystemSettingsPage";
import { AdminErrorBoundary } from "./admin/ui/AdminErrorBoundary";
import { shanghaiDate } from "./admin/ui/vocabulary";
import { ViralVideosPage } from "./admin/ViralVideosPage";
import "./admin/admin-login.css";
import { PageBanner } from "./admin/ui/PageBanner";
import { TabBar } from "./admin/ui/TabBar";
import { roleLabel } from "./admin/ui/vocabulary";
import { SESSION_EXPIRED_EVENT } from "./api";
import {
  AdminActivationError,
  type AdminActorInfo,
  adminActivationErrorMessage,
  clearAdminActivationSession,
  deleteAdminSession,
  exchangeAdminSession,
  fetchAdminSession,
  loginAdminWithPassword,
  recoverAdminPassword,
} from "./api.admin";
import zhongshuBrandLogo from "./assets/brand/zhongshu-logo-mark.svg";
import chartIcon from "./assets/icons/chart-no-axes-combined.svg";
import clapperboardIcon from "./assets/icons/clapperboard.svg";
import gaugeIcon from "./assets/icons/gauge.svg";
import settingsIcon from "./assets/icons/settings.svg";
import shieldIcon from "./assets/icons/shield-check.svg";
import usersIcon from "./assets/icons/users-round.svg";
import walletIcon from "./assets/icons/wallet.svg";

type AuthPhase =
  | "checking"
  | "anonymous"
  | "recovery"
  | "password-setup"
  | "ready";

type AuthPendingAction = "" | "login" | "exchange" | "recover";

// ADMIN-UI-AUDIT-20260911：登录门错误本地化。RATE_LIMITED 与
// ADMIN_SESSION_CONTEXT_CHANGED 是仅有的两个无中文映射的登录路径错误码，
// 未经映射时管理员会看到服务端英文原文；overrides 只作用于本组件，
// 不改 api.admin.ts 全局映射（其余页面文案已被既有测试钉住）。
// ADMIN-LOGIN-POLISH-20260912：会话绑定已按 B1·方案② 收窄为仅浏览器
// 环境（见 #53），文案同步去掉"网络"语义。
const loginErrorOverrides = {
  RATE_LIMITED: "登录尝试过于频繁，请稍后再试。",
  ADMIN_SESSION_CONTEXT_CHANGED: "检测到浏览器环境变化，请重新登录。",
};

export type AdminTab =
  | "overview"
  | "analytics"
  | "funds"
  | "customersMgmt"
  | "sessions"
  | "subAccounts"
  | "generationRecords"
  | "viralVideos"
  | "auditCenter"
  | "systemSettings";

const tabGroups: Array<{
  id: string;
  label: string;
  tabs: Array<{ id: AdminTab; label: string; helper: string }>;
}> = [
  {
    id: "overview",
    label: "运营概览",
    tabs: [
      { id: "overview", label: "总览仪表盘", helper: "核心指标与经营总览" },
      { id: "analytics", label: "经营分析", helper: "利润、成本与趋势" },
      { id: "funds", label: "资金流水", helper: "充值订单与额度流水" },
    ],
  },
  {
    id: "operations",
    label: "客户运营",
    tabs: [
      {
        id: "customersMgmt",
        label: "客户管理",
        helper: "客户账户与积分管理",
      },
      {
        id: "sessions",
        label: "会话与设备",
        helper: "在线会话、强制下线与设备解绑",
      },
      {
        id: "subAccounts",
        label: "子账号管理",
        helper: "为母账号创建和管理子账号",
      },
      {
        id: "generationRecords",
        label: "生成记录",
        helper: "视频、图片与 AI 评分费用追溯",
      },
      {
        id: "viralVideos",
        label: "爆款视频库",
        helper: "采集数据、首页展示与删除管理",
      },
    ],
  },
  {
    id: "governance",
    label: "系统治理",
    tabs: [
      { id: "auditCenter", label: "审计中心", helper: "审计日志与调账记录" },
      {
        id: "systemSettings",
        label: "系统设置",
        helper: "支付、费率与服务配置",
      },
    ],
  },
];
const tabPageTitles: Record<AdminTab, string> = {
  overview: "总览仪表盘",
  analytics: "经营分析",
  funds: "资金流水",
  customersMgmt: "客户管理",
  sessions: "会话与设备",
  subAccounts: "子账号管理",
  generationRecords: "用户生成记录",
  viralVideos: "爆款视频库",
  auditCenter: "审计中心",
  systemSettings: "系统设置",
};

const compactNavigationBreakpoint = 1024;
const navigationIcons: Record<AdminTab, string> = {
  overview: gaugeIcon,
  analytics: chartIcon,
  funds: walletIcon,
  customersMgmt: usersIcon,
  sessions: shieldIcon,
  subAccounts: usersIcon,
  generationRecords: clapperboardIcon,
  viralVideos: clapperboardIcon,
  auditCenter: shieldIcon,
  systemSettings: settingsIcon,
};

const adminTabs = new Set<AdminTab>(Object.keys(tabPageTitles) as AdminTab[]);
const adminIntents = new Set([
  "costDetails",
  "customerAdjustments",
  "failedGenerationRecords",
  "analysisFailures",
  "rates",
]);

export function adminRouteFromHash(hash: string): {
  tab: AdminTab;
  intent: string;
} {
  const [path, query = ""] = hash.replace(/^#/, "").split("?", 2);
  const requested = path.replace(/^admin\//, "") as AdminTab;
  return {
    tab: adminTabs.has(requested) ? requested : "overview",
    intent: (() => {
      const intent = new URLSearchParams(query).get("intent") ?? "";
      return adminIntents.has(intent) ? intent : "";
    })(),
  };
}

function adminHash(tab: AdminTab, intent = "") {
  const params = new URLSearchParams();
  if (intent) params.set("intent", intent);
  const query = params.toString();
  return `#admin/${tab}${query ? `?${query}` : ""}`;
}

export function AdminApp() {
  const [authPhase, setAuthPhase] = useState<AuthPhase>("checking");
  const [actor, setActor] = useState<AdminActorInfo | null>(null);
  const [loginUsername, setLoginUsername] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [recoveryCredential, setRecoveryCredential] = useState("");
  const [recoveryTrimmed, setRecoveryTrimmed] = useState(false);
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [authPending, setAuthPending] = useState<AuthPendingAction>("");
  const [showPassword, setShowPassword] = useState(false);
  const loginUsernameRef = useRef<HTMLInputElement>(null);
  const recoveryCredentialRef = useRef<HTMLInputElement>(null);
  const newPasswordRef = useRef<HTMLInputElement>(null);
  const initialRoute = adminRouteFromHash(window.location.hash);
  const [activeTab, setActiveTab] = useState<AdminTab>(initialRoute.tab);
  const [navigationIntent, setNavigationIntent] = useState(initialRoute.intent);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [isCompactNavigation, setIsCompactNavigation] = useState(() =>
    typeof window !== "undefined"
      ? window.innerWidth < compactNavigationBreakpoint
      : false,
  );
  const [isNavigationOpen, setIsNavigationOpen] = useState(() =>
    typeof window !== "undefined"
      ? window.innerWidth >= compactNavigationBreakpoint
      : true,
  );

  const navigateAdmin = useCallback((tab: AdminTab, intent = "") => {
    setActiveTab(tab);
    setNavigationIntent(intent);
    window.history.pushState(null, "", adminHash(tab, intent));
  }, []);

  useEffect(() => {
    const current = adminRouteFromHash(window.location.hash);
    const normalized = adminHash(current.tab, current.intent);
    if (window.location.hash !== normalized)
      window.history.replaceState(null, "", normalized);
    const restore = () => {
      const route = adminRouteFromHash(window.location.hash);
      setActiveTab(route.tab);
      setNavigationIntent(route.intent);
    };
    window.addEventListener("hashchange", restore);
    window.addEventListener("popstate", restore);
    return () => {
      window.removeEventListener("hashchange", restore);
      window.removeEventListener("popstate", restore);
    };
  }, []);

  const handleSessionExpired = useCallback(
    (message = "会话已失效，请重新登录。") => {
      clearAdminActivationSession();
      setActor(null);
      setAuthPhase("anonymous");
      setLoginPassword("");
      setShowPassword(false);
      setAuthPending("");
      setRecoveryCredential("");
      setRecoveryTrimmed(false);
      setNewPassword("");
      setConfirmPassword("");
      setIsNavigationOpen(
        typeof window !== "undefined" &&
          window.innerWidth >= compactNavigationBreakpoint,
      );
      setNotice("");
      setError(message);
    },
    [],
  );

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const session = await fetchAdminSession();
        if (cancelled) {
          return;
        }
        if (!session.csrf_token) {
          clearAdminActivationSession();
          setAuthPhase("anonymous");
          return;
        }
        setActor(session.actor);
        setLoginUsername(session.actor.username);
        setAuthPhase(
          session.auth_method === "exchange" ? "password-setup" : "ready",
        );
      } catch (cause) {
        if (cancelled) {
          return;
        }
        clearAdminActivationSession();
        if (!(cause instanceof AdminActivationError && cause.status === 401)) {
          setError(adminActivationErrorMessage(cause, "读取管理会话失败"));
        }
        setAuthPhase("anonymous");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const onSessionExpired = () => {
      handleSessionExpired();
    };
    window.addEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
    return () => {
      window.removeEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
    };
  }, [handleSessionExpired]);

  // 登录门各阶段进入时聚焦首个输入框（biome 禁用 autoFocus 属性，改用 ref）。
  useEffect(() => {
    if (authPhase === "anonymous") {
      loginUsernameRef.current?.focus();
    } else if (authPhase === "recovery") {
      recoveryCredentialRef.current?.focus();
    } else if (authPhase === "password-setup") {
      newPasswordRef.current?.focus();
    }
  }, [authPhase]);

  useEffect(() => {
    const syncNavigationMode = () => {
      const nextCompact = window.innerWidth < compactNavigationBreakpoint;
      setIsCompactNavigation(nextCompact);
      setIsNavigationOpen((current) => (nextCompact ? current : true));
    };
    syncNavigationMode();
    window.addEventListener("resize", syncNavigationMode);
    return () => window.removeEventListener("resize", syncNavigationMode);
  }, []);

  async function signInWithPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // 后端按 IP+账号双维度对每次登录尝试计数：提交期间必须拦住重复提交，
    // 否则双击会白烧限流预算，可能把管理员自己锁在门外。
    if (authPending) {
      return;
    }
    setError("");
    setNotice("");
    const username = loginUsername.trim();
    if (!username || !loginPassword) {
      setError("请输入管理员账号和密码。");
      return;
    }
    setAuthPending("login");
    try {
      const result = await loginAdminWithPassword(username, loginPassword);
      setActor(result.actor);
      setLoginPassword("");
      setShowPassword(false);
      setAuthPhase("ready");
      const route = adminRouteFromHash(window.location.hash);
      setActiveTab(route.tab);
      setNavigationIntent(route.intent);
    } catch (cause) {
      setError(
        adminActivationErrorMessage(cause, "后台登录失败", loginErrorOverrides),
      );
    } finally {
      setAuthPending("");
    }
  }

  async function verifyRecoveryCredential(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (authPending) {
      return;
    }
    setError("");
    setNotice("");
    const credential = recoveryCredential.trim();
    if (!credential) {
      setError("请输入一次性恢复凭据。");
      return;
    }
    setAuthPending("exchange");
    try {
      const result = await exchangeAdminSession(credential);
      setActor(result.actor);
      setLoginUsername(result.actor.username);
      setRecoveryCredential("");
      setRecoveryTrimmed(false);
      setAuthPhase("password-setup");
    } catch (cause) {
      setError(
        adminActivationErrorMessage(
          cause,
          "恢复凭据验证失败",
          loginErrorOverrides,
        ),
      );
    } finally {
      setAuthPending("");
    }
  }

  async function saveRecoveredPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (authPending) {
      return;
    }
    setError("");
    setNotice("");
    if (newPassword.length < 12 || newPassword.length > 128) {
      setError("新密码需为 12 至 128 个字符。");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("两次输入的新密码不一致。");
      return;
    }
    setAuthPending("recover");
    try {
      await recoverAdminPassword(newPassword);
      setActor(null);
      setNewPassword("");
      setConfirmPassword("");
      setAuthPhase("anonymous");
      setNotice("密码已设置，请使用管理员账号和新密码登录。");
    } catch (cause) {
      setError(
        adminActivationErrorMessage(
          cause,
          "设置管理员密码失败",
          loginErrorOverrides,
        ),
      );
    } finally {
      setAuthPending("");
    }
  }

  const signOut = useCallback(async () => {
    let serverReleased = true;
    try {
      await deleteAdminSession();
    } catch {
      serverReleased = false;
    } finally {
      handleSessionExpired("");
    }
    if (!serverReleased) {
      // 网络失败/5xx 时 HttpOnly 会话 cookie 在服务端仍然有效，共享机器上的
      // 下一位使用者可经 fetchAdminSession 静默恢复全套权限。本地照常回登录
      // 门，但必须把「未确认释放」说清楚（对齐客户泳道 CW-017 的诚实登出）。
      setNotice(
        "已回到登录页，但服务端未确认本次会话释放；在共用电脑上请关闭全部浏览器窗口，必要时联系管理员强制下线。",
      );
    }
  }, [handleSessionExpired]);

  if (authPhase !== "ready" || !actor) {
    return (
      <main className="admin-shell admin-shell--gate">
        <div className="admin-login">
          <section className="admin-login__brand">
            <div className="admin-login__brand-mark">
              <img alt="众墅之家" src={zhongshuBrandLogo} />
            </div>
            <h1>运营管理后台</h1>
            <ul className="admin-login__points">
              <li>会话绑定当前浏览器环境，更换浏览器后需重新登录</li>
              <li>登录与敏感操作全部记入审计日志</li>
              <li>管理员与审计员分角色授权，审计员只读</li>
            </ul>
          </section>

          <section className="admin-login__card" aria-label="后台登录">
            {error ? <PageBanner tone="error">{error}</PageBanner> : null}
            {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

            {authPhase === "checking" ? (
              <p className="admin-login__status">正在检查登录状态…</p>
            ) : null}

            {authPhase === "anonymous" ? (
              <form className="admin-login__form" onSubmit={signInWithPassword}>
                <h2>管理员登录</h2>
                <div className="admin-login__control">
                  <label htmlFor="admin-login-username">管理员账号</label>
                  <input
                    id="admin-login-username"
                    autoComplete="username"
                    ref={loginUsernameRef}
                    value={loginUsername}
                    onChange={(event) => setLoginUsername(event.target.value)}
                  />
                </div>
                <div className="admin-login__control">
                  <label htmlFor="admin-login-password">管理员密码</label>
                  <div className="admin-login__password-row">
                    <input
                      id="admin-login-password"
                      autoComplete="current-password"
                      type={showPassword ? "text" : "password"}
                      value={loginPassword}
                      onChange={(event) => setLoginPassword(event.target.value)}
                    />
                    <button
                      aria-label={showPassword ? "隐藏密码" : "显示密码"}
                      aria-pressed={showPassword}
                      className="admin-login__toggle"
                      type="button"
                      onClick={() => setShowPassword((current) => !current)}
                    >
                      {showPassword ? "隐藏" : "显示"}
                    </button>
                  </div>
                </div>
                <button
                  className="admin-login__submit"
                  disabled={authPending !== ""}
                  type="submit"
                >
                  {authPending === "login" ? "正在登录…" : "登录后台"}
                </button>
                <button
                  className="admin-login__link"
                  type="button"
                  onClick={() => {
                    setError("");
                    setNotice("");
                    setAuthPhase("recovery");
                  }}
                >
                  首次设置或找回密码
                </button>
              </form>
            ) : null}

            {authPhase === "recovery" ? (
              <form
                aria-label="管理员密码恢复"
                className="admin-login__form"
                onSubmit={verifyRecoveryCredential}
              >
                <h2>首次设置或找回密码</h2>
                <p className="admin-login__hint">
                  一次性恢复凭据只用于验证身份和设置新密码，不能作为日常登录方式。
                </p>
                <div className="admin-login__control">
                  <label htmlFor="admin-login-recovery">一次性恢复凭据</label>
                  <input
                    id="admin-login-recovery"
                    autoComplete="off"
                    placeholder="ASX1.…"
                    ref={recoveryCredentialRef}
                    type="password"
                    value={recoveryCredential}
                    onChange={(event) => {
                      setRecoveryCredential(event.target.value);
                      setRecoveryTrimmed(
                        event.target.value !== event.target.value.trim(),
                      );
                    }}
                  />
                  <small className="admin-login__hint">
                    凭据以 ASX1. 开头。
                  </small>
                  {recoveryTrimmed ? (
                    <small className="admin-login__hint">
                      已自动忽略首尾空白。
                    </small>
                  ) : null}
                </div>
                <button
                  className="admin-login__submit"
                  disabled={authPending !== ""}
                  type="submit"
                >
                  {authPending === "exchange" ? "正在验证…" : "验证恢复凭据"}
                </button>
                <button
                  className="admin-login__link"
                  type="button"
                  onClick={() => setAuthPhase("anonymous")}
                >
                  返回账号密码登录
                </button>
              </form>
            ) : null}

            {authPhase === "password-setup" ? (
              <form
                aria-label="设置管理员密码"
                className="admin-login__form"
                onSubmit={saveRecoveredPassword}
              >
                <h2>设置管理员密码</h2>
                <p className="admin-login__hint">
                  当前账号：{actor?.username}。保存后，所有旧会话都会失效。
                </p>
                <div className="admin-login__control">
                  <label htmlFor="admin-login-new-password">新管理员密码</label>
                  <input
                    id="admin-login-new-password"
                    autoComplete="new-password"
                    ref={newPasswordRef}
                    type="password"
                    value={newPassword}
                    onChange={(event) => setNewPassword(event.target.value)}
                  />
                </div>
                <div className="admin-login__control">
                  <label htmlFor="admin-login-confirm-password">
                    确认新管理员密码
                  </label>
                  <input
                    id="admin-login-confirm-password"
                    autoComplete="new-password"
                    type="password"
                    value={confirmPassword}
                    onChange={(event) => setConfirmPassword(event.target.value)}
                  />
                </div>
                <button
                  className="admin-login__submit"
                  disabled={authPending !== ""}
                  type="submit"
                >
                  {authPending === "recover" ? "正在保存…" : "保存新密码"}
                </button>
              </form>
            ) : null}
          </section>
        </div>
      </main>
    );
  }

  const activeTabMeta = tabGroups
    .flatMap((group) => group.tabs)
    .find((tab) => tab.id === activeTab);
  const activeGroupLabel =
    tabGroups.find((group) => group.tabs.some((tab) => tab.id === activeTab))
      ?.label ?? "运营后台";
  const activePageTitle = tabPageTitles[activeTab];
  const readOnly = actor.role === "auditor";

  return (
    <main className="admin-shell admin-shell--control">
      <header className="admin-header admin-header--workspace">
        <div className="admin-title-block">
          {isCompactNavigation ? (
            <button
              aria-controls="admin-navigation"
              aria-expanded={isNavigationOpen}
              aria-label={isNavigationOpen ? "关闭导航" : "展开导航"}
              className="admin-nav-toggle"
              type="button"
              onClick={() => setIsNavigationOpen((current) => !current)}
            >
              菜单
            </button>
          ) : null}
          <span className="admin-breadcrumb">
            {activeGroupLabel} / {activePageTitle}
          </span>
        </div>
        <div className="admin-session">
          <time dateTime={shanghaiDate()}>{shanghaiDate()}</time>
          <button type="button" onClick={() => void signOut()}>
            退出登录
          </button>
        </div>
      </header>

      <section className="admin-stage">
        {isCompactNavigation && isNavigationOpen ? (
          <button
            aria-label="关闭导航遮罩"
            className="admin-sidebar-backdrop"
            type="button"
            onClick={() => setIsNavigationOpen(false)}
          />
        ) : null}
        <aside
          aria-hidden={isCompactNavigation && !isNavigationOpen}
          className={
            isCompactNavigation && isNavigationOpen
              ? "admin-sidebar is-open"
              : "admin-sidebar"
          }
        >
          <div className="admin-sidebar__top">
            <div className="admin-brand">
              <img alt="众墅之家" src={zhongshuBrandLogo} />
              <span>AI 即创 · 运营管理后台</span>
            </div>
          </div>

          {isNavigationOpen ? (
            <nav
              aria-label="管理端导航"
              className="admin-nav-groups"
              id="admin-navigation"
            >
              {tabGroups.map((group) => (
                <section className="admin-nav-group" key={group.id}>
                  <h3>{group.label}</h3>
                  <div className="admin-nav-group__items">
                    {group.tabs.map((tab) => (
                      <button
                        aria-current={activeTab === tab.id ? "page" : undefined}
                        aria-label={tab.label}
                        className={
                          activeTab === tab.id
                            ? "admin-tab is-active"
                            : "admin-tab"
                        }
                        key={tab.id}
                        type="button"
                        onClick={() => {
                          navigateAdmin(tab.id);
                          // C3：切标签清掉上一页残留的全局提示。
                          setError("");
                          setNotice("");
                          if (isCompactNavigation) {
                            setIsNavigationOpen(false);
                          }
                        }}
                      >
                        <img
                          alt=""
                          aria-hidden="true"
                          className="admin-navigation-icon"
                          src={navigationIcons[tab.id]}
                        />
                        <span className="admin-navigation-copy">
                          <span>{tab.label}</span>
                          <small>{tab.helper}</small>
                        </span>
                      </button>
                    ))}
                  </div>
                </section>
              ))}
            </nav>
          ) : null}
          <div className="admin-sidebar__footer">
            <span className="admin-sidebar__avatar" aria-hidden="true">
              {actor.display_name.slice(0, 1)}
            </span>
            <div>
              <span>{actor.display_name}</span>
              <small>{roleLabel(actor.role)}</small>
            </div>
          </div>
        </aside>

        <div className="admin-workspace">
          {error ? <PageBanner tone="error">{error}</PageBanner> : null}
          {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

          <header className="admin-page-heading">
            <span>{activeGroupLabel}</span>
            <h1>{activePageTitle}</h1>
            <p>{activeTabMeta?.helper ?? "运营核心视图"}</p>
          </header>

          {activeTab === "overview" ? (
            <>
              <TabBar
                ariaLabel="运营概览快捷导航"
                items={tabGroups[0].tabs}
                active={activeTab}
                onChange={(tab) => navigateAdmin(tab as AdminTab)}
              />
              <OverviewPage
                readOnly={readOnly}
                onNavigate={(destination) => {
                  const routes: Record<string, AdminTab> = {
                    customerAdjustments: "customersMgmt",
                    costDetails: "analytics",
                    failedGenerationRecords: "generationRecords",
                    analysisFailures: "generationRecords",
                    rates: "systemSettings",
                  };
                  navigateAdmin(
                    routes[destination] ?? (destination as AdminTab),
                    destination,
                  );
                }}
              />
            </>
          ) : null}
          {activeTab === "analytics" ? (
            <AnalyticsPage
              readOnly={readOnly}
              initialTab={
                navigationIntent === "costDetails" ? "cost" : "profit"
              }
            />
          ) : null}
          {activeTab === "funds" ? <FundsPage readOnly={readOnly} /> : null}
          {activeTab === "customersMgmt" ? (
            /* 总览快捷入口（后台加款 / 发放赠送积分 / 历史激活码入口）都落在
               本页：把 intent 透传下去，客户管理页才能给出对应表单或说明，
               否则点完只是换了个页签、没有下文。 */
            <CustomersManagementPage
              initialIntent={navigationIntent}
              operatorId={actor.user_id}
              readOnly={readOnly}
            />
          ) : null}
          {/* 任务书 C：恢复在线会话挂载。多设备并存下按设备强制下线是管理刚需，
              readOnly 由会话页自身收敛（auditor 不渲染强制下线与调账入口）。 */}
          {activeTab === "sessions" ? (
            <SessionsPage readOnly={readOnly} />
          ) : null}
          {activeTab === "subAccounts" ? (
            <AdminErrorBoundary>
              <SubAccountsPage
                embedded={false}
                operatorId={actor.user_id}
                readOnly={readOnly}
              />
            </AdminErrorBoundary>
          ) : null}
          {activeTab === "generationRecords" ? (
            <GenerationRecordsPage
              key={`generationRecords:${navigationIntent}`}
              initialStatus={
                navigationIntent === "failedGenerationRecords" ||
                navigationIntent === "analysisFailures"
                  ? "FAILED"
                  : undefined
              }
              initialRecordType={
                navigationIntent === "analysisFailures" ? "ANALYSIS" : undefined
              }
              readOnly={readOnly}
            />
          ) : null}
          {activeTab === "auditCenter" ? <AuditCenterPage /> : null}
          {activeTab === "viralVideos" ? (
            <ViralVideosPage readOnly={readOnly} />
          ) : null}
          {activeTab === "systemSettings" ? (
            <SystemSettingsPage
              readOnly={readOnly}
              initialTab={navigationIntent === "rates" ? "rates" : "payment"}
            />
          ) : null}
        </div>
      </section>
    </main>
  );
}
