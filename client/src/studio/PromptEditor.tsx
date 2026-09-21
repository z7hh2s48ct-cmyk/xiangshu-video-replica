import { type ReactNode, useEffect, useRef, useState } from "react";
import {
  compileGenerationPrompt,
  createScriptVersion,
  type GenerationRatio,
  type GenerationVersion,
  getLatestGenerationPrompt,
  getLatestProjectShotCards,
  type PromptGenerationContext,
} from "../api";
import { anchorReplicaPromptToFirstFrame } from "./promptIdentity";
import { Icon } from "./ui";
import { usePromptOptimization } from "./usePromptOptimization";
import "./prompt-editor.css";

export type FinalReplicaSnapshot = {
  promptVersion?: GenerationVersion;
  inputKey: string;
  versionId: string;
  scriptVersionId: string;
  shotCardVersionId: string;
};

export type ReplicaPreflightCheck = {
  id: string;
  label: string;
  passed: boolean;
  reason: string;
  blocking?: boolean;
};

export function ReplicaPreflightChecklist({
  checks,
}: {
  checks: ReplicaPreflightCheck[];
}) {
  const blocking = checks.filter(
    (check) => !check.passed && check.blocking !== false,
  ).length;
  const warnings = checks.filter(
    (check) => !check.passed && check.blocking === false,
  ).length;
  return (
    <section className="replica-preflight" aria-label="生成前检查">
      <div className="replica-preflight__summary" aria-live="polite">
        <strong>生成前检查</strong>
        <span>
          {blocking
            ? `还需完成 ${blocking} 项${warnings ? ` · ${warnings} 项建议` : ""}`
            : warnings
              ? `可继续 · ${warnings} 项建议`
              : "全部通过"}
        </span>
      </div>
      <ul>
        {checks.map((check) => (
          <li
            className={
              check.passed
                ? "is-passed"
                : check.blocking === false
                  ? "is-warning"
                  : "is-blocked"
            }
            key={check.id}
          >
            <span aria-hidden="true">{check.passed ? "✓" : "!"}</span>
            <span>
              <strong>{check.label}</strong>
              <small>{check.passed ? "已完成" : check.reason}</small>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function replicaInputKey(input: {
  projectId: string;
  scriptText: string;
  firstFrameAssetId: string;
  duration: number;
  resolution: "768P" | "2K";
  ratio: GenerationRatio;
  shotCardVersionId?: string;
}) {
  return JSON.stringify(input);
}

/** Shared final step for all replica entry points; compilation is always explicit. */
export function ReplicaFinalPromptControls({
  input,
  sourceDuration = 0,
  sourceFrameTimestamp,
  showScriptPreview = true,
  scriptConfirmed,
  value,
  onChange,
  snapshot,
  onPrepared,
  readOnly = false,
  restoreEnabled = true,
  upstreamChecks = [],
}: {
  input: Parameters<typeof replicaInputKey>[0];
  sourceDuration?: number;
  sourceFrameTimestamp?: number;
  showScriptPreview?: boolean;
  scriptConfirmed?: boolean;
  value: string;
  onChange: (text: string) => void;
  snapshot: FinalReplicaSnapshot | null;
  onPrepared: (snapshot: FinalReplicaSnapshot | null) => void;
  readOnly?: boolean;
  restoreEnabled?: boolean;
  upstreamChecks?: ReplicaPreflightCheck[];
}) {
  const key = replicaInputKey(input);
  const [confirmedKey, setConfirmedKey] = useState("");
  const confirmed = scriptConfirmed ?? confirmedKey === key;
  const [scale, setScale] = useState(false);
  const [openingAction, setOpeningAction] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState<{
    text: string;
    snapshot: FinalReplicaSnapshot;
  } | null>(null);
  const current = useRef({ key, value });
  current.current = { key, value };
  const operation = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  // 服务端是门禁的唯一裁判。它拒绝后把对应的补救控件显示出来，否则前端算出的源时长
  // 或时间戳与服务端一分叉，用户就只能看到一句报错、找不到修正入口。判定记在当时的
  // inputKey 上，换首帧或时长后自动失效，与 confirmedKey / snapshot 的口径一致。
  const [alignmentConflictKey, setAlignmentConflictKey] = useState("");
  const [compressionConflictKey, setCompressionConflictKey] = useState("");
  // 与父组件 finalReady 同源：快照在、正文被清空时不算就绪，
  // 「已就绪 / 待合成」不能与下游按钮的口径脱节。
  const ready = Boolean(value.trim()) && snapshot?.inputKey === key;
  const requiresCompression =
    compressionConflictKey === key || sourceDuration > input.duration + 0.25;
  const extendsEnding =
    sourceDuration > 0 && sourceDuration < input.duration - 0.25;
  // 服务端把「未知时间戳」(-1) 与「中段帧」(>0.25) 一起判为必须填开场衔接。草稿里的
  // 时间戳没有恢复路径，未知是常态；把未知当成 0（视频开头）会在该必填时藏起输入框。
  const requiresOpeningAction =
    alignmentConflictKey === key ||
    sourceFrameTimestamp === undefined ||
    sourceFrameTimestamp < 0 ||
    sourceFrameTimestamp > 0.25;
  const preflightChecks: ReplicaPreflightCheck[] = [
    ...upstreamChecks,
    {
      id: "project",
      label: "来源视频",
      passed: Boolean(input.projectId),
      reason: "请先上传或选择来源视频。",
    },
    {
      id: "shot-version",
      label: "拆解分镜",
      passed: Boolean(input.shotCardVersionId),
      reason: "请先完成视频拆解并保存有效分镜。",
    },
    {
      id: "first-frame",
      label: "首帧选择",
      passed: Boolean(input.firstFrameAssetId),
      reason: "请先完成首帧置换并选定图片。",
    },
    {
      id: "script-confirmed",
      label: "口播文案",
      passed: confirmed,
      blocking: false,
      reason: input.scriptText.trim()
        ? "请在口播文案区域点击“确认”。"
        : "请确认本视频无口播。",
    },
    // 口播字数不再设门禁（服务端同步取消）。长度与时长的匹配由服务端编译时
    // 把文案按镜头切分到时间轴上承担，而不是在入口拒绝用户的文案。
    {
      id: "timeline",
      label: "视频时长",
      passed: !requiresCompression || scale,
      // 这里的 input.duration 是目标输出时长，不是系统上限。旧文案「源视频长于 N 秒」
      // 会让人以为 N 是个固定限制，必须把两个时长都写出来才说得清要调什么。
      reason: `源视频 ${sourceDuration.toFixed(1)} 秒长于目标时长 ${input.duration} 秒，请调整时长或确认压缩时间线。`,
    },
    {
      id: "opening-action",
      label: "开场衔接",
      passed: !requiresOpeningAction || Boolean(openingAction.trim()),
      reason:
        sourceFrameTimestamp === undefined || sourceFrameTimestamp < 0
          ? "首帧时间点未知，请说明如何从该画面开始。"
          : "所选首帧不是视频开头，请填写开场衔接。",
    },
  ];
  const blockingChecks = preflightChecks.filter(
    (check) => !check.passed && check.blocking !== false,
  );
  const preparedCallback = useRef(onPrepared);
  preparedCallback.current = onPrepared;
  useEffect(() => {
    let active = true;
    const revision = operation.current;
    if (!restoreEnabled || !input.projectId || !input.firstFrameAssetId) return;
    void Promise.resolve()
      .then(() => getLatestGenerationPrompt(input.projectId))
      .then((state) => {
        const version = state.version;
        const payload = version?.payload;
        if (
          !active ||
          revision !== operation.current ||
          !version ||
          state.stale ||
          !payload?.final_composition
        )
          return;
        if (
          payload.confirmed_script_text !== input.scriptText.trim() ||
          payload.first_frame_asset_id !== input.firstFrameAssetId ||
          payload.output_duration_seconds !== input.duration ||
          payload.resolution !== input.resolution ||
          payload.ratio !== input.ratio ||
          (input.shotCardVersionId &&
            payload.shot_card_version_id !== input.shotCardVersionId)
        )
          return;
        setConfirmedKey(key);
        setScale(payload.timeline_policy === "scale_confirmed");
        setOpeningAction(String(payload.opening_action ?? ""));
        preparedCallback.current({
          inputKey: key,
          versionId: version.id,
          promptVersion: version,
          scriptVersionId: String(payload.script_version_id),
          shotCardVersionId: String(payload.shot_card_version_id),
        });
      })
      .catch((error: unknown) => {
        if (!active || revision !== operation.current) return;
        setMessage(
          error instanceof Error
            ? `历史终稿读取失败：${error.message}`
            : "历史终稿读取失败，可重新合成。",
        );
      });
    return () => {
      active = false;
    };
  }, [
    key,
    input.projectId,
    input.firstFrameAssetId,
    input.scriptText,
    input.duration,
    input.resolution,
    input.ratio,
    input.shotCardVersionId,
    restoreEnabled,
  ]);
  async function compose() {
    if (busy || readOnly || blockingChecks.length > 0) return;
    operation.current += 1;
    setPending(null);
    setBusy(true);
    onPrepared(null);
    setMessage("");
    const start = { key, value };
    try {
      const shots =
        input.shotCardVersionId ||
        (await getLatestProjectShotCards(input.projectId))?.id;
      if (!shots) throw new Error("请先完成视频拆解和分镜准备。");
      if (!mounted.current || current.current.key !== start.key) return;
      const script = await createScriptVersion(input.projectId, {
        source: input.scriptText.trim() ? "custom" : "no_narration",
        text: input.scriptText,
        shot_card_version_id: shots,
      });
      if (!mounted.current || current.current.key !== start.key) return;
      const result = await compileGenerationPrompt(input.projectId, {
        script_version_id: script.id,
        shot_card_version_id: shots,
        first_frame_asset_id: input.firstFrameAssetId,
        output_duration_seconds: input.duration,
        resolution: input.resolution,
        ratio: input.ratio,
        timeline_policy: scale ? "scale_confirmed" : "preserve",
        opening_action: openingAction,
      });
      if (!mounted.current || current.current.key !== start.key) return;
      const text = anchorReplicaPromptToFirstFrame(
        String(result.payload.prompt_text ?? ""),
      );
      const prepared = {
        inputKey: key,
        versionId: result.id,
        promptVersion: result,
        scriptVersionId: script.id,
        shotCardVersionId: shots,
      };
      if (current.current.value !== start.value || start.value.trim()) {
        setPending({ text, snapshot: prepared });
        onPrepared(null);
        setMessage("新稿已就绪，当前正文保留；核对后采用新稿。");
      } else {
        onChange(text);
        onPrepared(prepared);
        setMessage("最终提示词已合成，请核对正文、首帧和费用后提交。");
      }
    } catch (error) {
      if (mounted.current) {
        const code = (error as { code?: string } | null)?.code;
        if (code === "FIRST_FRAME_ALIGNMENT_REQUIRED")
          setAlignmentConflictKey(start.key);
        if (code === "TIMELINE_CONFIRMATION_REQUIRED")
          setCompressionConflictKey(start.key);
        setMessage(error instanceof Error ? error.message : "最终合成失败");
      }
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  return (
    <section className="replica-final-controls" aria-label="最终提示词合成">
      <ReplicaPreflightChecklist checks={preflightChecks} />
      {showScriptPreview ? <pre>{input.scriptText || "无口播"}</pre> : null}
      {scriptConfirmed === undefined && (
        <label>
          <input
            type="checkbox"
            disabled={readOnly || busy}
            checked={confirmedKey === key}
            onChange={(event) => {
              setConfirmedKey(event.target.checked ? key : "");
              if (!event.target.checked) {
                operation.current += 1;
                onPrepared(null);
              }
            }}
          />
          {input.scriptText.trim() ? "采用这份文案" : "本视频无口播"}
        </label>
      )}
      {requiresCompression ? (
        <label>
          <input
            type="checkbox"
            disabled={readOnly || busy}
            checked={scale}
            onChange={(event) => {
              operation.current += 1;
              setPending(null);
              setScale(event.target.checked);
              onPrepared(null);
            }}
          />
          {/* 服务端判定需要压缩、而前端算不出源时长时，不能显示「将 0.0 秒压缩到」。 */}
          {sourceDuration > input.duration
            ? `将 ${sourceDuration.toFixed(1)} 秒内容压缩到 ${input.duration} 秒`
            : `确认将源视频内容压缩到 ${input.duration} 秒`}
        </label>
      ) : null}
      {extendsEnding ? <p>按目标时长放慢节奏。</p> : null}
      {requiresOpeningAction ? (
        <label>
          开场衔接
          <textarea
            value={openingAction}
            disabled={readOnly || busy}
            placeholder="例如：以当前首帧为起点，人物保持现有姿态，镜头缓慢推进，随后自然衔接到原视频的第一个动作。"
            onChange={(event) => {
              operation.current += 1;
              setPending(null);
              setOpeningAction(event.target.value);
              onPrepared(null);
            }}
          />
        </label>
      ) : (
        <details>
          <summary>高级设置</summary>
          <label>
            开场衔接（可选）
            <textarea
              value={openingAction}
              disabled={readOnly || busy}
              placeholder="例如：人物从首帧姿态自然起步，镜头跟随并衔接到原视频动作。"
              onChange={(event) => {
                operation.current += 1;
                setPending(null);
                setOpeningAction(event.target.value);
                onPrepared(null);
              }}
            />
          </label>
        </details>
      )}
      <button
        type="button"
        className="replica-final-compose"
        disabled={readOnly || busy || blockingChecks.length > 0}
        onClick={() => void compose()}
      >
        {busy ? "正在合成…" : ready ? "重新合成" : "合成最终提示词"}
      </button>
      <p role="status">
        {ready ? "已就绪" : "待合成"}。{message}
      </p>
      {pending && (
        <details open>
          <summary>核对新稿（当前编辑保留）</summary>
          <pre>{pending.text}</pre>
          <button
            type="button"
            disabled={pending.snapshot.inputKey !== key || readOnly}
            onClick={() => {
              onChange(pending.text);
              onPrepared(pending.snapshot);
              setPending(null);
            }}
          >
            采用这份最终稿
          </button>
        </details>
      )}
    </section>
  );
}

type Props = {
  value: string;
  onChange: (value: string) => void;
  context: PromptGenerationContext;
  scope: string;
  label?: string;
  placeholder?: string;
  readOnly?: boolean;
  optimizationDisabled?: boolean;
  optimizationActionLabel?: string;
  rows?: number;
  toolbarStart?: ReactNode;
  showToolbarLabel?: boolean;
  toolbarLabel?: ReactNode;
};

export function PromptEditor({
  value,
  onChange,
  context,
  scope,
  label = "提示词",
  placeholder,
  readOnly = false,
  optimizationDisabled = false,
  optimizationActionLabel = "AI 优化提示词",
  rows = 8,
  toolbarStart,
  showToolbarLabel = false,
  toolbarLabel = "画面描述",
}: Props) {
  const optimization = usePromptOptimization(value, context, onChange, scope);
  const count = Array.from(value).length;
  return (
    <div className="h3-prompt-editor">
      <div className="h3-prompt-tools">
        {showToolbarLabel && (
          <span className="h3-prompt-label">{toolbarLabel}</span>
        )}
        {toolbarStart}
        {optimization.canUndo && !readOnly && (
          <button type="button" onClick={optimization.undo}>
            撤销
          </button>
        )}
        <button
          type="button"
          aria-label="AI 优化提示词"
          title="按当前模式优化提示词"
          aria-busy={optimization.busy}
          disabled={
            readOnly ||
            optimizationDisabled ||
            optimization.busy ||
            !value.trim() ||
            count > 7000
          }
          onClick={() => void optimization.run()}
        >
          <Icon name={optimization.busy ? "refresh" : "sparkles"} size={16} />
          <span>
            {optimization.busy ? "正在优化…" : optimizationActionLabel}
          </span>
        </button>
      </div>
      {optimization.message && <p role="status">{optimization.message}</p>}
      <textarea
        aria-label={label}
        className="creation-textarea"
        value={value}
        rows={rows * 2}
        readOnly={readOnly}
        disabled={readOnly}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
      <small>{count}/7000 字</small>
      {count > 7000 && <p role="alert">提示词超过 7000 字，请精简后提交。</p>}
      {optimization.pending && (
        <details>
          <summary>查看基于旧内容的优化结果</summary>
          <pre>{optimization.pending.text}</pre>
          <button
            type="button"
            disabled={readOnly}
            onClick={optimization.applyPending}
          >
            应用此结果
          </button>
        </details>
      )}
    </div>
  );
}
