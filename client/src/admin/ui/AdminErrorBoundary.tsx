/**
 * Error Boundary wrapper for admin components
 * Catches JavaScript errors anywhere in child component tree and displays fallback UI
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
  fallback?: ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
}

export class AdminErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = {
      hasError: false,
      error: null,
    };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return {
      hasError: true,
      error,
    };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error("AdminErrorBoundary caught an error:", error, errorInfo);

    // Log to monitoring service (optional)
    // Example: Sentry.captureException(error, { extra: errorInfo });
  }

  render(): ReactNode {
    if (this.state.hasError) {
      return (
        this.props.fallback || (
          <div className="admin-error-boundary">
            <div className="error-content">
              <h2>⚠️ 页面出现错误</h2>
              <p className="error-message">
                {this.state.error?.message || "未知错误"}
              </p>
              <button
                type="button"
                onClick={() => window.location.reload()}
                className="retry-button"
              >
                刷新页面
              </button>
            </div>
          </div>
        )
      );
    }

    return this.props.children;
  }
}
