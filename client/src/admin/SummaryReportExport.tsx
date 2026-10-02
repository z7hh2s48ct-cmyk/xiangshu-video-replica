import { useState } from "react";
import { exportSummaryReportCsv } from "../api.admin";

export function SummaryReportExport({
  kind,
  range,
  disabled = false,
}: {
  kind: "business" | "funds";
  range: { start: string; end: string };
  disabled?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  async function download() {
    if (busy || disabled) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await exportSummaryReportCsv({
        kind,
        start_date: range.start,
        end_date: range.end,
      });
      setNotice(
        `已导出 ${result.filename}；范围 ${range.start} 至 ${range.end}。`,
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "导出失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="summary-export" aria-label="当前范围报表导出">
      <p>
        导出当前范围：{range.start} 至 {range.end}
        （北京时间）。预收余额为当前时点数；未知金额保留待核对。
      </p>
      <button
        type="button"
        disabled={
          disabled ||
          busy ||
          !range.start ||
          !range.end ||
          range.start > range.end
        }
        onClick={() => void download()}
      >
        {busy
          ? "正在导出…"
          : kind === "business"
            ? "导出当前经营报表 CSV"
            : "导出月度资金报表 CSV"}
      </button>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
    </section>
  );
}
