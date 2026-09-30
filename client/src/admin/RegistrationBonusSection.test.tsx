import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setAdminCsrfToken } from "../api";
import { RegistrationBonusSection } from "./RegistrationBonusSection";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

function settingsPayload(overrides: Record<string, unknown> = {}) {
  return {
    bonus_credits: 0,
    updated_by_user_id: null,
    updated_by_display_name: null,
    updated_at: null,
    ...overrides,
  };
}

interface Harness {
  fetchMock: ReturnType<typeof vi.fn>;
  puts: () => Array<{ body: Record<string, unknown>; headers: Headers }>;
}

/**
 * 内存里的设置行：PUT 成功后 GET 读到新值，与服务端「全量覆盖」语义一致。
 * `putResponses` 按次序消费，用来模拟先失败后成功。
 */
function installFetch(options: {
  settings?: Record<string, unknown>;
  putResponses?: Array<{ status: number; payload: unknown }>;
}): Harness {
  let current = options.settings ?? settingsPayload();
  const putResponses = [...(options.putResponses ?? [])];
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (
      url.endsWith("/api/control/settings/registration-bonus") &&
      method === "GET"
    ) {
      return jsonResponse(current);
    }
    if (
      url.endsWith("/api/control/settings/registration-bonus") &&
      method === "PUT"
    ) {
      const scripted = putResponses.shift();
      if (scripted) {
        return jsonResponse(scripted.payload, scripted.status);
      }
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      current = settingsPayload({
        bonus_credits: body.bonus_credits,
        updated_by_user_id: "admin-1",
        updated_by_display_name: "运营一号",
        updated_at: "2026-09-30T12:00:00Z",
      });
      return jsonResponse(current);
    }
    throw new Error(`unexpected request: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    fetchMock,
    puts: () =>
      fetchMock.mock.calls
        .filter(
          ([, init]) => (init as RequestInit | undefined)?.method === "PUT",
        )
        .map(([, init]) => ({
          body: JSON.parse(String((init as RequestInit).body)) as Record<
            string,
            unknown
          >,
          headers: new Headers((init as RequestInit).headers),
        })),
  };
}

describe("RegistrationBonusSection（注册赠送积分设置）", () => {
  beforeEach(() => {
    setAdminCsrfToken("csrf-token-1");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    setAdminCsrfToken("");
  });

  it("shows the current credits and keeps save disabled until something changes", async () => {
    installFetch({ settings: settingsPayload({ bonus_credits: 30 }) });
    render(<RegistrationBonusSection />);

    const input = await screen.findByLabelText("注册赠送积分（0 表示关闭）");
    expect(input).toHaveValue("30");
    expect(
      screen.getByRole("button", { name: "保存注册赠送设置" }),
    ).toBeDisabled();

    fireEvent.change(input, { target: { value: "50" } });
    expect(
      screen.getByRole("button", { name: "保存注册赠送设置" }),
    ).toBeEnabled();
  });

  it("tells operators the grant only covers new master accounts", async () => {
    installFetch({});
    render(<RegistrationBonusSection />);

    // 生效范围必须说清：子账号不发放、存量不补发，运营不能误读为补发。
    expect(await screen.findByText(/仅主账号注册时发放/)).toBeInTheDocument();
    expect(screen.getByText(/已注册账号不补发/)).toBeInTheDocument();
    expect(screen.getByText(/填 0 表示关闭赠送/)).toBeInTheDocument();
  });

  it.each([
    ["-1", /需为 0 到 2147483647 之间的整数/],
    ["1.5", /需为 0 到 2147483647 之间的整数/],
    ["abc", /需为 0 到 2147483647 之间的整数/],
    ["", /需为 0 到 2147483647 之间的整数/],
  ])(
    "blocks credits = %s locally and never opens the dialog",
    async (value, message) => {
      const harness = installFetch({});
      render(<RegistrationBonusSection />);
      const input = await screen.findByLabelText("注册赠送积分（0 表示关闭）");

      fireEvent.change(input, { target: { value } });
      fireEvent.click(screen.getByRole("button", { name: "保存注册赠送设置" }));

      expect(await screen.findByText(message)).toBeInTheDocument();
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(harness.puts()).toHaveLength(0);
    },
  );

  it("saves through the confirm dialog with the write contract", async () => {
    const harness = installFetch({});
    render(<RegistrationBonusSection />);
    const input = await screen.findByLabelText("注册赠送积分（0 表示关闭）");

    fireEvent.change(input, { target: { value: "20" } });
    fireEvent.click(screen.getByRole("button", { name: "保存注册赠送设置" }));

    const dialog = await screen.findByRole("dialog", {
      name: "保存注册赠送设置",
    });
    expect(dialog).toHaveTextContent(/一次性获得 20 积分/);
    // 原因必填：审计里的「为什么改」必须是操作人写的，不是固定文字。
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
    expect(await screen.findByText("请填写操作原因")).toBeInTheDocument();
    expect(harness.puts()).toHaveLength(0);

    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "新客获客活动，注册送 20" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

    await waitFor(() => expect(harness.puts()).toHaveLength(1));
    const [put] = harness.puts();
    expect(put.body).toEqual({
      bonus_credits: 20,
      confirm: true,
      reason: "新客获客活动，注册送 20",
    });
    expect(put.headers.get("X-Admin-CSRF")).toBe("csrf-token-1");
    expect(put.headers.get("Idempotency-Key")).toBeTruthy();

    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(
      screen.getByText(/此后新注册的主账号将一次性获得 20 积分/),
    ).toBeInTheDocument();
    // 保存后表单即现状：没有未提交的改动，按钮回到禁用。
    expect(input).toHaveValue("20");
    expect(
      screen.getByRole("button", { name: "保存注册赠送设置" }),
    ).toBeDisabled();
  });

  it("announces disabling (saving 0) instead of granting zero credits", async () => {
    installFetch({ settings: settingsPayload({ bonus_credits: 20 }) });
    render(<RegistrationBonusSection />);
    const input = await screen.findByLabelText("注册赠送积分（0 表示关闭）");

    fireEvent.change(input, { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "保存注册赠送设置" }));

    const dialog = await screen.findByRole("dialog", {
      name: "保存注册赠送设置",
    });
    expect(dialog).toHaveTextContent(/保存后注册赠送关闭/);
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "活动结束，关闭注册赠送" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

    await waitFor(() =>
      expect(
        screen.getByText(/赠送已关闭，此后新注册的主账号不再自动获得积分/),
      ).toBeInTheDocument(),
    );
  });

  it("shows the server's rejection inside the dialog and retries with the same idempotency key", async () => {
    const harness = installFetch({
      putResponses: [
        {
          status: 409,
          payload: {
            detail: {
              code: "IDEMPOTENCY_CONFLICT",
              message: "同一幂等键已被其他请求体占用。",
            },
          },
        },
      ],
    });
    render(<RegistrationBonusSection />);
    const input = await screen.findByLabelText("注册赠送积分（0 表示关闭）");

    fireEvent.change(input, { target: { value: "15" } });
    fireEvent.click(screen.getByRole("button", { name: "保存注册赠送设置" }));
    fireEvent.change(await screen.findByLabelText("操作原因"), {
      target: { value: "调整赠送额度" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

    // 409 幂等冲突走 CODE_MESSAGES 的固定中文文案，直接指向操作者该做什么。
    expect(await screen.findByText(/幂等键冲突/)).toBeInTheDocument();
    // 失败时对话框不关，表单里的改动也还在。
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(input).toHaveValue("15");

    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
    await waitFor(() => expect(harness.puts()).toHaveLength(2));
    const [first, second] = harness.puts();
    expect(second.headers.get("Idempotency-Key")).toBe(
      first.headers.get("Idempotency-Key"),
    );
  });

  it("renders a read-only view for auditors with no form and no writes", async () => {
    const harness = installFetch({
      settings: settingsPayload({ bonus_credits: 25 }),
    });
    render(<RegistrationBonusSection readOnly />);

    expect(await screen.findByText("25 积分")).toBeInTheDocument();
    expect(screen.getByText(/审计员仅可查看注册赠送设置/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "保存注册赠送设置" }),
    ).toBeNull();
    expect(screen.queryByLabelText("注册赠送积分（0 表示关闭）")).toBeNull();
    expect(harness.puts()).toHaveLength(0);
  });

  it("labels the disabled state as closed rather than zero credits", async () => {
    installFetch({});
    render(<RegistrationBonusSection readOnly />);

    expect(await screen.findByText("已关闭（0 积分）")).toBeInTheDocument();
  });

  it("surfaces a read failure with a retry instead of an empty form", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        jsonResponse({ detail: "注册赠送设置需要 PostgreSQL 运行时。" }, 503),
      ),
    );
    render(<RegistrationBonusSection />);

    expect(
      await screen.findByText(
        /读取注册赠送设置失败：注册赠送设置需要 PostgreSQL 运行时。（503）/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重新读取" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("注册赠送积分（0 表示关闭）")).toBeNull();
  });
});
