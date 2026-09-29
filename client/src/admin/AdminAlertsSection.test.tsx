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

function installFetch(payload: unknown, status = 200) {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (
      url.endsWith("/api/control/alerts/failure-rate") &&
      (init?.method ?? "GET") === "GET"
    ) {
      return jsonResponse(payload, status);
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
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
                category: "服务商故障",
                owner: "运营重试",
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
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
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
                category: "服务商故障",
                owner: "运营重试",
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
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(String(url)).toContain("/api/control/alerts/failure-rate");
    expect((init as RequestInit).method).toBe("GET");
    const headers = ((init as RequestInit).headers ?? {}) as Record<
      string,
      string
    >;
    expect(headers["X-Admin-CSRF"]).toBeUndefined();
    expect(headers["Idempotency-Key"]).toBeUndefined();
  });
});
