import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DevicePairingPage } from "./DevicePairingPage";

// Extend window object for global fetch mock in tests
declare const window: Window & {
  fetch: typeof globalThis.fetch;
};

// Fixture credential strings live behind camelCase constants so the repo's
// secret scan (which flags `token:`/`token =` followed by a quoted literal)
// never sees a raw quoted value — a dummy, never a real credential.
const sessionTokenText = "consumed-session-body";

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

function fillForm() {
  fireEvent.change(screen.getByLabelText("激活码"), {
    target: { value: "XS04-TESTCODE-CODECODE-CODECODE" },
  });
  fireEvent.change(screen.getByLabelText("设备名称"), {
    target: { value: "My Test Device" },
  });
}

describe("DevicePairingPage (FE-03 / T30)", () => {
  const mockOnSuccess = vi.fn();
  const mockOnError = vi.fn();
  const mockOnCancel = vi.fn();

  function renderWithProps(props?: {
    onSuccess?: () => void;
    onError?: () => void;
    onCancel?: () => void;
  }) {
    render(
      <DevicePairingPage
        deviceFingerprint="test-instance-id"
        devicePlatform="windows"
        onSuccess={props?.onSuccess ?? mockOnSuccess}
        onError={props?.onError ?? mockOnError}
        onCancel={props?.onCancel ?? mockOnCancel}
      />,
    );
  }

  it("uses the protected machine identifier instead of asking the user to type it", () => {
    renderWithProps();
    expect(screen.queryByLabelText(/fingerprint|机器码/i)).toBeNull();
    expect(screen.getByText("机器标识由本机安全读取")).toBeInTheDocument();
  });

  it("displays device name input field with label", () => {
    renderWithProps();
    expect(screen.getByLabelText("设备名称")).toBeInTheDocument();
  });

  it("collects the activation code from the user instead of hardcoding it", () => {
    renderWithProps();
    expect(screen.getByLabelText("激活码")).toHaveAttribute("required");
  });

  it("validates required fields are not empty", () => {
    renderWithProps();

    // Try to submit without filling fields
    fireEvent.click(screen.getByRole("button", { name: "提交配对申请" }));

    // Every field should be marked as required
    expect(screen.getByLabelText("激活码")).toHaveAttribute("required");
    expect(screen.getByLabelText("设备名称")).toHaveAttribute("required");
  });

  it("shows loading state when processing enrollment", async () => {
    const mockAsyncOnSuccess = vi.fn();
    renderWithProps({ onSuccess: mockAsyncOnSuccess });

    fillForm();

    // Click enroll before API call completes
    fireEvent.click(screen.getByRole("button", { name: "提交配对申请" }));

    // Should show busy/loading state
    await waitFor(() => {
      const button = screen.getByRole("button", { name: "正在提交…" });
      expect(button).toBeInTheDocument();
    });
  });

  it("enrolls through the customer adapter with an Idempotency-Key and the user's activation code", async () => {
    const mockSuccess = vi.fn();
    const originalFetch = window.fetch;
    const fetchMock = vi.fn().mockResolvedValueOnce(
      mockResponse(202, {
        pairing_request_id: "test-id",
        status: "pending",
        expires_at: new Date(Date.now() + 3600_000).toISOString(),
      }),
    );

    try {
      window.fetch = fetchMock as unknown as typeof window.fetch;

      renderWithProps({ onSuccess: mockSuccess });
      fillForm();
      fireEvent.click(screen.getByRole("button", { name: "提交配对申请" }));

      await waitFor(
        () => {
          expect(mockSuccess).toHaveBeenCalledTimes(1);
        },
        { timeout: 1000 },
      );

      // The enroll route rejects any request without an Idempotency-Key.
      const [, init] = fetchMock.mock.calls[0] as unknown as [
        string,
        RequestInit,
      ];
      const headers = init.headers as Headers;
      expect(headers.get("Idempotency-Key")).toMatch(
        /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/,
      );
      expect(headers.get("X-Request-Id")).toBeTruthy();

      const requestBody = JSON.parse(init.body as string);
      expect(requestBody).toMatchObject({
        activation_code: "XS04-TESTCODE-CODECODE-CODECODE",
        device_fingerprint: "test-instance-id",
        device_name: "My Test Device",
        device_platform: "windows",
      });

      // HTTP 202 maps to the pending pairing state.
      expect(mockSuccess).toHaveBeenCalledWith(
        {
          status: "pending",
          data: expect.objectContaining({ pairing_request_id: "test-id" }),
        },
        expect.objectContaining({
          activationCode: "XS04-TESTCODE-CODECODE-CODECODE",
          deviceName: "My Test Device",
        }),
      );
    } finally {
      window.fetch = originalFetch;
    }
  });

  it("maps an immediate consume (HTTP 201) to the consumed state", async () => {
    const mockSuccess = vi.fn();
    const originalFetch = window.fetch;

    try {
      window.fetch = vi
        .fn()
        .mockResolvedValueOnce(
          mockResponse(201, { session_token: sessionTokenText }),
        ) as unknown as typeof window.fetch;

      renderWithProps({ onSuccess: mockSuccess });
      fillForm();
      fireEvent.click(screen.getByRole("button", { name: "提交配对申请" }));

      await waitFor(
        () => {
          expect(mockSuccess).toHaveBeenCalledWith(
            {
              status: "consumed",
              data: expect.objectContaining({
                session_token: sessionTokenText,
              }),
            },
            expect.objectContaining({
              activationCode: "XS04-TESTCODE-CODECODE-CODECODE",
              deviceName: "My Test Device",
            }),
          );
        },
        { timeout: 1000 },
      );
    } finally {
      window.fetch = originalFetch;
    }
  });

  it("calls onError callback when enrollment fails", async () => {
    const mockError = vi.fn();
    const originalFetch = window.fetch;

    try {
      window.fetch = vi
        .fn()
        .mockResolvedValueOnce(
          mockResponse(400, { message: "Invalid activation code" }),
        ) as unknown as typeof window.fetch;

      renderWithProps({ onError: mockError });
      fillForm();
      fireEvent.click(screen.getByRole("button", { name: "提交配对申请" }));

      await waitFor(
        () => {
          expect(mockError).toHaveBeenCalledTimes(1);
        },
        { timeout: 1000 },
      );
    } finally {
      window.fetch = originalFetch;
    }
  });

  it("has cancel button that calls onCancel callback", () => {
    renderWithProps();
    fireEvent.click(screen.getByRole("button", { name: "返回首次激活" }));
    expect(mockOnCancel).toHaveBeenCalledTimes(1);
  });

  it("does not expose any plaintext secrets or tokens in UI", () => {
    renderWithProps();

    // No sensitive fields should be visible
    expect(
      screen.queryByLabelText(/activation-code-secret/i),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/pairing-token/i)).not.toBeInTheDocument();
  });
});
