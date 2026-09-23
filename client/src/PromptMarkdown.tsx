import { useEffect, useRef, useState } from "react";
import type { PromptGenerationContext } from "./api";
import { PromptEditor } from "./studio/PromptEditor";

import "./prompt-markdown.css";

type PromptMarkdownProps = {
  scope?: string;
  meta?: string | null;
  promptContext?: PromptGenerationContext;
  onChange?: (text: string) => void;
  onSave?: (text: string) => Promise<void>;
  readOnly?: boolean;
  text: string;
};

type PromptBlock =
  | { kind: "shot"; range: string; text: string }
  | { kind: "paragraph"; text: string };

// 服务端模板的时间码行：`[0.0-2.5s] 近景，…`（start/end 始终一位小数，
// 正则放宽为数字串以兼容手工编辑后的文本）。
const SHOT_LINE_PATTERN = /^\[([\d.]+(?:\.\d+)?)-([\d.]+(?:\.\d+)?)s\]\s*(.+)$/;
const COPY_FEEDBACK_MS = 2000;

// 提示词 Markdown 视图 = 展示层结构化渲染（时间码/镜头行高亮分块），
// 复制与编辑始终作用于原始 H3 全文，提交文本不因渲染格式而改变。
export function PromptMarkdown({
  scope,
  meta,
  promptContext,
  onChange,
  onSave,
  readOnly = false,
  text,
}: PromptMarkdownProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [draft, setDraft] = useState(text);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState("");
  const [isCopied, setIsCopied] = useState(false);
  const copyTimerRef = useRef<number | undefined>(undefined);

  useEffect(() => {
    return () => {
      window.clearTimeout(copyTimerRef.current);
    };
  }, []);

  function startEditing() {
    setDraft(text);
    setError("");
    setIsEditing(true);
  }

  function cancelEditing() {
    setIsEditing(false);
    setDraft(text);
    setError("");
  }

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(text);
      setIsCopied(true);
      window.clearTimeout(copyTimerRef.current);
      copyTimerRef.current = window.setTimeout(() => {
        setIsCopied(false);
      }, COPY_FEEDBACK_MS);
    } catch {
      setError("复制失败，请手动选择文本复制。");
    }
  }

  async function handleSave() {
    if (!onSave) {
      return;
    }
    const next = draft.trim();
    if (!next) {
      setError("提示词不能为空。");
      return;
    }
    if (Array.from(next).length > 7000) {
      setError("提示词不能超过 7000 字。");
      return;
    }
    setIsSaving(true);
    setError("");
    try {
      await onSave(next);
      setIsEditing(false);
    } catch (saveError) {
      setError(
        saveError instanceof Error
          ? saveError.message
          : "保存提示词失败，请重试。",
      );
    } finally {
      setIsSaving(false);
    }
  }

  const blocks = parsePromptBlocks(text);
  const canEdit = Boolean(onSave) && !readOnly;

  return (
    <div className="prompt-md">
      <div className="prompt-md__toolbar">
        {meta ? <span className="prompt-md__meta">{meta}</span> : <span />}
        <div className="prompt-md__actions">
          <button
            className="secondary-button"
            onClick={() => void handleCopy()}
            type="button"
          >
            {isCopied ? "已复制" : "复制"}
          </button>
          {canEdit && !isEditing ? (
            <button
              className="secondary-button"
              onClick={startEditing}
              type="button"
            >
              编辑
            </button>
          ) : null}
        </div>
      </div>
      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}
      {isEditing ? (
        <div className="prompt-md__editor">
          <PromptEditor
            label="提示词源码"
            value={draft}
            readOnly={isSaving || readOnly}
            scope={scope ?? promptContext?.project_id ?? "legacy"}
            context={
              promptContext ?? { route: "text_image", duration_seconds: 15 }
            }
            optimizationDisabled={!promptContext}
            onChange={(text) => {
              setDraft(text);
              onChange?.(text);
            }}
          />
          <div className="prompt-md__editor-actions">
            <button
              disabled={isSaving}
              onClick={() => void handleSave()}
              type="button"
            >
              {isSaving ? "正在保存" : "另存到我的提示词"}
            </button>
            <button
              className="secondary-button"
              disabled={isSaving}
              onClick={cancelEditing}
              type="button"
            >
              {onChange ? "完成编辑" : "取消"}
            </button>
          </div>
          <p className="status-note">另存到我的提示词，原始拆解内容保留。</p>
        </div>
      ) : (
        <div className="prompt-md__preview">
          {blocks.map((block, index) =>
            block.kind === "shot" ? (
              // biome-ignore lint/suspicious/noArrayIndexKey: 每次渲染由文本重新解析的纯展示列表，无重排/增删，顺序即内容。
              <p className="prompt-md__shot" key={index}>
                <span className="prompt-md__timecode">{block.range}</span>
                <span>{block.text}</span>
              </p>
            ) : (
              // biome-ignore lint/suspicious/noArrayIndexKey: 每次渲染由文本重新解析的纯展示列表，无重排/增删，顺序即内容。
              <p className="prompt-md__paragraph" key={index}>
                {block.text}
              </p>
            ),
          )}
        </div>
      )}
    </div>
  );
}

function parsePromptBlocks(text: string): PromptBlock[] {
  return text
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line): PromptBlock => {
      const shot = SHOT_LINE_PATTERN.exec(line);
      if (shot) {
        return {
          kind: "shot",
          range: `${shot[1]}-${shot[2]}s`,
          text: shot[3],
        };
      }
      return { kind: "paragraph", text: line };
    });
}
