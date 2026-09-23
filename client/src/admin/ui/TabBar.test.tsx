import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TabBar } from "./TabBar";

describe("TabBar", () => {
  it("renders items, marks the active tab, and reports selection", () => {
    const onChange = vi.fn();
    render(
      <TabBar
        active="a"
        ariaLabel="测试页签"
        items={[
          { id: "a", label: "甲" },
          { id: "b", label: "乙" },
        ]}
        onChange={onChange}
      />,
    );

    expect(screen.getByRole("tab", { name: "甲" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "乙" })).toHaveAttribute(
      "aria-selected",
      "false",
    );

    fireEvent.click(screen.getByRole("tab", { name: "乙" }));
    expect(onChange).toHaveBeenCalledWith("b");
  });

  it("renders the optional actions slot", () => {
    render(
      <TabBar
        active="a"
        actions={<button type="button">动作按钮</button>}
        ariaLabel="测试页签"
        items={[{ id: "a", label: "甲" }]}
        onChange={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("button", { name: "动作按钮" }),
    ).toBeInTheDocument();
  });
});
