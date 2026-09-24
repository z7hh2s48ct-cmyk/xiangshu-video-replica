import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  getStudioAnalytics,
  getStudioStats,
  type StudioAnalytics,
} from "../api";
import { useStudio } from "./context";
import { CREATION_KIND_LABELS } from "./live";
import { Button, Empty, formatTaskTime, Icon, Panel } from "./ui";
import "./analytics.css";

type Range = "7" | "30";

// 任务类型占比的环形分段配色（平台内创作通道维度，非外部平台语义）。
const KIND_COLORS = ["#efb524", "#45c77d", "#5aa7ff", "#bf78ff"];

const EMPTY_ANALYTICS: StudioAnalytics = {
  range_days: 7,
  generated_at: "",
  today_completed: 0,
  range_completed: 0,
  total_completed: 0,
  today_generation_batches: 0,
  range_generation_batches: 0,
  total_generation_batches: 0,
  range_generation_outputs: 0,
  range_oral_outputs: 0,
  daily: [],
  kind_breakdown: [],
  recent_works: [],
};

function kindLabel(kind: string) {
  if (kind === "oral") return "数字人口播";
  return CREATION_KIND_LABELS[kind] ?? "视频生成";
}

function TrendChart({
  labels,
  values,
  failedValues,
  rangeLabel,
}: {
  labels: string[];
  values: number[];
  failedValues: number[];
  rangeLabel: string;
}) {
  const width = 700;
  const height = 220;
  const left = 48;
  const right = 18;
  const top = 26;
  const bottom = 44;
  const maxValue = Math.max(
    3,
    Math.ceil(Math.max(0, ...values, ...failedValues)),
  );
  const chartWidth = width - left - right;
  const chartHeight = height - top - bottom;
  const xAt = (index: number, length: number) =>
    left + (chartWidth * index) / Math.max(1, length - 1);
  const points = values.map((value, index) => ({
    x: xAt(index, values.length),
    y: top + chartHeight * (1 - value / maxValue),
    value,
  }));
  const failedPoints = failedValues.map((value, index) => ({
    x: xAt(index, failedValues.length),
    y: top + chartHeight * (1 - value / maxValue),
    value,
  }));
  const failedTotal = failedValues.reduce((sum, value) => sum + value, 0);
  const pointList = points.map(({ x, y }) => `${x},${y}`).join(" ");
  const failedPointList = failedPoints.map(({ x, y }) => `${x},${y}`).join(" ");
  const area = `M ${left} ${height - bottom} L ${pointList.replaceAll(" ", " L ")} L ${width - right} ${height - bottom} Z`;
  const ticks = [0, 1, 2, 3].map((value) => (value * maxValue) / 3);
  // 30 天窗口的横轴标签按步长抽稀，避免重叠。
  const labelStep = Math.max(1, Math.ceil(labels.length / 8));

  return (
    <svg
      className="analytics-trend-chart"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`${rangeLabel}成片趋势`}
    >
      <defs>
        <linearGradient id="analytics-trend-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#e8ad29" stopOpacity=".35" />
          <stop offset="1" stopColor="#e8ad29" stopOpacity=".04" />
        </linearGradient>
      </defs>
      {ticks.map((tick) => {
        const y = top + chartHeight * (1 - tick / maxValue);
        return (
          <g key={tick}>
            <line x1={left} x2={width - right} y1={y} y2={y} />
            <text x={left - 12} y={y + 5} textAnchor="end">
              {tick === 0 ? "0" : `${Math.round(tick)}个`}
            </text>
          </g>
        );
      })}
      <line x1={left} x2={left} y1={top} y2={height - bottom} />
      <path d={area} className="analytics-trend-area" />
      <polyline points={pointList} className="analytics-trend-line" />
      {failedTotal > 0 ? (
        <polyline points={failedPointList} className="analytics-trend-failed" />
      ) : null}
      {points.map((point, index) => (
        <g key={labels[index] ?? index}>
          <circle cx={point.x} cy={point.y} r="5" />
          {failedTotal > 0 && failedPoints[index] ? (
            <circle
              className="analytics-point-failed"
              cx={failedPoints[index].x}
              cy={failedPoints[index].y}
              r="4"
            />
          ) : null}
          {index % labelStep === 0 ? (
            <text
              className="analytics-point-value"
              x={point.x}
              y={point.y - 14}
              textAnchor="middle"
            >
              {Math.round(point.value)}
            </text>
          ) : null}
          {index % labelStep === 0 ? (
            <text x={point.x} y={height - 16} textAnchor="middle">
              {labels[index]}
            </text>
          ) : null}
        </g>
      ))}
    </svg>
  );
}

