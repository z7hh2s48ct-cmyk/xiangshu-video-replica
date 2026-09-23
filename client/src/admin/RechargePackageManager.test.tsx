import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import type { CustomerRechargePackage } from "../api";
import {
  AdminControlError,
  createRechargePackage,
  listRechargePackages,
  updateRechargePackage,
} from "../api.admin";
import { RechargePackageManager } from "./RechargePackageManager";

// 组件用 AdminControlError 区分确定性 4xx 与可重试的 5xx，mock 工厂必须
// 提供同一个类，否则错误分支的 instanceof 会抛 TypeError。
vi.mock("../api.admin", () => {
  class AdminControlError extends Error {
    readonly status: number | undefined;
    readonly code: string | undefined;

    constructor(message: string, status?: number, code?: string) {
      super(message);
      this.name = "AdminControlError";
      this.status = status;
      this.code = code;
    }
  }
  return {
    AdminControlError,
    createRechargePackage: vi.fn(),
    listRechargePackages: vi.fn(),
    updateRechargePackage: vi.fn(),
  };
});

const standardPackage: CustomerRechargePackage = {
  id: "pkg-standard",
  name: "标准档",
  amount_fen: 20000,
  credits: 21000,
  discount_rate: "0.9000",
  discount_interfaces: ["video_generation"],
  sort_order: 1,
  is_active: true,
  version: 3,
  created_at: null,
  updated_at: null,
};

const basicPackage: CustomerRechargePackage = {
  id: "pkg-basic",
  name: "基础档",
  amount_fen: 5000,
  credits: 5000,
  discount_rate: null,
  discount_interfaces: [],
  sort_order: 2,
  is_active: false,
  version: 1,
  created_at: null,
  updated_at: null,
};

beforeEach(() => {
  vi.mocked(createRechargePackage).mockReset();
  vi.mocked(listRechargePackages).mockReset();
  vi.mocked(updateRechargePackage).mockReset();
});

test("renders packages with benefits, ordering and status", async () => {
  vi.mocked(listRechargePackages).mockResolvedValue([
    standardPackage,
    basicPackage,
  ]);
  render(<RechargePackageManager />);

  expect(await screen.findByText("标准档")).toBeInTheDocument();
  expect(screen.getByText("200 元")).toBeInTheDocument();
  expect(screen.getByText("21000")).toBeInTheDocument();
  expect(screen.getByText("视频生成 9折")).toBeInTheDocument();
  expect(screen.getByText("基础档")).toBeInTheDocument();
  expect(screen.getByText("50 元")).toBeInTheDocument();
  // 无折扣档位的权益列是占位符，不是空白。
  expect(screen.getByText("—")).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "编辑" })).toHaveLength(2);
  expect(screen.getByRole("button", { name: "停用" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "启用" })).toBeInTheDocument();
});

test("creates a package through the reason-confirm dialog", async () => {
  vi.mocked(listRechargePackages).mockResolvedValue([]);
  vi.mocked(createRechargePackage).mockResolvedValue({
    ...standardPackage,
    id: "pkg-premium",
    name: "尊享档",
  });
  render(<RechargePackageManager />);
  expect(await screen.findByText("暂无充值套餐")).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("套餐名称"), {
    target: { value: "尊享档" },
  });
  fireEvent.change(screen.getByLabelText("充值金额（元）"), {
    target: { value: "1998" },
  });
  fireEvent.change(screen.getByLabelText("到账积分"), {
    target: { value: "2000" },
  });
  fireEvent.change(screen.getByLabelText("折扣（折，留空表示无折扣）"), {
    target: { value: "9" },
  });
  fireEvent.click(screen.getByRole("checkbox", { name: "视频生成" }));
  fireEvent.change(screen.getByLabelText("排序（小在前）"), {
    target: { value: "5" },
  });
  fireEvent.click(screen.getByRole("button", { name: "新建套餐" }));

  expect(
    await screen.findByRole("dialog", { name: "新建充值套餐" }),
  ).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "上线尊享档" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认新建" }));

  await waitFor(() =>
    expect(createRechargePackage).toHaveBeenCalledWith(
      {
        name: "尊享档",
        amount_fen: 199800,
        credits: 2000,
        discount_rate: "0.9000",
        discount_interfaces: ["video_generation"],
        sort_order: 5,
        is_active: true,
      },
      "上线尊享档",
      expect.any(String),
    ),
  );
  expect(await screen.findByText(/套餐「尊享档」已创建/)).toBeInTheDocument();
});

