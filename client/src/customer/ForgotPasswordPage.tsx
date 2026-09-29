import { type FormEvent, useEffect, useRef, useState } from "react";
import {
  CustomerApiError,
  customerForgotPassword,
  customerResetPassword,
  customerVisibleErrorMessage,
} from "../api";
import zhongshuLogoMark from "../assets/brand/zhongshu-logo-mark.svg";
import { Icon } from "../studio/ui";
import "./account-access.css";

// 与服务端 ``password_hashing`` 同一套策略（6–128，scrypt）。这份只为了在
// 打字时给即时反馈，真正的门禁在服务端。
const MIN_PASSWORD_LENGTH = 6;
const MAX_PASSWORD_LENGTH = 128;
const CODE_PATTERN = /^\d{6}$/;
const CODE_TTL_MINUTES = 15;

type Step = "account" | "code" | "done";

/**
 * 邮箱找回密码（无会话）。三步：报账号 → 输验证码 + 新密码 → 完成。
 *
 * 防枚举：服务端对「账号不存在 / 没绑邮箱 / 子账号 / 冷却中」一律回同一句
 * 「如果该账号已绑定邮箱…」，本页原样转述，不在前端加任何「账号不存在」式
 * 的判断——那等于把服务端刻意抹掉的信息替用户猜回来。
 */
