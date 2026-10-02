import { useRef } from "react";
import type {
  AlertFailureRule,
  AlertNotificationPolicy,
  AlertRecipientCandidate,
} from "../api.admin";
import { GENERATION_RECORD_TYPE_LABELS } from "./ui/vocabulary";
import "./alert-rules.css";

export const ALERT_LABELS: Record<string, string> = {
  failure_rate: "失败率",
  unconfigured_rates: "成本未配置",
  reconciliation: "资金对账",
  sensitive_events: "高敏操作",
  collection_budget: "采集预算80%",
};

export function AlertRuleEditor({
  policies,
  rules,
  candidates,
  onPolicies,
  onRules,
  readOnly = false,
}: {
  policies?: AlertNotificationPolicy[];
  rules?: AlertFailureRule[];
  candidates: AlertRecipientCandidate[];
  onPolicies: (v: AlertNotificationPolicy[]) => void;
  onRules: (v: AlertFailureRule[]) => void;
  readOnly?: boolean;
}) {
  const rowIds = useRef<string[]>([]);
  while (rowIds.current.length < (rules?.length ?? 0))
    rowIds.current.push(crypto.randomUUID());
  const rowId = (i: number) => rowIds.current[i];
  const effective = Object.keys(ALERT_LABELS).map(
    (key) =>
      policies?.find((p) => p.key === key) ?? {
        key: key as AlertNotificationPolicy["key"],
        enabled: true,
        threshold_count: 1,
        window_minutes: 1440,
        channel: "email" as const,
        recipient_user_id: null,
      },
  );
  function patchPolicy(key: string, value: Partial<AlertNotificationPolicy>) {
    onPolicies(effective.map((p) => (p.key === key ? { ...p, ...value } : p)));
  }
  function patchRule(index: number, value: Partial<AlertFailureRule>) {
    onRules(
      (rules ?? []).map((r, i) => (i === index ? { ...r, ...value } : r)),
    );
  }
  if (readOnly)
    return (
      <section aria-label="独立告警规则">
        <h3>各类阈值与通知路由</h3>
        {effective.map((p) => (
          <p key={p.key}>
            {ALERT_LABELS[p.key]} · {p.enabled ? "启用" : "暂停"} · 数量阈值
            {p.threshold_count} · {p.channel ? "现有邮件通道" : "待配置"} ·{" "}
            {p.recipient_user_id
              ? (candidates.find((c) => c.user_id === p.recipient_user_id)
                  ?.display_name ?? "指定管理账号")
              : "沿用当前接收人"}
          </p>
        ))}
        <h3>类型与错误码独立失败率</h3>
        {(rules ?? []).map((r) => (
          <p key={`${r.record_type}:${r.error_code ?? "*"}`}>
            {GENERATION_RECORD_TYPE_LABELS[r.record_type] ?? "任务"} ·{" "}
            {r.error_code ?? "本类型整体"} · 阈值{r.threshold_percent}% ·
            最小样本{r.min_sample_size}
          </p>
        ))}
      </section>
    );
  return (
    <section aria-label="独立告警规则" className="admin-alert-rule-editor">
      <h3>各类阈值与通知路由</h3>
      <p>
        收件人复用已启用的管理账号；沿用表示保持当前接收配置。成本、对账和高敏按各自数量阈值；预算按已知成本80%触发、每月去重，未知费用单列。
      </p>
      {effective.map((policy) => (
        <fieldset key={policy.key} disabled={readOnly}>
          <legend>{ALERT_LABELS[policy.key]}</legend>
          <label>
            <input
              type="checkbox"
              checked={policy.enabled}
              onChange={(e) =>
                patchPolicy(policy.key, { enabled: e.target.checked })
              }
            />
            {ALERT_LABELS[policy.key]}启用
          </label>
          {!["failure_rate", "collection_budget"].includes(policy.key) ? (
            <label>
              {ALERT_LABELS[policy.key]}数量阈值
              <input
                type="number"
                min="1"
                value={policy.threshold_count}
                onChange={(e) =>
                  patchPolicy(policy.key, {
                    threshold_count: Number(e.target.value),
                  })
                }
              />
            </label>
          ) : null}
          {policy.key === "sensitive_events" ? (
            <label>
              高敏统计窗口（分钟）
              <input
                type="number"
                min="1"
                max="10080"
                value={policy.window_minutes}
                onChange={(e) =>
                  patchPolicy(policy.key, {
                    window_minutes: Number(e.target.value),
                  })
                }
              />
            </label>
          ) : null}
          <label>
            {ALERT_LABELS[policy.key]}通知通道
            <select
              value={policy.channel ?? ""}
              onChange={(e) =>
                patchPolicy(policy.key, {
                  channel: e.target.value === "email" ? "email" : null,
                })
              }
            >
              <option value="">未配置</option>
              <option value="email">现有邮件通道</option>
            </select>
          </label>
          <label>
            {ALERT_LABELS[policy.key]}接收人
            <select
              value={policy.recipient_user_id ?? ""}
              onChange={(e) =>
                patchPolicy(policy.key, {
                  recipient_user_id: e.target.value || null,
                })
              }
            >
              <option value="">沿用当前接收人</option>
              {candidates.map((c) => (
                <option key={c.user_id} value={c.user_id}>
                  {c.display_name || c.username}
                </option>
              ))}
            </select>
          </label>
        </fieldset>
      ))}
      <h3>类型与错误码独立失败率</h3>
      <p>
        错误码留空表示本类型整体；每个错误码的分母始终为该类型窗口内的全部终局任务。样本不足不发告警。
      </p>
      {(rules ?? []).map((rule, index) => (
        <fieldset key={rowId(index)} disabled={readOnly}>
          <legend>独立规则 {index + 1}</legend>
          <label>
            规则{index + 1}类型
            <select
              value={rule.record_type}
              onChange={(e) =>
                patchRule(index, { record_type: e.target.value })
              }
            >
              {Object.entries(GENERATION_RECORD_TYPE_LABELS).map(
                ([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ),
              )}
            </select>
          </label>
          <label>
            规则{index + 1}错误码
            <input
              value={rule.error_code ?? ""}
              maxLength={200}
              onChange={(e) =>
                patchRule(index, { error_code: e.target.value.trim() || null })
              }
            />
          </label>
          <label>
            规则{index + 1}失败率（%）
            <input
              type="number"
              min="0"
              max="100"
              value={rule.threshold_percent}
              onChange={(e) =>
                patchRule(index, { threshold_percent: Number(e.target.value) })
              }
            />
          </label>
          <label>
            规则{index + 1}最小样本
            <input
              type="number"
              min="1"
              value={rule.min_sample_size}
              onChange={(e) =>
                patchRule(index, { min_sample_size: Number(e.target.value) })
              }
            />
          </label>
          {!readOnly ? (
            <button
              type="button"
              onClick={() =>
                (() => {
                  rowIds.current.splice(index, 1);
                  onRules((rules ?? []).filter((_r, i) => i !== index));
                })()
              }
            >
              删除规则 {index + 1}
            </button>
          ) : null}
        </fieldset>
      ))}
      {!readOnly ? (
        <button
          type="button"
          onClick={() =>
            onRules([
              ...(rules ?? []),
              {
                record_type: "VIDEO",
                error_code: null,
                threshold_percent: 30,
                min_sample_size: 5,
              },
            ])
          }
        >
          添加类型/错误码阈值
        </button>
      ) : null}
    </section>
  );
}
