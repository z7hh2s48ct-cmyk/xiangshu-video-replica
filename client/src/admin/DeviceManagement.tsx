import "./device-management.css";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AdminDeviceError,
  type DeviceListItem,
  type DeviceListOptions,
  type DeviceListResponse,
  listDevices,
  revokeCustomerSession,
  revokeDeviceCredential,
  unbindDevice,
} from "../api.admin";
import { CustomerLink } from "./CustomerLink";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import {
  deviceStatusLabel,
  formatDateTime,
  formatRelativeTime,
  platformLabel,
} from "./ui/vocabulary";

const LIMIT = 50;
type Action = "logout" | "unbind" | "revoke";
const actionLabels: Record<Action, string> = {
  logout: "下线",
  unbind: "解绑设备",
  revoke: "永久禁用该设备",
};
const deviceName = (item: DeviceListItem) => item.display_name || "未命名设备";

export function DeviceManagement({
  userId,
  readOnly = false,
  global = false,
  onCustomerChange,
}: {
  userId?: string;
  readOnly?: boolean;
  global?: boolean;
  onCustomerChange?: (id: string | undefined) => void;
}) {
  const [scope, setScope] = useState<DeviceListOptions>({ userId, offset: 0 });
  const [keyword, setKeyword] = useState("");
  const [page, setPage] = useState<DeviceListResponse>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState<{
    action: Action;
    item: DeviceListItem;
  }>();
  const [busy, setBusy] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const [writeKey, setWriteKey] = useState<string>();
  const request = useRef(0);
  const context = useRef(0);

  const load = useCallback(async () => {
    const id = ++request.current;
    setLoading(true);
    setError("");
    try {
      const result = await listDevices({ ...scope, limit: LIMIT });
      if (id === request.current) {
        if ((scope.offset ?? 0) > 0 && (scope.offset ?? 0) >= result.total) {
          setScope((current) => ({
            ...current,
            offset: Math.max(0, Math.ceil(result.total / LIMIT) - 1) * LIMIT,
          }));
        } else setPage(result);
      }
    } catch (cause) {
      if (id === request.current) {
        setPage(undefined);
        setError(cause instanceof Error ? cause.message : "读取设备失败");
      }
    } finally {
      if (id === request.current) setLoading(false);
    }
  }, [scope]);

  useEffect(() => {
    void load();
    return () => {
      request.current += 1;
      context.current += 1;
    };
  }, [load]);

  function changeScope(next: DeviceListOptions) {
    context.current += 1;
    request.current += 1;
    setPage(undefined);
    setPending(undefined);
    setBusy(false);
    setWriteKey(undefined);
    setDialogError("");
    setNotice("");
    setScope(next);
  }

  function selectCustomer(id: string | undefined) {
    if (onCustomerChange) onCustomerChange(id);
    else changeScope({ ...scope, userId: id, offset: 0 });
  }

  async function submit(reason: string) {
    if (!pending || busy) return;
    const current = context.current;
    const key = writeKey ?? crypto.randomUUID();
    setWriteKey(key);
    setBusy(true);
    setDialogError("");
    try {
      const { item, action } = pending;
      if (action === "logout") {
        if (!item.session_id || item.session_epoch == null) {
          throw new Error("在线状态已变化，请刷新设备后重试。");
        }
        await revokeCustomerSession(
          item.session_id,
          item.session_epoch,
          reason,
          key,
        );
      } else if (action === "unbind")
        await unbindDevice(item.device_id, reason, key);
      else await revokeDeviceCredential(item.device_id, reason, key);
      if (current !== context.current) return;
      setNotice(`${deviceName(item)}：${actionLabels[action]}已完成。`);
      setPending(undefined);
      setWriteKey(undefined);
      await load();
    } catch (cause) {
      if (current !== context.current) return;
      setDialogError(cause instanceof Error ? cause.message : "设备操作失败");
      // 网络中断不能证明写入失败，重试复用同一键以回放首次结果。
      if (cause instanceof AdminDeviceError && cause.status !== undefined)
        setWriteKey(undefined);
    } finally {
      if (current === context.current) setBusy(false);
    }
  }

  function begin(action: Action, item: DeviceListItem) {
    setPending({ action, item });
    setDialogError("");
    setWriteKey(undefined);
  }

  const cards = [
    { key: "online_customers", attention: "online", label: "当前在线客户" },
    {
      key: "at_slot_limit",
      attention: "at_slot_limit",
      label: "设备数达上限的客户",
    },
    {
      key: "frequent_swaps_24h",
      attention: "frequent_swaps_24h",
      label: "近24小时频繁换设备的客户",
    },
  ] as const;
  return (
    <section
      className="admin-panel customer-devices"
      aria-label={global ? "登录与设备" : "客户设备"}
    >
      <header className="customer-devices__header">
        <h2>{global ? "登录与设备" : "设备"}</h2>
        <small>
          {loading
            ? "加载中…"
            : `共 ${page?.total ?? "待核对"} 台设备（含历史）`}
          {readOnly ? "（只读角色）" : ""}
        </small>
      </header>
      {global && (
        <div className="device-summary-cards">
          {cards.map((card) => (
            <button
              key={card.key}
              type="button"
              disabled={loading || page?.summary?.[card.key] == null}
              aria-pressed={scope.attention === card.attention}
              onClick={() =>
                changeScope({
                  ...scope,
                  status: undefined,
                  attention: card.attention,
                  offset: 0,
                })
              }
            >
              <span>{card.label}</span>
              <strong>{page?.summary?.[card.key] ?? "待核对"} 位</strong>
            </button>
          ))}
        </div>
      )}
      <form
        className="device-filters"
        onSubmit={(event) => {
          event.preventDefault();
          changeScope({
            ...scope,
            keyword: keyword.trim() || undefined,
            offset: 0,
          });
        }}
      >
        {global && (
          <label>
            公司名或用户名
            <input
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
            />
          </label>
        )}
        <label>
          系统
          <select
            value={scope.platform ?? ""}
            onChange={(event) =>
              changeScope({
                ...scope,
                platform: event.target.value || undefined,
                offset: 0,
              })
            }
          >
            <option value="">全部系统</option>
            {["windows", "macos", "linux", "ios", "android"].map((platform) => (
              <option key={platform} value={platform}>
                {platformLabel(platform)}
              </option>
            ))}
          </select>
        </label>
        <label>
          设备状态
          <select
            value={scope.status ?? ""}
            onChange={(event) =>
              changeScope({
                ...scope,
                status: event.target.value || undefined,
                offset: 0,
              })
            }
          >
            <option value="">全部设备（含历史）</option>
            {["BOUND", "UNBOUND", "REVOKED"].map((status) => (
              <option key={status} value={status}>
                {deviceStatusLabel(status)}
              </option>
            ))}
          </select>
        </label>
        <label>
          关注范围
          <select
            value={scope.attention ?? ""}
            onChange={(event) =>
              changeScope({
                ...scope,
                attention: (event.target.value ||
                  undefined) as DeviceListOptions["attention"],
                offset: 0,
              })
            }
          >
            <option value="">全部</option>
            <option value="online">在线</option>
            <option value="offline">离线</option>
            <option value="at_slot_limit">设备数达上限</option>
            <option value="frequent_swaps_24h">近24小时频繁换设备</option>
          </select>
        </label>
        {global && <button type="submit">搜索</button>}
        <button type="button" disabled={loading} onClick={() => void load()}>
          刷新
        </button>
        {global && scope.userId && (
          <button type="button" onClick={() => selectCustomer(undefined)}>
            查看全部设备
          </button>
        )}
      </form>
      {global && (
        <p className="admin-hint">
          卡片按当前公司、客户和系统范围统计去重客户，独立于分页和关注筛选；频繁换设备指近24小时内至少两台设备绑定后被解绑或禁用。
        </p>
      )}
      {error && <PageBanner tone="error">{error}</PageBanner>}
      {notice && <PageBanner tone="notice">{notice}</PageBanner>}
      {loading && <p role="status">正在读取设备…</p>}
      {!loading && page?.items.length === 0 && (
        <p className="admin-hint">该范围内没有设备记录。</p>
      )}
      {page && page.items.length > 0 && (
        <div className="admin-table-scroll">
          <table className="admin-data-table" aria-label="设备列表">
            <thead>
              <tr>
                {global && <th>客户</th>}
                <th>设备</th>
                <th>系统</th>
                <th>首次绑定</th>
                <th>最近活动</th>
                <th>状态</th>
                {!readOnly && <th>操作</th>}
                <th>详情</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((item) => (
                <tr key={item.device_id}>
                  {global && (
                    <td>
                      <CustomerLink
                        userId={item.user_id}
                        company={item.company_name}
                        username={item.username ?? "未填写"}
                      />
                      <button
                        type="button"
                        onClick={() => selectCustomer(item.user_id)}
                      >
                        选择客户
                      </button>
                    </td>
                  )}
                  <td>{deviceName(item)}</td>
                  <td>{platformLabel(item.platform)}</td>
                  <td>
                    {formatDateTime(item.first_bound_at ?? item.bound_at)}
                  </td>
                  <td>
                    <span
                      title={formatDateTime(
                        item.last_active_at ?? item.last_heartbeat_at,
                      )}
                    >
                      {formatRelativeTime(
                        item.last_active_at ?? item.last_heartbeat_at,
                      )}
                    </span>
                  </td>
                  <td>
                    {item.online ? "在线" : "离线"}
                    {item.status !== "BOUND" && (
                      <small>{deviceStatusLabel(item.status)}</small>
                    )}
                  </td>
                  {!readOnly && (
                    <td className="customer-devices__actions">
                      <button
                        type="button"
                        disabled={
                          !item.online ||
                          !item.session_id ||
                          item.session_epoch == null
                        }
                        onClick={() => begin("logout", item)}
                      >
                        下线
                      </button>
                      <button
                        type="button"
                        aria-label={`解绑设备 ${item.device_id}`}
                        disabled={item.status !== "BOUND"}
                        onClick={() => begin("unbind", item)}
                      >
                        解绑
                      </button>
                      <button
                        type="button"
                        aria-label={`永久禁用设备 ${item.device_id}`}
                        disabled={item.status === "REVOKED"}
                        onClick={() => begin("revoke", item)}
                      >
                        永久禁用
                      </button>
                    </td>
                  )}
                  <td>
                    <details>
                      <summary>技术详情</summary>
                      <dl>
                        <dt>设备编号</dt>
                        <dd>{item.device_id}</dd>
                        <dt>客户编号</dt>
                        <dd>{item.user_id}</dd>
                        <dt>最近绑定</dt>
                        <dd>{formatDateTime(item.bound_at)}</dd>
                        <dt>解绑时间</dt>
                        <dd>{formatDateTime(item.unbound_at)}</dd>
                        <dt>禁用时间</dt>
                        <dd>{formatDateTime(item.revoked_at)}</dd>
                        {item.session_epoch != null && (
                          <>
                            <dt>会话版本</dt>
                            <dd>{item.session_epoch}</dd>
                          </>
                        )}
                      </dl>
                    </details>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {page && (
        <Pagination
          offset={scope.offset ?? 0}
          limit={LIMIT}
          total={page.total}
          noun="台"
          disabled={loading}
          onPageChange={(offset) => changeScope({ ...scope, offset })}
        />
      )}
      <ConfirmDialog
        open={!!pending && !readOnly}
        busy={busy}
        error={dialogError}
        title={pending ? actionLabels[pending.action] : "设备操作"}
        confirmLabel={
          pending?.action === "revoke"
            ? "永久禁用"
            : pending?.action === "unbind"
              ? "解绑并结束会话"
              : "确认下线"
        }
        level={pending?.action === "revoke" ? "reasonAndAck" : "reason"}
        description={
          pending
            ? `${deviceName(pending.item)}：${pending.action === "revoke" ? "永久禁用后不能恢复，在线会话也会立即结束。" : pending.action === "unbind" ? "解绑会结束在线会话，重新登录需再次绑定。" : "结束当前登录，设备仍可重新登录。"}`
            : undefined
        }
        onClose={() => {
          if (!busy) {
            setPending(undefined);
            setWriteKey(undefined);
            setDialogError("");
          }
        }}
        onConfirm={(reason) => void submit(reason)}
      />
    </section>
  );
}
