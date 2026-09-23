import { type FormEvent, useRef, useState } from "react";
import { type CustomerSessionCredential, customerChangePassword } from "../api";

// 与服务端 ``password_hashing`` 同一套策略（6–128，scrypt）。客户端这份只为了
// 在打字时给即时反馈，真正的门禁在服务端——两边数字改了要一起改。
const MIN_PASSWORD_LENGTH = 6;
const MAX_PASSWORD_LENGTH = 128;

/**
 * 账号设置里的「修改密码」。
 *
 * 改密会连带撤销当前活跃会话（服务端语义：旧会话不得比旧凭据活得更久），所以
 * 成功后由 ``onChanged`` 的调用方把用户带回登录页，而不是在这里假装还在线。
 */
export function ChangePasswordForm({
  credential,
  onChanged,
}: {
  credential: () => Promise<CustomerSessionCredential>;
  onChanged: (sessionsRevoked: number) => void | Promise<void>;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // 同一份输入重试沿用同一个追踪号（审计里能看出是一次重试而不是两次操作）；
  // 输入一变就换新号。服务端不为此接口保留重放信封，所以键只承担追溯作用。
  const retry = useRef<{ fingerprint: string; key: string } | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    if (next.length < MIN_PASSWORD_LENGTH || next !== repeat) {
      setError(`新密码不少于 ${MIN_PASSWORD_LENGTH} 位，两次输入需一致。`);
      return;
    }
    if (next === current) {
      setError("新密码不能与当前密码相同。");
      return;
    }
    const fingerprint = JSON.stringify({ current, next });
    if (retry.current?.fingerprint !== fingerprint) {
      retry.current = { fingerprint, key: crypto.randomUUID() };
    }
    setBusy(true);
    setError("");
    let sessionsRevoked: number;
    try {
      const result = await customerChangePassword(
        await credential(),
        { currentPassword: current, newPassword: next },
        retry.current.key,
      );
      sessionsRevoked = result.sessions_revoked;
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "密码修改失败，请稍后重试。",
      );
      return;
    } finally {
      setBusy(false);
    }
    // 到这里密码**已经改掉了**。后面（清空表单、把用户带回登录页）出问题也不能
    // 报成「改密失败」——那会让用户以为凭据没变，而实际旧密码已经作废，
    // 再拿它重试只会得到 401。
    retry.current = null;
    setCurrent("");
    setNext("");
    setRepeat("");
    try {
      await onChanged(sessionsRevoked);
    } catch {
      setError("密码已修改，但自动退出登录失败，请手动退出后重新登录。");
    }
  }

  return (
    <form className="uc-security__password" onSubmit={submit}>
      <p>修改后当前登录状态会立即失效，需要用新密码重新登录。</p>
      <label htmlFor="uc-current-password">当前密码</label>
      <input
        autoComplete="current-password"
        disabled={busy}
        id="uc-current-password"
        maxLength={MAX_PASSWORD_LENGTH}
        onChange={(event) => setCurrent(event.target.value)}
        required
        type="password"
        value={current}
      />
      <label htmlFor="uc-new-password">新密码</label>
      <input
        autoComplete="new-password"
        disabled={busy}
        id="uc-new-password"
        maxLength={MAX_PASSWORD_LENGTH}
        minLength={MIN_PASSWORD_LENGTH}
        onChange={(event) => setNext(event.target.value)}
        required
        type="password"
        value={next}
      />
      <label htmlFor="uc-repeat-password">再次输入新密码</label>
      <input
        autoComplete="new-password"
        disabled={busy}
        id="uc-repeat-password"
        maxLength={MAX_PASSWORD_LENGTH}
        minLength={MIN_PASSWORD_LENGTH}
        onChange={(event) => setRepeat(event.target.value)}
        required
        type="password"
        value={repeat}
      />
      {error ? (
        <p className="uc-error" role="alert">
          {error}
        </p>
      ) : null}
      <button className="uc-primary" disabled={busy} type="submit">
        {busy ? "正在保存…" : "修改密码"}
      </button>
    </form>
  );
}
