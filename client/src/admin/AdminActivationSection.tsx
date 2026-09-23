import { type FormEvent, useState } from "react";

import {
  type ActivationBatchResult,
  type ActivationDownloadResult,
  type ActivationGenerateResult,
  AdminActivationError,
  type AdminActorInfo,
  adminActivationErrorMessage,
  createActivationCodeBatch,
  createIdempotencyKey,
  downloadActivationCodeExport,
  generateActivationCodes,
} from "../api.admin";
import { ActivationCodesPage } from "./ActivationCodesPage";
import { PageBanner } from "./ui/PageBanner";

const MAX_CODES_PER_REQUEST = 100;

type GenerationPhase = "idle" | "creating" | "generating" | "retrieving";

type CompletedGeneration = {
  quantity: number;
  result: ActivationDownloadResult;
};

function defaultExpiryDate(): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() + 1);
  return d.toISOString().slice(0, 10);
}

function expiryToIso(dateText: string): string {
  // 有效期取当日末尾（本地时区），与批次激活窗口语义一致。
  return new Date(`${dateText}T23:59:59`).toISOString();
}

function phaseLabel(phase: GenerationPhase, quantity: number): string {
  if (phase === "creating") return "正在准备激活码…";
  if (phase === "generating") return `正在生成 ${quantity} 个激活码…`;
  if (phase === "retrieving") return "正在取回激活码明文（仅此一次）…";
  return "生成激活码";
}

function phaseErrorFallback(phase: GenerationPhase): string {
  if (phase === "creating") return "准备激活码失败";
  if (phase === "generating") return "生成激活码失败";
  if (phase === "retrieving") return "取回激活码明文失败";
  return "生成激活码失败";
}

type CodeGeneratorFormProps = {
  readOnly: boolean;
  onGenerated: () => void;
  onSessionExpired: () => void;
};

/**
 * v4 定稿 — 创建即激活的生成表单。
 * 管理员填写数量、有效期、初始秒数与必填原因后一次提交：
 * 服务端同事务完成建码与发放（auto_issue），码即刻可激活并交付客户；
 * 明文仅在生成后一次性展示（下载留痕），系统内不存在独立的「发放」动作。
 */
