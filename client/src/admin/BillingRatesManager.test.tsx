import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { adminRead, adminWrite } from "../api.admin";
import { BillingRatesManager } from "./BillingRatesManager";

vi.mock("../api.admin", () => ({ adminRead: vi.fn(), adminWrite: vi.fn() }));
const catalog = {
  pricing: { version: 3, points_per_yuan: 100 },
  services: [
    {
      service: "video_768p",
      name: "视频生成 · 768P",
      unit: "second",
      provider: "metaso",
      module: "video",
      customer_charge_allowed: true,
      zero_cost_platform: false,
      configured: false,
      updated_at: "2026-09-20T04:00:00+00:00",
      updated_by: "price_admin",
      tariff: {
        enabled: false,
        unit_credits: null,
        unit_cost_fen: null,
        unit_rounding: "ceil",
        version: 0,
      },
    },
  ],
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(adminRead).mockResolvedValue(catalog);
  vi.mocked(adminWrite).mockResolvedValue(catalog);
});

test("cost-only configuration leaves the customer tariff absent and disabled", async () => {
  render(<BillingRatesManager />);
  fireEvent.click(
    await screen.findByRole("button", { name: "配置 视频生成 · 768P" }),
  );
  const row = screen
    .getByRole("textbox", { name: "成本（积分 / 秒，留空待核对）" })
    .closest("tr");
  expect(row).not.toBeNull();
  expect(
    within(row as HTMLTableRowElement).getByText("视频生成 · 768P"),
  ).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("成本（积分 / 秒，留空待核对）"), {
    target: { value: "0.000125" },
  });
  expect(screen.queryByLabelText("调整原因")).not.toBeInTheDocument();
  // 2026-09-12 评审 P2 之后：按钮拆成「保存」（校验）→ 确认框「确认并保存」（落库）。
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  fireEvent.click(await screen.findByRole("button", { name: "确认并保存" }));
  await waitFor(() =>
    expect(adminWrite).toHaveBeenCalledWith(
      "/api/control/billing/tariff",
      {
        service: "video_768p",
        expected_version: 0,
        expected_pricing_version: 3,
        tariff: {
          enabled: false,
          unit_credits: null,
          unit_cost_fen: "0.000125",
          unit_rounding: "ceil",
        },
      },
      "配置视频生成 · 768P成本与售价",
      expect.any(String),
      expect.any(String),
      "PUT",
    ),
  );
});

