import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  type AlertRecipientCandidate,
  type AlertSettings,
  type AlertSettingsFields,
  adminActivationErrorMessage,
  getAlertSettings,
  listAlertRecipientCandidates,
  updateAlertSettings,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { formatDateTime, roleLabel } from "./ui/vocabulary";

// 与服务端 AlertSettingsUpdate / 迁移 CHECK 同口径：窗口最长 7 天，阈值 0–100，
// 样本量至少 1。前端先拦一道只为少一次往返，服务端才是真正的校验方。
const MAX_WINDOW_MINUTES = 10080;

interface Draft {
  recipient: string;
  window: string;
  threshold: string;
  minSample: string;
}

function toDraft(settings: AlertSettings): Draft {
  return {
    recipient: settings.recipient_user_id ?? "",
    window: String(settings.failure_rate_window_minutes),
    threshold: String(settings.failure_rate_threshold_percent),
    minSample: String(settings.failure_rate_min_sample),
  };
}

function toFields(settings: AlertSettings): AlertSettingsFields {
  return {
    recipient_user_id: settings.recipient_user_id,
    failure_rate_window_minutes: settings.failure_rate_window_minutes,
    failure_rate_threshold_percent: settings.failure_rate_threshold_percent,
    failure_rate_min_sample: settings.failure_rate_min_sample,
  };
}

function parseDraft(
  draft: Draft,
): { ok: true; fields: AlertSettingsFields } | { ok: false; message: string } {
  const window = Number(draft.window.trim());
  if (
    !draft.window.trim() ||
    !Number.isInteger(window) ||
    window < 1 ||
    window > MAX_WINDOW_MINUTES
  ) {
    return {
      ok: false,
      message: `统计窗口需为 1 到 ${MAX_WINDOW_MINUTES} 之间的整数分钟`,
    };
  }
  const threshold = Number(draft.threshold.trim());
  if (
    !draft.threshold.trim() ||
    !Number.isFinite(threshold) ||
    threshold < 0 ||
    threshold > 100
  ) {
    return { ok: false, message: "失败率阈值需为 0 到 100 之间的数字" };
  }
  const minSample = Number(draft.minSample.trim());
  if (
    !draft.minSample.trim() ||
    !Number.isInteger(minSample) ||
    minSample < 1
  ) {
    return { ok: false, message: "最小样本量需为不小于 1 的整数" };
  }
  return {
    ok: true,
    fields: {
      recipient_user_id: draft.recipient || null,
      failure_rate_window_minutes: window,
      failure_rate_threshold_percent: threshold,
      failure_rate_min_sample: minSample,
    },
  };
}

function sameFields(a: AlertSettingsFields, b: AlertSettingsFields): boolean {
  return (
    a.recipient_user_id === b.recipient_user_id &&
    a.failure_rate_window_minutes === b.failure_rate_window_minutes &&
    a.failure_rate_threshold_percent === b.failure_rate_threshold_percent &&
    a.failure_rate_min_sample === b.failure_rate_min_sample
  );
}

/**
 * 告警口径设置（方案 P2-4）：统计窗口、失败率阈值、最小样本量与接收人。
 *
 * 服务端早已按这行配置计算失败率报告，此前界面只能看结果、改不了口径。
 * 接收人目前只是告警页上被点名的负责人——服务端没有任何外部推送通道，
 * 文案必须如实说明，不能让运营以为设了接收人就会收到短信或邮件。
 */
