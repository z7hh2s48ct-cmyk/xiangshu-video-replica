import { type FormEvent, useEffect, useRef, useState } from "react";
import type { CustomerRechargePackage } from "../api";
import {
  AdminControlError,
  type CustomerDiscount,
  createCustomerDiscount,
  deactivateCustomerDiscount,
  grantCustomerPackage,
  listCustomerDiscounts,
  listRechargePackages,
} from "../api.admin";
import {
  DISCOUNT_INTERFACE_LABELS,
  discountInterfaceLabel,
  formatDiscountZhe,
  packageBenefitLabel,
} from "../rechargePackageDisplay";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { formatDateTime, formatFen } from "./ui/vocabulary";

const INTERFACE_KEYS = Object.keys(DISCOUNT_INTERFACE_LABELS);

// 业务性 409 是确定结果（请求已被拒绝、无副作用）；其余 409（如同键请求仍在处理）
// 结果不确定，必须保留幂等键。
const DETERMINISTIC_CONFLICT_CODES = new Set([
  "RECHARGE_PACKAGE_VERSION_CONFLICT",
  "RECHARGE_PACKAGE_INACTIVE",
  "CUSTOMER_DISCOUNT_NOT_MANUAL",
]);

type PendingAction =
  | { kind: "grant"; pkg: CustomerRechargePackage; voucherRef: string }
  | {
      kind: "discount";
      discountRate: string;
      interfaces: string[];
      validUntil: string | null;
    }
  | { kind: "deactivate"; discount: CustomerDiscount };

function scopeLabel(interfaces: readonly string[]): string {
  return interfaces.length === 0
    ? "全部消耗"
    : interfaces.map(discountInterfaceLabel).join("、");
}

function packageOptionLabel(pkg: CustomerRechargePackage): string {
  return `${pkg.name} · ${formatFen(pkg.amount_fen)} · ${pkg.credits} 积分 · ${packageBenefitLabel(pkg) ?? "无折扣"}`;
}

/** 「折」输入（8.5）→ 4 位小数折扣率（"0.8500"）；非法 → null。 */
function zheToRate(text: string): string | null {
  const zhe = Number(text.trim());
  if (!text.trim() || !Number.isFinite(zhe) || zhe <= 0 || zhe > 10) {
    return null;
  }
  const rate = (zhe / 10).toFixed(4);
  return Number(rate) > 0 ? rate : null;
}

/**
 * 客户权益（客户详情）：查看当前折扣，代客开通套餐（线下已付款），设置/停用专项折扣。
 *
 * 计费时只取一条折扣：专项折扣优先于套餐权益（服务端优先级 200 > 100），因此这里
 * 把两类权益放在同一张表里展示，运营能直接看出哪一条在生效。
 */