test("enabling a tariff requires an explicit price and preserves the key after an uncertain write", async () => {
  vi.mocked(adminWrite).mockRejectedValueOnce(new Error("请求结果未知"));
  render(<BillingRatesManager />);
  fireEvent.click(
    await screen.findByRole("button", { name: "配置 视频生成 · 768P" }),
  );
  fireEvent.click(screen.getByLabelText("启用用户扣分"));
  expect(screen.queryByLabelText("调整原因")).not.toBeInTheDocument();
  // 校验不过时连确认框都不该出现：格式错误当场报，不必先弹框。
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  expect(adminWrite).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("售价（积分 / 秒）"), {
    target: { value: "0.25" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  fireEvent.click(await screen.findByRole("button", { name: "确认并保存" }));
  await screen.findByText("请求结果未知");
  // 失败留在确认框内，就地重试沿用同一幂等键。
  fireEvent.click(screen.getByRole("button", { name: "确认并保存" }));
  await waitFor(() => expect(adminWrite).toHaveBeenCalledTimes(2));
  expect(vi.mocked(adminWrite).mock.calls[1]).toEqual(
    vi.mocked(adminWrite).mock.calls[0],
  );
});

test("enabling a tariff rejects a zero price instead of storing a state the server refuses", async () => {
  render(<BillingRatesManager />);
  fireEvent.click(
    await screen.findByRole("button", { name: "配置 视频生成 · 768P" }),
  );
  fireEvent.click(screen.getByLabelText("启用用户扣分"));
  // "0" 能过格式校验（^\d+(\.\d{1,6})?$），但服务端 fail-closed 守卫把单价 0
  // 判为「资费未配置」并 503：放它入库只会让「明明配了却用不了」拖到运行时才发现。
  fireEvent.change(screen.getByLabelText("售价（积分 / 秒）"), {
    target: { value: "0" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  expect(await screen.findByText(/大于 0 的售价/)).toBeInTheDocument();
  expect(adminWrite).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("auditors can read tariff rows but cannot open the write form", async () => {
  render(<BillingRatesManager readOnly />);
  expect(
    await screen.findByRole("button", { name: "配置 视频生成 · 768P" }),
  ).toBeDisabled();
  expect(
    screen.queryByRole("checkbox", { name: "启用用户扣分" }),
  ).not.toBeInTheDocument();
});

test("cost credits use the current exchange ratio while sale credits stay unchanged", async () => {
  vi.mocked(adminRead).mockResolvedValue({
    ...catalog,
    pricing: { version: 9, points_per_yuan: 200 },
  });
  render(<BillingRatesManager />);
  fireEvent.click(
    await screen.findByRole("button", { name: "配置 视频生成 · 768P" }),
  );
  fireEvent.change(screen.getByLabelText("成本（积分 / 秒，留空待核对）"), {
    target: { value: "25" },
  });
  fireEvent.change(screen.getByLabelText("售价（积分 / 秒）"), {
    target: { value: "40" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  fireEvent.click(await screen.findByRole("button", { name: "确认并保存" }));
  await waitFor(() =>
    expect(adminWrite).toHaveBeenCalledWith(
      "/api/control/billing/tariff",
      expect.objectContaining({
        expected_pricing_version: 9,
        tariff: expect.objectContaining({
          unit_credits: "40",
          unit_cost_fen: "12.5",
        }),
      }),
      expect.any(String),
      expect.any(String),
      expect.any(String),
      "PUT",
    ),
  );
});

test("platform subjects list supplier cost separately and never charge customers", async () => {
  vi.mocked(adminRead).mockResolvedValue({
    pricing: catalog.pricing,
    services: [
      ...catalog.services,
      {
        service: "quality_inspection",
        name: "图片及视频质量检查",
        unit: "call",
        provider: "apilio",
        module: "internal",
        customer_charge_allowed: false,
        zero_cost_platform: false,
        configured: false,
        tariff: {
          enabled: false,
          unit_credits: null,
          unit_cost_fen: null,
          unit_rounding: "ceil",
          version: 0,
        },
      },
      {
        service: "cos",
        name: "云存储",
        unit: "call",
        provider: "cos",
        module: "infrastructure",
        customer_charge_allowed: false,
        zero_cost_platform: true,
        configured: false,
        tariff: {
          enabled: false,
          unit_credits: null,
          unit_cost_fen: null,
          unit_rounding: "ceil",
          version: 0,
        },
      },
    ],
  });
  render(<BillingRatesManager />);
  const paid = await screen.findByRole("table", { name: "API 端点成本与售价" });
  expect(
    within(paid).queryByText("图片及视频质量检查"),
  ).not.toBeInTheDocument();
  const platform = screen.getByRole("table", {
    name: "平台科目成本（不向客户收费）",
  });
  const inspectionRow = within(platform)
    .getByText("图片及视频质量检查")
    .closest("tr") as HTMLTableRowElement;
  expect(within(inspectionRow).getByText("平台承担")).toBeInTheDocument();
  expect(within(inspectionRow).getByText("不向客户收费")).toBeInTheDocument();
  fireEvent.click(
    within(inspectionRow).getByRole("button", {
      name: "配置 图片及视频质量检查",
    }),
  );
  fireEvent.change(
    within(platform).getByLabelText("成本（积分 / 次，留空待核对）"),
    { target: { value: "0.5" } },
  );
  expect(
    within(platform).queryByLabelText("售价（积分 / 次）"),
  ).not.toBeInTheDocument();
  expect(screen.queryByLabelText("启用用户扣分")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  fireEvent.click(await screen.findByRole("button", { name: "确认并保存" }));
  await waitFor(() =>
    expect(adminWrite).toHaveBeenCalledWith(
      "/api/control/billing/tariff",
      {
        service: "quality_inspection",
        expected_version: 0,
        expected_pricing_version: 3,
        tariff: {
          enabled: false,
          unit_credits: null,
          unit_cost_fen: "0.5",
          unit_rounding: "ceil",
        },
      },
      "配置图片及视频质量检查成本",
      expect.any(String),
      expect.any(String),
      "PUT",
    ),
  );
});

test("zero-cost platform subjects stay locked with an explanatory note", async () => {
  vi.mocked(adminRead).mockResolvedValue({
    pricing: catalog.pricing,
    services: [
      ...catalog.services,
      {
        service: "cos",
        name: "云存储",
        unit: "call",
        provider: "cos",
        module: "infrastructure",
        customer_charge_allowed: false,
        zero_cost_platform: true,
        configured: false,
        tariff: {
          enabled: false,
          unit_credits: null,
          unit_cost_fen: null,
          unit_rounding: "ceil",
          version: 0,
        },
      },
    ],
  });
  render(<BillingRatesManager />);
  const platform = await screen.findByRole("table", {
    name: "平台科目成本（不向客户收费）",
  });
  const row = within(platform)
    .getByText("云存储")
    .closest("tr") as HTMLTableRowElement;
  expect(within(row).getByText("按零费用核算")).toBeInTheDocument();
  expect(
    within(row).getByRole("button", { name: "配置 云存储" }),
  ).toBeDisabled();
});

test("last tariff editor and edit time are shown per subject", async () => {
  vi.mocked(adminRead).mockResolvedValue({
    pricing: catalog.pricing,
    services: [
      catalog.services[0],
      {
        ...catalog.services[0],
        service: "oral",
        name: "数字人口播",
        updated_at: null,
        updated_by: null,
      },
    ],
  });
  render(<BillingRatesManager />);
  const editedRow = (await screen.findByText("视频生成 · 768P")).closest(
    "tr",
  ) as HTMLTableRowElement;
  expect(within(editedRow).getByText("price_admin")).toBeInTheDocument();
  expect(within(editedRow).getByText(/2026/)).toBeInTheDocument();
  const untouchedRow = screen
    .getByText("数字人口播")
    .closest("tr") as HTMLTableRowElement;
  expect(within(untouchedRow).getByText("—")).toBeInTheDocument();
});

test("saving unchanged credit displays preserves the original sub-cent cost", async () => {
  vi.mocked(adminRead).mockResolvedValue({
    pricing: { version: 8, points_per_yuan: 3 },
    services: [
      {
        ...catalog.services[0],
        tariff: {
          ...catalog.services[0].tariff,
          unit_credits: "1",
          unit_cost_fen: "0.000001",
          enabled: true,
        },
      },
    ],
  });
  render(<BillingRatesManager />);
  // 成本按元展示（不足 1 分显示“< ¥0.01”，不虚报为 ¥0.00）；亚分成本在
  // 点击「配置」后的输入框里仍保留全精度。
  await screen.findByText("< ¥0.01");
  fireEvent.click(screen.getByRole("button", { name: "配置 视频生成 · 768P" }));
  expect(screen.getByLabelText("售价（积分 / 秒）")).toHaveValue("1");
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  fireEvent.click(await screen.findByRole("button", { name: "确认并保存" }));
  await waitFor(() =>
    expect(adminWrite).toHaveBeenCalledWith(
      "/api/control/billing/tariff",
      expect.objectContaining({
        expected_pricing_version: 8,
        tariff: expect.objectContaining({
          unit_credits: "1",
          unit_cost_fen: "0.000001",
        }),
      }),
      expect.any(String),
      expect.any(String),
      expect.any(String),
      "PUT",
    ),
  );
});

const tariffHistory = {
  service: "video_768p",
  name: "视频生成 · 768P",
  unit: "second",
  current_version: 2,
  items: [
    {
      version: 2,
      enabled: true,
      unit_credits: "0.25",
      unit_cost_fen: "0.000125",
      unit_rounding: "ceil",
      source: "audit",
      current: true,
      actor_user_id: "u-1",
      actor_username: "price_admin",
      reason: "首页活动价调整",
      effective_at: "2026-09-20T04:00:00+00:00",
    },
    {
      version: 1,
      enabled: false,
      unit_credits: null,
      unit_cost_fen: null,
      unit_rounding: "ceil",
      source: "current",
      current: false,
      actor_user_id: null,
      actor_username: null,
      reason: null,
      effective_at: null,
    },
  ],
};

const oralHistory = {
  service: "oral",
  name: "数字人口播",
  unit: "second",
  current_version: 1,
  items: [
    {
      version: 1,
      enabled: true,
      unit_credits: "0.5",
      unit_cost_fen: null,
      unit_rounding: "ceil",
      source: "audit",
      current: true,
      actor_user_id: "u-2",
      actor_username: "oral_admin",
      reason: null,
      effective_at: "2026-09-21T02:00:00+00:00",
    },
  ],
};

const twoSubjects = {
  pricing: catalog.pricing,
  services: [
    catalog.services[0],
    { ...catalog.services[0], service: "oral", name: "数字人口播" },
  ],
};

test("history button reads the subject price-version sequence and flags the current version", async () => {
  vi.mocked(adminRead).mockImplementation((path: string) =>
    Promise.resolve(path.includes("tariff-history") ? tariffHistory : catalog),
  );
  render(<BillingRatesManager />);
  fireEvent.click(
    await screen.findByRole("button", {
      name: "查看 视频生成 · 768P 的价目历史",
    }),
  );
  expect(adminRead).toHaveBeenCalledWith(
    "/api/control/billing/tariff-history?service=video_768p",
    "读取价目历史失败",
  );
  const table = await screen.findByRole("table", {
    name: "视频生成 · 768P价目版本历史",
  });
  const rows = within(table).getAllByRole("row").slice(1);
  expect(rows).toHaveLength(2);
  const [latest, earliest] = rows;
  expect(within(latest).getByText("V2")).toBeInTheDocument();
  expect(within(latest).getByText("当前")).toBeInTheDocument();
  expect(within(latest).getByText("0.25")).toBeInTheDocument();
  // P2-1：价目历史成本同样按元展示（不足 1 分显示“< ¥0.01”）。
  expect(within(latest).getByText("< ¥0.01")).toBeInTheDocument();
  expect(within(latest).getByText("用户承担")).toBeInTheDocument();
  expect(within(latest).getByText("price_admin")).toBeInTheDocument();
  expect(within(latest).getByText("首页活动价调整")).toBeInTheDocument();
  expect(within(latest).getByText("审计记录")).toBeInTheDocument();
  expect(within(latest).getByText(/2026/)).toBeInTheDocument();
  expect(within(earliest).getByText("V1")).toBeInTheDocument();
  expect(within(earliest).getByText("未配置")).toBeInTheDocument();
  expect(within(earliest).getByText("平台承担")).toBeInTheDocument();
  expect(within(earliest).getByText("当前行（无审计）")).toBeInTheDocument();
  expect(within(earliest).getAllByText("—").length).toBeGreaterThanOrEqual(3);
});

test("price history and inline editing stay mutually exclusive", async () => {
  vi.mocked(adminRead).mockImplementation((path: string) =>
    Promise.resolve(path.includes("tariff-history") ? tariffHistory : catalog),
  );
  render(<BillingRatesManager />);
  fireEvent.click(
    await screen.findByRole("button", {
      name: "查看 视频生成 · 768P 的价目历史",
    }),
  );
  await screen.findByRole("table", { name: "视频生成 · 768P价目版本历史" });
  fireEvent.click(screen.getByRole("button", { name: "配置 视频生成 · 768P" }));
  expect(
    screen.queryByRole("table", { name: "视频生成 · 768P价目版本历史" }),
  ).not.toBeInTheDocument();
  expect(
    screen.getByLabelText("成本（积分 / 秒，留空待核对）"),
  ).toBeInTheDocument();
  fireEvent.click(
    screen.getByRole("button", { name: "查看 视频生成 · 768P 的价目历史" }),
  );
  await screen.findByRole("table", { name: "视频生成 · 768P价目版本历史" });
  expect(
    screen.queryByLabelText("成本（积分 / 秒，留空待核对）"),
  ).not.toBeInTheDocument();
});

test("auditors can read price history while configuration stays locked", async () => {
  vi.mocked(adminRead).mockImplementation((path: string) =>
    Promise.resolve(path.includes("tariff-history") ? tariffHistory : catalog),
  );
  render(<BillingRatesManager readOnly />);
  const historyButton = await screen.findByRole("button", {
    name: "查看 视频生成 · 768P 的价目历史",
  });
  expect(historyButton).toBeEnabled();
  fireEvent.click(historyButton);
  await screen.findByRole("table", { name: "视频生成 · 768P价目版本历史" });
});

test("history failures surface an alert next to the subject", async () => {
  vi.mocked(adminRead).mockImplementation((path: string) => {
    if (path.includes("tariff-history"))
      return Promise.reject(new Error("读取价目历史失败：服务暂不可用（503）"));
    return Promise.resolve(catalog);
  });
  render(<BillingRatesManager />);
  fireEvent.click(
    await screen.findByRole("button", {
      name: "查看 视频生成 · 768P 的价目历史",
    }),
  );
  expect(await screen.findByText(/服务暂不可用/)).toBeInTheDocument();
});

test("a stale history response never replaces the newly selected subject", async () => {
  const pending: Array<(value: unknown) => void> = [];
  vi.mocked(adminRead).mockImplementation((path: string) => {
    if (path.includes("service=video_768p"))
      return new Promise((resolve) => pending.push(resolve));
    if (path.includes("tariff-history")) return Promise.resolve(oralHistory);
    return Promise.resolve(twoSubjects);
  });
  render(<BillingRatesManager />);
  fireEvent.click(
    await screen.findByRole("button", {
      name: "查看 视频生成 · 768P 的价目历史",
    }),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "查看 数字人口播 的价目历史" }),
  );
  await screen.findByRole("table", { name: "数字人口播价目版本历史" });
  for (const resolve of pending) resolve(tariffHistory);
  await waitFor(() =>
    expect(
      screen.queryByRole("table", { name: "视频生成 · 768P价目版本历史" }),
    ).not.toBeInTheDocument(),
  );
  expect(
    screen.getByRole("table", { name: "数字人口播价目版本历史" }),
  ).toBeInTheDocument();
});

test("holds the tariff write behind an explicit confirmation", async () => {
  // 2026-09-12 评审 P2：按钮写着"确认并保存"，保存却是直接落库。这条钉住
  // "确认过才写"，并顺带钉住取消路径不产生任何写入。
  render(<BillingRatesManager />);
  fireEvent.click(
    await screen.findByRole("button", { name: "配置 视频生成 · 768P" }),
  );
  fireEvent.change(screen.getByLabelText("售价（积分 / 秒）"), {
    target: { value: "2" },
  });
  fireEvent.click(screen.getByLabelText("启用用户扣分"));
  fireEvent.click(screen.getByRole("button", { name: "保存" }));

  const dialog = await screen.findByRole("dialog");
  // 确认框要把"改了什么"和"审计将记录什么原因"都摊开，避免盲确认。
  expect(dialog).toHaveTextContent("售价（积分）：未配置 → 2");
  expect(dialog).toHaveTextContent("收费状态：启用");
  expect(dialog).toHaveTextContent(
    "审计原因将记录为「配置视频生成 · 768P成本与售价」",
  );
  expect(adminWrite).not.toHaveBeenCalled();

  // 编辑表单里也有一个「取消」，这里要点确认框里的那个。
  fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(adminWrite).not.toHaveBeenCalled();
  // 取消不丢草稿：重新点保存还能拿回同一个确认框。
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  expect(await screen.findByRole("dialog")).toBeInTheDocument();
});
