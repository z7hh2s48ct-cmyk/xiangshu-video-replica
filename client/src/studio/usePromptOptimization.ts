import { useEffect, useRef, useState } from "react";
import {
  capturePromptSession,
  createPromptOptimization,
  customerVisibleErrorMessage,
  getPromptOptimization,
  type PromptGenerationContext,
  type PromptOptimizeInput,
  type PromptOptimizeResult,
} from "../api";
import { constrainReferenceVideoPrompt } from "./referencePrompt";

type AppliedOptimization = {
  context: PromptGenerationContext;
  text: string;
  optimization_task_id: string;
  context_hash: string;
};
const applied = new Map<string, AppliedOptimization>();
export function readAppliedOptimization(
  scope: string,
  text: string,
  source?: { script_version_id?: string; shot_card_version_id?: string },
) {
  let receipt = applied.get(scope);
  if (!receipt) {
    try {
      receipt = JSON.parse(
        localStorage.getItem(`h3.applied:${scope}`) || "null",
      );
    } catch {
      /* unavailable storage */
    }
  }
  if (
    source &&
    Object.entries(source).some(
      ([key, id]) =>
        id !== undefined &&
        receipt?.context?.[key as keyof PromptGenerationContext] !== id,
    )
  )
    return {};
  return receipt?.text === text &&
    typeof receipt.optimization_task_id === "string" &&
    typeof receipt.context_hash === "string"
    ? {
        source: "ai" as const,
        optimization_task_id: receipt.optimization_task_id,
        context_hash: receipt.context_hash,
      }
    : {};
}

