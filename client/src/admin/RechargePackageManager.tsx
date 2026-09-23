import { useEffect, useRef, useState } from "react";
import type { CustomerRechargePackage } from "../api";
import {
  AdminControlError,
  createRechargePackage,
  listRechargePackages,
  type RechargePackageDraft,
  updateRechargePackage,
} from "../api.admin";
import {
  DISCOUNT_INTERFACE_LABELS,
  packageBenefitLabel,
} from "../rechargePackageDisplay";
import { ConfirmDialog } from "./ui/ConfirmDialog";

const INTERFACE_KEYS = Object.keys(DISCOUNT_INTERFACE_LABELS);

type DraftState = {
  name: string;
  amountYuan: string;
  credits: string;
  /** 折扣输入以「折」为单位（9 = 0.9000）；空串 = 无权益档位。 */
  zhe: string;
  interfaces: string[];
  sortOrder: string;
  isActive: boolean;
};

const EMPTY_DRAFT: DraftState = {
  name: "",
  amountYuan: "",
  credits: "",
  zhe: "",
  interfaces: [],
  sortOrder: "0",
  isActive: true,
};

function rateToZheInput(rate: string | null): string {
  if (rate === null) {
    return "";
  }
  const zhe = Number((Number(rate) * 10).toFixed(2));
  return Number.isFinite(zhe) ? String(zhe) : "";
}

function draftFromPackage(pkg: CustomerRechargePackage): DraftState {
  return {
    name: pkg.name,
    amountYuan: formatYuan(pkg.amount_fen),
    credits: String(pkg.credits),
    zhe: rateToZheInput(pkg.discount_rate),
    interfaces: [...pkg.discount_interfaces],
    sortOrder: String(pkg.sort_order),
    isActive: pkg.is_active,
  };
}

function formatYuan(fen: number): string {
  const yuan = fen / 100;
  return Number.isInteger(yuan) ? String(yuan) : yuan.toFixed(2);
}

type ParseResult =
  | { ok: true; value: RechargePackageDraft }
  | { ok: false; error: string };

function parseDraft(draft: DraftState): ParseResult {
  const name = draft.name.trim();
  if (!name) {
    return { ok: false, error: "请填写套餐名称。" };
  }
  const amountYuan = Number(draft.amountYuan);
  const amountFen = Math.round(amountYuan * 100);
  if (
    !Number.isFinite(amountYuan) ||
    amountYuan <= 0 ||
    !Number.isSafeInteger(amountFen) ||
    amountFen < 1
  ) {
    return {
      ok: false,
      error: "充值金额须为大于 0 的元金额（最多两位小数）。",
    };
  }
  const credits = Number(draft.credits);
  if (!Number.isSafeInteger(credits) || credits < 1 || credits > 2147483647) {
    return { ok: false, error: "到账积分必须为 1–2147483647 的整数。" };
  }
  const sortOrder = Number(draft.sortOrder);
  if (!Number.isSafeInteger(sortOrder) || sortOrder < 0) {
    return { ok: false, error: "排序必须为不小于 0 的整数。" };
  }
  const zheText = draft.zhe.trim();
  let discountRate: string | null = null;
  if (zheText) {
    const zhe = Number(zheText);
    if (!Number.isFinite(zhe) || zhe <= 0 || zhe > 10) {
      return { ok: false, error: "折扣须为 0–10 之间的折数（9 表示 9 折）。" };
    }
    discountRate = (zhe / 10).toFixed(4);
  }
  const interfaces = discountRate === null ? [] : [...draft.interfaces];
  return {
    ok: true,
    value: {
      name,
      amount_fen: amountFen,
      credits,
      discount_rate: discountRate,
      discount_interfaces: interfaces,
      sort_order: sortOrder,
      is_active: draft.isActive,
    },
  };
}

/**
 * 充值套餐管理（系统设置 → 支付与价格）：配置档位金额、到账积分（含赠送）与
 * 消耗侧折扣（如视频生成 9 折）。写入走管理写契约（原因 + 幂等键 + 乐观锁）。
 * 折扣在客户充值到账后授予且永久有效；更新套餐不回溯历史订单与已授权益。
 */
