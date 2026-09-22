import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setAdminCsrfToken } from "../api";
import { SystemSettingsPage } from "./SystemSettingsPage";

// 付费探针入口的管理端接线：免费连接测试与付费探针在同一张服务卡上并列，
// 且付费探针必须走控制面写契约——带 X-Admin-CSRF（api.admin 的 requestControl
// 不会自动补这个头）与 Idempotency-Key，body 带 confirm + 非空 reason。
// 这里用真实 fetch mock 跑通真实 api.admin + 真实 SettingsPanel 的整条链路，
// 「缺 CSRF 头即 403 ADMIN_CSRF_REQUIRED」的坑（reconcile 踩过一次）由断言钉住。

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

const settingsSnapshot = {
  providers: {
    metaso: { provider: "metaso", configured: true, config: {} },
    apilio: { provider: "apilio", configured: true, config: {} },
    cos: {
      provider: "cos",
      configured: true,
      config: { bucket: "bucket-1", region: "ap-shanghai" },
    },
    deepseek: { provider: "deepseek", configured: false, config: {} },
    hifly: { provider: "hifly", configured: true, config: {} },
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

type FetchMock = ReturnType<typeof vi.fn>;

function installFetch(options?: {
  paidProbe?: "ok" | "not-implemented";
}): FetchMock {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith("/api/control/settings") && !init?.method) {
      return jsonResponse(settingsSnapshot);
    }
    if (url.endsWith("/api/control/settings/h3-accounts") && !init?.method) {
      return jsonResponse({
        managed: false,
        total_concurrency: 0,
        accounts: [],
      });
    }
    if (url.endsWith("/api/control/settings/queue-mode") && !init?.method) {
      return jsonResponse({ fair_queue_enabled: false });
    }
    if (url.endsWith("/api/control/settings/viral") && !init?.method) {
      return jsonResponse({
        collection_enabled: false,
        import_enabled: false,
        pending_imports: 0,
        running_imports: 0,
      });
    }
    if (
      url.endsWith("/api/control/settings/providers/apilio/paid-test") &&
      init?.method === "POST"
    ) {
      if (options?.paidProbe === "not-implemented") {
        return jsonResponse(
          {
            detail: {
              code: "PROVIDER_TEST_NOT_IMPLEMENTED",
              message:
                "A real provider client is required before paid tests can run.",
            },
          },
          501,
        );
      }
      return jsonResponse({
        status: "ok",
        provider: "apilio",
        test_kind: "paid_probe",
      });
    }
    throw new Error(`unexpected request: ${url} ${init?.method ?? "GET"}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** The paid probe lives on `apilio`: `metaso` is replaced by the video-account manager. */
async function apilioCard(container: HTMLElement): Promise<HTMLElement> {
  let card: HTMLElement | null = null;
  await waitFor(() => {
    card = container.querySelector("form[data-provider='apilio']");
    expect(card).not.toBeNull();
  });
  if (!card) {
    throw new Error("apilio provider card not rendered");
  }
  return card;
}

function paidProbePost(fetchMock: FetchMock) {
  return fetchMock.mock.calls.find(
    ([url, init]) =>
      String(url).endsWith(
        "/api/control/settings/providers/apilio/paid-test",
      ) && (init as RequestInit | undefined)?.method === "POST",
  );
}

/** Fill the reason and confirm — the paid probe is a write-contract action. */
function confirmPaidProbe(card: HTMLElement, reason = "上线前核对付费通道") {
  const scope = within(card);
  fireEvent.click(scope.getByRole("button", { name: "付费探针" }));
  fireEvent.change(scope.getByLabelText("操作原因（必填，写入审计）"), {
    target: { value: reason },
  });
  fireEvent.click(scope.getByRole("button", { name: "确认执行付费探针" }));
}

describe("SystemSettingsPage 付费探针入口", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setAdminCsrfToken("");
  });

  it("renders the paid probe beside the free connection test in the providers tab", async () => {
    setAdminCsrfToken("csrf-token-1");
    installFetch();
    const { container } = render(<SystemSettingsPage initialTab="services" />);

    const scope = within(await apilioCard(container));
    // 「真正并列」：同一张服务卡上，免费连接测试与付费探针并排。
    expect(scope.getByRole("button", { name: "测试连接" })).toBeInTheDocument();
    expect(scope.getByRole("button", { name: "付费探针" })).toBeInTheDocument();
  });

  it("sends the admin write envelope (CSRF + idempotency key + confirm + reason)", async () => {
    setAdminCsrfToken("csrf-token-1");
    const fetchMock = installFetch();
    const { container } = render(<SystemSettingsPage initialTab="services" />);

    const card = await apilioCard(container);
    confirmPaidProbe(card);

    await waitFor(() => expect(paidProbePost(fetchMock)).toBeDefined());
    const init = paidProbePost(fetchMock)?.[1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(headers.get("X-Admin-CSRF")).toBe("csrf-token-1");
    expect(headers.get("Idempotency-Key")).toBeTruthy();
    expect(JSON.parse(String(init.body))).toEqual({
      confirm: true,
      reason: "上线前核对付费通道",
    });
    await waitFor(() =>
      expect(card.textContent).toContain(
        "付费探针通过：供应商账号可完成一次计费调用",
      ),
    );
  });

  it("surfaces the unwired stub honestly without claiming a charge", async () => {
    setAdminCsrfToken("csrf-token-1");
    installFetch({ paidProbe: "not-implemented" });
    const { container } = render(<SystemSettingsPage initialTab="services" />);

    const card = await apilioCard(container);
    confirmPaidProbe(card);

    await waitFor(() =>
      expect(card.querySelector("[role='alert']")?.textContent).toBe(
        "付费探针未执行：当前版本尚未接入真实供应商客户端（服务端 501 未实现），未产生任何费用。",
      ),
    );
    expect(card.textContent).not.toContain("已产生费用");
    expect(card.textContent).not.toContain("已发起计费调用");
  });

  it("does not render a paid probe on the runtime service tab", async () => {
    setAdminCsrfToken("csrf-token-1");
    installFetch();
    const { container } = render(<SystemSettingsPage initialTab="services" />);

    await apilioCard(container);
    fireEvent.click(screen.getByRole("tab", { name: "运行控制" }));

    await waitFor(() =>
      expect(
        container.querySelector(".admin-services__runtime"),
      ).not.toBeNull(),
    );
    expect(container.querySelector("form[data-provider='apilio']")).toBeNull();
    expect(screen.queryByRole("button", { name: "付费探针" })).toBeNull();
  });
});
