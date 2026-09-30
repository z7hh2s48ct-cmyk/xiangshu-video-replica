import {
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Pagination } from "../admin/ui/Pagination";
import {
  type CreatedCustomerApiKey,
  CustomerApiError,
  type CustomerApiKey,
  type CustomerCenterSummary,
  type CustomerSubAccount,
  type CustomerWalletLedgerPage,
  customerCloseRechargeOrder,
  customerCreateApiKey,
  customerExportWalletTransactionsCSV,
  customerGetCenterSummary,
  customerGetProfile,
  customerInitializeDefaultApiKey,
  customerListApiKeys,
  customerListRechargeOrders,
  customerListSubAccounts,
  customerListWalletLedger,
  customerRevokeApiKey,
  customerRotateApiKey,
  getStudioNotificationPreferences,
  type RechargeOrderPage,
  updateStudioNotificationPreferences,
  type WalletTransaction,
} from "../api";
import { BrandIdentity } from "../BrandIdentity";
import { useStudio } from "../studio/context";
import { PublishAccountsPanel } from "../studio/MainPages";
import { Icon } from "../studio/ui";
import type { WorkspaceShellProps } from "../workspace-shell";
import { AccountPasswordSetup } from "./AccountPasswordSetup";
import { BUSINESS_LABEL } from "./businessLabels";
import { useCustomerConfirm } from "./CustomerConfirmDialog";
import { CustomerPricesPage } from "./CustomerPricesPage";
import { CustomerRechargeDialog } from "./CustomerRechargeDialog";
import { DeviceSection } from "./DeviceSection";
import { EmailBindingSection } from "./EmailBindingSection";
import { ErrorNote } from "./ErrorNote";
import { HelpDialog, TermHint } from "./HelpDialog";
import { LedgerEntryTable } from "./LedgerEntryTable";
import { LedgerOutcomeChips } from "./LedgerOutcomeChips";
import { HELD_CREDITS_LABEL, HOLD_EXPLANATION } from "./ledgerVocabulary";
import { useOnboarding } from "./OnboardingTour";
import { quotaPercentUsed, quotaState } from "./quotaViz";
import { RetryButton } from "./RetryButton";
import { SecuritySection } from "./SecuritySection";
import { SubAccountManagementPage } from "./SubAccountManagementPage";
import { tokenIdleDays, tokenUsageShares } from "./tokenUsage";
import type { CustomerStoredIdentity } from "./useCustomerSession";
import "./customer-center.css";

/** 页签全集。渲染前按**两条**规则过滤成 `visibleTabs`：
 * - 子账号管理对普通子账号不可见（母账号与获授权 SUB_ADMIN 可见）；
 * - 设备管理是桌面客户端独有：心跳/租约/配对在 web 端只会得到一张说不清状态的卡片
 *   （审计方案 A 的 IA）。
 * 保留全集、渲染前再过滤，页签条与键盘导航才都和可见页签一致。 */
const ALL_TABS = [
  ["tokens", "Token 管理"],
  ["consumption", "消费记录"],
  ["recharge", "充值记录"],
  ["prices", "接口价格"],
  ["publishing", "发布账号"],
  ["devices", "设备管理"],
  ["sub-accounts", "子账号管理"],
  ["settings", "账号设置"],
] as const;
type Tab = (typeof ALL_TABS)[number][0];
const DESKTOP_ONLY_TABS: ReadonlySet<Tab> = new Set<Tab>(["devices"]);
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
// 记录错误对象本身（分类提示要用它的 status/code），文案照旧由调用方决定。
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

/** 消费记录筛选变化后等多久再发请求（P1#16：避免连续切换打出一串请求）。 */
const LEDGER_FILTER_DEBOUNCE_MS = 300;

// 与服务端一致的名字长度上限（`users.display_name` / Token label）。
const NAME_MAX_LENGTH = 50;
const TOKEN_NAME_MAX_LENGTH = 100;

const BUSINESS_GROUPS: ReadonlyArray<readonly [string, readonly string[]]> = [
  ["视频生成", ["video", "first_frame"]],
  ["数字人", ["oral", "avatar_clone", "voice_clone"]],
  ["内容处理", ["analysis", "rewrite", "asr", "link_resolution"]],
  ["提示词", ["prompt_optimize"]],
  ["资产与账务", ["character", "viral_data", "recharge"]],
];

