// Task T29 (FE-02) — the customer-side screen state machine.
//
// Dev doc §4.1 defines the page states for the customer build and §4.2 the
// state flow. The reducer below is the single place that decides which screen
// the customer workspace shows; pages and hooks dispatch events, never raw
// `setState` screen jumps, so every transition stays auditable against the
// spec tables (§13.2 defines the client behaviour per business error code).
//
// §4.2 red line: a displaced session must surface as its own outcome
// (`session-replaced`) — never misreported as a balance, network, or generic
// service failure. Keeping the three terminal outcomes as distinct screens is
// what lets the UI show the sentence matching what actually happened.

export type CustomerScreen =
  | "checking"
  | "login"
  | "binding-conflict"
  | "workspace"
  | "session-expired"
  | "session-replaced"
  | "device-revoked";

export const initialCustomerScreen: CustomerScreen = "checking";

export type CustomerScreenEvent =
  // Boot: the credential store answered whether a device credential exists.
  // 新激活方案（2026-09-18）后两个分支都落到登录屏：获客入口是自助注册，
  // 注册与登录同在 AccountAccessPage，激活码不再是入口（任务书 B1）。
  | { type: "boot-check-completed"; hasDeviceCredential: boolean }
  | { type: "password-login-succeeded" }
  // A login with the stored device credential established a session.
  | { type: "login-succeeded" }
  // 401 OTHER_DEVICE_ONLINE during login: another device holds the live lease.
  // The explicit takeover (switch) is a user decision — nothing switches
  // silently (T30 / FE-03, dev doc §13.2).
  | { type: "conflict-detected" }
  // The user declined the takeover in the conflict dialog: back to login,
  // the other device keeps the lease.
  | { type: "conflict-cancelled" }
  // The user signed out (or the workspace session was closed deliberately);
  // the device credential survives, so the next stop is the login screen.
  | { type: "logout" }
  // 401 SESSION_EXPIRED / the CUSTOMER_SESSION_EXPIRED_EVENT: the session
  // token is gone but the device credential stays valid.
  | { type: "session-expired" }
  // 401 SESSION_REPLACED / the CUSTOMER_SESSION_REPLACED_EVENT: another
  // device displaced this one.
  | { type: "session-replaced" }
  // 401 DEVICE_REVOKED / the CUSTOMER_SESSION_REVOKED_EVENT: the whole device
  // credential is dead and only the recovery flow (re-activation or an admin
  // approved rebind) can bring the user back.
  | { type: "device-revoked" }
  // The three terminal screens（session-expired / session-replaced /
  // device-revoked）offer a "log in again" path back to the login screen.
  // 设备凭据报废后的恢复手段同样是重新登录——激活码通道已退役（任务书 B1），
  // 故本事件吸收了原先的 restart-activation。
  | { type: "restart-login" }
  // A retry login failed to find a stored device credential (vault cleared / I/O failure):
  // the user recovers by logging in again. This event is guarded to only fire
  // from the login screen (§4.1 state machine);
  | { type: "credential-missing" };

export function customerScreenReducer(
  screen: CustomerScreen,
  event: CustomerScreenEvent,
): CustomerScreen {
  switch (event.type) {
    case "boot-check-completed":
      if (screen !== "checking") {
        return screen;
      }
      // B1：有无线索都落到登录屏。获客入口已改为自助注册，注册与登录同在
      // AccountAccessPage，不再有独立的激活屏；hasDeviceCredential 仅用于
      // 调用方决定是否预填「记住密码」。
      return "login";

    case "password-login-succeeded":
      return screen === "login" || screen === "binding-conflict"
        ? "workspace"
        : screen;

    case "login-succeeded":
      // A login ends on the workspace; the explicit switch ends there too —
      // the server confirmed the takeover and minted a fresh session token.
      return screen === "login" || screen === "binding-conflict"
        ? "workspace"
        : screen;

    case "conflict-detected":
      return screen === "login" ? "binding-conflict" : screen;

    case "conflict-cancelled":
      return screen === "binding-conflict" ? "login" : screen;

    case "logout":
      return screen === "workspace" ? "login" : screen;

    case "session-expired":
      // Only a live workspace session can expire — the terminal screens are
      // already past it and the login/activation screens never held one.
      return screen === "workspace" ? "session-expired" : screen;

    case "session-replaced":
      return screen === "workspace" ? "session-replaced" : screen;

    case "device-revoked":
      // A revocation kills the device credential itself, so it applies from
      // any screen that assumed the credential was valid.
      return "device-revoked";

    case "restart-login":
      return screen === "session-expired" ||
        screen === "session-replaced" ||
        screen === "device-revoked"
        ? "login"
        : screen;

    case "credential-missing":
      // The stored device credential vanished in the middle of a retry login
      // or an explicit switch (FE-02 / P3 review): back to the login screen
      // for recovery（原先是回到激活屏，B1 后激活通道退役）。
      return screen === "login" || screen === "binding-conflict"
        ? "login"
        : screen;
  }
}