test("updates a package with the expected version and reason", async () => {
  vi.mocked(listRechargePackages).mockResolvedValue([standardPackage]);
  vi.mocked(updateRechargePackage).mockResolvedValue({
    ...standardPackage,
    version: 4,
  });
  render(<RechargePackageManager />);
  await screen.findByText("标准档");

  fireEvent.click(screen.getByRole("button", { name: "编辑" }));
  const zheInput = screen.getByLabelText("折扣（折，留空表示无折扣）");
  expect(zheInput).toHaveValue(9);

  fireEvent.change(zheInput, { target: { value: "9.5" } });
  fireEvent.click(screen.getByRole("checkbox", { name: "数字人口播" }));
  fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

  expect(
    await screen.findByRole("dialog", { name: "保存充值套餐修改" }),
  ).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "调整折扣范围" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

  await waitFor(() =>
    expect(updateRechargePackage).toHaveBeenCalledWith(
      "pkg-standard",
      {
        name: "标准档",
        amount_fen: 20000,
        credits: 21000,
        discount_rate: "0.9500",
        discount_interfaces: ["video_generation", "oral"],
        sort_order: 1,
        is_active: true,
      },
      3,
      "调整折扣范围",
      expect.any(String),
    ),
  );
});

test("toggles a package off through the confirm dialog", async () => {
  vi.mocked(listRechargePackages).mockResolvedValue([standardPackage]);
  vi.mocked(updateRechargePackage).mockResolvedValue({
    ...standardPackage,
    is_active: false,
    version: 4,
  });
  render(<RechargePackageManager />);
  await screen.findByText("标准档");

  fireEvent.click(screen.getByRole("button", { name: "停用" }));
  expect(
    await screen.findByRole("dialog", { name: "保存充值套餐修改" }),
  ).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "下架标准档" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

  await waitFor(() =>
    expect(updateRechargePackage).toHaveBeenCalledWith(
      "pkg-standard",
      expect.objectContaining({ is_active: false }),
      3,
      "下架标准档",
      expect.any(String),
    ),
  );
});

test("clearing the discount empties the interface scope", async () => {
  vi.mocked(listRechargePackages).mockResolvedValue([standardPackage]);
  vi.mocked(updateRechargePackage).mockResolvedValue({
    ...standardPackage,
    discount_rate: null,
    discount_interfaces: [],
    version: 4,
  });
  render(<RechargePackageManager />);
  await screen.findByText("标准档");

  fireEvent.click(screen.getByRole("button", { name: "编辑" }));
  const videoCheckbox = screen.getByRole("checkbox", { name: "视频生成" });
  expect(videoCheckbox).toBeChecked();

  fireEvent.change(screen.getByLabelText("折扣（折，留空表示无折扣）"), {
    target: { value: "" },
  });
  // 服务端拒绝「无折扣却勾接口」：清空折数必须同步清空勾选并禁用范围。
  expect(videoCheckbox).not.toBeChecked();
  expect(videoCheckbox).toBeDisabled();

  fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
  await screen.findByRole("dialog", { name: "保存充值套餐修改" });
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "取消折扣" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

  await waitFor(() =>
    expect(updateRechargePackage).toHaveBeenCalledWith(
      "pkg-standard",
      expect.objectContaining({
        discount_rate: null,
        discount_interfaces: [],
      }),
      3,
      "取消折扣",
      expect.any(String),
    ),
  );
});

test("read-only mode hides the form and row actions", async () => {
  vi.mocked(listRechargePackages).mockResolvedValue([standardPackage]);
  render(<RechargePackageManager readOnly />);
  await screen.findByText("标准档");

  expect(
    screen.queryByRole("button", { name: "新建套餐" }),
  ).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "编辑" })).toBeNull();
  expect(screen.queryByRole("button", { name: "停用" })).toBeNull();
  expect(screen.queryByLabelText("套餐名称")).toBeNull();
});

test("keeps the dialog open and surfaces deterministic write errors", async () => {
  vi.mocked(listRechargePackages).mockResolvedValue([standardPackage]);
  vi.mocked(updateRechargePackage).mockRejectedValue(
    new AdminControlError(
      "套餐已被他人修改，请刷新后重试。",
      409,
      "RECHARGE_PACKAGE_VERSION_CONFLICT",
    ),
  );
  render(<RechargePackageManager />);
  await screen.findByText("标准档");

  fireEvent.click(screen.getByRole("button", { name: "编辑" }));
  fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
  await screen.findByRole("dialog", { name: "保存充值套餐修改" });
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "调整排序" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

  await waitFor(() =>
    expect(
      screen
        .getAllByRole("alert")
        .map((node) => node.textContent)
        .join(" "),
    ).toContain("套餐已被他人修改，请刷新后重试。"),
  );
  // 失败后保留对话框与输入，操作者可改原因/刷新后重试。
  expect(
    screen.getByRole("dialog", { name: "保存充值套餐修改" }),
  ).toBeInTheDocument();
});

test("blocks submit when the draft fails validation", async () => {
  vi.mocked(listRechargePackages).mockResolvedValue([]);
  render(<RechargePackageManager />);
  await screen.findByText("暂无充值套餐");

  fireEvent.click(screen.getByRole("button", { name: "新建套餐" }));
  await screen.findByRole("dialog", { name: "新建充值套餐" });
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "创建" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认新建" }));

  await waitFor(() =>
    expect(screen.getByText(/请填写套餐名称/)).toBeInTheDocument(),
  );
  expect(createRechargePackage).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).toBeNull();
});
