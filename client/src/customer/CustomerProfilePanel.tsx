import { useEffect, useState } from "react";

import type { CustomerDeviceListResponse, CustomerProfile } from "../api";
import { CustomerWalletPanel } from "./CustomerWalletPanel";
import { DeviceManagementPage } from "./DeviceManagementPage";
import { HeartbeatStatus } from "./HeartbeatStatus";
import { LeaseCountdown } from "./LeaseCountdown";
import { PairingApprovalCard } from "./PairingApprovalCard";
import type {
  CustomerCredentialStore,
  CustomerLogoutOutcome,
  CustomerSessionRuntime,
} from "./useCustomerSession";

type ProfileTab = "overview" | "devices" | "billing";

export function CustomerProfilePanel({
  devices,
  deviceError,
  onApprovePairing,
  onDismissPairing,
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
}: {
  devices: CustomerDeviceListResponse | null;
  deviceError: string;
  onApprovePairing: (pairingId: string) => void;
  onDismissPairing: (pairingId: string) => void;
  onManualHeartbeat?: () => void;
  onLogout: () => Promise<CustomerLogoutOutcome>;
  /** 设备管理页"绑定第二台设备"的页内导航回调（缺省时隐藏入口）。 */
  onPairDevice?: () => void;
  onProfileUpdated: (profile: CustomerProfile) => void;
  onRefreshProfile: () => Promise<void>;
  onRecharge: (amountYuan?: number) => void;
  onRefreshDevices: () => Promise<void>;
  onSessionExpired: () => void;
  onUnbind: (deviceId: string) => void;
  onUpdateProfile: (displayName: string) => Promise<CustomerProfile>;
  profile: CustomerProfile | null;
  profileLoadError: string;
  sessionRuntime?: CustomerSessionRuntime | null;
  store: CustomerCredentialStore;
  walletRefreshKey: number;
}) {
  const [tab, setTab] = useState<ProfileTab>("overview");
  const [displayName, setDisplayName] = useState(profile?.display_name ?? "");
  const [profileError, setProfileError] = useState("");
  const [profileNotice, setProfileNotice] = useState("");
  const [isSavingProfile, setIsSavingProfile] = useState(false);
  const [isRetryingProfile, setIsRetryingProfile] = useState(false);
  const [isLoggingOut, setIsLoggingOut] = useState(false);
  // isResettingCode / replacementCode 已删除（激活码方案废弃）
  const [deferredPairingIds, setDeferredPairingIds] = useState<Set<string>>(
    () => new Set(),
  );
  const pendingPairings = devices?.pending_pairings ?? [];
  const overviewPairings = pendingPairings.filter(
    (pairing) => !deferredPairingIds.has(pairing.pairing_request_id),
  );
  const isOnline = useLeaseActive(sessionRuntime?.leaseExpiresAt ?? null);

  useEffect(() => {
    setDisplayName(profile?.display_name ?? "");
  }, [profile?.display_name]);

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

  function deferPairing(pairingId: string) {
    setDeferredPairingIds((current) => new Set(current).add(pairingId));
    setProfileNotice("已暂不处理，可稍后在设备管理中继续确认。");
    setTab("overview");
  }

  return (
    <section className="customer-profile" aria-label="个人中心">
      <header className="customer-profile__hero">
        <div>
          <p className="eyebrow">个人中心</p>
          <h2>{profile?.display_name ?? "客户账号"}</h2>
          <p>
            {profile?.username ??
              (profileLoadError ? "账号资料读取失败" : "正在读取账号信息")}
            {profile?.joined_at
              ? ` · ${formatDate(profile.joined_at)} 加入`
              : ""}
          </p>
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
            充值秒数
          </button>
        </div>
      </header>

      <nav aria-label="个人中心功能" className="customer-profile__tabs">
        {(
          [
            ["overview", "账号概览"],
            ["devices", "设备管理"],
            ["billing", "余额与记录"],
          ] as const
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
          </div>

          {/* 激活码方案已废弃（2026-09-19），改用注册登录 + user_id 绑定。
              原「当前激活凭证」卡片与「新激活码显示区」已删除。 */}

          {overviewPairings.length > 0 ? (
            <section className="customer-profile__pending">
              <h3>需要你确认</h3>
              {overviewPairings.map((pending) => (
                <PairingApprovalCard
                  key={pending.pairing_request_id}
                  onApprove={onApprovePairing}
                  onDelete={onDismissPairing}
                  onReject={() => deferPairing(pending.pairing_request_id)}
                  pairing={{
                    id: pending.pairing_request_id,
                    deviceFingerprint: `${pending.display_name} · ${pending.platform}`,
                    createdAt: pending.created_at,
                  }}
                />
              ))}
            </section>
          ) : null}
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
          {pendingPairings.map((pending) => (
            <PairingApprovalCard
              key={pending.pairing_request_id}
              onApprove={onApprovePairing}
              onDelete={onDismissPairing}
              onReject={() => deferPairing(pending.pairing_request_id)}
              pairing={{
                id: pending.pairing_request_id,
                deviceFingerprint: `${pending.display_name} · ${pending.platform}`,
                createdAt: pending.created_at,
              }}
            />
          ))}
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
          onRechargeRequested={(amount) => onRecharge(amount)}
          onSessionExpired={onSessionExpired}
          store={store}
        />
      ) : null}
    </section>
  );
}

// activationStatus 已删除（激活码方案废弃，2026-09-19）

function formatDate(value: string): string {
  return new Date(value).toLocaleDateString("zh-CN");
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim()
    ? error.message
    : fallback;
}

function useLeaseActive(expiresAt: string | null): boolean {
  const [isActive, setIsActive] = useState(() => leaseIsActive(expiresAt));

  useEffect(() => {
    const expiry = expiresAt ? Date.parse(expiresAt) : Number.NaN;
    const delayMs = expiry - Date.now();
    if (!Number.isFinite(expiry) || delayMs <= 0) {
      setIsActive(false);
      return;
    }
    setIsActive(true);
    const timer = window.setTimeout(() => setIsActive(false), delayMs + 1);
    return () => window.clearTimeout(timer);
  }, [expiresAt]);

  return isActive;
}

function leaseIsActive(expiresAt: string | null): boolean {
  if (!expiresAt) {
    return false;
  }
  const expiry = Date.parse(expiresAt);
  return Number.isFinite(expiry) && expiry > Date.now();
}
