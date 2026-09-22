import { useEffect, useRef, useState } from "react";
import {
  type CustomerPasswordState,
  type CustomerSessionCredential,
  customerPasswordState,
  customerSetInitialPassword,
} from "../api";
import { passwordStrength } from "./passwordStrength";

export function AccountPasswordSetup({
  credential,
  onComplete,
}: {
  credential: () => Promise<CustomerSessionCredential>;
  onComplete: () => void;
}) {
  const [state, setState] = useState<CustomerPasswordState | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState("");
  const strength = passwordStrength(password);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const retry = useRef<{ fingerprint: string; key: string } | null>(null);
  useEffect(() => {
    void refresh;
    let active = true;
    void credential()
      .then(customerPasswordState)
      .then((value) => {
        if (active) {
          setState(value);
          setUsername(value.username);
          setError("");
        }
      })
      .catch((cause) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "读取账号状态失败");
      });
    return () => {
      active = false;
    };
  }, [credential, refresh]);
  return (
    <section className="uc-card">
      <h2>账号登录</h2>
      {error && (
        <div role="alert" className="uc-error">
          {error}
          <button
            type="button"
            disabled={busy}
            onClick={() => setRefresh((value) => value + 1)}
          >
            重新读取
          </button>
        </div>
      )}
      {!state && !error && <p role="status">正在读取账号状态…</p>}
      {state?.has_password ? (
        <p>已设置登录密码，可使用用户名 {state.username} 登录。</p>
      ) : (
        state && (
          <form
            onSubmit={async (event) => {
              event.preventDefault();
              if (busy) return;
              if (password.length < 6 || password !== repeat) {
                setError("密码不少于 6 位，两次输入需一致。");
                return;
              }
              const fingerprint = JSON.stringify({
                username: username.trim(),
                password,
              });
              if (retry.current?.fingerprint !== fingerprint)
                retry.current = { fingerprint, key: crypto.randomUUID() };
              setBusy(true);
              setError("");
              try {
                const result = await customerSetInitialPassword(
                  await credential(),
                  { username: username.trim(), password },
                  retry.current.key,
                );
                setState(result);
                setPassword("");
                setRepeat("");
                retry.current = null;
                onComplete();
              } catch (cause) {
                setError(
                  cause instanceof Error ? cause.message : "账号设置失败",
                );
              } finally {
                setBusy(false);
              }
            }}
          >
            <p>为现有账号设置登录信息，原有积分、项目和 Token 继续保留。</p>
            <label htmlFor="legacy-username">登录用户名</label>
            <input
              id="legacy-username"
              value={username}
              minLength={3}
              maxLength={32}
              required
              disabled={busy}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="username"
            />
            <label htmlFor="legacy-password">设置登录密码</label>
            <input
              id="legacy-password"
              type="password"
              minLength={6}
              maxLength={128}
              value={password}
              required
              disabled={busy}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="new-password"
            />
            {strength.label ? (
              <p
                className={`uc-password-strength uc-password-strength--${strength.score}`}
                role="status"
              >
                <span
                  className="uc-password-strength__meter"
                  aria-hidden="true"
                >
                  <i />
                </span>
                强度：{strength.label}
                {strength.hint ? ` · ${strength.hint}` : ""}
              </p>
            ) : null}
            <label htmlFor="legacy-repeat">再次输入密码</label>
            <input
              id="legacy-repeat"
              type="password"
              minLength={6}
              value={repeat}
              required
              disabled={busy}
              onChange={(event) => setRepeat(event.target.value)}
              autoComplete="new-password"
            />
            <button type="submit" className="uc-primary" disabled={busy}>
              {busy ? "正在保存…" : "设置账号密码"}
            </button>
          </form>
        )
      )}
    </section>
  );
}
