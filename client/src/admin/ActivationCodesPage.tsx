import { useCallback, useEffect, useState } from "react";

import {
  type ActivationCodeListItem,
  AdminActivationError,
  adminActivationErrorMessage,
  archiveActivationCode,
  createIdempotencyKey,
  listActivationCodes,
  resumeActivationCode,
  revealActivationCode,
  revokeActivationCode,
  suspendActivationCode,
} from "../api.admin";
import { ConfirmDialog, type ConfirmLevel } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { ActivationCodeStatusBadge } from "./ui/StatusBadge";
import { activationCodeStatusLabel, formatDateTime } from "./ui/vocabulary";
import "./admin-activation.css";

type CodeAction = "revoke" | "archive" | "suspend" | "resume";

const PAGE_SIZE = 50;

type PendingAction =
  | { kind: "copy"; codeId: string }
  | { kind: "code"; action: CodeAction; codeId: string; status: string };

export function ActivationCodesPage({
  readOnly = false,
  refreshToken = 0,
  onSessionExpired,
}: {
  readOnly?: boolean;
  refreshToken?: number;
  onSessionExpired?: () => void;
}) {
  const [items, setItems] = useState<ActivationCodeListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [filterStatus, setFilterStatus] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [actionKey, setActionKey] = useState<string | null>(null);

  const handleFailure = useCallback(
    (cause: unknown, fallback: string) => {
      if (cause instanceof AdminActivationError && cause.status === 401) {
        setError("会话已失效，请重新登录");
        onSessionExpired?.();
        return;
      }
      setError(adminActivationErrorMessage(cause, fallback));
    },
    [onSessionExpired],
  );

  useEffect(() => {
    void refreshToken;
    let cancelled = false;
    void (async () => {
      setError("");
      setLoading(true);
      try {
        const page = await listActivationCodes({
          status: filterStatus || undefined,
          search: search || undefined,
          include_archived: includeArchived || undefined,
          limit: PAGE_SIZE,
          offset,
        });
        if (!cancelled) {
          setItems(page.items);
          setTotal(page.total);
        }
      } catch (cause) {
        if (!cancelled) {
          handleFailure(cause, "读取激活码失败");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    filterStatus,
    search,
    includeArchived,
    offset,
    handleFailure,
    refreshToken,
  ]);

  // 搜索（A9）已下沉到服务端：掩码码或绑定用户名匹配，跨全部页生效。
  const visibleItems = items;

  function openAction(action: PendingAction) {
    setPending(action);
    setActionKey(null);
    setError("");
    setNotice("");
    setActionError("");
  }

  async function submitAction(reason: string) {
    if (!pending || busy) {
      return;
    }

    setBusy(true);
    setActionError("");
    try {
      if (pending.kind === "copy") {
        const key = actionKey ?? createIdempotencyKey();
        setActionKey(key);
        const result = await revealActivationCode(pending.codeId, reason, key);
        await navigator.clipboard.writeText(result.activation_code);
        setNotice(`激活码已复制（request id: ${result.request_id}）`);
      } else if (pending.kind === "code") {
        const key = actionKey ?? createIdempotencyKey();
        setActionKey(key);
        if (pending.action === "archive") {
          const result = await archiveActivationCode(
            pending.codeId,
            reason,
            key,
          );
          setItems((current) =>
            current.filter((item) => item.code_id !== result.code_id),
          );
          setNotice(
            `激活码已删除并保留审计记录（request id: ${result.request_id}）`,
          );
        } else {
          const mutate =
            pending.action === "revoke"
              ? revokeActivationCode
              : pending.action === "suspend"
                ? suspendActivationCode
                : resumeActivationCode;
          const result = await mutate(pending.codeId, reason, key);
          setItems((current) =>
            current.map((item) =>
              item.code_id === result.code_id
                ? { ...item, status: result.status }
                : item,
            ),
          );
          setNotice(
            `${activationCodeStatusLabel(result.status)}（request id: ${result.request_id}）`,
          );
        }
      }
      setPending(null);
      setActionKey(null);
    } catch (cause) {
      const fallback =
        pending.kind === "copy"
          ? "复制激活码失败"
          : CODE_ACTION_FALLBACK[pending.action];
      if (cause instanceof AdminActivationError && cause.status === 401) {
        setError("会话已失效，请重新登录");
        onSessionExpired?.();
      } else if (cause instanceof AdminActivationError) {
        setActionError(adminActivationErrorMessage(cause, fallback));
      } else {
        setError(adminActivationErrorMessage(cause, fallback));
      }
      if (cause instanceof AdminActivationError && cause.status !== undefined) {
        // 明确失败释放幂等键；超时等模糊失败保留键以便重试重放。
        setActionKey(null);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="activation-device-page" aria-label="激活码管理">
      {readOnly ? (
        <PageBanner tone="notice">当前为只读模式，写操作不可用。</PageBanner>
      ) : null}
      {loading ? <p className="admin-hint">正在读取激活码…</p> : null}
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

      <section className="activation-code-list-card">
        <header className="activation-code-list-header">
          <h2>激活码列表</h2>
          <form
            className="activation-device-filters"
            onSubmit={(event) => {
              event.preventDefault();
              setOffset(0);
              setSearch(searchDraft.trim());
            }}
          >
            <label className="activation-filter-search">
              <span className="sr-only">搜索</span>
              <input
                aria-label="搜索"
                placeholder="请输入激活码或绑定账号"
                value={searchDraft}
                onChange={(event) => setSearchDraft(event.target.value)}
              />
            </label>
            <label>
              <span className="sr-only">状态</span>
              <select
                aria-label="状态"
                value={filterStatus}
                onChange={(event) => {
                  setOffset(0);
                  setFilterStatus(event.target.value);
                }}
              >
                <option value="">全部状态</option>
                <option value="GENERATED">待启用</option>
                <option value="ISSUED">可使用</option>
                <option value="ACTIVE">使用中</option>
                <option value="SUSPENDED">已暂停</option>
                <option value="REVOKED">已撤销</option>
                <option value="EXPIRED">已过期</option>
              </select>
            </label>
            <label className="activation-filter-archived">
              <input
                checked={includeArchived}
                type="checkbox"
                onChange={(event) => {
                  setOffset(0);
                  setIncludeArchived(event.target.checked);
                }}
              />
              显示已归档
            </label>
            <button type="submit">搜索</button>
          </form>
        </header>

        <div className="table-scroll admin-table-card">
          <table
            aria-label="激活码列表"
            className="internal-table admin-data-table"
          >
            <thead>
              <tr>
                <th>激活码</th>
                <th>状态</th>
                <th>绑定账号</th>
                <th>有效期至</th>
                <th>创建时间</th>
                {readOnly ? null : <th>操作</th>}
              </tr>
            </thead>
            <tbody>
              {visibleItems.map((item) => {
                return (
                  <tr key={item.code_id}>
                    <td>
                      <code>{item.masked_code}</code>
                      {readOnly ? null : (
                        <button
                          type="button"
                          onClick={() =>
                            openAction({ kind: "copy", codeId: item.code_id })
                          }
                        >
                          复制
                        </button>
                      )}
                    </td>
                    <td>
                      <ActivationCodeStatusBadge status={item.status} />
                      {item.archived_at ? (
                        <span className="status-badge status-badge--neutral">
                          已归档
                        </span>
                      ) : null}
                    </td>
                    <td>{item.bound_username ?? "未绑定"}</td>
                    <td
                      className={
                        item.status === "EXPIRED" ? "is-expired" : undefined
                      }
                    >
                      {formatDateTime(item.expires_at ?? null)}
                    </td>
                    <td>{formatDateTime(item.created_at ?? null)}</td>
                    {readOnly ? null : (
                      <td>
                        <div className="admin-actions admin-actions--table">
                          {item.status === "ACTIVE" ? (
                            <button
                              type="button"
                              onClick={() =>
                                openAction({
                                  kind: "code",
                                  action: "suspend",
                                  codeId: item.code_id,
                                  status: item.status,
                                })
                              }
                            >
                              暂停
                            </button>
                          ) : null}
                          {item.status === "SUSPENDED" ? (
                            <button
                              type="button"
                              onClick={() =>
                                openAction({
                                  kind: "code",
                                  action: "resume",
                                  codeId: item.code_id,
                                  status: item.status,
                                })
                              }
                            >
                              恢复
                            </button>
                          ) : null}
                          {item.status === "REVOKED" ? (
                            <button
                              className="admin-action--danger"
                              type="button"
                              onClick={() =>
                                openAction({
                                  kind: "code",
                                  action: "archive",
                                  codeId: item.code_id,
                                  status: item.status,
                                })
                              }
                            >
                              删除激活码
                            </button>
                          ) : item.status === "EXPIRED" ? (
                            <span className="admin-hint">无需操作</span>
                          ) : (
                            <button
                              className="admin-action--danger"
                              type="button"
                              onClick={() =>
                                openAction({
                                  kind: "code",
                                  action: "revoke",
                                  codeId: item.code_id,
                                  status: item.status,
                                })
                              }
                            >
                              撤销激活码
                            </button>
                          )}
                        </div>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {visibleItems.length === 0 ? (
          <p className="activation-device-empty">没有符合条件的激活码。</p>
        ) : null}

        <footer className="activation-list-footer">
          <span>共 {total} 条</span>
          <Pagination
            limit={PAGE_SIZE}
            offset={offset}
            total={total}
            onPageChange={setOffset}
          />
        </footer>
      </section>

      <ConfirmDialog
        busy={busy}
        confirmLabel={pending?.kind === "copy" ? "确认复制" : "确认执行"}
        description={pending ? actionWarning(pending) : null}
        error={actionError}
        level={confirmLevel(pending)}
        open={pending !== null}
        title={pending ? actionTitle(pending) : ""}
        onClose={() => {
          setPending(null);
          setActionError("");
        }}
        onConfirm={(reason: string) => void submitAction(reason)}
      />
    </section>
  );
}

const CODE_ACTION_FALLBACK: Record<CodeAction, string> = {
  archive: "删除激活码失败",
  revoke: "撤销激活码失败",
  suspend: "暂停激活码失败",
  resume: "恢复激活码失败",
};

/** 吊销/删除/吊销凭据是不可逆终态：要求原因 + 勾选；其余要求原因。 */
function confirmLevel(pending: PendingAction | null): ConfirmLevel {
  if (pending === null || pending.kind === "copy") {
    return "reason";
  }
  if (pending.kind === "code") {
    return pending.action === "revoke" || pending.action === "archive"
      ? "reasonAndAck"
      : "reason";
  }
  return "reason";
}

function actionTitle(action: PendingAction): string {
  if (action.kind === "copy") {
    return `复制激活码 ${action.codeId}`;
  }
  if (action.kind === "code") {
    if (action.action === "archive") {
      return `删除激活码 ${action.codeId}`;
    }
    if (action.action === "revoke") {
      return `撤销激活码 ${action.codeId}`;
    }
    return `${action.action === "suspend" ? "暂停" : "恢复"}激活码 ${action.codeId}`;
  }
  return "激活码操作";
}

function actionWarning(action: PendingAction): string {
  if (action.kind === "copy") {
    return "请输入本次查看明文的真实业务原因。复制操作会写入审计记录。";
  }
  if (action.kind === "code") {
    if (action.action === "archive") {
      return "删除后将从日常列表隐藏，但充值和审计记录会继续保留。";
    }
    if (action.action === "revoke") {
      return "撤销后该激活码不可恢复，关联账号无法再用它重新登录。";
    }
    return action.action === "suspend"
      ? "暂停后该激活码下的账号无法登录生成，恢复后立即生效。"
      : "恢复后该激活码下的账号可以重新登录生成。";
  }
  return "";
}
