import { useEffect, useRef, useState } from "react";
import { useStudio } from "./context";
import {
  type CloudPublishAccount,
  cancelLocalPublishLogin,
  canUseLocalPublishAccounts,
  checkLocalPublishLogin,
  deleteCloudPublishAccount,
  exportLocalPublishAccountState,
  focusLocalPublishLogin,
  importCloudPublishAccount,
  isPublishStorageState,
  type LocalPublishAccount,
  type LocalPublishLoginStatus,
  listCloudPublishAccounts,
  listLocalPublishAccounts,
  removeLocalPublishAccount,
  startLocalPublishLogin,
} from "./localPublishAccounts";
import {
  AccountAvatar,
  PlatformLogo,
  type PublishPlatform,
  publishPlatformNames,
} from "./PlatformLogo";
import { Button, Panel } from "./ui";
import "./publish-accounts.css";

const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "账号操作失败，请重试。";

const loginMessages: Record<LocalPublishLoginStatus["phase"], string> = {
  loading: "正在加载官方登录二维码…",
  qr_ready: "请使用对应平台的手机 App 扫码，并在手机上确认登录。",
  confirming: "已扫码，正在等待手机确认和平台账号信息…",
  action_required: "平台要求进一步验证，请按平台提示完成后重试。",
  expired: "二维码或本次连接已过期，请重新获取。",
  closed: "本次扫码连接已关闭，请重新获取二维码。",
  connected: "账号已连接。",
};
const loadingStatus: LocalPublishLoginStatus = {
  phase: "loading",
  image: null,
  account: null,
};
const cloudKey = (account: { platform: string; platform_user_id: string }) =>
  `${account.platform}:${account.platform_user_id}`;
const SYNC_FAILED_MESSAGE =
  "本机已连接，但同步到服务端失败，自动发布暂不可用；请点击「同步到服务端」重试。";
// 源码取码（隐藏窗口）的预算：超时仍拿不到二维码才降级为可见官方窗口。
const AUTO_OFFICIAL_WINDOW_MS = 30_000;

