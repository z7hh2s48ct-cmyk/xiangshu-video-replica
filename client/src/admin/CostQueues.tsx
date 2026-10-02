import { useEffect, useState } from "react";
import { adminRead } from "../api.admin";

const queues = [
  { key: "pending", label: "待结算" },
  { key: "unknown_cost", label: "待核对成本" },
  { key: "legacy", label: "历史待核对" },
];
export function CostQueues({
  query,
  revision,
  onPick,
}: {
  query: string;
  revision: number;
  onPick: (query: string) => void;
}) {
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [error, setError] = useState("");
  const scope = (key: string) => {
    const p = new URLSearchParams(query);
    p.delete("attention");
    p.set("attention", key === "pending" ? "pending" : "unknown_cost");
    if (key === "legacy") p.set("start", "2019-01-01");
    return p.toString();
  };
  useEffect(() => {
    void revision;
    let active = true;
    setCounts({});
    setError("");
    void Promise.all(
      queues.map(async (q) => {
        const p = new URLSearchParams(query);
        p.set("attention", q.key === "pending" ? "pending" : "unknown_cost");
        if (q.key === "legacy") p.set("start", "2019-01-01");
        p.set("limit", "1");
        const r = await adminRead<{ total: number }>(
          `/api/control/billing/operations?${p}`,
          "读取待核对队列失败",
        );
        return [q.key, r.total] as const;
      }),
    )
      .then((r) => {
        if (active) setCounts(Object.fromEntries(r));
      })
      .catch((e) => {
        if (active) setError(e instanceof Error ? e.message : "读取队列失败");
      });
    return () => {
      active = false;
    };
  }, [query, revision]);
  const range = new URLSearchParams(query);
  return (
    <section className="economics-kpis cost-queues" aria-label="待核对队列">
      {error && <p role="alert">{error}</p>}
      {queues.map((q) => (
        <article key={q.key}>
          <button
            type="button"
            disabled={counts[q.key] === undefined}
            onClick={() => onPick(scope(q.key))}
          >
            <span>{q.label}</span>
            <strong>{counts[q.key] ?? "…"}</strong>
            <small>
              {q.key === "legacy" ? "2019-01-01" : range.get("start")} 至{" "}
              {range.get("end")} · 当前客户与业务筛选
            </small>
          </button>
        </article>
      ))}
    </section>
  );
}
