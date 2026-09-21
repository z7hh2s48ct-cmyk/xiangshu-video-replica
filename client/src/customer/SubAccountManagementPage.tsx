import { type FormEvent, useCallback, useEffect, useState } from "react";
import {
  CustomerApiError,
  type CustomerSubAccount,
  customerCreateSubAccount,
  customerDeleteSubAccount,
  customerListSubAccounts,
  customerSetSubAccountPassword,
  customerUpdateSubAccount,
} from "../api";
import type { CustomerCredentialStore } from "./useCustomerSession";
import "./customer-subaccounts.css";

/**
 * CW-062 桌面端自助子账号管理（母账号限定）。
 *
 * 与旧「用户中心」/admin lane 不同，本页只对自己机构的子账号负责：所有
 * 请求都走客户会话围栏（`/api/customer/sub-accounts`，子账号访问得到 403），
 * 行级作用域由服务端 `parent_user_id = caller` 保证，前端不做乐观假设。
 * 会话过期（401）统一交给 `onSessionExpired` 收敛到登录页。
 */
export function SubAccountManagementPage({
  store,
  onSessionExpired,
}: {
  store: CustomerCredentialStore;
  onSessionExpired: () => void;
}) {
  const [subAccounts, setSubAccounts] = useState<CustomerSubAccount[] | null>(
    null,
  );
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isCreating, setIsCreating] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDisplayName, setEditDisplayName] = useState("");
  const [form, setForm] = useState({
    username: "",
    display_name: "",
    password: "",
  });

  const loadSession = useCallback(async () => {
    const token = await store.loadSessionToken();
    if (token === null) {
      onSessionExpired();
      return null;
    }
    return { kind: "session" as const, token };
  }, [store, onSessionExpired]);

  const reload = useCallback(async () => {
    setError("");
    try {
      const credential = await loadSession();
      if (credential === null) {
        return;
      }
      setSubAccounts(await customerListSubAccounts(credential));
    } catch (cause) {
      if (isSessionFailure(cause)) {
        onSessionExpired();
        return;
      }
      setError(errorMessage(cause, "子账号列表加载失败，请稍后重试。"));
    } finally {
      setIsLoading(false);
    }
  }, [loadSession, onSessionExpired]);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function createSubAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isCreating) {
      return;
    }
    const username = form.username.trim();
    const displayName = form.display_name.trim();
    if (!username) {
      setError("请输入用户名。");
      return;
    }
    if (!displayName) {
      setError("请输入显示名称。");
      return;
    }
    setIsCreating(true);
    setError("");
    setNotice("");
    try {
      const credential = await loadSession();
      if (credential === null) {
        return;
      }
      await customerCreateSubAccount(credential, {
        username,
        display_name: displayName,
        ...(form.password ? { password: form.password } : {}),
      });
      setForm({ username: "", display_name: "", password: "" });
      setNotice(
        form.password
          ? "子账号已创建，可以立即登录。"
          : "子账号已创建；设置密码后即可登录。",
      );
      await reload();
    } catch (cause) {
      if (isSessionFailure(cause)) {
        onSessionExpired();
        return;
      }
      setError(errorMessage(cause, "创建子账号失败，请稍后重试。"));
    } finally {
      setIsCreating(false);
    }
  }

  async function saveDisplayName(subAccountId: string) {
    const displayName = editDisplayName.trim();
    if (!displayName) {
      setError("显示名称不能为空。");
      return;
    }
    setBusyId(subAccountId);
    setError("");
    try {
      const credential = await loadSession();
      if (credential === null) {
        return;
      }
      await customerUpdateSubAccount(credential, subAccountId, {
        display_name: displayName,
      });
      setEditingId(null);
      await reload();
    } catch (cause) {
      if (isSessionFailure(cause)) {
        onSessionExpired();
        return;
      }
      setError(errorMessage(cause, "保存子账号信息失败，请稍后重试。"));
    } finally {
      setBusyId(null);
    }
  }

  async function toggleActive(subAccount: CustomerSubAccount) {
    const nextActive = !subAccount.is_active;
    if (
      !nextActive &&
      !window.confirm(
        `确认停用「${subAccount.display_name}」？该子账号当前登录会立即失效。`,
      )
    ) {
      return;
    }
    setBusyId(subAccount.id);
    setError("");
    setNotice("");
    try {
      const credential = await loadSession();
      if (credential === null) {
        return;
      }
      await customerUpdateSubAccount(credential, subAccount.id, {
        is_active: nextActive,
      });
      setNotice(nextActive ? "子账号已启用。" : "子账号已停用。");
      await reload();
    } catch (cause) {
      if (isSessionFailure(cause)) {
        onSessionExpired();
        return;
      }
      setError(errorMessage(cause, "更新子账号状态失败，请稍后重试。"));
    } finally {
      setBusyId(null);
    }
  }

  async function resetPassword(subAccount: CustomerSubAccount) {
    const password = window.prompt(
      `为「${subAccount.display_name}」设置新的登录密码：`,
    );
    if (password === null) {
      return;
    }
    if (!password) {
      setError("密码不能为空。");
      return;
    }
    setBusyId(subAccount.id);
    setError("");
    setNotice("");
    try {
      const credential = await loadSession();
      if (credential === null) {
        return;
      }
      await customerSetSubAccountPassword(credential, subAccount.id, password);
      setNotice("密码已更新；该子账号的旧登录已失效。");
      await reload();
    } catch (cause) {
      if (isSessionFailure(cause)) {
        onSessionExpired();
        return;
      }
      setError(errorMessage(cause, "设置密码失败，请稍后重试。"));
    } finally {
      setBusyId(null);
    }
  }

  async function removeSubAccount(subAccount: CustomerSubAccount) {
    if (
      !window.confirm(
        `确认删除子账号「${subAccount.display_name}」？\n\n该账号的设备与登录会一并清理；若已有消费记录将转为停用保留。`,
      )
    ) {
      return;
    }
    setBusyId(subAccount.id);
    setError("");
    setNotice("");
    try {
      const credential = await loadSession();
      if (credential === null) {
        return;
      }
      const result = await customerDeleteSubAccount(credential, subAccount.id);
      setNotice(
        result.deleted
          ? "子账号已删除。"
          : "该子账号已有消费记录，已转为停用保留。",
      );
      await reload();
    } catch (cause) {
      if (isSessionFailure(cause)) {
        onSessionExpired();
        return;
      }
      setError(errorMessage(cause, "删除子账号失败，请稍后重试。"));
    } finally {
      setBusyId(null);
    }
  }

  const items = subAccounts ?? [];

  return (
    <section className="sub-accounts-page" aria-label="子账号管理">
      <header className="sub-accounts-page__header">
        <div>
          <p className="eyebrow">子账号管理</p>
          <h3>为团队创建子账号</h3>
          <p>
            子账号共享本机构的余额与素材，消费记入母账号账单；每个子账号独立登录、独立设备。
          </p>
        </div>
      </header>

      <form className="sub-accounts-page__create" onSubmit={createSubAccount}>
        <label>
          用户名
          <input
            autoComplete="off"
            disabled={isCreating}
            onChange={(event) =>
              setForm((current) => ({
                ...current,
                username: event.target.value,
              }))
            }
            placeholder="登录账号，全局唯一"
            value={form.username}
          />
        </label>
        <label>
          显示名称
          <input
            autoComplete="off"
            disabled={isCreating}
            onChange={(event) =>
              setForm((current) => ({
                ...current,
                display_name: event.target.value,
              }))
            }
            placeholder="团队里怎么称呼"
            value={form.display_name}
          />
        </label>
        <label>
          初始密码（可选）
          <input
            autoComplete="new-password"
            disabled={isCreating}
            onChange={(event) =>
              setForm((current) => ({
                ...current,
                password: event.target.value,
              }))
            }
            placeholder="留空则稍后设置"
            type="password"
            value={form.password}
          />
        </label>
        <button disabled={isCreating} type="submit">
          {isCreating ? "正在创建" : "创建子账号"}
        </button>
      </form>

      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}
      {notice ? (
        <p className="wallet-notice" role="status">
          {notice}
        </p>
      ) : null}

      {isLoading ? (
        <p className="status-note">正在读取子账号…</p>
      ) : items.length === 0 ? (
        <p className="status-note">
          还没有子账号。创建后把用户名和密码交给团队成员即可。
        </p>
      ) : (
        <ul className="sub-accounts-list">
          {items.map((subAccount) => {
            const busy = busyId === subAccount.id;
            const editing = editingId === subAccount.id;
            return (
              <li className="sub-account-card" key={subAccount.id}>
                <div className="sub-account-card__identity">
                  {editing ? (
                    <input
                      aria-label={`显示名称 ${subAccount.username}`}
                      maxLength={64}
                      onChange={(event) =>
                        setEditDisplayName(event.target.value)
                      }
                      value={editDisplayName}
                    />
                  ) : (
                    <strong>{subAccount.display_name}</strong>
                  )}
                  <small>{subAccount.username}</small>
                </div>
                <div className="sub-account-card__badges">
                  <span
                    className={
                      subAccount.is_active
                        ? "sub-account-badge is-active"
                        : "sub-account-badge"
                    }
                  >
                    {subAccount.is_active ? "正常" : "已停用"}
                  </span>
                  <span
                    className={
                      subAccount.has_password
                        ? "sub-account-badge is-active"
                        : "sub-account-badge"
                    }
                  >
                    {subAccount.has_password ? "已设密码" : "未设密码"}
                  </span>
                </div>
                <div className="sub-account-card__actions">
                  {editing ? (
                    <>
                      <button
                        disabled={busy}
                        onClick={() => void saveDisplayName(subAccount.id)}
                        type="button"
                      >
                        保存
                      </button>
                      <button
                        disabled={busy}
                        onClick={() => setEditingId(null)}
                        type="button"
                      >
                        取消
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        disabled={busy}
                        onClick={() => {
                          setEditingId(subAccount.id);
                          setEditDisplayName(subAccount.display_name);
                        }}
                        type="button"
                      >
                        重命名
                      </button>
                      <button
                        disabled={busy}
                        onClick={() => void resetPassword(subAccount)}
                        type="button"
                      >
                        设置密码
                      </button>
                      <button
                        disabled={busy}
                        onClick={() => void toggleActive(subAccount)}
                        type="button"
                      >
                        {subAccount.is_active ? "停用" : "启用"}
                      </button>
                      <button
                        className="is-danger"
                        disabled={busy}
                        onClick={() => void removeSubAccount(subAccount)}
                        type="button"
                      >
                        删除
                      </button>
                    </>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

/** 会话失效（401/403 围栏拒绝）统一收敛：停用/权限错误交给上层退回登录。 */
function isSessionFailure(cause: unknown): boolean {
  return (
    cause instanceof CustomerApiError &&
    (cause.status === 401 || cause.status === 403)
  );
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim()
    ? error.message
    : fallback;
}
