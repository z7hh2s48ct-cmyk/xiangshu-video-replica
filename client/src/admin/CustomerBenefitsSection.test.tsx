import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import type { CustomerRechargePackage } from "../api";
import {
  AdminControlError,
  type CustomerDiscount,
  createCustomerDiscount,
  deactivateCustomerDiscount,
  grantCustomerPackage,
  listCustomerDiscounts,
  listRechargePackages,
} from "../api.admin";
import { CustomerBenefitsSection } from "./CustomerBenefitsSection";

// 组件用 AdminControlError 区分确定性 4xx 与结果不确定的失败，mock 必须提供同一个类。
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
    createCustomerDiscount: vi.fn(),
    deactivateCustomerDiscount: vi.fn(),
    grantCustomerPackage: vi.fn(),
    listCustomerDiscounts: vi.fn(),
    listRechargePackages: vi.fn(),
  };
});

const annualPackage: CustomerRechargePackage = {
  id: "pkg-annual",
  name: "年度档",
  amount_fen: 199800,
  credits: 2000,
  discount_rate: "0.9000",
  discount_interfaces: ["video_generation"],
  sort_order: 1,
  is_active: true,
  version: 4,
  created_at: null,
  updated_at: null,
};

const retiredPackage: CustomerRechargePackage = {
  ...annualPackage,
  id: "pkg-retired",
  name: "旧档",
  is_active: false,
};

const packageDiscount: CustomerDiscount = {
  id: "rpkg-discount-1",
  discount_rate: "0.9000",
  applicable_interfaces: ["video_generation"],
  priority: 100,
  is_active: true,
  valid_from: "2026-09-27T00:00:00+00:00",
  valid_until: null,
  source: "recharge_package",
  source_recharge_order_id: "order-1",
  package_name: "年度档",
  created_at: "2026-09-27T00:00:00+00:00",
};

const manualDiscount: CustomerDiscount = {
  ...packageDiscount,
  id: "manual-discount-1",
  discount_rate: "0.8500",
  applicable_interfaces: [],
  priority: 200,
  source: "manual",
  source_recharge_order_id: null,
  package_name: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listRechargePackages).mockResolvedValue([
    annualPackage,
    retiredPackage,
  ]);
  vi.mocked(listCustomerDiscounts).mockResolvedValue([
    manualDiscount,
    packageDiscount,
  ]);
});

// 等到数据行出现：「专项折扣」小标题在数据到达前就已渲染，不能作为加载完成信号。
async function waitForBenefits() {
  await screen.findByText("套餐「年度档」");
}

async function confirmWithReason(reason: string, acknowledge = false) {
  fireEvent.change(await screen.findByLabelText(/原因/), {
    target: { value: reason },
  });
  if (acknowledge) {
    fireEvent.click(screen.getByRole("checkbox", { name: /已知晓|确认/ }));
  }
  fireEvent.click(screen.getByRole("button", { name: /^确认/ }));
}

test("lists both benefit sources and only offers active packages", async () => {
  render(
    <CustomerBenefitsSection
      onChanged={vi.fn()}
      readOnly={false}
      userId="u1"
    />,
  );

  await waitForBenefits();
  // 表单小标题也叫「专项折扣」，表格里的来源列是第二处。
  expect(screen.getAllByText("专项折扣")).toHaveLength(2);
  expect(screen.getByText("全部消耗")).toBeTruthy();
  const options = screen
    .getAllByRole("option")
    .map((option) => option.textContent);
  expect(options.some((text) => text?.includes("年度档"))).toBe(true);
  expect(options.some((text) => text?.includes("旧档"))).toBe(false);
  // 套餐权益由最近覆盖管理，只有专项折扣能手工停用。
  expect(screen.getAllByRole("button", { name: "停用" })).toHaveLength(1);
});

test("grants a package with the displayed version and voucher, then refreshes", async () => {
  const onChanged = vi.fn();
  vi.mocked(grantCustomerPackage).mockResolvedValue({
    adjustment_id: "adj-1",
    order_id: "order-2",
    package_id: "pkg-annual",
    package_name: "年度档",
    amount_fen: 199800,
    credits: 2000,
    discount_id: "rpkg-discount-2",
    discount_rate: "0.9000",
    discount_interfaces: ["video_generation"],
    pricing_scope: "CUSTOMER_STANDARD",
    wallet_balance_after: 2050,
    source_document_type: "OFFLINE_PAYMENT",
    source_document_ref: "BANK-1",
    request_id: "req-1",
  });
  render(
    <CustomerBenefitsSection
      onChanged={onChanged}
      readOnly={false}
      userId="u1"
    />,
  );
  await waitForBenefits();

  fireEvent.change(screen.getByLabelText("套餐"), {
    target: { value: "pkg-annual" },
  });
  fireEvent.change(screen.getByLabelText("收款凭证号"), {
    target: { value: " BANK-1 " },
  });
  fireEvent.click(screen.getByRole("button", { name: "开通套餐" }));
  expect(await screen.findByText(/收款凭证号：BANK-1/)).toBeTruthy();
  await confirmWithReason("对公转账已到账", true);

  await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  expect(grantCustomerPackage).toHaveBeenCalledWith(
    "u1",
    { packageId: "pkg-annual", packageVersion: 4, sourceDocumentRef: "BANK-1" },
    "对公转账已到账",
    expect.any(String),
  );
  expect(await screen.findByText(/已开通「年度档」/)).toBeTruthy();
  expect(listCustomerDiscounts).toHaveBeenCalledTimes(2);
});

