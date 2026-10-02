import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import {
  type AdminExternalCall,
  type AdminExternalCallResponse,
  getAdminGenerationRecordCalls,
  getExternalCallResponse,
} from "../api.admin";
import { CopyDiagnosticReference } from "./CopyDiagnosticReference";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { formatDateTime, labelFrom } from "./ui/vocabulary";

const EXTERNAL_CALL_OUTCOME_LABELS: Record<string, string> = {
  SUCCEEDED: "成功",
  PROVIDER_ERROR: "上游报错",
  TIMEOUT: "超时",
  NETWORK_ERROR: "网络错误",
  PARSE_ERROR: "解析失败",
};
const CALL_PAGE_SIZE = 50;

function readableJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

function formatCallOutcome(call: AdminExternalCall): string {
  if (!call.outcome) return call.provider_error_code ?? "—";
  const label = labelFrom(EXTERNAL_CALL_OUTCOME_LABELS, call.outcome);
  return call.provider_error_code
    ? `${label} · ${call.provider_error_code}`
    : label;
}

type ExternalCallsState =
  | { phase: "idle" }
  | { phase: "loading" }
  | { phase: "error"; message: string }
  | { phase: "ready"; calls: AdminExternalCall[]; total: number };

export type ExternalCallResponseState =
  | { phase: "idle" }
  | { phase: "loading" }
  | { phase: "error"; message: string }
  | { phase: "ready"; detail: AdminExternalCallResponse };

/**
 * P0-9：一条记录的第三方调用留痕。生成记录详情与充值订单查单
 * （recordType=RECHARGE_ORDER）共用：首次展开时才懒加载（`active` 由外层
 * 的点击驱动）；「查看原始响应」每次都真实读取——服务端会为此写一条高敏
 * 审计（external_call.response_view），因此不做前端缓存，也不在展开时
 * 自动读取。审计员（readOnly）看得到调用概要，但服务端对其拒绝原始响应，
 * 入口一并隐藏。
 */
