import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setAdminCsrfToken } from "../api";
import { TeamManagementSection } from "./TeamManagementSection";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

type Member = {
  user_id: string;
  username: string;
  display_name: string;
  role: string;
  is_active: boolean;
  is_super_admin: boolean;
  has_password: boolean;
  last_login_at: string;
  created_at: string;
};

function member(overrides: Partial<Member> & { user_id: string }): Member {
  return {
    username: overrides.user_id,
    display_name: overrides.user_id,
    role: "admin",
    is_active: true,
    is_super_admin: false,
    has_password: true,
    last_login_at: "",
    created_at: "2026-09-01T00:00:00Z",
    ...overrides,
  };
}

const ME = member({
  user_id: "u-root",
  username: "root",
  display_name: "超管小李",
  is_super_admin: true,
  last_login_at: "2026-09-29T02:00:00Z",
});
const OPS = member({
  user_id: "u-ops",
  username: "ops",
  display_name: "运营小张",
});
const AUDITOR = member({
  user_id: "u-audit",
  username: "audit",
  display_name: "审计小王",
  role: "auditor",
});
const GONE = member({
  user_id: "u-off",
  username: "off",
  display_name: "已离职",
  role: "auditor",
  is_active: false,
});

type Call = {
  method: string;
  url: string;
  body: Record<string, unknown> | null;
  headers: Headers;
};