test("a package version conflict closes the dialog and reloads packages", async () => {
  vi.mocked(grantCustomerPackage).mockRejectedValueOnce(
    new AdminControlError(
      "开通套餐失败：套餐已被修改，请刷新后核对金额与权益再开通。（409）",
      409,
      "RECHARGE_PACKAGE_VERSION_CONFLICT",
    ),
  );
  render(
    <CustomerBenefitsSection
      onChanged={vi.fn()}
      readOnly={false}
      userId="u1"
    />,
  );
  await waitForBenefits();
  fireEvent.change(screen.getByLabelText("套餐"), {
    target: { value: "pkg-annual" },
  });
  fireEvent.change(screen.getByLabelText("收款凭证号"), {
    target: { value: "BANK-3" },
  });
  fireEvent.click(screen.getByRole("button", { name: "开通套餐" }));
  await confirmWithReason("线下收款", true);

  expect(await screen.findByText(/套餐已被修改/)).toBeTruthy();
  expect(screen.queryByRole("dialog")).toBeNull();
  await waitFor(() => expect(listRechargePackages).toHaveBeenCalledTimes(2));
});

test("an uncertain grant failure retries with the same idempotency key", async () => {
  vi.mocked(grantCustomerPackage)
    .mockRejectedValueOnce(new AdminControlError("服务暂不可用", 503))
    .mockRejectedValueOnce(new AdminControlError("服务暂不可用", 503));
  render(
    <CustomerBenefitsSection
      onChanged={vi.fn()}
      readOnly={false}
      userId="u1"
    />,
  );
  await waitForBenefits();
  fireEvent.change(screen.getByLabelText("套餐"), {
    target: { value: "pkg-annual" },
  });
  fireEvent.change(screen.getByLabelText("收款凭证号"), {
    target: { value: "BANK-2" },
  });
  fireEvent.click(screen.getByRole("button", { name: "开通套餐" }));
  await confirmWithReason("线下收款", true);
  expect(await screen.findByText(/结果未确认/)).toBeTruthy();

  // 对话框保留原因与已勾选的确认，直接再次确认即重试。
  fireEvent.click(screen.getByRole("button", { name: "确认开通" }));
  await waitFor(() => expect(grantCustomerPackage).toHaveBeenCalledTimes(2));
  const [first, second] = vi.mocked(grantCustomerPackage).mock.calls;
  expect(second[3]).toBe(first[3]);
});

test("sets a manual discount from zhe input with scope and expiry", async () => {
  vi.mocked(createCustomerDiscount).mockResolvedValue({
    discount: { ...manualDiscount, discount_rate: "0.8000" },
    replaced_discount_ids: ["manual-discount-1"],
    request_id: "req-2",
  });
  render(
    <CustomerBenefitsSection
      onChanged={vi.fn()}
      readOnly={false}
      userId="u1"
    />,
  );
  await waitForBenefits();

  fireEvent.change(screen.getByLabelText("折扣（折）"), {
    target: { value: "8" },
  });
  fireEvent.change(screen.getByLabelText("到期日（留空为永久）"), {
    target: { value: "2026-12-31" },
  });
  fireEvent.click(screen.getByRole("checkbox", { name: "视频生成" }));
  fireEvent.click(screen.getByRole("button", { name: "设置专项折扣" }));
  await confirmWithReason("年框价");

  await waitFor(() => expect(createCustomerDiscount).toHaveBeenCalled());
  expect(createCustomerDiscount).toHaveBeenCalledWith(
    "u1",
    {
      discountRate: "0.8000",
      applicableInterfaces: ["video_generation"],
      validUntil: "2026-12-31T23:59:59+08:00",
    },
    "年框价",
    expect.any(String),
  );
});

test("rejects an out-of-range zhe before opening the dialog", async () => {
  render(
    <CustomerBenefitsSection
      onChanged={vi.fn()}
      readOnly={false}
      userId="u1"
    />,
  );
  await waitForBenefits();
  fireEvent.change(screen.getByLabelText("折扣（折）"), {
    target: { value: "12" },
  });
  // 浏览器的 max 校验会先拦一道；这里绕过它，验证组件自身的兜底校验。
  const form = screen
    .getByRole("button", { name: "设置专项折扣" })
    .closest("form");
  if (!form) throw new Error("discount form missing");
  fireEvent.submit(form);

  expect(await screen.findByText(/折扣须为 0–10/)).toBeTruthy();
  expect(screen.queryByLabelText(/原因/)).toBeNull();
});

test("deactivates the manual discount", async () => {
  vi.mocked(deactivateCustomerDiscount).mockResolvedValue({
    discount_id: "manual-discount-1",
    was_active: true,
    request_id: "req-3",
  });
  render(
    <CustomerBenefitsSection
      onChanged={vi.fn()}
      readOnly={false}
      userId="u1"
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "停用" }));
  await confirmWithReason("到期");

  await waitFor(() =>
    expect(deactivateCustomerDiscount).toHaveBeenCalledWith(
      "u1",
      "manual-discount-1",
      "到期",
      expect.any(String),
    ),
  );
});

test("auditors see benefits without write controls or package reads", async () => {
  render(
    <CustomerBenefitsSection onChanged={vi.fn()} readOnly={true} userId="u1" />,
  );
  await waitForBenefits();

  expect(screen.queryByRole("button", { name: "开通套餐" })).toBeNull();
  expect(screen.queryByRole("button", { name: "停用" })).toBeNull();
  expect(listRechargePackages).not.toHaveBeenCalled();
});
