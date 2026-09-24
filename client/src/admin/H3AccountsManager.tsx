import { type FormEvent, useEffect, useRef, useState } from "react";
import { adminRead, adminWrite } from "../api.admin";

type Account = {
  id: string;
  name: string;
  concurrency_limit: number;
  enabled: boolean;
  configured: boolean;
  version: number;
  active_tasks: number;
};
type Snapshot = {
  accounts: Account[];
  total_concurrency: number;
  managed: boolean;
};
const endpoint = "/api/control/settings/h3-accounts";

export function H3AccountsManager({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [data, setData] = useState<Snapshot | null>(null);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    void revision;
    let active = true;
    void adminRead<Snapshot>(endpoint, "读取视频账号失败")
      .then((value) => {
        if (active) {
          setData(value);
          setError("");
        }
      })
      .catch((cause) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "读取视频账号失败");
      });
    return () => {
      active = false;
    };
  }, [revision]);
  return (
    <section className="provider-form h3-accounts" aria-label="视频生成账号">
      <header className="h3-accounts__header">
        <div>
          <h3>视频生成 · 多账号</h3>
          <p>按账号额度设置并发，空闲账号依次接收任务。</p>
        </div>
        {data && <strong>总并发 {data.total_concurrency}</strong>}
      </header>
      {error && (
        <p role="alert">
          {error}{" "}
          <button type="button" onClick={() => setRevision((v) => v + 1)}>
            重新读取
          </button>
        </p>
      )}
      {!data ? (
        <p role="status">正在读取账号…</p>
      ) : (
        <>
          <div className="h3-accounts__list">
            {data.accounts.map((account) => (
              <AccountForm
                key={`${account.id}:${account.version}`}
                account={account}
                readOnly={readOnly}
                onSaved={setData}
              />
            ))}
            {data.accounts.length === 0 && !adding && (
              <p>添加视频生成账号，并填写该账号可用的并发上限。</p>
            )}
            {adding && (
              <AccountForm
                readOnly={readOnly}
                onSaved={(value) => {
                  setData(value);
                  setAdding(false);
                }}
                onCancel={() => setAdding(false)}
              />
            )}
          </div>
          {!readOnly && !adding && (
            <button type="button" onClick={() => setAdding(true)}>
              添加账号
            </button>
          )}
        </>
      )}
    </section>
  );
}

function AccountForm({
  account,
  readOnly,
  onSaved,
  onCancel,
}: {
  account?: Account;
  readOnly: boolean;
  onSaved: (data: Snapshot) => void;
  onCancel?: () => void;
}) {
  const id = useRef(account?.id ?? crypto.randomUUID());
  const [name, setName] = useState(account?.name ?? "");
  const [key, setKey] = useState("");
  // A new account deliberately has no assumed concurrency entitlement.
  const [limit, setLimit] = useState(
    account ? String(account.concurrency_limit) : "",
  );
  const [enabled, setEnabled] = useState(account?.enabled ?? true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const saving = useRef(false);
  const retry = useRef<{ fingerprint: string; key: string } | null>(null);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (readOnly || saving.current) return;
    const concurrency = Number(limit);
    if (
      !name.trim() ||
      !Number.isSafeInteger(concurrency) ||
      concurrency < 1 ||
      concurrency > 1_000_000 ||
      (!account && !key.trim())
    ) {
      setError("请填写账号名称、API Key 及正整数并发上限。");
      return;
    }
    const payload = {
      name: name.trim(),
      api_key: key.trim(),
      concurrency_limit: concurrency,
      enabled,
      expected_version: account?.version ?? 0,
    };
    const fingerprint = JSON.stringify(payload);
    if (retry.current?.fingerprint !== fingerprint)
      retry.current = { fingerprint, key: crypto.randomUUID() };
    saving.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await adminWrite<Snapshot>(
        `${endpoint}/${encodeURIComponent(id.current)}`,
        payload,
        "配置视频生成账号及并发额度",
        "保存视频账号失败",
        retry.current.key,
        "PUT",
      );
      setKey("");
      retry.current = null;
      onSaved(result);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存视频账号失败");
    } finally {
      saving.current = false;
      setBusy(false);
    }
  }
  return (
    <form
      className="h3-account-row"
      onSubmit={submit}
      autoComplete="off"
      aria-label={account ? `${account.name}配置` : "新增视频账号"}
    >
      <label>
        账号名称
        <input
          required
          maxLength={80}
          disabled={readOnly || busy}
          value={name}
          placeholder="例如：视频账号 A"
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label>
        API Key
        <input
          type="password"
          autoComplete="new-password"
          required={!account}
          disabled={readOnly || busy}
          value={key}
          placeholder={
            account?.configured ? "已配置，留空保留" : "请输入账号密钥"
          }
          onChange={(e) => setKey(e.target.value)}
        />
      </label>
      <label>
        并发上限
        <input
          type="number"
          min="1"
          max="1000000"
          step="1"
          required
          disabled={readOnly || busy}
          value={limit}
          placeholder="按账号额度填写"
          onChange={(e) => setLimit(e.target.value)}
        />
      </label>
      <label>
        账号状态
        <select
          disabled={readOnly || busy}
          value={enabled ? "enabled" : "paused"}
          onChange={(e) => setEnabled(e.target.value === "enabled")}
        >
          <option value="enabled">启用</option>
          <option value="paused">暂停接单</option>
        </select>
      </label>
      <div className="h3-account-row__actions">
        <span>进行中 {account?.active_tasks ?? 0}</span>
        {!readOnly && (
          <button type="submit" disabled={busy}>
            {busy ? "保存中…" : "保存账号"}
          </button>
        )}
        {onCancel && (
          <button type="button" disabled={busy} onClick={onCancel}>
            取消
          </button>
        )}
      </div>
      {error && (
        <p className="h3-account-row__error" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
