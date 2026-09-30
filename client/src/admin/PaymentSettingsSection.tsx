import { type FormEvent, useEffect, useRef, useState } from "react";
import {
  adminWrite,
  type CustomerPaymentSettings,
  getCustomerPaymentSettings,
  selfCheckWechatNative,
  updateCustomerPaymentZPay,
} from "../api.admin";
import alipayLogo from "../assets/payments/alipay.ico";
import wechatLogo from "../assets/payments/wechat-pay.ico";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";

type Provider = "zpay" | "wechat_native";
type WeChatSettings = {
  provider: string;
  configured: boolean;
  config: Record<string, string>;
};
type PaymentSettings = CustomerPaymentSettings & {
  active_provider?: Provider;
  wechat_native?: WeChatSettings;
  deployment?: { ready: boolean; message: string };
};
type PendingConfirm = "zpay" | "wechat" | "provider" | null;
const providerName = (provider: Provider) =>
  provider === "zpay" ? "ZPay" : "微信官方（Native）";
const confirmLabels = {
  zpay: "保存 ZPay 设置",
  wechat: "保存微信官方设置",
  provider: "保存默认通道",
};
const emptyWechat = {
  appid: "",
  mchid: "",
  serial_no: "",
  api_v3_key: "",
  private_key: "",
  public_key_id: "",
  public_key: "",
};

