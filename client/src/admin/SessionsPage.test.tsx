import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setAdminCsrfToken } from "../api";
import { SessionsPage } from "./SessionsPage";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

const CUSTOMER_ID = "customer-1";
const sessionItem = {
  session_id: "sess-1",
  user_id: CUSTOMER_ID,
  username: "customer_one",
  device_id: "device-1",
  session_epoch: 3,
  lease_until: "2026-09-01T12:01:00+00:00",
  last_heartbeat_at: "2026-09-01T11:59:30+00:00",
  created_at: "2026-08-30T08:00:00+00:00",
  updated_at: "2026-09-01T11:59:30+00:00",
  device_name: "办公室电脑",
  platform: "windows",
  slot_no: 1,
  device_status: "ACTIVE",
};

function sessionList() {
  return { items: [sessionItem], total: 1, limit: 50, offset: 0 };
}

describe("SessionsPage", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-09-01T12:00:00+00:00"));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("loads all live sessions on mount and renders the session card", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      jsonResponse(sessionList()),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionsPage />);

    expect(await screen.findByText("customer_one")).toBeInTheDocument();
    expect(screen.queryByText(/办公室电脑/)).not.toBeInTheDocument();
    expect(screen.getByText("Windows")).toBeInTheDocument();
    // P2-1：租约 / 心跳 / Epoch 与每秒进度条从主视图移除。
    expect(screen.queryByText(/租约/)).toBeNull();
    expect(screen.queryByText(/心跳/)).toBeNull();
    expect(screen.queryByText(/Epoch/)).toBeNull();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain(
      "/api/control/customer-sessions/live?limit=50&offset=0",
    );
  });

  it("renders every platform through the shared vocabulary", async () => {
    // 本页曾私刻一份平台字典，且只列了 windows/macos/linux —— ios/android 落到
    // `?? platform` 兜底，运营看到的是英文码 "ios"/"android"（2026-09-12 评审
    // P3 记载的 platformLabel 与词典分叉）。现在统一走 ui/vocabulary。
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      jsonResponse({
        items: [
          { ...sessionItem, session_id: "sess-ios", platform: "ios" },
          { ...sessionItem, session_id: "sess-android", platform: "android" },
        ],
        total: 2,
        limit: 50,
        offset: 0,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionsPage />);

    expect(await screen.findByText("iOS")).toBeInTheDocument();
    expect(screen.getByText("Android")).toBeInTheDocument();
    // 兜底路径不再被走到：裸平台码不该出现在界面上。
    expect(screen.queryByText("ios")).toBeNull();
    expect(screen.queryByText("android")).toBeNull();
  });

  it("loads the requested customer automatically", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      jsonResponse(sessionList()),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionsPage userId={CUSTOMER_ID} />);

    await screen.findByText("customer_one");
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain(
      `/api/control/customers/${CUSTOMER_ID}/sessions?limit=50`,
    );
    expect(screen.queryByLabelText("客户编号")).not.toBeInTheDocument();
  });

  it("delegates embedded customer selection to the shared parent context", async () => {
    const onCustomerChange = vi.fn();
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      jsonResponse(sessionList()),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionsPage onCustomerChange={onCustomerChange} />);

    await screen.findByText("customer_one");
    fireEvent.change(screen.getByLabelText("客户编号"), {
      target: { value: "customer-b" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查看客户" }));
    expect(onCustomerChange).toHaveBeenCalledWith("customer-b");

    fireEvent.click(screen.getByRole("button", { name: "选择客户" }));
    expect(onCustomerChange).toHaveBeenCalledWith(CUSTOMER_ID);
    fireEvent.click(screen.getByRole("button", { name: "全部在线" }));
    expect(onCustomerChange).toHaveBeenCalledWith(undefined);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("switches the standalone view to the customer picked from a row", async () => {
    // 独立模式（无共享上下文）：点行内「选择客户」就地把视图切到该客户，
    // 而不是委托父级——嵌入模式的委托路径已有用例，独立路径此前无用例。
    const fetchMock = vi.fn((url: string, _init?: RequestInit) =>
      jsonResponse(
        String(url).includes(`/customers/${CUSTOMER_ID}/sessions`)
          ? sessionList()
          : { items: [sessionItem], total: 1, limit: 50, offset: 0 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionsPage />);

    await screen.findByText("customer_one");
    expect(screen.getByLabelText("客户编号")).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: "选择客户" }));

    await waitFor(() =>
      expect(String(fetchMock.mock.calls.at(-1)?.[0])).toContain(
        `/api/control/customers/${CUSTOMER_ID}/sessions?limit=50`,
      ),
    );
    expect(screen.getByLabelText("客户编号")).toHaveValue(CUSTOMER_ID);
  });

  it("ignores a stale customer response after switching context", async () => {
    let resolveCustomerA:
      | ((value: Awaited<ReturnType<typeof jsonResponse>>) => void)
      | undefined;
    const customerB = {
      ...sessionItem,
      session_id: "sess-b",
      user_id: "customer-b",
      username: "customer_b",
      device_name: "B 的电脑",
    };
    const fetchMock = vi.fn((url: string) => {
      if (url.includes("customer-a")) {
        return new Promise((resolve) => {
          resolveCustomerA = resolve;
        });
      }
      return jsonResponse({
        items: [customerB],
        total: 1,
        limit: 50,
        offset: 0,
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { rerender } = render(<SessionsPage userId="customer-a" />);
    rerender(<SessionsPage userId="customer-b" />);
    expect(await screen.findByText("customer_b")).toBeInTheDocument();

    resolveCustomerA?.(
      await jsonResponse({
        items: [sessionItem],
        total: 1,
        limit: 50,
        offset: 0,
      }),
    );

    await waitFor(() => expect(screen.queryByText("customer_one")).toBeNull());
    expect(screen.getByText("customer_b")).toBeInTheDocument();
  });

  it("does not apply an old revoke after leaving and returning to a customer", async () => {
    setAdminCsrfToken("csrf-token-1");
    let resolveRevoke:
      | ((value: Awaited<ReturnType<typeof jsonResponse>>) => void)
      | undefined;
    const fetchMock = vi.fn((url: string) => {
      if (url.includes("/revoke")) {
        return new Promise((resolve) => {
          resolveRevoke = resolve;
        });
      }
      const isCustomerB = url.includes("customer-b");
      const item = {
        ...sessionItem,
        session_id: isCustomerB ? "sess-b" : "sess-a",
        user_id: isCustomerB ? "customer-b" : "customer-a",
        username: isCustomerB ? "customer_b" : "customer_a",
      };
      return jsonResponse({ items: [item], total: 1, limit: 50, offset: 0 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { rerender } = render(<SessionsPage userId="customer-a" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "下线 customer_a" }),
    );
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客户反馈异常登录" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认下线" }));

    rerender(<SessionsPage userId="customer-b" />);
    await screen.findByText("customer_b");
    rerender(<SessionsPage userId="customer-a" />);
    await screen.findByText("customer_a");
    const listCallCount = fetchMock.mock.calls.filter(
      ([url]) => !String(url).includes("/revoke"),
    ).length;

    resolveRevoke?.(await jsonResponse({ request_id: "old-revoke" }));

    await waitFor(() => {
      expect(screen.queryByRole("status")).toBeNull();
      expect(
        fetchMock.mock.calls.filter(
          ([url]) => !String(url).includes("/revoke"),
        ),
      ).toHaveLength(listCallCount);
    });
  });

  it("revokes a session with a reason and refreshes the live list", async () => {
    setAdminCsrfToken("csrf-token-1");
    const fetchMock = vi.fn((url: string, _init?: RequestInit) =>
      String(url).includes("/revoke")
        ? jsonResponse({ request_id: "revoke-1" })
        : jsonResponse(sessionList()),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<SessionsPage />);
    await screen.findByText("customer_one");
    fireEvent.click(screen.getByRole("button", { name: "下线 customer_one" }));
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客服确认账号异常" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认下线" }));

    expect(await screen.findByText(/已下线 customer_one/)).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const revokeCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/revoke"),
    );
    expect(revokeCall?.[1]).toMatchObject({ method: "POST" });
    expect(JSON.parse(String(revokeCall?.[1]?.body))).toMatchObject({
      confirm: true,
      reason: "客服确认账号异常",
      session_epoch: 3,
    });
  });

  it("keeps the revoke idempotency key after an ambiguous failure", async () => {
    setAdminCsrfToken("csrf-token-1");
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(
      "11111111-1111-4111-8111-111111111111",
    );
    let revokeAttempts = 0;
    const fetchMock = vi.fn((url: string, _init?: RequestInit) => {
      if (String(url).includes("/revoke")) {
        revokeAttempts += 1;
        return revokeAttempts === 1
          ? Promise.reject(new TypeError("Failed to fetch"))
          : jsonResponse({ request_id: "revoke-1" });
      }
      return jsonResponse(sessionList());
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SessionsPage />);
    await screen.findByText("customer_one");
    fireEvent.click(screen.getByRole("button", { name: "下线 customer_one" }));
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "网络失败后重试" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认下线" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Failed to fetch",
    );
    fireEvent.click(screen.getByRole("button", { name: "确认下线" }));
    expect(await screen.findByText(/已下线 customer_one/)).toBeInTheDocument();

    const keys = fetchMock.mock.calls
      .filter(([url]) => String(url).includes("/revoke"))
      .map(([, init]) => new Headers(init?.headers).get("Idempotency-Key"));
    expect(keys).toEqual([
      "11111111-1111-4111-8111-111111111111",
      "11111111-1111-4111-8111-111111111111",
    ]);
  });

  it("no longer hosts credit adjustments (moved to customer detail, P0-2)", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      jsonResponse(sessionList()),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionsPage userId={CUSTOMER_ID} />);

    await screen.findByText("customer_one");
    expect(screen.queryByRole("button", { name: /调账/ })).toBeNull();
    expect(screen.queryByLabelText("积分整数")).toBeNull();
  });

  it("hides all write actions for read-only operators", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      jsonResponse(sessionList()),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionsPage userId={CUSTOMER_ID} readOnly />);

    await screen.findByText("customer_one");
    expect(
      screen.queryByRole("button", { name: /下线/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /后台调账/ }),
    ).not.toBeInTheDocument();
  });
});
