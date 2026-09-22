import { useEffect, useRef, useState } from "react";
import {
  capturePromptSession,
  customerVisibleErrorMessage,
  getLatestScriptRewriteTask,
  getScriptRewriteTask,
  rewriteProjectScript,
  type ScriptRewriteTask,
  ScriptRewriteTaskError,
  waitForScriptRewriteTask,
} from "../api";
import { useStudio } from "./context";
import {
  clearScriptRewriteIdempotencyKey,
  type ScriptRewriteScope,
  scriptRewriteIdempotencyKey,
  shouldClearScriptRewriteIdempotencyKey,
} from "./scriptRewrite";
import type { StudioDraft } from "./types";
import { Button, Hint, Icon, Panel } from "./ui";

// S7：改写任务跨会话恢复。离开或刷新页面会丢掉会话内的等待，任务仍在后台跑，
// 但结果无人接收。重进时若无恢复，用户会把"结果未回填"当成失败而重复提交——
// 任务已成功后重复提交会重新扣费。pending 记录发起时的正文与幂等 key：同 tab
// 重进优先按 taskId 精确查询，跨会话回退到 latest 查询，并按来源/身份/正文
// 严格匹配后才回填，避免把别的改写结果错误覆盖到当前文案。
function replicaRewritePendingStorageKey(accountId: string): string {
  return `studio:pending-replica-rewrite:${accountId}`;
}

interface ReplicaRewritePending {
  resultText: string;
  requestKey: string;
  startedAt: number;
  taskId?: string;
}

function readReplicaRewritePending(
  accountId: string,
): ReplicaRewritePending | undefined {
  try {
    const value = JSON.parse(
      sessionStorage.getItem(replicaRewritePendingStorageKey(accountId)) ??
        "null",
    );
    return value &&
      typeof value.resultText === "string" &&
      typeof value.requestKey === "string" &&
      typeof value.startedAt === "number"
      ? (value as ReplicaRewritePending)
      : undefined;
  } catch {
    return undefined;
  }
}

function storeReplicaRewritePending(
  accountId: string,
  pending: ReplicaRewritePending | undefined,
) {
  try {
    if (pending)
      sessionStorage.setItem(
        replicaRewritePendingStorageKey(accountId),
        JSON.stringify(pending),
      );
    else sessionStorage.removeItem(replicaRewritePendingStorageKey(accountId));
  } catch {
    /* sessionStorage 不可用时仅丢失跨会话恢复能力，不影响提交本身。 */
  }
}

function replicaRewriteTaskMatches(
  task: ScriptRewriteTask,
  draft: StudioDraft,
  expectedText: string,
): boolean {
  return (
    task.project_id === draft.projectId &&
    (task.identity_id ?? "") === (draft.ipId ?? "") &&
    (task.source_asset_id ?? "") === (draft.sourceAssetId ?? "") &&
    task.source_text === expectedText &&
    (task.instructions ?? "") === ""
  );
}