export function RecordCallsPanel({
  recordType,
  recordId,
  active,
  readOnly,
}: {
  recordType: string;
  recordId: string;
  active: boolean;
  readOnly: boolean;
}) {
  const [state, setState] = useState<ExternalCallsState>({ phase: "idle" });
  const [offset, setOffset] = useState(0);
  const [responseCallId, setResponseCallId] = useState<string | null>(null);
  const [responseState, setResponseState] = useState<ExternalCallResponseState>(
    { phase: "idle" },
  );
  const callsSeqRef = useRef(0);
  const responseSeqRef = useRef(0);
  const recordKey = `${recordType}:${recordId}:${readOnly ? "auditor" : "administrator"}`;
  const recordKeyRef = useRef(recordKey);

  useEffect(() => {
    if (recordKeyRef.current === recordKey) return;
    recordKeyRef.current = recordKey;
    // 切换任务时使旧请求失效，不能把上一条任务的敏感正文带到新详情。
    callsSeqRef.current += 1;
    responseSeqRef.current += 1;
    setOffset(0);
    setState({ phase: "idle" });
    setResponseCallId(null);
    setResponseState({ phase: "idle" });
  }, [recordKey]);

  const load = useCallback(async () => {
    const seq = callsSeqRef.current + 1;
    callsSeqRef.current = seq;
    setState({ phase: "loading" });
    try {
      const response = await getAdminGenerationRecordCalls(
        recordType,
        recordId,
        { limit: CALL_PAGE_SIZE, offset },
      );
      if (seq !== callsSeqRef.current) return;
      setState({
        phase: "ready",
        calls: response.items,
        total: response.total,
      });
    } catch (cause) {
      if (seq !== callsSeqRef.current) return;
      setState({
        phase: "error",
        message:
          cause instanceof Error && cause.message
            ? cause.message
            : "读取第三方调用记录失败",
      });
    }
  }, [recordType, recordId, offset]);

  useEffect(() => {
    if (active && state.phase === "idle") {
      void load();
    }
  }, [active, state.phase, load]);

  async function openResponse(callId: string) {
    const seq = responseSeqRef.current + 1;
    responseSeqRef.current = seq;
    setResponseCallId(callId);
    setResponseState({ phase: "loading" });
    try {
      const detail = await getExternalCallResponse(callId);
      // 连点两条时以最后点击的那条为准。
      if (seq !== responseSeqRef.current) return;
      setResponseState({ phase: "ready", detail });
    } catch (cause) {
      if (seq !== responseSeqRef.current) return;
      setResponseState({
        phase: "error",
        message:
          cause instanceof Error && cause.message
            ? cause.message
            : "读取调用原始响应失败",
      });
    }
  }

  if (!active) return null;

  return (
    <section
      aria-label={`记录 ${recordId} 的第三方调用记录`}
      className="admin-generation-records__calls"
    >
      <h4>第三方调用记录</h4>
      {state.phase === "loading" ? (
        <p className="admin-generation-records__calls-note">读取中…</p>
      ) : null}
      {state.phase === "error" ? (
        <div className="admin-generation-records__calls-error">
          <PageBanner tone="error">加载失败：{state.message}</PageBanner>
          <button type="button" onClick={() => void load()}>
            重试
          </button>
        </div>
      ) : null}
      {state.phase === "ready" && state.calls.length === 0 ? (
        <p className="admin-generation-records__calls-note">
          该记录暂无第三方调用留痕。
        </p>
      ) : null}
      {state.phase === "ready" && state.calls.length > 0 ? (
        <>
          <p className="admin-generation-records__calls-note">
            {state.total > state.calls.length
              ? `共 ${state.total} 次调用，当前第 ${offset + 1}–${offset + state.calls.length} 次。`
              : `共 ${state.total} 次调用。`}
          </p>
          <DataTable
            ariaLabel={`记录 ${recordId} 的第三方调用列表`}
            headers={
              <>
                <th>时间</th>
                <th>接口</th>
                <th>模型</th>
                <th>轮次</th>
                <th>状态码</th>
                <th>耗时</th>
                <th>结果</th>
                <th>我方任务编号</th>
                <th>第三方编号</th>
                {readOnly ? null : <th>操作</th>}
              </>
            }
          >
            {state.calls.map((call) => (
              <tr key={call.call_id}>
                <td>{formatDateTime(call.created_at)}</td>
                <td>{call.endpoint}</td>
                <td>{call.model ?? "—"}</td>
                <td>{call.attempt != null ? `第 ${call.attempt} 次` : "—"}</td>
                <td>{call.http_status ?? "—"}</td>
                <td>
                  {call.latency_ms != null ? `${call.latency_ms} ms` : "—"}
                </td>
                <td>
                  {formatCallOutcome(call)}
                  {(call.poll_count ?? 1) > 1 ? (
                    <small>
                      （同状态轮询 {call.poll_count} 次，最后{" "}
                      {formatDateTime(call.last_seen_at ?? call.created_at)}）
                    </small>
                  ) : null}
                </td>
                <td>
                  <CopyDiagnosticReference
                    value={call.task_id ?? recordId}
                    label="任务编号"
                  />
                </td>
                <td>
                  {call.provider_task_id ? (
                    <CopyDiagnosticReference
                      value={call.provider_task_id}
                      label="第三方任务号"
                    />
                  ) : null}
                  {call.provider_request_id ? (
                    <p>
                      <CopyDiagnosticReference
                        value={call.provider_request_id}
                        label="第三方请求编号"
                      />
                    </p>
                  ) : null}
                  {!call.provider_task_id && !call.provider_request_id
                    ? "—"
                    : null}
                </td>
                {readOnly ? null : (
                  <td>
                    {call.request_id ? (
                      <p>
                        我方请求编号：
                        <CopyDiagnosticReference
                          value={call.request_id}
                          label="我方请求编号"
                        />
                      </p>
                    ) : null}
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
                    {call.request_summary != null ? (
                      <details>
                        <summary>请求摘要</summary>
                        <pre>
                          {JSON.stringify(call.request_summary, null, 2)}
                        </pre>
                      </details>
                    ) : null}
                    <button
                      aria-label={`查看调用 ${call.call_id} 的原始响应`}
                      type="button"
                      onClick={() => void openResponse(call.call_id)}
                    >
                      查看原始响应
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </DataTable>
          {state.total > CALL_PAGE_SIZE ? (
            <nav aria-label="调用记录分页">
              <button
                type="button"
                disabled={offset === 0}
                onClick={() => {
                  setOffset(Math.max(0, offset - CALL_PAGE_SIZE));
                  setState({ phase: "idle" });
                }}
              >
                上一页
              </button>
              <button
                type="button"
                disabled={offset + state.calls.length >= state.total}
                onClick={() => {
                  setOffset(offset + CALL_PAGE_SIZE);
                  setState({ phase: "idle" });
                }}
              >
                下一页
              </button>
            </nav>
          ) : null}
        </>
      ) : null}
      {!readOnly && responseCallId && responseState.phase !== "idle" ? (
        <ResponseViewer
          key={responseCallId}
          callId={responseCallId}
          state={responseState}
          reason={
            state.phase === "ready"
              ? state.calls.find((call) => call.call_id === responseCallId)
                  ?.provider_message
              : null
          }
          onClose={() => {
            responseSeqRef.current += 1;
            setResponseCallId(null);
            setResponseState({ phase: "idle" });
          }}
        />
      ) : null}
    </section>
  );
}

export function ResponseViewer({
  callId,
  state,
  onClose,
  reason,
}: {
  callId: string;
  state: ExternalCallResponseState;
  onClose: () => void;
  reason: string | null | undefined;
}) {
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function exportResponse(action: "copy" | "download") {
    setActionError(null);
    setBusy(true);
    try {
      // 每次复制/下载重新向服务端读取；用户已看过正文也不能跳过敏感审计。
      const detail = await getExternalCallResponse(callId);
      const text = readableJson(detail.response_body ?? "");
      if (action === "copy") {
        await navigator.clipboard.writeText(text);
      } else {
        const url = URL.createObjectURL(
          new Blob([text], { type: "text/plain;charset=utf-8" }),
        );
        const link = document.createElement("a");
        link.href = url;
        link.download = `external-call-${callId}.txt`;
        link.click();
        URL.revokeObjectURL(url);
      }
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : "导出响应失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      aria-label={`调用 ${callId} 的原始响应`}
      className="admin-generation-records__response"
    >
      <h5>原始响应 · {callId}</h5>
      {reason ? <p>失败原因：{reason}</p> : null}
      {actionError ? <PageBanner tone="error">{actionError}</PageBanner> : null}
      {state.phase === "loading" ? (
        <p className="admin-generation-records__calls-note">读取中…</p>
      ) : null}
      {state.phase === "error" ? (
        <PageBanner tone="error">读取失败：{state.message}</PageBanner>
      ) : null}
      {state.phase === "ready" ? (
        <>
          <p className="admin-generation-records__calls-note">
            {state.detail.response_body_bytes != null
              ? `响应大小 ${state.detail.response_body_bytes} 字节`
              : "响应大小未知"}
            {state.detail.truncated ? "（超出上限已截断）" : ""}
            {state.detail.truncated === null
              ? "（历史记录，是否完整无法确认）"
              : ""}
            。本次查看已记入审计日志。
          </p>
          {state.detail.response_headers ? (
            <details>
              <summary>响应头</summary>
              <dl>
                {Object.entries(state.detail.response_headers).map(
                  ([name, value]) => (
                    <Fragment key={name}>
                      <dt>{name}</dt>
                      <dd>{value}</dd>
                    </Fragment>
                  ),
                )}
              </dl>
            </details>
          ) : null}
          <pre className="admin-generation-records__response-body">
            {readableJson(state.detail.response_body ?? "（无响应体）")}
          </pre>
          <button
            type="button"
            disabled={busy}
            onClick={() => void exportResponse("copy")}
          >
            复制响应
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void exportResponse("download")}
          >
            下载响应
          </button>
        </>
      ) : null}
      <button
        className="admin-generation-records__response-close"
        type="button"
        onClick={onClose}
      >
        收起响应
      </button>
    </section>
  );
}
