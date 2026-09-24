import {
  activationCodeStatusLabel,
  customerStatusLabel,
  deviceStatusLabel,
  labelFrom,
  PLATFORM_LABELS,
  rechargeOrderStatusLabel,
} from "./vocabulary";

export type BadgeTone = "neutral" | "good" | "warn" | "danger" | "info";

// 语义色只表达"要不要管"：good=正常运行、warn=需要留意、danger=已终态/
// 需要干预、info=尚未流转、neutral=其他。
const TONE_CLASS: Record<BadgeTone, string> = {
  neutral: "status-badge--neutral",
  good: "status-badge--good",
  warn: "status-badge--warn",
  danger: "status-badge--danger",
  info: "status-badge--info",
};

const ACTIVATION_CODE_TONES: Record<string, BadgeTone> = {
  GENERATED: "info",
  ISSUED: "neutral",
  ACTIVE: "good",
  SUSPENDED: "warn",
  REVOKED: "danger",
  EXPIRED: "neutral",
};

const DEVICE_TONES: Record<string, BadgeTone> = {
  ONLINE: "good",
  OFFLINE: "neutral",
  BOUND: "good",
  UNBOUND: "neutral",
  REVOKED: "danger",
};

const CUSTOMER_TONES: Record<string, BadgeTone> = {
  ACTIVE: "good",
  SUSPENDED: "warn",
  REVOKED: "danger",
};

const ORDER_TONES: Record<string, BadgeTone> = {
  PENDING: "warn",
  PAID: "good",
  FAILED: "danger",
  CLOSED: "neutral",
};

export function StatusBadge({
  tone,
  children,
}: {
  tone: BadgeTone;
  children: React.ReactNode;
}) {
  return <span className={`status-badge ${TONE_CLASS[tone]}`}>{children}</span>;
}

export function ActivationCodeStatusBadge({ status }: { status: string }) {
  return (
    <StatusBadge tone={ACTIVATION_CODE_TONES[status] ?? "neutral"}>
      {activationCodeStatusLabel(status)}
    </StatusBadge>
  );
}

export function DeviceStatusBadge({ status }: { status: string }) {
  return (
    <StatusBadge tone={DEVICE_TONES[status] ?? "neutral"}>
      {deviceStatusLabel(status)}
    </StatusBadge>
  );
}

export function CustomerStatusBadge({ status }: { status: string }) {
  return (
    <StatusBadge tone={CUSTOMER_TONES[status.toUpperCase()] ?? "neutral"}>
      {customerStatusLabel(status)}
    </StatusBadge>
  );
}

export function OrderStatusBadge({ status }: { status: string }) {
  return (
    <StatusBadge tone={ORDER_TONES[status] ?? "neutral"}>
      {rechargeOrderStatusLabel(status)}
    </StatusBadge>
  );
}

export function PlatformBadge({ platform }: { platform: string }) {
  return (
    <span className="platform-badge">
      {labelFrom(PLATFORM_LABELS, platform)}
    </span>
  );
}
