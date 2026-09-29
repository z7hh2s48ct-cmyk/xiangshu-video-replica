import { useEffect, useState } from "react";
import { adminRead } from "../api.admin";
import { billingStates, billingUnit } from "./billingTypes";
import { formatFen, formatRelativeTime } from "./ui/vocabulary";

type Action = {
  source_id: string;
  user_id: string | null;
  username: string;
  operation_count: number;
  pending_count: number;
  failed_count: number;
  reserved_credits: number;
  charged_credits: number;
  attempt_count: number;
  unknown_cost_count: number;
  known_cost_fen: string | null;
  cost_fen: string | null;
  inspection_attempt_count: number;
  inspection_cost_fen: string | null;
  services: string[];
  modules: string[];
  first_at: string;
  last_at: string;
};
type Attempt = {
  id: string;
  service: string;
  provider: string;
  unit: keyof typeof billingUnit;
  usage: string | null;
  effective_cost_fen: string | null;
  evidence_reference: string | null;
  state: string;
};
type Operation = {
  id: string;
  service: string;
  unit: keyof typeof billingUnit;
  state: string;
  actual_units: string | null;
  charged_credits: number;
  cost_fen: string | null;
  attempts: Attempt[];
};
type Panorama = { action: Action; operations: Operation[] };
type Page = { items: Action[]; total: number };

// P2-1：金额统一走 formatFen——两位小数，不足 1 分显示 < ¥0.01；
// 原实现除以 100 后留 10 位小数，运营对账得自己数字符。
const money = (value: string | number | null | undefined) =>
  value == null ? "待核对" : formatFen(Number(value));
const pageSize = 50;

