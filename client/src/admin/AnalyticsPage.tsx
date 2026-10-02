import { useState } from "react";
import { type BillingAttention, BillingEconomics } from "./BillingEconomics";
import { BusinessDashboard } from "./BusinessDashboard";
import { SummaryReportExport } from "./SummaryReportExport";
import "./economics.css";
import { TabBar } from "./ui/TabBar";

const tabs = [
  { id: "dashboard", label: "经营看板" },
  { id: "cost", label: "成本核对" },
];
function today() {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

export function AnalyticsPage({
  readOnly = false,
  initialTab = "dashboard",
  initialAttention = "",
  initialStart,
  initialEnd,
  initialCostQuery = "",
  onScopeChange,
  onCustomer,
}: {
  readOnly?: boolean;
  initialTab?: "dashboard" | "cost";
  initialAttention?: BillingAttention;
  initialStart?: string;
  initialEnd?: string;
  initialCostQuery?: string;
  onScopeChange?: (scope: Record<string, string>) => void;
  onCustomer?: (userId: string) => void;
}) {
  const [tab, setTab] = useState<string>(initialTab);
  const [range, setRange] = useState(() => ({
    start:
      initialStart ??
      (initialAttention ? today() : `${today().slice(0, 7)}-01`),
    end: initialEnd ?? today(),
  }));
  const [costQuery, setCostQuery] = useState(initialCostQuery);
  return (
    <div>
      <TabBar
        active={tab}
        ariaLabel="经营分析页签"
        items={tabs}
        onChange={(next) => {
          setTab(next);
          onScopeChange?.({ analyticsTab: next });
        }}
      />
      {tab === "cost" && (
        <BillingEconomics
          readOnly={readOnly}
          view="cost"
          initialAttention={initialAttention}
          initialStart={range.start}
          initialEnd={range.end}
          initialQuery={costQuery}
          onQueryChange={(query) => {
            setCostQuery(query);
            const p = new URLSearchParams(query);
            const nextRange = {
              start: p.get("start") ?? range.start,
              end: p.get("end") ?? range.end,
            };
            setRange(nextRange);
            onScopeChange?.({ ...nextRange, billingQuery: query });
          }}
        />
      )}
      {tab === "dashboard" && (
        <>
          <BusinessDashboard
            readOnly={readOnly}
            range={range}
            onCustomer={onCustomer}
            onRangeChange={(next) => {
              setRange(next);
              onScopeChange?.({
                ...next,
                billingQuery: new URLSearchParams({
                  ...Object.fromEntries(new URLSearchParams(costQuery)),
                  ...next,
                }).toString(),
              });
              setCostQuery((old) => {
                const p = new URLSearchParams(old);
                p.set("start", next.start);
                p.set("end", next.end);
                return p.toString();
              });
            }}
          />
          {!readOnly && <SummaryReportExport kind="business" range={range} />}
        </>
      )}
    </div>
  );
}
