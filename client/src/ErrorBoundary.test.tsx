import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "./ErrorBoundary";

/** 直播渲染异常的探针：只在首次挂载抛错，模拟懒加载 chunk 失效或渲染炸弹。 */
function Bomb({ shouldThrow }: { shouldThrow: boolean }) {
  if (shouldThrow) {
    throw new Error("模拟渲染异常");
  }
  return <p>正常内容</p>;
}

describe("ErrorBoundary", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("子树正常时不拦截渲染", () => {
    render(
      <ErrorBoundary>
        <Bomb shouldThrow={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("正常内容")).toBeInTheDocument();
  });

  it("渲染异常落到可操作的错误页而不是白屏", () => {
    // React 会把被边界接住的错误照常打进 console.error，测试里静音。
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Bomb shouldThrow={true} />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("页面出现了问题");
    expect(
      screen.getByRole("button", { name: "刷新页面" }),
    ).toBeInTheDocument();
  });
});
