import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * 客户壳层兜底边界（上线审查 P1-1）。
 *
 * 客户制品此前只在管理端有 ErrorBoundary：懒加载的 CustomerWorkspace 在生产
 * 部署替换 chunk 后动态 import 失败（经典的 "Failed to fetch dynamically
 * imported module"），或任何未预见渲染异常，都会一路抛到 React root 变成
 * 无法自愈的白屏。放在 src/ 顶层而非 admin/ 下，是因为 CW-019 入口合同禁止
 * 客户制品引用 admin/ 模块；两个制品各用各的边界实现，不共享引用。
 *
 * 兜底动作只给「刷新页面」：chunk 加载失败只能靠重新拉取部署产物解决，
 * 就地重渲染只会复现同一异常，假装可重试反而误导用户。
 */
export class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error): { error: Error | null } {
    return { error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error("客户界面渲染异常：", error, errorInfo.componentStack);
  }

  render(): ReactNode {
    if (this.state.error !== null) {
      return (
        <main className="centered-shell">
          <section className="login-card" role="alert">
            <span className="eyebrow">众墅之家 · AI 即创</span>
            <h1>页面出现了问题</h1>
            <p className="login-hint">
              界面加载遇到异常，请刷新页面重试；若反复出现，请退出后重新登录。
            </p>
            <button type="button" onClick={() => window.location.reload()}>
              刷新页面
            </button>
          </section>
        </main>
      );
    }
    return this.props.children;
  }
}
