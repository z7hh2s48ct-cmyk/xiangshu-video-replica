import { useEffect, useState } from "react";
import { adminRead } from "../api.admin";
import { PageBanner } from "./ui/PageBanner";
import { formatDateTime } from "./ui/vocabulary";

type Overview = {
  daily_consumption: { day: string; credits: number }[];
  timeline: {
    event_id: string;
    kind: string;
    label: string;
    created_at: string;
    credits: number | null;
  }[];
  basis: string;
};
export function CustomerOverview({ userId }: { userId: string }) {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    void revision;
    setData(null);
    setError("");
    void Promise.resolve()
      .then(() =>
        adminRead<Overview>(
          `/api/control/customers/${encodeURIComponent(userId)}/overview`,
          "概览加载失败",
        ),
      )
      .then((value) => {
        if (active) setData(value);
      })
      .catch((cause) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "概览加载失败");
      });
    return () => {
      active = false;
    };
  }, [userId, revision]);
  const days = data?.daily_consumption ?? [];
  const max = Math.max(1, ...days.map((day) => day.credits));
  return (
    <section className="customer-overview" aria-label="客户经营概览">
      <h2>近30天积分消耗</h2>
      {error && (
        <PageBanner tone="error">
          {error}
          <button
            type="button"
            onClick={() => setRevision((value) => value + 1)}
          >
            重试概览
          </button>
        </PageBanner>
      )}
      {!data && !error && <p role="status">正在加载经营概览…</p>}
      {data && (
        <>
          <p className="admin-hint">{data.basis}</p>
          <svg
            role="img"
            aria-label={`近30天积分消耗，共 ${days.reduce((sum, day) => sum + day.credits, 0)} 积分`}
            viewBox="0 0 900 150"
            className="customer-consumption-chart"
          >
            <title>每日实际消耗积分，含零消耗日</title>
            {days.map((day, index) => (
              <g key={day.day}>
                <rect
                  x={12 + index * 29}
                  y={115 - (day.credits / max) * 95}
                  width="20"
                  height={(day.credits / max) * 95}
                  fill="#d9b666"
                >
                  <title>
                    {day.day}：{day.credits} 积分
                  </title>
                </rect>
                {(index === 0 ||
                  index === days.length - 1 ||
                  (index % 7 === 0 && index < days.length - 3)) && (
                  <text
                    x={12 + index * 29}
                    y="140"
                    fill="#d8c7a1"
                    fontSize="12"
                  >
                    {day.day.slice(5)}
                  </text>
                )}
              </g>
            ))}
            <line x1="10" y1="116" x2="890" y2="116" stroke="#675c43" />
          </svg>
          {!days.some((day) => day.credits > 0) && (
            <p className="admin-hint">近30天暂无消耗。</p>
          )}
          <h2>最近动态</h2>
          {data.timeline?.length ? (
            <ol className="customer-timeline">
              {data.timeline.map((event) => (
                <li key={`${event.kind}:${event.event_id}`}>
                  <time dateTime={event.created_at}>
                    {formatDateTime(event.created_at)}
                  </time>
                  <strong>{event.label}</strong>
                  {event.credits !== null && (
                    <span>
                      {event.credits > 0 ? "+" : ""}
                      {event.credits} 积分
                    </span>
                  )}
                </li>
              ))}
            </ol>
          ) : (
            <p className="admin-hint">暂无充值、生成、调整或登录记录。</p>
          )}
        </>
      )}
    </section>
  );
}