function CodeGeneratorForm({
  readOnly,
  onGenerated,
  onSessionExpired,
}: CodeGeneratorFormProps) {
  const [quantity, setQuantity] = useState("1");
  const [expiresDate, setExpiresDate] = useState(defaultExpiryDate());
  const [credits, setCredits] = useState("0");
  const [confirmGrant, setConfirmGrant] = useState(false);
  const [reason, setReason] = useState("");
  const [phase, setPhase] = useState<GenerationPhase>("idle");
  const [error, setError] = useState("");
  const [copyNotice, setCopyNotice] = useState("");
  const [completed, setCompleted] = useState<CompletedGeneration | null>(null);
  const [pendingBatch, setPendingBatch] =
    useState<ActivationBatchResult | null>(null);
  const [pendingExport, setPendingExport] =
    useState<ActivationGenerateResult | null>(null);
  const [createKey, setCreateKey] = useState<string | null>(null);
  const [generateKey, setGenerateKey] = useState<string | null>(null);
  const [downloadKey, setDownloadKey] = useState<string | null>(null);

  const parsedQuantity = Number(quantity);
  const parsedCredits = Number(credits);
  const actionLabel = phaseLabel(phase, parsedQuantity);
  const locked =
    phase !== "idle" ||
    pendingBatch !== null ||
    pendingExport !== null ||
    createKey !== null ||
    generateKey !== null ||
    downloadKey !== null;

  function handleFailure(cause: unknown, fallback: string) {
    if (cause instanceof AdminActivationError && cause.status === 401) {
      setError("会话已失效，请重新登录");
      onSessionExpired();
      return;
    }
    setError(adminActivationErrorMessage(cause, fallback));
  }

  function validate(): string {
    if (
      !Number.isInteger(parsedQuantity) ||
      parsedQuantity < 1 ||
      parsedQuantity > MAX_CODES_PER_REQUEST
    ) {
      return `数量需为 1-${MAX_CODES_PER_REQUEST} 的整数`;
    }
    if (!expiresDate) {
      return "请选择有效期";
    }
    if (new Date(`${expiresDate}T23:59:59`).getTime() <= Date.now()) {
      return "有效期必须晚于今天";
    }
    if (
      !Number.isInteger(parsedCredits) ||
      parsedCredits < 0 ||
      parsedCredits > 2147483647
    ) {
      return "初始秒数需为不小于 0 的整数";
    }
    if (parsedCredits > 0 && !confirmGrant) {
      return "请确认初始秒数为免费赠送，不产生收款收入";
    }
    if (!reason.trim()) {
      return "请填写操作原因";
    }
    return "";
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setCopyNotice("");

    const validationError = validate();
    if (validationError) {
      setError(validationError);
      return;
    }

    const trimmedReason = reason.trim();
    let batch = pendingBatch;
    let generated = pendingExport;
    let currentPhase: GenerationPhase = "creating";

    try {
      if (!batch) {
        setPhase("creating");
        const key = createKey ?? createIdempotencyKey();
        setCreateKey(key);
        batch = await createActivationCodeBatch(
          {
            name: `激活码-${new Date()
              .toISOString()
              .slice(0, 16)
              .replace(/[-:T]/g, "")}`,
            face_value_fen: 0,
            credits: parsedCredits,
            quantity: parsedQuantity,
            activation_expires_at: expiryToIso(expiresDate),
            reason: trimmedReason,
            confirm_grant: parsedCredits > 0 && confirmGrant,
          },
          key,
        );
        setPendingBatch(batch);
        setCreateKey(null);
      }

      currentPhase = "generating";
      if (!generated) {
        setPhase("generating");
        const key = generateKey ?? createIdempotencyKey();
        setGenerateKey(key);
        // 创建即激活：auto_issue 让码在同事务内翻转为可使用，无发放动作。
        generated = await generateActivationCodes(
          batch.batch_id,
          parsedQuantity,
          trimmedReason,
          key,
          true,
        );
        setPendingExport(generated);
        setGenerateKey(null);
      }

      currentPhase = "retrieving";
      setPhase("retrieving");
      const key = downloadKey ?? createIdempotencyKey();
      setDownloadKey(key);
      const result = await downloadActivationCodeExport(
        generated.export_id,
        trimmedReason,
        key,
      );

      setCompleted({ quantity: parsedQuantity, result });
      setPendingBatch(null);
      setPendingExport(null);
      setCreateKey(null);
      setGenerateKey(null);
      setDownloadKey(null);
      setReason("");
      setConfirmGrant(false);
      onGenerated();
    } catch (cause) {
      handleFailure(cause, phaseErrorFallback(currentPhase));
      if (cause instanceof AdminActivationError && cause.status !== undefined) {
        setPendingBatch(null);
        setPendingExport(null);
        setCreateKey(null);
        setGenerateKey(null);
        setDownloadKey(null);
      }
    } finally {
      setPhase("idle");
    }
  }

  async function copyAllCodes() {
    if (!completed) {
      return;
    }
    try {
      await navigator.clipboard.writeText(completed.result.codes.join("\n"));
      setCopyNotice("全部激活码已复制");
    } catch {
      setCopyNotice("复制失败，请手动选择激活码");
    }
  }

  if (readOnly) {
    return (
      <section aria-label="生成激活码">
        <PageBanner tone="notice">当前为只读模式，不能生成激活码。</PageBanner>
      </section>
    );
  }

  return (
    <section aria-label="生成激活码">
      <div className="admin-panel">
        <h2>生成激活码</h2>
        <p className="admin-hint">
          创建即可激活并交付客户，无发放动作；明文仅在生成后一次性展示，下载与复制均记入审计。
        </p>
        <form className="admin-form" onSubmit={submit}>
          <label>
            数量（1-{MAX_CODES_PER_REQUEST}）
            <input
              autoComplete="off"
              disabled={locked}
              max={MAX_CODES_PER_REQUEST}
              min={1}
              type="number"
              value={quantity}
              onChange={(event) => setQuantity(event.target.value)}
            />
          </label>
          <label>
            有效期至
            <input
              disabled={locked}
              type="date"
              value={expiresDate}
              onChange={(event) => setExpiresDate(event.target.value)}
            />
          </label>
          <label>
            初始秒数（每个码）
            <input
              autoComplete="off"
              disabled={locked}
              min={0}
              type="number"
              value={credits}
              onChange={(event) => {
                setCredits(event.target.value);
                setConfirmGrant(false);
              }}
            />
          </label>
          <label>
            操作原因（必填）
            <input
              autoComplete="off"
              disabled={locked}
              type="text"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          {parsedCredits > 0 ? (
            <label className="admin-checkbox-label">
              <input
                type="checkbox"
                checked={confirmGrant}
                disabled={locked}
                onChange={(event) => setConfirmGrant(event.target.checked)}
              />
              确认免费赠送 {parsedCredits} 秒/码，未收款，不产生收款收入
            </label>
          ) : null}
          <button disabled={locked} type="submit">
            {locked ? actionLabel : "生成激活码"}
          </button>
        </form>
        {error ? <PageBanner tone="error">{error}</PageBanner> : null}
        {completed ? (
          <div className="activation-generator-result">
            <PageBanner tone="notice">
              已生成 {completed.quantity}{" "}
              个激活码，明文仅此一次展示，请立即交付客户。
            </PageBanner>
            <pre>{completed.result.codes.join("\n")}</pre>
            <button type="button" onClick={() => void copyAllCodes()}>
              复制全部
            </button>
            {copyNotice ? <span>{copyNotice}</span> : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}

type AdminActivationSectionProps = {
  actor: AdminActorInfo;
  onSessionExpired: () => void;
  showGenerator?: boolean;
};

/**
 * v4 导航合并 — 客户管理「激活码」页签。
 * 生成表单（创建即激活）与激活码列表同屏；不再有批次列表与发放登记。
 */
export function AdminActivationSection({
  actor,
  onSessionExpired,
  showGenerator = true,
}: AdminActivationSectionProps) {
  const [refreshToken, setRefreshToken] = useState(0);
  const readOnly = actor.role === "auditor";

  return (
    <section
      className="admin-panel activation-management"
      aria-label="激活码管理"
    >
      {readOnly ? (
        <PageBanner tone="notice">
          审计员只读：仅可查看，不能执行写操作。
        </PageBanner>
      ) : null}

      {showGenerator ? (
        <CodeGeneratorForm
          readOnly={readOnly}
          onGenerated={() => setRefreshToken((current) => current + 1)}
          onSessionExpired={onSessionExpired}
        />
      ) : null}
      <ActivationCodesPage
        readOnly={readOnly}
        refreshToken={refreshToken}
        onSessionExpired={onSessionExpired}
      />
    </section>
  );
}
