import { useMemo, useState } from "react";

import { ConfirmDialog } from "./ui/ConfirmDialog";
import { StatusBadge } from "./ui/StatusBadge";

export type AdminEnvironment = "local" | "cloud";

const DEFAULT_LOCAL_ADMIN_ORIGIN = "http://localhost:5200";
const DEFAULT_CLOUD_ADMIN_ORIGIN = "https://video.zszhj.cn";

function normalizedOrigin(value: string): string {
  return new URL(value).origin;
}

export function adminEnvironmentOrigins(): Record<AdminEnvironment, string> {
  return {
    local: normalizedOrigin(
      import.meta.env.VITE_LOCAL_ADMIN_ORIGIN || DEFAULT_LOCAL_ADMIN_ORIGIN,
    ),
    cloud: normalizedOrigin(
      import.meta.env.VITE_CLOUD_ADMIN_ORIGIN || DEFAULT_CLOUD_ADMIN_ORIGIN,
    ),
  };
}

export function resolveAdminEnvironment(
  currentOrigin: string,
  origins: Record<AdminEnvironment, string>,
): AdminEnvironment | "unknown" {
  const normalized = normalizedOrigin(currentOrigin);
  const hostname = new URL(normalized).hostname;
  if (normalized === origins.cloud) {
    return "cloud";
  }
  if (
    normalized === origins.local ||
    hostname === "localhost" ||
    hostname === "127.0.0.1"
  ) {
    return "local";
  }
  return "unknown";
}

export function adminEnvironmentUrl(
  environment: AdminEnvironment,
  origins: Record<AdminEnvironment, string>,
): string {
  return `${origins[environment]}/admin/#admin/systemSettings`;
}

/**
 * 本地与云端是两个独立站点，管理会话 Cookie 不能跨 origin 共享。因此这里执行
 * 整页跳转，而不是跨域调用另一套 API。服务器生产安全门仍由部署环境变量控制。
 */
export function AdminEnvironmentSwitch({
  readOnly = false,
  currentOrigin = window.location.origin,
  navigate = (url: string) => window.location.assign(url),
}: {
  readOnly?: boolean;
  currentOrigin?: string;
  navigate?: (url: string) => void;
}) {
  const origins = useMemo(() => adminEnvironmentOrigins(), []);
  const current = resolveAdminEnvironment(currentOrigin, origins);
  const [target, setTarget] = useState<AdminEnvironment | null>(null);

  function requestSwitch(environment: AdminEnvironment) {
    if (readOnly || environment === current) {
      return;
    }
    setTarget(environment);
  }

  return (
    <section
      aria-label="管理环境切换"
      className="admin-panel admin-environment-switch"
    >
      <div className="admin-environment-switch__heading">
        <div>
          <h2>管理环境</h2>
          <p className="admin-hint">
            本地与云端使用独立数据库、Cookie
            和任务队列；切换会打开对应管理站，目标环境需要单独登录。
          </p>
        </div>
        <StatusBadge tone={current === "cloud" ? "good" : "info"}>
          {current === "cloud"
            ? "云端正式环境"
            : current === "local"
              ? "本地环境"
              : "自定义环境"}
        </StatusBadge>
      </div>

      <div className="admin-environment-switch__options">
        <button
          aria-pressed={current === "local"}
          className={current === "local" ? "is-active" : "secondary-button"}
          disabled={readOnly || current === "local"}
          type="button"
          onClick={() => requestSwitch("local")}
        >
          本地环境
          <small>{origins.local}</small>
        </button>
        <button
          aria-pressed={current === "cloud"}
          className={current === "cloud" ? "is-active" : "secondary-button"}
          disabled={readOnly || current === "cloud"}
          type="button"
          onClick={() => requestSwitch("cloud")}
        >
          云端正式环境
          <small>{origins.cloud}</small>
        </button>
      </div>

      {readOnly ? (
        <p className="admin-hint">审计员只读，不能切换管理环境。</p>
      ) : null}

      <ConfirmDialog
        confirmLabel={target === "cloud" ? "进入云端环境" : "进入本地环境"}
        description={
          target === "cloud"
            ? `将跳转到 ${origins.cloud}/admin/。云端会话与本地会话独立。`
            : `将跳转到 ${origins.local}/admin/。请先确认本地管理服务已启动。`
        }
        level="standard"
        open={target !== null}
        title={target === "cloud" ? "切换到云端正式环境" : "切换到本地环境"}
        onClose={() => setTarget(null)}
        onConfirm={() => {
          if (target !== null) {
            navigate(adminEnvironmentUrl(target, origins));
          }
        }}
      />
    </section>
  );
}
