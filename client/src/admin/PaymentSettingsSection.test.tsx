import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ControlSettings } from "../api";

import { PaymentSettingsSection } from "./PaymentSettingsSection";

vi.mock("../api.admin", () => ({
  getCustomerPaymentSettings: vi.fn(),
  updateCustomerPaymentZPay: vi.fn(),
  adminWrite: vi.fn(),
  selfCheckWechatNative: vi.fn(),
}));

import {
  adminWrite,
  getCustomerPaymentSettings,
  selfCheckWechatNative,
  updateCustomerPaymentZPay,
} from "../api.admin";

const controlSettings: ControlSettings = {
  providers: {} as ControlSettings["providers"],
  runtime: {} as ControlSettings["runtime"],
  billing: {
    internal_base_unit_price_fen: 10,
    charged_unit_price_fen: 10,
    oral_unit_price_fen: 20,
    min_recharge_fen: 5000,
    recharge_step_fen: 1000,
  },
  zpay: {
    provider: "zpay" as const,
    configured: true,
    config: {
      pid: "pid-1",
      key: "已配置（掩码）",
      enabled_channels: "alipay,wxpay",
    },
  },
  deployment: {
    gateway_url: "https://pay.example.com",
    notify_url: "https://api.example.com/notify",
    return_url: "https://api.example.com/return",
  },
};

