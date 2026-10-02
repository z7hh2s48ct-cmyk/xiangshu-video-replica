import { useEffect, useState } from "react";
import {
  adminRead,
  adminWrite,
  estimateViralOperation,
  type ViralOperationEstimate,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { formatDateTime } from "./ui/vocabulary";
import { ViralOperationCostSummary } from "./ViralOperationCostSummary";

type Platform = "douyin" | "wechat_channels";
type Probe = {
  platform: Platform;
  state: string;
  checked_at: string;
  failure_category: string | null;
  advice: string | null;
};
export function ViralPlatformProbes({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30000);
    return () => window.clearInterval(timer);
  }, []);
  const [items, setItems] = useState<Probe[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<{
    platform: Platform;
    estimate: ViralOperationEstimate;
    key: string;
  } | null>(null);
  useEffect(() => {
    let active = true;
    void adminRead<{ items: Probe[] }>(
      "/api/control/viral/platforms/probes",
      "读取平台探测失败",
    )
      .then((page) => {
        if (active) setItems(page.items);
      })
      .catch((cause) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "读取平台探测失败");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);
  async function requestProbe(platform: Platform) {
    if (readOnly || busy) return;
    setBusy(true);
    setError("");
    try {
      const estimate = await estimateViralOperation({
        action: "search",
        platform,
      });
      setPending({ platform, estimate, key: crypto.randomUUID() });
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "费用预估失败，未探测平台",
      );
    } finally {
      setBusy(false);
    }
  }
  async function confirm(reason: string) {
    if (!pending || readOnly || busy) return;
    setBusy(true);
    setError("");
    try {
      const next = await adminWrite<Probe>(
        `/api/control/viral/platforms/${pending.platform}/probe`,
        { expected_cost_snapshot: pending.estimate.snapshot },
        reason,
        "探测平台失败",
        pending.key,
      );
      setItems((previous) => [
        ...previous.filter((p) => p.platform !== next.platform),
        next,
      ]);
      setPending(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "探测平台失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section aria-label="平台连接探测">
      <h3>平台连接探测</h3>
      <p className="admin-hint">
        按配置执行一页搜索验证；不入库、不扣客户积分。供应商请求可能收费，只有确认后才执行；结果10分钟后过期。
      </p>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {loading ? <p role="status">正在读取已记录探测…</p> : null}
      {(["douyin", "wechat_channels"] as const).map((platform) => {
        const probe = items.find((p) => p.platform === platform);
        const expired = probe && now - Date.parse(probe.checked_at) >= 600000;
        const state = expired ? "expired" : probe?.state;
        return (
          <article key={platform}>
            <strong>{platform === "douyin" ? "抖音" : "视频号"}</strong>
            <p>
              {state === "available"
                ? "本次探测可用"
                : state === "unavailable"
                  ? "本次探测异常"
                  : state === "expired"
                    ? "探测已过期"
                    : "尚未探测"}{" "}
              · {probe ? formatDateTime(probe.checked_at) : "无检查时间"}
            </p>
            {probe?.failure_category ? (
              <p>
                {probe.failure_category}；{probe.advice}
              </p>
            ) : null}
            {!readOnly ? (
              <button
                type="button"
                disabled={busy || loading}
                onClick={() => void requestProbe(platform)}
              >
                探测{platform === "douyin" ? "抖音" : "视频号"}
              </button>
            ) : null}
          </article>
        );
      })}
      <ConfirmDialog
        open={pending !== null}
        title="确认平台探测费用"
        confirmLabel="确认探测"
        description="只验证本次连接；重试会产生额外物理请求，完整费用仍需核对。"
        busy={busy}
        error={error}
        level="reason"
        onClose={() => !busy && setPending(null)}
        onConfirm={(reason) => void confirm(reason)}
      >
        {pending ? (
          <ViralOperationCostSummary estimate={pending.estimate} />
        ) : null}
      </ConfirmDialog>
    </section>
  );
}
