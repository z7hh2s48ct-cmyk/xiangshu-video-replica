import { businessLabel } from "./businessLabels";

/** 环形图的一段落。 */
export type ConsumptionSegment = {
  business: string;
  label: string;
  credits: number;
  /** 占总额的百分比（保留一位小数）。 */
  share: number;
  /** 在圆环上的起始偏移（百分比，0..100）。 */
  offset: number;
};

// 只画前 5 名，其余并成「其他」——12 段圆环在小屏上谁也看不清。
export const DONUT_TOP_N = 5;

/**
 * 把「按业务汇总」的数据折成圆环分段（纯函数，便于单测）。
 *
 * 口径：入参已由后端保证只含消费（不含退回）；这里只负责排序、截断与累加偏移。
 * 金额为 0 的项直接丢掉（画出来是一条看不见的线，还会占图例）。
 *
 * **「其他」永远只有一个段**：后端自己也会产出 `business: "other"`（没有计费科目的
 * 历史行），若折叠尾部时再拼一个同名的段，图例里就会出现两行「其他」、分段用
 * `key={segment.business}` 渲染出重复 key（React 会告警并可能错删一段），金额被拆成
 * 两处。所以先把真实的 `other` 桶从排名里摘出来，最后统一并进那个唯一的尾部段：
 * 它既是后端给的 `other`，也是被截断的那些小项。
 */
export function consumptionSegments(
  items: ReadonlyArray<{ business: string; credits: number }>,
  topN: number = DONUT_TOP_N,
): ConsumptionSegment[] {
  const positive = items
    .filter((item) => item.credits > 0)
    .sort((first, second) => second.credits - first.credits);
  if (positive.length === 0) {
    return [];
  }
  const otherCredits = positive
    .filter((item) => item.business === "other")
    .reduce((sum, item) => sum + item.credits, 0);
  const ranked = positive.filter((item) => item.business !== "other");
  const head = ranked.slice(0, topN);
  const tail = ranked.slice(topN);
  const foldedTotal =
    otherCredits + tail.reduce((sum, item) => sum + item.credits, 0);
  // 没有尾部小项、后端也没给 other 桶时，不凭空造一个「其他」段。
  const folded =
    tail.length || otherCredits > 0
      ? [...head, { business: "other", credits: foldedTotal }]
      : head;
  const total = folded.reduce((sum, item) => sum + item.credits, 0);
  let offset = 0;
  return folded.map((item) => {
    const share = total > 0 ? (item.credits / total) * 100 : 0;
    const segment: ConsumptionSegment = {
      business: item.business,
      label: businessLabel(item.business),
      credits: item.credits,
      share,
      offset,
    };
    offset += share;
    return segment;
  });
}

const SEGMENT_COLORS = [
  "#e8b441",
  "#c98f3a",
  "#8fb59a",
  "#7a9ec9",
  "#b08fc9",
  "#8a8f98",
];

/**
 * 近 30 天消费构成（审计方案 F / P1#10）。
 *
 * 用 SVG 画而不是引图表库：一段 `stroke-dasharray` 的圆就够，引一个图表库只为这一张
 * 图不划算（客户包体积与供应链都要管）。
 */
export function ConsumptionDonut({
  items,
  days,
}: {
  items: ReadonlyArray<{ business: string; credits: number }>;
  days: number;
}) {
  const segments = consumptionSegments(items);
  const total = segments.reduce((sum, item) => sum + item.credits, 0);
  if (segments.length === 0) {
    return (
      <p className="uc-donut__empty" role="status">
        最近 {days} 天还没有消费记录。
      </p>
    );
  }
  const radius = 56;
  const circumference = 2 * Math.PI * radius;
  return (
    <div className="uc-donut">
      <svg
        viewBox="0 0 160 160"
        role="img"
        aria-label={`最近 ${days} 天消费构成，共 ${total} 积分`}
      >
        {segments.map((segment, index) => (
          <circle
            cx="80"
            cy="80"
            fill="none"
            key={segment.business}
            r={radius}
            stroke={SEGMENT_COLORS[index % SEGMENT_COLORS.length]}
            strokeDasharray={`${(segment.share / 100) * circumference} ${circumference}`}
            strokeDashoffset={`${-(segment.offset / 100) * circumference}`}
            strokeWidth="18"
          />
        ))}
        <text className="uc-donut__total" textAnchor="middle" x="80" y="76">
          {total}
        </text>
        <text className="uc-donut__unit" textAnchor="middle" x="80" y="96">
          积分 / {days} 天
        </text>
      </svg>
      <ul className="uc-donut__legend">
        {segments.map((segment, index) => (
          <li
            className={index < 3 ? "uc-donut__legend-top" : undefined}
            key={segment.business}
          >
            <span
              aria-hidden="true"
              className="uc-donut__dot"
              style={{
                background: SEGMENT_COLORS[index % SEGMENT_COLORS.length],
              }}
            />
            <span>{segment.label}</span>
            <b>{segment.credits.toLocaleString("zh-CN")}</b>
            <small>{segment.share.toFixed(1)}%</small>
          </li>
        ))}
      </ul>
    </div>
  );
}
