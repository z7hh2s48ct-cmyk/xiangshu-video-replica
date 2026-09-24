import { useEffect, useState } from "react";
import { adminRead } from "../api.admin";

type Page<T> = { items: T[]; total: number };
type Batch = {
  id: string;
  platform: string;
  created_at: string;
  config: { keywords?: { keyword: string }[] };
  pricing: { credits: number; version: number };
  request_count: number;
  confirmed_count: number;
  uncertain_count: number;
  pending_requests: number;
  customer_count: number;
  pending_charges: number;
  failed_count: number;
  skipped_count: number;
  charged_credits: number;
  known_cost_fen: string;
  known_revenue_fen: string;
  profit_fen: string | null;
  unknown_cost_count: number;
  unknown_revenue_count: number;
};
type Charge = {
  request_id: string;
  user_id: string;
  username: string;
  state: string;
  due_credits: number;
  charged_credits: number;
};
const money = (value: string | null) =>
  value === null
    ? "待核对"
    : `¥${(Number(value) / 100).toFixed(10).replace(/0+$/, "").replace(/\.$/, ".00")}`;

export function ViralCollectionBilling({ query }: { query: string }) {
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<Page<Batch>>();
  const [selected, setSelected] = useState("");
  const [chargeOffset, setChargeOffset] = useState(0);
  const [charges, setCharges] = useState<Page<Charge>>();
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const range = new URLSearchParams(query);
  const dates = new URLSearchParams({
    start: range.get("start") ?? "",
    end: range.get("end") ?? "",
  }).toString();
  useEffect(() => {
    void revision;
    let active = true;
    setPage(undefined);
    setError("");
    void adminRead<Page<Batch>>(
      `/api/control/billing/viral-collections?${dates}&limit=25&offset=${offset}`,
      "读取采集账单失败",
    )
      .then((result) => {
        if (active) setPage(result);
      })
      .catch((cause: unknown) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "读取采集账单失败");
      });
    return () => {
      active = false;
    };
  }, [dates, offset, revision]);
  useEffect(() => {
    void revision;
    let active = true;
    setCharges(undefined);
    if (selected) {
      void adminRead<Page<Charge>>(
        `/api/control/billing/viral-collections/${encodeURIComponent(selected)}/charges?limit=50&offset=${chargeOffset}`,
        "读取客户扣费明细失败",
      )
        .then((result) => {
          if (active) setCharges(result);
        })
        .catch((cause: unknown) => {
          if (active)
            setError(
              cause instanceof Error ? cause.message : "读取客户扣费明细失败",
            );
        });
    }
    return () => {
      active = false;
    };
  }, [selected, chargeOffset, revision]);
  return (
    <section aria-label="爆款采集账单">
      <h3>爆款采集账单</h3>
      <p>
        按上方日期查看所有客户的采集批次，不受用户、模块、供应商筛选影响。每次已确认的接口请求按批次快照单价向客户逐项扣分；供应商成本只记录一次。数据库读取和云存储不收费。
      </p>
      <p>
        搜索分页和视频号详情均计入接口次数。结果不明的请求暂不向客户扣分，供应商成本保留待核对。客户收入明细不分摊公共采集成本，请以批次总账核对利润。
      </p>
      <button
        type="button"
        onClick={() => {
          setError("");
          setRevision((value) => value + 1);
        }}
      >
        刷新采集账单
      </button>
      {error && <p role="alert">{error}</p>}
      {!page && !error && <p role="status">正在读取采集账单…</p>}
      {page && (
        <>
          <div className="admin-table-scroll">
            <table>
              <thead>
                <tr>
                  <th>采集批次 / 关键词</th>
                  <th>接口请求</th>
                  <th>客户计费</th>
                  <th>成本 / 收入 / 利润</th>
                  <th>明细</th>
                </tr>
              </thead>
              <tbody>
                {page.items.map((batch) => (
                  <tr key={batch.id}>
                    <td>
                      {new Date(batch.created_at).toLocaleString("zh-CN", {
                        timeZone: "Asia/Shanghai",
                      })}
                      <br />
                      {batch.platform === "douyin" ? "抖音" : "视频号"}
                      <br />
                      {batch.config.keywords
                        ?.map((item) => item.keyword)
                        .join("、")}
                      <br />
                      <small>{batch.id}</small>
                    </td>
                    <td>
                      登记 {batch.request_count} 次；已确认{" "}
                      {batch.confirmed_count} 次<br />
                      结果不明 {batch.uncertain_count} 次；处理中{" "}
                      {batch.pending_requests} 次
                    </td>
                    <td>
                      {batch.customer_count} 位客户 · {batch.pricing.credits}{" "}
                      积分/次
                      <br />
                      已扣 {batch.charged_credits} 积分；失败{" "}
                      {batch.failed_count} 笔；待扣 {batch.pending_charges} 笔
                      <br />
                      已停用跳过 {batch.skipped_count ?? 0} 笔；价格版本{" "}
                      {batch.pricing.version}
                    </td>
                    <td>
                      已知成本 {money(batch.known_cost_fen)}
                      <br />
                      已知收入 {money(batch.known_revenue_fen)}
                      <br />
                      利润 {money(batch.profit_fen)}
                      <br />
                      成本待核对 {batch.unknown_cost_count} 笔；收入待核对{" "}
                      {batch.unknown_revenue_count} 笔
                    </td>
                    <td>
                      <button
                        type="button"
                        onClick={() => {
                          setSelected(batch.id);
                          setChargeOffset(0);
                        }}
                      >
                        客户扣费明细
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {page.items.length === 0 && <p>该日期范围暂无采集账单。</p>}
          <button
            type="button"
            disabled={offset === 0}
            onClick={() => setOffset((value) => Math.max(0, value - 25))}
          >
            上一批次页
          </button>
          <span> 共 {page.total} 个批次 </span>
          <button
            type="button"
            disabled={offset + 25 >= page.total}
            onClick={() => setOffset((value) => value + 25)}
          >
            下一批次页
          </button>
        </>
      )}
      {selected && (
        <section aria-label="采集客户扣费明细">
          <h4>客户扣费明细</h4>
          <p>批次 {selected}；每行对应一位客户的一次接口费用。</p>
          <button type="button" onClick={() => setSelected("")}>
            关闭明细
          </button>
          {!charges && !error && <p role="status">正在读取扣费明细…</p>}
          {charges && (
            <>
              <div className="admin-table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>客户</th>
                      <th>接口请求编号</th>
                      <th>应扣 / 实扣</th>
                      <th>结果</th>
                    </tr>
                  </thead>
                  <tbody>
                    {charges.items.map((charge) => (
                      <tr key={`${charge.request_id}:${charge.user_id}`}>
                        <td>{charge.username}</td>
                        <td>{charge.request_id}</td>
                        <td>
                          {charge.due_credits} / {charge.charged_credits} 积分
                        </td>
                        <td>
                          {charge.state === "SUCCEEDED"
                            ? "已结算"
                            : charge.state === "SKIPPED_INACTIVE"
                              ? "账号已停用，未扣费"
                              : "余额不足，扣费失败"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <button
                type="button"
                disabled={chargeOffset === 0}
                onClick={() =>
                  setChargeOffset((value) => Math.max(0, value - 50))
                }
              >
                上一明细页
              </button>
              <span> 共 {charges.total} 笔 </span>
              <button
                type="button"
                disabled={chargeOffset + 50 >= charges.total}
                onClick={() => setChargeOffset((value) => value + 50)}
              >
                下一明细页
              </button>
            </>
          )}
        </section>
      )}
    </section>
  );
}
