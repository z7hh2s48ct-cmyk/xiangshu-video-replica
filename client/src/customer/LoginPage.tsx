import type { CustomerApiError } from "../api";
import { CustomerAccessBrand } from "./ActivationPage";
import type { CustomerSessionConflict } from "./useCustomerSession";

/** The returning-device login screen (FE-02): the stored device credential
 * is the login material, so the user never retypes the activation code.
 *
 * When the other device holds the live lease (§13.2 OTHER_DEVICE_ONLINE) the
 * masked hint, slot number, and lease expiry are shown read-only — the
 * explicit takeover (switch) flow is T30's SessionConflictDialog and must
 * not be offered before the server can be asked to switch atomically. */
export function LoginPage({
  onRetryLogin,
  isBusy,
  error,
  conflict,
}: {
  onRetryLogin(): void;
  isBusy: boolean;
  error: CustomerApiError | null;
  conflict: CustomerSessionConflict | null;
}) {
  return (
    <main className="customer-access-shell">
      <section
        className="customer-access-card customer-access-card--login"
        aria-labelledby="login-title"
      >
        <CustomerAccessBrand />
        <div className="customer-access-body">
          <h1 id="login-title">欢迎回来</h1>
          <p className="customer-access-lead">
            使用保存在本机的设备凭据直接登录工作台。
          </p>
          {conflict ? (
            <div className="login-conflict" role="status">
              <p>另一台设备正在线上：{conflict.deviceNameMasked}</p>
              <p>
                占用 {conflict.slotNo} 号设备槽
                {conflict.leaseExpiresAt
                  ? `，租约到 ${formatLeaseExpiry(conflict.leaseExpiresAt)}`
                  : ""}
                。如需在本机使用，请联系管理员或稍后重试。
              </p>
            </div>
          ) : null}
          {error && !conflict ? (
            <p className="form-error">{error.message}</p>
          ) : null}
          <button type="button" onClick={onRetryLogin} disabled={isBusy}>
            {isBusy ? "正在登录…" : "使用本机设备登录"}
          </button>
        </div>
        <footer className="customer-access-footer">
          当前设备凭据将安全保存在本机
        </footer>
      </section>
    </main>
  );
}

function formatLeaseExpiry(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toLocaleString();
}
