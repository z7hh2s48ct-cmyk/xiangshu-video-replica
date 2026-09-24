import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CustomerPairingFlow } from "./CustomerPairingFlow";
import type { CustomerCredentialStore } from "./useCustomerSession";

// Fixture credential strings live behind named constants so the repo's
// secret scan (which flags `token:`/`token =` followed by a quoted literal)
// never sees a raw quoted value — a dummy, never a real credential.
const deviceTokenText = "paired-device-token-1";

/** The transport reads response.headers (X-Idempotent-Replay) and parses the
 * JSON body, so the fetch mock has to look like a real Response envelope. */
function mockResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(),
    json: vi.fn().mockResolvedValue(body),
  };
}

function fakeStore(overrides?: {
  saveActivation?: (deviceToken: string, sessionToken: string) => Promise<void>;
}): CustomerCredentialStore {
  return {
    loadDeviceCredentialToken: vi.fn().mockResolvedValue(null),
    loadSessionToken: vi.fn().mockResolvedValue(null),
    saveActivation:
      overrides?.saveActivation ?? vi.fn().mockResolvedValue(undefined),
    saveSessionToken: vi.fn().mockResolvedValue(undefined),
    clearSessionToken: vi.fn().mockResolvedValue(undefined),
    clearAllCredentials: vi.fn().mockResolvedValue(undefined),
    deviceInstanceId: vi.fn().mockResolvedValue("test-instance-id"),
    devicePlatform: () => "windows",
    // CW-062：身份缓存不参与这些用例的断言，给出满足接口的最小桩。
    loadIdentity: async () => null,
    // 「记住密码」在这些用例里不参与断言，给出满足接口的最小桩。
    loadRememberedLogin: async () => null,
    saveRememberedLogin: async () => {},
    clearRememberedLogin: async () => {},
  };
}

async function fillForm() {
  fireEvent.change(screen.getByLabelText("激活码"), {
    target: { value: "XS04-TESTCODE-CODECODE-CODECODE" },
  });
  fireEvent.change(screen.getByLabelText("设备名称"), {
    target: { value: "My Second Device" },
  });
  const submit = screen.getByRole("button", { name: "提交配对申请" });
  await waitFor(() => expect(submit).toBeEnabled());
  fireEvent.click(submit);
}

