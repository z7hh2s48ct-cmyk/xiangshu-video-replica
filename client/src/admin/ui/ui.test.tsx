import { act, fireEvent, render, screen } from "@testing-library/react";
import { Profiler, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConfirmDialog } from "./ConfirmDialog";
import { DataTable } from "./DataTable";
import { PageBanner } from "./PageBanner";
import { Pagination } from "./Pagination";
import {
  ActivationCodeStatusBadge,
  DeviceStatusBadge,
  OrderStatusBadge,
  PlatformBadge,
} from "./StatusBadge";

describe("PageBanner", () => {
  it("announces errors with role=alert and notices with role=status", () => {
    const { rerender } = render(<PageBanner tone="error">加载失败</PageBanner>);
    expect(screen.getByRole("alert")).toHaveTextContent("加载失败");

    rerender(<PageBanner tone="notice">已保存</PageBanner>);
    expect(screen.getByRole("status")).toHaveTextContent("已保存");
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("StatusBadge", () => {
  it("renders the unified vocabulary labels with a tone class", () => {
    render(
      <>
        <ActivationCodeStatusBadge status="REVOKED" />
        <ActivationCodeStatusBadge status="MYSTERY" />
        <DeviceStatusBadge status="REVOKED" />
        <OrderStatusBadge status="PAID" />
        <PlatformBadge platform="windows" />
      </>,
    );
    expect(screen.getByText("已撤销")).toHaveClass("status-badge--danger");
    expect(screen.getByText("MYSTERY")).toHaveClass("status-badge--neutral");
    expect(screen.getByText("永久禁用")).toBeInTheDocument();
    expect(screen.getByText("已支付")).toHaveClass("status-badge--good");
    expect(screen.getByText("Windows")).toBeInTheDocument();
  });
});

describe("Pagination", () => {
  it("shows the real total even when there is only one page", () => {
    render(
      <Pagination limit={50} offset={0} total={30} onPageChange={() => {}} />,
    );
    expect(screen.getByText("第 1 / 1 页（共 30 条）")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
  });

  it("shows unified page text and drives offset changes", () => {
    const onPageChange = vi.fn();
    const middlePage = render(
      <Pagination
        limit={20}
        offset={20}
        total={55}
        onPageChange={onPageChange}
      />,
    );
    expect(screen.getByText("第 2 / 3 页（共 55 条）")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "上一页" }));
    expect(onPageChange).toHaveBeenLastCalledWith(0);

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(onPageChange).toHaveBeenLastCalledWith(40);
    middlePage.unmount();

    // 末页的"下一页"要禁用。
    const lastPage = render(
      <Pagination
        limit={20}
        offset={40}
        total={55}
        onPageChange={onPageChange}
      />,
    );
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    lastPage.unmount();

    // 第一页的"上一页"也要禁用。
    render(
      <Pagination
        limit={20}
        offset={0}
        total={55}
        onPageChange={onPageChange}
      />,
    );
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
  });

  it("supports the customer noun", () => {
    render(
      <Pagination
        limit={20}
        offset={0}
        total={40}
        noun="位"
        onPageChange={() => {}}
      />,
    );
    expect(screen.getByText("第 1 / 2 页（共 40 位）")).toBeInTheDocument();
  });

  it("falls back to honest page-only text when the endpoint has no total", () => {
    const onPageChange = vi.fn();
    const firstPage = render(
      <Pagination hasMore limit={20} offset={0} onPageChange={onPageChange} />,
    );
    expect(screen.getByText("第 1 页")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一页" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(onPageChange).toHaveBeenLastCalledWith(20);
    firstPage.unmount();

    render(
      <Pagination
        hasMore={false}
        limit={20}
        offset={20}
        onPageChange={onPageChange}
      />,
    );
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
  });
});

describe("DataTable", () => {
  it("wraps rows in a scroll container with a labelled sticky header", () => {
    render(
      <DataTable
        ariaLabel="示例表"
        headers={
          <>
            <th>列一</th>
            <th>列二</th>
          </>
        }
      >
        <tr>
          <td>1</td>
          <td>2</td>
        </tr>
      </DataTable>,
    );
    expect(screen.getByRole("table", { name: "示例表" })).toBeInTheDocument();
    expect(screen.getByText("列一")).toBeInTheDocument();
  });
});

describe("ConfirmDialog", () => {
  it("打开即输入的原因不会被延后的初始化清空", async () => {
    const onConfirm = vi.fn();
    let entered = false;
    await act(async () => {
      render(
        <Profiler
          id="early-reason"
          onRender={() => {
            if (entered) return;
            entered = true;
            // 在提交周期尾部模拟已展示表单立即收到输入，钉住被动 effect 延迟时的丢字。
            const input = screen.getByLabelText("操作原因") as HTMLInputElement;
            const setValue = Object.getOwnPropertyDescriptor(
              HTMLInputElement.prototype,
              "value",
            )?.set;
            setValue?.call(input, "知晓预估后手动补货");
            input.dispatchEvent(new Event("input", { bubbles: true }));
          }}
        >
          <ConfirmDialog
            open
            title="立即采集"
            onConfirm={onConfirm}
            onClose={vi.fn()}
          />
        </Profiler>,
      );
    });
    expect(screen.getByLabelText("操作原因")).toHaveValue("知晓预估后手动补货");
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    expect(onConfirm).toHaveBeenCalledWith("知晓预估后手动补货");
  });

  function Harness({
    level,
    onConfirm,
  }: {
    level: "standard" | "reason" | "reasonAndAck";
    onConfirm: (reason: string) => void;
  }) {
    const [open, setOpen] = useState(true);
    return (
      <>
        <button type="button" onClick={() => setOpen(true)}>
          重新打开
        </button>
        <ConfirmDialog
          confirmLabel="确认执行"
          description="该操作不可逆。"
          level={level}
          open={open}
          title="确认危险操作"
          onClose={() => setOpen(false)}
          onConfirm={(reason) => {
            onConfirm(reason);
            setOpen(false);
          }}
        />
      </>
    );
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps Tab inside the dialog", () => {
    // `aria-modal="true"` 只是一句承诺：只加属性并不拦 Tab，键盘用户仍能一路
    // Tab 到对话框背后的页面控件上（2026-09-12 评审 P3 的 a11y 缺口）。这条钉住
    // 首尾回绕。Harness 里另有一个对话框外的「重新打开」按钮，用来确认回绕只在
    // 对话框内部进行。
    render(<Harness level="reason" onConfirm={vi.fn()} />);

    const reasonInput = screen.getByLabelText("操作原因");
    const cancel = screen.getByRole("button", { name: "取消" });
    expect(
      screen.getByRole("button", { name: "重新打开" }),
    ).toBeInTheDocument();

    // 最后一个可聚焦元素继续 Tab → 回绕到第一个。
    cancel.focus();
    fireEvent.keyDown(cancel, { key: "Tab" });
    expect(document.activeElement).toBe(reasonInput);

    // 第一个 Shift+Tab → 回绕到最后一个。
    reasonInput.focus();
    fireEvent.keyDown(reasonInput, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(cancel);
  });

  it("requires a reason before confirming", () => {
    const onConfirm = vi.fn();
    render(<Harness level="reason" onConfirm={onConfirm} />);

    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    expect(screen.getByRole("alert")).toHaveTextContent("请填写操作原因");
    expect(onConfirm).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客户投诉补发" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    expect(onConfirm).toHaveBeenCalledWith("客户投诉补发");
  });

  it("requires the acknowledgement checkbox at the high-risk level", () => {
    const onConfirm = vi.fn();
    render(<Harness level="reasonAndAck" onConfirm={onConfirm} />);

    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客服工单补发" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    expect(screen.getByRole("alert")).toHaveTextContent("请先勾选确认操作");

    fireEvent.click(screen.getByLabelText("我已知晓该操作的影响"));
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    expect(onConfirm).toHaveBeenCalledWith("客服工单补发");
  });

  it("focuses the reason input on open and closes on Escape", () => {
    const onConfirm = vi.fn();
    render(<Harness level="reason" onConfirm={onConfirm} />);

    expect(document.activeElement).toBe(screen.getByLabelText("操作原因"));

    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onConfirm).not.toHaveBeenCalled();

    // 关闭后可重新打开，且重新打开时输入被清空。
    fireEvent.click(screen.getByRole("button", { name: "重新打开" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByLabelText("操作原因")).toHaveValue("");
  });

  it("keeps the standard level to a bare confirmation and focuses the confirm button", () => {
    const onConfirm = vi.fn();
    render(<Harness level="standard" onConfirm={onConfirm} />);

    expect(screen.queryByLabelText("操作原因")).toBeNull();
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "确认执行" }),
    );

    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    expect(onConfirm).toHaveBeenCalledWith("");
  });
});
