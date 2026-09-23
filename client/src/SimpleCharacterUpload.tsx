import { useRef, useState } from "react";

import { type SimpleCharacterResult, uploadSimpleCharacter } from "./api";
import { StudioDialog } from "./studio/ui";

const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const ALLOWED_TYPES = ["image/png", "image/jpeg", "image/webp"];

export function SimpleCharacterUpload({
  onCreated,
  onGenerationFailed,
  onGenerationProgress,
  onGenerationStarted,
  projectId = null,
}: {
  onCreated: (result: SimpleCharacterResult, displayName: string) => void;
  onGenerationFailed?: (message: string) => void;
  onGenerationProgress?: (progress: number, stage: string) => void;
  onGenerationStarted?: (file: File, displayName: string) => void;
  projectId?: string | null;
}) {
  const [busy, setBusy] = useState(false);
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState("");
  const [fileName, setFileName] = useState("");
  const [message, setMessage] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [authorization, setAuthorization] = useState<{
    file: File;
    name: string;
  }>();
  const [authorizationAccepted, setAuthorizationAccepted] = useState(false);
  const submittingRef = useRef(false);

  function pickFile(file: File | undefined) {
    setAuthorization(undefined);
    setAuthorizationAccepted(false);
    setError("");
    setMessage("");
    if (!file) {
      setFileName("");
      return;
    }
    if (!ALLOWED_TYPES.includes(file.type)) {
      setFileName("");
      setError("仅支持 PNG、JPEG 或 WebP 图片。");
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setFileName("");
      setError("图片超过 10MB 限制。");
      return;
    }
    setFileName(file.name);
  }

  function submit() {
    const file = fileInputRef.current?.files?.[0];
    const name = displayName.trim();
    if (!file || !name) {
      setError("请选择图片并填写人物名称。");
      return;
    }
    if (!ALLOWED_TYPES.includes(file.type) || file.size > MAX_UPLOAD_BYTES) {
      setError("请选择不超过 10MB 的 PNG、JPEG 或 WebP 图片。");
      return;
    }
    setAuthorizationAccepted(false);
    setAuthorization({ file, name });
  }

  async function confirmAndSubmit() {
    if (!authorization || !authorizationAccepted || submittingRef.current)
      return;
    const { file, name } = authorization;
    submittingRef.current = true;
    setAuthorization(undefined);
    setAuthorizationAccepted(false);
    setBusy(true);
    setError("");
    setMessage("");
    onGenerationStarted?.(file, name);
    let progress = 8;
    onGenerationProgress?.(progress, "正在上传授权图片");
    const progressTimer = window.setInterval(() => {
      progress = Math.min(
        92,
        progress + (progress < 56 ? 4 : progress < 78 ? 2 : 1),
      );
      const stage =
        progress < 28
          ? "正在上传授权图片"
          : progress < 58
            ? "正在分析人物特征"
            : "正在生成多视角拼合图";
      onGenerationProgress?.(progress, stage);
    }, 3_000);
    try {
      const result = await uploadSimpleCharacter(
        projectId,
        file,
        name,
        "",
        "2026-09-14-v1",
      );
      onGenerationProgress?.(100, "多视角拼合图已生成");
      setMessage(`人物“${name}”五视图拼合图已生成，可在下方预览与下载。`);
      setDisplayName("");
      setFileName("");
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
      onCreated(result, name);
    } catch (requestError) {
      const requestMessage =
        requestError instanceof Error
          ? requestError.message
          : "一键创建人物失败，请重试。";
      setError(requestMessage);
      onGenerationFailed?.(requestMessage);
    } finally {
      window.clearInterval(progressTimer);
      setBusy(false);
      submittingRef.current = false;
    }
  }

  return (
    <section className="simple-character-upload" aria-label="一键上传人物">
      <div className="simple-character-upload__intro">
        <p className="simple-character-upload__title">
          上传人物图片，生成五视图拼合图
        </p>
        <p className="simple-character-upload__note">
          授权图片 PNG / JPEG / WebP，不超过 10MB · AI 绘制约 1~3 分钟
        </p>
      </div>
      <div className="simple-character-upload-form">
        <label>
          人物名称
          <input
            onChange={(event) => setDisplayName(event.target.value)}
            placeholder="例如：荣哥"
            type="text"
            value={displayName}
          />
        </label>
        <div className="simple-character-upload__picker">
          <input
            accept={ALLOWED_TYPES.join(",")}
            aria-label="授权图片"
            hidden
            onChange={(event) => pickFile(event.target.files?.[0])}
            ref={fileInputRef}
            type="file"
          />
          <button
            className="secondary-button"
            disabled={busy}
            onClick={() => fileInputRef.current?.click()}
            type="button"
          >
            选择图片
          </button>
          <span className="simple-character-upload__file">
            {fileName || "未选择图片"}
          </span>
        </div>
        <button
          className="simple-character-upload__submit"
          disabled={busy}
          onClick={submit}
          type="button"
        >
          {busy ? "正在生成拼合图（约 1~3 分钟）…" : "一键生成五视图拼合图"}
        </button>
      </div>
      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}
      {message ? <p className="setup-success">{message}</p> : null}
      {authorization ? (
        <StudioDialog
          title="人物图像使用授权"
          onClose={() => setAuthorization(undefined)}
        >
          <p>
            即将上传：{authorization.file.name}，用于生成“{authorization.name}
            ”的人物多视图。
          </p>
          <p>
            请确认图片为本人照片，或已获得照片中人物及其他相关权利人的明确授权，且授权范围包含上传、AI
            图像处理和人物多视图生成。
          </p>
          <p>
            确认后，平台将上传该图片并提交至已配置的图像生成服务进行处理，保存生成结果及本次授权记录。此授权不代替数字人分身或声音克隆的单独授权。
          </p>
          <p>
            上传者应确保图片来源和授权真实、合法，不得上传未经授权的他人照片。因未经授权上传、虚假声明或超出授权范围使用图片造成的风险、侵权及纠纷，由上传责任人依法承担相应责任。
          </p>
          <label style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
            <input
              type="checkbox"
              style={{ width: "auto", flexShrink: 0, marginTop: 4 }}
              checked={authorizationAccepted}
              onChange={(event) =>
                setAuthorizationAccepted(event.target.checked)
              }
            />
            我已阅读并确认以上图像授权声明
          </label>
          <div className="dialog-actions">
            <button
              type="button"
              className="secondary-button"
              onClick={() => setAuthorization(undefined)}
            >
              取消上传
            </button>
            <button
              type="button"
              disabled={!authorizationAccepted}
              onClick={() => void confirmAndSubmit()}
            >
              确认授权并生成
            </button>
          </div>
        </StudioDialog>
      ) : null}
    </section>
  );
}
