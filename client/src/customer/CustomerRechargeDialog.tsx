import { type FormEvent, useCallback, useEffect, useState } from "react";

import {
  type CreatedRechargeOrder,
  CustomerApiError,
  type CustomerPaymentCode,
  type CustomerRechargePackage,
  customerCreateRechargeOrder,
  customerCreateRechargePaymentCode,
  customerGetRechargeOrder,
  customerGetWallet,
  customerListRechargePackages,
  type WalletSnapshot,
} from "../api";
import {
  matchingPackagesForAmount,
  packageBelowMinimum,
  packageBenefitLabel,
  packageBonusCredits,
} from "../rechargePackageDisplay";
import type { CustomerCredentialStore } from "./useCustomerSession";

const ORDER_POLL_INTERVAL_MS = 2_000;

export function CustomerRechargeDialog({
  isOpen,
  onClose,
  onOrderCreated,
  onPaid,
  onSessionExpired,
  store,
  suggestedAmountYuan,
  suggestedPackageId,
}: {
  isOpen: boolean;
  onClose: () => void;
  onOrderCreated: () => void;
  onPaid: () => void;
  onSessionExpired: () => void;
  store: CustomerCredentialStore;
  suggestedAmountYuan?: number;
  /** 上游（如钱包套餐卡片）已选中的套餐：高亮提示，仍需用户再确认一次。 */
  suggestedPackageId?: string;
}) {
  const [wallet, setWallet] = useState<WalletSnapshot | null>(null);
  const [packages, setPackages] = useState<CustomerRechargePackage[]>([]);
  const [packageError, setPackageError] = useState("");
  const [amountYuan, setAmountYuan] = useState("");
  const [order, setOrder] = useState<CreatedRechargeOrder | null>(null);
  const [paymentCode, setPaymentCode] = useState<CustomerPaymentCode | null>(
    null,
  );
  const [paymentState, setPaymentState] = useState<
    "choosing" | "creating" | "waiting" | "paid"
  >("choosing");
  const [isQrLoaded, setIsQrLoaded] = useState(false);
  const [error, setError] = useState("");

  const loadCredential = useCallback(async () => {
    const token = await store.loadSessionToken();
    if (token === null) {
      onSessionExpired();
      return null;
    }
    return { kind: "session" as const, token };
  }, [store, onSessionExpired]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }
    setAmountYuan(suggestedAmountYuan ? String(suggestedAmountYuan) : "");
    setOrder(null);
    setPaymentCode(null);
    setPaymentState("choosing");
    setIsQrLoaded(false);
    setError("");
    setPackages([]);
    setPackageError("");
    let active = true;
    void loadCredential()
      .then(async (credential) => {
        if (credential === null) {
          return;
        }
        // 钱包与套餐互不拖累：套餐加载失败时自定义金额仍可用。
        const [walletResult, packageResult] = await Promise.allSettled([
          customerGetWallet(credential),
          customerListRechargePackages(credential),
        ]);
        if (!active) {
          return;
        }
        if (walletResult.status === "fulfilled") {
          setWallet(walletResult.value);
        } else {
          setError(
            visibleError(walletResult.reason, "充值信息暂不可用，请稍后重试。"),
          );
        }
        if (packageResult.status === "fulfilled") {
          setPackages(packageResult.value);
        } else {
          setPackageError("充值套餐暂不可用，可使用自定义金额充值。");
        }
      })
      .catch((cause) => {
        if (active) {
          setError(visibleError(cause, "充值信息暂不可用，请稍后重试。"));
        }
      });
    return () => {
      active = false;
    };
  }, [isOpen, suggestedAmountYuan, loadCredential]);

  useEffect(() => {
    if (!isOpen || paymentState !== "waiting" || order === null) {
      return;
    }
    let active = true;
    let timer: number | undefined;

    async function pollOrder() {
      const credential = await loadCredential();
      if (!active || credential === null) {
        return;
      }
      try {
        const latest = await customerGetRechargeOrder(
          credential,
          order?.order_no ?? "",
        );
        if (!active) {
          return;
        }
        if (latest.status === "PAID") {
          setPaymentState("paid");
          onPaid();
          return;
        }
        if (latest.status === "FAILED" || latest.status === "CLOSED") {
          setError("该充值订单已结束，请重新创建订单。");
          setPaymentState("choosing");
          return;
        }
        timer = window.setTimeout(pollOrder, ORDER_POLL_INTERVAL_MS);
      } catch (cause) {
        if (active) {
          setError(
            visibleError(cause, "暂时无法确认支付结果，系统会继续查询。"),
          );
          timer = window.setTimeout(pollOrder, ORDER_POLL_INTERVAL_MS);
        }
      }
    }

    void pollOrder();
    return () => {
      active = false;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [isOpen, paymentState, order, loadCredential, onPaid]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
      }
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [isOpen, onClose]);

  async function createOrder(input: { amountFen: number; packageId?: string }) {
    if (paymentState === "creating") {
      return;
    }
    const credential = await loadCredential();
    if (credential === null) {
      return;
    }
    setPaymentState("creating");
    setError("");
    setIsQrLoaded(false);
    try {
      const created = await customerCreateRechargeOrder(
        credential,
        input.amountFen,
        {
          idempotencyKey: crypto.randomUUID(),
          packageId: input.packageId,
        },
      );
      setOrder(created);
      // The order is durable before the provider QR call.  Refresh the wallet
      // immediately so a temporary provider failure cannot hide a PENDING
      // order that still needs payment, retry, or closure.
      onOrderCreated();
      const code = await customerCreateRechargePaymentCode(
        credential,
        created.order_no,
      );
      setPaymentCode(code);
      setPaymentState("waiting");
    } catch (cause) {
      setError(visibleError(cause, "支付二维码暂时无法生成，请稍后重试。"));
      setPaymentState("choosing");
    }
  }

  async function createCustomPayment(nextAmountYuan: number) {
    if (!wallet) {
      return;
    }
    const amountFen = nextAmountYuan * 100;
    if (
      !Number.isInteger(nextAmountYuan) ||
      amountFen < wallet.min_recharge_fen ||
      amountFen % wallet.recharge_step_fen !== 0
    ) {
      setError(
        `充值金额须为${formatFen(wallet.min_recharge_fen)}起，并按${formatFen(wallet.recharge_step_fen)}递增。`,
      );
      return;
    }
    await createOrder({ amountFen });
  }

  async function retryPaymentCode() {
    if (order === null) {
      return;
    }
    const credential = await loadCredential();
    if (credential === null) {
      return;
    }
    setPaymentState("creating");
    setError("");
    try {
      const code = await customerCreateRechargePaymentCode(
        credential,
        order.order_no,
      );
      setPaymentCode(code);
      setIsQrLoaded(false);
      setPaymentState("waiting");
    } catch (cause) {
      setError(visibleError(cause, "支付二维码暂时无法生成，请稍后重试。"));
      setPaymentState("choosing");
    }
  }

  function submitCustom(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void createCustomPayment(Number(amountYuan));
  }

  if (!isOpen) {
    return null;
  }

  return (
    <div className="recharge-dialog">
      <section
        aria-labelledby="recharge-dialog-title"
        aria-modal="true"
        className="recharge-dialog__panel"
        role="dialog"
      >
        <header className="recharge-dialog__header">
          <div>
            <p className="eyebrow">余额充值</p>
            <h2 id="recharge-dialog-title">扫码充值</h2>
          </div>
          <button
            aria-label="关闭充值窗口"
            className="secondary-button"
            onClick={onClose}
            type="button"
          >
            关闭
          </button>
        </header>

        {paymentState === "paid" ? (
          <div className="recharge-dialog__success" role="status">
            <strong>充值成功</strong>
            <p>积分已经到账，可以继续创建视频。</p>
            <button onClick={onClose} type="button">
              完成
            </button>
          </div>
        ) : paymentCode && order ? (
          <div className="recharge-dialog__payment">
            <div className="recharge-dialog__qr">
              {!isQrLoaded ? <span>二维码加载中…</span> : null}
              <img
                alt="充值支付二维码"
                onError={() => {
                  setIsQrLoaded(false);
                  setError("二维码图片加载失败，请点击重新获取。");
                }}
                onLoad={() => setIsQrLoaded(true)}
                src={paymentCode.qr_image_url}
              />
            </div>
            <div className="recharge-dialog__summary">
              <span>支付金额</span>
              <strong>{formatFen(order.amount_fen)}</strong>
              <p>到账 {order.credits} 积分 · 支付完成后自动到账</p>
              <span className="recharge-dialog__waiting" role="status">
                正在等待支付结果
              </span>
              {paymentCode.payment_url.startsWith("weixin://") ? (
                <p>请使用微信扫一扫完成支付。</p>
              ) : (
                <button
                  className="secondary-button"
                  onClick={() =>
                    window.open(
                      paymentCode.payment_url,
                      "_blank",
                      "noopener,noreferrer",
                    )
                  }
                  type="button"
                >
                  无法扫码？打开支付页面
                </button>
              )}
            </div>
          </div>
        ) : (
          <div className="recharge-dialog__chooser">
            <p>
              {wallet
                ? `当前可用 ${wallet.available_credits} 积分，${wallet.points_per_yuan ? `1 元 = ${wallet.points_per_yuan} 积分` : `兑换单价 ${formatFen(wallet.internal_unit_price_fen)}/积分`}。`
                : "正在读取充值信息…"}
            </p>
            {packages.length ? (
              <div className="recharge-dialog__packages">
                {packages.map((pkg) => {
                  const benefit = packageBenefitLabel(pkg);
                  const bonus = packageBonusCredits(wallet, pkg);
                  // 低于生效起充额的档位后端必 422：钱包快照就绪后置灰并说明原因。
                  const belowMinimum = packageBelowMinimum(pkg, wallet);
                  return (
                    <button
                      className={
                        pkg.id === suggestedPackageId
                          ? "recharge-package-card recharge-package-card--suggested"
                          : "recharge-package-card"
                      }
                      disabled={paymentState === "creating" || belowMinimum}
                      key={pkg.id}
                      onClick={() =>
                        void createOrder({
                          amountFen: pkg.amount_fen,
                          packageId: pkg.id,
                        })
                      }
                      type="button"
                    >
                      <strong>{pkg.name}</strong>
                      <span>
                        {formatFen(pkg.amount_fen)} → {pkg.credits} 积分
                      </span>
                      {bonus !== null ? <span>含赠送 {bonus} 积分</span> : null}
                      {benefit ? <span>{benefit}</span> : null}
                      {belowMinimum && wallet ? (
                        <span>
                          低于起充金额 {formatFen(wallet.min_recharge_fen)}
                          ，暂不可购
                        </span>
                      ) : null}
                      {pkg.id === suggestedPackageId ? (
                        <span>已选套餐</span>
                      ) : null}
                    </button>
                  );
                })}
              </div>
            ) : packageError ? (
              <p role="status">{packageError}</p>
            ) : null}
            <form onSubmit={submitCustom}>
              <label>
                自定义金额（元）
                <input
                  inputMode="numeric"
                  onChange={(event) => setAmountYuan(event.target.value)}
                  type="number"
                  value={amountYuan}
                />
              </label>
              <button
                disabled={!wallet || paymentState === "creating"}
                type="submit"
              >
                {paymentState === "creating"
                  ? "正在生成二维码…"
                  : "生成支付二维码"}
              </button>
            </form>
            {matchingPackagesForAmount(packages, Number(amountYuan) * 100)
              .length ? (
              <p role="status">
                该金额有对应套餐（含赠送/折扣）；选择上方套餐可享受套餐到账与权益，
                自定义金额按基础汇率到账。
              </p>
            ) : null}
            {wallet &&
              Number.isSafeInteger(Number(amountYuan)) &&
              Number(amountYuan) > 0 && (
                <p role="status">
                  预计到账{" "}
                  {wallet.points_per_yuan
                    ? Number(amountYuan) * wallet.points_per_yuan
                    : Math.floor(
                        (Number(amountYuan) * 100) /
                          wallet.internal_unit_price_fen,
                      )}{" "}
                  积分，以创建订单时的兑换规则为准。
                </p>
              )}
            {order && !paymentCode ? (
              <button
                className="secondary-button"
                onClick={() => void retryPaymentCode()}
                type="button"
              >
                重新获取上一订单二维码
              </button>
            ) : null}
          </div>
        )}

        {error ? (
          <p className="settings-error" role="alert">
            {error}
          </p>
        ) : null}
        <footer>请以本窗口显示的金额为准；如已扣款，请勿重复支付。</footer>
      </section>
    </div>
  );
}

function formatFen(amountFen: number): string {
  const yuan = amountFen / 100;
  return `${Number.isInteger(yuan) ? yuan : yuan.toFixed(2)}元`;
}

function visibleError(cause: unknown, fallback: string): string {
  if (cause instanceof CustomerApiError && cause.status === 401) {
    return "登录已失效，请重新进入工作台。";
  }
  return cause instanceof Error && cause.message.trim()
    ? cause.message
    : fallback;
}
