import { type FormEvent, useEffect, useRef, useState } from "react";
import { CustomerApiError, customerVisibleErrorMessage } from "../api";
import { Icon } from "../studio/ui";
import "./account-access.css";

export type AccountAccessInput = {
  mode: "login" | "register";
  username: string;
  password: string;
  /** 用户是否勾选「记住密码」；持久化由调用方决定，本页不碰存储。 */
  remember: boolean;
};

export function AccountAccessPage({
  onSubmit,
  onHome,
  initialMode = "login",
  onModeChange,
  remembered = null,
}: {
  onSubmit(input: AccountAccessInput): Promise<void>;
  onHome(): void;
  initialMode?: "login" | "register";
  onModeChange?(mode: "login" | "register"): void;
  /** 系统凭据库里已记住的登录，用于预填；本页只读不写。 */
  remembered?: { username: string; password: string } | null;
}) {
  const [mode, setMode] = useState<"login" | "register">(initialMode);
  const [username, setUsername] = useState(remembered?.username ?? "");
  const [password, setPassword] = useState(remembered?.password ?? "");
  const [remember, setRemember] = useState(remembered !== null);
  const [confirmation, setConfirmation] = useState("");
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef(false);
  useEffect(() => {
    setMode(initialMode);
    setError("");
  }, [initialMode]);

  async function submit() {
    if (pending.current) return;
    setError("");
    if (mode === "register" && password !== confirmation) {
      setError("两次输入的密码不一致，请重新输入。");
      return;
    }
    pending.current = true;
    setBusy(true);
    try {
      await onSubmit({
        mode,
        username: username.trim(),
        password,
        // 注册模式不提供这个选项：口令刚由用户亲手设定，没有免输价值。
        remember: mode === "login" && remember,
      });
    } catch (cause) {
      setError(
        cause instanceof CustomerApiError
          ? customerVisibleErrorMessage(cause)
          : "暂时无法完成登录，请检查网络后重试。",
      );
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    void submit();
  }

  return (
    <main className="account-access">
      <button
        className="account-access-home"
        type="button"
        onClick={onHome}
        disabled={busy}
      >
        <Icon name="home" /> 返回主界面
      </button>
      <section className="account-access-story" aria-label="众墅之家 AI 即创">
        <div className="account-official-brand">
          <img src="/studio/brand.png" alt="众墅之家" />
          <span>AI 即创</span>
        </div>
        <div className="account-access-message">
          <h1>
            {mode === "login" ? (
              <>
                欢迎回来
                <br />
                继续你的创作
              </>
            ) : (
              <>
                让每一个好想法
                <br />
                成为爆款视频
              </>
            )}
          </h1>
          <i aria-hidden="true" />
          <p>众墅之家 · AI 即创</p>
        </div>
      </section>
      <section
        className="account-access-form-panel"
        aria-labelledby="account-access-title"
      >
        <h2 id="account-access-title">
          {mode === "login" ? "登录账号" : "创建账号"}
        </h2>
        <p className="account-access-subtitle">
          {mode === "login"
            ? "登录后，即可开始创作。"
            : "简单注册，开启你的创作之旅。"}
        </p>
        <form onSubmit={handleSubmit}>
          <label htmlFor="account-username">用户名</label>
          <div className="account-field">
            <Icon name="person" />
            <input
              id="account-username"
              autoComplete="username"
              required
              minLength={3}
              maxLength={32}
              pattern="[A-Za-z0-9._\-]+"
              title="3–32 位字母、数字、点、下划线或短横线"
              placeholder="请输入用户名"
              value={username}
              disabled={busy}
              onChange={(e) => setUsername(e.target.value)}
            />
          </div>
          {mode === "register" && (
            <small className="account-field-help">
              3–32 位，支持字母、数字、点、下划线和短横线
            </small>
          )}
          <label htmlFor="account-password">密码</label>
          <div className="account-field">
            <Icon name="shield" />
            <input
              id="account-password"
              type={visible ? "text" : "password"}
              required
              minLength={mode === "register" ? 6 : 1}
              maxLength={128}
              autoComplete={
                mode === "register" ? "new-password" : "current-password"
              }
              placeholder={
                mode === "register" ? "请输入不少于 6 位的密码" : "请输入密码"
              }
              value={password}
              disabled={busy}
              onChange={(e) => setPassword(e.target.value)}
            />
            <button
              type="button"
              className="account-password-toggle"
              aria-label={visible ? "隐藏密码" : "显示密码"}
              aria-pressed={visible}
              onClick={() => setVisible((v) => !v)}
            >
              {visible ? "隐藏" : "显示"}
            </button>
          </div>
          {mode === "register" && (
            <>
              <label htmlFor="account-confirmation">确认密码</label>
              <div className="account-field">
                <Icon name="shield" />
                <input
                  id="account-confirmation"
                  type={visible ? "text" : "password"}
                  autoComplete="new-password"
                  required
                  minLength={6}
                  maxLength={128}
                  placeholder="请再次输入密码"
                  disabled={busy}
                  value={confirmation}
                  onChange={(e) => setConfirmation(e.target.value)}
                />
              </div>
            </>
          )}
          {mode === "login" && (
            <label className="account-remember" htmlFor="account-remember">
              <input
                id="account-remember"
                type="checkbox"
                // 可见 label 里还带一行小字说明，显式命名避免读屏把说明一起念成控件名
                aria-label="记住密码"
                disabled={busy}
                checked={remember}
                onChange={(e) => setRemember(e.target.checked)}
              />
              <span>记住密码</span>
              <small>保存在本机系统钥匙串，不会明文落盘</small>
            </label>
          )}
          {error && (
            <p className="account-error" role="alert">
              {error}
            </p>
          )}
          <button className="account-submit" type="submit" disabled={busy}>
            {busy ? "正在处理…" : mode === "login" ? "登录" : "注册并登录"}
          </button>
        </form>
        <p className="account-switch">
          {mode === "login" ? "还没有账号？" : "已有账号？"}
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              const nextMode = mode === "login" ? "register" : "login";
              setMode(nextMode);
              onModeChange?.(nextMode);
              setError("");
              setConfirmation("");
              setPassword("");
              setVisible(false);
            }}
          >
            {mode === "login" ? "去注册" : "去登录"}
          </button>
        </p>
        <p className="account-access-footer">
          {mode === "login"
            ? "登录成功后返回主界面"
            : "注册后自动登录并返回主界面"}
        </p>
      </section>
    </main>
  );
}
