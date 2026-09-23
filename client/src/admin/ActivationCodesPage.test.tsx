import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  type ActivationCodePage,
  clearAdminActivationSession,
  exchangeAdminSession,
} from "../api.admin";
import { ActivationCodesPage } from "./ActivationCodesPage";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

const CSRF_TOKEN_TEXT = "csrf-token-1";

const exchangePayload = {
  session_id: "session-1",
  expires_at: "2026-08-23T20:00:00+00:00",
  csrf_token: CSRF_TOKEN_TEXT,
  actor: {
    user_id: "admin-1",
    username: "admin",
    display_name: "管理员一号",
    role: "admin",
  },
};

const codesPage: ActivationCodePage = {
  items: [
    {
      code_id: "code-1",
      batch_id: "batch-1",
      masked_code: "XS****01",
      status: "GENERATED",
      bound_user_id: null,
      bound_username: null,
      issued_at: null,
      archived_at: null,
      devices: [],
      pending_pairings: [],
    },
    {
      code_id: "code-2",
      batch_id: "batch-1",
      masked_code: "XS****02",
      status: "ACTIVE",
      bound_user_id: "user-9",
      bound_username: "customer_9",
      issued_at: "2026-08-20T10:00:00+00:00",
      archived_at: null,
      devices: [
        {
          device_id: "device-1",
          slot_no: 1,
          display_name: "办公室电脑",
          platform: "windows",
          status: "BOUND",
          bound_at: "2026-08-20T10:05:00+00:00",
          last_active_at: "2026-08-21T10:05:00+00:00",
          unbound_at: null,
          revoked_at: null,
        },
      ],
      pending_pairings: [
        {
          pairing_request_id: "pairing-1",
          display_name: "新办公室电脑",
          platform: "windows",
          status: "PENDING",
          created_at: "2026-08-22T10:00:00+00:00",
          expires_at: "2026-08-22T10:15:00+00:00",
        },
      ],
    },
    {
      code_id: "code-3",
      batch_id: "batch-1",
      masked_code: "XS****03",
      status: "REVOKED",
      bound_user_id: "user-10",
      bound_username: "customer_10",
      issued_at: "2026-08-19T10:00:00+00:00",
      archived_at: null,
      devices: [],
      pending_pairings: [],
    },
  ],
  total: 3,
  limit: 50,
  offset: 0,
};

