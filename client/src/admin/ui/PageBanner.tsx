// 统一的页面级提示横幅：错误必须用 role="alert"（读屏即时播报），
// 通知/成功用 role="status"。样式复用后台既有的 settings-error /
// wallet-notice 两套 class，避免第三套视觉。

export function PageBanner({
  tone,
  children,
}: {
  tone: "error" | "notice";
  children: React.ReactNode;
}) {
  if (tone === "error") {
    return (
      <p className="settings-error" role="alert">
        {children}
      </p>
    );
  }
  return (
    <p className="wallet-notice" role="status">
      {children}
    </p>
  );
}