describe("PaymentSettingsSection (A-01/A-03)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getCustomerPaymentSettings).mockResolvedValue(controlSettings);
  });

  it("加载后显示默认通道和官方支付标识，不再提供旧版价格设置", async () => {
    render(<PaymentSettingsSection />);
    await waitFor(() =>
      expect(vi.mocked(getCustomerPaymentSettings)).toHaveBeenCalled(),
    );
    expect(
      screen.getByRole("button", { name: "保存 ZPay 设置" }),
    ).toBeEnabled();
    expect(screen.getByRole("button", { name: "保存默认通道" })).toBeEnabled();
    expect(
      screen.queryByText("充值限制与旧版兼容价格"),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText("最低充值（分）")).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: "支付宝" })).toHaveAttribute(
      "src",
      expect.stringContaining("alipay.ico"),
    );
    expect(screen.getByRole("img", { name: "微信支付" })).toHaveAttribute(
      "src",
      expect.stringContaining("wechat-pay.ico"),
    );
  });

  it("加载完成前禁止保存任何支付配置", async () => {
    vi.mocked(getCustomerPaymentSettings).mockReturnValue(
      new Promise(() => {
        /* 挂起，模拟加载中 */
      }),
    );
    render(<PaymentSettingsSection />);
    expect(screen.getByRole("button", { name: "保存默认通道" })).toBeDisabled();
  });

  it("保存 ZPay 直接确认，自动记录操作且不要求重复输入", async () => {
    vi.mocked(updateCustomerPaymentZPay).mockResolvedValue(
      controlSettings.zpay,
    );
    render(<PaymentSettingsSection />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "保存 ZPay 设置" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "保存 ZPay 设置" }));

    const dialog = screen.getByRole("dialog", { name: "保存 ZPay 支付设置" });
    expect(dialog).toBeInTheDocument();

    expect(screen.queryByLabelText("操作原因")).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("我已知晓该操作的影响"),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认保存 ZPay 设置" }));

    await waitFor(() =>
      expect(vi.mocked(updateCustomerPaymentZPay)).toHaveBeenCalledTimes(1),
    );
    expect(vi.mocked(updateCustomerPaymentZPay)).toHaveBeenCalledWith(
      { pid: "pid-1", key: "", enabled_channels: ["alipay", "wxpay"] },
      "保存 ZPay 设置",
      expect.any(String),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "保存 ZPay 支付设置" }),
      ).not.toBeInTheDocument(),
    );
    expect(await screen.findByText("ZPay 设置已保存。")).toBeInTheDocument();
  });

  it("管理员确认默认通道后才保存，微信密钥留空不会回填掩码", async () => {
    vi.mocked(adminWrite).mockResolvedValue({
      active_provider: "wechat_native",
    });
    render(<PaymentSettingsSection />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "保存默认通道" }),
      ).toBeEnabled(),
    );
    fireEvent.change(screen.getByLabelText("默认充值通道"), {
      target: { value: "wechat_native" },
    });
    expect(screen.getByLabelText("API v3 密钥")).toHaveValue("");
    expect(screen.getByLabelText("商户私钥（PEM）")).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: "保存默认通道" }));
    expect(adminWrite).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认保存默认通道" }));
    await waitFor(() =>
      expect(adminWrite).toHaveBeenCalledWith(
        "/api/control/settings/customer-payments/provider",
        {
          active_provider: "wechat_native",
          wechat_native: {
            appid: "",
            mchid: "",
            serial_no: "",
            api_v3_key: "",
            private_key: "",
          },
        },
        "保存默认通道",
        "保存默认通道失败。",
        expect.any(String),
        "PATCH",
      ),
    );
    expect(
      await screen.findByText("当前默认：微信官方（Native）"),
    ).toBeInTheDocument();
  });

  it("保存微信官方配置使用受保护写请求且成功后清空新密钥", async () => {
    vi.mocked(adminWrite).mockResolvedValue({
      provider: "wechat_native",
      configured: true,
      config: {
        appid: "app-test",
        api_v3_key: "********",
        private_key: "********",
      },
    });
    render(<PaymentSettingsSection />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "保存默认通道" }),
      ).toBeEnabled(),
    );
    fireEvent.change(screen.getByLabelText("默认充值通道"), {
      target: { value: "wechat_native" },
    });
    fireEvent.change(screen.getByLabelText("AppID"), {
      target: { value: "app-test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存微信官方设置" }));
    fireEvent.click(
      screen.getByRole("button", { name: "确认保存微信官方设置" }),
    );
    await waitFor(() =>
      expect(adminWrite).toHaveBeenCalledWith(
        "/api/control/settings/customer-payments/wechat-native",
        {
          config: {
            appid: "app-test",
            mchid: "",
            serial_no: "",
            api_v3_key: "",
            private_key: "",
          },
        },
        "保存微信官方设置",
        "保存微信官方设置失败。",
        expect.any(String),
        "PATCH",
      ),
    );
    expect(await screen.findByText("微信官方设置已保存。")).toBeInTheDocument();
    expect(screen.getByLabelText("API v3 密钥")).toHaveValue("");
    expect(screen.getByLabelText("商户私钥（PEM）")).toHaveValue("");
  });

  it("只读管理员不能修改默认通道及商户配置", async () => {
    render(<PaymentSettingsSection readOnly />);
    await screen.findByDisplayValue("pid-1");
    expect(screen.getByLabelText("默认充值通道")).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "保存 ZPay 设置" }),
    ).toBeDisabled();
  });

  it("确认框内失败时错误留在对话框内且不关闭", async () => {
    vi.mocked(updateCustomerPaymentZPay).mockRejectedValue(
      new Error("网关校验失败"),
    );
    render(<PaymentSettingsSection />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "保存 ZPay 设置" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "保存 ZPay 设置" }));
    fireEvent.click(screen.getByRole("button", { name: "确认保存 ZPay 设置" }));

    await waitFor(() =>
      expect(screen.getByText("网关校验失败")).toBeInTheDocument(),
    );
    // 失败后对话框仍在，可修正后重试
    expect(
      screen.getByRole("dialog", { name: "保存 ZPay 支付设置" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认保存 ZPay 设置" }));
    await waitFor(() =>
      expect(vi.mocked(updateCustomerPaymentZPay)).toHaveBeenCalledTimes(2),
    );
    expect(vi.mocked(updateCustomerPaymentZPay).mock.calls[0][2]).toBe(
      vi.mocked(updateCustomerPaymentZPay).mock.calls[1][2],
    );
  });

  it("设置默认通道一起提交刚填写的商户配置，回调未就绪不会误报保存失败", async () => {
    vi.mocked(adminWrite).mockResolvedValue({
      active_provider: "zpay",
      zpay: controlSettings.zpay,
      deployment: { ready: false, message: "回调域名尚未配置，充值暂不可用。" },
    });
    render(<PaymentSettingsSection />);
    await screen.findByDisplayValue("pid-1");
    fireEvent.change(screen.getByLabelText("ZPay 商户 PID"), {
      target: { value: "new-pid" },
    });
    fireEvent.change(screen.getByLabelText("新商户密钥"), {
      target: { value: "local-test-merchant-key" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存默认通道" }));
    expect(adminWrite).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认保存默认通道" }));
    await waitFor(() =>
      expect(adminWrite).toHaveBeenCalledWith(
        "/api/control/settings/customer-payments/provider",
        {
          active_provider: "zpay",
          zpay: {
            pid: "new-pid",
            key: "local-test-merchant-key",
            enabled_channels: ["alipay", "wxpay"],
          },
        },
        "保存默认通道",
        "保存默认通道失败。",
        expect.any(String),
        "PATCH",
      ),
    );
    expect(
      await screen.findByText("回调域名尚未配置，充值暂不可用。"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("新商户密钥")).toHaveValue("");
  });
});

describe("PaymentSettingsSection 凭据自检", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getCustomerPaymentSettings).mockResolvedValue({
      ...controlSettings,
      active_provider: "wechat_native",
      wechat_native: {
        provider: "wechat_native",
        configured: true,
        config: {
          appid: "app-live",
          mchid: "mch-live",
          serial_no: "SERIAL01",
          api_v3_key: "********",
          private_key: "********",
        },
      },
    } as ControlSettings);
  });

  it("自检通过时展示平台证书数量", async () => {
    vi.mocked(selfCheckWechatNative).mockResolvedValue({
      ok: true,
      code: null,
      message: "商户凭据有效：签名被微信接受，平台证书解密成功。",
      platform_certificates: 2,
    });
    render(<PaymentSettingsSection />);
    const check = await screen.findByRole("button", { name: "凭据自检" });
    fireEvent.click(check);
    expect(
      await screen.findByText("凭据自检通过（平台证书 2 张）。"),
    ).toBeInTheDocument();
    expect(selfCheckWechatNative).toHaveBeenCalledTimes(1);
  });

  it("自检失败时透出微信侧错误信息", async () => {
    vi.mocked(selfCheckWechatNative).mockResolvedValue({
      ok: false,
      code: "WECHAT_SELF_CHECK_FAILED",
      message:
        "WeChat API request failed (HTTP 401 code=SIGN_ERROR message=签名错误)",
    });
    render(<PaymentSettingsSection />);
    fireEvent.click(await screen.findByRole("button", { name: "凭据自检" }));
    expect(
      await screen.findByText(/凭据自检未通过：.*SIGN_ERROR/),
    ).toBeInTheDocument();
  });
});
