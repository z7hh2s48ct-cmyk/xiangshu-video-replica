import { invoke } from "@tauri-apps/api/core";
import { check, type Update } from "@tauri-apps/plugin-updater";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { UpdateDialog } from "./UpdateDialog";
import { isTauriRuntime } from "./useCustomerSession";

/**
 * 桌面端自动更新（仅客户 lane；浏览器版没有自动更新能力）。
 *
 * 状态机：idle → checking → available（弹窗，用户确认）→ downloading →
 * installed（提示重启）→ 重启。检查/下载/安装全部走 Tauri 官方 updater
 * 插件：清单里的安装包带 minisign 签名，插件下载后会先验签再安装，前端
 * 不自己拼升级逻辑。
 *
 * 自动检查每天最多一次、延迟启动避开登录期的接口高峰，且**静默**——只有
 * 发现新版本才弹窗，已是最新或检查失败都不打扰；手动入口（用户中心）则
 * 全程有界面反馈。
 */

export type AppUpdatePhase =
  | "idle"
  | "checking"
  | "up-to-date"
  | "available"
  | "downloading"
  | "installed"
  | "failed";

export interface AvailableAppUpdate {
  version: string;
  notes: string;
}

export interface AppUpdateController {
  supported: boolean;
  phase: AppUpdatePhase;
  currentVersion: string;
  update: AvailableAppUpdate | null;
  progress: { received: number; total: number | null } | null;
  error: string;
  dialog: ReactNode;
  openManualCheck: () => void;
  beginDownload: () => void;
  restart: () => void;
  retry: () => void;
  close: () => void;
}

/** UpdateDialog 只消费状态与动作，不消费 dialog 自身（避免自引用）。 */
export type AppUpdateView = Omit<AppUpdateController, "dialog">;

const AUTO_CHECK_DAY_KEY = "uc:update-auto-check-day";
const AUTO_CHECK_DELAY_MS = 8000;

function localDayStamp(): string {
  const now = new Date();
  const month = `${now.getMonth() + 1}`.padStart(2, "0");
  const day = `${now.getDate()}`.padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

function autoCheckAlreadyRanToday(): boolean {
  try {
    return window.localStorage.getItem(AUTO_CHECK_DAY_KEY) === localDayStamp();
  } catch {
    // 读不到（隐私模式等）就当作今天已经检查过：少打扰比多打扰好。
    return true;
  }
}

function markAutoCheckDone(): void {
  try {
    window.localStorage.setItem(AUTO_CHECK_DAY_KEY, localDayStamp());
  } catch {
    // 记不住就明天再说，不影响其它功能。
  }
}

export function useAppUpdate({
  autoCheck = false,
}: {
  autoCheck?: boolean;
} = {}): AppUpdateController {
  const supported = isTauriRuntime();
  const [phase, setPhase] = useState<AppUpdatePhase>("idle");
  const [update, setUpdate] = useState<AvailableAppUpdate | null>(null);
  const [progress, setProgress] = useState<{
    received: number;
    total: number | null;
  } | null>(null);
  const [error, setError] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const pendingRef = useRef<Update | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const runCheck = useCallback(async (): Promise<
    "available" | "up-to-date" | "failed"
  > => {
    setPhase("checking");
    setUpdate(null);
    setError("");
    pendingRef.current = null;
    try {
      const found = await check();
      if (!mountedRef.current) {
        return "failed";
      }
      markAutoCheckDone();
      if (!found) {
        setPhase("up-to-date");
        return "up-to-date";
      }
      pendingRef.current = found;
      setUpdate({
        version: found.version,
        notes: (found.body ?? "").trim(),
      });
      setPhase("available");
      return "available";
    } catch {
      if (mountedRef.current) {
        setPhase("failed");
        setError("检查更新失败，请稍后重试。");
      }
      return "failed";
    }
  }, []);

  useEffect(() => {
    if (!autoCheck || !supported || autoCheckAlreadyRanToday()) {
      return;
    }
    const timer = window.setTimeout(() => {
      void runCheck().then((outcome) => {
        // 静默检查只有「发现新版本」才弹窗；已是最新/失败都不出声。
        if (mountedRef.current && outcome === "available") {
          setDialogOpen(true);
        }
      });
    }, AUTO_CHECK_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [autoCheck, supported, runCheck]);

  const openManualCheck = useCallback(() => {
    if (!supported) {
      return;
    }
    setDialogOpen(true);
    void runCheck();
  }, [supported, runCheck]);

  const beginDownload = useCallback(() => {
    const pending = pendingRef.current;
    if (!pending) {
      setPhase("failed");
      setError("更新信息已失效，请重新检查更新。");
      return;
    }
    setPhase("downloading");
    setProgress({ received: 0, total: null });
    setError("");
    let receivedBytes = 0;
    void pending
      .downloadAndInstall((event) => {
        if (!mountedRef.current) {
          return;
        }
        switch (event.event) {
          case "Started":
            setProgress({
              received: 0,
              total: event.data.contentLength ?? null,
            });
            break;
          case "Progress":
            receivedBytes += event.data.chunkLength;
            setProgress((current) => ({
              received: receivedBytes,
              total: current?.total ?? null,
            }));
            break;
          default:
            break;
        }
      })
      .then(() => {
        // 走到这里安装包已验签并安装完成，重启后即运行新版本。
        if (mountedRef.current) {
          setPhase("installed");
        }
      })
      .catch(() => {
        if (mountedRef.current) {
          setPhase("failed");
          setError("下载或安装更新失败，请检查网络后重试。");
        }
      });
  }, []);

  const restart = useCallback(() => {
    void invoke("restart_app").catch(() => {
      if (mountedRef.current) {
        setPhase("failed");
        setError("自动重启失败，请手动退出客户端后重新打开完成升级。");
      }
    });
  }, []);

  const retry = useCallback(() => {
    setDialogOpen(true);
    void runCheck();
  }, [runCheck]);

  const close = useCallback(() => {
    // 下载中不允许关：半途关掉最容易留下「我以为已经更新了」的错觉。
    if (phase === "downloading") {
      return;
    }
    setDialogOpen(false);
    setPhase("idle");
  }, [phase]);

  const controller: Omit<AppUpdateController, "dialog"> = {
    supported,
    phase,
    currentVersion: __APP_VERSION__,
    update,
    progress,
    error,
    openManualCheck,
    beginDownload,
    restart,
    retry,
    close,
  };

  return {
    ...controller,
    dialog:
      dialogOpen && supported ? <UpdateDialog update={controller} /> : null,
  };
}
