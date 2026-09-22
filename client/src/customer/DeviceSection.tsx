import { useEffect, useRef, useState } from "react";
import type { CustomerDeviceListResponse } from "../api";
import { DeviceManagementPage } from "./DeviceManagementPage";
import { HeartbeatStatus } from "./HeartbeatStatus";
import { LeaseCountdown } from "./LeaseCountdown";
import { PairingApprovalCard } from "./PairingApprovalCard";
import type { CustomerSessionRuntime } from "./useCustomerSession";
import { useLeaseActive } from "./useLeaseActive";

/**
 * 个人中心的「设备管理」（审计方案 A：桌面端专属页签）。
 *
 * 从**已下线**的旧面板 `CustomerProfilePanel` 移植而来，承接旧面板独占的三件事：
 * 设备槽位管理、配对审批、心跳/租约健康。移植而不是重写，是因为这三块的行为
 * （含「暂不处理」只藏不销、租约到期自动翻面）已经在旧面板上被验证过，重写只会
 * 引入新的差异。
 *
 * 与旧面板的两处差别：
 * - 一并展示 `devices.history`（旧面板只渲染 2 个槽位，解绑过的设备在界面上
 *   无迹可寻，而服务端一直返回这段历史）；
 * - 「暂不处理」的请求不再是单向消失：给出「显示已暂缓」开关，避免用户误触后
 *   再也找不到那条请求。
 */
/** 「暂不处理」的配对请求存哪里：按账号分开，免得切换账号时把别人的暂缓读进来。 */
const DEFERRED_STORAGE_PREFIX = "uc:deferred-pairings:";

function readDeferredPairings(scope: string): ReadonlySet<string> {
  try {
    const raw = window.localStorage.getItem(DEFERRED_STORAGE_PREFIX + scope);
    if (!raw) {
      return new Set();
    }
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed)
      ? new Set(
          parsed.filter((item): item is string => typeof item === "string"),
        )
      : new Set();
  } catch {
    // 隐私模式/禁用存储：退化成「本次会话不记住」，不影响别的功能。
    return new Set();
  }
}

function writeDeferredPairings(scope: string, ids: ReadonlySet<string>): void {
  try {
    window.localStorage.setItem(
      DEFERRED_STORAGE_PREFIX + scope,
      JSON.stringify([...ids]),
    );
  } catch {
    // 写不进去就只在本次会话内有效。
  }
}

