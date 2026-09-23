import { type FormEvent, useState } from "react";

import { type CustomerApiError, customerVisibleErrorMessage } from "../api";
import activationDeviceIcon from "../assets/brand/activation-device.png";
import activationLockIcon from "../assets/brand/activation-lock.png";
import zhongshuLogoMark from "../assets/brand/zhongshu-logo-mark.svg";
import type { CustomerActivationFormInput } from "./useCustomerSession";

export function CustomerAccessBrand() {
  return (
    <div className="customer-access-brand">
      <img alt="众墅之家" src={zhongshuLogoMark} />
      <span className="customer-access-brand__name">
        <strong>众墅之家</strong>
        <small>AI 即创</small>
      </span>
    </div>
  );
}

/** The first-run activation form (FE-02): redeem an activation code, name
 * this device, and land in the workspace. The dev doc §13.2 client
 * behaviours drive the error copy: the anti-enumeration rejection shows the
 * server's unified message verbatim (never a guessed reason), a rate limit
 * shows the Retry-After wait instead of looping submits, and an idempotency
 * conflict surfaces the request id for the audit-trail lookup. */
export function ActivationPage({
  onActivate,
  isBusy,
  error,
  onPairDevice,
}: {
  onActivate(input: CustomerActivationFormInput): void;
  isBusy: boolean;
  error: CustomerApiError | null;
  onPairDevice?(): void;
}) {
  const [activationCode, setActivationCode] = useState("");
  const [deviceName, setDeviceName] = useState("");

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isBusy || !activationCode.trim() || !deviceName.trim()) {
      return;
    }
    onActivate({
      activationCode: activationCode.trim(),
      deviceName: deviceName.trim(),
    });
  };

  return (
    <main className="customer-access-shell">
      <section
        className="customer-access-card"
        aria-labelledby="activation-title"
      >
        <CustomerAccessBrand />
        <div className="customer-access-body">
          <h1 id="activation-title">激活众墅之家 · AI 即创</h1>
          <p className="customer-access-lead">
            输入有效激活码即可进入，本机会自动识别并关联您的账号。
          </p>
          <form className="customer-access-form" onSubmit={handleSubmit}>
            <label>
              激活码
              <span className="customer-access-input customer-access-input--trailing">
                <input
                  value={activationCode}
                  onChange={(event) => setActivationCode(event.target.value)}
                  autoComplete="off"
                  placeholder="XS04-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX"
                  disabled={isBusy}
                />
                <img alt="" aria-hidden="true" src={activationLockIcon} />
              </span>
            </label>
            <label>
              设备名称
              <span className="customer-access-input customer-access-input--leading">
                <img alt="" aria-hidden="true" src={activationDeviceIcon} />
                <input
                  value={deviceName}
                  onChange={(event) => setDeviceName(event.target.value)}
                  autoComplete="off"
                  placeholder="例如：工作电脑"
                  disabled={isBusy}
                />
              </span>
            </label>
            {error ? (
              <p className="form-error">{activationErrorText(error)}</p>
            ) : null}
            <button type="submit" disabled={isBusy}>
              {isBusy ? "正在激活…" : "激活并进入工作台"}
            </button>
            {onPairDevice ? (
              <button type="button" disabled={isBusy} onClick={onPairDevice}>
                已有账号，添加或更换设备
              </button>
            ) : null}
          </form>
        </div>
        <footer className="customer-access-footer">
          遇到问题？请联系服务人员
        </footer>
      </section>
    </main>
  );
}

function activationErrorText(error: CustomerApiError): string {
  if (error.code === "PAIRING_APPROVAL_REQUIRED") {
    return "该账号已激活，请使用设备配对并由主设备或管理员审批。";
  }
  if (error.code === "ACTIVATION_UNAVAILABLE") {
    return customerVisibleErrorMessage(error);
  }
  switch (error.kind) {
    case "rate-limited":
      return error.retryAfterSeconds !== undefined
        ? `${error.message}，请等待 ${error.retryAfterSeconds} 秒后重试`
        : error.message;
    case "idempotency-conflict":
      return error.requestId
        ? `${error.message}（请求编号 ${error.requestId}，请联系客服核查）`
        : error.message;
    default:
      return error.message;
  }
}
