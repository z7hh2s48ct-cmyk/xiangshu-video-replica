import { useCallback, useEffect, useRef, useState } from "react";

import {
  AdminDeviceError,
  type DeviceListItem,
  listDevices,
  revokeDeviceCredential,
  unbindDevice,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";

// 任务书 C（2026-09-18）第 2 项：客户详情设备视图。
// 只接 unbind 与 revoke-credential —— approve/replace-device 属旧配对审批链，
// 随任务书 B 退役，这里刻意不提供入口。
const DEVICE_LIMIT = 50;

type DeviceActionKind = "unbind" | "revoke";

type PendingDeviceAction = {
  kind: DeviceActionKind;
  device: DeviceListItem;
};

const ACTION_COPY: Record<
  DeviceActionKind,
  {
    title: string;
    confirmLabel: string;
    level: "reason" | "reasonAndAck";
    description: (device: DeviceListItem) => string;
  }
> = {
  unbind: {
    title: "解绑设备",
    confirmLabel: "解绑并踢会话",
    // 解绑会结束该设备在线会话，但凭据仍可用；属中危，要求原因但不要求勾选。
    level: "reason",
    description: (device) =>
      `解绑后 ${device.device_id} 的在线会话会被立即结束；该设备重新登录需重新绑定。`,
  },
  revoke: {
    title: "吊销设备凭据",
    confirmLabel: "吊销凭据",
    // 吊销是终态不可逆（后端 REVOKED），比解绑更强，故要走 reasonAndAck。
    level: "reasonAndAck",
    description: (device) =>
      `吊销后 ${device.device_id} 的凭据永久失效且不可恢复（后端置为 REVOKED 终态）。`,
  },
};

function platformLabel(platform: string) {
  const labels: Record<string, string> = {
    windows: "Windows",
    macos: "macOS",
    linux: "Linux",
    android: "Android",
    ios: "iOS",
  };
  return labels[platform.toLowerCase()] ?? platform;
}

function formatDateTime(value: string | null | undefined) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("zh-CN", { hour12: false });
}

export function CustomerDeviceSection({
  userId,
  readOnly,
}: {
  userId: string;
  readOnly: boolean;
}) {
  const [items, setItems] = useState<DeviceListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState<PendingDeviceAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const contextIdRef = useRef(0);
  const requestIdRef = useRef(0);

  const load = useCallback(async () => {
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    setLoading(true);
    setError("");
    try {
      const response = await listDevices({
        status: "BOUND",
        userId,
        limit: DEVICE_LIMIT,
      });
      if (requestId !== requestIdRef.current) return;
      setItems(response.items);
      setTotal(response.total);
    } catch (cause) {
      if (requestId !== requestIdRef.current) return;
      setError(
        cause instanceof Error && cause.message
          ? `加载设备失败：${cause.message}`
          : "加载设备失败：未知错误",
      );
    } finally {
      if (requestId === requestIdRef.current) {
        setLoading(false);
      }
    }
  }, [userId]);

  useEffect(() => {
    // 换客户即换上下文：作废在途请求与未提交的确认，避免把 A 的操作落到 B。
    contextIdRef.current += 1;
    requestIdRef.current += 1;
    setItems([]);
    setTotal(0);
    setError("");
    setNotice("");
    setPending(null);
    setDialogError("");
    setBusy(false);
    void load();
  }, [load]);

  function beginAction(kind: DeviceActionKind, device: DeviceListItem) {
    setPending({ kind, device });
    setDialogError("");
  }

  async function submitAction(reason: string) {
    if (!pending || busy) return;
    const actionContextId = contextIdRef.current;
    const { kind, device } = pending;
    setBusy(true);
    setDialogError("");
    try {
      const result =
        kind === "unbind"
          ? await unbindDevice(device.device_id, reason)
          : await revokeDeviceCredential(device.device_id, reason);
      if (contextIdRef.current !== actionContextId) return;
      setNotice(
        `${kind === "unbind" ? "已解绑并踢出会话" : "已吊销凭据"}：${result.device_id}`,
      );
      setPending(null);
      await load();
    } catch (cause) {
      if (contextIdRef.current !== actionContextId) return;
      setDialogError(
        cause instanceof Error && cause.message
          ? cause.message
          : "设备操作失败：未知错误",
      );
      // 幂等键由 adminWrite 每次生成；失败后重试是新的一次尝试，
      // 无需在上层保留键（与 SessionsPage 的显式键不同，设备封装不暴露键参数）。
      if (cause instanceof AdminDeviceError && cause.status === undefined) {
        setPending(null);
      }
    } finally {
      if (contextIdRef.current === actionContextId) {
        setBusy(false);
      }
    }
  }

  const copy = pending ? ACTION_COPY[pending.kind] : null;

  return (
    <section aria-label="客户设备" className="admin-panel customer-devices">
      <header className="customer-devices__header">
        <h2>设备</h2>
        <small>
          {loading ? "加载中…" : `已绑定 ${total} 台`}
          {readOnly ? "（只读角色）" : ""}
        </small>
      </header>

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

      {!loading && items.length === 0 && !error ? (
        <p className="admin-hint">该客户当前没有已绑定设备。</p>
      ) : null}

      {items.length > 0 ? (
        <div className="admin-table-scroll">
          <table className="admin-data-table">
            <thead>
              <tr>
                <th scope="col">设备</th>
                <th scope="col">平台</th>
                <th scope="col">槽位</th>
                <th scope="col">绑定时间</th>
                <th scope="col">最后心跳</th>
                {!readOnly ? <th scope="col">操作</th> : null}
              </tr>
            </thead>
            <tbody>
              {items.map((device) => (
                <tr key={device.device_id}>
                  <td>
                    <span>{device.display_name || device.device_id}</span>
                    {device.display_name ? (
                      <small>{device.device_id}</small>
                    ) : null}
                  </td>
                  <td>{platformLabel(device.platform)}</td>
                  <td>{device.slot_no}</td>
                  <td>{formatDateTime(device.bound_at)}</td>
                  <td>
                    {device.online
                      ? "在线"
                      : formatDateTime(device.last_heartbeat_at)}
                  </td>
                  {!readOnly ? (
                    <td className="customer-devices__actions">
                      <button
                        aria-label={`解绑设备 ${device.device_id}`}
                        className="btn-secondary"
                        type="button"
                        onClick={() => beginAction("unbind", device)}
                      >
                        解绑
                      </button>
                      <button
                        aria-label={`吊销设备凭据 ${device.device_id}`}
                        className="customer-devices__revoke"
                        type="button"
                        onClick={() => beginAction("revoke", device)}
                      >
                        吊销凭据
                      </button>
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      <ConfirmDialog
        busy={busy}
        confirmLabel={copy?.confirmLabel ?? "确认执行"}
        description={
          pending && copy ? copy.description(pending.device) : undefined
        }
        error={dialogError}
        level={copy?.level ?? "reason"}
        open={pending !== null && copy !== null && !readOnly}
        title={copy?.title ?? "设备操作"}
        onClose={() => {
          if (busy) return;
          setPending(null);
          setDialogError("");
        }}
        onConfirm={(reason) => void submitAction(reason)}
      />
    </section>
  );
}