export function AnalyticsPage() {
  const { data, review, navigate, updateData, user } = useStudio();
  const [range, setRange] = useState<Range>("7");
  const [fresh, setFresh] = useState<Partial<Record<Range, StudioAnalytics>>>(
    {},
  );
  const [stale, setStale] = useState<Partial<Record<Range, boolean>>>({});
  const [refreshing, setRefreshing] = useState(false);
  const [analyticsAccountId, setAnalyticsAccountId] = useState(user.id);
  const requestIdRef = useRef(0);
  const accountIdRef = useRef(user.id);
  const cachedAnalytics = range === "7" ? data.analytics7 : data.analytics30;
  const selectedAnalytics =
    analyticsAccountId === user.id ? (fresh[range] ?? cachedAnalytics) : null;
  const stats = data.stats;
  const rangeLabel = range === "7" ? "近7天" : "近30天";

  const refreshAnalytics = useCallback(async () => {
    if (review) return;
    const accountId = user.id;
    const requestId = ++requestIdRef.current;
    setRefreshing(true);
    try {
      const [next, nextStats] = await Promise.all([
        getStudioAnalytics(Number(range) as 7 | 30),
        getStudioStats(),
      ]);
      if (
        requestId !== requestIdRef.current ||
        accountId !== accountIdRef.current
      )
        return;
      setFresh((current) => ({ ...current, [range]: next }));
      setStale((current) => ({ ...current, [range]: false }));
      setAnalyticsAccountId(accountId);
      updateData((current) => ({
        ...current,
        [range === "7" ? "analytics7" : "analytics30"]: next,
        stats: nextStats,
      }));
    } catch {
      if (
        requestId === requestIdRef.current &&
        accountId === accountIdRef.current
      ) {
        setStale((current) => ({ ...current, [range]: true }));
      }
    } finally {
      if (
        requestId === requestIdRef.current &&
        accountId === accountIdRef.current
      )
        setRefreshing(false);
    }
  }, [range, review, updateData, user.id]);

  useLayoutEffect(() => {
    if (accountIdRef.current === user.id) return;
    accountIdRef.current = user.id;
    setFresh({});
    setStale({});
    requestIdRef.current += 1;
  }, [user.id]);

  useEffect(() => {
    void refreshAnalytics();
    return () => {
      requestIdRef.current += 1;
    };
  }, [refreshAnalytics]);

  if (!review && !selectedAnalytics)
    return (
      <section className="analytics-page analytics-empty-page">
        <Empty
          title="统计数据尚未就绪"
          description="看板统计加载失败或还没有成片记录；完成创作后，这里会呈现真实的成片趋势与作品列表。"
        />
        <Button onClick={() => void refreshAnalytics()} disabled={refreshing}>
          {refreshing ? "刷新中…" : "重试刷新"}
        </Button>
      </section>
    );

  const analytics: StudioAnalytics = selectedAnalytics ?? EMPTY_ANALYTICS;

  const dailyLabels = analytics.daily.map((day) => day.day.slice(5));
  const dailyValues = analytics.daily.map((day) => day.completed);
  const dailyFailed = analytics.daily.map((day) => day.failed);
  const kindTotal = analytics.kind_breakdown.reduce(
    (sum, item) => sum + item.completed,
    0,
  );
  const kindStops: string[] = [];
  let kindAccumulated = 0;
  for (const [index, item] of analytics.kind_breakdown.entries()) {
    const start = (kindAccumulated / kindTotal) * 100;
    kindAccumulated += item.completed;
    const end = (kindAccumulated / kindTotal) * 100;
    kindStops.push(
      `${KIND_COLORS[index % KIND_COLORS.length]} ${start}% ${end}%`,
    );
  }
  const kindGradient = kindStops.length
    ? `conic-gradient(${kindStops.join(", ")})`
    : "conic-gradient(#2a2b28 0% 100%)";

  const metrics = [
    {
      label: "期间成片（产出项）",
      value: `${analytics.range_completed} 个`,
      icon: "video",
      detail: `普通生成 ${analytics.range_generation_outputs} 个 · 口播 ${analytics.range_oral_outputs} 个`,
    },
    {
      label: "今日成片（产出项）",
      value: `${analytics.today_completed} 个`,
      icon: "chart",
      detail: "按北京时间自然日",
    },
    {
      label: "普通生成批次",
      value: `${analytics.range_generation_batches} 批`,
      icon: "chart",
      detail: `${rangeLabel}去重批次`,
    },
    {
      label: "成片队列",
      value: stats ? `${stats.running + stats.queued} 个` : "—",
      icon: "heart",
      detail: "普通生成与口播任务",
    },
    {
      label: "待处理",
      value: stats ? `${stats.needs_attention} 个` : "—",
      icon: "star",
      detail: "失败或需要人工处理",
    },
  ];

  function viewWork(work: StudioAnalytics["recent_works"][number]) {
    const oral = work.task_kind === "oral";
    const detailId = oral ? work.task_id : work.batch_id;
    if (!detailId) return;
    navigate("task-detail", {
      selectedTaskId: detailId,
      selectedTaskKind: oral ? "oral_task" : "generation_batch",
      selectedTaskBackendId: detailId,
      returnTo: "analytics",
    });
  }

  return (
    <section className="analytics-page">
      <header className="analytics-heading">
        <h1>数据看板</h1>
        {!review ? (
          <Button
            variant="outline"
            onClick={() => void refreshAnalytics()}
            disabled={refreshing}
          >
            <Icon name="refresh" size={16} />
            {refreshing ? "刷新中…" : "刷新报表"}
          </Button>
        ) : null}
      </header>

      <fieldset className="analytics-filters">
        <legend>数据筛选</legend>
        <label>
          <Icon name="clock" size={18} />
          <select
            aria-label="时间筛选"
            value={range}
            onChange={(event) => setRange(event.target.value as Range)}
          >
            <option value="7">近7天</option>
            <option value="30">近30天</option>
          </select>
        </label>
        {review ? (
          <span className="analytics-sample-mark">
            <i />
            示例数据
          </span>
        ) : null}
      </fieldset>

      {!review ? (
        <div className="analytics-freshness" aria-live="polite">
          <span>
            更新时间：
            {analytics.generated_at
              ? formatTaskTime(analytics.generated_at)
              : "未知"}
          </span>
          {stale[range] ? (
            <span className="is-stale">
              数据可能已过期，请重试刷新。
              <Button variant="quiet" onClick={() => void refreshAnalytics()}>
                重试刷新
              </Button>
            </span>
          ) : null}
        </div>
      ) : null}

      <div className="analytics-metrics">
        {metrics.map((metric) => (
          <Panel key={metric.label} className="analytics-metric">
            <span className="analytics-metric-icon">
              <Icon name={metric.icon} size={34} />
            </span>
            <div>
              <span>{metric.label}</span>
              <strong>{metric.value}</strong>
              <small>{metric.detail}</small>
            </div>
          </Panel>
        ))}
      </div>

      <div className="analytics-charts">
        <Panel className="analytics-trend">
          <h2>
            成片趋势 <small>{rangeLabel}</small>
          </h2>
          <div className="analytics-trend-legend">
            <span>
              <i className="is-completed" />
              成片
            </span>
            <span>
              <i className="is-failed" />
              失败
            </span>
          </div>
          {dailyValues.length ? (
            <TrendChart
              labels={dailyLabels}
              values={dailyValues}
              failedValues={dailyFailed}
              rangeLabel={rangeLabel}
            />
          ) : (
            <Empty
              title="暂无成片记录"
              description="完成创作后这里会出现趋势曲线。"
            />
          )}
          <p>合计成片产出项：{analytics.range_completed} 个</p>
        </Panel>
        <Panel className="analytics-share">
          <h2>
            任务类型占比 <small>{rangeLabel}</small>
          </h2>
          <div className="analytics-share-body">
            <div
              className="analytics-donut"
              style={{ background: kindGradient }}
            >
              <strong>
                {kindTotal
                  ? `${((analytics.kind_breakdown[0].completed / kindTotal) * 100).toFixed(1)}%`
                  : "—"}
              </strong>
            </div>
            {kindTotal ? (
              <dl>
                {analytics.kind_breakdown.map((item, index) => (
                  <div key={item.kind}>
                    <dt>
                      <i
                        style={{
                          background: KIND_COLORS[index % KIND_COLORS.length],
                        }}
                      />
                      {kindLabel(item.kind)}
                    </dt>
                    <dd>
                      {item.completed} 个（
                      {((item.completed / kindTotal) * 100).toFixed(1)}%）
                    </dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="analytics-share-empty">暂无成片记录</p>
            )}
          </div>
          <p>合计成片产出项：{analytics.range_completed} 个</p>
        </Panel>
      </div>

      <Panel className="analytics-performance">
        <h2>最近成片</h2>
        {analytics.recent_works.length ? (
          <div className="analytics-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>作品</th>
                  <th>类型</th>
                  <th>完成时间</th>
                  <th>消耗</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {analytics.recent_works.map((work) => (
                  <tr
                    key={`${work.task_id}-${work.completed_at}-${work.title}`}
                  >
                    <td>
                      <div className="analytics-work">
                        <strong>{work.title}</strong>
                      </div>
                    </td>
                    <td>{kindLabel(work.creation_kind)}</td>
                    <td>{formatTaskTime(work.completed_at)}</td>
                    <td>
                      {work.cost_credits != null
                        ? `${work.cost_credits} 积分`
                        : "—"}
                    </td>
                    <td>
                      <div className="analytics-actions">
                        <Button
                          variant="outline"
                          onClick={() => viewWork(work)}
                        >
                          查看视频
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty
            title="当前筛选下暂无成片"
            description="该时间段内还没有完成的视频，去创作第一条吧。"
          />
        )}
      </Panel>
    </section>
  );
}