function installFetch(options?: {
  list?: "ok" | "unauthorized";
  suspendCodeId?: string;
  resumeCodeId?: string;
  items?: typeof codesPage.items;
}) {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith("/api/control/admin/session/exchange")) {
      return jsonResponse(exchangePayload);
    }
    if (url.includes("/api/control/activation-codes?")) {
      if (options?.list === "unauthorized") {
        return jsonResponse(
          {
            detail: {
              code: "ADMIN_SESSION_INVALID",
              message: "Admin session is missing, revoked or invalid.",
            },
          },
          401,
        );
      }
      return jsonResponse({
        ...codesPage,
        items: options?.items ?? codesPage.items,
      });
    }
    if (
      url.endsWith("/activation-codes/code-1/reveal") &&
      init?.method === "POST"
    ) {
      return jsonResponse({
        code_id: "code-1",
        activation_code: "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD",
        masked_code: "XS****01",
        request_id: "req-reveal-1",
      });
    }
    if (
      url.endsWith("/activation-codes/code-2/revoke") &&
      init?.method === "POST"
    ) {
      return jsonResponse({
        code_id: "code-2",
        status: "REVOKED",
        request_id: "req-revoke-1",
      });
    }
    if (
      options?.suspendCodeId &&
      url.endsWith(`/activation-codes/${options.suspendCodeId}/suspend`) &&
      init?.method === "POST"
    ) {
      return jsonResponse({
        code_id: options.suspendCodeId,
        status: "SUSPENDED",
        request_id: "req-suspend-1",
      });
    }
    if (
      options?.resumeCodeId &&
      url.endsWith(`/activation-codes/${options.resumeCodeId}/resume`) &&
      init?.method === "POST"
    ) {
      return jsonResponse({
        code_id: options.resumeCodeId,
        status: "ACTIVE",
        request_id: "req-resume-1",
      });
    }
    if (
      url.endsWith("/activation-codes/code-3/archive") &&
      init?.method === "POST"
    ) {
      return jsonResponse({
        code_id: "code-3",
        archived_at: "2026-08-23T10:00:00+00:00",
        request_id: "req-archive-1",
      });
    }
    if (
      url.endsWith("/device-pairings/pairing-1/replace-device") &&
      init?.method === "POST"
    ) {
      return jsonResponse({
        pairing_id: "pairing-1",
        status: "APPROVED",
        replaced_device_id: "device-1",
        request_id: "req-replace-1",
      });
    }
    if (
      url.endsWith("/device-pairings/pairing-4/approve") &&
      init?.method === "POST"
    ) {
      return jsonResponse({
        pairing_id: "pairing-4",
        status: "APPROVED",
        request_id: "req-approve-4",
      });
    }
    if (url.endsWith("/devices/device-1/unbind") && init?.method === "POST") {
      return jsonResponse({
        device_id: "device-1",
        status: "UNBOUND",
        outcome: "unbound",
        request_id: "req-unbind-1",
      });
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("ActivationCodesPage", () => {
  beforeEach(async () => {
    installFetch();
    await exchangeAdminSession("ASX1.body.signature");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearAdminActivationSession();
  });

  it("shows activation codes and accounts without device binding or pairing controls", async () => {
    render(<ActivationCodesPage />);
    expect(await screen.findByText("XS****01")).toBeInTheDocument();
    expect(screen.getByText("customer_9")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "激活码列表" }),
    ).toBeInTheDocument();
    expect(screen.getByText("共 3 条")).toBeInTheDocument();
    expect(
      screen.queryByRole("columnheader", { name: "关联设备" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("table", { name: "待批准配对列表" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("办公室电脑")).not.toBeInTheDocument();
  });

  it("copies a code through the audited reveal route", async () => {
    const fetchMock = installFetch();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(<ActivationCodesPage />);
    fireEvent.click(
      (await screen.findAllByRole("button", { name: "复制" }))[0],
    );
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).endsWith("/activation-codes/code-1/reveal"),
      ),
    ).toBe(false);
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "交付客户首次激活" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认复制" }));

    expect(await screen.findByText(/req-reveal-1/)).toBeInTheDocument();
    expect(writeText).toHaveBeenCalledWith(
      "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD",
    );
    const revealCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/activation-codes/code-1/reveal"),
    );
    expect(revealCall?.[1]?.body).toBe(
      JSON.stringify({ confirm: true, reason: "交付客户首次激活" }),
    );
  });

  it("does not offer lifecycle mutations for an expired code", async () => {
    const expired = {
      ...codesPage.items[0],
      code_id: "code-expired",
      masked_code: "XS****EX",
      status: "EXPIRED",
    };
    installFetch({ items: [expired] });
    render(<ActivationCodesPage />);

    const row = (await screen.findByText("XS****EX")).closest("tr");
    expect(row).not.toBeNull();
    expect(
      within(row as HTMLElement).getByText("无需操作"),
    ).toBeInTheDocument();
    expect(
      within(row as HTMLElement).queryByRole("button", { name: "撤销激活码" }),
    ).toBeNull();
    expect(
      within(row as HTMLElement).queryByRole("button", { name: "恢复" }),
    ).toBeNull();
  });

  it("revokes a code with reason and explicit confirmation", async () => {
    const fetchMock = installFetch();
    render(<ActivationCodesPage />);

    fireEvent.click(
      (await screen.findAllByRole("button", { name: "撤销激活码" }))[1],
    );
    // 吊销走 reasonAndAck 级别：原因 + 我已知晓勾选都在对话框里。
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客户申请停用" },
    });
    fireEvent.click(screen.getByLabelText("我已知晓该操作的影响"));
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));

    expect(await screen.findByText(/req-revoke-1/)).toBeInTheDocument();
    expect(screen.getAllByRole("cell", { name: "已撤销" })).toHaveLength(2);
    const revokeCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/activation-codes/code-2/revoke"),
    );
    expect(revokeCall?.[1]?.body).toBe(
      JSON.stringify({ confirm: true, reason: "客户申请停用" }),
    );
  });

  it("suspends and resumes a code through the new lifecycle buttons", async () => {
    const fetchMock = installFetch({
      suspendCodeId: "code-2",
      resumeCodeId: "code-2",
    });
    render(<ActivationCodesPage />);

    // ACTIVE 码出现"暂停"按钮。
    fireEvent.click(await screen.findByRole("button", { name: "暂停" }));
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客户欠费临时停用" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));

    expect(await screen.findByText(/req-suspend-1/)).toBeInTheDocument();

    // SUSPENDED 码出现"恢复"按钮。
    fireEvent.click(await screen.findByRole("button", { name: "恢复" }));
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "欠费结清恢复使用" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));

    expect(await screen.findByText(/req-resume-1/)).toBeInTheDocument();
    const suspendCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/activation-codes/code-2/suspend"),
    );
    expect(suspendCall?.[1]?.body).toBe(
      JSON.stringify({ confirm: true, reason: "客户欠费临时停用" }),
    );
  });

  it("archives a revoked activation code without deleting its audit history", async () => {
    const fetchMock = installFetch();
    render(<ActivationCodesPage />);

    fireEvent.click(await screen.findByRole("button", { name: "删除激活码" }));
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "清理已撤销测试码" },
    });
    fireEvent.click(screen.getByLabelText("我已知晓该操作的影响"));
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));

    expect(await screen.findByText(/req-archive-1/)).toBeInTheDocument();
    expect(screen.queryByText("XS****03")).toBeNull();
    const archiveCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/activation-codes/code-3/archive"),
    );
    expect(archiveCall?.[1]?.body).toBe(
      JSON.stringify({ confirm: true, reason: "清理已撤销测试码" }),
    );
  });

  it("searches server-side and requests the selected status", async () => {
    const fetchMock = installFetch();
    render(<ActivationCodesPage />);
    await screen.findByText("XS****01");

    // A9：搜索在提交时下沉到服务端，不再客户端过滤当前页。
    fireEvent.change(screen.getByLabelText("搜索"), {
      target: { value: "customer_9" },
    });
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("search=customer_9"),
        ),
      ).toBe(true),
    );

    // 搜索词已应用，随后切换状态会带着同一检索词请求。
    fireEvent.change(screen.getByLabelText("状态"), {
      target: { value: "ACTIVE" },
    });
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).endsWith(
            "/api/control/activation-codes?status=ACTIVE&search=customer_9&limit=50&offset=0",
          ),
        ),
      ).toBe(true),
    );
  });

  it("reports an expired session and hides writes for auditors", async () => {
    installFetch({ list: "unauthorized" });
    const onSessionExpired = vi.fn();
    const { unmount } = render(
      <ActivationCodesPage onSessionExpired={onSessionExpired} />,
    );
    expect(
      await screen.findByText(/会话已失效，请重新登录/),
    ).toBeInTheDocument();
    expect(onSessionExpired).toHaveBeenCalled();
    unmount();

    installFetch();
    render(<ActivationCodesPage readOnly />);
    expect(await screen.findByText("XS****01")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "复制" })).toBeNull();
    expect(screen.queryByRole("button", { name: "撤销激活码" })).toBeNull();
    expect(screen.getByText(/当前为只读模式/)).toBeInTheDocument();
  });
});