export function ReplicaNarration() {
  const { state, user, review, patchDraft } = useStudio();
  const draft = state.draft;
  const readOnly = user.role === "auditor";
  const key = JSON.stringify([
    user.id,
    draft.id,
    draft.projectId,
    draft.sourceAssetId,
    draft.ipId,
  ]);
  const latest = useRef({
    key,
    text: draft.script.text,
    revision: 0,
    draft,
    patchDraft,
    readOnly,
  });
  const revision =
    latest.current.revision +
    Number(
      latest.current.key !== key || latest.current.text !== draft.script.text,
    );
  latest.current = {
    key,
    text: draft.script.text,
    revision,
    draft,
    patchDraft,
    readOnly,
  };
  const mounted = useRef(true);
  const inFlight = useRef(false);
  const recoveryCheckedRef = useRef(new Set<string>());
  const recoveryOperationRef = useRef(0);
  const narrationRef = useRef<HTMLTextAreaElement | null>(null);
  const narrationChars = Array.from(draft.script.text).length;
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [undo, setUndo] = useState<{
    key: string;
    before: string;
    after: string;
  } | null>(null);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  // 口播文案框按内容自适应高度：固定大框在短文案时留出一整块空白，长文案又要
  // 内部滚动。夹在 84–264px 之间（与 CSS 的 min/max-height 同源），超出后才滚动。
  // biome-ignore lint/correctness/useExhaustiveDependencies: 文本变化是重算高度的唯一触发条件，effect 内部靠 scrollHeight 测量实际内容。
  useEffect(() => {
    const el = narrationRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(Math.max(el.scrollHeight, 84), 264)}px`;
  }, [draft.script.text]);
  // S7：挂载恢复。等待中的任务只存在于会话内，重进时查询并接管：运行中的
  // 恢复等待、已完成的直接回填（附撤销）、失败的给出上次失败信息。正文在
  // 等待期间被编辑过时不覆盖；来源/身份/正文不匹配一律不动。
  // biome-ignore lint/correctness/useExhaustiveDependencies: 恢复检查按项目范围仅执行一次（recoveryCheckedRef 保证），依赖列表覆盖 effect 内实际读取的草稿字段。
  useEffect(() => {
    if (review || readOnly || !draft.projectId || !draft.script.text.trim())
      return;
    const checkKey = JSON.stringify([
      user.id,
      draft.projectId,
      draft.sourceAssetId ?? "",
      draft.ipId ?? "",
    ]);
    if (recoveryCheckedRef.current.has(checkKey)) return;
    recoveryCheckedRef.current.add(checkKey);
    const operation = ++recoveryOperationRef.current;
    const started = { ...latest.current };
    const sessionCurrent = capturePromptSession();
    const current = () =>
      operation === recoveryOperationRef.current &&
      mounted.current &&
      sessionCurrent() &&
      latest.current.key === started.key &&
      !latest.current.readOnly;
    const projectId = draft.projectId;
    const pending = readReplicaRewritePending(user.id);
    const expectedText = pending?.resultText ?? draft.script.text;
    void (async () => {
      let task: ScriptRewriteTask | null;
      try {
        task = pending?.taskId
          ? await getScriptRewriteTask(pending.taskId)
          : await getLatestScriptRewriteTask(
              projectId,
              draft.ipId ?? null,
              draft.sourceAssetId ?? null,
            );
      } catch (cause) {
        if (current())
          setMessage(
            customerVisibleErrorMessage(
              cause,
              "读取上次 AI 改写任务失败，可重新提交。",
            ),
          );
        return;
      }
      if (!current() || !task) return;
      if (!replicaRewriteTaskMatches(task, latest.current.draft, expectedText))
        return;
      // 结果已经回填过（任务标记或正文任一命中）就不再处理，避免重复提示与撤销。
      if (
        task.id === latest.current.draft.script.rewriteTaskId ||
        task.result?.rewritten_text?.trim() === latest.current.draft.script.text
      ) {
        storeReplicaRewritePending(user.id, undefined);
        return;
      }
      const scope: ScriptRewriteScope = {
        accountId: user.id,
        projectId,
        sourceAssetId: latest.current.draft.sourceAssetId ?? "",
        identityId: latest.current.draft.ipId ?? "",
        scriptId: latest.current.draft.script.id,
        scriptVersion: latest.current.draft.script.version,
        text: expectedText,
      };
      let completed = task;
      if (task.status === "PENDING" || task.status === "RUNNING") {
        inFlight.current = true;
        if (current()) setBusy(true);
        try {
          completed = await waitForScriptRewriteTask(task.id);
        } catch (cause) {
          if (shouldClearScriptRewriteIdempotencyKey(cause)) {
            if (pending?.requestKey)
              clearScriptRewriteIdempotencyKey(scope, pending.requestKey);
            storeReplicaRewritePending(user.id, undefined);
          }
          if (current())
            setMessage(
              customerVisibleErrorMessage(
                cause,
                "上次 AI 改写未完成，请重试。",
              ),
            );
          return;
        } finally {
          inFlight.current = false;
          if (mounted.current) setBusy(false);
        }
      }
      if (!current()) return;
      const rewritten = completed.result?.rewritten_text?.trim();
      if (completed.status !== "SUCCEEDED" || !rewritten) {
        if (
          completed.status === "FAILED" ||
          completed.status === "SUBMISSION_UNCERTAIN"
        ) {
          const failure = new ScriptRewriteTaskError(completed);
          if (shouldClearScriptRewriteIdempotencyKey(failure)) {
            if (pending?.requestKey)
              clearScriptRewriteIdempotencyKey(scope, pending.requestKey);
            storeReplicaRewritePending(user.id, undefined);
          }
          setMessage(
            customerVisibleErrorMessage(
              failure,
              "上次 AI 改写未完成，请重试。",
            ),
          );
        }
        return;
      }
      // 任务成功：还原结果。正文在等待期间被编辑过时不覆盖，仅提示。
      if (pending?.requestKey)
        clearScriptRewriteIdempotencyKey(scope, pending.requestKey);
      storeReplicaRewritePending(user.id, undefined);
      const latestScript = latest.current.draft.script;
      if (latestScript.text !== expectedText) {
        setMessage("AI 改写已完成，但文案已修改，结果未覆盖。");
        return;
      }
      setUndo({ key, before: expectedText, after: rewritten });
      latest.current.patchDraft({
        script: {
          ...latestScript,
          text: rewritten,
          confirmed: false,
          resultKind: "rewritten",
          rewriteTaskId: completed.id,
        },
        scriptEdited: true,
      });
      setMessage("AI 改写已完成，可继续编辑");
    })();
  }, [
    draft.id,
    draft.ipId,
    draft.projectId,
    draft.script.id,
    draft.script.text,
    draft.sourceAssetId,
    key,
    readOnly,
    review,
    user.id,
  ]);
  const rewrite = async () => {
    if (
      review ||
      readOnly ||
      inFlight.current ||
      !draft.projectId ||
      !draft.script.text.trim()
    )
      return;
    const started = { ...latest.current };
    const sessionCurrent = capturePromptSession();
    const current = () =>
      mounted.current &&
      sessionCurrent() &&
      latest.current.key === started.key &&
      !latest.current.readOnly;
    const scope = {
      accountId: user.id,
      projectId: draft.projectId,
      sourceAssetId: draft.sourceAssetId ?? "",
      identityId: draft.ipId ?? "",
      scriptId: draft.script.id,
      scriptVersion: draft.script.version,
      text: draft.script.text,
    };
    // S7：上次留下的 pending 若正文一致，复用同一幂等 key——失败重试时服务端
    // 拉起原任务而不是新建任务；也已失效的旧 key 不会让新请求误命中旧结果。
    const retryPending = readReplicaRewritePending(user.id);
    const requestKey =
      retryPending && retryPending.resultText === scope.text
        ? retryPending.requestKey
        : scriptRewriteIdempotencyKey(scope);
    const pendingRecord = {
      resultText: scope.text,
      requestKey,
      startedAt: Date.now(),
    };
    storeReplicaRewritePending(user.id, pendingRecord);
    inFlight.current = true;
    setBusy(true);
    setMessage("");
    try {
      const task = await rewriteProjectScript(
        draft.projectId,
        draft.script.text,
        draft.ipId,
        draft.sourceAssetId,
        requestKey,
      );
      storeReplicaRewritePending(user.id, {
        ...pendingRecord,
        taskId: task.id,
      });
      const result = await waitForScriptRewriteTask(task.id);
      if (
        result.status !== "SUCCEEDED" ||
        !result.result?.rewritten_text?.trim()
      )
        throw new Error(
          result.error_message || "AI 改写未完成，当前文案已保留。",
        );
      clearScriptRewriteIdempotencyKey(scope, requestKey);
      storeReplicaRewritePending(user.id, undefined);
      if (!current()) return;
      if (latest.current.revision !== started.revision) {
        setMessage("文案已修改，迟到的改写结果未覆盖当前内容。");
        return;
      }
      const text = result.result.rewritten_text;
      setUndo({ key, before: started.text, after: text });
      latest.current.patchDraft({
        script: {
          ...latest.current.draft.script,
          text,
          confirmed: false,
          resultKind: "rewritten",
          rewriteTaskId: result.id,
        },
        scriptEdited: true,
      });
      setMessage("AI 改写已完成，可继续编辑");
    } catch (cause) {
      if (shouldClearScriptRewriteIdempotencyKey(cause)) {
        clearScriptRewriteIdempotencyKey(scope, requestKey);
        storeReplicaRewritePending(user.id, undefined);
      }
      if (current())
        setMessage(
          customerVisibleErrorMessage(cause, "AI 改写失败，当前文案已保留。"),
        );
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  return (
    <Panel className="creation-replica-narration">
      <div className="creation-panel-title-row">
        <span className="creation-replica-narration__title">
          口播文案
          {narrationChars > 0 ? <small>{narrationChars} 字</small> : null}
        </span>
        <Button
          variant="outline"
          disabled={
            readOnly ||
            review ||
            busy ||
            !draft.projectId ||
            !draft.script.text.trim()
          }
          onClick={() => void rewrite()}
        >
          <Icon name="sparkles" />
          {busy ? "AI 改写中…" : "AI 改写"}
        </Button>
      </div>
      <textarea
        ref={narrationRef}
        aria-label="口播文案"
        className="creation-textarea"
        rows={3}
        readOnly={readOnly}
        value={draft.script.text}
        onChange={(event) => {
          if (!readOnly)
            patchDraft({
              script: {
                ...draft.script,
                text: event.target.value,
                confirmed: false,
              },
              scriptEdited: true,
            });
        }}
        placeholder="完成拆解后，原视频口播文案会出现在这里。"
      />
      <div className="creation-panel-title-row">
        {message && <Hint>{message}</Hint>}
        <Button
          variant="primary"
          disabled={
            readOnly ||
            review ||
            busy ||
            !draft.projectId ||
            draft.script.confirmed
          }
          onClick={() =>
            patchDraft({ script: { ...draft.script, confirmed: true } })
          }
        >
          {draft.script.confirmed
            ? "已确认"
            : draft.script.text.trim()
              ? "确认"
              : "确认无口播"}
        </Button>
        {undo?.key === key && undo.after === draft.script.text && (
          <Button
            variant="quiet"
            disabled={readOnly || busy}
            onClick={() => {
              patchDraft({
                script: {
                  ...draft.script,
                  text: undo.before,
                  confirmed: false,
                },
                scriptEdited: true,
              });
              setUndo(null);
              setMessage("已撤销上次改写");
            }}
          >
            撤销上次改写
          </Button>
        )}
      </div>
    </Panel>
  );
}