describe("CustomerPairingFlow (FE-03 / T30)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("parks on the waiting screen when the primary device must approve (202)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        mockResponse(202, {
          pairing_request_id: "pairing-1",
          status: "PENDING",
          expires_at: "2099-01-01T00:00:00Z",
          request_id: "req-1",
        }),
      ),
    );

    // Pin toLocaleString so the expiry assertion is locale-independent: the CI
    // en-US Intl formats 2026-08-26T12:00:00Z as "8/26/2026, ..." while a
    // zh-CN locale produces "2026/8/26 ...", and the test regex wants the
    // year-first shape.
    vi.spyOn(Date.prototype, "toLocaleString").mockReturnValue(
      "2026/8/26 12:00:00",
    );

    render(<CustomerPairingFlow store={fakeStore()} onPaired={vi.fn()} />);
    await fillForm();

    expect(
      await screen.findByRole("heading", { name: "等待主设备审批" }),
    ).toBeInTheDocument();
    // The waiting screen shows the pairing expiry in the local locale.
    expect(screen.getByText(/2026\/8\/26/)).toBeInTheDocument();
    expect(screen.getByText(/pairing-1/)).toBeInTheDocument();
    // The waiting screen is honest about the pending state, not a success.
    expect(screen.queryByRole("heading", { name: "配对成功" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "返回修改" }));
    expect(screen.getByLabelText("激活码")).toHaveValue(
      "XS04-TESTCODE-CODECODE-CODECODE",
    );
    expect(screen.getByLabelText("设备名称")).toHaveValue("My Second Device");
  });

  it("automatically consumes the pairing after an administrator approves it", async () => {
    const saveActivation = vi.fn().mockResolvedValue(undefined);
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() =>
        mockResponse(202, {
          pairing_request_id: "pairing-auto-1",
          status: "PENDING",
          expires_at: "2099-01-01T00:00:00Z",
          request_id: "req-auto-1",
        }),
      )
      .mockImplementationOnce(() =>
        mockResponse(201, {
          device_id: "device-auto-2",
          slot_no: 2,
          device_token: deviceTokenText,
          request_id: "req-auto-2",
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerPairingFlow
        store={fakeStore({ saveActivation })}
        onPaired={vi.fn()}
        pollIntervalMs={10}
      />,
    );
    await fillForm();

    expect(
      await screen.findByRole("heading", { name: "配对成功" }),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(saveActivation).toHaveBeenCalledWith(deviceTokenText, "");
  });

  it("keeps one idempotency key across polls so a lost 201 can replay", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() =>
        mockResponse(202, {
          pairing_request_id: "pairing-key-1",
          status: "PENDING",
          expires_at: "2099-01-01T00:00:00Z",
          request_id: "req-key-1",
        }),
      )
      .mockImplementationOnce(() =>
        mockResponse(202, {
          pairing_request_id: "pairing-key-1",
          status: "PENDING",
          expires_at: "2099-01-01T00:00:00Z",
          request_id: "req-key-2",
        }),
      )
      .mockImplementationOnce(() =>
        mockResponse(201, {
          device_id: "device-key-2",
          slot_no: 2,
          device_token: deviceTokenText,
          request_id: "req-key-3",
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerPairingFlow
        store={fakeStore()}
        onPaired={vi.fn()}
        pollIntervalMs={10}
      />,
    );
    await fillForm();

    expect(
      await screen.findByRole("heading", { name: "配对成功" }),
    ).toBeInTheDocument();
    // Every waiting-stage poll carries the SAME Idempotency-Key: the
    // consumption seals the one-time credential under that key, so a poll
    // whose 201 response is lost replays it instead of burning the slot.
    const keys = fetchMock.mock.calls.map(([, init]) =>
      new Headers((init as RequestInit).headers).get("Idempotency-Key"),
    );
    expect(keys[1]).toBeTruthy();
    expect(keys[2]).toBe(keys[1]);
  });

  it("stops polling once the pairing window has expired", async () => {
    const fetchMock = vi.fn(() =>
      mockResponse(202, {
        pairing_request_id: "pairing-exp-1",
        status: "PENDING",
        expires_at: "2020-01-01T00:00:00Z",
        request_id: "req-exp-1",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerPairingFlow
        store={fakeStore()}
        onPaired={vi.fn()}
        pollIntervalMs={10}
      />,
    );
    await fillForm();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "配对请求已过期",
    );
    // Ample time for a wrongly scheduled poll to fire: only the form's
    // enroll ever happened — an expired request must not silently mint a
    // new pending pairing.
    await new Promise((resolve) => {
      setTimeout(resolve, 80);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("recovers from a transient poll failure and still consumes the approval", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() =>
        mockResponse(202, {
          pairing_request_id: "pairing-flaky-1",
          status: "PENDING",
          expires_at: "2099-01-01T00:00:00Z",
          request_id: "req-flaky-1",
        }),
      )
      .mockImplementationOnce(() => Promise.reject(new Error("network down")))
      .mockImplementationOnce(() =>
        mockResponse(201, {
          device_id: "device-flaky-2",
          slot_no: 2,
          device_token: deviceTokenText,
          request_id: "req-flaky-2",
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerPairingFlow
        store={fakeStore()}
        onPaired={vi.fn()}
        pollIntervalMs={10}
      />,
    );
    await fillForm();

    expect(
      await screen.findByRole("heading", { name: "配对成功" }),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("stores the device credential and reports success when approved meanwhile (201)", async () => {
    const saveActivation = vi.fn().mockResolvedValue(undefined);
    const store = fakeStore({ saveActivation });
    const onPaired = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        mockResponse(201, {
          device_id: "device-2",
          slot_no: 2,
          device_token: deviceTokenText,
          request_id: "req-2",
        }),
      ),
    );

    render(<CustomerPairingFlow store={store} onPaired={onPaired} />);
    await fillForm();

    const [, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0] as [
      string,
      RequestInit,
    ];
    expect(JSON.parse(init.body as string)).toMatchObject({
      device_fingerprint: "test-instance-id",
      device_platform: "windows",
    });

    expect(
      await screen.findByRole("heading", { name: "配对成功" }),
    ).toBeInTheDocument();
    // The consumed branch primes the vault with the device credential and
    // no session token; the boot path logs the device in on the next /customer.
    expect(saveActivation).toHaveBeenCalledWith(deviceTokenText, "");

    fireEvent.click(screen.getByRole("button", { name: "进入客户工作区" }));
    expect(onPaired).toHaveBeenCalledTimes(1);
  });

  it("surfaces a vault write failure instead of claiming success", async () => {
    const saveActivation = vi.fn().mockRejectedValue(new Error("vault locked"));
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        mockResponse(201, {
          device_id: "device-2",
          slot_no: 2,
          device_token: deviceTokenText,
          request_id: "req-3",
        }),
      ),
    );

    render(
      <CustomerPairingFlow
        store={fakeStore({ saveActivation })}
        onPaired={vi.fn()}
      />,
    );
    await fillForm();

    expect(await screen.findByRole("alert")).toHaveTextContent("vault locked");
    // Back on the form; the credential was never persisted.
    expect(
      screen.getByRole("button", { name: "提交配对申请" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "配对成功" })).toBeNull();
  });
});