type LedgerFilters = {
  source: string;
  /** 结果筛选（进行中 / 已完成 / 有退回 / 入账与调整），空串为全部。 */
  outcome: string;
  business: string;
  start: string;
  end: string;
  subAccount: string;
};

/**
 * 消费记录的筛选 → 查询参数。
 *
 * **列表与 CSV 导出共用这一个函数**：两个入口各拼一套参数，迟早会出现「导出的是
 * 全部、界面显示的是筛选后的」这类对不上账的问题。
 */
function ledgerFilterParams(
  filters: LedgerFilters,
  options: { summarize?: boolean } = {},
): Record<string, string> {
  const params: Record<string, string> = {};
  if (filters.source.startsWith("token:")) {
    params.token_group_id = filters.source.slice(6);
  } else if (filters.source) {
    params.auth_source = filters.source;
  }
  if (filters.outcome) params.outcome = filters.outcome;
  if (filters.business) params.business = filters.business;
  if (filters.subAccount) params.sub_account_id = filters.subAccount;
  if (filters.start) {
    params.started_at = new Date(
      `${filters.start}T00:00:00+08:00`,
    ).toISOString();
  }
  if (filters.end) {
    params.ended_at = new Date(
      new Date(`${filters.end}T00:00:00+08:00`).getTime() + 86_400_000,
    ).toISOString();
  }
  if (options.summarize) params.group_by_sub_account = "true";
  return params;
}

