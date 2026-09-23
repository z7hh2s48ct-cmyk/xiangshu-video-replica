import { useState } from "react";
import { customerEnrollDevice } from "../api";
import { CustomerAccessBrand } from "./ActivationPage";

/** Second-device pairing enrollment page (FE-03 / T30).
 * Collects the activation code, device fingerprint and name, and enrolls
 * through the customer API adapter so the request carries the
 * Idempotency-Key the server's enroll route requires (T17 / DEV-02).
 */
export function DevicePairingPage({
  deviceFingerprint,
  devicePlatform,
  initialActivationCode = "",
  initialDeviceName = "",
  onSuccess,
  onError,
  onCancel,
}: {
  deviceFingerprint: string;
  devicePlatform: string;
  initialActivationCode?: string;
  initialDeviceName?: string;
  onSuccess: (
    result: {
      status: "pending" | "consumed";
      data: unknown;
    },
    input: { activationCode: string; deviceName: string },
  ) => void;
  onError: (error: Error) => void;
  onCancel: () => void;
}): React.JSX.Element {
  const [isBusy, setIsBusy] = useState(false);
  const [activationCode, setActivationCode] = useState(initialActivationCode);
  const [deviceName, setDeviceName] = useState(initialDeviceName);

  const handleEnroll = async () => {
    setIsBusy(true);
    try {
      const input = {
        activationCode: activationCode.trim(),
        deviceName: deviceName.trim(),
      };
      const result = await customerEnrollDevice({
        activationCode: input.activationCode,
        deviceFingerprint: deviceFingerprint.trim(),
        deviceName: input.deviceName,
        devicePlatform,
        // Every enrollment attempt gets its own key; the transport adds the
        // X-Request-Id correlation id.
        idempotencyKey: crypto.randomUUID(),
      });
      if (result.status === 202) {
        onSuccess({ status: "pending", data: result.pending }, input);
      } else {
        onSuccess({ status: "consumed", data: result.credential }, input);
      }
    } catch (cause) {
      const error = cause instanceof Error ? cause : new Error(String(cause));
      onError(error);
    } finally {
      setIsBusy(false);
    }
  };

  return (
    <main className="customer-access-shell">
      <section
        className="customer-access-card"
        aria-labelledby="pairing-page-title"
      >
        <CustomerAccessBrand />
        <div className="customer-access-body">
          <h1 id="pairing-page-title">添加已有账号设备</h1>
          <p className="customer-access-lead">
            适用于激活码已完成首次激活，需要将当前电脑加入同一账号的情况。
          </p>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void handleEnroll();
            }}
            className="customer-access-form"
          >
            <div className="form-group">
              <label htmlFor="activation-code">激活码</label>
              <input
                type="text"
                id="activation-code"
                value={activationCode}
                onChange={(e) => setActivationCode(e.target.value)}
                placeholder="XS04-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX"
                autoComplete="off"
                required
                aria-required="true"
              />
            </div>
            <p className="customer-access-machine-note">
              机器标识由本机安全读取
            </p>
            <div className="form-group">
              <label htmlFor="device-name">设备名称</label>
              <input
                type="text"
                id="device-name"
                value={deviceName}
                onChange={(e) => setDeviceName(e.target.value)}
                placeholder="例如：家庭电脑"
                required
                aria-required="true"
              />
            </div>
            <div className="form-actions">
              <button
                type="button"
                className="btn-secondary"
                onClick={onCancel}
                disabled={isBusy}
              >
                返回首次激活
              </button>

              <button
                type="submit"
                className="btn-primary"
                disabled={
                  isBusy ||
                  !activationCode.trim() ||
                  !deviceName.trim() ||
                  !deviceFingerprint
                }
              >
                {isBusy ? "正在提交…" : "提交配对申请"}
              </button>
            </div>
          </form>
        </div>
      </section>
    </main>
  );
}
