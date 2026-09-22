import {
  type FormEvent,
  Fragment,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { Pagination } from "../admin/ui/Pagination";
import {
  type CreatedCustomerApiKey,
  CustomerApiError,
  type CustomerApiKey,
  type CustomerCenterSummary,
  customerCloseRechargeOrder,
  customerCreateApiKey,
  customerGetCenterSummary,
  customerGetProfile,
  customerInitializeDefaultApiKey,
  customerListApiKeys,
  customerListRechargeOrders,
  customerListWalletTransactions,
  customerRevokeApiKey,
  customerRotateApiKey,
  getStudioNotificationPreferences,
  type RechargeOrderPage,
  updateStudioNotificationPreferences,
  type WalletTransaction,
  type WalletTransactionPage,
} from "../api";
import { BrandIdentity } from "../BrandIdentity";
import { useStudio } from "../studio/context";
import { PublishAccountsPanel } from "../studio/MainPages";
import { Icon } from "../studio/ui";
import type { WorkspaceShellProps } from "../workspace-shell";
import { AccountPasswordSetup } from "./AccountPasswordSetup";
import { useCustomerConfirm } from "./CustomerConfirmDialog";
import { CustomerPricesPage } from "./CustomerPricesPage";
import { CustomerRechargeDialog } from "./CustomerRechargeDialog";
import { LedgerPairingSummary } from "./LedgerPairingSummary";
import {
  groupLedgerRows,
  type LedgerPairState,
  netAvailableDelta,
  netReservedDelta,
} from "./ledger-pairing";
import { quotaPercentUsed, quotaState } from "./quotaViz";
import { SecuritySection } from "./SecuritySection";
import { SubAccountManagementPage } from "./SubAccountManagementPage";
import { TransactionPricingBreakdown } from "./TransactionPricingBreakdown";
import type { CustomerStoredIdentity } from "./useCustomerSession";
import "./customer-center.css";

/** 页签全集。子账号管理对普通子账号不可见（见 `visibleTabs`），
 * 所以这里保留全集、渲染前再过滤，键盘导航才和可见页签一致。 */
const ALL_TABS = [
  ["tokens", "Token 管理"],
  ["consumption", "消费记录"],
  ["recharge", "充值记录"],
  ["prices", "接口价格"],
  ["publishing", "发布账号"],
  ["sub-accounts", "子账号管理"],
  ["settings", "账号设置"],
] as const;
type Tab = (typeof ALL_TABS)[number][0];
type Mutation =
  | { kind: "create" }
  | { kind: "rotate" | "revoke"; token: CustomerApiKey };
const date = (value: string | null) =>
  value
    ? new Date(value).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        hour12: false,
      })
    : "尚未使用";
const message = (error: unknown) =>
  error instanceof Error ? error.message : "操作失败，请稍后重试。";

/** 服务端把账号身份收敛为 MASTER/SUB/SUB_ADMIN。 */
type AccountType = "MASTER" | "SUB" | "SUB_ADMIN";

/**
 * 身份解析：`profile.account_type` 是权威副本（服务端必返、默认 MASTER），
 * 凭据库里的缓存身份只在 profile 到达前兜底——重启恢复的先头帧可能拿不到身份。
 * 两者都没有时返回 null，界面保持沉默，而不是替用户猜一个「母账号」。
 */
function resolveAccountType(
  profileType: string | null | undefined,
  identity: CustomerStoredIdentity | null,
): AccountType | null {
  const raw = profileType ?? identity?.accountType ?? null;
  if (!raw) return null;
  return raw === "SUB" || raw === "SUB_ADMIN" ? raw : "MASTER";
}

const LEDGER_TYPE_LABEL: Record<WalletTransaction["type"], string> = {
  CHARGE: "积分入账",
  CONVERSION: "历史积分转换",
  RESERVE: "任务预扣",
  SETTLE: "任务消费",
  RELEASE: "积分退回",
};

/** P1-7：折叠后的计费周期按最终态命名，而不是最后写入的那一笔。 */
const PAIR_STATE_LABEL: Record<LedgerPairState, string> = {
  PENDING: "任务预扣",
  SETTLED: "任务消费",
  RELEASED: "积分退回",
};

const CREDIT_SOURCE_LABEL: Record<string, string> = {
  FREE_GRANT: "积分赠送",
  CREDIT_COMPENSATION: "积分补偿",
  zpay: "在线充值",
  wechat_native: "微信充值",
  activation_code: "账号激活",
  FINANCE_RECEIPT: "后台入账",
  COMPENSATION_APPROVAL: "后台调整",
};

function signedCredits(value: number): string {
  return `${value > 0 ? "+" : ""}${value} 积分`;
}

/** 流水行的业务描述：优先具体服务名，退到任务类型。 */
function businessDescription(item: WalletTransaction): string {
  if (item.service_name) return item.service_name;
  if (item.type === "CONVERSION") return "历史余额";
  if (item.oral_task_id) return "数字人口播";
  if (item.task_id) return "视频生成";
  return "充值 / 赠送";
}

