import { useEffect, useState } from "react";

import {
  type CustomerDeviceListResponse,
  type CustomerProfile,
  customerListSubAccounts,
} from "../api";
import { CustomerWalletPanel } from "./CustomerWalletPanel";
import { DeviceManagementPage } from "./DeviceManagementPage";
import { HeartbeatStatus } from "./HeartbeatStatus";
import { LeaseCountdown } from "./LeaseCountdown";
import {
  forecastQuota,
  type QuotaOverview,
  type ShanghaiMonthProgress,
  shanghaiMonthProgress,
  summarizeSubAccountQuotas,
} from "./quotaViz";
import { SubAccountManagementPage } from "./SubAccountManagementPage";
import type {
  CustomerCredentialStore,
  CustomerLogoutOutcome,
  CustomerSessionRuntime,
  CustomerStoredIdentity,
} from "./useCustomerSession";
import { useLeaseActive } from "./useLeaseActive";

type ProfileTab = "overview" | "devices" | "billing" | "sub-accounts";

export function CustomerProfilePanel({
  devices,
  deviceError,
  identity = null,
  identityLoader,
  onManualHeartbeat,
  onLogout,
  onPairDevice,
  onProfileUpdated,
  onRefreshProfile,
  onRecharge,
  onRefreshDevices,
  onSessionExpired,
  onUnbind,
  onUpdateProfile,
  profile,
  profileLoadError,
  sessionRuntime = null,
  store,
  walletRefreshKey,
  now,
}: {
  devices: CustomerDeviceListResponse | null;
  deviceError: string;
  /** CW-062：会话身份（首次登录响应/凭据库缓存），null 时徽章隐藏。 */
  identity?: CustomerStoredIdentity | null;
  /** CW-062：再读一次本地缓存身份（挂载时 user 尚未携带身份的恢复路径）。 */
  identityLoader?: () => Promise<CustomerStoredIdentity | null>;
  onApprovePairing: (pairingId: string) => void;
  onDismissPairing: (pairingId: string) => void;
  onManualHeartbeat?: () => void;
  onLogout: () => Promise<CustomerLogoutOutcome>;
  /** 设备管理页"绑定第二台设备"的页内导航回调（缺省时隐藏入口）。 */
  onPairDevice?: () => void;
  onProfileUpdated: (profile: CustomerProfile) => void;
  onRefreshProfile: () => Promise<void>;
  onRecharge: (amountYuan?: number, packageId?: string) => void;
  onRefreshDevices: () => Promise<void>;
  onSessionExpired: () => void;
  onUnbind: (deviceId: string) => void;
  onUpdateProfile: (displayName: string) => Promise<CustomerProfile>;
  profile: CustomerProfile | null;
  profileLoadError: string;
  sessionRuntime?: CustomerSessionRuntime | null;
  store: CustomerCredentialStore;
  walletRefreshKey: number;
  /** 测试注入固定时刻；缺省取渲染时当前时间（与子账号额度卡的月末口径一致）。 */
  now?: Date;
}) {
  const [displayName, setDisplayName] = useState(profile?.display_name ?? "");
  const [profileError, setProfileError] = useState("");
  const [profileNotice, setProfileNotice] = useState("");
  const [isSavingProfile, setIsSavingProfile] = useState(false);
  const [isRetryingProfile, setIsRetryingProfile] = useState(false);
  const [isLoggingOut, setIsLoggingOut] = useState(false);
  // isResettingCode / replacementCode 已删除（激活码方案废弃）
  // deferredPairingIds 已删除：它只服务于「暂缓配对」，随配对审批入口一并摘除（B1）。
  // 批次1：母账号概览「子账号数 / 本月子账号消费」两卡——惰性拉一次列表，
  // 失败静默（概览卡是锦上添花，不打扰个人中心主路径）。
  const [subAccountOverview, setSubAccountOverview] =
    useState<QuotaOverview | null>(null);
  const pendingPairings = devices?.pending_pairings ?? [];
  const isOnline = useLeaseActive(sessionRuntime?.leaseExpiresAt ?? null);

  // CW-062：身份来自会话缓存（首登响应写入）；profile 是权威副本，加载后
  // 以它为准（重启恢复的先头帧可能还是未知身份）。
  const [loadedIdentity, setLoadedIdentity] =
    useState<CustomerStoredIdentity | null>(identity);
  useEffect(() => {
    if (identity) {
      setLoadedIdentity(identity);
      return;
    }
    if (!identityLoader) {
      return;
    }
    let active = true;
    void identityLoader()
      .then((next) => {
        if (active && next) {
          setLoadedIdentity(next);
        }
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [identity, identityLoader]);
  useEffect(() => {
    const accountType = profile?.account_type;
    if (!accountType) {
      return;
    }
    setLoadedIdentity((current) => ({
      accountType: parseAccountType(accountType),
      parentUserId: profile.parent_user_id ?? current?.parentUserId ?? null,
      parentDisplayName:
        profile.parent_display_name ?? current?.parentDisplayName ?? null,
    }));
  }, [
    profile?.account_type,
    profile?.parent_user_id,
    profile?.parent_display_name,
  ]);
  const isMaster = loadedIdentity?.accountType === "MASTER";

  const [tab, setTab] = useState<ProfileTab>("overview");
  // 子账号页签只对母账号出现；身份后到时（恢复路径）若已站在该页签，退回概览。
  useEffect(() => {
    if (tab === "sub-accounts" && !isMaster) {
      setTab("overview");
    }
  }, [tab, isMaster]);

  useEffect(() => {
    setDisplayName(profile?.display_name ?? "");
  }, [profile?.display_name]);

  useEffect(() => {
    if (!isMaster || tab !== "overview" || subAccountOverview !== null) {
      return;
    }
    let active = true;
    void (async () => {
      try {
        const token = await store.loadSessionToken();
        if (token === null) {
          return;
        }
        const list = await customerListSubAccounts({ kind: "session", token });
        if (active) {
          setSubAccountOverview(summarizeSubAccountQuotas(list));
        }
      } catch {
        // 静默：概览卡加载失败不改变个人中心其余内容。
      }
    })();
    return () => {
      active = false;
    };
  }, [isMaster, tab, store, subAccountOverview]);

  async function saveProfile(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextName = displayName.trim();
    if (!nextName) {
      setProfileError("请输入显示名称。");
      return;
    }
    setIsSavingProfile(true);
    setProfileError("");
    setProfileNotice("");
    try {
      const nextProfile = await onUpdateProfile(nextName);
      onProfileUpdated(nextProfile);
      setProfileNotice("个人资料已保存。");
    } catch (cause) {
      setProfileError(errorMessage(cause, "保存个人资料失败，请稍后重试。"));
    } finally {
      setIsSavingProfile(false);
    }
  }

  // resetActivationCode 已删除（激活码方案废弃，2026-09-19）

  async function retryProfile() {
    if (isRetryingProfile) {
      return;
    }
    setIsRetryingProfile(true);
    try {
      await onRefreshProfile();
    } catch (cause) {
      setProfileError(
        errorMessage(cause, "重新加载账号资料失败，请稍后重试。"),
      );
    } finally {
      setIsRetryingProfile(false);
    }
  }

  async function logout() {
    if (isLoggingOut) {
      return;
    }
    setIsLoggingOut(true);
    setProfileError("");
    try {
      await onLogout();
    } catch (cause) {
      setProfileError(errorMessage(cause, "退出登录失败，请稍后重试。"));
    } finally {
      setIsLoggingOut(false);
    }
  }

  // Phase 3a：只有子账号会话带 quota 字段（母账号两者皆 null），据此决定
  // 「本月额度」卡是否出现。负数已用量（历史跨月退回遗留）显示前钳到 0。
  const quotaCap = profile?.monthly_quota_credits ?? null;
  const rawQuotaUsed = profile?.quota_used_credits ?? null;
  const quotaUsed = rawQuotaUsed === null ? null : Math.max(0, rawQuotaUsed);
  const quotaRemaining =
    quotaCap === null || quotaUsed === null
      ? null
      : Math.max(0, quotaCap - quotaUsed);
  // 批次1：月度进度（上海自然月）与预测，用于额度卡的百分比/距离月末/日均行。
  const quotaProgress = shanghaiMonthProgress(now ?? new Date());
  const quotaForecast =
    quotaUsed !== null && quotaCap !== null
      ? forecastQuota(quotaUsed, quotaCap, quotaProgress)
      : null;

  return (
    <section className="customer-profile" aria-label="个人中心">
      <header className="customer-profile__hero">
        <div>
          <p className="eyebrow">个人中心</p>
          {/* 徽章放在 h2 外：标题的 accessible name 必须是纯显示名。 */}
          <div className="customer-profile__name-line">
            <h2>{profile?.display_name ?? "客户账号"}</h2>
            {identityBadge(loadedIdentity) ? (
              <span
                className={`customer-profile__badge ${
                  isMaster ? "is-master" : "is-sub"
                }`}
              >
                {identityBadge(loadedIdentity)}
              </span>
            ) : null}
          </div>
          <p>
            {profile?.username ??
              (profileLoadError ? "账号资料读取失败" : "正在读取账号信息")}
            {profile?.joined_at
              ? ` · ${formatDate(profile.joined_at)} 加入`
              : ""}
          </p>
          {loadedIdentity && !isMaster && loadedIdentity.parentDisplayName ? (
            <p className="customer-profile__parent-line">
              所属母账号：{loadedIdentity.parentDisplayName}
            </p>
          ) : null}
        </div>
        <div className="customer-profile__hero-actions">
          <button
            className="secondary-button"
            onClick={() => void logout()}
            disabled={isLoggingOut}
            type="button"
          >
            {isLoggingOut ? "正在退出" : "退出登录"}
          </button>
          <button onClick={() => onRecharge()} type="button">
            充值积分
          </button>
        </div>
      </header>

      <nav aria-label="个人中心功能" className="customer-profile__tabs">
        {(isMaster
          ? ([
              ["overview", "账号概览"],
              ["devices", "设备管理"],
              ["billing", "余额与记录"],
              // CW-062：子账号管理只对母账号出现（子账号无组织管理权）。
              ["sub-accounts", "子账号管理"],
            ] as const)
          : ([
              ["overview", "账号概览"],
              ["devices", "设备管理"],
              ["billing", "余额与记录"],
            ] as const)
        ).map(([value, label]) => (
          <button
            aria-current={tab === value ? "page" : undefined}
            className={tab === value ? "is-active" : ""}
            key={value}
            onClick={() => {
              setTab(value);
              if (value === "devices") {
                void onRefreshDevices();
              }
            }}
            type="button"
          >
            {label}
            {value === "devices" && pendingPairings.length > 0 ? (
              <span>{pendingPairings.length}</span>
            ) : null}
          </button>
        ))}
      </nav>

      {profileLoadError ? (
        <div
          aria-label={profileLoadError}
          className="settings-error"
          role="alert"
        >
          <span>{profileLoadError}</span>
          <button
            className="secondary-button"
            disabled={isRetryingProfile}
            onClick={() => void retryProfile()}
            type="button"
          >
            {isRetryingProfile ? "正在重新加载" : "重新加载账号资料"}
          </button>
        </div>
      ) : null}

      {tab === "overview" ? (
        <div className="customer-profile__overview">
          <section
            className="customer-profile__account"
            aria-labelledby="profile-title"
          >
            <div>
              <p className="eyebrow">个人资料</p>
              <h3 id="profile-title">账号信息</h3>
              <p>
                账号编号 <strong>{profile?.username ?? "正在读取…"}</strong>
              </p>
            </div>
            <form onSubmit={saveProfile}>
              <label>
                显示名称
                <input
                  maxLength={50}
                  disabled={!profile}
                  onChange={(event) => setDisplayName(event.target.value)}
                  value={displayName}
                />
              </label>
              <button disabled={isSavingProfile || !profile} type="submit">
                {isSavingProfile ? "正在保存" : "保存个人资料"}
              </button>
            </form>
          </section>

          {profileError ? (
            <p className="settings-error" role="alert">
              {profileError}
            </p>
          ) : null}
          {profileNotice ? (
            <p className="wallet-notice" role="status">
              {profileNotice}
            </p>
          ) : null}

          <div className="customer-profile__metrics">
            <article>
              <span>账号状态</span>
              <strong>正常</strong>
              <small>已登录，可正常使用</small>
            </article>
            <article>
              <span>已绑定设备</span>
              <strong>{profile?.device_slots_used ?? 0} 台</strong>
              <small>设备数量不限，支持同时在线</small>
            </article>
            <article>
              <span>待确认设备</span>
              <strong>{pendingPairings.length}</strong>
              <small>请只批准本人设备</small>
            </article>
            {isMaster && subAccountOverview !== null ? (
              <article>
                <span>子账号数</span>
                <strong>{subAccountOverview.count}</strong>
                <small>
                  {subAccountOverview.cappedCount > 0
                    ? `${subAccountOverview.cappedCount} 个已设月度额度`
                    : "均共享母账号额度"}
                </small>
              </article>
            ) : null}
            {isMaster && subAccountOverview !== null ? (
              <article>
                <span>本月子账号消费</span>
                <strong>{subAccountOverview.totalUsed} 积分</strong>
                <small>所有子账号合计</small>
              </article>
            ) : null}
            {quotaUsed !== null ? (
              <article>
                <span>本月额度</span>
                <strong>
                  {quotaCap === null
                    ? `已用 ${quotaUsed} 积分`
                    : `剩余 ${quotaRemaining} 积分`}
                </strong>
                <small>
                  {quotaSummary(quotaUsed, quotaCap, quotaProgress)}
                </small>
                {quotaForecast ? (
                  <small>
                    {`日均 ${Math.round(quotaForecast.dailyAvg)} · 预计月末用量 ${quotaForecast.projectedMonthEnd}`}
                  </small>
                ) : null}
              </article>
            ) : null}
          </div>

          {/* 激活码方案已废弃（2026-09-19），改用注册登录 + user_id 绑定。
              原「当前激活凭证」卡片与「新激活码显示区」已删除。 */}

          {/* 任务书 B1：配对审批入口已摘除（新激活方案下密码登录即用，无需审批）。
              组件文件保留，实际删除归 B2。 */}
        </div>
      ) : null}

      {tab === "devices" ? (
        <div className="customer-profile__devices">
          {deviceError ? (
            <p className="settings-error" role="alert">
              {deviceError}
            </p>
          ) : null}
          {sessionRuntime ? (
            <div className="customer-session-status">
              <HeartbeatStatus
                connectivity={sessionRuntime.connectivity}
                lastHeartbeatAt={sessionRuntime.lastHeartbeatAt}
                onRefresh={() => onManualHeartbeat?.()}
              />
              {sessionRuntime.leaseExpiresAt ? (
                <LeaseCountdown
                  expiresAt={sessionRuntime.leaseExpiresAt}
                  onRefresh={() => onManualHeartbeat?.()}
                />
              ) : null}
            </div>
          ) : null}
          {/* 任务书 B1：配对审批入口已摘除（新激活方案下密码登录即用，无需审批）。
              组件文件保留，实际删除归 B2。 */}
          {devices ? (
            <DeviceManagementPage
              devices={devices}
              isOnline={isOnline}
              leaseExpiresAt={sessionRuntime?.leaseExpiresAt ?? null}
              onPairDevice={onPairDevice}
              onRecharge={() => onRecharge()}
              onUnbind={onUnbind}
            />
          ) : (
            <p className="status-note">正在读取设备信息…</p>
          )}
        </div>
      ) : null}

      {tab === "billing" ? (
        <CustomerWalletPanel
          key={walletRefreshKey}
          onRechargeRequested={(amount, packageId) =>
            onRecharge(amount, packageId)
          }
          onSessionExpired={onSessionExpired}
          store={store}
        />
      ) : null}

      {tab === "sub-accounts" && isMaster ? (
        <SubAccountManagementPage
          now={now}
          onSessionExpired={onSessionExpired}
          store={store}
        />
      ) : null}
    </section>
  );
}

// activationStatus 已删除（激活码方案废弃，2026-09-19）

/** 子账号额度小字：无上限 → 说明；用尽 → 恢复路径；否则剩余比例 + 月末距离。 */
function quotaSummary(
  used: number,
  cap: number | null,
  progress: ShanghaiMonthProgress,
): string {
  if (cap === null) {
    return "额度不限，消费由母账号统一承担";
  }
  if (cap <= 0 || used >= cap) {
    return "额度已用尽；调高额度或等下月 1 日重置";
  }
  const remainingPercent = Math.round(((cap - used) / cap) * 100);
  return `剩余 ${remainingPercent}% · 距离月末还有 ${progress.daysLeft} 天`;
}

function identityBadge(identity: CustomerStoredIdentity | null): string {
  if (!identity) {
    return "";
  }
  return identity.accountType === "MASTER" ? "母账号" : "子账号";
}

function parseAccountType(value: string): "MASTER" | "SUB" | "SUB_ADMIN" {
  return value === "SUB" || value === "SUB_ADMIN" ? value : "MASTER";
}

function formatDate(value: string): string {
  return new Date(value).toLocaleDateString("zh-CN");
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim()
    ? error.message
    : fallback;
}