export function CustomerBenefitsSection({
  userId,
  readOnly,
  onChanged,
}: {
  userId: string;
  readOnly: boolean;
  /** 开通套餐改变了余额：通知上层刷新客户摘要。 */
  onChanged: () => void;
}) {
  const [packages, setPackages] = useState<CustomerRechargePackage[] | null>(
    null,
  );
  const [discounts, setDiscounts] = useState<CustomerDiscount[] | null>(null);
  const [loadError, setLoadError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [packageId, setPackageId] = useState("");
  const [voucherRef, setVoucherRef] = useState("");
  const [zhe, setZhe] = useState("");
  const [interfaces, setInterfaces] = useState<string[]>([]);
  const [validUntilDate, setValidUntilDate] = useState("");
  const [formError, setFormError] = useState("");
  const [notice, setNotice] = useState("");
  const [lastRequestId, setLastRequestId] = useState("");
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [dialogError, setDialogError] = useState("");
  const [busy, setBusy] = useState(false);
  // 结果不确定（超时 / 5xx / 并发占位 409）时复用同一幂等键重试，
  // 否则重试可能让一笔线下收款开通两次。
  const retry = useRef<{ fingerprint: string; key: string } | null>(null);

  // biome-ignore lint/correctness/useExhaustiveDependencies: refresh 是显式重读信号。
  useEffect(() => {
    let cancelled = false;
    setLoadError("");
    const packagesRequest = readOnly
      ? Promise.resolve<CustomerRechargePackage[]>([])
      : listRechargePackages();
    void Promise.all([listCustomerDiscounts(userId), packagesRequest])
      .then(([discountItems, packageItems]) => {
        if (cancelled) return;
        setDiscounts(discountItems);
        setPackages(packageItems.filter((pkg) => pkg.is_active));
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setLoadError(
            cause instanceof Error && cause.message.trim()
              ? cause.message
              : "读取客户权益失败",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [userId, readOnly, refresh]);

  function requestGrant(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const pkg = packages?.find((item) => item.id === packageId);
    if (!pkg) {
      setFormError("请选择要开通的套餐");
      return;
    }
    if (!voucherRef.trim()) {
      setFormError("请填写线下收款凭证号");
      return;
    }
    setFormError("");
    setDialogError("");
    setPending({ kind: "grant", pkg, voucherRef: voucherRef.trim() });
  }

  function requestDiscount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const discountRate = zheToRate(zhe);
    if (discountRate === null) {
      setFormError(
        "折扣须大于 0 且不超过 10 折（8.5 表示 8.5 折），精度不能小到取整为零",
      );
      return;
    }
    setFormError("");
    setDialogError("");
    setPending({
      kind: "discount",
      discountRate,
      interfaces: [...interfaces],
      // 到期日按北京时间当天结束失效，与后台其余时间展示口径一致。
      validUntil: validUntilDate ? `${validUntilDate}T23:59:59+08:00` : null,
    });
  }

  async function submit(action: PendingAction, reason: string) {
    const fingerprint = JSON.stringify({
      action:
        action.kind === "grant"
          ? {
              kind: action.kind,
              id: action.pkg.id,
              version: action.pkg.version,
              ref: action.voucherRef,
            }
          : action.kind === "deactivate"
            ? { kind: action.kind, id: action.discount.id }
            : action,
      reason,
    });
    if (retry.current?.fingerprint !== fingerprint) {
      retry.current = { fingerprint, key: crypto.randomUUID() };
    }
    const key = retry.current.key;
    setBusy(true);
    setDialogError("");
    try {
      if (action.kind === "grant") {
        const result = await grantCustomerPackage(
          userId,
          {
            packageId: action.pkg.id,
            packageVersion: action.pkg.version,
            sourceDocumentRef: action.voucherRef,
          },
          reason,
          key,
        );
        setNotice(
          `已开通「${result.package_name}」：到账 ${result.credits} 积分，余额 ${result.wallet_balance_after} 积分`,
        );
        setLastRequestId(result.request_id);
        setPackageId("");
        setVoucherRef("");
        onChanged();
      } else if (action.kind === "discount") {
        const result = await createCustomerDiscount(
          userId,
          {
            discountRate: action.discountRate,
            applicableInterfaces: action.interfaces,
            validUntil: action.validUntil,
          },
          reason,
          key,
        );
        setNotice(
          `专项折扣已设置：${scopeLabel(result.discount.applicable_interfaces)} ${formatDiscountZhe(result.discount.discount_rate)}`,
        );
        setLastRequestId(result.request_id);
        setZhe("");
        setInterfaces([]);
        setValidUntilDate("");
      } else {
        const result = await deactivateCustomerDiscount(
          userId,
          action.discount.id,
          reason,
          key,
        );
        setNotice("专项折扣已停用");
        setLastRequestId(result.request_id);
      }
      retry.current = null;
      setPending(null);
      setRefresh((value) => value + 1);
    } catch (cause) {
      const deterministic =
        cause instanceof AdminControlError &&
        cause.status !== undefined &&
        cause.status >= 400 &&
        cause.status < 500 &&
        (![408, 409, 429].includes(cause.status) ||
          DETERMINISTIC_CONFLICT_CODES.has(cause.code ?? ""));
      const message =
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : "操作失败，请重试";
      if (deterministic) {
        // 确定性拒绝在对话框里改不了（对话框只能改原因）：关掉它，按提示改表单后重提。
        retry.current = null;
        setPending(null);
        setFormError(message);
      } else {
        setDialogError(
          `${message}。结果未确认：请先核对下方权益与余额，再决定是否重试（重试不会重复执行同一请求）。`,
        );
      }
      // 失败后一律重读：版本冲突 / 套餐停用需要最新套餐，结果未确认需要核对现状。
      setRefresh((value) => value + 1);
    } finally {
      setBusy(false);
    }
  }

  const dialog = (() => {
    if (!pending) return null;
    if (pending.kind === "grant") {
      const benefit = packageBenefitLabel(pending.pkg);
      return {
        title: "开通套餐（线下已付款）",
        confirmLabel: "确认开通",
        level: "reasonAndAck" as const,
        description: (
          <>
            套餐：{pending.pkg.name}，金额 {formatFen(pending.pkg.amount_fen)}
            ，到账 {pending.pkg.credits} 积分。
            <br />
            权益：
            {benefit
              ? `${benefit}（替换该客户此前的套餐权益；专项折扣仍优先）`
              : "无（不影响已有权益）"}
            <br />
            收款凭证号：{pending.voucherRef}
          </>
        ),
      };
    }
    if (pending.kind === "discount") {
      return {
        title: "设置专项折扣",
        confirmLabel: "确认设置",
        level: "reason" as const,
        description: (
          <>
            {scopeLabel(pending.interfaces)}{" "}
            {formatDiscountZhe(pending.discountRate)}，
            {pending.validUntil
              ? `有效至 ${pending.validUntil.slice(0, 10)}（北京时间当天结束）`
              : "永久有效"}
            。
            <br />
            将替换该客户当前的专项折扣，并优先于套餐权益生效。
          </>
        ),
      };
    }
    return {
      title: "停用专项折扣",
      confirmLabel: "确认停用",
      level: "reason" as const,
      description: `停用后该客户按套餐权益（如有）或原价计费：${scopeLabel(pending.discount.applicable_interfaces)} ${formatDiscountZhe(pending.discount.discount_rate)}。`,
    };
  })();

  return (
    <section
      aria-label="套餐与折扣"
      className="customer-detail-section customer-benefits"
      id="customer-benefits"
    >
      <h3>套餐与折扣</h3>
      <p className="admin-hint">
        客户权益内专项折扣优先于套餐权益；同类权益只保留最近一次设置。
        全局折扣更优惠时仍按全局折扣计价，最终售价可在下方试算。
      </p>
      {loadError ? (
        <PageBanner tone="error">
          {loadError}{" "}
          <button type="button" onClick={() => setRefresh((v) => v + 1)}>
            重新读取
          </button>
        </PageBanner>
      ) : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {lastRequestId ? (
        <details className="admin-technical-details">
          <summary>操作技术信息</summary>
          <p>请求编号：{lastRequestId}</p>
        </details>
      ) : null}
      {formError ? <PageBanner tone="error">{formError}</PageBanner> : null}

      {discounts === null ? (
        loadError ? null : (
          <p className="admin-hint">正在读取客户权益…</p>
        )
      ) : (
        <div className="table-scroll">
          <table className="internal-table">
            <thead>
              <tr>
                <th>来源</th>
                <th>折扣</th>
                <th>适用范围</th>
                <th>有效期至</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {discounts.length ? (
                discounts.map((discount) => (
                  <tr key={discount.id}>
                    <td>
                      {discount.source === "manual"
                        ? "专项折扣"
                        : `套餐「${discount.package_name ?? "—"}」`}
                    </td>
                    <td>{formatDiscountZhe(discount.discount_rate) ?? "—"}</td>
                    <td>{scopeLabel(discount.applicable_interfaces)}</td>
                    <td>
                      {discount.valid_until
                        ? formatDateTime(discount.valid_until)
                        : "永久"}
                    </td>
                    <td>
                      {discount.state === "effective"
                        ? "生效中"
                        : discount.state === "pending"
                          ? "未生效"
                          : discount.state === "expired"
                            ? "已过期"
                            : discount.state === "disabled" ||
                                !discount.is_active
                              ? "已停用"
                              : "待核对（缺少生效状态）"}
                    </td>
                    <td>
                      {!readOnly &&
                      discount.source === "manual" &&
                      discount.is_active ? (
                        <button
                          className="table-action-button"
                          type="button"
                          onClick={() => {
                            setDialogError("");
                            setPending({ kind: "deactivate", discount });
                          }}
                        >
                          停用
                        </button>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={6}>暂无折扣权益</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {readOnly ? (
        <p className="admin-hint">审计员仅可查看，不能开通套餐或设置折扣。</p>
      ) : (
        <>
          <h4>开通套餐（客户已线下付款）</h4>
          <form className="admin-form" onSubmit={requestGrant}>
            <label>
              套餐
              <select
                disabled={busy || packages === null}
                value={packageId}
                onChange={(event) => setPackageId(event.target.value)}
              >
                <option value="">
                  {packages?.length === 0 ? "暂无启用的套餐" : "请选择套餐"}
                </option>
                {packages?.map((pkg) => (
                  <option key={pkg.id} value={pkg.id}>
                    {packageOptionLabel(pkg)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              收款凭证号
              <input
                disabled={busy}
                placeholder="例如：对公转账流水号"
                value={voucherRef}
                onChange={(event) => setVoucherRef(event.target.value)}
              />
            </label>
            <button disabled={busy} type="submit">
              开通套餐
            </button>
          </form>

          <h4>专项折扣</h4>
          <form className="admin-form" onSubmit={requestDiscount}>
            <label>
              折扣（折）
              <input
                disabled={busy}
                inputMode="decimal"
                max="10"
                min="0.1"
                placeholder="例如：8.5"
                step="0.1"
                type="number"
                value={zhe}
                onChange={(event) => setZhe(event.target.value)}
              />
            </label>
            <label>
              到期日（留空为永久）
              <input
                disabled={busy}
                type="date"
                value={validUntilDate}
                onChange={(event) => setValidUntilDate(event.target.value)}
              />
            </label>
            <fieldset className="customer-benefits__scope" disabled={busy}>
              <legend>适用范围（不勾选 = 全部消耗）</legend>
              {INTERFACE_KEYS.map((key) => (
                <label key={key}>
                  <input
                    checked={interfaces.includes(key)}
                    type="checkbox"
                    onChange={(event) =>
                      setInterfaces((current) =>
                        event.target.checked
                          ? [...current, key]
                          : current.filter((item) => item !== key),
                      )
                    }
                  />
                  {DISCOUNT_INTERFACE_LABELS[key]}
                </label>
              ))}
            </fieldset>
            <button disabled={busy} type="submit">
              设置专项折扣
            </button>
          </form>
        </>
      )}

      <ConfirmDialog
        busy={busy}
        confirmLabel={dialog?.confirmLabel}
        description={dialog?.description}
        error={dialogError}
        level={dialog?.level}
        open={pending !== null}
        title={dialog?.title ?? ""}
        onClose={() => {
          if (!busy) {
            setPending(null);
            setDialogError("");
          }
        }}
        onConfirm={(reason) => {
          if (pending) void submit(pending, reason);
        }}
      />
    </section>
  );
}
