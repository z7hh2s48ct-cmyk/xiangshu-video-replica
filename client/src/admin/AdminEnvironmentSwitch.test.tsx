import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  AdminEnvironmentSwitch,
  adminEnvironmentUrl,
  resolveAdminEnvironment,
} from "./AdminEnvironmentSwitch";

const origins = {
  local: "http://localhost:5200",
  cloud: "https://video.zszhj.cn",
} as const;

describe("AdminEnvironmentSwitch", () => {
  it("identifies the local and cloud management origins", () => {
    expect(resolveAdminEnvironment("http://127.0.0.1:5200", origins)).toBe(
      "local",
    );
    expect(resolveAdminEnvironment("https://video.zszhj.cn", origins)).toBe(
      "cloud",
    );
    expect(resolveAdminEnvironment("https://preview.zszhj.cn", origins)).toBe(
      "unknown",
    );
  });

  it("builds the system-settings URL for each independent environment", () => {
    expect(adminEnvironmentUrl("local", origins)).toBe(
      "http://localhost:5200/admin/#admin/systemSettings",
    );
    expect(adminEnvironmentUrl("cloud", origins)).toBe(
      "https://video.zszhj.cn/admin/#admin/systemSettings",
    );
  });

  it("confirms before navigating from local to the cloud admin", () => {
    const navigate = vi.fn();
    render(
      <AdminEnvironmentSwitch
        currentOrigin="http://localhost:5200"
        navigate={navigate}
      />,
    );

    expect(
      screen.getByText("本地环境", { selector: ".status-badge" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /本地环境/ })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /云端正式环境/ }));
    expect(
      screen.getByRole("dialog", { name: "切换到云端正式环境" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "进入云端环境" }));
    expect(navigate).toHaveBeenCalledWith(
      "https://video.zszhj.cn/admin/#admin/systemSettings",
    );
  });

  it("keeps the environment selector read-only for auditors", () => {
    render(
      <AdminEnvironmentSwitch
        currentOrigin="https://video.zszhj.cn"
        readOnly
      />,
    );

    expect(
      screen.getByText("云端正式环境", { selector: ".status-badge" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /本地环境/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /云端正式环境/ })).toBeDisabled();
    expect(screen.getByText(/审计员只读/)).toBeInTheDocument();
  });
});
