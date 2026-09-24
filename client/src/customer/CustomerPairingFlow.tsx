import { useCallback, useEffect, useState } from "react";
import { customerEnrollDevice } from "../api";
import { DevicePairingPage } from "./DevicePairingPage";
import type { CustomerCredentialStore } from "./useCustomerSession";

/**
 * T30 / FE-03 — the second-device pairing entry at /customer/pairing.
 *
 * Wires the M4-shipped DevicePairingPage into the customer lane:
 * - a 202 enroll (another device is already bound) parks the user on a
 *   waiting screen with the pairing expiry; the client repeats the enroll
 *   with ONE stable idempotency key until an approved pairing returns 201 —
 *   the server exempts those status polls from the shared activation rate
 *   limit, a lost 201 replays the sealed credential under the same key,
 *   polling stops at the pairing expiry, and transport failures back off;
 * - a 201 enroll (primary device approved meanwhile) stores the device
 *   credential — the same shape the vault holds after activation, but
 *   without a session token — then hands back to /customer, where the
 *   state machine's boot path logs the new device in automatically.
 */
export function CustomerPairingFlow({
  store,
  onPaired,
  pollIntervalMs = 3000,
}: {
  store: CustomerCredentialStore;
  onPaired: () => void;
  pollIntervalMs?: number;
}) {
  const [stage, setStage] = useState<"form" | "pending" | "consumed">("form");
  const [pendingExpiry, setPendingExpiry] = useState("");
  const [pendingPairingId, setPendingPairingId] = useState("");
  // One stable idempotency key for the whole waiting stage: the approval
  // consumption seals the one-time credential under this key, so a poll
  // whose 201 response is lost replays the credential instead of losing
  // the freshly consumed device slot forever.
  const [pollKey, setPollKey] = useState("");
  const [error, setError] = useState("");
  const [deviceFingerprint, setDeviceFingerprint] = useState("");
  const [draft, setDraft] = useState({ activationCode: "", deviceName: "" });

  useEffect(() => {
    let active = true;
    void store
      .deviceInstanceId()
      .then((value) => {
        if (active) {
          setDeviceFingerprint(value);
        }
      })
      .catch(() => {
        if (active) {
          setError("无法读取本机机器标识，请重启应用后重试。");
        }
      });
    return () => {
      active = false;
    };
  }, [store]);

  const handleEnrollSuccess = useCallback(
    async (
      result: {
        status: "pending" | "consumed";
        data: unknown;
      },
      input: { activationCode: string; deviceName: string },
    ) => {
      setDraft(input);
      setError("");
      if (result.status === "pending") {
        const pending = result.data as {
          pairing_request_id: string;
          expires_at: string;
        };
        setPendingExpiry(pending.expires_at);
        setPendingPairingId(pending.pairing_request_id);
        // Minted once per waiting stage (a re-submitted form resets it via
        // the form branch below); repeated 202s keep the current key.
        setPollKey((current) => current || crypto.randomUUID());
        setStage("pending");
        return;
      }
      const consumed = result.data as { device_token: string };
      try {
        // The consumed branch carries only the device credential; the session
        // token arrives on the first login, so the vault is primed without one.
        await store.saveActivation(consumed.device_token, "");
        setStage("consumed");
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "保存设备凭据失败");
        setStage("form");
      }
    },
    [store],
  );

  useEffect(() => {
    if (
      stage !== "pending" ||
      !pollKey ||
      !draft.activationCode ||
      !draft.deviceName ||
      !deviceFingerprint
    ) {
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    let consecutiveFailures = 0;
    const expiryMs = pendingExpiry ? Date.parse(pendingExpiry) : Number.NaN;

    const stop = () => {
      cancelled = true;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
    const schedule = (delay: number) => {
      timer = window.setTimeout(() => void check(), delay);
    };
    const check = async () => {
      if (cancelled) {
        return;
      }
      // The pairing window is over: stop instead of polling on — the next
      // enroll would lazily mint a NEW pending request, which only the
      // user should decide to submit.
      if (!Number.isNaN(expiryMs) && Date.now() >= expiryMs) {
        stop();
        setError("配对请求已过期，请返回重新提交。");
        return;
      }
      try {
        const result = await customerEnrollDevice({
          activationCode: draft.activationCode,
          deviceFingerprint,
          deviceName: draft.deviceName,
          devicePlatform: store.devicePlatform(),
          // The waiting-stage key: stable across polls so a lost 201 can
          // replay the sealed one-time credential on the next poll.
          idempotencyKey: pollKey,
        });
        if (cancelled) {
          return;
        }
        consecutiveFailures = 0;
        if (result.status === 201) {
          stop();
        }
        await handleEnrollSuccess(
          result.status === 202
            ? { status: "pending", data: result.pending }
            : { status: "consumed", data: result.credential },
          draft,
        );
        if (!cancelled && result.status === 202) {
          schedule(pollIntervalMs);
        }
      } catch (cause) {
        if (cancelled) {
          return;
        }
        consecutiveFailures += 1;
        setError(cause instanceof Error ? cause.message : "检查配对状态失败");
        // Transport failures (backend restarting, network drop) back off
        // instead of hammering the endpoint, capped so an approved
        // pairing is still picked up promptly once the backend recovers.
        const backoff = pollIntervalMs * 2 ** Math.min(consecutiveFailures, 4);
        schedule(Math.min(backoff, 30_000));
      }
    };
    schedule(pollIntervalMs);
    return stop;
  }, [
    deviceFingerprint,
    draft,
    handleEnrollSuccess,
    pendingExpiry,
    pollIntervalMs,
    pollKey,
    stage,
    store,
  ]);

  if (stage === "pending") {
    return (
      <main
        className="device-pairing-page"
        aria-labelledby="pairing-waiting-title"
      >
        <header>
          <h1 id="pairing-waiting-title">等待主设备审批</h1>
          <p className="page-subtitle">
            配对请求已提交，请在已登录的主设备个人中心确认；也可以联系管理员审批。
          </p>
        </header>
        <section className="pairing-pending" aria-live="polite">
          {pendingExpiry ? (
            <p className="request-time">
              本请求将在 {new Date(pendingExpiry).toLocaleString()} 过期。
            </p>
          ) : null}
          {pendingPairingId ? (
            <p className="request-time">配对编号：{pendingPairingId}</p>
          ) : null}
          <p className="pending-status">等待主设备或管理员确认</p>
          <p>系统会自动检查审批结果，批准后将直接完成设备绑定。</p>
          {error ? <p role="alert">{error}</p> : null}
          <div className="form-actions">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => {
                // A re-submitted form starts a fresh pairing attempt with a
                // fresh waiting-stage key.
                setPollKey("");
                setStage("form");
              }}
            >
              返回修改
            </button>
          </div>
        </section>
      </main>
    );
  }

  if (stage === "consumed") {
    return (
      <main
        className="device-pairing-page"
        aria-labelledby="pairing-success-title"
      >
        <header>
          <h1 id="pairing-success-title">配对成功</h1>
          <p className="page-subtitle">本设备已加入您的客户账号。</p>
        </header>
        <section className="pairing-pending" aria-live="polite">
          <button type="button" className="btn-primary" onClick={onPaired}>
            进入客户工作区
          </button>
        </section>
      </main>
    );
  }

  return (
    <>
      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}
      <DevicePairingPage
        deviceFingerprint={deviceFingerprint}
        devicePlatform={store.devicePlatform()}
        initialActivationCode={draft.activationCode}
        initialDeviceName={draft.deviceName}
        onSuccess={(result, input) => void handleEnrollSuccess(result, input)}
        onError={(cause) => setError(cause.message)}
        onCancel={onPaired}
      />
    </>
  );
}
