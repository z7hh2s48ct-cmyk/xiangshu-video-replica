import { useState } from "react";
import { customerErrorHint } from "./customerErrorHint";
import { RetryButton } from "./RetryButton";

/**
 * 客户个人中心的错误块（审计 P1 清单 #8：错误分类 + RetryButton 统一）。
 *
 * 结构固定为三层：**分类提示**（该做什么）→ 平台给的原因 → 重试入口。
 * 分类提示来自 `customerErrorHint`，原来客户只看到一句技术原因，不知道该重试、
 * 该重新登录还是该找管理员。
 *
 * 一处刻意的取舍：只要调用方给了 `onRetry` 就渲染重试按钮，不用 `hint.retryable`
 * 去藏它——权限/会话类错误重试确实没用，但把按钮藏掉会让原本能点的用户以为界面
 * 坏了；提示语已经说清「重试没用」。这个 flag 仍留在提示函数里供后续使用。
 */
export function ErrorNote({
  error,
  message,
  onRetry,
  retryLabel,
}: {
  /** 原始错误对象，用来分类；缺省时退到通用提示。 */
  error?: unknown;
  /** 平台给的原因（或调用方整理的文案）。 */
  message: string;
  onRetry?: () => void;
  retryLabel?: string;
}) {
  const hint = customerErrorHint(error);
  const [copied, setCopied] = useState(false);
  /**
   * 复制错误信息给客服。
   *
   * 审计要的是「错误码 + 反馈入口」；错误码体系还没定（属产品口径），但反馈入口可以先
   * 落地——把分类、原因、以及平台给的问题编号（`CustomerApiError` 会把 requestId 拼在
   * message 里）一次性复制走，客服凭问题编号就能在审计日志里定位。
   */
  async function copyForSupport() {
    const text = [
      `【${hint.hint}】`,
      message,
      `时间：${new Date().toLocaleString("zh-CN")}`,
    ].join(" | ");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      // 剪贴板不可用（权限/非安全上下文）时不留假成功：交给用户手选。
      setCopied(false);
    }
  }
  return (
    <div role="alert" className="uc-error">
      <span className="uc-error__hint">{hint.hint}</span>
      <span>{message}</span>
      {onRetry ? <RetryButton label={retryLabel} onClick={onRetry} /> : null}
      <button
        className="uc-error__feedback"
        type="button"
        onClick={() => void copyForSupport()}
      >
        {copied ? "已复制，可发给客服" : "复制错误信息"}
      </button>
    </div>
  );
}