function ledgerSource(item: WalletTransaction) {
  const label = item.credit_source
    ? (CREDIT_SOURCE_LABEL[item.credit_source] ?? "后台入账")
    : item.api_key_id
      ? `${item.token_label || "Token"} · V${item.credential_version ?? 1}`
      : item.auth_source === "session"
        ? "软件操作"
        : item.auth_source === "internal"
          ? "内部操作"
          : "历史来源未记录";
  return (
    <>
      {label}
      {item.credit_price_version != null && (
        <small>价格 V{item.credit_price_version}</small>
      )}
    </>
  );
}

export function CustomerCenterPage({
  account,
}: {
  account: NonNullable<WorkspaceShellProps["customerAccount"]>;
}) {
  const { navigate, notify, user } = useStudio();
  const { confirm: confirmAction, dialog: confirmDialog } =
    useCustomerConfirm();
  const [tab, setTab] = useState<Tab>("tokens");
  const [summary, setSummary] = useState<CustomerCenterSummary | null>(null);
  const [tokens, setTokens] = useState<CustomerApiKey[] | null>(null);
  const [error, setError] = useState("");
  const [tokenError, setTokenError] = useState("");
  const [notice, setNotice] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [mutation, setMutation] = useState<Mutation | null>(null);
  const [secret, setSecret] = useState<CreatedCustomerApiKey | null>(null);
  const [tokenName, setTokenName] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const retry = useRef<{ fingerprint: string; key: string } | null>(null);
  const defaultKey = useRef(crypto.randomUUID());
  const defaultPending = useRef(false);
  const [recharge, setRecharge] = useState(false);
  const [displayName, setDisplayName] = useState(
    account.profile?.display_name ?? user.display_name,
  );
  // CW-062：缓存身份只作为 profile 到达前的兜底，读失败就保持未知（徽章不出现）。
  const [cachedIdentity, setCachedIdentity] =
    useState<CustomerStoredIdentity | null>(account.identity ?? null);
  const [notifications, setNotifications] = useState<boolean | null>(null);
  const [preferencesError, setPreferencesError] = useState("");
  const [transactionPage, setTransactionPage] =
    useState<WalletTransactionPage | null>(null);
  // P1-7：同一计费周期的行折叠成一组，展开状态只属于当前页面。
  const [expandedPairs, setExpandedPairs] = useState<ReadonlySet<string>>(
    new Set(),
  );
  const [orderPage, setOrderPage] = useState<RechargeOrderPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [filters, setFilters] = useState({
    source: "",
    type: "",
    business: "",
    start: "",
    end: "",
  });
  function updateFilter(key: keyof typeof filters, value: string) {
    setFilters((previous) => ({ ...previous, [key]: value }));
    setOffset(0);
    setTransactionPage(null);
  }
  const [recordsError, setRecordsError] = useState("");
  const [recordsBusy, setRecordsBusy] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const profile = account.profile;
  const name = profile?.display_name || user.display_name || user.username;
  const accountType = resolveAccountType(profile?.account_type, cachedIdentity);
  const isMasterAccount = accountType === "MASTER";
  // SUB_ADMIN 也归子账号：它有母账号、受额度约束，只是额外获授权管子账号。
  const isSubAccount = accountType === "SUB" || accountType === "SUB_ADMIN";
  // 子账号管理页签：服务端只放行母账号与获授权的 SUB_ADMIN，普通 SUB 一律 403，
  // 所以只对「确认是 SUB」的会话隐藏（产品判断：组织管理，Web 与桌面都出现——
  // 它跟 devices 那种端专属能力不同，没有设备依赖；要改成端专属只需动这一行）。
  const canManageSubAccounts = accountType !== "SUB";
  const visibleTabs = ALL_TABS.filter(
    ([id]) => id !== "sub-accounts" || canManageSubAccounts,
  );
  const parentDisplayName =
    profile?.parent_display_name ?? cachedIdentity?.parentDisplayName ?? null;
  // 额度读数：母账号自己持有钱包、服务端恒返 null/null，所以只对子账号出现。
  const quotaCap = profile?.monthly_quota_credits ?? null;
  const rawQuotaUsed = profile?.quota_used_credits;
  const quotaUsed = rawQuotaUsed == null ? null : Math.max(0, rawQuotaUsed);
  const quotaRemaining =
    quotaCap === null || quotaUsed === null
      ? null
      : Math.max(0, quotaCap - quotaUsed);
  const quotaPercent =
    quotaUsed === null ? null : quotaPercentUsed(quotaUsed, quotaCap);
  const quotaLevel =
    quotaUsed === null ? null : quotaState(quotaUsed, quotaCap);
  // 读数三态：资料未到 / 读取失败 / 就绪。就绪前不拿 0 顶替真实数字。
  const quotaPending =
    profile === null
      ? account.profileLoadError
        ? "读取失败"
        : "读取中"
      : null;
  const quotaCapText =
    quotaPending ??
    (quotaCap === null ? "不限" : `${quotaCap.toLocaleString("zh-CN")} 积分`);
  const quotaUsedText =
    quotaPending ??
    (quotaUsed === null ? "—" : `${quotaUsed.toLocaleString("zh-CN")} 积分`);
  const quotaRemainingText =
    quotaPending ??
    (quotaRemaining === null
      ? "不限"
      : `${quotaRemaining.toLocaleString("zh-CN")} 积分`);
  const deviceSlotsText =
    profile === null
      ? account.profileLoadError
        ? "读取失败"
        : "读取中"
      : `${profile.device_slots_used} 台`;
  const credential = useCallback(async () => {
    const token = await account.store.loadSessionToken();
    if (!token) {
      account.onSessionExpired();
      throw new Error("登录已失效，请重新登录。");
    }
    return { kind: "session" as const, token };
  }, [account.store, account.onSessionExpired]);
  useEffect(() => {
    setDisplayName(profile?.display_name ?? user.display_name);
  }, [profile?.display_name, user.display_name]);
  useEffect(() => {
    if (account.identity) {
      setCachedIdentity(account.identity);
      return;
    }
    const load = account.loadIdentity;
    if (!load) return;
    let active = true;
    void load()
      .then((next) => {
        if (active && next) setCachedIdentity(next);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [account.identity, account.loadIdentity]);
  // 身份后到时若正站在子账号管理页签（恢复路径），退回 Token 页签：
  // 否则面板会带着 aria-labelledby 指向一个已经不存在的页签。
  useEffect(() => {
    if (!canManageSubAccounts && tab === "sub-accounts") setTab("tokens");
  }, [canManageSubAccounts, tab]);
  useEffect(() => {
    if (!mutation && !secret) return;
    const element = dialog.current;
    const previous = document.activeElement as HTMLElement | null;
    if (element && !element.open) {
      if (typeof element.showModal === "function") element.showModal();
      else element.setAttribute("open", "");
    }
    return () => {
      element?.close?.();
      previous?.focus();
    };
  }, [mutation, secret]);
  // biome-ignore lint/correctness/useExhaustiveDependencies: Refresh explicitly invalidates cached results after writes or retry.
  useEffect(() => {
    let active = true;
    setError("");
    setTokenError("");
    void credential()
      .then(async (auth) => {
        await Promise.allSettled([
          customerGetCenterSummary(auth)
            .then((data) => {
              if (active) setSummary(data);
            })
            .catch((cause) => {
              if (active) setError(message(cause));
            }),
          (async () => {
            try {
              let result = await customerListApiKeys(auth);
              if (
                defaultPending.current ||
                !result.items.some((item) => item.is_default)
              ) {
                defaultPending.current = true;
                const created = await customerInitializeDefaultApiKey(
                  auth,
                  defaultKey.current,
                );
                defaultPending.current = false;
                if (active && created.plaintext) setSecret(created);
                result = await customerListApiKeys(auth);
                const latest = await customerGetCenterSummary(auth);
                if (active) setSummary(latest);
              }
              if (active) setTokens(result.items);
            } catch (cause) {
              // A definitive rejection ends recovery. The next reload reads the
              // persisted default before deciding whether initialization is needed.
              // Transport/5xx failures retain the key to recover a committed write.
              if (
                cause instanceof CustomerApiError &&
                cause.status &&
                cause.status < 500
              ) {
                defaultPending.current = false;
                defaultKey.current = crypto.randomUUID();
              }
              if (active) setTokenError(message(cause));
            }
          })(),
        ]);
      })
      .catch((cause) => {
        if (active) setError(message(cause));
      });
    return () => {
      active = false;
    };
  }, [credential, refresh]);
  // biome-ignore lint/correctness/useExhaustiveDependencies: Refresh explicitly invalidates cached results after writes or retry.
  useEffect(() => {
    let active = true;
    setPreferencesError("");
    void getStudioNotificationPreferences()
      .then((data) => {
        if (active) setNotifications(data.enabled);
      })
      .catch((cause) => {
        if (active) setPreferencesError(message(cause));
      });
    return () => {
      active = false;
    };
  }, [refresh]);
  // biome-ignore lint/correctness/useExhaustiveDependencies: Refresh explicitly invalidates cached results after writes or retry.
  useEffect(() => {
    let active = true;
    if (tab !== "consumption" && tab !== "recharge") return;
    setRecordsError("");
    setRecordsBusy(true);
    void credential()
      .then(async (auth) => {
        if (tab === "recharge") {
          const result = await customerListRechargeOrders(auth, {
            limit: 20,
            offset,
          });
          if (active) setOrderPage(result);
        } else {
          const result = await customerListWalletTransactions(auth, {
            limit: 20,
            offset,
            filters: {
              ...(filters.source.startsWith("token:")
                ? { token_group_id: filters.source.slice(6) }
                : filters.source
                  ? { auth_source: filters.source }
                  : {}),
              ...(filters.type ? { transaction_type: filters.type } : {}),
              ...(filters.business ? { business: filters.business } : {}),
              ...(filters.start
                ? {
                    started_at: new Date(
                      `${filters.start}T00:00:00+08:00`,
                    ).toISOString(),
                  }
                : {}),
              ...(filters.end
                ? {
                    ended_at: new Date(
                      new Date(`${filters.end}T00:00:00+08:00`).getTime() +
                        86400000,
                    ).toISOString(),
                  }
                : {}),
            },
          });
          if (active) setTransactionPage(result);
        }
      })
      .catch((cause) => {
        if (active) setRecordsError(message(cause));
      })
      .finally(() => {
        if (active) setRecordsBusy(false);
      });
    return () => {
      active = false;
    };
  }, [credential, tab, offset, refresh, filters]);
  function selectTab(value: Tab) {
    if (value !== tab) refreshData();
    setTab(value);
    setOffset(0);
    setRecordsError("");
  }
  function refreshData() {
    setRefresh((value) => value + 1);
  }
  async function act(event: FormEvent) {
    event.preventDefault();
    if (!mutation || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setTokenError("");
    const fingerprint =
      mutation.kind === "create"
        ? `create:${tokenName.trim()}`
        : `${mutation.kind}:${mutation.token.id}`;
    if (retry.current?.fingerprint !== fingerprint)
      retry.current = { fingerprint, key: crypto.randomUUID() };
    try {
      const auth = await credential();
      let result: CreatedCustomerApiKey | null = null;
      if (mutation.kind === "create")
        result = await customerCreateApiKey(
          auth,
          tokenName.trim(),
          retry.current.key,
        );
      else if (mutation.kind === "rotate")
        result = await customerRotateApiKey(
          auth,
          mutation.token.id,
          retry.current.key,
        );
      else await customerRevokeApiKey(auth, mutation.token.id);
      retry.current = null;
      setMutation(null);
      if (result?.plaintext) setSecret(result);
      else setNotice("Token 已撤销，其他 Token 和登录设备不受影响。");
      refreshData();
    } catch (cause) {
      if (
        cause instanceof CustomerApiError &&
        cause.status &&
        cause.status < 500
      )
        retry.current = null;
      setTokenError(message(cause));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }
  function closeOrder(orderNo: string) {
    if (busyRef.current) return;
    // 关单不可撤销，所以走产品级确认框（audit-10 / P0 清单 #2）：失败信息留在
    // 框内，用户能就地重试，而不是把错误甩到页面另一处。
    confirmAction({
      title: "关闭这个待支付订单？",
      description:
        "如已扫码付款，请先等待到账。关闭后订单不可恢复，历史记录会保留。",
      level: "acknowledge",
      confirmLabel: "关闭订单",
      onConfirm: async () => {
        busyRef.current = true;
        setBusy(true);
        setRecordsError("");
        try {
          await customerCloseRechargeOrder(await credential(), orderNo);
          setNotice("待支付订单已关闭，历史记录已保留。");
          refreshData();
        } finally {
          busyRef.current = false;
          setBusy(false);
        }
      },
    });
  }
  async function saveProfile(event: FormEvent) {
    event.preventDefault();
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await account.onUpdateProfile(displayName.trim());
      account.onProfileUpdated(result);
      setNotice("个人资料已保存。");
    } catch (cause) {
      setError(message(cause));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }
  async function toggleNotifications() {
    if (notifications === null || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setPreferencesError("");
    try {
      const next = !notifications;
      await updateStudioNotificationPreferences(next);
      setNotifications(next);
    } catch (cause) {
      setPreferencesError(message(cause));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }
  async function logout() {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    try {
      await account.onLogout();
    } catch (cause) {
      setError(message(cause));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }
  const retryButton = (
    <button type="button" onClick={refreshData}>
      重新加载
    </button>
  );
  const tokenPanel = (
    <section className="uc-card uc-tokens">
      <header>
        <div>
          <h2>
            我的 Token{" "}
            <small>
              {tokens
                ? `${tokens.filter((item) => !item.revoked_at).length} 个有效 Token`
                : "读取中"}
            </small>
          </h2>
          <p>所有 Token 共用账号积分，消费统一计入当前账号。</p>
        </div>
        <button
          type="button"
          className="uc-outline"
          onClick={() => {
            setTokenError("");
            setTokenName("");
            setMutation({ kind: "create" });
          }}
        >
          <Icon name="plus" />
          新建 Token
        </button>
      </header>
      {tokenError && !mutation && (
        <div role="alert" className="uc-error">
          {tokenError}
          {retryButton}
        </div>
      )}
      <div className="uc-table-scroll">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>Token（已隐藏）</th>
              <th>状态 / 最近使用</th>
              <th>累计消费</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {tokens?.map((item) => (
              <tr key={item.id}>
                <td>
                  <strong>{item.label}</strong>
                  {item.is_default && <span className="uc-tag">自动生成</span>}
                </td>
                <td>
                  <code>xsk_live_{item.key_prefix}_••••</code>
                  <small>凭据版本 {item.credential_version}</small>
                </td>
                <td>
                  <span className={item.revoked_at ? "uc-muted" : "uc-valid"}>
                    {item.revoked_at ? "已撤销" : "有效"}
                  </span>
                  <small>{date(item.last_used_at)}</small>
                </td>
                <td>
                  {item.total_consumed_credits.toLocaleString("zh-CN")} 积分
                </td>
                <td>
                  <div className="uc-row-actions">
                    <button
                      type="button"
                      disabled={!!item.revoked_at || busy}
                      onClick={() => {
                        setTokenError("");
                        setMutation({ kind: "rotate", token: item });
                      }}
                    >
                      <Icon name="refresh" />
                      更新
                    </button>
                    <button
                      type="button"
                      className="uc-danger"
                      disabled={!!item.revoked_at || busy}
                      onClick={() => {
                        setTokenError("");
                        setMutation({ kind: "revoke", token: item });
                      }}
                    >
                      <Icon name="close" />
                      撤销
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {!tokens?.length && (
              <tr>
                <td colSpan={5}>
                  {tokenError
                    ? "Token 暂未读取成功"
                    : tokens
                      ? "暂无 Token"
                      : "正在读取 Token…"}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <p className="uc-footnote">
        <Icon name="info" />
        完整 Token 仅在生成或更新时显示，请及时保存。更新会保留历史版本的消费。
      </p>
    </section>
  );
  const togglePair = (operationId: string) => {
    setExpandedPairs((current) => {
      const next = new Set(current);
      if (next.has(operationId)) {
        next.delete(operationId);
      } else {
        next.add(operationId);
      }
      return next;
    });
  };

  const taskDetailButton = (item: WalletTransaction) =>
    item.generation_batch_id || item.oral_task_id ? (
      <button
        type="button"
        onClick={() =>
          navigate("task-detail", {
            selectedTaskId: item.oral_task_id
              ? `oral-${item.oral_task_id}`
              : item.generation_batch_id || undefined,
            selectedTaskKind: item.oral_task_id
              ? "oral_task"
              : "generation_batch",
            selectedTaskBackendId:
              item.oral_task_id || item.generation_batch_id || undefined,
            returnTo: "profile",
          })
        }
      >
        查看任务
      </button>
    ) : null;

  const ledgerRowCells = (item: WalletTransaction) => (
    <>
      <td>{date(item.created_at)}</td>
      <td>
        {LEDGER_TYPE_LABEL[item.type]}
        <small>{businessDescription(item)}</small>
        <TransactionPricingBreakdown
          credential={credential}
          transaction={item}
        />
        {taskDetailButton(item)}
      </td>
      <td>{ledgerSource(item)}</td>
      <td>{signedCredits(item.available_delta)}</td>
      <td>{signedCredits(item.reserved_delta)}</td>
    </>
  );

  const recordPanel = (
    <section className="uc-card">
      <header>
        <h2>消费与积分流水</h2>
      </header>
      <div className="uc-record-filters">
        <label>
          消费来源
          <select
            value={filters.source}
            onChange={(event) => updateFilter("source", event.target.value)}
          >
            <option value="">全部来源</option>
            <option value="session">软件操作</option>
            <option value="historical">历史来源未记录</option>
            {tokens?.map((token) => (
              <option
                key={token.token_group_id}
                value={`token:${token.token_group_id}`}
              >
                {token.label || "未命名 Token"}（含历史版本）
              </option>
            ))}
          </select>
        </label>
        <label>
          流水类型
          <select
            value={filters.type}
            onChange={(event) => updateFilter("type", event.target.value)}
          >
            <option value="">全部类型</option>
            <option value="SETTLE">最终消费</option>
            <option value="RESERVE">任务预扣</option>
            <option value="RELEASE">积分退回</option>
            <option value="CHARGE">积分入账</option>
            <option value="CONVERSION">历史积分转换</option>
          </select>
        </label>
        <label>
          业务
          <select
            value={filters.business}
            onChange={(event) => updateFilter("business", event.target.value)}
          >
            <option value="">全部业务</option>
            <option value="video">视频生成</option>
            <option value="oral">数字人口播</option>
            <option value="character">人物形象及任务图片</option>
            <option value="first_frame">首帧图片</option>
            <option value="analysis">视频分析</option>
            <option value="rewrite">文案改写</option>
            <option value="asr">语音转写</option>
            <option value="link_resolution">链接解析</option>
            <option value="avatar_clone">口播分身创建</option>
            <option value="voice_clone">声音克隆</option>
            <option value="viral_data">爆款视频数据请求</option>
            <option value="recharge">充值 / 赠送</option>
          </select>
        </label>
        <label>
          开始日期
          <input
            type="date"
            value={filters.start}
            onChange={(event) => updateFilter("start", event.target.value)}
          />
        </label>
        <label>
          结束日期
          <input
            type="date"
            value={filters.end}
            onChange={(event) => updateFilter("end", event.target.value)}
          />
        </label>
      </div>
      {recordsError && (
        <div role="alert" className="uc-error">
          {recordsError}
          {retryButton}
        </div>
      )}
      <div className="uc-table-scroll">
        <table>
          <thead>
            <tr>
              <th>时间（北京时间）</th>
              <th>业务</th>
              <th>来源</th>
              <th>可用积分变化</th>
              <th>待结算变化</th>
            </tr>
          </thead>
          <tbody>
            {groupLedgerRows(transactionPage?.items ?? []).map((entry) => {
              if (entry.kind === "row") {
                return <tr key={entry.row.id}>{ledgerRowCells(entry.row)}</tr>;
              }
              const { pair } = entry;
              // 摘要行落在组内最新一行的时间上，业务描述取自同一行。
              const latest = pair.rows[pair.rows.length - 1];
              const expanded = expandedPairs.has(pair.operationId);
              return (
                <Fragment key={`pair-${pair.operationId}`}>
                  <tr className="ledger-pair-parent">
                    <td>{date(latest.created_at)}</td>
                    <td>
                      {PAIR_STATE_LABEL[pair.state]}
                      <small>{businessDescription(latest)}</small>
                      <LedgerPairingSummary
                        pair={pair}
                        expanded={expanded}
                        onToggle={() => togglePair(pair.operationId)}
                      />
                    </td>
                    <td>{ledgerSource(latest)}</td>
                    <td>{signedCredits(netAvailableDelta(pair.rows))}</td>
                    <td>{signedCredits(netReservedDelta(pair.rows))}</td>
                  </tr>
                  {expanded &&
                    pair.rows.map((row) => (
                      <tr key={row.id} className="ledger-pair-child">
                        {ledgerRowCells(row)}
                      </tr>
                    ))}
                </Fragment>
              );
            })}
            {!transactionPage?.items.length && (
              <tr>
                <td colSpan={5}>
                  {recordsError
                    ? "记录暂未读取成功"
                    : recordsBusy
                      ? "正在读取记录…"
                      : "暂无积分流水，开始创作后会在这里记录。"}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {tab === "consumption" && (
        <>
          <p className="uc-footnote">
            预扣与退回不计入累计消费；历史未记录 Token
            来源的消费仅计入账号总额。
          </p>
          <Pagination
            disabled={recordsBusy}
            limit={20}
            offset={offset}
            total={transactionPage?.total ?? 0}
            onPageChange={setOffset}
          />
        </>
      )}
    </section>
  );
  return (
    <section className="uc-center" aria-label="用户中心">
      <header className="uc-brandbar">
        <div className="uc-brand">
          <BrandIdentity />
        </div>
        <nav aria-label="账号导航">
          <button type="button" onClick={() => navigate("workbench")}>
            <Icon name="home" />
            返回主界面
          </button>
          <span className="uc-top-name">
            <span className="uc-avatar uc-avatar-small">
              {name.slice(0, 1)}
            </span>
            {name}
          </span>
          <button type="button" disabled={busy} onClick={() => void logout()}>
            退出登录
          </button>
        </nav>
      </header>
      <div className="uc-content">
        <header className="uc-title">
          <h1>用户中心</h1>
          <p>管理账号、积分与创作服务</p>
        </header>
        {(error || account.profileLoadError) && (
          <div role="alert" className="uc-error">
            {error || account.profileLoadError}
            <button
              type="button"
              onClick={() => {
                refreshData();
                void account
                  .onRefreshProfile()
                  .catch((cause) => setError(message(cause)));
              }}
            >
              重试加载账号
            </button>
          </div>
        )}
        {notice && (
          <p className="uc-notice" role="status">
            {notice}
            <button type="button" onClick={() => setNotice("")}>
              关闭提示
            </button>
          </p>
        )}
        <section className="uc-identity uc-card" aria-label="账号身份">
          <div className="uc-person">
            <span className="uc-avatar">{name.slice(0, 1)}</span>
            <div>
              <h2>
                {name}
                <button type="button" onClick={() => selectTab("settings")}>
                  <Icon name="pen" />
                  编辑资料
                </button>
              </h2>
              {accountType && (
                <p className="uc-identity-badge">
                  <span
                    className={isMasterAccount ? "uc-tag" : "uc-tag uc-tag-sub"}
                  >
                    {isMasterAccount ? "母账号" : "子账号"}
                  </span>
                  {!isMasterAccount && (
                    <small>
                      所属母账号：
                      {parentDisplayName ??
                        (profile === null ? "读取中" : "未记录")}
                    </small>
                  )}
                </p>
              )}
              <dl>
                <div>
                  <dt>用户名</dt>
                  <dd>{profile?.username ?? user.username}</dd>
                </div>
                <div>
                  <dt>账号 ID</dt>
                  <dd>{profile?.user_id ?? "读取中"}</dd>
                </div>
                <div>
                  <dt>注册时间</dt>
                  <dd>
                    {profile?.joined_at
                      ? date(profile.joined_at).split(" ")[0]
                      : "读取中"}
                  </dd>
                </div>
                {/* 设备数取 profile.device_slots_used：本页不读设备接口，
                    所以不出现槽位列表/解绑那类设备管理能力。 */}
                <div>
                  <dt>已绑定设备</dt>
                  <dd>{deviceSlotsText}</dd>
                </div>
                {/* 子账号的月度额度：没有这一面，用户只能等生成失败才知道上限。 */}
                {isSubAccount && (
                  <>
                    <div>
                      <dt>本月额度</dt>
                      <dd>{quotaCapText}</dd>
                    </div>
                    <div>
                      <dt>本月已用</dt>
                      <dd>{quotaUsedText}</dd>
                    </div>
                    <div>
                      <dt>本月剩余</dt>
                      <dd>
                        {quotaRemainingText}
                        {quotaPending === null &&
                          quotaPercent !== null &&
                          quotaLevel && (
                            <small className={`uc-quota-${quotaLevel}`}>
                              {quotaPercent}%
                              {quotaLevel === "exhausted" ? " · 已用尽" : ""}
                            </small>
                          )}
                      </dd>
                    </div>
                  </>
                )}
              </dl>
            </div>
          </div>
          <div className="uc-balance">
            <h3>账户可用积分</h3>
            <div>
              <strong>
                {summary
                  ? summary.available_credits.toLocaleString("zh-CN")
                  : error
                    ? "读取失败"
                    : "—"}
              </strong>
              <span>积分</span>
              <button
                type="button"
                className="uc-primary"
                onClick={() => setRecharge(true)}
              >
                充值积分
              </button>
            </div>
            <p>
              待结算{" "}
              <b>{summary?.reserved_credits.toLocaleString("zh-CN") ?? "—"}</b>{" "}
              积分{" "}
              <span>
                累计消费{" "}
                <b>
                  {summary?.total_consumed_credits.toLocaleString("zh-CN") ??
                    "—"}
                </b>{" "}
                积分
              </span>
            </p>
          </div>
        </section>
        <div className="uc-tabs" role="tablist" aria-label="用户中心功能">
          {visibleTabs.map(([id, label]) => (
            <button
              type="button"
              role="tab"
              id={`uc-tab-${id}`}
              aria-controls="uc-tab-panel"
              aria-selected={tab === id}
              key={id}
              onClick={() => selectTab(id)}
              tabIndex={tab === id ? 0 : -1}
              onKeyDown={(event) => {
                const index = visibleTabs.findIndex(([value]) => value === id);
                const next =
                  event.key === "ArrowRight"
                    ? (index + 1) % visibleTabs.length
                    : event.key === "ArrowLeft"
                      ? (index + visibleTabs.length - 1) % visibleTabs.length
                      : event.key === "Home"
                        ? 0
                        : event.key === "End"
                          ? visibleTabs.length - 1
                          : null;
                if (next === null) return;
                event.preventDefault();
                selectTab(visibleTabs[next][0]);
                document
                  .getElementById(`uc-tab-${visibleTabs[next][0]}`)
                  ?.focus();
              }}
            >
              {label}
            </button>
          ))}
        </div>
        <div
          id="uc-tab-panel"
          role="tabpanel"
          aria-labelledby={`uc-tab-${tab}`}
        >
          {tab === "tokens" && tokenPanel}
          {tab === "consumption" && recordPanel}
          {tab === "prices" && <CustomerPricesPage credential={credential} />}
          {tab === "recharge" && (
            <section className="uc-card">
              <header>
                <h2>充值记录</h2>
              </header>
              {recordsError && (
                <div role="alert" className="uc-error">
                  {recordsError}
                  {retryButton}
                </div>
              )}
              <div className="uc-table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>订单号 / 创建时间</th>
                      <th>支付金额</th>
                      <th>订单积分</th>
                      <th>状态</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {orderPage?.items.map((item) => (
                      <tr key={item.order_no}>
                        <td>
                          {item.order_no}
                          <small>{date(item.created_at)}</small>
                        </td>
                        <td>¥{(item.amount_fen / 100).toFixed(2)}</td>
                        <td>{item.credits} 积分</td>
                        <td>
                          {
                            {
                              PENDING: "待支付",
                              PAID: "已到账",
                              CLOSED: "已关闭",
                              FAILED: "支付失败",
                            }[item.status]
                          }
                        </td>
                        <td>
                          {item.status === "PENDING" ? (
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => void closeOrder(item.order_no)}
                            >
                              关闭待支付订单
                            </button>
                          ) : (
                            "—"
                          )}
                        </td>
                      </tr>
                    ))}
                    {!orderPage?.items.length && (
                      <tr>
                        <td colSpan={5}>
                          {recordsError
                            ? "订单暂未读取成功"
                            : recordsBusy
                              ? "正在读取充值记录…"
                              : "暂无充值记录"}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
              <p className="uc-footnote">
                待支付订单不会增加积分，到账状态以服务端核验结果为准。
              </p>
              <Pagination
                disabled={recordsBusy}
                limit={20}
                offset={offset}
                total={orderPage?.total ?? 0}
                onPageChange={setOffset}
              />
            </section>
          )}
          {tab === "publishing" && (
            <section className="uc-card">
              <PublishAccountsPanel notify={notify} />
            </section>
          )}
          {tab === "sub-accounts" && canManageSubAccounts && (
            <SubAccountManagementPage
              store={account.store}
              onSessionExpired={account.onSessionExpired}
            />
          )}
          {tab === "settings" && (
            <div className="uc-settings">
              <AccountPasswordSetup
                credential={credential}
                onComplete={() => {
                  setRefresh((value) => value + 1);
                  void credential()
                    .then(customerGetProfile)
                    .then(account.onProfileUpdated)
                    .catch((cause) => setError(message(cause)));
                }}
              />
              <section className="uc-card">
                <h2>账号资料</h2>
                <form onSubmit={saveProfile}>
                  <label htmlFor="uc-name">显示名称</label>
                  <input
                    id="uc-name"
                    value={displayName}
                    maxLength={50}
                    required
                    onChange={(event) => setDisplayName(event.target.value)}
                  />
                  <p>
                    用户名 {profile?.username ?? user.username} ·
                    昵称用于软件内展示
                  </p>
                  <button
                    type="submit"
                    className="uc-primary"
                    disabled={busy || !displayName.trim()}
                  >
                    保存资料
                  </button>
                </form>
              </section>
              <section className="uc-card">
                <h2>通知偏好</h2>
                <div className="uc-preference">
                  <Icon name="bell" />
                  <div>
                    <strong>任务通知</strong>
                    <p>接收平台公告和任务完成提醒</p>
                  </div>
                  <button
                    type="button"
                    className="uc-switch"
                    role="switch"
                    aria-label="任务通知"
                    aria-checked={notifications === true}
                    disabled={notifications === null || busy}
                    onClick={() => void toggleNotifications()}
                  >
                    <span />
                  </button>
                </div>
                {preferencesError && (
                  <div role="alert" className="uc-error">
                    {preferencesError}
                    {retryButton}
                  </div>
                )}
              </section>
              <SecuritySection
                activeTokenCount={
                  // 列表没读到就不要报「0 枚」——那会和实际撤销掉的枚数对不上。
                  tokens === null
                    ? null
                    : tokens.filter((item) => !item.revoked_at).length
                }
                credential={credential}
                onSessionsEnded={async (reason) => {
                  // 改密/下线以后当前会话已经死了：先留一句说明，再走正常登出
                  // 流程回登录页（本地凭据由会话钩子清理）。
                  setNotice(reason);
                  await account.onLogout();
                }}
                onTokensRevoked={refreshData}
              />
            </div>
          )}
        </div>
      </div>
      {(mutation || secret) && (
        <dialog
          className="uc-dialog"
          ref={dialog}
          aria-label={
            secret
              ? "保存完整 Token"
              : mutation?.kind === "create"
                ? "新建 Token"
                : mutation?.kind === "rotate"
                  ? "更新 Token"
                  : "撤销 Token"
          }
          onCancel={(event) => {
            if (busy) event.preventDefault();
            else {
              setMutation(null);
              setSecret(null);
            }
          }}
        >
          {secret ? (
            <>
              <h2>请保存你的 Token</h2>
              <p>完整凭据仅在这次操作中显示，请妥善保存。</p>
              <label htmlFor="uc-secret">完整 Token</label>
              <input
                id="uc-secret"
                readOnly
                value={secret.plaintext ?? ""}
                autoComplete="off"
                spellCheck={false}
              />
              <div className="uc-dialog-actions">
                <button
                  type="button"
                  className="uc-primary"
                  onClick={() => {
                    void navigator.clipboard
                      .writeText(secret.plaintext ?? "")
                      .then(
                        () => setNotice("Token 已复制。"),
                        () => setNotice("复制失败，请选中文本手动复制。"),
                      );
                  }}
                >
                  复制 Token
                </button>
                <button type="button" onClick={() => setSecret(null)}>
                  已保存，关闭
                </button>
              </div>
            </>
          ) : (
            <form onSubmit={act}>
              <h2>
                {mutation?.kind === "create"
                  ? "新建 Token"
                  : mutation?.kind === "rotate"
                    ? "更新 Token"
                    : "撤销 Token"}
              </h2>
              {mutation?.kind === "create" ? (
                <>
                  <label htmlFor="uc-token-name">Token 名称</label>
                  <input
                    id="uc-token-name"
                    maxLength={100}
                    required
                    value={tokenName}
                    onChange={(event) => setTokenName(event.target.value)}
                    placeholder="例如：工作电脑、自动脚本"
                  />
                </>
              ) : (
                <p>
                  {mutation?.token.label}：
                  {mutation?.kind === "rotate"
                    ? "更新后旧凭据立即失效，请同步替换使用它的程序；历史消费与已受理任务保留。"
                    : "撤销后这枚 Token 无法再发起请求，其他 Token 和登录设备不受影响。"}
                </p>
              )}
              {tokenError && (
                <p role="alert" className="uc-error">
                  {tokenError}
                </p>
              )}
              <div className="uc-dialog-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setMutation(null)}
                >
                  取消
                </button>
                <button
                  type="submit"
                  className={
                    mutation?.kind === "revoke" ? "uc-danger" : "uc-primary"
                  }
                  disabled={busy}
                >
                  {busy
                    ? "正在处理…"
                    : mutation?.kind === "create"
                      ? "确认新建"
                      : mutation?.kind === "rotate"
                        ? "确认更新"
                        : "确认撤销"}
                </button>
              </div>
            </form>
          )}
        </dialog>
      )}
      {confirmDialog}
      <CustomerRechargeDialog
        isOpen={recharge}
        onClose={() => setRecharge(false)}
        onOrderCreated={refreshData}
        onPaid={() => {
          refreshData();
          void account
            .onRefreshProfile()
            .catch((cause) => setError(message(cause)));
        }}
        onSessionExpired={account.onSessionExpired}
        store={account.store}
      />
    </section>
  );
}