export function ForgotPasswordPage({ onBack }: { onBack(): void }) {
  const [step, setStep] = useState<Step>("account");
  const [account, setAccount] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const [sessionsRevoked, setSessionsRevoked] = useState(0);
  const pending = useRef(false);

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

  async function sendCode(event?: FormEvent) {
    event?.preventDefault();
    if (pending.current) return;
    const target = account.trim();
    if (!target) {
      setError("请输入用户名或邮箱。");
      return;
    }
    pending.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await customerForgotPassword(target);
      setAccount(target);
      setNotice(result.message);
      setCooldown(result.resend_after_seconds);
      setCode("");
      setStep("code");
    } catch (cause) {
      captureError(cause, "暂时无法发送验证码，请检查网络后重试。");
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }

  async function resetPassword(event: FormEvent) {
    event.preventDefault();
    if (pending.current) return;
    if (!CODE_PATTERN.test(code.trim())) {
      setError("请输入 6 位数字验证码。");
      return;
    }
    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`新密码不少于 ${MIN_PASSWORD_LENGTH} 位。`);
      return;
    }
    if (password !== confirmation) {
      setError("两次输入的密码不一致，请重新输入。");
      return;
    }
    pending.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await customerResetPassword({
        account,
        code: code.trim(),
        newPassword: password,
      });
      setSessionsRevoked(result.sessions_revoked);
      setStep("done");
    } catch (cause) {
      captureError(cause, "重置失败，请检查网络后重试。");
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }

  return (
    <main className="account-access">
      <button
        className="account-access-home"
        type="button"
        onClick={onBack}
        disabled={busy}
      >
        <Icon name="back" /> 返回登录
      </button>
      <section className="account-access-story" aria-label="众墅之家 AI 即创">
        <div className="account-official-brand">
          <img src={zhongshuLogoMark} alt="众墅之家" />
          <span>AI 即创</span>
        </div>
        <div className="account-access-message">
          <h1>
            忘了密码
            <br />
            找回来继续创作
          </h1>
          <i aria-hidden="true" />
          <p>众墅之家 · AI 即创</p>
        </div>
      </section>
      <section
        className="account-access-form-panel"
        aria-labelledby="account-access-title"
      >
        {step === "account" && (
          <>
            <h2 id="account-access-title">找回密码</h2>
            <p className="account-access-subtitle">
              输入用户名或已绑定邮箱，验证码会发到绑定邮箱。
            </p>
            <form onSubmit={sendCode}>
              <label htmlFor="forgot-account">用户名或邮箱</label>
              <div className="account-field">
                <Icon name="person" />
                <input
                  id="forgot-account"
                  autoComplete="username"
                  required
                  maxLength={254}
                  placeholder="请输入用户名或邮箱"
                  value={account}
                  disabled={busy}
                  onChange={(event) => setAccount(event.target.value)}
                />
              </div>
              {error && (
                <p className="account-error" role="alert">
                  {error}
                </p>
              )}
              <button className="account-submit" type="submit" disabled={busy}>
                {busy ? "正在发送…" : "发送验证码"}
              </button>
            </form>
            <p className="account-switch">
              想起密码了？
              <button type="button" onClick={onBack} disabled={busy}>
                返回登录
              </button>
            </p>
          </>
        )}
        {step === "code" && (
          <>
            <h2 id="account-access-title">设置新密码</h2>
            <p className="account-access-subtitle">
              {notice || "验证码已发送，请查收邮件。"}
              验证码 {CODE_TTL_MINUTES} 分钟内有效。
            </p>
            <form onSubmit={resetPassword}>
              <label htmlFor="forgot-code">验证码</label>
              <div className="account-field">
                <Icon name="shield" />
                <input
                  id="forgot-code"
                  autoComplete="one-time-code"
                  inputMode="numeric"
                  required
                  maxLength={6}
                  placeholder="6 位数字验证码"
                  value={code}
                  disabled={busy}
                  onChange={(event) => setCode(event.target.value)}
                />
              </div>
              <label htmlFor="forgot-password">新密码</label>
              <div className="account-field">
                <Icon name="shield" />
                <input
                  id="forgot-password"
                  type="password"
                  autoComplete="new-password"
                  required
                  minLength={MIN_PASSWORD_LENGTH}
                  maxLength={MAX_PASSWORD_LENGTH}
                  placeholder={`请输入不少于 ${MIN_PASSWORD_LENGTH} 位的密码`}
                  value={password}
                  disabled={busy}
                  onChange={(event) => setPassword(event.target.value)}
                />
              </div>
              <label htmlFor="forgot-confirmation">确认新密码</label>
              <div className="account-field">
                <Icon name="shield" />
                <input
                  id="forgot-confirmation"
                  type="password"
                  autoComplete="new-password"
                  required
                  minLength={MIN_PASSWORD_LENGTH}
                  maxLength={MAX_PASSWORD_LENGTH}
                  placeholder="请再次输入新密码"
                  value={confirmation}
                  disabled={busy}
                  onChange={(event) => setConfirmation(event.target.value)}
                />
              </div>
              {error && (
                <p className="account-error" role="alert">
                  {error}
                </p>
              )}
              <p className="account-forgot-row">
                <button
                  type="button"
                  className="account-forgot"
                  disabled={busy || cooldown > 0}
                  onClick={() => void sendCode()}
                >
                  {cooldown > 0
                    ? `重新获取验证码（${cooldown} 秒）`
                    : "重新获取验证码"}
                </button>
              </p>
              <button className="account-submit" type="submit" disabled={busy}>
                {busy ? "正在重置…" : "重置密码"}
              </button>
            </form>
            <p className="account-switch">
              想起密码了？
              <button type="button" onClick={onBack} disabled={busy}>
                返回登录
              </button>
            </p>
          </>
        )}
        {step === "done" && (
          <>
            <h2 id="account-access-title">密码已重置</h2>
            <p className="account-access-subtitle">
              {sessionsRevoked > 0
                ? `新密码已生效，其他设备上的 ${sessionsRevoked} 处登录已下线，请用新密码重新登录。`
                : "新密码已生效，请用新密码重新登录。"}
            </p>
            <button
              className="account-submit"
              type="button"
              onClick={onBack}
              disabled={busy}
            >
              返回登录
            </button>
            <p className="account-access-footer">
              登录后可在「个人中心 · 账号安全」查看最近的登录记录。
            </p>
          </>
        )}
      </section>
    </main>
  );
}