/** Merchant secrets are never prefilled; blank secret fields retain their saved values. */
export function PaymentSettingsSection({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [settings, setSettings] = useState<PaymentSettings | null>(null);
  const [provider, setProvider] = useState<Provider>("zpay");
  const [zpayPid, setZpayPid] = useState("");
  const [zpayKey, setZpayKey] = useState("");
  const [channels, setChannels] = useState<Array<"alipay" | "wxpay">>([
    "alipay",
    "wxpay",
  ]);
  const [wechat, setWechat] = useState(emptyWechat);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [pendingConfirm, setPendingConfirm] = useState<PendingConfirm>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [selfChecking, setSelfChecking] = useState(false);
  const [confirmError, setConfirmError] = useState("");
  const saving = useRef(false);
  const retry = useRef<{ fingerprint: string; key: string } | null>(null);
  const disabled = readOnly || !settings || pendingConfirm !== null;

  useEffect(() => {
    let cancelled = false;
    void getCustomerPaymentSettings()
      .then((value) => {
        if (cancelled) return;
        const next = value as PaymentSettings;
        setSettings(next);
        setProvider(next.active_provider ?? "zpay");
        setZpayPid(next.zpay.config.pid ?? "");
        setChannels(parseChannels(next.zpay.config.enabled_channels));
        setWechat({
          ...emptyWechat,
          appid: next.wechat_native?.config.appid ?? "",
          mchid: next.wechat_native?.config.mchid ?? "",
          serial_no: next.wechat_native?.config.serial_no ?? "",
          public_key_id: next.wechat_native?.config.public_key_id ?? "",
          public_key: next.wechat_native?.config.public_key ?? "",
        });
      })
      .catch((cause) => {
        if (!cancelled)
          setError(
            cause instanceof Error
              ? `加载失败：${cause.message}`
              : "加载支付配置失败。",
          );
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function requestSave(
    event: FormEvent<HTMLFormElement>,
    kind: Exclude<PendingConfirm, null>,
  ) {
    event.preventDefault();
    if (disabled || saving.current) return;
    setNotice("");
    setError("");
    setConfirmError("");
    setPendingConfirm(kind);
  }

  /** 真实调一次微信验证已保存的三件套：配错在这里暴露，不等第一个客户下单。 */
  async function runSelfCheck() {
    if (readOnly || selfChecking) return;
    setNotice("");
    setError("");
    setSelfChecking(true);
    try {
      const result = await selfCheckWechatNative();
      if (result.ok)
        setNotice(
          result.verification_mode === "public_key"
            ? "凭据自检通过（微信支付公钥模式）。"
            : `凭据自检通过（平台证书 ${result.platform_certificates ?? 0} 张）。`,
        );
      else setError(`凭据自检未通过：${result.message}`);
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? cause.message
          : "凭据自检失败。",
      );
    } finally {
      setSelfChecking(false);
    }
  }

  async function runConfirmedSave() {
    if (!pendingConfirm || !settings || readOnly || saving.current) return;
    const reason = confirmLabels[pendingConfirm];
    const fingerprint = JSON.stringify({
      pendingConfirm,
      provider,
      zpayPid,
      zpayKey,
      channels,
      wechat,
      reason,
    });
    if (retry.current?.fingerprint !== fingerprint)
      retry.current = { fingerprint, key: crypto.randomUUID() };
    saving.current = true;
    setConfirmBusy(true);
    setConfirmError("");
    try {
      if (pendingConfirm === "zpay") {
        const zpay = await updateCustomerPaymentZPay(
          { pid: zpayPid, key: zpayKey, enabled_channels: channels },
          reason,
          retry.current.key,
        );
        setSettings({ ...settings, zpay });
        setZpayKey("");
        setNotice("ZPay 设置已保存。");
      } else if (pendingConfirm === "wechat") {
        const next = await adminWrite<WeChatSettings>(
          "/api/control/settings/customer-payments/wechat-native",
          { config: wechat },
          reason,
          "保存微信官方设置失败。",
          retry.current.key,
          "PATCH",
        );
        setSettings({ ...settings, wechat_native: next });
        setWechat({ ...wechat, api_v3_key: "", private_key: "" });
        setNotice("微信官方设置已保存。");
      } else {
        const next = await adminWrite<
          PaymentSettings & { active_provider: Provider }
        >(
          "/api/control/settings/customer-payments/provider",
          {
            active_provider: provider,
            ...(provider === "zpay"
              ? {
                  zpay: {
                    pid: zpayPid,
                    key: zpayKey,
                    enabled_channels: channels,
                  },
                }
              : { wechat_native: wechat }),
          },
          reason,
          "保存默认通道失败。",
          retry.current.key,
          "PATCH",
        );
        setSettings({ ...settings, ...next });
        if (provider === "zpay") setZpayKey("");
        else setWechat({ ...wechat, api_v3_key: "", private_key: "" });
        setNotice("商户配置及默认充值通道已保存。");
      }
      retry.current = null;
      setPendingConfirm(null);
    } catch (cause) {
      setConfirmError(
        cause instanceof Error && cause.message ? cause.message : "保存失败。",
      );
    } finally {
      saving.current = false;
      setConfirmBusy(false);
    }
  }

  return (
    <section
      aria-label="支付设置"
      className="admin-panel admin-payment-settings"
    >
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {settings?.deployment?.ready === false ? (
        <p className="admin-hint" role="status">
          {settings.deployment.message}
        </p>
      ) : null}
      <form
        className="admin-form"
        onSubmit={(event) => requestSave(event, "provider")}
      >
        <h2>充值通道</h2>
        <p>
          {settings
            ? `当前默认：${providerName(settings.active_provider ?? "zpay")}`
            : "正在加载支付配置…"}
        </p>
        <label>
          默认充值通道
          <select
            disabled={disabled}
            value={provider}
            onChange={(event) => setProvider(event.target.value as Provider)}
          >
            <option value="zpay">ZPay</option>
            <option value="wechat_native">微信官方（Native）</option>
          </select>
        </label>
        <p className="admin-hint">
          客户按默认通道充值。切换后，新订单使用新通道，已有订单继续使用原通道。
        </p>
        <button disabled={disabled} type="submit">
          保存默认通道
        </button>
      </form>
      {provider === "zpay" ? (
        <form
          className="admin-form"
          onSubmit={(event) => requestSave(event, "zpay")}
        >
          <h2>ZPay 商户配置</h2>
          <label>
            ZPay 商户 PID
            <input
              disabled={disabled}
              value={zpayPid}
              onChange={(event) => setZpayPid(event.target.value)}
            />
          </label>
          <div className="admin-readonly-field">
            已保存密钥
            <span className="readonly-value">
              {settings?.zpay.config.key || "未配置"}
            </span>
          </div>
          <label>
            新商户密钥
            <input
              autoComplete="new-password"
              disabled={disabled}
              placeholder="留空则保留当前密钥"
              type="password"
              value={zpayKey}
              onChange={(event) => setZpayKey(event.target.value)}
            />
          </label>
          <fieldset
            className="admin-checks admin-payment-channels"
            disabled={disabled}
          >
            <legend>支付方式</legend>
            <label>
              <input
                checked={channels.includes("alipay")}
                type="checkbox"
                onChange={() => toggleChannel("alipay")}
              />
              <img src={alipayLogo} alt="支付宝" />
              <span aria-hidden="true">支付宝</span>
            </label>
            <label>
              <input
                checked={channels.includes("wxpay")}
                type="checkbox"
                onChange={() => toggleChannel("wxpay")}
              />
              <img src={wechatLogo} alt="微信支付" />
              <span aria-hidden="true">微信支付</span>
            </label>
          </fieldset>
          <p className="admin-hint">支付接口地址由系统自动配置，无需填写。</p>
          <button disabled={disabled} type="submit">
            保存 ZPay 设置
          </button>
        </form>
      ) : (
        <form
          className="admin-form"
          onSubmit={(event) => requestSave(event, "wechat")}
        >
          <h2 className="admin-payment-brand">
            <img src={wechatLogo} alt="微信支付" />
            微信官方商户配置
          </h2>
          <label>
            AppID
            <input
              disabled={disabled}
              value={wechat.appid}
              onChange={(event) =>
                setWechat({ ...wechat, appid: event.target.value })
              }
            />
          </label>
          <label>
            商户号（mchid）
            <input
              disabled={disabled}
              value={wechat.mchid}
              onChange={(event) =>
                setWechat({ ...wechat, mchid: event.target.value })
              }
            />
          </label>
          <label>
            商户证书序列号
            <input
              disabled={disabled}
              value={wechat.serial_no}
              onChange={(event) =>
                setWechat({ ...wechat, serial_no: event.target.value })
              }
            />
          </label>
          <label>
            API v3 密钥
            <input
              autoComplete="new-password"
              disabled={disabled}
              type="password"
              placeholder={
                settings?.wechat_native?.configured
                  ? "已配置，留空保留"
                  : "填写 32 字节 API v3 密钥"
              }
              value={wechat.api_v3_key}
              onChange={(event) =>
                setWechat({ ...wechat, api_v3_key: event.target.value })
              }
            />
          </label>
          <label>
            商户私钥（PEM）
            <textarea
              autoComplete="off"
              disabled={disabled}
              rows={4}
              placeholder={
                settings?.wechat_native?.configured
                  ? "已配置，留空保留"
                  : "粘贴商户证书对应的 PEM 私钥"
              }
              value={wechat.private_key}
              onChange={(event) =>
                setWechat({ ...wechat, private_key: event.target.value })
              }
            />
          </label>
          <label>
            微信支付公钥 ID（可选）
            <input
              disabled={disabled}
              placeholder="PUB_KEY_ID_ 开头；未使用微信支付公钥则留空"
              value={wechat.public_key_id}
              onChange={(event) =>
                setWechat({ ...wechat, public_key_id: event.target.value })
              }
            />
          </label>
          <label>
            微信支付公钥（PEM，可选）
            <textarea
              autoComplete="off"
              disabled={disabled}
              rows={4}
              placeholder="粘贴商户平台「API 安全」下载的微信支付公钥"
              value={wechat.public_key}
              onChange={(event) =>
                setWechat({ ...wechat, public_key: event.target.value })
              }
            />
          </label>
          <p className="admin-hint">
            新开通的商户号只能使用微信支付公钥，请与公钥 ID
            成对填写；两项都留空则使用平台证书。
          </p>
          <p className="admin-hint">
            保存默认通道时会一起保存当前商户配置。回调地址使用服务端
            PUBLIC_BASE_URL，需配置可访问的 HTTPS 域名。
          </p>
          <div style={{ display: "flex", gap: "0.75rem" }}>
            <button disabled={disabled} type="submit">
              保存微信官方设置
            </button>
            <button
              disabled={readOnly || selfChecking}
              type="button"
              onClick={() => void runSelfCheck()}
            >
              {selfChecking ? "自检中…" : "凭据自检"}
            </button>
          </div>
        </form>
      )}
      <ConfirmDialog
        busy={confirmBusy}
        confirmLabel={`确认${confirmLabels[pendingConfirm ?? "provider"]}`}
        description={
          pendingConfirm === "provider"
            ? `保存当前商户配置，并将默认充值通道设置为${providerName(provider)}，对新订单生效。`
            : "该操作更新商户收款配置，保存后立即生效。"
        }
        error={confirmError}
        level="standard"
        open={pendingConfirm !== null}
        title={
          pendingConfirm === "zpay"
            ? "保存 ZPay 支付设置"
            : confirmLabels[pendingConfirm ?? "provider"]
        }
        onClose={() => {
          if (!saving.current) {
            setPendingConfirm(null);
            setConfirmError("");
          }
        }}
        onConfirm={() => void runConfirmedSave()}
      />
    </section>
  );

  function toggleChannel(channel: "alipay" | "wxpay") {
    setChannels((current) =>
      current.includes(channel)
        ? current.filter((item) => item !== channel)
        : [...current, channel],
    );
  }
}

function parseChannels(value: unknown): Array<"alipay" | "wxpay"> {
  if (typeof value !== "string") return ["alipay"];
  const next = value
    .split(",")
    .filter(
      (item): item is "alipay" | "wxpay" =>
        item === "alipay" || item === "wxpay",
    );
  return next.length ? next : ["alipay"];
}
