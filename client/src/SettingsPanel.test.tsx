import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getControlSettings,
  SESSION_EXPIRED_EVENT,
  testControlProviderConnection,
  updateControlBillingSettings,
  updateControlProviderSettings,
  updateControlRuntimeSettings,
} from "./api";
import { type SettingsBackend, SettingsPanel } from "./SettingsPanel";

// CW-019：控制面后端由管理端调用方注入，SettingsPanel 本体不再静态引用
// `*Control*` API。测试沿用真实 api 函数 + fetch mock 验证注入后的行为等价。
const controlTestBackend: SettingsBackend = {
  load: getControlSettings,
  saveProvider: updateControlProviderSettings,
  saveRuntime: updateControlRuntimeSettings,
  saveBilling: updateControlBillingSettings,
  testProvider: testControlProviderConnection,
};

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

// Secret discipline: only ever a dummy placeholder value, never a real key.
const DUMMY_KEY = "test-key-1";

const settingsSnapshot = {
  providers: {
    metaso: { provider: "metaso", configured: true, config: {} },
    apilio: { provider: "apilio", configured: false, config: {} },
    cos: {
      provider: "cos",
      configured: true,
      config: { bucket: "bucket-1", region: "ap-shanghai" },
    },
    deepseek: { provider: "deepseek", configured: false, config: {} },
    hifly: {
      provider: "hifly",
      configured: true,
      config: {},
    },
    tikhub: { provider: "tikhub", configured: false, config: {} },
    dashscope: { provider: "dashscope", configured: false, config: {} },
    douyidou: { provider: "douyidou", configured: false, config: {} },
  },
  runtime: {
    max_generation_count_per_batch: 5,
    max_concurrent_h3_tasks: 2,
    active_storage_provider: "cos",
  },
  billing: {
    internal_base_unit_price_fen: 1000,
    charged_unit_price_fen: 1000,
    oral_unit_price_fen: 1800,
    min_recharge_fen: 10000,
    recharge_step_fen: 1000,
  },
};