export function LocalPublishAccountsPanel({
  notify,
}: {
  notify(message: string): void;
}) {
  const { user, review } = useStudio();
  const [accounts, setAccounts] = useState<LocalPublishAccount[]>([]);
  const [platform, setPlatform] = useState<PublishPlatform>("douyin");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loginId, setLoginId] = useState<string | null>(null);
  const [loginStatus, setLoginStatus] = useState(loadingStatus);
  const [retryPoll, setRetryPoll] = useState(0);
  const [pollPaused, setPollPaused] = useState(false);
  const [removing, setRemoving] = useState<LocalPublishAccount | null>(null);
  // Desktop only: server-side copies keyed by platform + platform_user_id, so the
  // list can say whether automatic publishing is available for a local profile.
  const [cloudAccounts, setCloudAccounts] = useState<
    Record<string, CloudPublishAccount>
  >({});
  const [syncErrors, setSyncErrors] = useState<Record<string, string>>({});
  const [syncing, setSyncing] = useState<string | null>(null);
  const currentLogin = useRef<string | null>(null);
  const loginAccount = useRef<LocalPublishAccount | undefined>(undefined);
  const notifyRef = useRef(notify);
  notifyRef.current = notify;
  // The login poll effect must not restart when this callback's identity changes.
  const syncToCloudRef = useRef<
    (account: LocalPublishAccount, storageState?: unknown) => Promise<void>
  >(async () => {});
  // Same reason: auto-degrade calls focus() from inside the poll loop.
  const focusRef = useRef<() => Promise<void>>(async () => {});
  const generation = useRef(0);
  const pending = useRef(false);
  const native = canUseLocalPublishAccounts();
  useEffect(() => {
    void refresh;
    const current = ++generation.current;
    setError("");
    setAccounts([]);
    setLoading(true);
    if (review) {
      setLoading(false);
      return;
    }
    void listLocalPublishAccounts(user.id)
      .then((value) => {
        if (generation.current === current) setAccounts(value);
      })
      .catch((cause) => {
        if (generation.current === current) setError(errorMessage(cause));
      })
      .finally(() => {
        if (generation.current === current) setLoading(false);
      });
    if (native)
      void listCloudPublishAccounts()
        .then((value) => {
          if (generation.current !== current) return;
          setCloudAccounts(
            Object.fromEntries(value.map((item) => [cloudKey(item), item])),
          );
        })
        .catch(() => {
          // The local list still renders; sync status simply shows as unknown.
          if (generation.current === current) setCloudAccounts({});
        });
    return () => {
      generation.current += 1;
    };
  }, [user.id, review, refresh, native]);
  const rememberCloud = (account: CloudPublishAccount) => {
    setCloudAccounts((previous) => ({
      ...previous,
      [cloudKey(account)]: account,
    }));
    setSyncErrors((previous) => {
      const { [cloudKey(account)]: _dropped, ...rest } = previous;
      return rest;
    });
  };
  const uploadLoginState = async (
    account: LocalPublishAccount,
    storageState: unknown,
  ) => {
    if (!isPublishStorageState(storageState))
      throw new Error("本机未能导出登录状态");
    const cloud = await importCloudPublishAccount(
      account.platform,
      {
        platform_user_id: account.platform_user_id,
        username: account.username,
        avatar_url: account.avatar_url ?? null,
      },
      storageState,
    );
    rememberCloud(cloud);
  };
  async function syncToCloud(
    account: LocalPublishAccount,
    storageState?: unknown,
  ) {
    const key = cloudKey(account);
    setSyncing(key);
    try {
      if (storageState === undefined) {
        const exported = await exportLocalPublishAccountState(
          user.id,
          account.id,
        );
        await uploadLoginState(account, exported.storage_state);
      } else {
        await uploadLoginState(account, storageState);
      }
      notifyRef.current("登录状态已同步到服务端，可用于自动发布");
    } catch (cause) {
      setSyncErrors((previous) => ({
        ...previous,
        [key]: cause instanceof Error ? cause.message : SYNC_FAILED_MESSAGE,
      }));
    } finally {
      setSyncing((current) => (current === key ? null : current));
    }
  }
  syncToCloudRef.current = syncToCloud;
  useEffect(() => {
    setLoginId(null);
    setLoginStatus(loadingStatus);
    setRemoving(null);
    setBusy(false);
    return () => {
      const id = currentLogin.current;
      if (id) void cancelLocalPublishLogin(user.id, id).catch(() => {});
      currentLogin.current = null;
    };
  }, [user.id]);
  useEffect(() => {
    void retryPoll;
    if (!loginId) return;
    let active = true;
    let failures = 0;
    let timer: ReturnType<typeof setTimeout>;
    // 自动降级只触发一次；手动「打开官方窗口」按钮不受此标记限制。
    let autoOpened = false;
    const startedAt = Date.now();
    setPollPaused(false);
    const poll = async () => {
      try {
        const status = await checkLocalPublishLogin(user.id, loginId);
        if (!active || currentLogin.current !== loginId) return;
        failures = 0;
        setError("");
        setLoginStatus(status);
        if (status.phase === "connected" && status.account) {
          const account = status.account;
          currentLogin.current = null;
          setLoginId(null);
          setPlatform(account.platform);
          setAccounts((previous) => [
            account,
            ...previous.filter((item) => item.id !== account.id),
          ]);
          notifyRef.current(
            `已连接 ${publishPlatformNames[account.platform]} · ${account.username}`,
          );
          if (canUseLocalPublishAccounts())
            void syncToCloudRef.current(account, status.storage_state ?? null);
        } else {
          // 源码取码失败才降级：需要额外验证，或预算内始终拿不到二维码。
          // confirming 说明用户已在扫码，不弹可见窗口打断。
          if (
            canUseLocalPublishAccounts() &&
            !autoOpened &&
            (status.phase === "action_required" ||
              (status.phase === "loading" &&
                Date.now() - startedAt >= AUTO_OFFICIAL_WINDOW_MS))
          ) {
            autoOpened = true;
            void focusRef.current();
          }
          if (status.phase !== "expired" && status.phase !== "closed") {
            timer = setTimeout(() => void poll(), 1500);
          }
        }
      } catch (cause) {
        if (!active || currentLogin.current !== loginId) return;
        failures += 1;
        setLoginStatus(loadingStatus);
        setError(errorMessage(cause));
        if (failures < 3)
          timer = setTimeout(() => void poll(), failures * 1500);
        else setPollPaused(true);
      }
    };
    timer = setTimeout(() => void poll(), 1000);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [loginId, user.id, retryPoll]);
  async function start(
    account?: LocalPublishAccount,
    selectedPlatform = platform,
  ) {
    if (
      pending.current ||
      currentLogin.current ||
      review ||
      user.role === "auditor"
    )
      return;
    pending.current = true;
    setBusy(true);
    setError("");
    setLoginStatus(loadingStatus);
    setPollPaused(false);
    loginAccount.current = account;
    if (account) setPlatform(account.platform);
    const current = generation.current;
    try {
      const id = await startLocalPublishLogin(
        user.id,
        account?.platform ?? selectedPlatform,
        account?.id,
      );
      if (current !== generation.current) {
        await cancelLocalPublishLogin(user.id, id);
        return;
      }
      currentLogin.current = id;
      setLoginId(id);
    } catch (cause) {
      if (current === generation.current) setError(errorMessage(cause));
    } finally {
      pending.current = false;
      if (current === generation.current) setBusy(false);
    }
  }
  function selectPlatform(selected: PublishPlatform) {
    setPlatform(selected);
    setError("");
    setRemoving(null);
    if (!accounts.some((account) => account.platform === selected)) {
      void start(undefined, selected);
    }
  }
  async function cancel(): Promise<boolean> {
    if (!loginId || pending.current) return false;
    pending.current = true;
    setBusy(true);
    currentLogin.current = null;
    const current = generation.current;
    try {
      await cancelLocalPublishLogin(user.id, loginId);
      if (current !== generation.current) return false;
      currentLogin.current = null;
      setLoginId(null);
      setLoginStatus(loadingStatus);
      setError("");
      return true;
    } catch (cause) {
      if (current !== generation.current) return false;
      currentLogin.current = loginId;
      setRetryPoll((value) => value + 1);
      setError(errorMessage(cause));
      return false;
    } finally {
      pending.current = false;
      if (current === generation.current) setBusy(false);
    }
  }
  async function restart() {
    const account = loginAccount.current;
    if (await cancel()) await start(account);
  }
  async function focus() {
    if (!loginId) return;
    const id = loginId;
    const ownerId = user.id;
    const current = generation.current;
    try {
      await focusLocalPublishLogin(ownerId, id);
      if (current !== generation.current || currentLogin.current !== id) return;
      // Only a paused poll needs resuming. When the window is shown automatically
      // (or manually) while detection is still active, restarting the poll loop
      // would double the cadence, so leave the running timer untouched.
      if (pollPaused) setRetryPoll((value) => value + 1);
    } catch (cause) {
      if (current === generation.current && currentLogin.current === id)
        setError(errorMessage(cause));
    }
  }
  focusRef.current = focus;
  async function remove() {
    if (!removing || pending.current) return;
    pending.current = true;
    setBusy(true);
    setError("");
    const current = generation.current;
    try {
      await removeLocalPublishAccount(user.id, removing.id);
      if (native) {
        const cloud = cloudAccounts[cloudKey(removing)];
        if (cloud) await deleteCloudPublishAccount(cloud.id).catch(() => {});
      }
      if (current !== generation.current) return;
      setRemoving(null);
      setRefresh((value) => value + 1);
      notify(`已清除该账号的${native ? "本机与服务端" : "云端"}登录状态`);
    } catch (cause) {
      if (current === generation.current) setError(errorMessage(cause));
    } finally {
      pending.current = false;
      if (current === generation.current) setBusy(false);
    }
  }
  return (
    <Panel>
      <h2>发布账号管理</h2>
      <p>选择平台，在下方扫描官方二维码，确认后账号将显示在对应标签下。</p>
      {!native && !review && (
        <p role="status">账号的登录状态加密保存在服务器，可在个人中心解绑。</p>
      )}
      {review && <p>审核预览：扫码和账号操作需登录工作台后使用。</p>}
      <div className="content-platform-options">
        {(Object.keys(publishPlatformNames) as PublishPlatform[]).map(
          (value) => (
            <Button
              key={value}
              aria-pressed={platform === value}
              disabled={loading || busy || Boolean(loginId)}
              onClick={() => selectPlatform(value)}
            >
              <PlatformLogo platform={value} />
              {publishPlatformNames[value]}
            </Button>
          ),
        )}
      </div>
      {error && (
        <p role="alert">
          {error}
          {!loginId && (
            <Button onClick={() => setRefresh((value) => value + 1)}>
              刷新账号
            </Button>
          )}
        </p>
      )}
      {native && (
        <p>
          桌面端账号的登录状态保存在本机，并在连接时加密同步一份到服务端用于自动发布。
        </p>
      )}
      {loading && <p role="status">正在读取发布账号…</p>}
      {!loading &&
        !loginId &&
        !busy &&
        !accounts.some((account) => account.platform === platform) && (
          <p>
            尚未连接{publishPlatformNames[platform]}
            账号，点击平台或扫码添加账号。
          </p>
        )}
      {accounts
        .filter((account) => account.platform === platform)
        .map((account) => (
          <div
            className="studio-publish-account publish-account"
            key={account.id}
          >
            <AccountAvatar account={account} size={32} />
            <span>
              {publishPlatformNames[account.platform]} · {account.username}
              <small>账号 ID：{account.platform_user_id}</small>
            </span>
            <small>
              {native ? "本机已连接" : "云端已连接"} · 最后验证{" "}
              {new Date(account.verified_at * 1000).toLocaleString("zh-CN")}
              {native &&
                (syncErrors[cloudKey(account)]
                  ? " · 服务端未同步"
                  : cloudAccounts[cloudKey(account)]?.status === "connected"
                    ? " · 已同步服务端，可自动发布"
                    : "")}
              {((native &&
                cloudAccounts[cloudKey(account)]?.status === "invalid") ||
                (!native &&
                  (account as Partial<CloudPublishAccount>).status ===
                    "invalid")) && (
                <>
                  {" · "}
                  <strong
                    className="publish-account-status--invalid"
                    role="alert"
                  >
                    服务端登录态失效，请重新登录
                  </strong>
                </>
              )}
            </small>
            {native && syncErrors[cloudKey(account)] && (
              <p role="alert">{syncErrors[cloudKey(account)]}</p>
            )}
            {native &&
              (syncErrors[cloudKey(account)] ||
                !cloudAccounts[cloudKey(account)]) && (
                <Button
                  disabled={
                    busy ||
                    Boolean(loginId) ||
                    syncing === cloudKey(account) ||
                    user.role === "auditor"
                  }
                  onClick={() => void syncToCloud(account)}
                >
                  {syncing === cloudKey(account) ? "同步中…" : "同步到服务端"}
                </Button>
              )}
            <Button
              disabled={busy || Boolean(loginId) || user.role === "auditor"}
              onClick={() => void start(account)}
            >
              验证或重新登录
            </Button>
            <Button
              disabled={busy || Boolean(loginId) || user.role === "auditor"}
              onClick={() => setRemoving(account)}
            >
              解绑
            </Button>
          </div>
        ))}
      {loginId ? (
        <section
          className="publish-login"
          aria-label={`${publishPlatformNames[platform]}扫码登录`}
        >
          <h3>{publishPlatformNames[platform]}扫码添加账号</h3>
          {loginStatus.phase === "qr_ready" &&
            loginStatus.image &&
            /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/]+=*$/.test(
              loginStatus.image,
            ) && (
              <img
                className="publish-login__qr"
                src={loginStatus.image}
                alt={`${publishPlatformNames[platform]}登录二维码`}
                onError={() =>
                  setLoginStatus({ ...loadingStatus, phase: "action_required" })
                }
              />
            )}
          <p role="status">
            {pollPaused
              ? "自动检测已暂停，请重试检测或重新获取二维码。"
              : (loginStatus.message ?? loginMessages[loginStatus.phase])}
          </p>
          <div className="publish-login__actions">
            {native &&
              (loginStatus.phase === "action_required" || pollPaused) && (
                <Button
                  disabled={busy || loginStatus.phase === "closed"}
                  onClick={() => void focus()}
                >
                  打开官方窗口
                </Button>
              )}
            {pollPaused && (
              <Button
                disabled={busy}
                onClick={() => setRetryPoll((value) => value + 1)}
              >
                重试检测
              </Button>
            )}
            <Button disabled={busy} onClick={() => void restart()}>
              重新获取二维码
            </Button>
            <Button disabled={busy} onClick={() => void cancel()}>
              取消扫码
            </Button>
          </div>
        </section>
      ) : (
        <Button
          disabled={review || loading || busy || user.role === "auditor"}
          onClick={() => void start()}
        >
          扫码添加账号
        </Button>
      )}
      {removing && (
        <fieldset>
          <legend>确认解绑账号</legend>
          <p>
            确认解绑 {removing.username} 并清除该账号的
            {native ? "本机" : "云端"}登录状态？
          </p>
          <Button disabled={busy} onClick={() => void remove()}>
            确认解绑
          </Button>
          <Button disabled={busy} onClick={() => setRemoving(null)}>
            取消
          </Button>
        </fieldset>
      )}
    </Panel>
  );
}
