import { type FormEvent, useCallback, useEffect, useState } from "react";
import {
  CustomerApiError,
  type CustomerSubAccount,
  customerCreateSubAccount,
  customerDeleteSubAccount,
  customerListSubAccounts,
  customerSetSubAccountPassword,
  customerSetSubAccountQuota,
  customerUpdateSubAccount,
} from "../api";
import {
  type QuotaState,
  quotaBarData,
  quotaPercentUsed,
  quotaState,
  quotaVizMessage,
  shanghaiMonthProgress,
  summarizeSubAccountQuotas,
} from "./quotaViz";
import type { CustomerCredentialStore } from "./useCustomerSession";
import "./customer-subaccounts.css";

/** 与后端 ``sub_account_quota.MAX_MONTHLY_QUOTA_CREDITS`` 对齐的上限。 */
const MAX_MONTHLY_QUOTA_CREDITS = 1_000_000_000;

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
  now,
}: {
  store: CustomerCredentialStore;
  onSessionExpired: () => void;
  /** 测试注入固定时刻；缺省取渲染时当前时间（月度进度按上海自然月折算）。 */
  now?: Date;
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
    monthly_quota: "",
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
    const quota = parseQuotaInput(form.monthly_quota);
    if (quota === undefined) {
      setError("月度额度必须是不超过 10 亿的整数（单位：积分）。");
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
        ...(quota !== null ? { monthly_quota_credits: quota } : {}),
      });
      setForm({
        username: "",
        display_name: "",
        password: "",
        monthly_quota: "",
      });
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

  async function setQuota(subAccount: CustomerSubAccount) {
    const raw = window.prompt(
      `为「${subAccount.display_name}」设置月度额度（单位：积分）。\n输入整数：0 表示不允许消费；留空表示不限额度。`,
      subAccount.monthly_quota_credits === null
        ? ""
        : String(subAccount.monthly_quota_credits),
    );
    if (raw === null) {
      return;
    }
    const quota = parseQuotaInput(raw);
    if (quota === undefined) {
      setError("月度额度必须是不超过 10 亿的整数（单位：积分）。");
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
      await customerSetSubAccountQuota(credential, subAccount.id, quota);
      setNotice(
        quota === null
          ? `已清除「${subAccount.display_name}」的额度限制。`
          : `已将「${subAccount.display_name}」的月度额度设为 ${quota} 积分。`,
      );
      await reload();
    } catch (cause) {
      if (isSessionFailure(cause)) {
        onSessionExpired();
        return;
      }
      setError(errorMessage(cause, "设置额度失败，请稍后重试。"));
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

  // 批次1 额度可视化：月度进度与聚合只依赖列表数据，渲染期一次算好（纯函数）。
  const progress = shanghaiMonthProgress(now ?? new Date());
  const overview = summarizeSubAccountQuotas(items);
  const bars = quotaBarData(items);

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
        <label>
          月度额度（积分，可选）
          <input
            autoComplete="off"
            disabled={isCreating}
            inputMode="numeric"
            onChange={(event) =>
              setForm((current) => ({
                ...current,
                monthly_quota: event.target.value,
              }))
            }
            placeholder="留空则不限"
            value={form.monthly_quota}
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

      {!isLoading && items.length > 0 ? (
        <>
          <section className="sub-accounts-kpis" aria-label="子账号额度概览">
            <article>
              <span>子账号数</span>
              <strong>{overview.count}</strong>
              <small>
                {overview.cappedCount > 0
                  ? `${overview.cappedCount} 个已设月度额度`
                  : "均未设置月度额度"}
              </small>
            </article>
            <article>
              <span>本月总消费</span>
              <strong>{overview.totalUsed} 积分</strong>
              <small>所有子账号合计</small>
            </article>
            <article>
              <span>剩余额度总和</span>
              <strong>{overview.remainingTotal} 积分</strong>
              <small>{overview.cappedCount} 个设限子账号合计</small>
            </article>
            <article
              className={overview.exhaustedCount > 0 ? "is-danger" : undefined}
            >
              <span>额度超限</span>
              <strong>{overview.exhaustedCount}</strong>
              <small>
                {overview.exhaustedCount > 0
                  ? "调高额度或等下月 1 日重置"
                  : "暂无额度超限的子账号"}
              </small>
            </article>
          </section>

          <section className="sub-accounts-chart" aria-label="本月消费占比">
            <div className="sub-accounts-chart__header">
              <span>本月消费占比</span>
              <small>
                {progress.year} 年 {progress.month} 月 · 按子账号聚合
              </small>
            </div>
            {bars.map((bar) => (
              <div
                className={
                  bar.state === "exhausted"
                    ? "sub-account-bar-row is-exhausted"
                    : "sub-account-bar-row"
                }
                key={bar.id}
              >
                <span className="sub-account-bar-row__name">{bar.label}</span>
                <span aria-hidden="true" className="sub-account-bar-row__track">
                  <span
                    className="sub-account-bar-row__fill"
                    style={{ width: `${bar.percent}%` }}
                  />
                </span>
                <span className="sub-account-bar-row__amount">
                  <strong>{bar.used}</strong>
                  <small>{bar.percent}%</small>
                </span>
              </div>
            ))}
          </section>
        </>
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
            // 负数已用量（历史跨月退回遗留）显示前钳到 0，进度条不接受负值。
            const quotaUsed = Math.max(0, subAccount.quota_used_credits);
            const quotaVizState = quotaState(
              quotaUsed,
              subAccount.monthly_quota_credits,
            );
            // 评审 P2：上限为 0（不允许消费）时按满条渲染——HTML 的 max=0
            // 会回退为 1，照搬会渲染成空条，与「额度已用尽」状态自相矛盾。
            const quotaCap = subAccount.monthly_quota_credits;
            const barMax = quotaCap !== null && quotaCap > 0 ? quotaCap : 1;
            const barValue =
              quotaCap !== null && quotaCap > 0
                ? Math.min(quotaUsed, quotaCap)
                : barMax;
            const quotaMessage = quotaVizMessage(
              quotaUsed,
              subAccount.monthly_quota_credits,
              progress,
            );
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
                <div className="sub-account-card__quota">
                  {subAccount.monthly_quota_credits === null ? (
                    <span>额度不限</span>
                  ) : (
                    <>
                      <span>
                        {`本月已用 ${quotaUsed} / ${subAccount.monthly_quota_credits} 积分（${quotaPercentUsed(quotaUsed, subAccount.monthly_quota_credits)}%）`}
                      </span>
                      <progress
                        className={quotaBarClassName(quotaVizState)}
                        max={barMax}
                        value={barValue}
                      />
                    </>
                  )}
                  {quotaMessage ? (
                    <small
                      className={
                        quotaVizState === "exhausted"
                          ? "sub-account-card__forecast is-danger"
                          : "sub-account-card__forecast"
                      }
                    >
                      {quotaMessage}
                    </small>
                  ) : null}
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
                  {quotaVizState === "warning" ? (
                    <span className="sub-account-badge is-warning">
                      额度接近上限
                    </span>
                  ) : null}
                  {quotaVizState === "exhausted" ? (
                    <span className="sub-account-badge is-danger">
                      额度已用尽
                    </span>
                  ) : null}
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
                        onClick={() => void setQuota(subAccount)}
                        type="button"
                      >
                        设置额度
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

/** 三态进度条的 className 映射（unlimited 不渲染进度条，只处理有上限者）。 */
function quotaBarClassName(state: QuotaState): string {
  if (state === "exhausted") {
    return "sub-account-quota-bar is-exhausted";
  }
  if (state === "warning") {
    return "sub-account-quota-bar is-warning";
  }
  return "sub-account-quota-bar";
}

/** 解析额度输入："" → null（不限）；非法或超上限 → undefined（调用方报错）。 */
function parseQuotaInput(raw: string): number | null | undefined {
  const trimmed = raw.trim();
  if (trimmed === "") {
    return null;
  }
  if (!/^\d+$/.test(trimmed)) {
    return undefined;
  }
  const value = Number(trimmed);
  return value <= MAX_MONTHLY_QUOTA_CREDITS ? value : undefined;
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