export function RechargePackageManager({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [packages, setPackages] = useState<CustomerRechargePackage[] | null>(
    null,
  );
  const [draft, setDraft] = useState<DraftState>(EMPTY_DRAFT);
  const [editing, setEditing] = useState<CustomerRechargePackage | null>(null);
  const [pending, setPending] = useState<"create" | "update" | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const retry = useRef<{ fingerprint: string; key: string } | null>(null);

  useEffect(() => {
    void refresh;
    let active = true;
    setPackages(null);
    setError("");
    void listRechargePackages()
      .then((items) => {
        if (active) setPackages(items);
      })
      .catch((cause) => {
        if (active) {
          setError(cause instanceof Error ? cause.message : "读取充值套餐失败");
        }
      });
    return () => {
      active = false;
    };
  }, [refresh]);

  function startCreate() {
    setEditing(null);
    setDraft(EMPTY_DRAFT);
    setError("");
    setNotice("");
  }

  function startEdit(pkg: CustomerRechargePackage) {
    setEditing(pkg);
    setDraft(draftFromPackage(pkg));
    setError("");
    setNotice("");
  }

  function requestToggle(pkg: CustomerRechargePackage) {
    setEditing(pkg);
    setDraft({ ...draftFromPackage(pkg), isActive: !pkg.is_active });
    setError("");
    setNotice("");
    setPending("update");
  }

  async function submitConfirmed(reason: string) {
    const parsed = parseDraft(draft);
    if (!parsed.ok) {
      setError(parsed.error);
      setPending(null);
      return;
    }
    const fingerprint = JSON.stringify({
      editingId: editing?.id ?? null,
      version: editing?.version ?? null,
      draft: parsed.value,
      reason,
    });
    if (retry.current?.fingerprint !== fingerprint) {
      retry.current = { fingerprint, key: crypto.randomUUID() };
    }
    setBusy(true);
    setError("");
    try {
      if (editing) {
        await updateRechargePackage(
          editing.id,
          parsed.value,
          editing.version,
          reason,
          retry.current.key,
        );
        setNotice(
          `套餐「${parsed.value.name}」已更新；历史订单与已授予权益不受影响。`,
        );
      } else {
        await createRechargePackage(parsed.value, reason, retry.current.key);
        setNotice(`套餐「${parsed.value.name}」已创建，客户充值页立即可见。`);
      }
      retry.current = null;
      setPending(null);
      setEditing(null);
      setDraft(EMPTY_DRAFT);
      setRefresh((value) => value + 1);
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "保存充值套餐失败，请重试",
      );
      if (
        cause instanceof AdminControlError &&
        cause.status !== undefined &&
        cause.status < 500
      ) {
        // 4xx 是确定性结果（含 409 版本冲突）：换新键重试才有意义。
        retry.current = null;
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      className="admin-panel admin-recharge-packages"
      aria-label="充值套餐"
    >
      <h2>充值套餐</h2>
      <p className="admin-hint">
        配置客户充值页的档位：充值金额、到账积分（可含赠送，如 1998 元到账 2000
        积分）与消耗侧折扣（如视频生成 9
        折）。折扣在充值到账后自动授予且永久有效，
        再次购买带权益套餐时最近一次生效；未配置折扣的档位不影响已有权益。
      </p>
      {error ? (
        <div role="alert">
          {error}{" "}
          <button
            disabled={busy}
            onClick={() => setRefresh((value) => value + 1)}
            type="button"
          >
            刷新套餐
          </button>
        </div>
      ) : null}
      {notice ? <p role="status">{notice}</p> : null}
      {!packages ? (
        !error && <p role="status">正在读取充值套餐…</p>
      ) : (
        <>
          <div className="table-scroll">
            <table className="internal-table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>充值金额</th>
                  <th>到账积分</th>
                  <th>权益</th>
                  <th>排序</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {packages.length ? (
                  packages.map((pkg) => (
                    <tr key={pkg.id}>
                      <td>{pkg.name}</td>
                      <td>{formatYuan(pkg.amount_fen)} 元</td>
                      <td>{pkg.credits}</td>
                      <td>{packageBenefitLabel(pkg) ?? "—"}</td>
                      <td>{pkg.sort_order}</td>
                      <td>{pkg.is_active ? "启用" : "停用"}</td>
                      <td>
                        {readOnly ? (
                          "—"
                        ) : (
                          <>
                            <button
                              className="table-action-button"
                              onClick={() => startEdit(pkg)}
                              type="button"
                            >
                              编辑
                            </button>
                            <button
                              className="table-action-button"
                              onClick={() => requestToggle(pkg)}
                              type="button"
                            >
                              {pkg.is_active ? "停用" : "启用"}
                            </button>
                          </>
                        )}
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={7}>暂无充值套餐</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {!readOnly ? (
            <form
              className="admin-recharge-packages__form"
              onSubmit={(event) => {
                event.preventDefault();
                setPending(editing ? "update" : "create");
              }}
            >
              <h3>{editing ? `编辑：${editing.name}` : "新建套餐"}</h3>
              <label>
                套餐名称
                <input
                  disabled={busy}
                  onChange={(event) =>
                    setDraft({ ...draft, name: event.target.value })
                  }
                  value={draft.name}
                />
              </label>
              <label>
                充值金额（元）
                <input
                  disabled={busy}
                  inputMode="decimal"
                  min="0.01"
                  step="0.01"
                  type="number"
                  value={draft.amountYuan}
                  onChange={(event) =>
                    setDraft({ ...draft, amountYuan: event.target.value })
                  }
                />
              </label>
              <label>
                到账积分
                <input
                  disabled={busy}
                  inputMode="numeric"
                  min="1"
                  step="1"
                  type="number"
                  value={draft.credits}
                  onChange={(event) =>
                    setDraft({ ...draft, credits: event.target.value })
                  }
                />
              </label>
              <label>
                折扣（折，留空表示无折扣）
                <input
                  disabled={busy}
                  inputMode="decimal"
                  min="0.1"
                  max="10"
                  placeholder="如 9 表示 9 折"
                  step="0.1"
                  type="number"
                  value={draft.zhe}
                  onChange={(event) => {
                    const zhe = event.target.value;
                    // 清除折扣时同步清空接口范围：服务端拒绝「无折扣却勾接口」。
                    setDraft({
                      ...draft,
                      zhe,
                      interfaces: zhe.trim() ? draft.interfaces : [],
                    });
                  }}
                />
              </label>
              <fieldset disabled={busy || !draft.zhe.trim()}>
                <legend>折扣适用接口（不勾选 = 全部消耗）</legend>
                {INTERFACE_KEYS.map((key) => (
                  <label key={key}>
                    <input
                      checked={draft.interfaces.includes(key)}
                      onChange={(event) =>
                        setDraft({
                          ...draft,
                          interfaces: event.target.checked
                            ? [...draft.interfaces, key]
                            : draft.interfaces.filter((item) => item !== key),
                        })
                      }
                      type="checkbox"
                    />
                    {DISCOUNT_INTERFACE_LABELS[key]}
                  </label>
                ))}
              </fieldset>
              <label>
                排序（小在前）
                <input
                  disabled={busy}
                  inputMode="numeric"
                  min="0"
                  step="1"
                  type="number"
                  value={draft.sortOrder}
                  onChange={(event) =>
                    setDraft({ ...draft, sortOrder: event.target.value })
                  }
                />
              </label>
              <label className="admin-dialog__ack">
                <input
                  checked={draft.isActive}
                  disabled={busy}
                  onChange={(event) =>
                    setDraft({ ...draft, isActive: event.target.checked })
                  }
                  type="checkbox"
                />
                启用（客户充值页可见）
              </label>
              <div className="admin-actions">
                <button disabled={busy} type="submit">
                  {editing ? "保存修改" : "新建套餐"}
                </button>
                {editing ? (
                  <button disabled={busy} onClick={startCreate} type="button">
                    取消编辑
                  </button>
                ) : null}
              </div>
            </form>
          ) : null}
        </>
      )}
      <ConfirmDialog
        busy={busy}
        confirmLabel={editing ? "确认保存" : "确认新建"}
        description={
          editing
            ? "保存后立即对新的充值订单生效；历史订单与已授予权益不变。"
            : "新建后立即出现在客户充值页（启用状态下）。"
        }
        error={error}
        onClose={() => {
          if (!busy) {
            setPending(null);
          }
        }}
        onConfirm={(reason) => {
          void submitConfirmed(reason);
        }}
        open={pending !== null}
        title={editing ? "保存充值套餐修改" : "新建充值套餐"}
      />
    </section>
  );
}
