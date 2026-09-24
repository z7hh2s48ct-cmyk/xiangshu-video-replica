import { useEffect, useRef, useState } from "react";
import {
  applyLegacyCreditConversion,
  getLegacyCreditConversion,
  getLegacyCreditPolicy,
  type LegacyCreditConversion,
  type LegacyCreditPolicy,
  saveLegacyCreditPolicy,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";

export function LegacyCreditPolicyManager({
  userId,
  readOnly = false,
  onChanged,
}: {
  userId?: string;
  readOnly?: boolean;
  onChanged?: () => void;
}) {
  const [policy, setPolicy] = useState<LegacyCreditPolicy | null>(null);
  const [preview, setPreview] = useState<LegacyCreditConversion | null>(null);
  const [mode, setMode] = useState<"keep" | "convert">("keep");
  const [numerator, setNumerator] = useState("1");
  const [denominator, setDenominator] = useState("1");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const retry = useRef<{ fingerprint: string; key: string } | null>(null);
  useEffect(() => {
    void refresh;
    let active = true;
    setError("");
    setPolicy(null);
    setPreview(null);
    const operation = userId
      ? getLegacyCreditConversion(userId)
      : getLegacyCreditPolicy();
    void operation
      .then((value) => {
        if (!active) return;
        const p = "policy" in value ? value.policy : value;
        setPolicy(p);
        setMode(p.mode);
        setNumerator(String(p.numerator));
        setDenominator(String(p.denominator));
        if ("policy" in value) setPreview(value);
      })
      .catch((cause) => {
        if (active)
          setError(
            cause instanceof Error ? cause.message : "历史积分信息加载失败",
          );
      });
    return () => {
      active = false;
    };
  }, [userId, refresh]);
  async function write(reason: string) {
    if (busy || !policy) return;
    const input = {
      expected_version: policy.version,
      mode,
      numerator: mode === "keep" ? 1 : Number(numerator),
      denominator: mode === "keep" ? 1 : Number(denominator),
    };
    if (
      !userId &&
      (!Number.isSafeInteger(input.numerator) ||
        !Number.isSafeInteger(input.denominator) ||
        input.numerator < 1 ||
        input.denominator < 1 ||
        input.numerator > 1000000 ||
        input.denominator > 1000000)
    ) {
      setError("比例必须为 1–1000000 的整数");
      return;
    }
    const fingerprint = JSON.stringify({ userId, input, preview, reason });
    if (retry.current?.fingerprint !== fingerprint)
      retry.current = { fingerprint, key: crypto.randomUUID() };
    setBusy(true);
    setError("");
    try {
      if (userId) {
        if (!preview) return;
        setPreview(
          await applyLegacyCreditConversion(
            userId,
            preview,
            reason,
            retry.current.key,
          ),
        );
        onChanged?.();
      } else {
        setPolicy(
          await saveLegacyCreditPolicy(input, reason, retry.current.key),
        );
      }
      setNotice(
        userId
          ? "本账号已完成历史积分转换。"
          : "历史积分策略已保存；已有余额须逐账号预览后执行。",
      );
      setConfirm(false);
      retry.current = null;
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "操作失败，请重新预览后重试",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="customer-detail-section" aria-label="历史积分转换">
      <h3>{userId ? "历史余额转换" : "历史余额处理策略"}</h3>
      <p>
        仅用于旧激活账号，每个账号可执行一次。已有新积分交易、待结算积分或可补付订单时需要先核对账务。
      </p>
      {error && <PageBanner tone="error">{error}</PageBanner>}
      {notice && <PageBanner tone="notice">{notice}</PageBanner>}
      <button
        type="button"
        disabled={busy}
        onClick={() => {
          setConfirm(false);
          setRefresh((v) => v + 1);
        }}
      >
        重新预览
      </button>
      {!policy && !error && <p role="status">正在读取历史积分策略…</p>}
      {policy && (
        <>
          <p>
            策略 V{policy.version} ·{" "}
            {policy.mode === "keep"
              ? "保留原数值"
              : `原余额 × ${policy.numerator} ÷ ${policy.denominator}，向下取整`}
          </p>
          {userId && preview ? (
            <>
              <p>
                {preview.before_credits} 积分 → {preview.after_credits} 积分
              </p>
              {preview.converted ? (
                <p>该账号已完成转换；以上是执行时的账务快照。</p>
              ) : (
                !readOnly && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => setConfirm(true)}
                  >
                    执行本账号转换
                  </button>
                )
              )}
            </>
          ) : (
            !userId && (
              <form
                className="admin-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  setConfirm(true);
                }}
              >
                <label>
                  处理方式
                  <select
                    disabled={readOnly || busy}
                    value={mode}
                    onChange={(e) =>
                      setMode(e.target.value as "keep" | "convert")
                    }
                  >
                    <option value="keep">原数值保留为积分</option>
                    <option value="convert">按比例转换</option>
                  </select>
                </label>
                {mode === "convert" && (
                  <>
                    <label>
                      乘数
                      <input
                        type="number"
                        min={1}
                        max={1000000}
                        step={1}
                        required
                        disabled={readOnly || busy}
                        value={numerator}
                        onChange={(e) => setNumerator(e.target.value)}
                      />
                    </label>
                    <label>
                      除数
                      <input
                        type="number"
                        min={1}
                        max={1000000}
                        step={1}
                        required
                        disabled={readOnly || busy}
                        value={denominator}
                        onChange={(e) => setDenominator(e.target.value)}
                      />
                    </label>
                  </>
                )}
                {!readOnly && (
                  <button type="submit" disabled={busy}>
                    保存历史积分策略
                  </button>
                )}
              </form>
            )
          )}
        </>
      )}
      <ConfirmDialog
        open={confirm}
        busy={busy}
        title={userId ? "执行一次性余额转换" : "保存历史积分策略"}
        description={
          userId
            ? `将本账号 ${preview?.before_credits} 积分转换为 ${preview?.after_credits} 积分。原流水保留，差额另记账。`
            : "修改只影响后续预览和执行，不会自动批量改动用户余额。"
        }
        confirmLabel="确认执行"
        level="reasonAndAck"
        error={error}
        onClose={() => {
          if (!busy) setConfirm(false);
        }}
        onConfirm={(reason) => void write(reason)}
      />
    </section>
  );
}