export function SourceActionPanorama({
  query,
  name,
}: {
  query: string;
  name: (service: string) => string;
}) {
  const [offset, setOffset] = useState(0);
  const [list, setList] = useState<Page>();
  const [selected, setSelected] = useState<Action>();
  const [detail, setDetail] = useState<Panorama>();
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    void revision;
    let active = true;
    setList(undefined);
    setError("");
    void adminRead<Page>(
      `/api/control/billing/source-actions?${query}&limit=${pageSize}&offset=${offset}`,
      "读取操作全景失败",
    )
      .then((result) => {
        if (active) setList(result);
      })
      .catch((cause: unknown) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "读取操作全景失败");
      });
    return () => {
      active = false;
    };
  }, [query, offset, revision]);
  useEffect(() => {
    void revision;
    let active = true;
    setDetail(undefined);
    if (selected) {
      // 同一操作编号可能分属不同账号：明细必须带明确作用域，平台行走 platform。
      const scope = selected.user_id
        ? `user_id=${encodeURIComponent(selected.user_id)}`
        : "platform=true";
      void adminRead<Panorama>(
        `/api/control/billing/source-actions/${encodeURIComponent(selected.source_id)}?${scope}`,
        "读取操作明细失败",
      )
        .then((result) => {
          if (active) setDetail(result);
        })
        .catch((cause: unknown) => {
          if (active)
            setError(
              cause instanceof Error ? cause.message : "读取操作明细失败",
            );
        });
    }
    return () => {
      active = false;
    };
  }, [selected, revision]);
  return (
    <section aria-label="操作全景">
      <h3>操作全景</h3>
      <p>
        一次操作 =
        同一操作编号下的所有生成与供应商调用，含内部质检。客户只为用户业务被扣分；
        内部业务与质检成本单独列出，证据不齐时按待核对显示，不计成零成本。
      </p>
      <button
        type="button"
        onClick={() => {
          setError("");
          setSelected(undefined);
          setRevision((value) => value + 1);
        }}
      >
        刷新操作
      </button>
      {error && <p role="alert">{error}</p>}
      {!list && !error && <p role="status">正在读取操作…</p>}
      {list && (
        <>
          <div className="admin-table-scroll">
            <table className="admin-data-table" aria-label="操作列表">
              <thead>
                <tr>
                  <th>操作编号</th>
                  <th>用户</th>
                  <th>生成与调用</th>
                  <th>积分</th>
                  <th>成本</th>
                  <th>质检成本</th>
                  <th>最近活动</th>
                  <th>明细</th>
                </tr>
              </thead>
              <tbody>
                {list.items.map((item) => (
                  <tr key={`${item.user_id ?? "platform"}:${item.source_id}`}>
                    <td>
                      <small>{item.source_id}</small>
                    </td>
                    <td>{item.username}</td>
                    <td>
                      {item.operation_count} 次生成 · {item.attempt_count}{" "}
                      次调用
                      {item.pending_count > 0 &&
                        ` · 处理中 ${item.pending_count}`}
                      {item.failed_count > 0 && ` · 失败 ${item.failed_count}`}
                      <br />
                      {item.services.map((service) => name(service)).join("、")}
                    </td>
                    <td>{item.charged_credits} 积分</td>
                    <td>
                      {item.cost_fen == null
                        ? `待核对 · 已知 ${money(item.known_cost_fen)}`
                        : money(item.cost_fen)}
                      {item.unknown_cost_count > 0 && (
                        <>
                          <br />
                          待核对 {item.unknown_cost_count} 次调用
                        </>
                      )}
                    </td>
                    <td>
                      {item.inspection_attempt_count === 0
                        ? "无"
                        : `质检 ${money(item.inspection_cost_fen)} · ${item.inspection_attempt_count} 次`}
                    </td>
                    <td>{formatRelativeTime(item.last_at)}</td>
                    <td>
                      <button type="button" onClick={() => setSelected(item)}>
                        查看操作明细
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {list.items.length === 0 && <p>该范围内没有操作记录。</p>}
          <button
            type="button"
            disabled={offset === 0}
            onClick={() => setOffset((value) => Math.max(0, value - pageSize))}
          >
            上一页
          </button>
          <span>共 {list.total} 次操作</span>
          <button
            type="button"
            disabled={offset + pageSize >= list.total}
            onClick={() => setOffset((value) => value + pageSize)}
          >
            下一页
          </button>
        </>
      )}
      {selected && (
        <aside aria-label="操作明细">
          <h4>操作明细</h4>
          <p>
            操作编号 {selected.source_id}；{selected.username}。
          </p>
          <button type="button" onClick={() => setSelected(undefined)}>
            关闭明细
          </button>
          {!detail && !error && <p role="status">正在读取操作明细…</p>}
          {detail && (
            <>
              <p>
                {detail.action.operation_count} 次生成 ·{" "}
                {detail.action.attempt_count} 次调用 · 净扣{" "}
                {detail.action.charged_credits} 积分；成本{" "}
                {money(detail.action.cost_fen)}（已知{" "}
                {money(detail.action.known_cost_fen)}）；质检成本{" "}
                {detail.action.inspection_attempt_count === 0
                  ? "无"
                  : money(detail.action.inspection_cost_fen)}
                。
              </p>
              <h5>动作内生成</h5>
              <div className="admin-table-scroll">
                <table className="admin-data-table" aria-label="动作内生成">
                  <thead>
                    <tr>
                      <th>业务</th>
                      <th>状态</th>
                      <th>用量</th>
                      <th>积分</th>
                      <th>成本</th>
                      <th>调用</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.operations.map((operation) => (
                      <tr key={operation.id}>
                        <td>{name(operation.service)}</td>
                        <td>{billingStates[operation.state]}</td>
                        <td>
                          {operation.actual_units ?? "处理中"}{" "}
                          {billingUnit[operation.unit]}
                        </td>
                        <td>{operation.charged_credits}</td>
                        <td>{money(operation.cost_fen)}</td>
                        <td>{operation.attempts.length} 次</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {detail.operations.map((operation) => (
                <section key={operation.id}>
                  <h5>{name(operation.service)} 供应商调用</h5>
                  <table
                    className="admin-data-table"
                    aria-label={`${name(operation.service)} 供应商调用`}
                  >
                    <thead>
                      <tr>
                        <th>业务</th>
                        <th>服务商</th>
                        <th>用量</th>
                        <th>成本</th>
                        <th>凭据</th>
                      </tr>
                    </thead>
                    <tbody>
                      {operation.attempts.map((item) => (
                        <tr key={item.id}>
                          <td>{name(item.service)}</td>
                          <td>{item.provider}</td>
                          <td>
                            {item.usage ?? "待确认"} {billingUnit[item.unit]}
                          </td>
                          <td>{money(item.effective_cost_fen)}</td>
                          <td>
                            {item.evidence_reference
                              ? "凭据已核对"
                              : billingStates[item.state]}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </section>
              ))}
            </>
          )}
        </aside>
      )}
    </section>
  );
}
