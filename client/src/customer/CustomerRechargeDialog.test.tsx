import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";
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
    // CW-062：身份缓存不参与这些用例的断言，给出满足接口的最小桩。
    loadIdentity: async () => null,
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

// 管理端配置的档位（100 元 → 10 积分，无权益）：客户端不再内置金额列表。
const rechargePackagePayload = {
  items: [
    {
      id: "pkg-basic",
      name: "标准档",
      amount_fen: 10000,
      credits: 10,
      discount_rate: null,
      discount_interfaces: [],
      sort_order: 0,
      is_active: true,
      version: 1,
      created_at: "2026-09-22 10:00:00",
      updated_at: "2026-09-22 10:00:00",
    },
  ],
};

// 需求原文场景：1998 元到账 2000 积分 + 视频生成 9 折。
const discountPackagePayload = {
  items: [
    {
      id: "pkg-premium",
      name: "尊享档",
      amount_fen: 199800,
      credits: 2000,
      discount_rate: "0.9000",
      discount_interfaces: ["video_generation"],
      sort_order: 1,
      is_active: true,
      version: 2,
      created_at: "2026-09-22 10:00:00",
      updated_at: "2026-09-22 10:00:00",
    },
  ],
};

const walletPayload = {
  available_credits: 12,
  reserved_credits: 0,
  internal_unit_price_fen: 1000,
  min_recharge_fen: 10000,
  recharge_step_fen: 1000,
};

function renderDialog(
  props: Partial<ComponentProps<typeof CustomerRechargeDialog>> = {},
) {
  return render(
    <CustomerRechargeDialog
      isOpen
      onClose={vi.fn()}
      onOrderCreated={vi.fn()}
      onPaid={vi.fn()}
      onSessionExpired={vi.fn()}
      store={fakeStore()}
      {...props}
    />,
  );
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
        if (url.endsWith("/api/customer/recharge-packages")) {
          return jsonResponse(rechargePackagePayload);
        }
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

      renderDialog({ onOrderCreated });

      // 档位来自管理端配置：点击套餐卡片按套餐下单（金额必须等于套餐金额）。
      fireEvent.click(await screen.findByRole("button", { name: /100元/ }));

      const qr = await screen.findByRole("img", { name: "充值支付二维码" });
      expect(qr).toHaveAttribute("src", "https://payment.example/qr.png");
      expect(screen.getByText("正在等待支付结果")).toBeInTheDocument();
      expect(screen.getByText(/到账 10 积分/)).toBeInTheDocument();
      expect(onOrderCreated).toHaveBeenCalledOnce();
      const createCall = fetchMock.mock.calls.find(
        ([url, options]) =>
          String(url).endsWith("/api/customer/recharge-orders") &&
          options?.method === "POST",
      );
      expect(createCall?.[1]?.body).toBe(
        JSON.stringify({ amount_fen: 10000, package_id: "pkg-basic" }),
      );
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
      vi.fn((url: string) => {
        if (String(url).endsWith("/api/customer/recharge-packages")) {
          return jsonResponse({ items: [] });
        }
        return jsonResponse(walletPayload);
      }),
    );
    renderDialog({ onClose });

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("disables packages priced below the effective minimum recharge amount", async () => {
    // 低于起充额的档位（50 元 < 100 元）后端下单必 422，对话框不得放行点击。
    const belowMinimum = {
      ...rechargePackagePayload.items[0],
      id: "pkg-50",
      name: "五十元档",
      amount_fen: 5000,
      credits: 5,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (String(url).endsWith("/api/customer/recharge-packages")) {
          return jsonResponse({
            items: [belowMinimum, ...rechargePackagePayload.items],
          });
        }
        return jsonResponse(walletPayload);
      }),
    );
    renderDialog();
    const lowCard = await screen.findByRole("button", { name: /五十元档/ });
    expect(lowCard).toBeDisabled();
    expect(lowCard).toHaveTextContent("低于起充金额");
    const okCard = screen.getByRole("button", { name: /标准档/ });
    expect(okCard).toBeEnabled();
  });

  it("keeps the custom-amount path when packages are unavailable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (String(url).endsWith("/api/customer/recharge-packages")) {
          return Promise.reject(new Error("套餐接口不可用"));
        }
        return jsonResponse(walletPayload);
      }),
    );
    renderDialog();

    expect(
      await screen.findByText("充值套餐暂不可用，可使用自定义金额充值。"),
    ).toBeInTheDocument();
    expect(screen.queryByText("标准档")).toBeNull();
    expect(screen.getByLabelText("自定义金额（元）")).toBeInTheDocument();
  });

  it("hints at a matching package but still orders the raw custom amount", async () => {
    const fetchMock = vi.fn((url: string, options?: RequestInit) => {
      if (String(url).endsWith("/api/customer/recharge-packages")) {
        return jsonResponse(rechargePackagePayload);
      }
      if (String(url).endsWith("/api/customer/wallet")) {
        return jsonResponse(walletPayload);
      }
      if (
        String(url).endsWith("/api/customer/recharge-orders") &&
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
      if (String(url).endsWith("/payment-code")) {
        return jsonResponse({
          order_no: "202608270001",
          amount_fen: 10000,
          credits: 10,
          qr_image_url: "https://payment.example/qr.png",
          payment_url: "https://payment.example/pay",
        });
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderDialog();

    fireEvent.change(screen.getByLabelText("自定义金额（元）"), {
      target: { value: "100" },
    });
    expect(await screen.findByText(/该金额有对应套餐/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "生成支付二维码" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([url, options]) =>
            String(url).endsWith("/api/customer/recharge-orders") &&
            options?.method === "POST",
        ),
      ).toBe(true),
    );
    const createCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url).endsWith("/api/customer/recharge-orders") &&
        options?.method === "POST",
    );
    // 自定义金额不带 package_id：按基础汇率到账，不享受套餐赠送/权益。
    expect(createCall?.[1]?.body).toBe(JSON.stringify({ amount_fen: 10000 }));
  });

  it("marks the package recommended by the wallet entry as selected", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (String(url).endsWith("/api/customer/recharge-packages")) {
          return jsonResponse(discountPackagePayload);
        }
        if (String(url).endsWith("/api/customer/wallet")) {
          return jsonResponse(walletPayload);
        }
        return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
      }),
    );
    renderDialog({
      suggestedAmountYuan: 1998,
      suggestedPackageId: "pkg-premium",
    });

    const card = await screen.findByRole("button", { name: /尊享档/ });
    expect(card).toHaveClass("recharge-package-card--suggested");
    expect(card).toHaveTextContent("已选套餐");
    expect(card).toHaveTextContent("视频生成 9折");
    expect(card).toHaveTextContent("含赠送 1801 积分");
    expect(screen.getByLabelText("自定义金额（元）")).toHaveValue(1998);
  });
});