export function usePromptOptimization(
  value: string,
  context: PromptGenerationContext,
  onChange: (text: string) => void,
  scope: string,
) {
  const key = JSON.stringify([scope, context]);
  const latest = useRef({ value, key, revision: 0, onChange });
  if (latest.current.value !== value || latest.current.key !== key) {
    latest.current = {
      value,
      key,
      revision: latest.current.revision + 1,
      onChange,
    };
  } else latest.current.onChange = onChange;
  const storageKey = `h3-optimization:${key}`;
  type Recovery = { input: PromptOptimizeInput; taskId?: string };
  const saveRecovery = (item: Recovery | null) => {
    try {
      if (item) localStorage.setItem(storageKey, JSON.stringify(item));
      else localStorage.removeItem(storageKey);
    } catch {
      /* In-memory request still protects a retry when storage is unavailable. */
    }
  };
  const recovery = useRef<Recovery | null>(null);
  const operation = useRef(0);
  const active = useRef(false);
  const mounted = useRef(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState<{
    text: string;
    receipt: AppliedOptimization;
    key: string;
    sessionCurrent: () => boolean;
  } | null>(null);
  const [undo, setUndo] = useState<{
    before: string;
    after: string;
    key: string;
    sessionCurrent: () => boolean;
  } | null>(null);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      operation.current += 1;
    };
  }, []);
  useEffect(() => {
    operation.current += 1;
    active.current = false;
    setBusy(false);
    try {
      recovery.current = JSON.parse(localStorage.getItem(storageKey) || "null");
    } catch {
      recovery.current = null;
    }
    setMessage(
      recovery.current
        ? "有未完成的优化任务，点击 AI 图标继续查询，不会重复提交。"
        : "",
    );
    setPending(null);
    setUndo(null);
  }, [storageKey]);

  const apply = (text: string, receipt: AppliedOptimization) => {
    applied.set(scope, receipt);
    try {
      localStorage.setItem(`h3.applied:${scope}`, JSON.stringify(receipt));
    } catch {
      /* memory fallback */
    }
    setUndo({
      before: latest.current.value,
      after: text,
      key: latest.current.key,
      sessionCurrent: capturePromptSession(),
    });
    latest.current.onChange(text);
    setPending(null);
    setMessage("已按 H3 格式优化");
  };
  const run = async () => {
    if (active.current || !value.trim()) return;
    active.current = true;
    setBusy(true);
    setMessage("正在提交优化任务，请稍候…");
    const id = ++operation.current;
    const started = { ...latest.current };
    const sessionCurrent = capturePromptSession();
    const current = () =>
      mounted.current &&
      operation.current === id &&
      latest.current.key === started.key &&
      sessionCurrent();
    try {
      const saved = recovery.current ?? {
        input: {
          ...context,
          prompt_text: value,
          editor_revision: started.revision,
          idempotency_key: crypto.randomUUID(),
        },
      };
      recovery.current = saved;
      saveRecovery(saved);
      let result: PromptOptimizeResult = saved.taskId
        ? await getPromptOptimization(saved.taskId)
        : await createPromptOptimization(saved.input);
      saved.taskId = result.task_id;
      if (!current()) return;
      setMessage(
        result.status === "RUNNING"
          ? "AI 正在按当前素材重写提示词…"
          : result.status === "PENDING"
            ? "优化任务已排队，请稍候…"
            : "",
      );
      saveRecovery(saved);
      const deadline = Date.now() + 12 * 60 * 1000;
      while (
        current() &&
        (result.status === "PENDING" || result.status === "RUNNING")
      ) {
        if (Date.now() > deadline)
          throw new Error("任务仍在处理中，原文已保留；请稍后查询任务结果。");
        await new Promise((resolve) => setTimeout(resolve, 1500));
        if (!current()) return;
        result = await getPromptOptimization(result.task_id);
        if (current())
          setMessage(
            result.status === "RUNNING"
              ? "AI 正在按当前素材重写提示词…"
              : result.status === "PENDING"
                ? "优化任务已排队，请稍候…"
                : "",
          );
      }
      if (!current()) return;
      if (result.status !== "SUBMISSION_UNCERTAIN") {
        recovery.current = null;
        saveRecovery(null);
      }
      if (
        result.status !== "SUCCEEDED" ||
        !result.result?.prompt_text ||
        result.result.validation_status !== "valid"
      ) {
        setMessage(
          result.error_message ||
            result.result?.warnings.map((item) => item.message).join("；") ||
            "优化未完成，原文已保留。",
        );
        return;
      }
      const optimizedText =
        context.route === "reference"
          ? constrainReferenceVideoPrompt(result.result.prompt_text)
          : result.result.prompt_text;
      const receipt = {
        context,
        text: optimizedText,
        optimization_task_id: result.task_id,
        context_hash: result.context_hash,
      };
      if (
        latest.current.revision !== started.revision ||
        latest.current.value !== saved.input.prompt_text
      ) {
        setPending({
          text: optimizedText,
          receipt,
          key: started.key,
          sessionCurrent,
        });
        setMessage("你已修改内容，优化结果未覆盖当前文字。");
      } else apply(optimizedText, receipt);
    } catch (error) {
      const status = (error as { status?: number })?.status;
      if (
        current() &&
        !recovery.current?.taskId &&
        status !== undefined &&
        [400, 402, 422].includes(status)
      ) {
        recovery.current = null;
        saveRecovery(null);
      }
      if (current())
        setMessage(
          customerVisibleErrorMessage(error, "优化失败，原文已保留。"),
        );
    } finally {
      if (mounted.current && operation.current === id) {
        active.current = false;
        setBusy(false);
      }
    }
  };
  return {
    busy,
    message:
      message === "已按 H3 格式优化" && undo?.after !== value
        ? "已编辑，请核对当前内容。"
        : message,
    run,
    pending: pending?.key === key && pending.sessionCurrent() ? pending : null,
    applyPending: () => {
      if (pending?.key === latest.current.key && pending.sessionCurrent())
        apply(pending.text, pending.receipt);
    },
    canUndo:
      undo !== null &&
      undo.key === key &&
      undo.after === value &&
      undo.sessionCurrent(),
    undo: () => {
      if (
        undo &&
        undo.key === latest.current.key &&
        undo.after === latest.current.value &&
        undo.sessionCurrent()
      ) {
        latest.current.onChange(undo.before);
        setUndo(null);
        setMessage("");
      }
    },
  };
}
