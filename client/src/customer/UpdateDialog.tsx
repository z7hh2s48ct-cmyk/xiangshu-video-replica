import { useEffect, useRef } from "react";
import type { AppUpdateView } from "./appUpdate";

/**
 * 「软件更新」弹窗（桌面客户端专用）。状态全部由 useAppUpdate 持有，
 * 这里只做展示：检查中 / 已是最新 / 发现新版本（含更新日志）/ 下载进度 /
 * 已安装待重启 / 失败重试。
 *
 * Esc 在下载中被拦下（见 onCancel）：下载半途关掉弹窗只会让用户误以为
 * 「已经更新过了」；安装完成后允许「稍后重启」。
 */
export function UpdateDialog({ update }: { update: AppUpdateView }) {
  const dialog = useRef<HTMLDialogElement | null>(null);
  const phase = update.phase;
  const downloading = phase === "downloading";

  useEffect(() => {
    const element = dialog.current;
    if (!element || element.open) {
      return;
    }
    if (typeof element.showModal === "function") {
      element.showModal();
    } else {
      element.setAttribute("open", "");
    }
  }, []);

  const percent = update.progress?.total
    ? Math.min(
        100,
        Math.floor((update.progress.received / update.progress.total) * 100),
      )
    : null;

  return (
    <dialog
      aria-label="软件更新"
      className="uc-dialog uc-update"
      onCancel={(event) => {
        event.preventDefault();
        if (!downloading) {
          update.close();
        }
      }}
      ref={dialog}
    >
      {phase === "checking" ? (
        <>
          <h2>正在检查更新…</h2>
          <p className="uc-update__hint">正在连接更新服务，请稍候。</p>
          <div className="uc-dialog-actions">
            <button type="button" onClick={update.close}>
              取消
            </button>
          </div>
        </>
      ) : phase === "up-to-date" ? (
        <>
          <h2>已是最新版本</h2>
          <p className="uc-update__hint">
            当前版本 V{update.currentVersion}，暂无可用更新。
          </p>
          <div className="uc-dialog-actions">
            <button className="uc-primary" type="button" onClick={update.close}>
              知道了
            </button>
          </div>
        </>
      ) : phase === "available" ? (
        <>
          <h2>发现新版本 V{update.update?.version}</h2>
          <p className="uc-update__hint">
            当前版本 V{update.currentVersion}，建议尽快升级。
          </p>
          {update.update?.notes ? (
            <div className="uc-update-notes">
              <h3>更新内容</h3>
              <p>{update.update.notes}</p>
            </div>
          ) : null}
          <div className="uc-dialog-actions">
            <button
              className="uc-primary"
              type="button"
              onClick={update.beginDownload}
            >
              立即更新
            </button>
            <button type="button" onClick={update.close}>
              稍后
            </button>
          </div>
        </>
      ) : phase === "downloading" ? (
        <>
          <h2>正在下载新版本 V{update.update?.version}</h2>
          <div className="uc-update__progress">
            {percent === null ? (
              <progress aria-label="下载进度" />
            ) : (
              <progress aria-label="下载进度" value={percent} max={100} />
            )}
            <span className="uc-update__percent">
              {percent === null ? "下载中…" : `${percent}%`}
            </span>
          </div>
          <p className="uc-update__hint">
            下载完成后会自动安装，期间请不要关闭客户端。
          </p>
        </>
      ) : phase === "installed" ? (
        <>
          <h2>新版本已就绪</h2>
          <p className="uc-update__hint">
            V{update.update?.version} 已安装完成，重启客户端后生效。
          </p>
          <div className="uc-dialog-actions">
            <button
              className="uc-primary"
              type="button"
              onClick={update.restart}
            >
              立即重启
            </button>
            <button type="button" onClick={update.close}>
              稍后重启
            </button>
          </div>
        </>
      ) : (
        <>
          <h2>更新失败</h2>
          <p className="uc-update__hint">
            {update.error || "更新出现问题，请稍后重试。"}
          </p>
          <div className="uc-dialog-actions">
            <button className="uc-primary" type="button" onClick={update.retry}>
              重试
            </button>
            <button type="button" onClick={update.close}>
              关闭
            </button>
          </div>
        </>
      )}
    </dialog>
  );
}
