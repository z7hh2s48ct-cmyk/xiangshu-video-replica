import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  attachCustomerSessionToken,
  CustomerApiError,
  type CustomerDeviceListResponse,
  type CustomerProfile,
  customerApproveDevicePairing,
  customerDismissDevicePairing,
  customerGetProfile,
  customerListDevices,
  customerUnbindDevice,
  customerUpdateProfile,
} from "../api";
import { clearAccountResidue } from "../studio/draftResidue";
import { StudioWorkspace } from "../studio/StudioWorkspace";
import { useAppUpdate } from "./appUpdate";
import { useCustomerConfirm } from "./CustomerConfirmDialog";
import { customerToCurrentUser } from "./customerToCurrentUser";
import type {
  CustomerCredentialStore,
  CustomerLogoutOutcome,
  CustomerSessionRuntime,
  CustomerStoredIdentity,
  CustomerWorkspaceUser,
} from "./useCustomerSession";

/**
 * Customer workspace: the shared generation shell plus a session-backed
 * personal centre. Device actions still use the device credential stored in
 * the desktop vault; account and recharge data use the operator session.
 */
export function CustomerWorkspace({
  user,
  sessionRuntime = null,
  onManualHeartbeat,
  onLogout,
  store,
  onSessionExpired,
  onPairDevice,
}: {
  user: CustomerWorkspaceUser;
  sessionRuntime?: CustomerSessionRuntime | null;
  onManualHeartbeat?: () => void;
  onLogout: () => Promise<CustomerLogoutOutcome>;
  store: CustomerCredentialStore;
  /** 可选 notice 随本地过期落到终屏（P2-2：改密/退出所有设备成功后的说明）。 */
  onSessionExpired: (notice?: string) => void;
  /** 设备管理页"绑定第二台设备"的页内导航（F-01 review：不得用 <a href> 整页重载）。 */
  onPairDevice?: () => void;
}) {
  const [devices, setDevices] = useState<CustomerDeviceListResponse | null>(
    null,
  );
  const [profile, setProfile] = useState<CustomerProfile | null>(null);
  const [profileLoadError, setProfileLoadError] = useState("");
  const [deviceError, setDeviceError] = useState("");
  const [deviceLoadError, setDeviceLoadError] = useState("");
  const { confirm, dialog: confirmDialog } = useCustomerConfirm();
  // 桌面端自动更新挂在客户会话壳上：启动静默检查（每天最多一次），
  // 发现新版本弹 UpdateDialog；浏览器 lane 里 supported=false 自动失效。
  const appUpdate = useAppUpdate({ autoCheck: true });
  // CW-062：个人中心身份徽章/子账号入口的身份来源。会话用户自带首次登录
  // 时的身份；重启恢复阶段 user 可能未知（null），此时从凭据库补读缓存。
  const [storedIdentity, setStoredIdentity] =
    useState<CustomerStoredIdentity | null>(() =>
      user.accountType
        ? {
            accountType: user.accountType,
            parentUserId: user.parentUserId,
            parentDisplayName: user.parentDisplayName,
          }
        : null,
    );
  const [workspaceCredential, setWorkspaceCredential] = useState<{
    store: CustomerCredentialStore;
    user: CustomerWorkspaceUser;
    token: string;
  } | null>(null);
  const currentCredential =
    workspaceCredential?.store === store && workspaceCredential.user === user
      ? workspaceCredential
      : null;
  const profileRequestIdRef = useRef(0);
  const sessionRuntimeRef = useRef(sessionRuntime);
  sessionRuntimeRef.current = sessionRuntime;

  const loadProfile = useCallback(
    async (existingToken?: string) => {
      const requestId = ++profileRequestIdRef.current;
      setProfileLoadError("");
      let token = existingToken;
      if (!token) {
        try {
          token = (await store.loadSessionToken()) ?? undefined;
        } catch {
          if (requestId === profileRequestIdRef.current) {
            setProfileLoadError("账号资料加载失败，请稍后重试。");
          }
          return;
        }
      }
      if (requestId !== profileRequestIdRef.current) {
        return;
      }
      if (!token) {
        onSessionExpired();
        return;
      }
      try {
        const nextProfile = await customerGetProfile(
          {
            kind: "session",
            token,
          },
          {
            shouldDispatchLifecycle: () =>
              requestId === profileRequestIdRef.current,
          },
        );
        if (requestId === profileRequestIdRef.current) {
          setProfile(nextProfile);
          setProfileLoadError("");
        }
      } catch (cause) {
        if (requestId !== profileRequestIdRef.current) {
          return;
        }
        if (cause instanceof CustomerApiError && cause.status === 401) {
          onSessionExpired();
          return;
        }
        setProfileLoadError("账号资料加载失败，请稍后重试。");
      }
    },
    [store, onSessionExpired],
  );

  // Bind before child effects can issue business requests. Fast Refresh
  // replays effects while retaining state, so async rebinding leaves a gap.
  useLayoutEffect(() => {
    if (!currentCredential) return;
    return attachCustomerSessionToken(currentCredential.token);
  }, [currentCredential]);

  useEffect(() => {
    let active = true;
    void store
      .loadSessionToken()
      .then((token) => {
        if (!active) {
          return;
        }
        if (token === null) {
          onSessionExpired();
          return;
        }
        setWorkspaceCredential({ store, user, token });
        void loadProfile(token);
      })
      .catch(() => {
        if (active) {
          onSessionExpired();
        }
      });
    return () => {
      active = false;
      profileRequestIdRef.current += 1;
    };
  }, [store, user, onSessionExpired, loadProfile]);

  const loadDevices = useCallback(async () => {
    const token = await store.loadDeviceCredentialToken();
    if (token === null) {
      onSessionExpired();
      return;
    }
    try {
      const response = await customerListDevices({ kind: "device", token });
      setDevices(response);
      setDeviceLoadError("");
    } catch (cause) {
      if (cause instanceof CustomerApiError && cause.status === 401) {
        onSessionExpired();
        return;
      }
      setDeviceLoadError(
        cause instanceof Error && cause.message
          ? cause.message
          : "设备列表加载失败",
      );
    }
  }, [store, onSessionExpired]);

  // CW-062：首次登录时 user 已带身份，徽章零闪烁；身份未知（设备凭据恢复）
  // 时再读一次本地缓存（不发光网络请求，旧金库无声返回 null）。
  const loadIdentity = useCallback(() => store.loadIdentity(), [store]);
  useEffect(() => {
    if (user.accountType || storedIdentity) {
      return;
    }
    let active = true;
    void loadIdentity()
      .then((identity) => {
        if (active && identity) {
          setStoredIdentity(identity);
        }
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [loadIdentity, user.accountType, storedIdentity]);

  // Device management is not part of the personal center; load only on an explicit legacy action.

  async function handleUnbind(deviceId: string) {
    const hasActiveLease = () => {
      const leaseExpiresAt = sessionRuntimeRef.current?.leaseExpiresAt;
      const leaseExpiry = leaseExpiresAt
        ? Date.parse(leaseExpiresAt)
        : Number.NaN;
      if (Number.isFinite(leaseExpiry) && leaseExpiry > Date.now()) {
        return true;
      }
      setDeviceError("会话租约已过期，请重新登录后管理设备。");
      return false;
    };
    if (!hasActiveLease()) {
      return;
    }
    // 解绑不可撤销，走产品级确认框（audit-25 / P0 清单 #2）。
    confirm({
      title: "下线并解绑这台设备？",
      description: "这台设备解绑后需要重新激活才能继续使用。",
      level: "acknowledge",
      confirmLabel: "下线并解绑",
      onConfirm: async () => {
        // 确认框是异步的：动作前把租约与凭据再核一次，别拿旧状态去改服务端。
        // 这里的失败必须抛出去（失败留在框内）——静默 return 会被确认框当成
        // 「回调正常结束」而关掉弹窗，用户会以为解绑成功了。
        if (!hasActiveLease()) {
          throw new Error("会话租约已过期，请重新登录后再解绑设备。");
        }
        const token = await store.loadDeviceCredentialToken();
        if (!hasActiveLease()) {
          throw new Error("会话租约已过期，请重新登录后再解绑设备。");
        }
        if (token === null) {
          onSessionExpired();
          return;
        }
        try {
          await customerUnbindDevice({ kind: "device", token }, deviceId, {
            idempotencyKey: crypto.randomUUID(),
          });
          setDeviceError("");
          await loadDevices();
        } catch (cause) {
          if (cause instanceof CustomerApiError && cause.status === 401) {
            onSessionExpired();
            return;
          }
          throw cause instanceof Error && cause.message
            ? cause
            : new Error("解绑设备失败");
        }
      },
    });
  }

  async function handleApprovePairing(pairingId: string) {
    const token = await store.loadDeviceCredentialToken();
    if (token === null) {
      onSessionExpired();
      return;
    }
    try {
      await customerApproveDevicePairing({ kind: "device", token }, pairingId);
      setDeviceError("");
      await loadDevices();
    } catch (cause) {
      if (cause instanceof CustomerApiError && cause.status === 401) {
        onSessionExpired();
        return;
      }
      setDeviceError(
        cause instanceof Error && cause.message
          ? cause.message
          : "审批配对失败",
      );
    }
  }

  async function handleDismissPairing(pairingId: string) {
    confirm({
      title: "删除这个无效的设备绑定请求？",
      description: "被删除的请求不会被批准，对方需要重新发起配对。",
      level: "standard",
      confirmLabel: "删除请求",
      onConfirm: async () => {
        const token = await store.loadDeviceCredentialToken();
        if (token === null) {
          onSessionExpired();
          return;
        }
        try {
          await customerDismissDevicePairing(
            { kind: "device", token },
            pairingId,
          );
          setDeviceError("");
          await loadDevices();
        } catch (cause) {
          if (cause instanceof CustomerApiError && cause.status === 401) {
            onSessionExpired();
            return;
          }
          throw cause instanceof Error && cause.message
            ? cause
            : new Error("删除设备绑定请求失败");
        }
      },
    });
  }

  async function handleUpdateProfile(
    displayName: string,
  ): Promise<CustomerProfile> {
    const token = await store.loadSessionToken();
    if (token === null) {
      onSessionExpired();
      throw new Error("登录已失效，请重新进入工作台。");
    }
    try {
      return await customerUpdateProfile(
        { kind: "session", token },
        displayName,
      );
    } catch (cause) {
      if (cause instanceof CustomerApiError && cause.status === 401) {
        onSessionExpired();
      }
      throw cause;
    }
  }

  // handleResetActivationCode 已删除（激活码方案废弃，2026-09-19）

  /** 登出先清本账号的本地创作残留与页间缓存：进程不重启就换账号时，
   * 模块级缓存和 localStorage 都会原样串进下一个人的工作区。
   * 清理是本地的、失败也不影响登出本身，所以放在等待服务端释放之前。 */
  async function handleLogout(): Promise<CustomerLogoutOutcome> {
    clearAccountResidue(user.userId);
    return onLogout();
  }

  return (
    <div className="customer-workspace">
      {confirmDialog}
      {appUpdate.dialog}
      {currentCredential ? (
        <StudioWorkspace
          currentUser={customerToCurrentUser(user, profile)}
          customerAccount={{
            devices,
            deviceError: deviceError || deviceLoadError,
            onApprovePairing: (pairingId) =>
              void handleApprovePairing(pairingId),
            onDismissPairing: (pairingId) =>
              void handleDismissPairing(pairingId),
            onProfileUpdated: setProfile,
            onRefreshProfile: () => loadProfile(),
            onLogout: handleLogout,
            onRefreshDevices: loadDevices,
            onUnbind: (deviceId) => void handleUnbind(deviceId),
            onUpdateProfile: handleUpdateProfile,
            profile,
            profileLoadError,
            store,
            onSessionExpired,
            identity: storedIdentity,
            loadIdentity,
            sessionRuntime,
            onManualHeartbeat,
            onPairDevice,
          }}
        />
      ) : (
        <main className="centered-shell" aria-live="polite">
          <p className="login-hint">正在进入工作区…</p>
        </main>
      )}
    </div>
  );
}
