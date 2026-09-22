import { useEffect, useRef, useState } from "react";
import {
  capturePromptSession,
  customerVisibleErrorMessage,
  rewriteProjectScript,
  waitForScriptRewriteTask,
} from "../api";
import { useStudio } from "./context";
import {
  clearScriptRewriteIdempotencyKey,
  scriptRewriteIdempotencyKey,
  shouldClearScriptRewriteIdempotencyKey,
} from "./scriptRewrite";
import { Button, Hint, Icon, Panel } from "./ui";

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
    const requestKey = scriptRewriteIdempotencyKey(scope);
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
      const result = await waitForScriptRewriteTask(task.id);
      if (
        result.status !== "SUCCEEDED" ||
        !result.result?.rewritten_text?.trim()
      )
        throw new Error(
          result.error_message || "AI 改写未完成，当前文案已保留。",
        );
      clearScriptRewriteIdempotencyKey(scope, requestKey);
      if (!current()) return;
      if (latest.current.revision !== started.revision) {
        setMessage("文案已修改，迟到的改写结果未覆盖当前内容。");
        return;
      }
      const text = result.result.rewritten_text;
      setUndo({ key, before: started.text, after: text });
      latest.current.patchDraft({
        script: { ...latest.current.draft.script, text, confirmed: false },
        scriptEdited: true,
      });
      setMessage("AI 改写已完成，可继续编辑");
    } catch (cause) {
      if (shouldClearScriptRewriteIdempotencyKey(cause))
        clearScriptRewriteIdempotencyKey(scope, requestKey);
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
          {draft.script.confirmed ? "已确认" : "确认"}
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
