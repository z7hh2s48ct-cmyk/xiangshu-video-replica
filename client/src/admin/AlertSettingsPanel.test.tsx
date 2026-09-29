import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setAdminCsrfToken } from "../api";
import { AlertSettingsPanel } from "./AlertSettingsPanel";

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
    recipient_user_id: null,
    recipient_display_name: null,
    failure_rate_window_minutes: 60,
    failure_rate_threshold_percent: 30,
    failure_rate_min_sample: 5,
    updated_by_user_id: null,
    updated_at: null,
    ...overrides,
  };
}

const CANDIDATES = [
  { user_id: "u-tech", username: "tech", display_name: "王工", role: "admin" },
  {
    user_id: "u-audit",
    username: "audit",
    display_name: "李审",
    role: "auditor",
  },
];

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
  candidates?: unknown[];
  putResponses?: Array<{ status: number; payload: unknown }>;
}): Harness {
  let current = options.settings ?? settingsPayload();
  const putResponses = [...(options.putResponses ?? [])];
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url.endsWith("/api/control/settings/alerts") && method === "GET") {
      return jsonResponse(current);
    }
    if (url.endsWith("/api/control/settings/alerts/recipient-candidates")) {
      return jsonResponse({ items: options.candidates ?? CANDIDATES });
    }
    if (url.endsWith("/api/control/settings/alerts") && method === "PUT") {
      const scripted = putResponses.shift();
      if (scripted) {
        return jsonResponse(scripted.payload, scripted.status);
      }
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      current = settingsPayload({
        recipient_user_id: body.recipient_user_id,
        recipient_display_name: body.recipient_user_id ? "王工" : null,
        failure_rate_window_minutes: body.failure_rate_window_minutes,
        failure_rate_threshold_percent: body.failure_rate_threshold_percent,
        failure_rate_min_sample: body.failure_rate_min_sample,
        updated_by_user_id: "admin-1",
        updated_at: "2026-09-29T12:00:00Z",
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

function field(label: string) {
  return screen.getByLabelText(label);
}

describe("AlertSettingsPanel（P2-4 告警口径设置）", () => {
  beforeEach(() => {
    setAdminCsrfToken("csrf-token-1");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    setAdminCsrfToken("");
  });

  it("shows the current settings and keeps save disabled until something changes", async () => {
    installFetch({
      settings: settingsPayload({
        recipient_user_id: "u-tech",
        recipient_display_name: "王工",
        failure_rate_window_minutes: 120,
        failure_rate_threshold_percent: 25,
        failure_rate_min_sample: 8,
        updated_at: "2026-09-28T02:00:00Z",
      }),
    });
    render(<AlertSettingsPanel />);

    expect(await screen.findByLabelText("统计窗口（分钟）")).toHaveValue("120");
    expect(field("失败率阈值（%）")).toHaveValue("25");
    expect(field("最小样本量（终局任务数）")).toHaveValue("8");
    expect(field("接收人")).toHaveValue("u-tech");
    expect(screen.getByRole("button", { name: "保存告警设置" })).toBeDisabled();

    fireEvent.change(field("失败率阈值（%）"), { target: { value: "40" } });
    expect(screen.getByRole("button", { name: "保存告警设置" })).toBeEnabled();
  });

  it("tells operators the recipient is not an external push channel", async () => {
    installFetch({});
    render(<AlertSettingsPanel />);

    // 服务端没有任何推送通道：文案不能让运营以为设了接收人就会收到短信 / 邮件。
    expect(
      await screen.findByText(/暂不发送短信或邮件等外部提醒/),
    ).toBeInTheDocument();
  });

  it.each([
    ["统计窗口（分钟）", "0", /统计窗口需为 1 到 10080/],
    ["统计窗口（分钟）", "10081", /统计窗口需为 1 到 10080/],
    ["统计窗口（分钟）", "1.5", /统计窗口需为 1 到 10080/],
    ["失败率阈值（%）", "101", /失败率阈值需为 0 到 100/],
    ["失败率阈值（%）", "abc", /失败率阈值需为 0 到 100/],
    ["最小样本量（终局任务数）", "0", /最小样本量需为不小于 1 的整数/],
  ])(
    "blocks %s = %s locally and never opens the dialog",
    async (label, value, message) => {
      const harness = installFetch({});
      render(<AlertSettingsPanel />);
      await screen.findByLabelText(label);

      fireEvent.change(field(label), { target: { value } });
      fireEvent.click(screen.getByRole("button", { name: "保存告警设置" }));

      expect(await screen.findByText(message)).toBeInTheDocument();
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(harness.puts()).toHaveLength(0);
    },
  );

  it("saves through the confirm dialog with the write contract and reloads the report", async () => {
    const harness = installFetch({});
    const onSaved = vi.fn();
    render(<AlertSettingsPanel onSaved={onSaved} />);
    await screen.findByLabelText("统计窗口（分钟）");

    fireEvent.change(field("接收人"), { target: { value: "u-tech" } });
    fireEvent.change(field("失败率阈值（%）"), { target: { value: "45" } });
    fireEvent.click(screen.getByRole("button", { name: "保存告警设置" }));

    const dialog = await screen.findByRole("dialog", { name: "保存告警设置" });
    // 原因必填：审计里的「为什么改」必须是操作人写的，不是固定文字。
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
    expect(await screen.findByText("请填写操作原因")).toBeInTheDocument();
    expect(harness.puts()).toHaveLength(0);

    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "夜间低峰误报太多，先放宽阈值" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

    await waitFor(() => expect(harness.puts()).toHaveLength(1));
    const [put] = harness.puts();
    expect(put.body).toEqual({
      recipient_user_id: "u-tech",
      failure_rate_window_minutes: 60,
      failure_rate_threshold_percent: 45,
      failure_rate_min_sample: 5,
      confirm: true,
      reason: "夜间低峰误报太多，先放宽阈值",
    });
    expect(put.headers.get("X-Admin-CSRF")).toBe("csrf-token-1");
    expect(put.headers.get("Idempotency-Key")).toBeTruthy();

    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(screen.getByText(/告警设置已保存/)).toBeInTheDocument();
    expect(onSaved).toHaveBeenCalledTimes(1);
    // 保存后表单即现状：没有未提交的改动，按钮回到禁用。
    expect(field("失败率阈值（%）")).toHaveValue("45");
    expect(screen.getByRole("button", { name: "保存告警设置" })).toBeDisabled();
  });

  it("clears the recipient explicitly instead of inventing a default", async () => {
    const harness = installFetch({
      settings: settingsPayload({
        recipient_user_id: "u-tech",
        recipient_display_name: "王工",
      }),
    });
    render(<AlertSettingsPanel />);
    await screen.findByLabelText("接收人");

    fireEvent.change(field("接收人"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "保存告警设置" }));
    fireEvent.change(await screen.findByLabelText("操作原因"), {
      target: { value: "负责人离岗，暂不指定" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

    await waitFor(() => expect(harness.puts()).toHaveLength(1));
    expect(harness.puts()[0].body.recipient_user_id).toBeNull();
  });

  it("shows the server's rejection inside the dialog and retries with the same idempotency key", async () => {
    const harness = installFetch({
      putResponses: [
        {
          status: 400,
          payload: {
            detail: {
              code: "ALERT_SETTINGS_VALIDATION_FAILED",
              message: "接收人必须是启用中的管理员账号。",
            },
          },
        },
      ],
    });
    const onSaved = vi.fn();
    render(<AlertSettingsPanel onSaved={onSaved} />);
    await screen.findByLabelText("统计窗口（分钟）");

    fireEvent.change(field("统计窗口（分钟）"), { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: "保存告警设置" }));
    fireEvent.change(await screen.findByLabelText("操作原因"), {
      target: { value: "缩短窗口更快发现故障" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

    expect(
      await screen.findByText(/接收人必须是启用中的管理员账号/),
    ).toBeInTheDocument();
    // 失败时对话框不关、不通知外层重拉，表单里的改动也还在。
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
    expect(field("统计窗口（分钟）")).toHaveValue("30");

    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
    await waitFor(() => expect(harness.puts()).toHaveLength(2));
    const [first, second] = harness.puts();
    expect(second.headers.get("Idempotency-Key")).toBe(
      first.headers.get("Idempotency-Key"),
    );
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
  });

  it("keeps a deactivated recipient visible instead of showing it as unassigned", async () => {
    installFetch({
      settings: settingsPayload({
        recipient_user_id: "u-gone",
        recipient_display_name: "老张",
      }),
      candidates: CANDIDATES,
    });
    render(<AlertSettingsPanel />);

    const select = await screen.findByLabelText("接收人");
    expect(select).toHaveValue("u-gone");
    expect(
      screen.getByRole("option", { name: "老张（已停用，请更换）" }),
    ).toBeInTheDocument();
  });

  it("lists active admins and auditors as recipient candidates", async () => {
    installFetch({});
    render(<AlertSettingsPanel />);

    expect(
      await screen.findByRole("option", { name: "王工（管理员）" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "李审（审计员）" }),
    ).toBeInTheDocument();
  });

  it("renders a read-only view for auditors with no form and no writes", async () => {
    const harness = installFetch({
      settings: settingsPayload({
        recipient_user_id: "u-tech",
        recipient_display_name: "王工",
        failure_rate_threshold_percent: 35,
      }),
    });
    render(<AlertSettingsPanel readOnly />);

    expect(await screen.findByText("35%")).toBeInTheDocument();
    expect(screen.getByText("王工")).toBeInTheDocument();
    expect(screen.getByText(/审计员仅可查看告警设置/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存告警设置" })).toBeNull();
    expect(screen.queryByLabelText("失败率阈值（%）")).toBeNull();
    expect(harness.puts()).toHaveLength(0);
  });

  it("surfaces a read failure with a retry instead of an empty form", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        jsonResponse({ detail: "告警设置需要 PostgreSQL 运行时。" }, 503),
      ),
    );
    render(<AlertSettingsPanel />);

    expect(
      await screen.findByText(
        /读取告警设置失败：告警设置需要 PostgreSQL 运行时。（503）/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重新读取" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("统计窗口（分钟）")).toBeNull();
  });
});
