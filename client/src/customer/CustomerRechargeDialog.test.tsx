import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CustomerRechargeDialog } from "./CustomerRechargeDialog";
import type { CustomerCredentialStore } from "./useCustomerSession";

const sessionCredentialText = "recharge-dialog-session-credential";

function fakeStore(): CustomerCredentialStore {
  return {
    loadDeviceCredentialToken: vi.fn().mockResolvedValue(null),
    loadSessionToken: vi.fn().mockResolvedValue(sessionCredentialText),
    saveActivation: vi.fn().mockResolvedValue(undefined),
    saveSessionToken: vi.fn().mockResolvedValue(undefined),
    clearSessionToken: vi.fn().mockResolvedValue(undefined),
    clearAllCredentials: vi.fn().mockResolvedValue(undefined),
    deviceInstanceId: vi.fn().mockResolvedValue("test-instance-id"),
    devicePlatform: () => "windows",
    // 「记住密码」在这些用例里不参与断言，给出满足接口的最小桩。
    loadRememberedLogin: async () => null,
    saveRememberedLogin: async () => {},
    clearRememberedLogin: async () => {},
  };
}

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

describe("CustomerRechargeDialog", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it.each([
    "https://payment.example/pay",
    "weixin://wxpay/bizpayurl?pr=local-test",
  ])(
    "creates a payment code and renders its QR image inside the app",
    async (paymentUrl) => {
      const onOrderCreated = vi.fn();
      const fetchMock = vi.fn((url: string, options?: RequestInit) => {
        if (url.endsWith("/api/customer/wallet")) {
          return jsonResponse({
            available_credits: 12,
            reserved_credits: 0,
            internal_unit_price_fen: 1000,
            min_recharge_fen: 10000,
            recharge_step_fen: 1000,
          });
        }
        if (
          url.endsWith("/api/customer/recharge-orders") &&
          options?.method === "POST"
        ) {
          return jsonResponse(
            {
              order_no: "202608270001",
              status: "PENDING",
              amount_fen: 10000,
              credits: 10,
              gateway_url: "https://payment.example/submit",
              method: "POST",
              form_fields: {},
            },
            201,
          );
        }
        if (url.endsWith("/payment-code")) {
          return jsonResponse({
            order_no: "202608270001",
            amount_fen: 10000,
            credits: 10,
            qr_image_url: "https://payment.example/qr.png",
            payment_url: paymentUrl,
          });
        }
        if (url.endsWith("/api/customer/recharge-orders/202608270001")) {
          return jsonResponse({
            order_no: "202608270001",
            status: "PENDING",
            amount_fen: 10000,
            credits: 10,
            channel: "wechat",
            created_at: "2026-08-27T00:00:00Z",
            paid_at: null,
          });
        }
        throw new Error(`unexpected request: ${url}`);
      });
      vi.stubGlobal("fetch", fetchMock);

      render(
        <CustomerRechargeDialog
          isOpen
          onClose={vi.fn()}
          onOrderCreated={onOrderCreated}
          onPaid={vi.fn()}
          onSessionExpired={vi.fn()}
          store={fakeStore()}
        />,
      );

      fireEvent.click(await screen.findByRole("button", { name: /100 元/ }));

      const qr = await screen.findByRole("img", { name: "充值支付二维码" });
      expect(qr).toHaveAttribute("src", "https://payment.example/qr.png");
      expect(screen.getByText("正在等待支付结果")).toBeInTheDocument();
      expect(screen.getByText(/到账 10 积分/)).toBeInTheDocument();
      expect(onOrderCreated).toHaveBeenCalledOnce();
      if (paymentUrl.startsWith("weixin://")) {
        expect(
          screen.getByText("请使用微信扫一扫完成支付。"),
        ).toBeInTheDocument();
        expect(
          screen.queryByRole("button", { name: "无法扫码？打开支付页面" }),
        ).not.toBeInTheDocument();
      } else {
        expect(
          screen.getByRole("button", { name: "无法扫码？打开支付页面" }),
        ).toBeInTheDocument();
      }
      expect(screen.queryByText(/zpay|微信支付商户|支付宝商户/i)).toBeNull();
      await waitFor(() =>
        expect(
          fetchMock.mock.calls.some(([url]) =>
            String(url).endsWith("/payment-code"),
          ),
        ).toBe(true),
      );
    },
  );

  it("closes on Escape", () => {
    const onClose = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        jsonResponse({
          available_credits: 0,
          reserved_credits: 0,
          internal_unit_price_fen: 1000,
          min_recharge_fen: 10000,
          recharge_step_fen: 1000,
        }),
      ),
    );
    render(
      <CustomerRechargeDialog
        isOpen
        onClose={onClose}
        onOrderCreated={vi.fn()}
        onPaid={vi.fn()}
        onSessionExpired={vi.fn()}
        store={fakeStore()}
      />,
    );

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });
});
