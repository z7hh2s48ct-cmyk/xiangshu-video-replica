import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CreationNavigation } from "./CreationNavigation";

const navigate = vi.fn();
const context = { state: { page: "replacement" }, navigate };
vi.mock("./context", () => ({ useStudio: () => context }));

describe("统一创作导航", () => {
  it("人物置换归属视频复刻，AI 视频模式共用一个一级入口", () => {
    const { rerender } = render(<CreationNavigation />);
    expect(screen.getAllByRole("tab")).toHaveLength(3);
    expect(screen.getByRole("tab", { name: "视频复刻" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      screen.queryByRole("tab", { name: "人物置换" }),
    ).not.toBeInTheDocument();
    context.state.page = "reference";
    rerender(<CreationNavigation />);
    expect(screen.getByRole("tab", { name: "AI 视频" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    fireEvent.click(screen.getByRole("tab", { name: "视频复刻" }));
    expect(navigate).toHaveBeenCalledWith("replica");
  });
});