/** 团队接口的最小替身：写请求按次序消费 `scripted`，否则回成功。 */
function installFetch(options: {
  members?: Member[];
  scripted?: Array<{ status: number; payload: unknown }>;
  listStatus?: number;
}) {
  const members = options.members ?? [ME, OPS, AUDITOR, GONE];
  const scripted = [...(options.scripted ?? [])];
  const calls: Call[] = [];
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const path = url.replace(/^https?:\/\/[^/]+/, "");
    calls.push({
      method,
      url: path,
      body: init?.body
        ? (JSON.parse(String(init.body)) as Record<string, unknown>)
        : null,
      headers: new Headers(init?.headers),
    });
    if (path === "/api/control/team/members" && method === "GET") {
      if (options.listStatus && options.listStatus >= 400) {
        return jsonResponse(
          { detail: "团队成员管理需要 PostgreSQL 运行时。" },
          options.listStatus,
        );
      }
      return jsonResponse({ items: members });
    }
    const write = scripted.shift();
    if (write) {
      return jsonResponse(write.payload, write.status);
    }
    if (path === "/api/control/team/members" && method === "POST") {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      return jsonResponse(
        member({
          user_id: "u-new",
          username: String(body.username),
          display_name: String(body.display_name),
          role: String(body.role),
        }),
        201,
      );
    }
    if (/\/password$/.test(path) && method === "POST") {
      return jsonResponse({
        user_id: "u-ops",
        revoked_sessions: 2,
        request_id: "r-1",
      });
    }
    if (method === "PATCH") {
      return jsonResponse(OPS);
    }
    throw new Error(`unexpected request: ${method} ${path}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    calls,
    writes: () => calls.filter((call) => call.method !== "GET"),
  };
}

async function renderTeam() {
  render(<TeamManagementSection currentUserId="u-root" />);
  await screen.findByRole("table", { name: "团队成员" });
}

function rowOf(username: string) {
  return screen
    .getByText(new RegExp(`^${username}`))
    .closest("tr") as HTMLElement;
}

function submitDialog(
  reason: string,
  options: { ack?: boolean; confirm?: string } = {},
) {
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: reason },
  });
  if (options.ack) {
    fireEvent.click(screen.getByLabelText("我已知晓该操作的影响"));
  }
  fireEvent.click(
    screen.getByRole("button", { name: options.confirm ?? "确认执行" }),
  );
}

describe("TeamManagementSection（P2-4 团队与权限）", () => {
  beforeEach(() => {
    setAdminCsrfToken("csrf-token-1");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    setAdminCsrfToken("");
  });

  it("lists members with role, status and last login", async () => {
    installFetch({});
    await renderTeam();

    const root = rowOf("root");
    expect(within(root).getByText("超级管理员")).toBeInTheDocument();
    expect(within(root).getByText(/2026/)).toBeInTheDocument();
    expect(within(rowOf("ops")).getByText("从未登录")).toBeInTheDocument();
    expect(within(rowOf("audit")).getByText("审计员")).toBeInTheDocument();
    expect(within(rowOf("off")).getByText("已停用")).toBeInTheDocument();
    expect(within(rowOf("ops")).getByText("启用中")).toBeInTheDocument();
  });

  it("gives the signed-in super admin no way to lock themselves out", async () => {
    installFetch({});
    await renderTeam();

    const root = rowOf("root");
    expect(within(root).getByText(/当前账号/)).toBeInTheDocument();
    expect(
      within(root).getByRole("button", { name: "修改显示名 root" }),
    ).toBeInTheDocument();
    // 停用 / 超管标记 / 重置密码在服务端都会被拒；界面直接不给入口。
    expect(
      within(root).queryByRole("button", {
        name: /停用成员|设为超管|取消超管|重置密码/,
      }),
    ).toBeNull();
  });

  it("offers the super-admin flag only for admin accounts", async () => {
    installFetch({});
    await renderTeam();

    expect(
      screen.getByRole("button", { name: "设为超管 ops" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "设为超管 audit" })).toBeNull();
  });

  it("validates a new member locally before any request is sent", async () => {
    const harness = installFetch({});
    await renderTeam();

    fireEvent.click(screen.getByRole("button", { name: "新增成员" }));
    await screen.findByRole("dialog", { name: "新增团队成员" });
    fireEvent.change(screen.getByLabelText("登录名"), {
      target: { value: "newbie" },
    });
    fireEvent.change(screen.getByLabelText("显示名"), {
      target: { value: "新同事" },
    });
    fireEvent.change(screen.getByLabelText(/初始密码/), {
      target: { value: "short" },
    });
    submitDialog("新同事入职", { ack: true, confirm: "确认新增" });

    expect(
      await screen.findByText(/密码需为 12 到 128 个字符/),
    ).toBeInTheDocument();
    expect(harness.writes()).toHaveLength(0);
  });

  it("creates a member with the write contract, reloads, and forgets the password", async () => {
    const harness = installFetch({});
    await renderTeam();

    fireEvent.click(screen.getByRole("button", { name: "新增成员" }));
    await screen.findByRole("dialog", { name: "新增团队成员" });
    fireEvent.change(screen.getByLabelText("登录名"), {
      target: { value: "  newbie  " },
    });
    fireEvent.change(screen.getByLabelText("显示名"), {
      target: { value: "新同事" },
    });
    fireEvent.change(screen.getByLabelText("角色"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByLabelText(/初始密码/), {
      target: { value: "correct-horse-battery" },
    });
    // 新增账号按高危口径：不勾选确认不能提交。
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "新同事入职" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认新增" }));
    expect(await screen.findByText("请先勾选确认操作")).toBeInTheDocument();
    expect(harness.writes()).toHaveLength(0);

    fireEvent.click(screen.getByLabelText("我已知晓该操作的影响"));
    fireEvent.click(screen.getByRole("button", { name: "确认新增" }));

    await waitFor(() => expect(harness.writes()).toHaveLength(1));
    const [post] = harness.writes();
    expect(post.method).toBe("POST");
    expect(post.url).toBe("/api/control/team/members");
    expect(post.body).toEqual({
      username: "newbie",
      display_name: "新同事",
      role: "admin",
      password: "correct-horse-battery",
      confirm: true,
      reason: "新同事入职",
    });
    expect(post.headers.get("X-Admin-CSRF")).toBe("csrf-token-1");
    expect(post.headers.get("Idempotency-Key")).toBeTruthy();

    expect(
      await screen.findByText(/已新增管理员「新同事」（登录名 newbie）/),
    ).toBeInTheDocument();
    // 成功后重新读列表（服务端为准），且下次打开对话框不残留上次的密码。
    await waitFor(() =>
      expect(harness.calls.filter((c) => c.method === "GET")).toHaveLength(2),
    );
    fireEvent.click(screen.getByRole("button", { name: "新增成员" }));
    expect(await screen.findByLabelText(/初始密码/)).toHaveValue("");
    expect(screen.getByLabelText("登录名")).toHaveValue("");
  });

  it("deactivating a member asks for acknowledgement and says sessions are revoked", async () => {
    const harness = installFetch({});
    await renderTeam();

    fireEvent.click(screen.getByRole("button", { name: "停用成员 ops" }));
    await screen.findByRole("dialog", { name: "停用成员：运营小张" });
    expect(screen.getByText(/所有在线会话会立刻被吊销/)).toBeInTheDocument();
    submitDialog("已离职", { ack: true });

    await waitFor(() => expect(harness.writes()).toHaveLength(1));
    const [patch] = harness.writes();
    expect(patch.method).toBe("PATCH");
    expect(patch.url).toBe("/api/control/team/members/u-ops");
    expect(patch.body).toEqual({
      is_active: false,
      confirm: true,
      reason: "已离职",
    });
    expect(
      await screen.findByText(/已停用「运营小张」，其在线会话已全部吊销/),
    ).toBeInTheDocument();
  });

  it("re-enabling only needs a reason (no acknowledgement)", async () => {
    const harness = installFetch({});
    await renderTeam();

    fireEvent.click(screen.getByRole("button", { name: "启用成员 off" }));
    await screen.findByRole("dialog", { name: "启用成员：已离职" });
    expect(screen.queryByLabelText("我已知晓该操作的影响")).toBeNull();
    submitDialog("回聘");

    await waitFor(() => expect(harness.writes()).toHaveLength(1));
    expect(harness.writes()[0].body).toEqual({
      is_active: true,
      confirm: true,
      reason: "回聘",
    });
  });

  it("grants the super-admin flag behind an acknowledgement", async () => {
    const harness = installFetch({});
    await renderTeam();

    fireEvent.click(screen.getByRole("button", { name: "设为超管 ops" }));
    await screen.findByRole("dialog", { name: "设为超级管理员：运营小张" });
    submitDialog("接手团队管理", { ack: true });

    await waitFor(() => expect(harness.writes()).toHaveLength(1));
    expect(harness.writes()[0].body).toEqual({
      is_super_admin: true,
      confirm: true,
      reason: "接手团队管理",
    });
    expect(
      await screen.findByText(/已将「运营小张」设为超级管理员/),
    ).toBeInTheDocument();
  });

  it("renames with the current display name prefilled", async () => {
    const harness = installFetch({});
    await renderTeam();

    fireEvent.click(screen.getByRole("button", { name: "修改显示名 ops" }));
    await screen.findByRole("dialog", { name: "修改显示名：运营小张" });
    expect(screen.getByLabelText("显示名")).toHaveValue("运营小张");
    fireEvent.change(screen.getByLabelText("显示名"), {
      target: { value: "运营主管" },
    });
    submitDialog("岗位调整");

    await waitFor(() => expect(harness.writes()).toHaveLength(1));
    expect(harness.writes()[0].body).toEqual({
      display_name: "运营主管",
      confirm: true,
      reason: "岗位调整",
    });
  });

  it("resets a password with a length check and reports revoked sessions", async () => {
    const harness = installFetch({});
    await renderTeam();

    fireEvent.click(screen.getByRole("button", { name: "重置密码 ops" }));
    await screen.findByRole("dialog", { name: "重置密码：运营小张" });
    fireEvent.change(screen.getByLabelText(/新密码/), {
      target: { value: "tooshort" },
    });
    submitDialog("忘记密码", { ack: true });
    expect(
      await screen.findByText(/密码需为 12 到 128 个字符/),
    ).toBeInTheDocument();
    expect(harness.writes()).toHaveLength(0);

    fireEvent.change(screen.getByLabelText(/新密码/), {
      target: { value: "a-much-longer-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));

    await waitFor(() => expect(harness.writes()).toHaveLength(1));
    const [post] = harness.writes();
    expect(post.url).toBe("/api/control/team/members/u-ops/password");
    expect(post.body).toEqual({
      password: "a-much-longer-secret",
      confirm: true,
      reason: "忘记密码",
    });
    expect(
      await screen.findByText(
        /已重置「运营小张」的密码，其 2 个在线会话已吊销/,
      ),
    ).toBeInTheDocument();
    // 密码不进提示与列表。
    expect(screen.queryByText(/a-much-longer-secret/)).toBeNull();
  });

  it("shows the server's protection message and retries with the same idempotency key", async () => {
    const harness = installFetch({
      scripted: [
        {
          status: 400,
          payload: {
            detail: {
              code: "LAST_SUPER_ADMIN_REQUIRED",
              message: "至少需要保留一个启用中的超级管理员。",
            },
          },
        },
      ],
    });
    await renderTeam();

    fireEvent.click(screen.getByRole("button", { name: "停用成员 ops" }));
    await screen.findByRole("dialog", { name: "停用成员：运营小张" });
    submitDialog("清退", { ack: true });

    expect(
      await screen.findByText(/至少需要保留一个启用中的超级管理员/),
    ).toBeInTheDocument();
    // 失败不关对话框、不重拉列表，也不谎报成功。
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByText(/已停用「运营小张」/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    await waitFor(() => expect(harness.writes()).toHaveLength(2));
    const [first, second] = harness.writes();
    expect(second.headers.get("Idempotency-Key")).toBe(
      first.headers.get("Idempotency-Key"),
    );
  });

  it("surfaces a read failure without showing an empty roster as fact", async () => {
    installFetch({ listStatus: 503 });
    render(<TeamManagementSection currentUserId="u-root" />);

    expect(
      await screen.findByText(
        /读取团队成员失败：团队成员管理需要 PostgreSQL 运行时。（503）/,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("还没有团队成员。")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
  });
});
