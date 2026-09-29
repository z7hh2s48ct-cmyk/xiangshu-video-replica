import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setAdminCsrfToken } from "../api";
import { AdminAlertsSection } from "./AdminAlertsSection";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

function reportPayload(overrides: Record<string, unknown> = {}) {
  return {
    generated_at: "2026-09-29T12:00:00Z",
    window_minutes: 60,
    threshold_percent: 30,
    min_sample_size: 5,
    total: 0,
    failed: 0,
    failure_rate_percent: 0,
    exceeded: false,
    alerting: false,
    groups: [],
    ...overrides,
  };
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

function installFetch(payload: unknown, status = 200) {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url.endsWith("/api/control/alerts/failure-rate") && method === "GET") {
      return jsonResponse(payload, status);
    }
    // 下方「告警设置」区块随本组件一起挂载；它有自己的用例
    //（AlertSettingsPanel.test.tsx），这里只需要让它读得到。
    if (url.endsWith("/api/control/settings/alerts") && method === "GET") {
      return jsonResponse(settingsPayload());
    }
    if (url.endsWith("/api/control/settings/alerts/recipient-candidates")) {
      return jsonResponse({ items: [] });
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function reportCalls(fetchMock: ReturnType<typeof installFetch>) {
  return fetchMock.mock.calls.filter(([url]) =>
    String(url).endsWith("/api/control/alerts/failure-rate"),
  );
}

describe("AdminAlertsSection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setAdminCsrfToken("");
  });

  it("renders the alerting report with runbook annotations", async () => {
    const fetchMock = installFetch(
      reportPayload({
        total: 12,
        failed: 6,
        failure_rate_percent: 50,
        exceeded: true,
        alerting: true,
        groups: [
          {
            record_type: "VIDEO",
            total: 10,
            failed: 5,
            failure_rate_percent: 50,
            exceeded: true,
            top_errors: [
              {
                error_code: "PROVIDER_TERMINAL",
                count: 5,
                category: "PROVIDER_FAULT",
                owner: "OPS",
                advice: "等待服务商恢复后原地重试",
              },
            ],
          },
        ],
      }),
    );
    render(<AdminAlertsSection />);

    expect(await screen.findByText("超阈值")).toBeInTheDocument();
    expect(screen.getByText(/请技术负责人尽快处理/)).toBeInTheDocument();
    expect(screen.getByText("视频生成")).toBeInTheDocument();
    expect(
      screen.getByText(/失败分类：服务商故障 · 处理人：运营重试/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/处理建议：等待服务商恢复后原地重试/),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    await waitFor(() => expect(reportCalls(fetchMock)).toHaveLength(2));
  });

  it("labels groups by threshold status without overstating small samples", async () => {
    installFetch(
      reportPayload({
        total: 20,
        failed: 2,
        failure_rate_percent: 10,
        groups: [
          {
            record_type: "VIDEO",
            total: 10,
            failed: 1,
            failure_rate_percent: 10,
            exceeded: false,
            top_errors: [
              {
                error_code: "PROVIDER_TERMINAL",
                count: 1,
                category: "PROVIDER_FAULT",
                owner: "OPS",
                advice: "等待恢复",
              },
            ],
          },
          {
            record_type: "ANALYSIS",
            total: 2,
            failed: 1,
            failure_rate_percent: 50,
            exceeded: false,
            top_errors: [
              {
                error_code: null,
                count: 1,
                category: null,
                owner: null,
                advice: null,
              },
            ],
          },
          {
            record_type: "FIRST_FRAME_IMAGE",
            total: 8,
            failed: 0,
            failure_rate_percent: 0,
            exceeded: false,
            top_errors: [],
          },
        ],
      }),
    );
    render(<AdminAlertsSection />);

    expect(await screen.findByText("低于阈值")).toBeInTheDocument();
    // 样本不足（2 < 5）时失败率再高也不告警，绝不能写成「正常」。
    expect(screen.getByText("样本不足")).toBeInTheDocument();
    expect(screen.getByText("正常")).toBeInTheDocument();
    expect(screen.getByText(/未记录错误码/)).toBeInTheDocument();
    expect(screen.getByText(/没有类型越过 30% 失败率阈值/)).toBeInTheDocument();
  });

  it("states that nothing ended in the window when there are no terminal tasks", async () => {
    installFetch(reportPayload());
    render(<AdminAlertsSection />);

    expect(await screen.findByText(/没有已终结的任务/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("surfaces read failures without pretending the system is healthy", async () => {
    installFetch({ detail: "统计服务不可用" }, 503);
    render(<AdminAlertsSection />);

    expect(
      await screen.findByText(/读取失败率告警失败：统计服务不可用（503）/),
    ).toBeInTheDocument();
    expect(screen.queryByText("正常")).toBeNull();
  });

  it("reads with a plain GET (no write contract headers)", async () => {
    const fetchMock = installFetch(reportPayload());
    render(<AdminAlertsSection />);

    await screen.findByText(/没有已终结的任务/);
    const [url, init] = reportCalls(fetchMock)[0] ?? [];
    expect(String(url)).toContain("/api/control/alerts/failure-rate");
    expect((init as RequestInit).method).toBe("GET");
    const headers = ((init as RequestInit).headers ?? {}) as Record<
      string,
      string
    >;
    expect(headers["X-Admin-CSRF"]).toBeUndefined();
    expect(headers["Idempotency-Key"]).toBeUndefined();
  });
  it("names the designated recipient and follows the configured window", async () => {
    installFetch(
      reportPayload({
        window_minutes: 15,
        total: 10,
        failed: 6,
        failure_rate_percent: 60,
        exceeded: true,
        alerting: true,
        recipient_user_id: "u-tech",
        recipient_display_name: "王工",
        groups: [
          {
            record_type: "VIDEO",
            total: 10,
            failed: 6,
            failure_rate_percent: 60,
            exceeded: true,
            top_errors: [],
          },
        ],
      }),
    );
    render(<AdminAlertsSection />);

    expect(await screen.findByText(/指定负责人：王工/)).toBeInTheDocument();
    // 窗口来自报告而不是写死的「近 1 小时」：设置里改了窗口，文案跟着变。
    expect(screen.getByText(/按业务类型统计近 15 分钟内/)).toBeInTheDocument();
    expect(screen.queryByText(/近 1 小时/)).toBeNull();
  });

  it("re-reads the report after the alert settings are saved", async () => {
    let reads = 0;
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/api/control/alerts/failure-rate")) {
        reads += 1;
        // 第二次读到的是新口径下的报告：阈值 45%。
        return jsonResponse(
          reportPayload({ threshold_percent: reads > 1 ? 45 : 30 }),
        );
      }
      if (url.endsWith("/api/control/settings/alerts") && method === "GET") {
        return jsonResponse(settingsPayload());
      }
      if (url.endsWith("/api/control/settings/alerts/recipient-candidates")) {
        return jsonResponse({ items: [] });
      }
      if (url.endsWith("/api/control/settings/alerts") && method === "PUT") {
        return jsonResponse(
          settingsPayload({ failure_rate_threshold_percent: 45 }),
        );
      }
      throw new Error(`unexpected request: ${method} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    setAdminCsrfToken("csrf-token-1");
    render(<AdminAlertsSection />);

    expect(
      await screen.findByText(/没有类型越过 30% 失败率阈值/),
    ).toBeInTheDocument();
    fireEvent.change(await screen.findByLabelText("失败率阈值（%）"), {
      target: { value: "45" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存告警设置" }));
    fireEvent.change(await screen.findByLabelText("操作原因"), {
      target: { value: "放宽阈值" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

    expect(
      await screen.findByText(/没有类型越过 45% 失败率阈值/),
    ).toBeInTheDocument();
    expect(reads).toBe(2);
  });

  it("gives auditors the read-only settings view", async () => {
    installFetch(reportPayload());
    render(<AdminAlertsSection readOnly />);

    expect(
      await screen.findByText(/审计员仅可查看告警设置/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存告警设置" })).toBeNull();
  });
});