function installFetch(options?: {
  providerSave?: "ok" | "fail";
  hiflyCheck?:
    | "ok"
    | "auth"
    | "timeout"
    | "missing-credit"
    | "unsafe-credit"
    | "infinite-credit";
}) {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith("/api/admin/settings")) {
      return jsonResponse(settingsSnapshot);
    }
    if (
      url.endsWith("/api/admin/settings/billing") &&
      init?.method === "PATCH"
    ) {
      return jsonResponse({
        ...settingsSnapshot.billing,
        oral_unit_price_fen: 2500,
      });
    }
    if (
      url.endsWith("/api/admin/settings/providers/metaso") &&
      init?.method === "PUT"
    ) {
      if (options?.providerSave === "fail") {
        return jsonResponse({}, 500);
      }
      return jsonResponse({
        provider: "metaso",
        configured: true,
        config: {},
      });
    }
    if (
      url.endsWith("/api/admin/settings/providers/metaso/connection-test") &&
      init?.method === "POST"
    ) {
      return jsonResponse({
        status: "ok",
        provider: "metaso",
        test_kind: "metaso_h3",
      });
    }
    if (
      url.endsWith(
        "/api/admin/settings/providers/metaso/secrets/api_key/reveal",
      ) &&
      init?.method === "POST"
    ) {
      return jsonResponse({ value: DUMMY_KEY });
    }
    if (
      url.endsWith("/api/admin/settings/providers/hifly/connection-test") &&
      init?.method === "POST"
    ) {
      if (options?.hiflyCheck === "auth") {
        return jsonResponse(
          {
            detail: {
              code: "HIFLY_AUTH_FAILED",
              message: "Hifly 凭据认证失败；未创建收费任务。",
            },
          },
          422,
        );
      }
      if (options?.hiflyCheck === "timeout") {
        return jsonResponse(
          {
            detail: {
              code: "HIFLY_ACCOUNT_CHECK_TIMEOUT",
              message: "Hifly 只读账户检查超时；未创建收费任务。",
            },
          },
          504,
        );
      }
      if (options?.hiflyCheck === "missing-credit") {
        return jsonResponse({
          status: "ok",
          provider: "hifly",
          test_kind: "account_credit",
        });
      }
      if (options?.hiflyCheck === "unsafe-credit") {
        return jsonResponse({
          status: "ok",
          provider: "hifly",
          test_kind: "account_credit",
          account_credit: Number.MAX_SAFE_INTEGER + 1,
        });
      }
      if (options?.hiflyCheck === "infinite-credit") {
        return jsonResponse({
          status: "ok",
          provider: "hifly",
          test_kind: "account_credit",
          account_credit: Number.POSITIVE_INFINITY,
        });
      }
      return jsonResponse({
        status: "ok",
        provider: "hifly",
        test_kind: "account_credit",
        account_credit: 321,
      });
    }
    if (
      url.endsWith("/api/admin/settings/providers/hifly") &&
      init?.method === "PUT"
    ) {
      return jsonResponse({
        provider: "hifly",
        configured: true,
        config: {},
      });
    }
    throw new Error(`unexpected request: ${url} ${init?.method ?? "GET"}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("SettingsPanel", () => {
  function providerCard(container: HTMLElement, provider: string) {
    const card = container.querySelector(
      `form[data-provider="${provider}"]`,
    ) as HTMLElement | null;
    if (!card) {
      throw new Error(`provider card not rendered: ${provider}`);
    }
    return within(card);
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders provider cards and runtime values from the loaded snapshot", async () => {
    installFetch();
    const { container } = render(<SettingsPanel />);

    expect(screen.getByText("正在读取服务设置")).toBeInTheDocument();
    expect(
      await screen.findByRole("region", { name: "服务设置" }),
    ).toBeInTheDocument();

    const metaso = providerCard(container, "metaso");
    expect(metaso.getByText("视频生成")).toBeInTheDocument();
    expect(metaso.getByText("已配置")).toBeInTheDocument();

    const apilio = providerCard(container, "apilio");
    expect(apilio.getByText("未配置")).toBeInTheDocument();

    const cos = providerCard(container, "cos");
    expect(cos.getByLabelText("Bucket")).toHaveValue("bucket-1");

    const hifly = providerCard(container, "hifly");
    expect(hifly.getByText("数字人口播")).toBeInTheDocument();
    expect(hifly.getByLabelText("API Key")).toHaveAttribute("type", "password");
    expect(
      hifly.getByText("Hifly · 只读检查账户余额，不会创建收费任务"),
    ).toBeInTheDocument();
    expect(hifly.getByLabelText("API Key")).toHaveValue("");
    expect(hifly.getByLabelText("API Key")).toHaveAttribute(
      "placeholder",
      "已保存，留空不修改",
    );

    expect(screen.getByText("运行设置")).toBeInTheDocument();
    expect(screen.getByLabelText("单次生成数量上限")).toHaveValue(5);
    expect(screen.getByLabelText("视频生成并发数")).toHaveValue(2);
    expect(screen.getByLabelText("数字人口播单价（元/条）")).toHaveValue(18);
  });

  it("explicitly enables cloud storage without saving unrelated draft limits", async () => {
    const runtime = {
      ...settingsSnapshot.runtime,
      active_storage_provider: "local" as const,
    };
    const saveRuntime = vi
      .fn()
      .mockResolvedValue({ ...runtime, active_storage_provider: "cos" });
    const backend = {
      ...controlTestBackend,
      load: vi.fn().mockResolvedValue({ ...settingsSnapshot, runtime }),
      saveRuntime,
    };
    render(
      <SettingsPanel
        source="control"
        controlBackend={backend}
        section="runtime"
      />,
    );
    const enable = await screen.findByRole("button", {
      name: "启用腾讯云存储",
    });
    expect(
      screen.getByText(/当前仍使用本地存储，云端分析无法读取本地视频/),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("单次生成数量上限"), {
      target: { value: "7" },
    });
    fireEvent.click(enable);
    await waitFor(() =>
      expect(saveRuntime).toHaveBeenCalledWith({
        ...runtime,
        active_storage_provider: "cos",
      }),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "启用腾讯云存储" }),
      ).toBeNull(),
    );
  });

  it("updates the oral unit price through the admin billing route", async () => {
    const fetchMock = installFetch();
    render(<SettingsPanel />);

    const input = await screen.findByLabelText("数字人口播单价（元/条）");
    fireEvent.change(input, { target: { value: "25" } });
    fireEvent.click(screen.getByRole("button", { name: "保存口播价格" }));

    expect(await screen.findByText("口播价格已保存")).toBeInTheDocument();
    const saveCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).endsWith("/api/admin/settings/billing") &&
        init?.method === "PATCH",
    );
    expect(saveCall?.[1]?.body).toBe(
      JSON.stringify({
        internal_base_unit_price_fen: 1000,
        oral_unit_price_fen: 2500,
        min_recharge_fen: 10000,
        recharge_step_fen: 1000,
      }),
    );
  });

  it("shows a role=alert error when the settings snapshot cannot be loaded", async () => {
    const fetchMock = vi.fn(() => jsonResponse({}, 500));
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsPanel />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("设置暂不可用（500）");
    expect(screen.queryByText("视频生成")).toBeNull();
  });

  it("saves an edited provider key through the PUT settings route", async () => {
    const fetchMock = installFetch();
    const { container } = render(<SettingsPanel />);

    await screen.findByText("视频生成");
    const metaso = providerCard(container, "metaso");

    const keyInput = metaso.getByLabelText("API Key");
    expect(keyInput).toHaveAttribute("type", "password");

    fireEvent.change(keyInput, { target: { value: DUMMY_KEY } });
    fireEvent.click(metaso.getByRole("button", { name: "保存" }));

    expect(await metaso.findByText("已保存")).toBeInTheDocument();
    // A saved secret never lingers in the form.
    expect(metaso.getByLabelText("API Key")).toHaveValue("");

    const saveCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).endsWith("/api/admin/settings/providers/metaso") &&
        init?.method === "PUT",
    );
    expect(saveCall).toBeDefined();
    expect(saveCall?.[1]?.body).toBe(
      JSON.stringify({ config: { api_key: DUMMY_KEY } }),
    );
  });

  it("按需读取并显示已保存密钥", async () => {
    const fetchMock = installFetch();
    const { container } = render(<SettingsPanel />);

    await screen.findByText("视频生成");
    const metaso = providerCard(container, "metaso");
    const input = metaso.getByLabelText("API Key");
    const reveal = metaso.getByRole("button", { name: "显示API Key" });

    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveValue("");

    fireEvent.click(reveal);

    await waitFor(() => expect(input).toHaveValue(DUMMY_KEY));
    expect(input).toHaveAttribute("type", "text");
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url).endsWith(
            "/api/admin/settings/providers/metaso/secrets/api_key/reveal",
          ) && init?.method === "POST",
      ),
    ).toBe(true);

    fireEvent.click(metaso.getByRole("button", { name: "隐藏API Key" }));
    expect(input).toHaveAttribute("type", "password");
  });

  it("reports a provider save failure inline without crashing", async () => {
    installFetch({ providerSave: "fail" });
    const { container } = render(<SettingsPanel />);

    await screen.findByText("视频生成");
    const metaso = providerCard(container, "metaso");

    fireEvent.change(metaso.getByLabelText("API Key"), {
      target: { value: DUMMY_KEY },
    });
    fireEvent.click(metaso.getByRole("button", { name: "保存" }));

    expect(
      await metaso.findByText("保存失败，请检查必填项与管理员权限。"),
    ).toBeInTheDocument();
    // 失败提示必须是 role="alert"（整改清单 评估登记 6：读屏即时播报）。
    expect(metaso.getByRole("alert")).toHaveTextContent("保存失败");
  });

  it("runs the connection test and surfaces the ok result", async () => {
    const fetchMock = installFetch();
    const { container } = render(<SettingsPanel />);

    await screen.findByText("视频生成");
    const metaso = providerCard(container, "metaso");

    fireEvent.click(metaso.getByRole("button", { name: "测试连接" }));

    expect(await metaso.findByText("连接测试通过")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url).endsWith(
            "/api/admin/settings/providers/metaso/connection-test",
          ) && init?.method === "POST",
      ),
    ).toBe(true);
  });

  it("shows the Hifly read-only account result without starting a paid task", async () => {
    const fetchMock = installFetch();
    const { container } = render(<SettingsPanel />);

    await screen.findByText("数字人口播");
    const hifly = providerCard(container, "hifly");
    fireEvent.click(hifly.getByRole("button", { name: "只读检查" }));

    expect(
      await hifly.findByText("只读账户检查通过，余额 321 积分；未创建收费任务"),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url).endsWith(
            "/api/admin/settings/providers/hifly/connection-test",
          ) && init?.method === "POST",
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("/api/oral/tasks"),
      ),
    ).toBe(false);
  });

  it("saves the Hifly token through the encrypted provider settings route", async () => {
    const fetchMock = installFetch();
    const { container } = render(<SettingsPanel />);

    await screen.findByText("数字人口播");
    const hifly = providerCard(container, "hifly");
    fireEvent.change(hifly.getByLabelText("API Key"), {
      target: { value: DUMMY_KEY },
    });
    fireEvent.click(hifly.getByRole("button", { name: "保存" }));

    expect(await hifly.findByText("已保存")).toBeInTheDocument();
    expect(hifly.getByLabelText("API Key")).toHaveValue("");
    const call = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).endsWith("/api/admin/settings/providers/hifly") &&
        init?.method === "PUT",
    );
    expect(call?.[1]?.body).toBe(
      JSON.stringify({ config: { api_key: DUMMY_KEY } }),
    );
  });

  it.each([
    ["auth", "Hifly 凭据认证失败；未创建收费任务。"],
    ["timeout", "Hifly 只读账户检查超时；未创建收费任务。"],
  ] as const)(
    "surfaces the Hifly %s result without claiming success",
    async (kind, message) => {
      installFetch({ hiflyCheck: kind });
      const { container } = render(<SettingsPanel />);

      await screen.findByText("数字人口播");
      const hifly = providerCard(container, "hifly");
      fireEvent.click(hifly.getByRole("button", { name: "只读检查" }));

      expect(await hifly.findByRole("alert")).toHaveTextContent(message);
      expect(hifly.queryByText(/检查通过/)).toBeNull();
    },
  );

  it.each(["missing-credit", "unsafe-credit", "infinite-credit"] as const)(
    "rejects a Hifly %s response without claiming success",
    async (kind) => {
      installFetch({ hiflyCheck: kind });
      const { container } = render(<SettingsPanel />);

      await screen.findByText("数字人口播");
      const hifly = providerCard(container, "hifly");
      fireEvent.click(hifly.getByRole("button", { name: "只读检查" }));

      expect(await hifly.findByRole("alert")).toHaveTextContent(
        "只读账户检查响应异常，请稍后重试。",
      );
      expect(hifly.queryByText(/检查通过/)).toBeNull();
      expect(hifly.queryByText(/连接测试通过/)).toBeNull();
    },
  );

  it.each([
    ["workspace", "/api/admin/settings"],
    ["control", "/api/control/settings"],
  ] as const)(
    "keeps the %s settings session active after Hifly rejects its own credential",
    async (source, basePath) => {
      const onSessionExpired = vi.fn();
      window.addEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
      const fetchMock = vi.fn((url: string, init?: RequestInit) => {
        if (url.endsWith(basePath) && !init?.method) {
          return jsonResponse(settingsSnapshot);
        }
        if (
          url.endsWith(`${basePath}/providers/hifly/connection-test`) &&
          init?.method === "POST"
        ) {
          return jsonResponse(
            {
              detail: {
                code: "HIFLY_AUTH_FAILED",
                message: "Hifly 凭据认证失败；未创建收费任务。",
              },
            },
            422,
          );
        }
        throw new Error(`unexpected request: ${url} ${init?.method ?? "GET"}`);
      });
      vi.stubGlobal("fetch", fetchMock);

      const { container } = render(
        source === "control" ? (
          <SettingsPanel controlBackend={controlTestBackend} source="control" />
        ) : (
          <SettingsPanel />
        ),
      );
      await screen.findByText("数字人口播");
      if (source === "control") {
        expect(
          screen.queryByRole("heading", { name: "数字人口播价格" }),
        ).not.toBeInTheDocument();
        expect(
          screen.queryByRole("button", { name: "保存口播价格" }),
        ).not.toBeInTheDocument();
      }
      const hifly = providerCard(container, "hifly");
      fireEvent.click(hifly.getByRole("button", { name: "只读检查" }));

      expect(await hifly.findByRole("alert")).toHaveTextContent(
        "Hifly 凭据认证失败；未创建收费任务。",
      );
      expect(onSessionExpired).not.toHaveBeenCalled();
      expect(hifly.getByRole("button", { name: "只读检查" })).toBeEnabled();
      expect(hifly.getByLabelText("API Key")).toBeEnabled();
      window.removeEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
    },
  );

  it("does not offer a paid probe on the workspace backend", async () => {
    installFetch();
    render(<SettingsPanel />);

    await screen.findByText("视频生成");
    // 付费探针只由管理端注入：客户/工作台面后端没有它，入口因此不出现。
    expect(screen.queryByRole("button", { name: "付费探针" })).toBeNull();
  });

  it("runs the paid probe through the injected write-contract backend", async () => {
    const testPaidProvider = vi.fn().mockResolvedValue({
      status: "ok",
      provider: "metaso",
      test_kind: "paid_probe",
    });
    const { container } = render(
      <SettingsPanel
        controlBackend={{
          ...controlTestBackend,
          load: vi.fn().mockResolvedValue(settingsSnapshot),
          testPaidProvider,
        }}
        section="providers"
        source="control"
      />,
    );

    await screen.findByText("视频生成");
    const metaso = providerCard(container, "metaso");
    // 免费连接测试与付费探针在每个服务卡上并列。
    expect(
      metaso.getByRole("button", { name: "测试连接" }),
    ).toBeInTheDocument();
    fireEvent.click(metaso.getByRole("button", { name: "付费探针" }));

    // 现状必须如实说明：供应商客户端未接入，执行不会产生费用。
    expect(metaso.getByText(/真实供应商客户端尚未接入/)).toBeInTheDocument();
    expect(metaso.getByText(/也不会产生任何费用/)).toBeInTheDocument();

    // 服务端要求非空 reason：空原因不得发起调用。
    fireEvent.click(metaso.getByRole("button", { name: "确认执行付费探针" }));
    expect(testPaidProvider).not.toHaveBeenCalled();
    expect(metaso.getByRole("alert")).toHaveTextContent(
      "请填写付费探针的操作原因（会写入审计）",
    );

    fireEvent.change(metaso.getByLabelText("操作原因（必填，写入审计）"), {
      target: { value: "  上线前核对付费通道  " },
    });
    fireEvent.click(metaso.getByRole("button", { name: "确认执行付费探针" }));

    await waitFor(() =>
      expect(testPaidProvider).toHaveBeenCalledWith(
        "metaso",
        "上线前核对付费通道",
      ),
    );
    expect(
      await metaso.findByText("付费探针通过：供应商账号可完成一次计费调用"),
    ).toBeInTheDocument();
    // 成功后确认面板收起（原因输入框随面板一起消失）。
    expect(metaso.queryByLabelText("操作原因（必填，写入审计）")).toBeNull();
  });

  it("states that nothing was charged while the provider client is unwired", async () => {
    const notImplemented = Object.assign(
      new Error(
        "付费探针执行失败：A real provider client is required before paid tests can run.（501）",
      ),
      { code: "PROVIDER_TEST_NOT_IMPLEMENTED" },
    );
    const testPaidProvider = vi.fn().mockRejectedValue(notImplemented);
    const { container } = render(
      <SettingsPanel
        controlBackend={{
          ...controlTestBackend,
          load: vi.fn().mockResolvedValue(settingsSnapshot),
          testPaidProvider,
        }}
        section="providers"
        source="control"
      />,
    );

    await screen.findByText("视频生成");
    const metaso = providerCard(container, "metaso");
    fireEvent.click(metaso.getByRole("button", { name: "付费探针" }));
    fireEvent.change(metaso.getByLabelText("操作原因（必填，写入审计）"), {
      target: { value: "上线前核对付费通道" },
    });
    fireEvent.click(metaso.getByRole("button", { name: "确认执行付费探针" }));

    const alert = await metaso.findByRole("alert");
    expect(alert).toHaveTextContent(
      "付费探针未执行：该服务尚未接入真实供应商客户端（服务端 501 未实现），未产生任何费用。",
    );
    // 现状下不得出现「会产生真实费用」这类不成立的提示。
    expect(alert).not.toHaveTextContent(/已产生费用/);
    expect(alert).not.toHaveTextContent(/已发起计费/);
  });

  it("keeps provider settings and checks disabled for read-only operators", async () => {
    installFetch();
    const { container } = render(<SettingsPanel readOnly />);

    await screen.findByText("数字人口播");
    const hifly = providerCard(container, "hifly");
    expect(hifly.getByLabelText("API Key")).toBeDisabled();
    expect(hifly.getByRole("button", { name: "保存" })).toBeDisabled();
    expect(hifly.getByRole("button", { name: "只读检查" })).toBeDisabled();
  });

  // 爆款数据源备用通道：主入口失效时服务端自动切到备用入口重取，
  // 界面要负责的三件事——怎么启用、提交什么、校验失败时把原因落在哪里。
  function tikhubBackend(config: Record<string, string> = {}) {
    const saveProvider = vi.fn().mockResolvedValue({
      provider: "tikhub",
      configured: true,
      config,
    });
    const backend: SettingsBackend = {
      load: vi.fn().mockResolvedValue({
        ...settingsSnapshot,
        providers: {
          ...settingsSnapshot.providers,
          tikhub: { provider: "tikhub", configured: true, config },
        },
      }),
      saveProvider,
      saveRuntime: vi.fn(),
      saveBilling: vi.fn(),
      testProvider: vi.fn(),
    };
    return { backend, saveProvider };
  }

  it("爆款数据源把备用通道单独成组，填了备用入口才给出启用说明", async () => {
    const { backend } = tikhubBackend({
      backup_base_url: "https://api-backup.example.com",
    });
    const { container } = render(
      <SettingsPanel
        source="control"
        controlBackend={backend}
        section="providers"
      />,
    );

    await screen.findByText("爆款视频数据源");
    const tikhub = providerCard(container, "tikhub");
    expect(tikhub.getByText("备用通道（可选）")).toBeInTheDocument();
    expect(tikhub.getByLabelText("备用入口")).toHaveValue(
      "https://api-backup.example.com",
    );
    // 备用密钥同样是密钥字段：只回显掩码，留空不改动。
    expect(tikhub.getByLabelText("备用密钥")).toHaveAttribute(
      "type",
      "password",
    );
    expect(tikhub.getByText(/备用通道已启用/)).toBeInTheDocument();
    expect(tikhub.getByText(/三个计费节点共用这条通道/)).toBeInTheDocument();
    expect(tikhub.getByText(/客户端不重复扣费/)).toBeInTheDocument();
  });

  it("未填备用入口时不出现启用说明，只留配对规则", async () => {
    const { backend } = tikhubBackend();
    const { container } = render(
      <SettingsPanel
        source="control"
        controlBackend={backend}
        section="providers"
      />,
    );

    await screen.findByText("爆款视频数据源");
    const tikhub = providerCard(container, "tikhub");
    expect(tikhub.queryByText(/备用通道已启用/)).toBeNull();
    expect(
      tikhub.getByText(/备用密钥必须与备用入口一起配置/),
    ).toBeInTheDocument();
  });

  it("保存爆款数据源时把备用入口与备用密钥一并提交", async () => {
    const { backend, saveProvider } = tikhubBackend();
    const { container } = render(
      <SettingsPanel
        source="control"
        controlBackend={backend}
        section="providers"
      />,
    );

    await screen.findByText("爆款视频数据源");
    const tikhub = providerCard(container, "tikhub");
    fireEvent.change(tikhub.getByLabelText("备用入口"), {
      target: { value: "https://api-backup.example.com" },
    });
    fireEvent.change(tikhub.getByLabelText("备用密钥"), {
      target: { value: DUMMY_KEY },
    });
    fireEvent.click(tikhub.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(saveProvider).toHaveBeenCalledWith("tikhub", {
        api_key: "",
        backup_base_url: "https://api-backup.example.com",
        backup_api_key: DUMMY_KEY,
      }),
    );
    // 保存后密钥字段清空：已存的密钥不会留在表单里。
    expect(tikhub.getByLabelText("备用密钥")).toHaveValue("");
  });

  it("备用通道校验失败时就近报错，并保留服务端的原始原因", async () => {
    const { backend } = tikhubBackend();
    const saveProvider = backend.saveProvider as ReturnType<typeof vi.fn>;
    saveProvider.mockRejectedValue(
      Object.assign(new Error("备用密钥需要与备用入口一起配置"), {
        code: "INVALID_SERVICE_SETTINGS",
      }),
    );
    const { container } = render(
      <SettingsPanel
        source="control"
        controlBackend={backend}
        section="providers"
      />,
    );

    await screen.findByText("爆款视频数据源");
    const tikhub = providerCard(container, "tikhub");
    fireEvent.change(tikhub.getByLabelText("备用密钥"), {
      target: { value: DUMMY_KEY },
    });
    fireEvent.click(tikhub.getByRole("button", { name: "保存" }));

    const alert = await tikhub.findByRole("alert");
    expect(alert).toHaveTextContent("备用密钥需要与备用入口一起配置");
    // 同一句话落在备用通道分组里（顶栏播报一次，读屏不重复）。
    expect(tikhub.getAllByText("备用密钥需要与备用入口一起配置")).toHaveLength(
      2,
    );
    expect(
      container.querySelector('form[data-provider="tikhub"] .provider-backup'),
    ).toHaveAttribute("data-invalid", "true");
  });

  it("非校验类保存失败不把英文原文抛给用户", async () => {
    const { backend } = tikhubBackend();
    const saveProvider = backend.saveProvider as ReturnType<typeof vi.fn>;
    saveProvider.mockRejectedValue(
      Object.assign(new Error("Failed to fetch"), { code: "NETWORK" }),
    );
    const { container } = render(
      <SettingsPanel
        source="control"
        controlBackend={backend}
        section="providers"
      />,
    );

    await screen.findByText("爆款视频数据源");
    const tikhub = providerCard(container, "tikhub");
    fireEvent.click(tikhub.getByRole("button", { name: "保存" }));

    const alert = await tikhub.findByRole("alert");
    expect(alert).toHaveTextContent("保存失败，请检查必填项与管理员权限。");
  });
});
