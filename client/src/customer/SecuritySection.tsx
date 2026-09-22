import { useEffect, useState } from "react";
import {
  type CustomerSessionCredential,
  type CustomerSessionEvent,
  customerListLoginHistory,
  customerRevokeAllApiKeys,
  customerRevokeAllSessions,
} from "../api";
import { Icon } from "../studio/ui";
import { ChangePasswordForm } from "./ChangePasswordForm";
import { useCustomerConfirm } from "./CustomerConfirmDialog";

// 会话事件的中文说法。服务端只会回 029 里那几个取值；认不出的原样显示，
// 总比显示空白好——这条记录的意义就是「看得见发生过什么」。
const EVENT_LABELS: Record<string, string> = {
  ACTIVATED: "激活账号",
  LOGIN: "登录",
  SWITCH: "切换设备",
  LOGOUT: "退出登录",
  TIMEOUT: "超时下线",
};

// 首屏只给最近几条；要更多就去「全部登录记录」（本轮仍是同一接口的 limit）。
const HISTORY_LIMIT = 5;

const eventLabel = (event: string) => EVENT_LABELS[event] ?? event;

const timeLabel = (value: string) =>
  new Date(value).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false,
  });

/**
 * 账号设置里的「账号安全」（个人中心审计方案 E / 机会点 4）。
 *
 * 三条自救路径各自独立：改密、退出所有设备、撤销全部 Token。前两条会让**当前
 * 登录也失效**——这是单会话架构的应有之义（`customer_session_state` 主键即
 * user_id），所以成功后由 `onSessionsEnded` 把用户带回登录页，而不是留在一个
 * 已经掉线的页面上假装还在。
 */
export function SecuritySection({
  credential,
  activeTokenCount,
  onTokensRevoked,
  onSessionsEnded,
}: {
  credential: () => Promise<CustomerSessionCredential>;
  /** null = Token 列表还没读到（或读取失败）：此时不报数字，绝不拿 0 冒充。 */
  activeTokenCount: number | null;
  onTokensRevoked: () => void;
  onSessionsEnded: (message: string) => void | Promise<void>;
}) {
  const { confirm, dialog } = useCustomerConfirm();
  const [events, setEvents] = useState<CustomerSessionEvent[] | null>(null);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyError, setHistoryError] = useState("");
  const [notice, setNotice] = useState("");
  const [refresh, setRefresh] = useState(0);

  // biome-ignore lint/correctness/useExhaustiveDependencies: Refresh 明确用于在下线动作后重新读取记录。
  useEffect(() => {
    let active = true;
    setHistoryError("");
    void credential()
      .then((auth) => customerListLoginHistory(auth, HISTORY_LIMIT))
      .then((data) => {
        if (!active) return;
        setEvents(data.items);
        setHistoryTotal(data.total);
      })
      .catch((cause: unknown) => {
        if (active) {
          setHistoryError(
            cause instanceof Error ? cause.message : "读取登录记录失败。",
          );
        }
      });
    return () => {
      active = false;
    };
  }, [credential, refresh]);

  function endSessions() {
    confirm({
      title: "退出所有设备？",
      description:
        "立即失效当前账号的全部登录状态（含这台设备），需要用密码重新登录后才能继续。",
      level: "acknowledge",
      confirmLabel: "退出所有设备",
      onConfirm: async () => {
        await customerRevokeAllSessions(await credential());
        setRefresh((value) => value + 1);
        await onSessionsEnded("已退出所有设备，请重新登录。");
      },
    });
  }

  function revokeTokens() {
    // 数字只在真读到列表时才出现：拿 0 冒充会让用户对「同意撤销几枚」的判断失真。
    const countLabel =
      activeTokenCount === null
        ? ""
        : `名下 ${activeTokenCount} 枚可用 Token 会立即失效，`;
    confirm({
      title: "撤销全部 Token？",
      description: `${countLabel}正在调用它们的程序将收到 401。已撤销的记录会保留。`,
      level: "acknowledge",
      confirmLabel: "撤销全部 Token",
      onConfirm: async () => {
        const result = await customerRevokeAllApiKeys(await credential());
        setNotice(`已撤销 ${result.revoked} 枚 Token。`);
        onTokensRevoked();
      },
    });
  }

  return (
    <section className="uc-card uc-security">
      <h2>
        <Icon name="shield" size={18} /> 账号安全
      </h2>
      <p>
        怀疑账号被盗用时，按顺序做三件事：改密码、让所有设备下线、撤销已发出的
        Token。三项操作都不可撤销。
      </p>
      <ChangePasswordForm
        credential={credential}
        onChanged={async (sessionsRevoked) => {
          await onSessionsEnded(
            sessionsRevoked > 0
              ? "密码已修改，其他设备已下线，请用新密码重新登录。"
              : "密码已修改，请用新密码重新登录。",
          );
        }}
      />
      {notice ? (
        <p className="uc-notice" role="status">
          {notice}
          <button type="button" onClick={() => setNotice("")}>
            关闭提示
          </button>
        </p>
      ) : null}
      <div className="uc-security__action">
        <div>
          <strong>退出所有设备</strong>
          <p>当前登录状态立即失效，所有设备都需重新登录。</p>
        </div>
        <button
          className="uc-security__danger"
          onClick={endSessions}
          type="button"
        >
          退出所有设备
        </button>
      </div>
      <div className="uc-security__action">
        <div>
          <strong>撤销全部 Token</strong>
          <p>
            {activeTokenCount === null
              ? "已发出的 Token 会立即失效，程序调用会被拒绝。"
              : `已发出的 ${activeTokenCount} 枚 Token 立即失效，程序调用会被拒绝。`}
          </p>
        </div>
        <button
          className="uc-security__danger"
          onClick={revokeTokens}
          type="button"
        >
          撤销全部 Token
        </button>
      </div>
      <div className="uc-security__history">
        <strong>最近登录</strong>
        {historyError ? (
          <div className="uc-error" role="alert">
            {historyError}
            <button
              type="button"
              onClick={() => setRefresh((value) => value + 1)}
            >
              重新加载
            </button>
          </div>
        ) : events === null ? (
          <p role="status">正在读取登录记录…</p>
        ) : events.length === 0 ? (
          <p>暂无可显示的登录记录。</p>
        ) : (
          <>
            <ul>
              {events.map((item) => (
                <li key={`${item.occurred_at}-${item.event}`}>
                  <span>{timeLabel(item.occurred_at)}</span>
                  <span>{eventLabel(item.event)}</span>
                  <span>
                    {item.device_name || "未知设备"}
                    {item.platform ? ` · ${item.platform}` : ""}
                  </span>
                </li>
              ))}
            </ul>
            {historyTotal > events.length ? (
              <p>
                共 {historyTotal} 条记录，仅显示最近 {events.length} 条。
              </p>
            ) : null}
          </>
        )}
      </div>
      {dialog}
    </section>
  );
}
