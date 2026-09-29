import { useCallback, useEffect, useRef, useState } from "react";

import {
  adminActivationErrorMessage,
  createTeamMember,
  listTeamMembers,
  resetTeamMemberPassword,
  type TeamMember,
  type TeamRole,
  updateTeamMember,
} from "../api.admin";
import { ConfirmDialog, type ConfirmLevel } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { StatusBadge } from "./ui/StatusBadge";
import { formatDateTime, roleLabel } from "./ui/vocabulary";

// 与服务端 admin_auth_routes / admin_team_routes 同口径。前端先拦一道只为
// 少一次往返，服务端才是真正的校验方，也是唯一的自我保护与「最后一个超管」防线。
const MIN_PASSWORD_LENGTH = 12;
const MAX_PASSWORD_LENGTH = 128;
const MAX_USERNAME_LENGTH = 128;
const MAX_DISPLAY_NAME_LENGTH = 80;

type PendingAction =
  | { kind: "create" }
  | { kind: "rename"; member: TeamMember }
  | { kind: "toggleActive"; member: TeamMember }
  | { kind: "toggleSuper"; member: TeamMember }
  | { kind: "resetPassword"; member: TeamMember };

interface CreateDraft {
  username: string;
  displayName: string;
  role: TeamRole;
  password: string;
}

const EMPTY_CREATE_DRAFT: CreateDraft = {
  username: "",
  displayName: "",
  role: "auditor",
  password: "",
};

function memberName(member: TeamMember): string {
  return member.display_name || member.username;
}

function passwordProblem(password: string): string | null {
  if (
    password.length < MIN_PASSWORD_LENGTH ||
    password.length > MAX_PASSWORD_LENGTH
  ) {
    return `密码需为 ${MIN_PASSWORD_LENGTH} 到 ${MAX_PASSWORD_LENGTH} 个字符`;
  }
  if (!password.trim()) {
    return "密码不能全为空白";
  }
  return null;
}

function dialogLevel(action: PendingAction): ConfirmLevel {
  switch (action.kind) {
    // 新增账号 / 授予超管 / 停用（踢下线）/ 重置密码都改变谁能进后台，
    // 按高危口径要求勾选确认；改名与重新启用只要求原因。
    case "create":
    case "toggleSuper":
    case "resetPassword":
      return "reasonAndAck";
    case "toggleActive":
      return action.member.is_active ? "reasonAndAck" : "reason";
    case "rename":
      return "reason";
  }
}

function dialogTitle(action: PendingAction): string {
  switch (action.kind) {
    case "create":
      return "新增团队成员";
    case "rename":
      return `修改显示名：${memberName(action.member)}`;
    case "toggleActive":
      return action.member.is_active
        ? `停用成员：${memberName(action.member)}`
        : `启用成员：${memberName(action.member)}`;
    case "toggleSuper":
      return action.member.is_super_admin
        ? `取消超级管理员：${memberName(action.member)}`
        : `设为超级管理员：${memberName(action.member)}`;
    case "resetPassword":
      return `重置密码：${memberName(action.member)}`;
  }
}

function dialogDescription(action: PendingAction): string {
  switch (action.kind) {
    case "create":
      return "新成员用这里设置的登录名与初始密码登录；管理员可以改动业务数据，审计员只读。";
    case "rename":
      return "只改显示名，不影响登录名与权限。";
    case "toggleActive":
      return action.member.is_active
        ? "停用后该成员的所有在线会话会立刻被吊销，需要重新启用才能再次登录。不提供删除，历史审计记录保留。"
        : "启用后该成员可以用原密码重新登录。";
    case "toggleSuper":
      return action.member.is_super_admin
        ? "取消后该成员不再能管理团队成员，也看不到技术配置类页面。"
        : "超级管理员可以新增、停用成员并重置他人密码。";
    case "resetPassword":
      return "旧密码立即失效，该成员所有在线会话会被吊销；请通过安全渠道把新密码告知本人。";
  }
}

/**
 * 团队与权限（方案 P2-4）：管理端成员的新增、停用启用、超管标记与重置密码。
 *
 * 服务端整组接口都是超级管理员专属，并自带三条防线：不能停用自己、不能改
 * 自己的超管标记、不能移除最后一个启用中的超管。界面对「自己」那一行直接
 * 不给危险按钮，其余情形交给服务端的中文错误说明——不在前端复刻一遍规则。
 * 没有物理删除：审计与历史幂等记录都引用成员 id。
 */
