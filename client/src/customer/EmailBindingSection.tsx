import { type FormEvent, useEffect, useRef, useState } from "react";
import {
  CustomerApiError,
  type CustomerEmailState,
  type CustomerSessionCredential,
  customerEmailState,
  customerSendEmailBindCode,
  customerVerifyEmailBindCode,
  customerVisibleErrorMessage,
} from "../api";
import { Icon } from "../studio/ui";
import { RetryButton } from "./RetryButton";

const CODE_PATTERN = /^\d{6}$/;

const timeLabel = (value: string) =>
  new Date(value).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false,
  });

/**
 * 账号设置里的「绑定邮箱」。绑定的意义是留一条不依赖密码的回家路：忘记密码
 * 时验证码发到这里。未验证的地址只活在验证码行里，所以「已绑定」的地址永远
 * 是证明过归属的地址。
 *
 * 三种不该发码的情形都直接说明原因，不摆一个注定失败的按钮：发信未开通
 * （service_available）、子账号（can_bind）、以及表单里的重复地址（409 由
 * 服务端文案转述）。
 */
export function EmailBindingSection({
  credential,
}: {
  credential: () => Promise<CustomerSessionCredential>;
}) {
  const [state, setState] = useState<CustomerEmailState | null>(null);
  const [loadError, setLoadError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [mode, setMode] = useState<"idle" | "email" | "code">("idle");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [cooldown, setCooldown] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);

  // biome-ignore lint/correctness/useExhaustiveDependencies: Refresh 明确用于在读取失败后重新拉取状态。
  useEffect(() => {
    let active = true;
    setLoadError("");
    void credential()
      .then((auth) => customerEmailState(auth))
      .then((data) => {
        if (active) setState(data);
      })
      .catch((cause: unknown) => {
        if (active) {
          setLoadError(
            cause instanceof Error ? cause.message : "读取绑定邮箱失败。",
          );
        }
      });
    return () => {
      active = false;
    };
  }, [credential, refresh]);

  // 重发冷却的秒数来自服务端（resend_after_seconds / Retry-After），前端只管倒数。
  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = window.setTimeout(
      () => setCooldown((value) => value - 1),
      1000,
    );
    return () => window.clearTimeout(timer);
  }, [cooldown]);

  function captureError(cause: unknown, fallback: string) {
    if (cause instanceof CustomerApiError && cause.retryAfterSeconds) {
      setCooldown(cause.retryAfterSeconds);
    }
    setError(customerVisibleErrorMessage(cause, fallback));
  }

  function startBinding() {
    setEmail(state?.email ?? "");
    setCode("");
    setError("");
    setNotice("");
    setMode("email");
  }

  async function sendCode(event?: FormEvent) {
    event?.preventDefault();
    if (pending.current) return;
    const target = email.trim();
    if (!target) {
      setError("请输入邮箱地址。");
      return;
    }
    pending.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await customerSendEmailBindCode(
        await credential(),
        target,
      );
      setEmail(target);
      setCooldown(result.resend_after_seconds);
      setCode("");
      setMode("code");
    } catch (cause) {
      captureError(cause, "验证码发送失败，请检查网络后重试。");
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }

  async function verifyCode(event: FormEvent) {
    event.preventDefault();
    if (pending.current) return;
    if (!CODE_PATTERN.test(code.trim())) {
      setError("请输入 6 位数字验证码。");
      return;
    }
    pending.current = true;
    setBusy(true);
    setError("");
    try {
      const updated = await customerVerifyEmailBindCode(await credential(), {
        email: email.trim(),
        code: code.trim(),
      });
      setState(updated);
      setNotice("邮箱已绑定。忘记密码时验证码会发到这个邮箱。");
      setMode("idle");
      setCode("");
    } catch (cause) {
      captureError(cause, "验证失败，请检查网络后重试。");
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }

  return (
    <section className="uc-card uc-email">
      <h2>
        <Icon name="mail" size={18} /> 绑定邮箱
      </h2>
      <p>绑定邮箱后，忘记密码可以自助重置；没有它，忘记密码只能联系客服。</p>
      {loadError ? (
        <div className="uc-error" role="alert">
          {loadError}
          <RetryButton onClick={() => setRefresh((value) => value + 1)} />
        </div>
      ) : state === null ? (
        <p role="status">正在读取绑定邮箱…</p>
      ) : (
        <>
          {notice ? (
            <p className="uc-notice" role="status">
              {notice}
              <button type="button" onClick={() => setNotice("")}>
                关闭提示
              </button>
            </p>
          ) : null}
          {!state.service_available ? (
            <p className="uc-muted">
              邮件服务暂未开通，绑定邮箱暂不可用，请联系客服。
            </p>
          ) : !state.can_bind ? (
            <p className="uc-muted">
              子账号无需绑定邮箱：密码由主账号统一管理。
            </p>
          ) : mode === "idle" ? (
            <div className="uc-preference">
              <Icon name="mail" />
              <div>
                <strong>{state.email ?? "未绑定邮箱"}</strong>
                <p>
                  {state.email
                    ? `已验证${state.verified_at ? ` · ${timeLabel(state.verified_at)}` : ""}`
                    : "绑定后可用邮箱找回密码。"}
                </p>
              </div>
              <button
                className={state.email ? "uc-outline" : "uc-primary"}
                type="button"
                onClick={startBinding}
              >
                {state.email ? "更换邮箱" : "绑定邮箱"}
              </button>
            </div>
          ) : mode === "email" ? (
            <form onSubmit={sendCode}>
              <label htmlFor="uc-email">邮箱地址</label>
              <input
                id="uc-email"
                type="email"
                autoComplete="email"
                required
                maxLength={254}
                placeholder="name@example.com"
                value={email}
                disabled={busy}
                onChange={(event) => setEmail(event.target.value)}
              />
              <p className="uc-muted">
                验证码会发到这个地址；验证通过之前，它不会写进账号。
              </p>
              {error ? (
                <p className="uc-error" role="alert">
                  {error}
                </p>
              ) : null}
              <div className="uc-email__actions">
                <button className="uc-primary" type="submit" disabled={busy}>
                  {busy ? "正在发送…" : "发送验证码"}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setMode("idle");
                    setError("");
                  }}
                >
                  取消
                </button>
              </div>
            </form>
          ) : (
            <form onSubmit={verifyCode}>
              <p className="uc-muted">验证码已发往 {email}。</p>
              <label htmlFor="uc-email-code">验证码</label>
              <input
                id="uc-email-code"
                autoComplete="one-time-code"
                inputMode="numeric"
                required
                maxLength={6}
                placeholder="6 位数字验证码"
                value={code}
                disabled={busy}
                onChange={(event) => setCode(event.target.value)}
              />
              {error ? (
                <p className="uc-error" role="alert">
                  {error}
                </p>
              ) : null}
              <div className="uc-email__actions">
                <button className="uc-primary" type="submit" disabled={busy}>
                  {busy ? "正在验证…" : "确认绑定"}
                </button>
                <button
                  type="button"
                  disabled={busy || cooldown > 0}
                  onClick={() => void sendCode()}
                >
                  {cooldown > 0 ? `重新发送（${cooldown} 秒）` : "重新发送"}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setMode("email");
                    setCode("");
                    setError("");
                  }}
                >
                  换一个邮箱
                </button>
              </div>
            </form>
          )}
        </>
      )}
    </section>
  );
}