export function DeviceSection({
  devices,
  deviceError,
  sessionRuntime,
  scope,
  onApprovePairing,
  onDismissPairing,
  onManualHeartbeat,
  onPairDevice,
  onRecharge,
  onRefreshDevices,
  onUnbind,
}: {
  devices: CustomerDeviceListResponse | null;
  deviceError: string;
  /** 暂缓状态的归属键（一般是当前账号 id）：换账号不会读到别人的暂缓。 */
  scope: string;
  sessionRuntime?: CustomerSessionRuntime | null;
  onApprovePairing: (pairingId: string) => void;
  onDismissPairing: (pairingId: string) => void;
  onManualHeartbeat?: () => void;
  onPairDevice?: () => void;
  onRecharge: () => void;
  onRefreshDevices?: () => Promise<void>;
  onUnbind: (deviceId: string) => void;
}): React.JSX.Element {
  // 「暂不处理」只藏不销，并按账号记住（P2#22：此前只活在组件 state 里，
  // 刷新或切页签就丢，用户会以为那条请求消失了）。
  const [deferredPairingIds, setDeferredPairingIds] = useState<
    ReadonlySet<string>
  >(() => readDeferredPairings(scope));
  /**
   * 当前 state 里的集合**属于哪个 scope**。
   *
   * scope 取自 `account.profile?.user_id ?? "anonymous"`：profile 还没到时组件以
   * `anonymous` 挂载，随后换成真实 user_id。若写入 effect 直接依赖 scope，这一次
   * 切换会把匿名（空）集合写到该账号的键上——用户此前「暂不处理」的记录被清空、
   * 那些请求又冒出来；反向切换时则会把上一个账号的暂缓写进另一个账号。
   * 所以 scope 变化时**先重读、不写**，只在 scope 与 state 归属一致时才落盘。
   */
  const deferredScope = useRef(scope);
  useEffect(() => {
    if (deferredScope.current === scope) {
      writeDeferredPairings(scope, deferredPairingIds);
      return;
    }
    deferredScope.current = scope;
    setDeferredPairingIds(readDeferredPairings(scope));
  }, [scope, deferredPairingIds]);
  const [showDeferred, setShowDeferred] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);

  const pendingPairings = devices?.pending_pairings ?? [];
  const deferred = pendingPairings.filter((pairing) =>
    deferredPairingIds.has(pairing.pairing_request_id),
  );
  const visiblePairings = showDeferred
    ? [...pendingPairings]
    : pendingPairings.filter(
        (pairing) => !deferredPairingIds.has(pairing.pairing_request_id),
      );
  const history = devices?.history ?? [];
  const isOnline = useLeaseActive(sessionRuntime?.leaseExpiresAt ?? null);

  // 工作区只在「明确动作」时拉设备（旧的个人中心不展示设备），所以第一次进入本
  // 页签时自己补一次读取；失败不重试（错误由上层写进 deviceError，另有刷新按钮）。
  const autoLoadDone = useRef(false);
  useEffect(() => {
    if (autoLoadDone.current || devices || !onRefreshDevices) {
      return;
    }
    autoLoadDone.current = true;
    void onRefreshDevices().catch(() => {});
  }, [devices, onRefreshDevices]);

  async function refresh() {
    if (!onRefreshDevices || isRefreshing) {
      return;
    }
    setIsRefreshing(true);
    try {
      await onRefreshDevices();
    } catch {
      // 刷新失败不改页面：错误由上层写入 deviceError 并显示在下面那行。
    } finally {
      setIsRefreshing(false);
    }
  }

  return (
    <div className="uc-devices">
      {deviceError ? (
        <p className="uc-error" role="alert">
          {deviceError}
        </p>
      ) : null}

      {sessionRuntime ? (
        <section className="uc-card uc-devices__health">
          <h2>本机连接</h2>
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
        </section>
      ) : null}

      {pendingPairings.length > 0 ? (
        <section className="uc-card uc-devices__pending">
          <h2>需要你确认</h2>
          {visiblePairings.length === 0 ? (
            <p>已全部暂缓处理，没有待确认的绑定请求。</p>
          ) : (
            visiblePairings.map((pending) => (
              <PairingApprovalCard
                key={pending.pairing_request_id}
                onApprove={onApprovePairing}
                onDelete={onDismissPairing}
                onReject={() =>
                  setDeferredPairingIds((current) => {
                    const next = new Set(current);
                    next.add(pending.pairing_request_id);
                    return next;
                  })
                }
                pairing={{
                  id: pending.pairing_request_id,
                  deviceFingerprint: `${pending.display_name} · ${pending.platform}`,
                  createdAt: pending.created_at,
                }}
              />
            ))
          )}
          {deferred.length > 0 ? (
            <button
              className="uc-devices__deferred-toggle"
              onClick={() => setShowDeferred((value) => !value)}
              type="button"
            >
              {showDeferred
                ? `收起已暂缓的 ${deferred.length} 条`
                : `显示已暂缓的 ${deferred.length} 条`}
            </button>
          ) : null}
        </section>
      ) : null}

      <section className="uc-card uc-devices__slots">
        <header className="uc-devices__slots-head">
          <h2>设备槽位</h2>
          {onRefreshDevices ? (
            <button disabled={isRefreshing} onClick={refresh} type="button">
              {isRefreshing ? "正在刷新…" : "刷新"}
            </button>
          ) : null}
        </header>
        {devices ? (
          <DeviceManagementPage
            devices={devices}
            isOnline={isOnline}
            leaseExpiresAt={sessionRuntime?.leaseExpiresAt ?? null}
            onPairDevice={onPairDevice}
            onRecharge={onRecharge}
            onUnbind={onUnbind}
          />
        ) : (
          <p className="status-note">正在读取设备信息…</p>
        )}
      </section>

      {history.length > 0 ? (
        <section className="uc-card uc-devices__history">
          <h2>已解绑设备</h2>
          <ul>
            {history.map((device) => (
              <li key={device.id}>
                <span>{device.display_name}</span>
                <span>{device.platform}</span>
                <span>
                  {device.unbound_at
                    ? `解绑于 ${device.unbound_at}`
                    : device.revoked_at
                      ? `撤销于 ${device.revoked_at}`
                      : "已停用"}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