export function AlertSettingsPanel({
  readOnly = false,
  onSaved,
}: {
  readOnly?: boolean;
  /** 保存成功后通知外层重新拉取失败率报告（口径变了，旧报告不再成立）。 */
  onSaved?: () => void;
}) {
  const [settings, setSettings] = useState<AlertSettings | null>(null);
  const [candidates, setCandidates] = useState<AlertRecipientCandidate[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [formError, setFormError] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const [saving, setSaving] = useState(false);
  // 同一份内容失败后重试沿用同一幂等键（服务端重放不重复写审计），
  // 改了内容才换键——与生成记录补偿同一做法。
  const retryRef = useRef<{ fingerprint: string; key: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [current, recipients] = await Promise.all([
        getAlertSettings(),
        listAlertRecipientCandidates(),
      ]);
      setSettings(current);
      setCandidates(recipients.items);
      setDraft(toDraft(current));
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "读取告警设置失败"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading && !settings) {
    return <p className="admin-hint">正在读取告警设置…</p>;
  }
  if (!settings || !draft) {
    return (
      <section aria-label="告警设置" className="admin-panel">
        <h2>告警设置</h2>
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
  // 当前接收人已被停用时不在候选里：保留一个可见选项，避免下拉悄悄显示成
  // 「未指定」，让运营误以为没人负责。保存时服务端会拒绝停用账号。
  const recipientMissing =
    settings.recipient_user_id !== null &&
    !candidates.some((item) => item.user_id === settings.recipient_user_id);

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
      const updated = await updateAlertSettings(
        parsed.fields,
        reason,
        retryRef.current.key,
      );
      retryRef.current = null;
      setSettings(updated);
      setDraft(toDraft(updated));
      setNotice("告警设置已保存，失败率报告已按新口径重新统计。");
      setDialogOpen(false);
      onSaved?.();
    } catch (cause) {
      setDialogError(adminActivationErrorMessage(cause, "保存告警设置失败"));
    } finally {
      setSaving(false);
    }
  }

  const recipientLabel = settings.recipient_user_id
    ? (settings.recipient_display_name ?? settings.recipient_user_id)
    : "未指定";

  return (
    <section aria-label="告警设置" className="admin-panel">
      <h2>告警设置</h2>
      <p className="admin-hint">
        决定「近 N
        分钟失败率超过多少算告警」。样本量不足时不告警，避免夜间低峰被个别失败刷屏。
        接收人只会在告警页上被标出为负责人，暂不发送短信或邮件等外部提醒。
      </p>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

      {readOnly ? (
        <>
          <dl className="admin-settings-view">
            <div>
              <dt>接收人</dt>
              <dd>{recipientLabel}</dd>
            </div>
            <div>
              <dt>统计窗口</dt>
              <dd>{settings.failure_rate_window_minutes} 分钟</dd>
            </div>
            <div>
              <dt>失败率阈值</dt>
              <dd>{settings.failure_rate_threshold_percent}%</dd>
            </div>
            <div>
              <dt>最小样本量</dt>
              <dd>{settings.failure_rate_min_sample} 个终局任务</dd>
            </div>
          </dl>
          <p className="admin-hint">审计员仅可查看告警设置，不能修改。</p>
        </>
      ) : (
        <form className="admin-form" onSubmit={requestSave}>
          <label>
            接收人
            <select
              value={draft.recipient}
              onChange={(event) => patch({ recipient: event.target.value })}
            >
              <option value="">未指定</option>
              {recipientMissing && settings.recipient_user_id ? (
                <option value={settings.recipient_user_id}>
                  {`${settings.recipient_display_name ?? settings.recipient_user_id}（已停用，请更换）`}
                </option>
              ) : null}
              {candidates.map((candidate) => (
                <option key={candidate.user_id} value={candidate.user_id}>
                  {`${candidate.display_name || candidate.username}（${roleLabel(candidate.role)}）`}
                </option>
              ))}
            </select>
          </label>
          <label>
            统计窗口（分钟）
            <input
              inputMode="numeric"
              value={draft.window}
              onChange={(event) => patch({ window: event.target.value })}
            />
          </label>
          <label>
            失败率阈值（%）
            <input
              inputMode="decimal"
              value={draft.threshold}
              onChange={(event) => patch({ threshold: event.target.value })}
            />
          </label>
          <label>
            最小样本量（终局任务数）
            <input
              inputMode="numeric"
              value={draft.minSample}
              onChange={(event) => patch({ minSample: event.target.value })}
            />
          </label>
          {settings.updated_at ? (
            <p className="admin-hint">
              最后修改 {formatDateTime(settings.updated_at)}
            </p>
          ) : null}
          {formError ? (
            <p className="settings-error" role="alert">
              {formError}
            </p>
          ) : null}
          <div className="admin-actions">
            <button disabled={saving || !dirty} type="submit">
              保存告警设置
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
        description="新口径立即生效，失败率报告会按新的窗口、阈值与样本量重新统计。"
        error={dialogError}
        level="reason"
        open={dialogOpen}
        title="保存告警设置"
        onClose={() => {
          setDialogOpen(false);
          setDialogError("");
        }}
        onConfirm={(reason) => void persist(reason)}
      />
    </section>
  );
}
