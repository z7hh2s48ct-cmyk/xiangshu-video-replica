import { useEffect, useState } from "react";
import { adminRead } from "../api.admin";
import { ALERT_LABELS } from "./AlertRuleEditor";

type Delivery = {
  id: string;
  alert_key: string;
  state: string;
  attempts: number;
  last_error: string | null;
  recipient_display_name: string | null;
};
type Configuration = {
  key: string;
  enabled: boolean;
  channel_configured: boolean;
  recipient_configured: boolean;
  recipient_display_name: string | null;
};
type Report = { items: Delivery[]; configuration?: Configuration[] };
const states: Record<string, string> = {
  SENT: "已发送",
  FAILED: "发送失败，后台将重试",
  CLAIMED: "正在发送，超时可恢复",
  UNCONFIGURED: "待配置",
};
const reasons: Record<string, string> = {
  CHANNEL_NOT_CONFIGURED: "邮件通道或通知模板未配置",
  RECIPIENT_NOT_CONFIGURED: "接收人邮箱未配置或账号不可用",
  EMAIL_DELIVERY_FAILED: "发送未确认成功",
};
export function AlertDeliveryStatus() {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  // biome-ignore lint/correctness/useExhaustiveDependencies: 刷新重新读取通知元数据，不触发发送。
  useEffect(() => {
    let active = true;
    void adminRead<Report>("/api/control/alerts/deliveries", "读取通知状态失败")
      .then((r) => {
        if (active) {
          setReport(r);
          setError("");
        }
      })
      .catch((e) => {
        if (active)
          setError(e instanceof Error ? e.message : "读取通知状态失败");
      });
    return () => {
      active = false;
    };
  }, [reload]);
  return (
    <section aria-label="通知投递状态">
      <h3>通知投递状态</h3>
      <button type="button" onClick={() => setReload((x) => x + 1)}>
        刷新通知状态
      </button>
      {error ? <p role="alert">{error}</p> : null}
      {report?.configuration?.map((c) => (
        <p key={c.key}>
          {ALERT_LABELS[c.key] ?? "告警"}：
          {!c.enabled
            ? "已暂停"
            : !c.channel_configured || !c.recipient_configured
              ? "待配置"
              : "配置可用，等待阈值触发"}
          {c.recipient_display_name ? ` · ${c.recipient_display_name}` : ""}
        </p>
      ))}
      <details>
        <summary>技术详情：发送记录与重试</summary>
        {report?.items.length === 0 ? <p>暂无投递记录。</p> : null}
        {report?.items.map((d) => (
          <p key={d.id}>
            {ALERT_LABELS[d.alert_key] ?? "告警"} ·{" "}
            {states[d.state] ?? "待核对"} · 尝试{d.attempts}次 ·{" "}
            {d.recipient_display_name ?? "未指定接收人"}
            {d.last_error
              ? ` · ${reasons[d.last_error] ?? "请核对通知配置"}`
              : ""}
          </p>
        ))}
      </details>
    </section>
  );
}
