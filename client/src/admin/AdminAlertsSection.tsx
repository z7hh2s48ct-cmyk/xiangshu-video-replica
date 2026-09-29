import { useCallback, useEffect, useState } from "react";

import {
  type AdminFailureRateGroup,
  type AdminFailureRateReport,
  adminActivationErrorMessage,
  getFailureRateAlerts,
} from "../api.admin";
import { PageBanner } from "./ui/PageBanner";
import { StatusBadge } from "./ui/StatusBadge";
import { GENERATION_RECORD_TYPE_LABELS, labelFrom } from "./ui/vocabulary";

/**
 * 「通知与告警」页签（方案 P1-5）：按业务类型展示近 1 小时失败率，
 * 越过阈值且样本达标的类型标红提醒技术负责人；每条错误码挂 runbook
 * 的分类/处理人/处理建议（与生成记录页同一份词典，不另起口径）。
 */
export function AdminAlertsSection() {
  const [report, setReport] = useState<AdminFailureRateReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setReport(await getFailureRateAlerts());
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

  return (
    <section aria-label="失败率告警" className="admin-panel">
      <header className="admin-alerts__header">
        <h2>失败率告警</h2>
        <button disabled={loading} type="button" onClick={() => void load()}>
          刷新
        </button>
      </header>
      <p className="admin-hint">
        按业务类型统计近 1 小时内已终结任务的失败率（客户取消与进行中不计入）；
        越过阈值且样本充足时在这里提醒技术负责人。
      </p>

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {loading && !report ? <p className="admin-hint">读取中…</p> : null}

      {report ? (
        <>
          {report.alerting ? (
            <PageBanner tone="error">
              {`近 ${report.window_minutes} 分钟整体失败率 ${report.failure_rate_percent}%（${report.failed}/${report.total}），`}
              已有类型越过 {report.threshold_percent}%
              阈值，请技术负责人尽快处理下方标红项。
            </PageBanner>
          ) : (
            <PageBanner tone="notice">
              近 {report.window_minutes} 分钟没有类型越过{" "}
              {report.threshold_percent}% 失败率阈值。
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
                          minSample={report.min_sample_size}
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
            </span>
            {error.category || error.owner ? (
              <small>
                失败分类：{error.category ?? "未分类"}
                {error.owner ? ` · 处理人：${error.owner}` : ""}
              </small>
            ) : null}
            {error.advice ? <small>处理建议：{error.advice}</small> : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
