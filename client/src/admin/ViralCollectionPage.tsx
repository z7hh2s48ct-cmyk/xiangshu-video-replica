import { ViralCollectionRecords } from "./ViralCollectionRecords";
import { ViralKeywordsManager } from "./ViralKeywordsManager";
import { ViralPlatformProbes } from "./ViralPlatformProbes";
import { ViralRuntimeSection } from "./ViralRuntimeSection";

export function ViralCollectionPage({
  readOnly = false,
  initialBatchId = "",
}: {
  readOnly?: boolean;
  initialBatchId?: string;
}) {
  return (
    <section className="admin-content-settings" aria-label="采集设置">
      <header className="admin-panel">
        <span className="admin-viral-eyebrow">内容运营</span>
        <h2>采集设置</h2>
        <p className="admin-hint">
          维护采集计划、关键词、质量规则与预算；修改后由后台执行。
        </p>
      </header>
      <ViralRuntimeSection readOnly={readOnly} />
      <ViralKeywordsManager readOnly={readOnly} />
      <ViralPlatformProbes readOnly={readOnly} />
      <ViralCollectionRecords initialBatchId={initialBatchId} />
    </section>
  );
}
