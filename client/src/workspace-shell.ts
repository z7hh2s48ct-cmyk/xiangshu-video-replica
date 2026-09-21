import type {
  CurrentUser,
  CustomerDeviceListResponse,
  CustomerProfile,
} from "./api";
import type {
  CustomerCredentialStore,
  CustomerLogoutOutcome,
  CustomerSessionRuntime,
  CustomerStoredIdentity,
} from "./customer/useCustomerSession";

/**
 * 客户工作台外壳（WorkspaceShell）的 props 契约类型。
 *
 * CW-012：此形状原为 `App.tsx` 中 `WorkspaceShell` 的内联匿名 props 类型。
 * `studio/StudioWorkspace` 与 `studio/LiveWorkspacePanel` 需经
 * `ComponentProps<typeof WorkspaceShell>` 从旧入口 `App.tsx` 反向派生该类型，
 * 而 `App.tsx` 又运行时 import 这两个 studio 组件，构成 `App ↔ studio` 的
 * `import type` 环。抽出到本叶子模块后，studio 与 App 都只依赖此公共契约，
 * 环被打断；`WorkspaceShell` 组件本体仍留在 `App.tsx`，仅以本类型注解其 props。
 *
 * 纯类型叶子（无运行时代码）：仅依赖 `./api` 与 `./customer/useCustomerSession`
 * 的类型，二者均不回指本模块，故不引入任何 import 环。
 */
export type WorkspaceShellProps = {
  currentUser: CurrentUser;
  customerAccount?: {
    devices: CustomerDeviceListResponse | null;
    deviceError: string;
    onApprovePairing: (pairingId: string) => void;
    onDismissPairing: (pairingId: string) => void;
    onProfileUpdated: (profile: CustomerProfile) => void;
    onRefreshProfile: () => Promise<void>;
    onLogout: () => Promise<CustomerLogoutOutcome>;
    onRefreshDevices: () => Promise<void>;
    // onResetActivationCode 已删除（激活码方案废弃，2026-09-19）
    onUnbind: (deviceId: string) => void;
    onUpdateProfile: (displayName: string) => Promise<CustomerProfile>;
    profile: CustomerProfile | null;
    profileLoadError: string;
    store: CustomerCredentialStore;
    onSessionExpired: () => void;
    /** CW-062：会话身份（母账号 / 子账号 + 所属母账号），供个人中心身份
     * 徽章与子账号管理入口判定；设备凭据恢复时可能为 null（身份未知）。 */
    identity?: CustomerStoredIdentity | null;
    /** CW-062：再次读取本地缓存身份（挂载时同步读，不触发网络）。 */
    loadIdentity?: () => Promise<CustomerStoredIdentity | null>;
    /** Live heartbeat/lease health from the customer session hook; absent
     * on the internal lane. Rendered in the profile centre's device tab. */
    sessionRuntime?: CustomerSessionRuntime | null;
    onManualHeartbeat?: () => void;
    /** 设备管理页"绑定第二台设备"的页内导航（F-01 review：客户 lane 必传，
     * 内部 lane 无配对流程故缺省）。 */
    onPairDevice?: () => void;
  };
  customerWallet?: {
    store: CustomerCredentialStore;
    onSessionExpired: () => void;
  };
};
