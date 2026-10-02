import { useEffect, useRef, useState } from "react";
import {
  type GlobalExternalCallFilters,
  type GlobalExternalCallPage,
  getExternalCallResponse,
  listGlobalExternalCalls,
} from "../api.admin";
import { CopyDiagnosticReference } from "./CopyDiagnosticReference";
import {
  type ExternalCallResponseState,
  ResponseViewer,
} from "./RecordCallsPanel";
import { PageBanner } from "./ui/PageBanner";
import { formatDateTime } from "./ui/vocabulary";

const outcomes: Record<string, string> = {
  SUCCEEDED: "成功",
  PROVIDER_ERROR: "服务返回失败",
  TIMEOUT: "超时",
  NETWORK_ERROR: "网络错误",
  PARSE_ERROR: "响应解析失败",
};
const emptyFilters: GlobalExternalCallFilters = {
  provider: "",
  endpoint: "",
  outcome: "",
  task_ref: "",
  created_from: "",
  created_to: "",
};

export function ExternalCallsPage() {
  const [draft, setDraft] = useState(emptyFilters);
  const [query, setQuery] = useState({ ...emptyFilters, offset: 0, limit: 50 });
  const [page, setPage] = useState<GlobalExternalCallPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [response, setResponse] = useState<ExternalCallResponseState>({
    phase: "idle",
  });
  const requestSequence = useRef(0);
  const responseSequence = useRef(0);
  useEffect(() => {
    const seq = ++requestSequence.current;
    setLoading(true);
    setError("");
    setSelected(null);
    ++responseSequence.current;
    void listGlobalExternalCalls(query)
      .then((result) => {
        if (seq === requestSequence.current) setPage(result);
      })
      .catch((cause) => {
        if (seq === requestSequence.current)
          setError(cause instanceof Error ? cause.message : "读取失败");
      })
      .finally(() => {
        if (seq === requestSequence.current) setLoading(false);
      });
    return () => {
      ++requestSequence.current;
      ++responseSequence.current;
    };
  }, [query]);
  async function openResponse(callId: string) {
    const seq = ++responseSequence.current;
    setSelected(callId);
    setResponse({ phase: "loading" });
    try {
      const detail = await getExternalCallResponse(callId);
      if (seq === responseSequence.current)
        setResponse({ phase: "ready", detail });
    } catch (cause) {
      if (seq === responseSequence.current)
        setResponse({
          phase: "error",
          message: cause instanceof Error ? cause.message : "读取响应失败",
        });
    }
  }
  const serviceLabel = (provider: string) => {
    const index = page?.providers.indexOf(provider) ?? -1;
    return index >= 0 ? `接口服务 ${index + 1}` : "接口服务";
  };
  return (
    <section aria-label="全局接口调用日志">
      <h2>接口调用日志</h2>
      <p>
        仅超级管理员可用。近24小时指标逐次统计，连续等待轮询虽合并展示，仍逐次计入指标；耗时未知不按0处理。历史未采集的指标不回填。
      </p>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {page ? (
        <section aria-label="近24小时服务指标">
          <p>统计起点：{formatDateTime(page.metrics_since)}</p>
          {page.metrics.length === 0 ? (
            <p>近24小时暂无调用观测。</p>
          ) : (
            page.metrics.map((metric) => (
              <article key={metric.provider}>
                <h3>{serviceLabel(metric.provider)}</h3>
                <p>
                  {metric.total} 次调用，失败 {metric.failed} 次；失败率{" "}
                  {metric.failure_rate_pct == null
                    ? "待核对"
                    : `${metric.failure_rate_pct.toFixed(1)}%`}
                  ；平均耗时{" "}
                  {metric.avg_latency_ms == null
                    ? "待核对"
                    : `${Math.round(metric.avg_latency_ms)} 毫秒`}
                  （已知耗时 {metric.latency_samples} 次）
                </p>
              </article>
            ))
          )}
        </section>
      ) : null}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setQuery({ ...draft, offset: 0, limit: 50 });
        }}
      >
        <label>
          服务
          <select
            value={draft.provider}
            onChange={(event) =>
              setDraft({ ...draft, provider: event.target.value })
            }
          >
            <option value="">全部服务</option>
            {page?.providers.map((provider) => (
              <option key={provider} value={provider}>
                {serviceLabel(provider)}
              </option>
            ))}
          </select>
        </label>
        <label>
          接口
          <select
            value={draft.endpoint}
            onChange={(event) =>
              setDraft({ ...draft, endpoint: event.target.value })
            }
          >
            <option value="">全部接口</option>
            {page?.endpoints.map((endpoint) => (
              <option key={endpoint} value={endpoint}>
                {endpoint}
              </option>
            ))}
          </select>
        </label>
        <label>
          结果
          <select
            value={draft.outcome}
            onChange={(event) =>
              setDraft({ ...draft, outcome: event.target.value })
            }
          >
            <option value="">全部结果</option>
            {Object.entries(outcomes).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          开始时间
          <input
            type="datetime-local"
            value={draft.created_from}
            onChange={(event) =>
              setDraft({ ...draft, created_from: event.target.value })
            }
          />
        </label>
        <label>
          结束时间
          <input
            type="datetime-local"
            value={draft.created_to}
            onChange={(event) =>
              setDraft({ ...draft, created_to: event.target.value })
            }
          />
        </label>
        <label>
          任务编号、8 位错误编号或第三方任务号；重号用服务商:凭证
          <input
            value={draft.task_ref}
            placeholder="我方完整或8位编号、第三方任务号或请求编号"
            onChange={(event) =>
              setDraft({ ...draft, task_ref: event.target.value })
            }
          />
        </label>
        <button type="submit" disabled={loading}>
          查询
        </button>
      </form>
      {loading ? <p>读取中…</p> : null}
      {!loading && !error && page ? (
        <>
          <p>共 {page.total} 条调用记录</p>
          {page.items.length === 0 ? (
            <p>没有符合条件的调用。</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>时间</th>
                  <th>服务</th>
                  <th>结果</th>
                  <th>耗时</th>
                  <th>调用次数</th>
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                {page.items.map(({ call, task_type, task_id, request_id }) => (
                  <tr key={call.call_id}>
                    <td>{formatDateTime(call.created_at)}</td>
                    <td>{serviceLabel(call.provider)}</td>
                    <td>{outcomes[call.outcome ?? ""] ?? "历史结果未知"}</td>
                    <td>
                      {call.latency_ms == null
                        ? "待核对"
                        : `${call.latency_ms} 毫秒`}
                    </td>
                    <td>{call.poll_count}</td>
                    <td>
                      <details>
                        <summary>技术详情</summary>
                        <p>
                          接口：{call.method} {call.endpoint}；服务：
                          {call.provider}
                        </p>
                        <p>
                          我方任务：{task_type}{" "}
                          {task_id ? (
                            <CopyDiagnosticReference
                              value={task_id}
                              label="任务编号"
                            />
                          ) : (
                            "未关联"
                          )}
                          ；请求编号：
                          {request_id ? (
                            <CopyDiagnosticReference
                              value={request_id}
                              label="我方请求编号"
                            />
                          ) : (
                            "未知"
                          )}
                        </p>
                        <p>
                          第三方任务号：
                          {call.provider_task_id ? (
                            <CopyDiagnosticReference
                              value={call.provider_task_id}
                              label="第三方任务号"
                            />
                          ) : (
                            "未知"
                          )}
                          ；第三方请求编号：
                          {call.provider_request_id ? (
                            <CopyDiagnosticReference
                              value={call.provider_request_id}
                              label="第三方请求编号"
                            />
                          ) : (
                            "未知"
                          )}
                        </p>
                        {call.exception_type ? (
                          <p>异常类型：{call.exception_type}</p>
                        ) : null}
                        {call.advice ? <p>{call.advice}</p> : null}
                        {call.mapping_revision ? (
                          <p>
                            归类版本：{call.mapping_revision} ·{" "}
                            {call.mapping_evidence}
                          </p>
                        ) : null}
                        <p>
                          状态码：{call.http_status ?? "未知"}；原因：
                          {call.provider_message ??
                            call.error_message ??
                            "未附原因"}
                        </p>
                        <pre>
                          {JSON.stringify(call.request_summary, null, 2)}
                        </pre>
                        {call.has_response_body ? (
                          <button
                            type="button"
                            onClick={() => void openResponse(call.call_id)}
                          >
                            查看原始响应
                          </button>
                        ) : (
                          <span>无响应正文</span>
                        )}
                      </details>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <nav aria-label="调用日志分页">
            <button
              type="button"
              disabled={query.offset === 0}
              onClick={() =>
                setQuery({ ...query, offset: Math.max(0, query.offset - 50) })
              }
            >
              上一页
            </button>
            <button
              type="button"
              disabled={query.offset + 50 >= page.total}
              onClick={() => setQuery({ ...query, offset: query.offset + 50 })}
            >
              下一页
            </button>
          </nav>
        </>
      ) : null}
      {selected ? (
        <ResponseViewer
          key={selected}
          callId={selected}
          state={response}
          reason={
            page?.items.find(({ call }) => call.call_id === selected)?.call
              .provider_message
          }
          onClose={() => {
            ++responseSequence.current;
            setSelected(null);
          }}
        />
      ) : null}
    </section>
  );
}
