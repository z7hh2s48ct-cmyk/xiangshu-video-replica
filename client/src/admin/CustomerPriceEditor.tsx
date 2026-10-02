import { type FormEvent, useEffect, useState } from "react";
import {
  type CustomerUnitPrice,
  fetchCustomerUnitPrice,
  updateCustomerUnitPrice,
} from "../api.admin";
import { yuanInputToFen } from "../rechargePackageDisplay";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { formatFen, formatYuanFromFen } from "./ui/vocabulary";
import "./admin-customer-detail.css";

export function CustomerPriceEditor({
  userId,
  readOnly,
}: {
  userId: string;
  readOnly: boolean;
}) {
  const [pricing, setPricing] = useState<CustomerUnitPrice | null>(null);
  const [priceYuan, setPriceYuan] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  // null = dialog closed; number = save that price; "reset" = restore default.
  const [pendingWrite, setPendingWrite] = useState<
    { kind: "save"; unitPriceFen: number } | { kind: "reset" } | null
  >(null);
  const [dialogError, setDialogError] = useState("");

  useEffect(() => {
    let cancelled = false;
    void fetchCustomerUnitPrice(userId)
      .then((result) => {
        if (cancelled) {
          return;
        }
        setPricing(result);
        setPriceYuan(formatYuanFromFen(result.unit_price_fen));
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(
            cause instanceof Error && cause.message.trim()
              ? cause.message
              : "读取客户单价失败",
          );
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  function requestSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const unitPriceFen = yuanInputToFen(priceYuan);
    if (unitPriceFen === null) {
      setError("客户售价必须是大于 0、最多两位小数的金额");
      return;
    }
    setError("");
    setDialogError("");
    setPendingWrite({ kind: "save", unitPriceFen });
  }

  function requestReset() {
    setError("");
    setDialogError("");
    setPendingWrite({ kind: "reset" });
  }

  async function persistPrice(reason: string) {
    if (!pendingWrite || saving) {
      return;
    }
    const unitPriceFen =
      pendingWrite.kind === "save" ? pendingWrite.unitPriceFen : null;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const updated = await updateCustomerUnitPrice(
        userId,
        unitPriceFen,
        reason,
      );
      setPricing(updated);
      setPriceYuan(formatYuanFromFen(updated.unit_price_fen));
      setNotice(
        unitPriceFen === null ? "已恢复全局默认售价" : "客户售价已保存",
      );
      setPendingWrite(null);
    } catch (cause) {
      setDialogError(
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : "保存客户售价失败",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <section aria-label="客户售价" className="customer-detail-section">
      <h3>客户售价</h3>
      {loading ? <p className="admin-hint">正在读取客户售价…</p> : null}
      {pricing ? (
        <p className="admin-hint">
          当前 {formatFen(pricing.unit_price_fen)} / 秒 · 全局默认{" "}
          {formatFen(pricing.default_unit_price_fen)} / 秒
          {pricing.custom_unit_price_fen === null
            ? "（使用默认）"
            : "（独立定价）"}
        </p>
      ) : null}
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {!loading && pricing && !readOnly ? (
        <form className="admin-form" onSubmit={requestSave}>
          <label>
            售价（元/秒）
            <input
              inputMode="decimal"
              min="0.01"
              step="0.01"
              type="number"
              value={priceYuan}
              onChange={(event) => setPriceYuan(event.target.value)}
            />
          </label>
          <div className="admin-actions">
            <button disabled={saving} type="submit">
              {saving ? "正在保存" : "保存客户售价"}
            </button>
            <button
              className="btn-secondary"
              disabled={saving || pricing.custom_unit_price_fen === null}
              type="button"
              onClick={requestReset}
            >
              恢复全局默认
            </button>
          </div>
        </form>
      ) : null}
      {!loading && pricing && readOnly ? (
        <p className="admin-hint">审计员仅可查看定价，不能修改。</p>
      ) : null}

      <ConfirmDialog
        busy={saving}
        confirmLabel={
          pendingWrite?.kind === "reset" ? "确认恢复默认" : "确认保存"
        }
        description={
          pendingWrite?.kind === "reset"
            ? "将清除该客户的独立定价，恢复为全局默认售价。"
            : `将把该客户售价改为 ${pendingWrite ? formatFen(pendingWrite.unitPriceFen) : ""} / 秒（仅影响该客户之后的充值换算）。`
        }
        error={dialogError}
        level="standard"
        open={pendingWrite !== null}
        title={
          pendingWrite?.kind === "reset" ? "恢复全局默认售价" : "修改客户售价"
        }
        onClose={() => {
          setPendingWrite(null);
          setDialogError("");
        }}
        onConfirm={() =>
          void persistPrice(
            pendingWrite?.kind === "reset"
              ? "恢复客户默认售价"
              : "更新客户售价",
          )
        }
      />
    </section>
  );
}
