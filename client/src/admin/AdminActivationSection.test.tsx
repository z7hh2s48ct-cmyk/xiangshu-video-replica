import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setAdminCsrfToken } from "../api";
import { AdminActivationSection } from "./AdminActivationSection";

const adminActor = {
  user_id: "admin-1",
  username: "admin",
  display_name: "管理员一号",
  role: "admin",
};

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

function installFetch() {
  const fetchMock = vi.fn((url: string, options?: RequestInit) => {
    if (
      url.endsWith("/api/control/activation-code-batches") &&
      options?.method === "POST"
    ) {
      return jsonResponse({ batch_id: "batch-1" });
    }
    if (url.includes("/activation-code-batches/batch-1/generate")) {
      return jsonResponse({ export_id: "export-1" });
    }
    if (url.includes("/activation-code-exports/export-1/download")) {
      return jsonResponse({ codes: ["XS04-TESTCODE"] });
    }
    if (url.includes("/api/control/activation-codes")) {
      return jsonResponse({ items: [], total: 0, limit: 100, offset: 0 });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  setAdminCsrfToken("csrf-test");
  vi.stubGlobal("URL", {
    createObjectURL: vi.fn(() => "blob:test"),
    revokeObjectURL: vi.fn(),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AdminActivationSection（创建即激活）", () => {
  it("renders the create-then-activate form and the code list without issuance UI", () => {
    installFetch();
    render(
      <AdminActivationSection actor={adminActor} onSessionExpired={vi.fn()} />,
    );

    expect(
      screen.getByRole("heading", { name: "生成激活码" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/创建即可激活并交付客户，无发放动作/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "激活码列表" }),
    ).toBeInTheDocument();
    // 发放与批次概念不再出现在界面上。
    expect(screen.queryByText(/发放激活码/)).toBeNull();
    expect(screen.queryByRole("button", { name: /发放/ })).toBeNull();
  });

  it("rejects a submission without the mandatory reason", async () => {
    const fetchMock = installFetch();
    render(
      <AdminActivationSection actor={adminActor} onSessionExpired={vi.fn()} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "生成激活码" }));

    expect(await screen.findByText("请填写操作原因")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("/activation-code-batches"),
      ),
    ).toBe(false);
  });

  it("requires confirmation before issuing codes with initial free seconds", async () => {
    const fetchMock = installFetch();
    render(
      <AdminActivationSection actor={adminActor} onSessionExpired={vi.fn()} />,
    );

    fireEvent.change(screen.getByLabelText(/初始秒数/), {
      target: { value: "600" },
    });
    fireEvent.change(screen.getByLabelText(/操作原因/), {
      target: { value: "客户续费交付" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成激活码" }));

    expect(
      await screen.findByText("请确认初始秒数为免费赠送，不产生收款收入"),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("/activation-code-batches"),
      ),
    ).toBe(false);
    fireEvent.click(screen.getByRole("checkbox", { name: /确认免费赠送/ }));
    fireEvent.click(screen.getByRole("button", { name: "生成激活码" }));
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) =>
        String(url).endsWith("/activation-code-batches"),
      );
      expect(call).toBeDefined();
      expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
        face_value_fen: 0,
        credits: 600,
        confirm_grant: true,
      });
    });
  });

  it("generates codes with auto-issue, configurable expiry, credits and the operator reason", async () => {
    const fetchMock = installFetch();
    render(
      <AdminActivationSection actor={adminActor} onSessionExpired={vi.fn()} />,
    );

    fireEvent.change(screen.getByLabelText(/数量/), { target: { value: "5" } });
    fireEvent.change(screen.getByLabelText(/操作原因/), {
      target: { value: "客户续费交付" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成激活码" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) => String(url).includes("/generate")),
      ).toBe(true);
    });

    const generateCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/generate"),
    );
    expect(JSON.parse(String(generateCall?.[1]?.body))).toMatchObject({
      quantity: 5,
      auto_issue: true,
      reason: "客户续费交付",
    });

    const createCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url).endsWith("/api/control/activation-code-batches") &&
        options?.method === "POST",
    );
    const createBody = JSON.parse(String(createCall?.[1]?.body));
    expect(createBody.credits).toBe(0);
    expect(createBody.face_value_fen).toBe(0);
    expect(createBody.quantity).toBe(5);
    expect(createBody.reason).toBe("客户续费交付");
    expect(createBody.activation_expires_at).toBeTruthy();

    expect(await screen.findByText(/明文仅此一次展示/)).toBeInTheDocument();
    expect(screen.getByText("XS04-TESTCODE")).toBeInTheDocument();
  });

  it("marks auditor sessions as read-only", () => {
    installFetch();
    render(
      <AdminActivationSection
        actor={{ ...adminActor, role: "auditor" }}
        onSessionExpired={vi.fn()}
      />,
    );

    expect(screen.getByText(/审计员只读/)).toBeInTheDocument();
    expect(
      screen.getByText(/当前为只读模式，不能生成激活码/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "生成激活码" })).toBeNull();
  });
});
