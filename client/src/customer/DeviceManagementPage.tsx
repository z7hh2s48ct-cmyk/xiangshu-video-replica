import type { CustomerDeviceListResponse } from "../api";

export function DeviceManagementPage({
  devices,
  isOnline,
  leaseExpiresAt,
  onPairDevice,
  onUnbind,
  onRecharge,
}: {
  devices: CustomerDeviceListResponse;
  isOnline: boolean;
  leaseExpiresAt?: string | null;
  /** 页内导航到配对流程。必须是回调而非 <a href>：Tauri 桌面壳里原生
   * 导航会整页重载，配对流程状态与挂载都由上层状态机持有。
   * 缺省（内部 lane）时不渲染绑定入口。 */
  onPairDevice?: () => void;
  onUnbind: (deviceId: string) => void;
  onRecharge: () => void;
}): React.JSX.Element {
  return (
    <section className="device-management-page" aria-labelledby="device-title">
      <header className="device-management-page__header">
        <div>
          <h2 id="device-title">设备管理</h2>
          <p>设备数量不限，多台设备可以同时在线，互不影响。</p>
        </div>
        <span
          className={
            isOnline ? "device-online-state is-online" : "device-online-state"
          }
        >
          {isOnline ? "本机在线" : "本机离线"}
        </span>
      </header>

      {isOnline && leaseExpiresAt ? (
        <p className="device-session-expiry">
          本次登录有效至 {formatDate(leaseExpiresAt)}
        </p>
      ) : null}

      <div className="device-slot-grid">
        {devices.slots.map((slot) => {
          const device = slot.device;
          return (
            <article
              className={
                device?.is_current
                  ? "device-slot-card device-slot-card--current"
                  : "device-slot-card"
              }
              key={slot.slot_no}
            >
              <div className="device-slot-card__heading">
                <span>设备 {slot.slot_no}</span>
                <strong>
                  {device
                    ? device.is_current
                      ? "当前设备"
                      : "已绑定"
                    : "空闲"}
                </strong>
              </div>
              {device ? (
                <>
                  <h3>{device.display_name}</h3>
                  <dl>
                    <div>
                      <dt>系统</dt>
                      <dd>{device.platform || "未知"}</dd>
                    </div>
                    <div>
                      <dt>最近使用</dt>
                      <dd>{formatDate(device.last_active_at)}</dd>
                    </div>
                    <div>
                      <dt>绑定时间</dt>
                      <dd>{formatDate(device.bound_at)}</dd>
                    </div>
                  </dl>
                  <button
                    className="secondary-button"
                    disabled={!isOnline}
                    onClick={() => onUnbind(device.id)}
                    type="button"
                  >
                    {device.is_current ? "解绑当前设备" : "下线并解绑"}
                  </button>
                </>
              ) : (
                <div className="device-slot-card__empty">
                  <p>这个位置还没有绑定设备。</p>
                  {onPairDevice ? (
                    <button onClick={onPairDevice} type="button">
                      绑定其他设备
                    </button>
                  ) : null}
                </div>
              )}
            </article>
          );
        })}
      </div>

      <div className="device-management-page__footer">
        <p>发现陌生设备时请立即下线；解绑当前设备后，需要重新登录。</p>
        <button onClick={onRecharge} type="button">
          充值积分
        </button>
      </div>
    </section>
  );
}

function formatDate(value: string | null): string {
  if (!value) {
    return "暂无记录";
  }
  return new Date(value).toLocaleString("zh-CN");
}
