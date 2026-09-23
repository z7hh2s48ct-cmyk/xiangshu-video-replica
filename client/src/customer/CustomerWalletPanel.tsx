import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { Pagination } from "../admin/ui/Pagination";
import {
  type CreatedRechargeOrder,
  CustomerApiError,
  type CustomerRechargePackage,
  customerCloseRechargeOrder,
  customerCreateRechargeOrder,
  customerGetRechargeOrder,
  customerGetWallet,
  customerListRechargeOrders,
  customerListRechargePackages,
  customerListWalletTransactions,
  type GenerationPriceQuote,
  getGenerationPriceQuote,
  type RechargeOrder,
  type RechargeOrderPage,
  type WalletSnapshot,
  type WalletTransaction,
  type WalletTransactionPage,
} from "../api";
import {
  discountSourceLabel,
  formatDiscountZhe,
  matchingPackagesForAmount,
  packageBelowMinimum,
  packageBenefitLabel,
  packageBonusCredits,
} from "../rechargePackageDisplay";
import { useCustomerConfirm } from "./CustomerConfirmDialog";
import { LedgerPairingSummary } from "./LedgerPairingSummary";
import {
  groupLedgerRows,
  type LedgerPairState,
  netAvailableDelta,
} from "./ledger-pairing";
import { orderPollDelay, orderPollWithinWindow } from "./orderPolling";
import { RetryButton } from "./RetryButton";
import { TransactionPricingBreakdown } from "./TransactionPricingBreakdown";
import type { CustomerCredentialStore } from "./useCustomerSession";
import "./customer-wallet.css";

const HISTORY_PAGE_SIZE = 20;

/** The customer wallet view (task #7): balance, recharge and orders under the
 * customer session. Mirrors the internal WalletPanel but talks to the
 * customer-lane endpoints (the internal wallet API 401'd for a customer
 * session). Never a second main-code entry — recharge reuses the same wallet. */
