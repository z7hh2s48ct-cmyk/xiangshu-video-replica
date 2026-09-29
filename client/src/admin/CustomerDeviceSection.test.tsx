import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setAdminCsrfToken } from "../api";
import { CustomerDeviceSection } from "./CustomerDeviceSection";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

const CUSTOMER_ID = "customer-1";

const boundDevice = {
  device_id: "device-1",
  activation_code_id: null,
  user_id: CUSTOMER_ID,
  slot_no: 1,
  display_name: "办公室电脑",
  platform: "windows",
  status: "BOUND",
  bound_at: "2026-09-01T10:00:00+00:00",
  unbound_at: null,
  revoked_at: null,
  last_heartbeat_at: "2026-09-01T11:59:00+00:00",
  online: true,
};

function deviceList(items = [boundDevice]) {
  return { items, total: items.length, limit: 50, offset: 0 };
}

describe("CustomerDeviceSection", () => {
  beforeEach(() => {
    setAdminCsrfToken("csrf-token-1");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("loads only the customer's BOUND devices", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      jsonResponse(deviceList()),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<CustomerDeviceSection readOnly={false} userId={CUSTOMER_ID} />);

    expect(await screen.findByText("办公室电脑")).toBeInTheDocument();
    expect(screen.getByText("Windows")).toBeInTheDocument();
    expect(screen.getByText("在线")).toBeInTheDocument();
    expect(screen.getByText("已绑定 1 台")).toBeInTheDocument();
    // 时间列锁定北京时间口径（Asia/Shanghai）：10:00 UTC → 18:00。
    expect(screen.getByText("2026/9/1 18:00:00")).toBeInTheDocument();

    const url = String(fetchMock.mock.calls[0]?.[0]);
    expect(url).toContain("/api/control/devices?");
    expect(url).toContain(`user_id=${CUSTOMER_ID}`);
    expect(url).toContain("status=BOUND");
  });

  it("shows an empty hint when the customer has no bound device", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => jsonResponse(deviceList([]))),
    );

    render(<CustomerDeviceSection readOnly={false} userId={CUSTOMER_ID} />);

    expect(
      await screen.findByText("该客户当前没有已绑定设备。"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("hides every action for the read-only auditor role", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => jsonResponse(deviceList())),
    );

    render(<CustomerDeviceSection readOnly userId={CUSTOMER_ID} />);

    await screen.findByText("办公室电脑");
    expect(screen.queryByRole("button", { name: /解绑设备/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /吊销设备凭据/ })).toBeNull();
    expect(screen.getByText(/（只读角色）/)).toBeInTheDocument();
  });

  it("unbinds a device with a reason and refreshes the list", async () => {
    const fetchMock = vi.fn((url: string, _init?: RequestInit) =>
      String(url).includes("/unbind")
        ? jsonResponse({
            device_id: "device-1",
            status: "UNBOUND",
            outcome: "UNBOUND",
            request_id: "req-1",
          })
        : jsonResponse(deviceList()),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<CustomerDeviceSection readOnly={false} userId={CUSTOMER_ID} />);
    await screen.findByText("办公室电脑");

    fireEvent.click(screen.getByRole("button", { name: "解绑设备 device-1" }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();

    // reason 级别：只填原因即可提交，不需要勾选知晓。
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客户换机" },
    });
    fireEvent.click(screen.getByRole("button", { name: "解绑并踢会话" }));

    expect(
      await screen.findByText(/已解绑并踢出会话：device-1/),
    ).toBeInTheDocument();

    const unbindCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/unbind"),
    );
    expect(String(unbindCall?.[0])).toContain(
      "/api/control/devices/device-1/unbind",
    );
    expect(unbindCall?.[1]).toMatchObject({ method: "POST" });
    expect(JSON.parse(String(unbindCall?.[1]?.body))).toMatchObject({
      confirm: true,
      reason: "客户换机",
    });
  });

  it("revokes a credential only after the acknowledgement is ticked", async () => {
    const fetchMock = vi.fn((url: string, _init?: RequestInit) =>
      String(url).includes("/revoke-credential")
        ? jsonResponse({
            device_id: "device-1",
            status: "REVOKED",
            outcome: "REVOKED",
            request_id: "req-2",
          })
        : jsonResponse(deviceList()),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<CustomerDeviceSection readOnly={false} userId={CUSTOMER_ID} />);
    await screen.findByText("办公室电脑");

    fireEvent.click(
      screen.getByRole("button", { name: "吊销设备凭据 device-1" }),
    );
    await screen.findByRole("dialog");

    // reasonAndAck 级别：未勾选时不得提交。
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "凭据疑似泄漏" },
    });
    fireEvent.click(screen.getByRole("button", { name: "吊销凭据" }));
    expect(await screen.findByText("请先勾选确认操作")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("/revoke-credential"),
      ),
    ).toBe(false);

    fireEvent.click(screen.getByLabelText("我已知晓该操作的影响"));
    fireEvent.click(screen.getByRole("button", { name: "吊销凭据" }));

    expect(await screen.findByText(/已吊销凭据：device-1/)).toBeInTheDocument();
    const revokeCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/revoke-credential"),
    );
    expect(String(revokeCall?.[0])).toContain(
      "/api/control/devices/device-1/revoke-credential",
    );
  });

  it("surfaces a load failure without inventing device rows", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => jsonResponse({ detail: "读取设备列表失败" }, 500)),
    );

    render(<CustomerDeviceSection readOnly={false} userId={CUSTOMER_ID} />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("table")).toBeNull());
  });
});
