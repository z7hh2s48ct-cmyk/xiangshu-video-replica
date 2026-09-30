import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  adminActivationErrorMessage,
  getRegistrationBonusSettings,
  type RegistrationBonusFields,
  type RegistrationBonusSettings,
  updateRegistrationBonusSettings,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { formatDateTime } from "./ui/vocabulary";

// 与服务端 StrictInt 校验、迁移 CHECK 同口径：0 = 关闭，上界对齐钱包
// int4。前端先拦一道只为少一次往返，服务端才是真正的校验方。
const MAX_BONUS_CREDITS = 2147483647;

interface Draft {
  credits: string;
}

function toDraft(settings: RegistrationBonusSettings): Draft {
  return { credits: String(settings.bonus_credits) };
}

function toFields(
  settings: RegistrationBonusSettings,
): RegistrationBonusFields {
  return { bonus_credits: settings.bonus_credits };
}

function parseDraft(
  draft: Draft,
):
  | { ok: true; fields: RegistrationBonusFields }
  | { ok: false; message: string } {
  const credits = Number(draft.credits.trim());
  if (
    !draft.credits.trim() ||
    !Number.isInteger(credits) ||
    credits < 0 ||
    credits > MAX_BONUS_CREDITS
  ) {
    return {
      ok: false,
      message: `注册赠送积分需为 0 到 ${MAX_BONUS_CREDITS} 之间的整数（0 表示关闭）`,
    };
  }
  return { ok: true, fields: { bonus_credits: credits } };
}

function sameFields(a: RegistrationBonusFields, b: RegistrationBonusFields) {
  return a.bonus_credits === b.bonus_credits;
}

/**
 * 注册赠送积分设置：新注册主账号一次性发放的积分数。
 *
 * 只影响此后新注册的主账号——注册端点只创建主账号，子账号由主账号自建
 * 且没有独立钱包，天然不在发放范围；已注册账号不补发。文案必须把这条
 * 生效范围说清，不能让运营误以为改了会补发存量客户。
 */
export function RegistrationBonusSection({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [settings, setSettings] = useState<RegistrationBonusSettings | null>(
    null,
  );
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [formError, setFormError] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const [saving, setSaving] = useState(false);
  // 同一份内容失败后重试沿用同一幂等键（服务端重放不重复写审计），
  // 改了内容才换键——与告警设置同一做法。
  const retryRef = useRef<{ fingerprint: string; key: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const current = await getRegistrationBonusSettings();
      setSettings(current);
      setDraft(toDraft(current));
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "读取注册赠送设置失败"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading && !settings) {
    return <p className="admin-hint">正在读取注册赠送设置…</p>;
  }
  if (!settings || !draft) {
    return (
      <section aria-label="注册赠送积分" className="admin-panel">
        <h2>注册赠送积分</h2>
        {error ? <PageBanner tone="error">{error}</PageBanner> : null}
        <div className="admin-actions">
          <button disabled={loading} type="button" onClick={() => void load()}>
            重新读取
          </button>
        </div>
      </section>
    );
  }

  const parsed = parseDraft(draft);
  const dirty = !parsed.ok || !sameFields(parsed.fields, toFields(settings));

  function patch(next: Partial<Draft>) {
    setDraft((current) => (current ? { ...current, ...next } : current));
    setFormError("");
    setNotice("");
  }

  function requestSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!parsed.ok) {
      setFormError(parsed.message);
      return;
    }
    setFormError("");
    setDialogError("");
    setDialogOpen(true);
  }

  async function persist(reason: string) {
    if (!parsed.ok || saving) {
      return;
    }
    const fingerprint = JSON.stringify(parsed.fields);
    if (retryRef.current?.fingerprint !== fingerprint) {
      retryRef.current = { fingerprint, key: crypto.randomUUID() };
    }
    setSaving(true);
    setDialogError("");
    try {
      const updated = await updateRegistrationBonusSettings(
        parsed.fields,
        reason,
        retryRef.current.key,
      );
      retryRef.current = null;
      setSettings(updated);
      setDraft(toDraft(updated));
      setNotice(
        updated.bonus_credits > 0
          ? `注册赠送设置已保存：此后新注册的主账号将一次性获得 ${updated.bonus_credits} 积分；已注册账号不补发。`
          : "注册赠送设置已保存：赠送已关闭，此后新注册的主账号不再自动获得积分。",
      );
      setDialogOpen(false);
    } catch (cause) {
      setDialogError(
        adminActivationErrorMessage(cause, "保存注册赠送设置失败"),
      );
    } finally {
      setSaving(false);
    }
  }

  const updaterLabel = settings.updated_by_display_name?.trim();

  return (
    <section aria-label="注册赠送积分" className="admin-panel">
      <h2>注册赠送积分</h2>
      <p className="admin-hint">
        决定「新客户注册成功时一次性到账多少积分」。仅主账号注册时发放：
        子账号由主账号创建、没有独立积分，不在此列；只影响此后新注册的
        账号，已注册账号不补发。填 0 表示关闭赠送。
      </p>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

      {readOnly ? (
        <>
          <dl className="admin-settings-view">
            <div>
              <dt>注册赠送积分</dt>
              <dd>
                {settings.bonus_credits > 0
                  ? `${settings.bonus_credits} 积分`
                  : "已关闭（0 积分）"}
              </dd>
            </div>
          </dl>
          <p className="admin-hint">审计员仅可查看注册赠送设置，不能修改。</p>
        </>
      ) : (
        <form className="admin-form" onSubmit={requestSave}>
          <label>
            注册赠送积分（0 表示关闭）
            <input
              inputMode="numeric"
              value={draft.credits}
              onChange={(event) => patch({ credits: event.target.value })}
            />
          </label>
          {settings.updated_at ? (
            <p className="admin-hint">
              最后修改 {formatDateTime(settings.updated_at)}
              {updaterLabel ? `（${updaterLabel}）` : ""}
            </p>
          ) : null}
          {formError ? (
            <p className="settings-error" role="alert">
              {formError}
            </p>
          ) : null}
          <div className="admin-actions">
            <button disabled={saving || !dirty} type="submit">
              保存注册赠送设置
            </button>
            <button
              disabled={saving || !dirty}
              type="button"
              onClick={() => {
                setDraft(toDraft(settings));
                setFormError("");
              }}
            >
              还原
            </button>
          </div>
        </form>
      )}

      <ConfirmDialog
        busy={saving}
        confirmLabel="确认保存"
        description={
          parsed.ok && parsed.fields.bonus_credits > 0
            ? `此后新注册的主账号将一次性获得 ${parsed.fields.bonus_credits} 积分；子账号不发放，已注册账号不补发。`
            : "保存后注册赠送关闭：此后新注册的主账号不再自动获得积分。"
        }
        error={dialogError}
        level="reason"
        open={dialogOpen}
        title="保存注册赠送设置"
        onClose={() => {
          setDialogOpen(false);
          setDialogError("");
        }}
        onConfirm={(reason) => void persist(reason)}
      />
    </section>
  );
}
