import { type FormEvent, useCallback, useEffect, useState } from "react";
import {
  CustomerApiError,
  type CustomerSubAccount,
  customerCreateSubAccount,
  customerDeleteSubAccount,
  customerListSubAccounts,
  customerSetSubAccountPassword,
  customerSetSubAccountPermissions,
  customerSetSubAccountQuota,
  customerUpdateSubAccount,
} from "../api";
import { useCustomerConfirm } from "./CustomerConfirmDialog";
import {
  BUSINESS_FEATURES,
  isAllGranted,
  isPermissionRestricted,
  type PermissionDraft,
  permissionDraft,
  permissionSummary,
  permissionsPayload,
  toggleBusiness,
} from "./permissionViz";
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
  isMasterCaller = true,
}: {
  store: CustomerCredentialStore;
  onSessionExpired: () => void;
  /** 测试注入固定时刻；缺省取渲染时当前时间（月度进度按上海自然月折算）。 */
  now?: Date;
  /**
   * 调用方是否母账号。SUB_ADMIN 能进本页（服务端放行列表/额度/权限），
   * 但创建/改名/密码/角色/停用/删除走 _lock_master 一律 403——对它隐藏这些
   * 入口，避免「点按钮 → 403 报错」的死路（上线前检查 P1-1）。
   */
  isMasterCaller?: boolean;
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
  // 批次2 功能权限矩阵：Modal 目标子账号 + 受控草稿（关闭时目标为 null）。
  const [permissionTarget, setPermissionTarget] =
    useState<CustomerSubAccount | null>(null);
  const [permissionDraftState, setPermissionDraftState] =
    useState<PermissionDraft>(() => permissionDraft(null));
  const [permissionError, setPermissionError] = useState("");
  const [isSavingPermissions, setIsSavingPermissions] = useState(false);
  const { confirm, dialog: confirmDialog } = useCustomerConfirm();
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

  /** 停用/启用共用的写路径。失败时抛出：确认框里的失败要留在框内，直接执行的分支自己接住。 */
  async function applyActiveChange(
    subAccount: CustomerSubAccount,
    nextActive: boolean,
  ) {
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
      throw new Error(errorMessage(cause, "更新子账号状态失败，请稍后重试。"));
    } finally {
      setBusyId(null);
    }
  }

  function toggleActive(subAccount: CustomerSubAccount) {
    const nextActive = !subAccount.is_active;
    if (nextActive) {
      void applyActiveChange(subAccount, true).catch((cause: unknown) =>
        setError(
          cause instanceof Error ? cause.message : "更新子账号状态失败。",
        ),
      );
      return;
    }
    // 停用会让该子账号当场掉线，属不可逆操作，走产品级确认框（P0 清单 #2）。
    confirm({
      title: `停用「${subAccount.display_name}」？`,
      description: "该子账号当前登录会立即失效，已创建的项目与 Token 保留。",
      level: "acknowledge",
      confirmLabel: "停用子账号",
      onConfirm: () => applyActiveChange(subAccount, false),
    });
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

  function removeSubAccount(subAccount: CustomerSubAccount) {
    // 删号不可撤销，走产品级确认框（P0 清单 #2）。
    confirm({
      title: `删除子账号「${subAccount.display_name}」？`,
      description:
        "该账号的设备与登录会一并清理；若已有消费记录将转为停用保留，历史不会被改写。",
      level: "acknowledge",
      confirmLabel: "删除子账号",
      onConfirm: async () => {
        setBusyId(subAccount.id);
        setError("");
        setNotice("");
        try {
          const credential = await loadSession();
          if (credential === null) {
            return;
          }
          const result = await customerDeleteSubAccount(
            credential,
            subAccount.id,
          );
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
          throw new Error(errorMessage(cause, "删除子账号失败，请稍后重试。"));
        } finally {
          setBusyId(null);
        }
      },
    });
  }

  /** 打开权限 Modal：草稿从服务端现值展开（null = 全允许 → 全部勾选）。 */
  function openPermissions(subAccount: CustomerSubAccount) {
    setPermissionTarget(subAccount);
    setPermissionDraftState(permissionDraft(subAccount.permissions));
    setPermissionError("");
    setError("");
    setNotice("");
  }

  /** 保存功能权限：PUT 三字段（全必填）；全开值由后端折叠为删行。 */
  async function savePermissions() {
    if (permissionTarget === null || isSavingPermissions) {
      return;
    }
    const target = permissionTarget;
    const draft = permissionDraftState;
    setIsSavingPermissions(true);
    setPermissionError("");
    try {
      const credential = await loadSession();
      if (credential === null) {
        return;
      }
      await customerSetSubAccountPermissions(
        credential,
        target.id,
        permissionsPayload(draft),
      );
      setNotice(
        isAllGranted(draft)
          ? `已恢复「${target.display_name}」的全部权限。`
          : `已更新「${target.display_name}」的功能权限。`,
      );
      setPermissionTarget(null);
      await reload();
    } catch (cause) {
      if (isSessionFailure(cause)) {
        onSessionExpired();
        return;
      }
      setPermissionError(errorMessage(cause, "保存功能权限失败，请稍后重试。"));
    } finally {
      setIsSavingPermissions(false);
    }
  }

  /** 设为/取消管理员（母账号限定的角色变更，PATCH account_type）。 */
  function toggleAdminRole(subAccount: CustomerSubAccount) {
    const nextType =
      subAccount.account_type === "SUB_ADMIN" ? "SUB" : "SUB_ADMIN";
    const promoting = nextType === "SUB_ADMIN";
    // 角色变更扩大或收回他人的管理面，属不可逆操作，走产品级确认框。
    confirm({
      title: promoting
        ? `设为管理员：「${subAccount.display_name}」？`
        : `取消管理员：「${subAccount.display_name}」？`,
      description: promoting
        ? "管理员可以管理本机构的其他子账号（仍不能管理母账号）。"
        : "取消后该子账号只能使用分配给它的功能与额度。",
      level: "acknowledge",
      confirmLabel: promoting ? "设为管理员" : "取消管理员",
      onConfirm: async () => {
        setBusyId(subAccount.id);
        setError("");
        setNotice("");
        try {
          const credential = await loadSession();
          if (credential === null) {
            return;
          }
          await customerUpdateSubAccount(credential, subAccount.id, {
            account_type: nextType,
          });
          setNotice(
            promoting
              ? `已将「${subAccount.display_name}」设为管理员。`
              : `已取消「${subAccount.display_name}」的管理员身份。`,
          );
          await reload();
        } catch (cause) {
          if (isSessionFailure(cause)) {
            onSessionExpired();
            return;
          }
          throw new Error(
            errorMessage(cause, "更新子账号角色失败，请稍后重试。"),
          );
        } finally {
          setBusyId(null);
        }
      },
    });
  }

  const items = subAccounts ?? [];

  // 批次1 额度可视化：月度进度与聚合只依赖列表数据，渲染期一次算好（纯函数）。
  const progress = shanghaiMonthProgress(now ?? new Date());
  const overview = summarizeSubAccountQuotas(items);
  const bars = quotaBarData(items);

  return (
    <section className="sub-accounts-page" aria-label="子账号管理">
      {confirmDialog}
      <header className="sub-accounts-page__header">
        <div>
          <p className="eyebrow">子账号管理</p>
          <h3>{isMasterCaller ? "为团队创建子账号" : "管理团队子账号"}</h3>
          <p>
            子账号共享本机构的余额与素材，消费记入母账号账单；每个子账号独立登录、独立设备。
          </p>
        </div>
      </header>

      {isMasterCaller ? (
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
      ) : null}

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
                  <span className="sub-account-card__permissions">
                    {`权限：${permissionSummary(subAccount.permissions) || "全部开放"}`}
                  </span>
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
                  {subAccount.account_type === "SUB_ADMIN" ? (
                    <span className="sub-account-badge is-admin">管理员</span>
                  ) : null}
                  {isPermissionRestricted(subAccount.permissions) ? (
                    <span className="sub-account-badge is-warning">
                      权限受限
                    </span>
                  ) : null}
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
                      {isMasterCaller ? (
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
                        </>
                      ) : null}
                      <button
                        disabled={busy}
                        onClick={() => void setQuota(subAccount)}
                        type="button"
                      >
                        设置额度
                      </button>
                      <button
                        disabled={busy}
                        onClick={() => openPermissions(subAccount)}
                        type="button"
                      >
                        设置权限
                      </button>
                      {isMasterCaller ? (
                        <>
                      <button
                        disabled={busy}
                        onClick={() => void toggleAdminRole(subAccount)}
                        type="button"
                      >
                        {subAccount.account_type === "SUB_ADMIN"
                          ? "取消管理员"
                          : "设为管理员"}
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
                      ) : null}
                    </>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {permissionTarget !== null ? (
        <div className="sub-account-modal">
          <section
            aria-label={`功能权限 ${permissionTarget.display_name}`}
            aria-modal="true"
            className="sub-account-modal__panel"
            role="dialog"
          >
            <header className="sub-account-modal__header">
              <h4>功能权限</h4>
              <p>
                {permissionTarget.display_name}（{permissionTarget.username}）
              </p>
            </header>
            <p className="sub-account-modal__hint">
              未勾选的业务对该子账号不可用；全部开放即恢复默认（不存储限制）。
            </p>
            <fieldset className="sub-account-permission-grid">
              <legend>业务权限</legend>
              {BUSINESS_FEATURES.map((feature) => (
                <label key={feature.key}>
                  <input
                    checked={permissionDraftState.businesses.includes(
                      feature.key,
                    )}
                    disabled={isSavingPermissions}
                    onChange={() =>
                      setPermissionDraftState((current) =>
                        toggleBusiness(current, feature.key),
                      )
                    }
                    type="checkbox"
                  />
                  {feature.label}
                </label>
              ))}
            </fieldset>
            <fieldset className="sub-account-permission-switches">
              <legend>系统权限</legend>
              <label>
                <input
                  checked={permissionDraftState.allowApiKeys}
                  disabled={isSavingPermissions}
                  onChange={(event) =>
                    setPermissionDraftState((current) => ({
                      ...current,
                      allowApiKeys: event.target.checked,
                    }))
                  }
                  type="checkbox"
                />
                允许创建 API Token
              </label>
              <label>
                <input
                  checked={permissionDraftState.allowPublishAccounts}
                  disabled={isSavingPermissions}
                  onChange={(event) =>
                    setPermissionDraftState((current) => ({
                      ...current,
                      allowPublishAccounts: event.target.checked,
                    }))
                  }
                  type="checkbox"
                />
                允许使用发布账号（导入 / 扫码）
              </label>
            </fieldset>
            {isAllGranted(permissionDraftState) ? (
              <p className="sub-account-modal__hint">
                当前为全部开放：保存后该子账号恢复默认权限。
              </p>
            ) : null}
            {permissionError ? (
              <p className="settings-error" role="alert">
                {permissionError}
              </p>
            ) : null}
            <div className="sub-account-modal__actions">
              <button
                disabled={isSavingPermissions}
                onClick={() => void savePermissions()}
                type="button"
              >
                {isSavingPermissions ? "正在保存" : "保存权限"}
              </button>
              <button
                disabled={isSavingPermissions}
                onClick={() => setPermissionTarget(null)}
                type="button"
              >
                取消
              </button>
            </div>
          </section>
        </div>
      ) : null}
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
  // 403 是权限拒绝（如 SUB_ADMIN 触达母账号专属操作），不是会话失效：
  // 它必须落在页面错误区展示服务端文案，绝不能触发 onSessionExpired 把
  // 在线用户踢回登录页（上线前检查 P1-1）。会话失效只有 401。
  return cause instanceof CustomerApiError && cause.status === 401;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim()
    ? error.message
    : fallback;
}
