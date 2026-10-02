import { useCallback, useEffect, useState } from "react";

import {
  type AdminAlertsOverview,
  type AdminFailureRateGroup,
  type AdminFailureRateReport,
  adminActivationErrorMessage,
  getAlertsOverview,
  getFailureRateAlerts,
} from "../api.admin";
import { AlertDeliveryStatus } from "./AlertDeliveryStatus";
import { AlertSettingsPanel } from "./AlertSettingsPanel";
import { PageBanner } from "./ui/PageBanner";
import { StatusBadge } from "./ui/StatusBadge";
import {
  FAILURE_CATEGORY_LABELS,
  FAILURE_OWNER_LABELS,
  GENERATION_RECORD_TYPE_LABELS,
  labelFrom,
} from "./ui/vocabulary";

/**
 * 「通知与告警」页签（方案 P1-5）：按业务类型展示近 1 小时失败率，
 * 越过阈值且样本达标的类型标红提醒技术负责人；每条错误码挂 runbook
 * 的分类/处理人/处理建议（与生成记录页同一份词典，不另起口径）。
 * 窗口 / 阈值 / 样本量 / 接收人由下方「告警设置」维护（P2-4），保存后重拉报告。
 */
export function AdminAlertsSection({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [report, setReport] = useState<AdminFailureRateReport | null>(null);
  const [overview, setOverview] = useState<AdminAlertsOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [rateReport, overviewReport] = await Promise.all([
        getFailureRateAlerts(),
        // 总览失败不拖垮失败率报告：四类告警各自独立降级。
        getAlertsOverview().catch(() => null),
      ]);
      setReport(rateReport);
      setOverview(overviewReport);
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "读取失败率告警失败"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const failedGroups = report
    ? report.groups.filter((group) => group.failed > 0)
    : [];

  // 窗口以服务端报告为准（可在下方改）；报告未到之前不猜一个具体数字。
  const windowLabel = report
    ? `近 ${report.window_minutes} 分钟`
    : "近一段时间";
  const recipientName = report?.recipient_display_name?.trim();

  return (
    <>
      {/* 方案 P2：四类告警总览先于失败率明细——danger 在前、warn 在后。 */}
      {overview && overview.items.length > 0 ? (
        <section aria-label="告警总览" className="admin-panel">
          <h2>告警总览</h2>
          {overview.items.map((item) => (
            <p
              key={item.key}
              className={
                item.severity === "danger"
                  ? "admin-alerts__overview-item admin-alerts__overview-item--danger"
                  : "admin-alerts__overview-item"
              }
              role={item.severity === "danger" ? "alert" : undefined}
            >
              <strong>{item.headline}</strong>
              <small>{item.detail}</small>
            </p>
          ))}
        </section>
      ) : null}

      <section aria-label="失败率告警" className="admin-panel">
        <header className="admin-alerts__header">
          <h2>失败率告警</h2>
          <button disabled={loading} type="button" onClick={() => void load()}>
            刷新
          </button>
        </header>
        <p className="admin-hint">
          按业务类型统计{windowLabel}
          内已终结任务的失败率（客户取消与进行中不计入）；
          越过阈值且样本充足时在这里提醒技术负责人。
        </p>

        {error ? <PageBanner tone="error">{error}</PageBanner> : null}
        {loading && !report ? <p className="admin-hint">读取中…</p> : null}

        {report ? (
          <>
            {report.alerting ? (
              <PageBanner tone="error">
                {`近 ${report.window_minutes} 分钟整体失败率 ${report.failure_rate_percent}%（${report.failed}/${report.total}），`}
                已有类型或错误码达到各自告警阈值，请技术负责人处理标记项。
                {recipientName ? `指定负责人：${recipientName}。` : null}
              </PageBanner>
            ) : (
              <PageBanner tone="notice">
                近 {report.window_minutes}{" "}
                分钟没有类型或错误码达到各自告警阈值。
              </PageBanner>
            )}

            {report.total === 0 ? (
              <p className="admin-hint">
                近 {report.window_minutes} 分钟没有已终结的任务。
              </p>
            ) : (
              <div className="admin-table-scroll">
                <table aria-label="失败率告警分组" className="admin-data-table">
                  <thead>
                    <tr>
                      <th scope="col">业务类型</th>
                      <th scope="col">终局样本</th>
                      <th scope="col">失败</th>
                      <th scope="col">失败率</th>
                      <th scope="col">状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.groups.map((group) => (
                      <tr key={group.record_type}>
                        <td>
                          {labelFrom(
                            GENERATION_RECORD_TYPE_LABELS,
                            group.record_type,
                          )}
                        </td>
                        <td>{group.total}</td>
                        <td>{group.failed}</td>
                        <td>{`${group.failure_rate_percent}%`}</td>
                        <td>
                          <GroupStatusBadge
                            group={group}
                            minSample={
                              group.min_sample_size ?? report.min_sample_size
                            }
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {failedGroups.length > 0 ? (
              <section aria-label="主要错误码">
                <h3>主要错误码</h3>
                {failedGroups.map((group) => (
                  <ErrorGroup key={group.record_type} group={group} />
                ))}
              </section>
            ) : null}
          </>
        ) : null}
      </section>
      <AlertDeliveryStatus />
      <AlertSettingsPanel readOnly={readOnly} onSaved={() => void load()} />
    </>
  );
}

/**
 * 状态徽标按「要不要管」三档表达：超阈值=需要干预；样本不足时失败率
 * 再高也不告警（服务端口径），不能写成「正常」误导值班人。
 */
function GroupStatusBadge({
  group,
  minSample,
}: {
  group: AdminFailureRateGroup;
  minSample: number;
}) {
  if (group.exceeded) {
    return <StatusBadge tone="danger">超阈值</StatusBadge>;
  }
  if (group.top_errors.some((error) => error.exceeded)) {
    return <StatusBadge tone="danger">错误码告警</StatusBadge>;
  }
  if (group.failed > 0 && group.total < minSample) {
    return <StatusBadge tone="warn">样本不足</StatusBadge>;
  }
  if (group.failed > 0) {
    return <StatusBadge tone="neutral">低于阈值</StatusBadge>;
  }
  return <StatusBadge tone="good">正常</StatusBadge>;
}

function ErrorGroup({ group }: { group: AdminFailureRateGroup }) {
  return (
    <div>
      <h4>
        {labelFrom(GENERATION_RECORD_TYPE_LABELS, group.record_type)}
        {group.exceeded ? "（超阈值）" : ""}
      </h4>
      <ul>
        {group.top_errors.map((error) => (
          <li
            className="admin-alerts__error"
            key={error.error_code ?? "unknown"}
          >
            <span>
              {error.error_code ?? "未记录错误码"}
              {" · "}
              {error.count} 条
              {error.total !== undefined
                ? ` / ${error.total}个本类型终局任务，失败率 ${error.failure_rate_percent}%（阈值${error.threshold_percent}%，最小样本${error.min_sample_size}）${error.exceeded ? " · 超阈值" : ""}`
                : ""}
            </span>
            {error.category || error.owner ? (
              <small>
                失败分类：
                {error.category
                  ? labelFrom(FAILURE_CATEGORY_LABELS, error.category)
                  : "未分类"}
                {error.owner
                  ? ` · 处理人：${labelFrom(FAILURE_OWNER_LABELS, error.owner)}`
                  : ""}
              </small>
            ) : null}
            {error.advice ? <small>处理建议：{error.advice}</small> : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