export function CustomerWalletPanel({
  store,
  onSessionExpired,
  onRechargeRequested,
}: {
  store: CustomerCredentialStore;
  onSessionExpired: () => void;
  onRechargeRequested?: (amountYuan: number, packageId?: string) => void;
}) {
  const [wallet, setWallet] = useState<WalletSnapshot | null>(null);
  const [packages, setPackages] = useState<CustomerRechargePackage[]>([]);
  const [packageError, setPackageError] = useState("");
  const [transactionPage, setTransactionPage] =
    useState<WalletTransactionPage | null>(null);
  const [orders, setOrders] = useState<RechargeOrder[]>([]);
  const [orderHistoryPage, setOrderHistoryPage] =
    useState<RechargeOrderPage | null>(null);
  const [showOrderHistory, setShowOrderHistory] = useState(false);
  const [customAmount, setCustomAmount] = useState("");
  const [pendingOrderNo, setPendingOrderNo] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [pollingError, setPollingError] = useState("");
  const [summaryError, setSummaryError] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isCreating, setIsCreating] = useState(false);
  const [closingOrderNo, setClosingOrderNo] = useState<string | null>(null);
  const { confirm, dialog: confirmDialog } = useCustomerConfirm();
  const [priceQuotes, setPriceQuotes] = useState<GenerationPriceQuote[]>([]);
  const [quoteError, setQuoteError] = useState("");
  const [isTransactionLoading, setIsTransactionLoading] = useState(false);
  const [transactionError, setTransactionError] = useState("");
  const [failedTransactionOffset, setFailedTransactionOffset] = useState<
    number | null
  >(null);
  const [isOrderHistoryLoading, setIsOrderHistoryLoading] = useState(false);
  const [orderHistoryError, setOrderHistoryError] = useState("");
  const [failedOrderHistoryOffset, setFailedOrderHistoryOffset] = useState<
    number | null
  >(null);
  // P1-7：同一计费周期的行折叠成一组，展开状态只属于当前页面。
  const [expandedPairs, setExpandedPairs] = useState<ReadonlySet<string>>(
    new Set(),
  );
  const summaryRequestIdRef = useRef(0);
  const transactionRequestIdRef = useRef(0);
  const orderHistoryRequestIdRef = useRef(0);
  const showOrderHistoryRef = useRef(showOrderHistory);
  const transactionOffsetRef = useRef(0);
  const orderHistoryOffsetRef = useRef(0);
  showOrderHistoryRef.current = showOrderHistory;

  const loadSession = useCallback(async (): Promise<{
    kind: "session";
    token: string;
  } | null> => {
    const token = await store.loadSessionToken();
    if (token === null) {
      onSessionExpired();
      return null;
    }
    return { kind: "session", token };
  }, [store, onSessionExpired]);

  const loadSummary = useCallback(async () => {
    const requestId = ++summaryRequestIdRef.current;
    try {
      const credential = await loadSession();
      if (credential === null || requestId !== summaryRequestIdRef.current) {
        return;
      }
      const [nextWallet, recentOrderPage, packageOutcome] = await Promise.all([
        customerGetWallet(credential),
        customerListRechargeOrders(credential, {
          limit: HISTORY_PAGE_SIZE,
          offset: 0,
        }),
        // 套餐加载失败不拖垮钱包：自定义金额仍可用。
        customerListRechargePackages(credential).then(
          (items) => ({ ok: true as const, items }),
          () => ({ ok: false as const, items: [] }),
        ),
      ]);
      if (requestId !== summaryRequestIdRef.current) {
        return;
      }
      setWallet(nextWallet);
      if (packageOutcome.ok) {
        setPackages(packageOutcome.items);
        setPackageError("");
      } else {
        setPackageError("充值套餐暂不可用，可使用自定义金额充值。");
      }
      setSummaryError("");
      setOrders(
        recentOrderPage.items.filter((order) => order.status !== "CLOSED"),
      );
      // Codex P2 (PR #65): the pending order number lives only in component
      // state, so reopening/remounting the wallet (or exhausting the poll
      // window) would never resume tracking an outstanding payment. Derive the
      // most recent still-pending order from the fetched list and restart the
      // status poll — unless one is already being tracked.
      const pending = recentOrderPage.items.find(
        (order) => order.status === "PENDING",
      );
      if (pending) {
        setPendingOrderNo((current) => current ?? pending.order_no);
      }
    } catch (cause) {
      if (requestId === summaryRequestIdRef.current) {
        setSummaryError(errorMessage(cause, "钱包暂不可用，请稍后重试。"));
        throw cause;
      }
    }
  }, [loadSession]);

  const loadTransactions = useCallback(
    async (offset: number) => {
      const requestId = ++transactionRequestIdRef.current;
      setIsTransactionLoading(true);
      setTransactionError("");
      try {
        const credential = await loadSession();
        if (
          credential === null ||
          requestId !== transactionRequestIdRef.current
        ) {
          return;
        }
        const nextPage = await customerListWalletTransactions(credential, {
          limit: HISTORY_PAGE_SIZE,
          offset,
        });
        if (requestId !== transactionRequestIdRef.current) {
          return;
        }
        setTransactionPage(nextPage);
        transactionOffsetRef.current = nextPage.offset;
        setFailedTransactionOffset(null);
      } catch (cause) {
        if (requestId === transactionRequestIdRef.current) {
          setTransactionError(errorMessage(cause, "额度流水加载失败"));
          setFailedTransactionOffset(offset);
        }
      } finally {
        if (requestId === transactionRequestIdRef.current) {
          setIsTransactionLoading(false);
        }
      }
    },
    [loadSession],
  );

  const loadOrderHistory = useCallback(
    async (offset: number) => {
      const requestId = ++orderHistoryRequestIdRef.current;
      setIsOrderHistoryLoading(true);
      setOrderHistoryError("");
      try {
        const credential = await loadSession();
        if (
          credential === null ||
          requestId !== orderHistoryRequestIdRef.current
        ) {
          return;
        }
        const nextPage = await customerListRechargeOrders(credential, {
          limit: HISTORY_PAGE_SIZE,
          offset,
        });
        if (requestId !== orderHistoryRequestIdRef.current) {
          return;
        }
        setOrderHistoryPage(nextPage);
        orderHistoryOffsetRef.current = nextPage.offset;
        setFailedOrderHistoryOffset(null);
      } catch (cause) {
        if (requestId === orderHistoryRequestIdRef.current) {
          setOrderHistoryError(errorMessage(cause, "充值记录加载失败"));
          setFailedOrderHistoryOffset(offset);
        }
      } finally {
        if (requestId === orderHistoryRequestIdRef.current) {
          setIsOrderHistoryLoading(false);
        }
      }
    },
    [loadSession],
  );

  const refresh = useCallback(async () => {
    const requests: Promise<void>[] = [
      loadSummary(),
      loadTransactions(transactionOffsetRef.current),
    ];
    if (showOrderHistoryRef.current) {
      requests.push(loadOrderHistory(orderHistoryOffsetRef.current));
    }
    await Promise.allSettled(requests);
  }, [loadOrderHistory, loadSummary, loadTransactions]);

  useEffect(() => {
    let active = true;
    Promise.all([loadSummary(), loadTransactions(0)])
      .catch(() => undefined)
      .finally(() => {
        if (active) {
          setIsLoading(false);
        }
      });
    return () => {
      active = false;
      summaryRequestIdRef.current += 1;
      transactionRequestIdRef.current += 1;
      orderHistoryRequestIdRef.current += 1;
    };
  }, [loadSummary, loadTransactions]);

  useEffect(() => {
    let active = true;
    Promise.all([
      getGenerationPriceQuote({
        resolution: "768P",
        duration_seconds: 4,
        quantity: 1,
      }),
      getGenerationPriceQuote({
        resolution: "2K",
        duration_seconds: 4,
        quantity: 1,
      }),
    ])
      .then((quotes) => {
        const validQuotes = quotes.filter(
          (quote) =>
            (quote.resolution === "768P" || quote.resolution === "2K") &&
            Number.isFinite(quote.unit_price_fen_per_second),
        );
        if (validQuotes.length !== 2) {
          throw new Error("生成单价暂不可用，请稍后重试。");
        }
        if (active) setPriceQuotes(validQuotes);
      })
      .catch((cause: unknown) => {
        if (active) {
          setPriceQuotes([]);
          setQuoteError(errorMessage(cause, "生成单价暂不可用，请稍后重试。"));
        }
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    setPollingError("");
    if (!pendingOrderNo) {
      return;
    }
    let active = true;
    let timer: number | undefined;
    // P2#20：2s 起、每次翻倍、封顶 32s，总窗口 5 分钟（原来是固定 2s×30 次）。
    let attempts = 0;
    const startedAt = Date.now();

    async function checkOrder() {
      const nextDelay = orderPollDelay(attempts);
      attempts += 1;
      const credential = await loadSession();
      if (!active || credential === null) {
        return;
      }
      try {
        const order = await customerGetRechargeOrder(
          credential,
          pendingOrderNo as string,
        );
        if (!active) {
          return;
        }
        setPollingError("");
        if (order.status === "PAID") {
          setPendingOrderNo(null);
          setNotice("充值已到账，钱包余额已更新。");
          await refresh();
          return;
        }
        if (order.status === "FAILED" || order.status === "CLOSED") {
          setPendingOrderNo(null);
          setNotice("该充值订单已结束，未增加积分。");
          await refresh();
          return;
        }
        setNotice("支付结果确认中，请完成支付后返回本页。");
        if (!orderPollWithinWindow(startedAt)) {
          setNotice("支付结果仍待确认，可稍后刷新页面继续查询。");
          return;
        }
        timer = window.setTimeout(checkOrder, nextDelay);
      } catch (cause) {
        if (!active) {
          return;
        }
        setPollingError(errorMessage(cause, "暂时无法查询充值状态。"));
        if (!orderPollWithinWindow(startedAt)) {
          return;
        }
        timer = window.setTimeout(checkOrder, nextDelay);
      }
    }

    void checkOrder();
    return () => {
      active = false;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [pendingOrderNo, refresh, loadSession]);

  const refreshOrderViews = useCallback(() => {
    void loadSummary().catch(() => undefined);
    if (showOrderHistoryRef.current) {
      void loadOrderHistory(orderHistoryOffsetRef.current);
    }
  }, [loadOrderHistory, loadSummary]);

  async function createRechargeOrder(input: {
    amountFen: number;
    packageId?: string;
  }) {
    const credential = await loadSession();
    if (credential === null) {
      return;
    }
    setIsCreating(true);
    setError("");
    setNotice("");
    try {
      const created = await customerCreateRechargeOrder(
        credential,
        input.amountFen,
        { idempotencyKey: crypto.randomUUID(), packageId: input.packageId },
      );
      setPendingOrderNo(created.order_no);
      setNotice("支付页已打开，本页会自动确认到账。");
      submitPaymentForm(created);
      refreshOrderViews();
    } catch (cause) {
      setError(errorMessage(cause, "创建充值订单失败。"));
    } finally {
      setIsCreating(false);
    }
  }

  async function startPackageRecharge(pkg: CustomerRechargePackage) {
    if (isCreating) {
      return;
    }
    if (onRechargeRequested) {
      onRechargeRequested(pkg.amount_fen / 100, pkg.id);
      return;
    }
    await createRechargeOrder({
      amountFen: pkg.amount_fen,
      packageId: pkg.id,
    });
  }

  async function startCustomRecharge(amountYuan: number) {
    if (!wallet || isCreating) {
      return;
    }
    const amountFen = amountYuan * 100;
    if (
      !Number.isInteger(amountYuan) ||
      amountFen < wallet.min_recharge_fen ||
      amountFen % wallet.recharge_step_fen !== 0
    ) {
      setError(
        `充值金额须为${formatFen(wallet.min_recharge_fen)}起，并按${formatFen(wallet.recharge_step_fen)}递增。`,
      );
      return;
    }
    if (onRechargeRequested) {
      onRechargeRequested(amountYuan);
      return;
    }
    await createRechargeOrder({ amountFen });
  }

  function submitCustomAmount(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void startCustomRecharge(Number(customAmount));
  }

  function closePendingOrder(orderNo: string) {
    if (closingOrderNo) {
      return;
    }
    // 删单不可撤销，走产品级确认框（audit-10 / P0 清单 #2）；失败信息留在框内。
    confirm({
      title: "删除这个待支付订单？",
      description:
        "如果已经扫码付款，请不要删除，先等待到账。删除后订单不可恢复。",
      level: "acknowledge",
      confirmLabel: "删除订单",
      onConfirm: async () => {
        const credential = await loadSession();
        if (credential === null) {
          return;
        }
        setClosingOrderNo(orderNo);
        setError("");
        try {
          await customerCloseRechargeOrder(credential, orderNo);
          setOrders((current) =>
            current.filter((order) => order.order_no !== orderNo),
          );
          setOrderHistoryPage((current) =>
            current
              ? {
                  ...current,
                  items: current.items.map((order) =>
                    order.order_no === orderNo
                      ? { ...order, status: "CLOSED" }
                      : order,
                  ),
                }
              : current,
          );
          setPendingOrderNo((current) =>
            current === orderNo ? null : current,
          );
          setNotice("待支付订单已删除。");
          refreshOrderViews();
        } finally {
          setClosingOrderNo(null);
        }
      },
    });
  }

  if (isLoading && !wallet) {
    return <p className="status-note">正在读取钱包</p>;
  }

  if (!wallet) {
    return (
      <section className="settings-error" role="alert">
        <span>{summaryError || "钱包暂不可用。"}</span>
        <RetryButton
          label="重新加载钱包"
          onClick={() => void loadSummary().catch(() => undefined)}
        />
      </section>
    );
  }

  const transactions = transactionPage?.items ?? [];
  // 报价里的套餐折扣（取更优后）：钱包页展示用户当前实际享有的优惠。
  const discountQuote = priceQuotes.find(
    (quote) => formatDiscountZhe(quote.discount_rate) !== null,
  );
  const discountZhe = discountQuote
    ? formatDiscountZhe(discountQuote.discount_rate)
    : null;
  const discountSource = discountSourceLabel(discountQuote?.discount_source);
  const discountNote = discountZhe
    ? `已享${discountZhe}${discountSource ? `（${discountSource}）` : ""}`
    : null;

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

  const ledgerRowCells = (transaction: WalletTransaction) => (
    <>
      <td>{transaction.created_at}</td>
      <td>{transactionTypeLabel(transaction.type)}</td>
      <td>{signedNumber(transaction.available_delta)}</td>
      <td>
        {transactionDetail(transaction)}
        <TransactionPricingBreakdown
          credential={loadSession}
          transaction={transaction}
        />
      </td>
    </>
  );

  return (
    <section className="wallet-page" aria-label="余额与充值">
      {confirmDialog}
      <div className="wallet-summary-grid">
        <article className="wallet-summary-card">
          <span>可用额度</span>
          <strong>{wallet.available_credits} 积分</strong>
          <small>冻结中 {wallet.reserved_credits} 积分</small>
        </article>
        <article className="wallet-summary-card">
          <span>价目</span>
          {priceQuotes.length ? (
            priceQuotes.map((quote) => (
              <strong key={quote.resolution}>
                {quote.resolution} {formatFen(quote.unit_price_fen_per_second)}{" "}
                / 秒
              </strong>
            ))
          ) : quoteError ? (
            <p className="error" role="alert">
              {quoteError}
            </p>
          ) : (
            <span>正在获取生成单价…</span>
          )}
          <small>
            {wallet.points_per_yuan
              ? `充值换算：1元 = ${wallet.points_per_yuan} 积分`
              : "充值积分价格待配置"}
          </small>
          {discountNote ? <small>{discountNote}</small> : null}
          <small>按提交档位计费，生成失败全额退回</small>
        </article>
      </div>

      <section className="wallet-section" aria-labelledby="recharge-title">
        <div className="wallet-section__heading">
          <div>
            <h2 id="recharge-title">充值积分</h2>
            <p>
              {formatFen(wallet.min_recharge_fen)}起充，按
              {formatFen(wallet.recharge_step_fen)}递增。
            </p>
          </div>
        </div>
        {packages.length ? (
          <div className="recharge-presets recharge-presets--packages">
            {packages.map((pkg) => {
              const benefit = packageBenefitLabel(pkg);
              const bonus = packageBonusCredits(wallet, pkg);
              // 低于生效起充额的档位后端必 422：客户端先行置灰并说明原因。
              const belowMinimum = packageBelowMinimum(pkg, wallet);
              return (
                <button
                  aria-label={`购买套餐${pkg.name}`}
                  disabled={isCreating || belowMinimum}
                  key={pkg.id}
                  onClick={() => void startPackageRecharge(pkg)}
                  type="button"
                >
                  <strong>{pkg.name}</strong>
                  <span>
                    {formatFen(pkg.amount_fen)} → {pkg.credits} 积分
                  </span>
                  {bonus !== null ? <span>含赠送 {bonus} 积分</span> : null}
                  {benefit ? <span>{benefit}</span> : null}
                  {belowMinimum ? (
                    <span>
                      低于起充金额 {formatFen(wallet.min_recharge_fen)}
                      ，暂不可购
                    </span>
                  ) : null}
                </button>
              );
            })}
          </div>
        ) : packageError ? (
          <p role="status">{packageError}</p>
        ) : null}
        <form
          className="custom-recharge-form"
          noValidate
          onSubmit={submitCustomAmount}
        >
          <label>
            自定义充值金额（元）
            <input
              inputMode="numeric"
              min={wallet.min_recharge_fen / 100}
              step={wallet.recharge_step_fen / 100}
              type="number"
              value={customAmount}
              onChange={(event) => setCustomAmount(event.target.value)}
            />
          </label>
          <button disabled={isCreating} type="submit">
            {isCreating ? "正在创建订单" : "确认充值"}
          </button>
        </form>
        {matchingPackagesForAmount(packages, Number(customAmount) * 100)
          .length ? (
          <p role="status">
            该金额有对应套餐（含赠送/折扣）；选择上方套餐可享受套餐到账与权益，
            自定义金额按基础汇率到账。
          </p>
        ) : null}
        {error || pollingError ? (
          <p className="settings-error" role="alert">
            {error || pollingError}
          </p>
        ) : null}
        {notice ? (
          <p className="wallet-notice" role="status">
            {notice}
          </p>
        ) : null}
      </section>

      <section className="wallet-section" aria-labelledby="orders-title">
        <div className="wallet-section__heading">
          <h2 id="orders-title">最近充值订单</h2>
          <button
            className="secondary-button"
            onClick={() => {
              const nextVisible = !showOrderHistory;
              showOrderHistoryRef.current = nextVisible;
              setShowOrderHistory(nextVisible);
              if (nextVisible) {
                orderHistoryOffsetRef.current = 0;
                void loadOrderHistory(0);
              } else {
                orderHistoryRequestIdRef.current += 1;
              }
            }}
            type="button"
          >
            {showOrderHistory ? "收起全部充值记录" : "查看全部充值记录"}
          </button>
        </div>
        {summaryError ? (
          <div className="settings-error" role="alert">
            <span>{summaryError}</span>
            <RetryButton
              label="重新加载钱包和最近订单"
              onClick={() => void loadSummary().catch(() => undefined)}
            />
          </div>
        ) : null}
        <div className="table-scroll">
          <table className="internal-table">
            <thead>
              <tr>
                <th>订单号</th>
                <th>金额</th>
                <th>到账积分</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {orders.length ? (
                orders.map((order) => (
                  <tr key={order.order_no}>
                    <td>{order.order_no}</td>
                    <td>{formatFen(order.amount_fen)}</td>
                    <td>{order.credits}</td>
                    <td>{orderStatusLabel(order.status)}</td>
                    <td>
                      {order.status === "PENDING" ? (
                        <button
                          className="table-action-button table-action-button--danger"
                          disabled={closingOrderNo === order.order_no}
                          onClick={() => void closePendingOrder(order.order_no)}
                          type="button"
                        >
                          {closingOrderNo === order.order_no
                            ? "正在删除"
                            : "删除待支付订单"}
                        </button>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={5}>暂无充值订单</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {showOrderHistory ? (
        <section
          className="wallet-section"
          aria-labelledby="order-history-title"
        >
          <h2 id="order-history-title">充值订单历史</h2>
          <div className="table-scroll">
            <table className="internal-table">
              <thead>
                <tr>
                  <th>订单号</th>
                  <th>金额</th>
                  <th>到账积分</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {orderHistoryPage?.items.length ? (
                  orderHistoryPage.items.map((order) => (
                    <tr key={order.order_no}>
                      <td>{order.order_no}</td>
                      <td>{formatFen(order.amount_fen)}</td>
                      <td>{order.credits}</td>
                      <td>{orderStatusLabel(order.status)}</td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={4}>
                      {isOrderHistoryLoading
                        ? "正在读取充值记录"
                        : "暂无充值记录"}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          {orderHistoryError ? (
            <div className="settings-error" role="alert">
              <span>{orderHistoryError}</span>
              <RetryButton
                label="重试加载充值记录"
                onClick={() =>
                  void loadOrderHistory(
                    failedOrderHistoryOffset ?? orderHistoryPage?.offset ?? 0,
                  )
                }
              />
            </div>
          ) : null}
          <Pagination
            disabled={isOrderHistoryLoading}
            limit={orderHistoryPage?.limit ?? HISTORY_PAGE_SIZE}
            offset={orderHistoryPage?.offset ?? 0}
            onPageChange={(nextOffset) => void loadOrderHistory(nextOffset)}
            total={orderHistoryPage?.total ?? 0}
          />
        </section>
      ) : null}

      <section className="wallet-section" aria-labelledby="ledger-title">
        <h2 id="ledger-title">额度流水</h2>
        <div className="table-scroll">
          <table className="internal-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>类型</th>
                <th>可用变化</th>
                <th>计费明细</th>
              </tr>
            </thead>
            <tbody>
              {transactions.length ? (
                groupLedgerRows(transactions).map((entry) => {
                  if (entry.kind === "row") {
                    return (
                      <tr key={entry.row.id}>{ledgerRowCells(entry.row)}</tr>
                    );
                  }
                  const { pair } = entry;
                  // 摘要行落在组内最新一行的时间上，符合时间倒序的列表观感。
                  const latest = pair.rows[pair.rows.length - 1];
                  const expanded = expandedPairs.has(pair.operationId);
                  return (
                    <Fragment key={`pair-${pair.operationId}`}>
                      <tr className="ledger-pair-parent">
                        <td>{latest.created_at}</td>
                        <td>{PAIR_STATE_LABEL[pair.state]}</td>
                        <td>{signedNumber(netAvailableDelta(pair.rows))}</td>
                        <td>
                          <LedgerPairingSummary
                            pair={pair}
                            expanded={expanded}
                            onToggle={() => togglePair(pair.operationId)}
                          />
                        </td>
                      </tr>
                      {expanded &&
                        pair.rows.map((row) => (
                          <tr key={row.id} className="ledger-pair-child">
                            {ledgerRowCells(row)}
                          </tr>
                        ))}
                    </Fragment>
                  );
                })
              ) : (
                <tr>
                  <td colSpan={4}>暂无额度流水</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {transactionError ? (
          <div className="settings-error" role="alert">
            <span>{transactionError}</span>
            <RetryButton
              label="重试加载额度流水"
              onClick={() =>
                void loadTransactions(
                  failedTransactionOffset ?? transactionPage?.offset ?? 0,
                )
              }
            />
          </div>
        ) : null}
        <Pagination
          disabled={isTransactionLoading}
          limit={transactionPage?.limit ?? HISTORY_PAGE_SIZE}
          offset={transactionPage?.offset ?? 0}
          onPageChange={(nextOffset) => void loadTransactions(nextOffset)}
          total={transactionPage?.total ?? 0}
        />
      </section>
    </section>
  );
}

function submitPaymentForm(order: CreatedRechargeOrder) {
  const form = document.createElement("form");
  form.method = order.method;
  form.action = order.gateway_url;
  form.target = "_blank";
  form.setAttribute("rel", "noopener");
  for (const [name, value] of Object.entries(order.form_fields)) {
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    form.append(input);
  }
  document.body.append(form);
  form.submit();
  form.remove();
}

function formatFen(amountFen: number): string {
  const yuan = amountFen / 100;
  return `${Number.isInteger(yuan) ? yuan : yuan.toFixed(2)}元`;
}

function signedNumber(value: number): string {
  return `${value > 0 ? "+" : ""}${value} 积分`;
}

function transactionDetail(transaction: WalletTransaction): string {
  if (transaction.service_name) return transaction.service_name;
  if (transaction.task_id) {
    return "视频生成";
  }
  if (transaction.recharge_order_id) return "充值到账";
  return transaction.type === "RELEASE" ? "未消费积分退回" : "积分变动";
}

function orderStatusLabel(status: RechargeOrder["status"]): string {
  return {
    PENDING: "待支付",
    PAID: "已到账",
    FAILED: "支付失败",
    CLOSED: "已关闭",
  }[status];
}

function transactionTypeLabel(type: WalletTransaction["type"]): string {
  return {
    CONVERSION: "历史积分转换",
    CHARGE: "充值到账",
    RESERVE: "任务冻结",
    SETTLE: "成功结算",
    RELEASE: "失败返还",
  }[type];
}

/** P1-7：折叠后的计费周期按最终态命名，与逐笔类型标签保持同一套措辞。 */
const PAIR_STATE_LABEL: Record<LedgerPairState, string> = {
  PENDING: "任务冻结",
  SETTLED: "成功结算",
  RELEASED: "失败返还",
};

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof CustomerApiError && error.message.trim()) {
    return error.message;
  }
  return error instanceof Error && error.message.trim()
    ? error.message
    : fallback;
}