export function TeamManagementSection({
  currentUserId,
}: {
  /** 当前登录的超管：对自己那一行只允许改显示名。 */
  currentUserId: string;
}) {
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [dialogError, setDialogError] = useState("");
  const [busy, setBusy] = useState(false);
  const [createDraft, setCreateDraft] =
    useState<CreateDraft>(EMPTY_CREATE_DRAFT);
  const [displayNameDraft, setDisplayNameDraft] = useState("");
  const [passwordDraft, setPasswordDraft] = useState("");
  // 同一份内容失败后重试沿用同一幂等键：新增成员若首个请求已落库而响应丢了，
  // 换键重试只会撞上「登录名已被使用」；改了内容才换键。
  const retryRef = useRef<{ fingerprint: string; key: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setMembers((await listTeamMembers()).items);
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "读取团队成员失败"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function open(action: PendingAction) {
    setDialogError("");
    setNotice("");
    retryRef.current = null;
    setCreateDraft(EMPTY_CREATE_DRAFT);
    setPasswordDraft("");
    setDisplayNameDraft(
      action.kind === "rename" ? action.member.display_name : "",
    );
    setPending(action);
  }

  function close() {
    setPending(null);
    setDialogError("");
    // 密码不在界面里多留一刻。
    setCreateDraft(EMPTY_CREATE_DRAFT);
    setPasswordDraft("");
  }

  function keyFor(fingerprint: string): string {
    if (retryRef.current?.fingerprint !== fingerprint) {
      retryRef.current = { fingerprint, key: crypto.randomUUID() };
    }
    return retryRef.current.key;
  }

  async function execute(action: PendingAction, reason: string) {
    if (busy) {
      return;
    }
    setDialogError("");
    // 本地先做和服务端同口径的必填检查，出错留在对话框里（不关闭、不发请求）。
    let run: () => Promise<string>;
    switch (action.kind) {
      case "create": {
        const username = createDraft.username.trim();
        const displayName = createDraft.displayName.trim();
        if (!username || username.length > MAX_USERNAME_LENGTH) {
          setDialogError(`请填写登录名（最多 ${MAX_USERNAME_LENGTH} 个字符）`);
          return;
        }
        if (!displayName || displayName.length > MAX_DISPLAY_NAME_LENGTH) {
          setDialogError(
            `请填写显示名（最多 ${MAX_DISPLAY_NAME_LENGTH} 个字符）`,
          );
          return;
        }
        const problem = passwordProblem(createDraft.password);
        if (problem) {
          setDialogError(problem);
          return;
        }
        const fields = {
          username,
          display_name: displayName,
          role: createDraft.role,
          password: createDraft.password,
        };
        const key = keyFor(JSON.stringify(["create", fields]));
        run = async () => {
          const created = await createTeamMember(fields, reason, key);
          return `已新增${roleLabel(created.role)}「${memberName(created)}」（登录名 ${created.username}）。`;
        };
        break;
      }
      case "rename": {
        const displayName = displayNameDraft.trim();
        if (!displayName || displayName.length > MAX_DISPLAY_NAME_LENGTH) {
          setDialogError(
            `请填写显示名（最多 ${MAX_DISPLAY_NAME_LENGTH} 个字符）`,
          );
          return;
        }
        const key = keyFor(
          JSON.stringify(["rename", action.member.user_id, displayName]),
        );
        run = async () => {
          await updateTeamMember(
            action.member.user_id,
            { display_name: displayName },
            reason,
            key,
          );
          return "显示名已更新。";
        };
        break;
      }
      case "toggleActive": {
        const nextActive = !action.member.is_active;
        const key = keyFor(
          JSON.stringify(["active", action.member.user_id, nextActive]),
        );
        run = async () => {
          await updateTeamMember(
            action.member.user_id,
            { is_active: nextActive },
            reason,
            key,
          );
          return nextActive
            ? `已启用「${memberName(action.member)}」。`
            : `已停用「${memberName(action.member)}」，其在线会话已全部吊销。`;
        };
        break;
      }
      case "toggleSuper": {
        const nextSuper = !action.member.is_super_admin;
        const key = keyFor(
          JSON.stringify(["super", action.member.user_id, nextSuper]),
        );
        run = async () => {
          await updateTeamMember(
            action.member.user_id,
            { is_super_admin: nextSuper },
            reason,
            key,
          );
          return nextSuper
            ? `已将「${memberName(action.member)}」设为超级管理员。`
            : `已取消「${memberName(action.member)}」的超级管理员。`;
        };
        break;
      }
      case "resetPassword": {
        const problem = passwordProblem(passwordDraft);
        if (problem) {
          setDialogError(problem);
          return;
        }
        const key = keyFor(
          JSON.stringify(["password", action.member.user_id, passwordDraft]),
        );
        run = async () => {
          const result = await resetTeamMemberPassword(
            action.member.user_id,
            passwordDraft,
            reason,
            key,
          );
          return `已重置「${memberName(action.member)}」的密码，其 ${result.revoked_sessions} 个在线会话已吊销。`;
        };
        break;
      }
    }

    setBusy(true);
    try {
      const message = await run();
      retryRef.current = null;
      setNotice(message);
      close();
      await load();
    } catch (cause) {
      setDialogError(adminActivationErrorMessage(cause, "操作失败，请重试"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="团队与权限" className="admin-panel">
      <header className="admin-alerts__header">
        <h2>团队与权限</h2>
        <div className="admin-actions">
          <button disabled={loading} type="button" onClick={() => void load()}>
            刷新
          </button>
          <button type="button" onClick={() => open({ kind: "create" })}>
            新增成员
          </button>
        </div>
      </header>
      <p className="admin-hint">
        管理端的管理员与审计员账号。仅超级管理员可见；成员只能停用、不能删除，
        停用会立即把对方踢下线。
      </p>

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {loading && members.length === 0 ? (
        <p className="admin-hint">正在读取团队成员…</p>
      ) : null}
      {!loading && !error && members.length === 0 ? (
        <p className="admin-hint">还没有团队成员。</p>
      ) : null}

      {members.length > 0 ? (
        <div className="admin-table-scroll">
          <table aria-label="团队成员" className="admin-data-table">
            <thead>
              <tr>
                <th scope="col">登录名</th>
                <th scope="col">显示名</th>
                <th scope="col">角色</th>
                <th scope="col">状态</th>
                <th scope="col">最近登录</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {members.map((member) => {
                const isSelf = member.user_id === currentUserId;
                return (
                  <tr key={member.user_id}>
                    <td data-label="登录名">
                      {member.username}
                      {isSelf ? " （当前账号）" : ""}
                    </td>
                    <td data-label="显示名">{member.display_name}</td>
                    <td data-label="角色">
                      {roleLabel(member.role)}
                      {member.is_super_admin ? (
                        <>
                          {" "}
                          <StatusBadge tone="info">超级管理员</StatusBadge>
                        </>
                      ) : null}
                    </td>
                    <td data-label="状态">
                      {member.is_active ? (
                        <StatusBadge tone="good">启用中</StatusBadge>
                      ) : (
                        <StatusBadge tone="neutral">已停用</StatusBadge>
                      )}
                    </td>
                    <td data-label="最近登录">
                      {member.last_login_at
                        ? formatDateTime(member.last_login_at)
                        : "从未登录"}
                    </td>
                    <td data-label="操作">
                      <div className="admin-actions admin-actions--table">
                        <button
                          aria-label={`修改显示名 ${member.username}`}
                          type="button"
                          onClick={() => open({ kind: "rename", member })}
                        >
                          改名
                        </button>
                        {isSelf ? null : (
                          <>
                            <button
                              aria-label={`${member.is_active ? "停用" : "启用"}成员 ${member.username}`}
                              type="button"
                              onClick={() =>
                                open({ kind: "toggleActive", member })
                              }
                            >
                              {member.is_active ? "停用" : "启用"}
                            </button>
                            {member.role === "admin" ? (
                              <button
                                aria-label={`${member.is_super_admin ? "取消超管" : "设为超管"} ${member.username}`}
                                type="button"
                                onClick={() =>
                                  open({ kind: "toggleSuper", member })
                                }
                              >
                                {member.is_super_admin
                                  ? "取消超管"
                                  : "设为超管"}
                              </button>
                            ) : null}
                            <button
                              aria-label={`重置密码 ${member.username}`}
                              type="button"
                              onClick={() =>
                                open({ kind: "resetPassword", member })
                              }
                            >
                              重置密码
                            </button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}

      <ConfirmDialog
        busy={busy}
        confirmLabel={pending?.kind === "create" ? "确认新增" : "确认执行"}
        description={pending ? dialogDescription(pending) : undefined}
        error={dialogError}
        level={pending ? dialogLevel(pending) : "reason"}
        open={pending !== null}
        title={pending ? dialogTitle(pending) : ""}
        onClose={close}
        onConfirm={(reason) => {
          if (pending) {
            void execute(pending, reason);
          }
        }}
      >
        {pending?.kind === "create" ? (
          <>
            <label>
              登录名
              <input
                autoComplete="off"
                value={createDraft.username}
                onChange={(event) =>
                  setCreateDraft({
                    ...createDraft,
                    username: event.target.value,
                  })
                }
              />
            </label>
            <label>
              显示名
              <input
                autoComplete="off"
                value={createDraft.displayName}
                onChange={(event) =>
                  setCreateDraft({
                    ...createDraft,
                    displayName: event.target.value,
                  })
                }
              />
            </label>
            <label>
              角色
              <select
                value={createDraft.role}
                onChange={(event) =>
                  setCreateDraft({
                    ...createDraft,
                    role: event.target.value as TeamRole,
                  })
                }
              >
                <option value="auditor">审计员（只读）</option>
                <option value="admin">管理员</option>
              </select>
            </label>
            <label>
              初始密码（{MIN_PASSWORD_LENGTH}–{MAX_PASSWORD_LENGTH} 个字符）
              <input
                autoComplete="new-password"
                type="password"
                value={createDraft.password}
                onChange={(event) =>
                  setCreateDraft({
                    ...createDraft,
                    password: event.target.value,
                  })
                }
              />
            </label>
          </>
        ) : null}
        {pending?.kind === "rename" ? (
          <label>
            显示名
            <input
              autoComplete="off"
              value={displayNameDraft}
              onChange={(event) => setDisplayNameDraft(event.target.value)}
            />
          </label>
        ) : null}
        {pending?.kind === "resetPassword" ? (
          <label>
            新密码（{MIN_PASSWORD_LENGTH}–{MAX_PASSWORD_LENGTH} 个字符）
            <input
              autoComplete="new-password"
              type="password"
              value={passwordDraft}
              onChange={(event) => setPasswordDraft(event.target.value)}
            />
          </label>
        ) : null}
      </ConfirmDialog>
    </section>
  );
}
