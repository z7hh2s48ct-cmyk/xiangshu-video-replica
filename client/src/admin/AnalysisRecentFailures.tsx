import { useCallback, useEffect, useState } from "react";

import { getAdminGenerationRecords } from "../api.admin";
import { formatDateTime } from "./ui/vocabulary";

/** 近 7 天失败拆解清单（方案 P1 失败诊断改造）：进入页签先看到默认清单，
 *  不再要求手输任务编号；点行上的「诊断」把编号交给下方面板做定点查询。 */
export function AnalysisRecentFailures({
  onDiagnose,
}: {
  onDiagnose: (taskId: string) => void;
}) {
  const [items, setItems] = useState<
    Awaited<ReturnType<typeof getAdminGenerationRecords>>["items"]
  >([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      // 上海挂钟「7 天前」的日期串，与服务端 created_from 的日期口径一致。
      const sevenDaysAgo = new Intl.DateTimeFormat("en-CA", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      }).format(new Date(Date.now() - 7 * 86_400_000));
      const page = await getAdminGenerationRecords({
        recordType: "ANALYSIS",
        // 拆解分支的失败终态；两种取消拼写一并带上，与列表 5 组口径一致。
        status: "FAILED,CANCELED,CANCELLED,ARCHIVE_FAILED",
        createdFrom: sevenDaysAgo,
        limit: 20,
      });
      setItems(page.items);
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "读取近 7 天失败拆解失败",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section className="admin-panel" aria-label="近 7 天拆解失败">
      <h3>近 7 天拆解失败（最新 20 条）</h3>
      <p className="admin-hint">
        默认清单按失败时间倒序；需要定点排查时点行上的「诊断」，或在下方输入
        任务编号 / 问题编号。
      </p>
      {error && <p role="alert">{error}</p>}
      {loading && <p role="status">正在加载…</p>}
      {!loading && !error && items.length === 0 ? (
        <p>近 7 天没有失败的拆解任务。</p>
      ) : null}
      {!loading && items.length > 0 ? (
        <div className="admin-table-scroll">
          <table className="admin-data-table" aria-label="近 7 天失败拆解列表">
            <thead>
              <tr>
                <th>时间</th>
                <th>客户</th>
                <th>任务编号</th>
                <th>错误码</th>
                <th>失败阶段</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.record_id}>
                  <td>{formatDateTime(item.created_at)}</td>
                  <td>{item.username}</td>
                  <td>
                    <code>{item.record_id}</code>
                  </td>
                  <td>{item.error_code ?? "—"}</td>
                  <td>{item.failure_phase ?? "—"}</td>
                  <td>
                    <button
                      type="button"
                      onClick={() => onDiagnose(item.record_id)}
                    >
                      诊断
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}