export function CustomerCenterPage({
  account,
}: {
  account: NonNullable<WorkspaceShellProps["customerAccount"]>;
}) {
  const { navigate, notify, user } = useStudio();
  const { confirm: confirmAction, dialog: confirmDialog } =
    useCustomerConfirm();
  // 读不到平台就按 web 处理：少一个页签不会误操作，多一个会误导。
  const isDesktopClient = useMemo(() => {
    try {
      return account.store.devicePlatform() !== "browser";
    } catch {
      return false;
    }
  }, [account.store]);
  // 首访引导（方案 G / P2#14）：按账号记「看过」，换账号会重新引导一次。
  const { tour } = useOnboarding(account.profile?.user_id ?? "anonymous");
  const [help, setHelp] = useState(false);
  const [tab, setTab] = useState<Tab>("tokens");
  const [summary, setSummary] = useState<CustomerCenterSummary | null>(null);
  const [tokens, setTokens] = useState<CustomerApiKey[] | null>(null);
  const [error, setError] = useState("");
  // 分类提示要看错误对象的 status/code，而状态里只存了文案；这里留一份原始对象。
  const [lastError, setLastError] = useState<unknown>(null);
  // 记录错误对象本身（分类提示要用 status/code），文案照旧。
  const captureError = useCallback((cause: unknown) => {
    setLastError(cause);
    setError(message(cause));
  }, []);
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
  // 上线前检查 P2-6：allow_api_keys=false 的子账号没有 default key 也建不了，
  // 初始化恒 403——记住这个终局，本次挂载内不再重放注定失败的 POST
  // （旧实现每次进 Token 页签/刷新都打一发 403）。
  const defaultForbidden = useRef(false);
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
    useState<CustomerWalletLedgerPage | null>(null);
  const [orderPage, setOrderPage] = useState<RechargeOrderPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [filters, setFilters] = useState<LedgerFilters>({
    source: "",
    outcome: "",
    business: "",
    start: "",
    end: "",
    subAccount: "",
  });
  // P1#16：筛选变化不立刻清空列表——旧数据留在屏幕上比闪成「正在读取记录…」好读得多；
  // 真正的新结果由下面带 debounce 的 effect 换上。
  function updateFilter(key: keyof LedgerFilters, value: string) {
    setFilters((previous) => ({ ...previous, [key]: value }));
    // 切换子账号/汇总维度时旧的摘要已经不适用，清掉避免误读。
    // 这一句必须在 updater **外面**：React 会在有 pending update 时于渲染阶段
    // 调用 updater（StrictMode 下还调两次），在里面改另一个 state 等于渲染期更新。
    if (key === "subAccount") setTransactionPage(null);
    setOffset(0);
  }
  function clearFilters() {
    setFilters({
      source: "",
      outcome: "",
      business: "",
      start: "",
      end: "",
      subAccount: "",
    });
    setSummarize(false);
    setOffset(0);
    setTransactionPage(null);
  }
  const [summarize, setSummarize] = useState(false);
  const [subAccounts, setSubAccounts] = useState<CustomerSubAccount[] | null>(
    null,
  );
  const [isExporting, setIsExporting] = useState(false);
  const anyFilterActive =
    filters.source !== "" ||
    filters.outcome !== "" ||
    filters.business !== "" ||
    filters.start !== "" ||
    filters.end !== "" ||
    filters.subAccount !== "" ||
    summarize;
  // P2#19：重名只提示不拦截——服务端允许同名，这里只是让用户有机会区分开。
  const duplicateTokenLabel = (tokens ?? []).some(
    (item) => !item.revoked_at && item.label === tokenName.trim(),
  );
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
  // 唯一的可见页签口径：两边的过滤条件合在这里，页签条 / 键盘导航 / 数字键都用它。
  // （合并前的两份过滤曾各改一半——那样 web 端会漏出设备管理页签。）
  const visibleTabs = useMemo(
    () =>
      ALL_TABS.filter(
        ([id]) =>
          (!DESKTOP_ONLY_TABS.has(id) || isDesktopClient) &&
          (id !== "sub-accounts" || canManageSubAccounts),
      ),
    [isDesktopClient, canManageSubAccounts],
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
              if (active) captureError(cause);
            }),
          (async () => {
            try {
              let result = await customerListApiKeys(auth);
              if (
                !defaultForbidden.current &&
                (defaultPending.current ||
                  !result.items.some((item) => item.is_default))
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
                // 403 = 该账号无权建 API key（如 allow_api_keys=false 的子账号），
                // 属会话期终局：重试同一 POST 只会再 403。
                if (cause.status === 403) defaultForbidden.current = true;
              }
              if (active) setTokenError(message(cause));
            }
          })(),
        ]);
      })
      .catch((cause) => {
        if (active) captureError(cause);
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
    // 只负责充值记录；消费记录由下面带 debate 的 effect 负责（两者筛选维度不同，
    // 合在一起会让「改一个筛选把另一个页签也重取一遍」）。
    if (tab !== "recharge") return;
    setRecordsError("");
    setRecordsBusy(true);
    void credential()
      .then(async (auth) => {
        const result = await customerListRechargeOrders(auth, {
          limit: 20,
          offset,
        });
        if (active) setOrderPage(result);
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
  }, [credential, tab, offset, refresh]);
  // P1#16：筛选/翻页不再一变就发请求——300ms 内的连续变更只发最后一次。
  // 旧的实现是每次 updateFilter 立刻打一次，还会把已渲染的行清空。
  // biome-ignore lint/correctness/useExhaustiveDependencies: Refresh explicitly invalidates cached results after writes or retry.
  useEffect(() => {
    if (tab !== "consumption") return;
    setRecordsError("");
    // 置位必须在 debounce **之前**：否则防抖窗口里页签显示空态（「暂无积分流水」）
    // 而不是「正在读取记录…」，底部分页也一直可点。下面 finally 清的就是这个标志。
    setRecordsBusy(true);
    let active = true;
    const timer = window.setTimeout(() => {
      void credential()
        .then(async (auth) => {
          const result = await customerListWalletLedger(auth, {
            limit: 20,
            offset,
            filters: ledgerFilterParams(filters, { summarize }),
          });
          if (active) setTransactionPage(result);
        })
        .catch((cause) => {
          if (active) setRecordsError(message(cause));
        })
        .finally(() => {
          if (active) setRecordsBusy(false);
        });
    }, LEDGER_FILTER_DEBOUNCE_MS);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [credential, tab, offset, refresh, filters, summarize]);
  // 子账号清单：母账号才需要它来筛选，读一次就够（失败静默——筛选是加分项，
  // 不该因为它读不到就打断消费记录主路径）。
  useEffect(() => {
    if (tab !== "consumption" || subAccounts !== null) return;
    let active = true;
    void credential()
      .then((auth) => customerListSubAccounts(auth))
      .then((items) => {
        if (active) setSubAccounts(items);
      })
      .catch(() => {
        if (active) setSubAccounts([]);
      });
    return () => {
      active = false;
    };
  }, [credential, tab, subAccounts]);
  function selectTab(value: Tab) {
    if (value !== tab) refreshData();
    setTab(value);
    setOffset(0);
    setRecordsError("");
  }
  function refreshData() {
    setRefresh((value) => value + 1);
  }
  async function exportLedger() {
    if (isExporting) return;
    setIsExporting(true);
    setRecordsError("");
    try {
      // 与列表共用同一个参数函数：导出的必须是「我正在看的这一份」。
      const { filename, text } = await customerExportWalletTransactionsCSV(
        await credential(),
        ledgerFilterParams(filters),
      );
      const url = URL.createObjectURL(
        new Blob([text], { type: "text/csv;charset=utf-8" }),
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setNotice(`已导出 ${filename}。`);
    } catch (cause) {
      setRecordsError(message(cause));
    } finally {
      setIsExporting(false);
    }
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
      captureError(cause);
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
      captureError(cause);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }
  const retryButton = <RetryButton onClick={refreshData} />;
  const tokenUsage = tokenUsageShares(tokens ?? []);
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
      <ul className="uc-token-list">
        {tokens?.map((item) => {
          const usage = tokenUsage.get(item.id);
          return (
            <li
              className={
                item.revoked_at ? "uc-token uc-token--revoked" : "uc-token"
              }
              key={item.id}
            >
              <div className="uc-token__head">
                <strong>{item.label}</strong>
                {item.is_default ? (
                  <span className="uc-tag">自动生成</span>
                ) : null}
                <span className={item.revoked_at ? "uc-muted" : "uc-valid"}>
                  {item.revoked_at ? "已撤销" : "有效"}
                </span>
              </div>
              <code>xsk_live_{item.key_prefix}_••••</code>
              <p className="uc-token__meta">
                第 {item.credential_version} 次更新 · 最近使用{" "}
                {date(item.last_used_at)}
              </p>
              {/* 机会点 2：主力占比可视化——让客户一眼看出哪个 Token 在花钱。 */}
              {(() => {
                const idleDays = item.revoked_at ? null : tokenIdleDays(item);
                return idleDays === null ? null : (
                  <p className="uc-token__idle" role="status">
                    {idleDays} 天未使用，可考虑撤销或更新
                  </p>
                );
              })()}
              <div className="uc-token__usage">
                <span>
                  累计消费 {item.total_consumed_credits.toLocaleString("zh-CN")}{" "}
                  积分
                </span>
                <span
                  className="uc-token__share"
                  role="img"
                  aria-label={`占账号累计消费 ${usage?.share ?? 0}%`}
                >
                  <i style={{ width: `${usage?.share ?? 0}%` }} />
                </span>
                <small>{usage?.share ?? 0}%</small>
              </div>
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
            </li>
          );
        })}
      </ul>
      {!tokens?.length ? (
        <div className="uc-token-empty" role="status">
          {tokenError ? (
            <p>Token 暂未读取成功：{tokenError}</p>
          ) : tokens ? (
            <>
              {/* 机会点 2 的空态引导：不再只写「暂无 Token」，而是说清它是什么、下一步做什么。 */}
              <h3>还没有 Token</h3>
              <p>
                Token
                是给你的程序调用平台接口用的凭据；完整值只在创建时显示一次，请立即保存。
              </p>
              <button
                className="uc-primary"
                type="button"
                onClick={() => {
                  setTokenError("");
                  setTokenName("");
                  setMutation({ kind: "create" });
                }}
              >
                创建第一个 Token
              </button>
            </>
          ) : (
            <p>正在读取 Token…</p>
          )}
        </div>
      ) : null}
      <p className="uc-footnote">
        <Icon name="info" />
        完整 Token 仅在生成或更新时显示，请及时保存。更新会保留历史版本的消费。
      </p>
    </section>
  );
  // 「查看任务」：由流水里带的批次 / 口播任务 ID 跳到任务详情，返回时回到用户中心。
  const openTask = (item: WalletTransaction) =>
    navigate("task-detail", {
      selectedTaskId: item.oral_task_id
        ? `oral-${item.oral_task_id}`
        : item.generation_batch_id || undefined,
      selectedTaskKind: item.oral_task_id ? "oral_task" : "generation_batch",
      selectedTaskBackendId:
        item.oral_task_id || item.generation_batch_id || undefined,
      returnTo: "profile",
    });

  const recordPanel = (
    <section className="uc-card">
      <header>
        <h2>消费与积分流水</h2>
        {/* P1#6：累计消费原先挤在首屏 hero 里，与「充值」抢焦点；它们是「看账」的
            信息，挪到看账的页签里更合适。这里并排放消费、退回与暂扣中三个数：
            用户只看到扣钱、看不到退回，是这块最常见的误解。 */}
        <section className="uc-record-totals" aria-label="流水总额">
          <span>
            累计消费{" "}
            <b>
              {summary?.total_consumed_credits.toLocaleString("zh-CN") ?? "—"}
            </b>{" "}
            积分
          </span>
          <span className="uc-record-totals__returned">
            累计退回{" "}
            <b>
              {summary?.total_returned_credits?.toLocaleString("zh-CN") ?? "—"}
            </b>{" "}
            积分
          </span>
          <span>
            <TermHint hint={HOLD_EXPLANATION} term={HELD_CREDITS_LABEL} />{" "}
            <b>{summary?.reserved_credits.toLocaleString("zh-CN") ?? "—"}</b>{" "}
            积分
          </span>
        </section>
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
            <option value="historical">早期版本消费</option>
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
          业务
          <select
            value={filters.business}
            onChange={(event) => updateFilter("business", event.target.value)}
          >
            <option value="">全部业务</option>
            {BUSINESS_GROUPS.map(([group, keys]) => (
              <optgroup key={group} label={group}>
                {keys.map((key) => (
                  <option key={key} value={key}>
                    {BUSINESS_LABEL[key] ?? key}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
        {subAccounts && subAccounts.length > 0 ? (
          <label>
            子账号
            <select
              value={filters.subAccount}
              onChange={(event) =>
                updateFilter("subAccount", event.target.value)
              }
            >
              <option value="">全部子账号</option>
              {subAccounts.map((sub) => (
                <option key={sub.id} value={sub.id}>
                  {sub.display_name || sub.username}
                </option>
              ))}
            </select>
          </label>
        ) : null}
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
      <div className="uc-record-actions">
        <label className="uc-record-actions__toggle">
          <input
            type="checkbox"
            checked={summarize}
            onChange={(event) => setSummarize(event.target.checked)}
          />
          按子账号汇总
        </label>
        <button
          type="button"
          disabled={isExporting}
          onClick={() => void exportLedger()}
        >
          {isExporting ? "正在导出…" : "导出 CSV"}
        </button>
        {anyFilterActive ? (
          <button type="button" onClick={clearFilters}>
            清除筛选
          </button>
        ) : null}
      </div>
      <LedgerOutcomeChips
        counts={transactionPage?.counts}
        value={filters.outcome}
        onChange={(value) => updateFilter("outcome", value)}
      />
      {summarize && transactionPage?.sub_account_summary?.length ? (
        <div className="uc-table-scroll">
          <table className="uc-responsive-table uc-summary-table">
            <caption className="uc-record-summary__caption">
              当前筛选下的子账号汇总（消费＝实扣的额度，退回＝返回名下的额度）
            </caption>
            <thead>
              <tr>
                <th>子账号</th>
                <th>消费</th>
                <th>退回</th>
                <th>条数</th>
              </tr>
            </thead>
            <tbody>
              {transactionPage.sub_account_summary.map((row) => (
                <tr key={row.sub_account_id}>
                  <td>{row.sub_account_name}</td>
                  <td>{row.debit_total} 积分</td>
                  <td>{row.credit_total} 积分</td>
                  <td>{row.transaction_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {recordsError && (
        <div role="alert" className="uc-error">
          {recordsError}
          {retryButton}
        </div>
      )}
      <LedgerEntryTable
        credential={credential}
        empty={
          recordsError
            ? "记录暂未读取成功"
            : recordsBusy
              ? "正在读取记录…"
              : "暂无积分流水，开始创作后会在这里记录。"
        }
        entries={transactionPage?.items ?? []}
        onOpenTask={openTask}
      />
      {tab === "consumption" && (
        <>
          <p className="uc-footnote">
            暂扣与退回不计入累计消费，累计消费只统计实扣；历史未记录 Token
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
          <button type="button" onClick={() => setHelp(true)}>
            <Icon name="info" />
            帮助
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
          <ErrorNote
            error={lastError}
            message={error || account.profileLoadError}
            onRetry={() => {
              refreshData();
              void account
                .onRefreshProfile()
                .catch((cause) => captureError(cause));
            }}
            retryLabel="重试加载账号"
          />
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
                // P2#17：数字键直达页签（1..N）。键盘用户不必按方向键一路挪过去。
                // 一律走 visibleTabs：数字键与方向键都只能在**可见**页签里移动，
                // 否则会落到被过滤掉（web 端的设备管理 / 普通子账号的子账号管理）的页签上。
                const digit = Number(event.key);
                if (
                  Number.isInteger(digit) &&
                  digit >= 1 &&
                  digit <= visibleTabs.length
                ) {
                  event.preventDefault();
                  const target = visibleTabs[digit - 1][0];
                  selectTab(target);
                  document.getElementById(`uc-tab-${target}`)?.focus();
                  return;
                }
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
                <table className="uc-responsive-table uc-orders-table">
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
              {/* 审计 P1#11：这一页此前只有一行组件，客户不知道「绑了之后能干什么」。 */}
              <header className="uc-publishing-head">
                <h2>发布账号</h2>
                <p>
                  这里绑定的是各平台的登录状态（抖音 / 视频号 /
                  小红书）。绑定后回到主界面的
                  「发布」页，就能把做好的成片直接投递出去；解绑后对应平台需要重新扫码登录。
                </p>
                <button
                  className="uc-primary"
                  type="button"
                  onClick={() => navigate("publishing")}
                >
                  去发布成片
                </button>
              </header>
              <PublishAccountsPanel notify={notify} />
            </section>
          )}
          {tab === "devices" && (
            <DeviceSection
              deviceError={account.deviceError}
              devices={account.devices}
              onApprovePairing={account.onApprovePairing}
              onDismissPairing={account.onDismissPairing}
              onManualHeartbeat={account.onManualHeartbeat}
              onPairDevice={account.onPairDevice}
              onRecharge={() => setRecharge(true)}
              onRefreshDevices={account.onRefreshDevices}
              onUnbind={account.onUnbind}
              scope={account.profile?.user_id ?? "anonymous"}
              sessionRuntime={account.sessionRuntime}
            />
          )}
          {tab === "sub-accounts" && canManageSubAccounts && (
            <SubAccountManagementPage
              store={account.store}
              onSessionExpired={account.onSessionExpired}
              isMasterCaller={isMasterAccount}
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
                    .catch((cause) => captureError(cause));
                }}
              />
              <section className="uc-card">
                <h2>账号资料</h2>
                <form onSubmit={saveProfile}>
                  <label htmlFor="uc-name">显示名称</label>
                  <input
                    id="uc-name"
                    value={displayName}
                    maxLength={NAME_MAX_LENGTH}
                    required
                    onChange={(event) => setDisplayName(event.target.value)}
                  />
                  <p>
                    用户名 {profile?.username ?? user.username} ·
                    建议填写公司名称，管理后台按公司名称识别您的账号
                    <span className="uc-count">
                      {displayName.length}/{NAME_MAX_LENGTH}
                    </span>
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
              <EmailBindingSection credential={credential} />
              <SecuritySection
                activeTokenCount={
                  // 列表没读到就不要报「0 枚」——那会和实际撤销掉的枚数对不上。
                  tokens === null
                    ? null
                    : tokens.filter((item) => !item.revoked_at).length
                }
                credential={credential}
                onSessionsEnded={async (reason) => {
                  // 改密/下线以后当前会话已被服务端撤销：直接本地过期并把
                  // reason 带到「登录已过期」终屏（P2-2）。不再走 onLogout——
                  // 那会拿已撤销的 token 再发一次注定 401 的请求，且传输层
                  // EXPIRED 事件会抢先切屏，让 setNotice 的说明永远不可见。
                  account.onSessionExpired(reason);
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
                    maxLength={TOKEN_NAME_MAX_LENGTH}
                    required
                    value={tokenName}
                    onChange={(event) => setTokenName(event.target.value)}
                    placeholder="例如：工作电脑、自动脚本"
                  />
                  <p className="uc-count">
                    {tokenName.length}/{TOKEN_NAME_MAX_LENGTH}
                    {duplicateTokenLabel ? (
                      <span role="status" className="uc-count__warning">
                        ⚠️ 已有同名 Token，建议换个名字以便区分
                      </span>
                    ) : null}
                  </p>
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
      {tour}
      {help && <HelpDialog onClose={() => setHelp(false)} />}
      <CustomerRechargeDialog
        isOpen={recharge}
        onClose={() => setRecharge(false)}
        onOrderCreated={refreshData}
        onPaid={() => {
          refreshData();
          void account.onRefreshProfile().catch((cause) => captureError(cause));
        }}
        onSessionExpired={account.onSessionExpired}
        store={account.store}
      />
    </section>
  );
}
