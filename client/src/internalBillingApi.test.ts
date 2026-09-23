import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createRechargeOrder,
  downloadControlRechargeOrdersCsv,
  downloadControlWalletTransactionsCsv,
  getControlAccounts,
  getControlSettings,
  getWallet,
  listRechargeOrders,
  listWalletTransactions,
  setInternalAccessToken,
  testControlProviderConnection,
  updateControlProviderSettings,
  updateControlRuntimeSettings,
  updateControlZPaySettings,
} from "./api";

const SERVICE_KEY_TEXT = ["service", "key"].join("-");

describe("internal billing API", () => {
  afterEach(() => {
    setInternalAccessToken(null);
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("sends the transient Bearer token to business wallet and recharge APIs", async () => {
    setInternalAccessToken("internal-user-token");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        items: [],
        available_credits: 10,
        reserved_credits: 0,
        internal_unit_price_fen: 1000,
        min_recharge_fen: 10000,
        recharge_step_fen: 1000,
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await getWallet();
    await listWalletTransactions({ limit: 20, offset: 0 });
    await createRechargeOrder(10000);
    await listRechargeOrders({ limit: 20, offset: 0 });

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://127.0.0.1:8000/api/wallet",
      "http://127.0.0.1:8000/api/wallet/transactions?limit=20&offset=0",
      "http://127.0.0.1:8000/api/recharge-orders",
      "http://127.0.0.1:8000/api/recharge-orders?limit=20&offset=0",
    ]);
    for (const [, options] of fetchMock.mock.calls) {
      expect((options.headers as Headers).get("Authorization")).toBe(
        "Bearer internal-user-token",
      );
    }
    expect(fetchMock.mock.calls[2]?.[1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ amount_fen: 10000 }),
      }),
    );
  });

  it("rejects a wallet response without usable pricing", async () => {
    setInternalAccessToken("internal-user-token");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          available_credits: 10,
          reserved_credits: 0,
          internal_unit_price_fen: null,
          min_recharge_fen: null,
          recharge_step_fen: null,
        }),
      }),
    );

    await expect(getWallet()).rejects.toThrow("钱包定价配置不完整");
  });

  it("keeps control requests separate from business and development identity", async () => {
    setInternalAccessToken("must-not-reach-control-api");
    vi.stubEnv("VITE_DEV_USER_ID", "employee_1");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ items: [], total: 0, limit: 50, offset: 0 }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await getControlAccounts();

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/control/accounts?limit=50&offset=0",
      expect.objectContaining({ headers: expect.any(Headers) }),
    );
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((options.headers as Headers).has("Authorization")).toBe(false);
    expect((options.headers as Headers).has("X-Dev-User-Id")).toBe(false);
  });

  it("reads masked control settings and only submits the allowed ZPay fields", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          zpay: {
            provider: "zpay",
            configured: true,
            config: { pid: "merchant", key: "********cret" },
          },
          billing: {},
          deployment: {},
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          provider: "zpay",
          configured: true,
          config: { pid: "merchant", key: "********cret" },
        }),
      });
    vi.stubGlobal("fetch", fetchMock);

    const snapshot = await getControlSettings();
    await updateControlZPaySettings({
      pid: "merchant",
      key: "",
      enabled_channels: ["alipay", "wxpay"],
    });

    expect(snapshot.zpay.config.key).toBe("********cret");
    const zpayRequest = fetchMock.mock.calls[1]?.[1] as RequestInit | undefined;
    expect(zpayRequest).toBeTruthy();
    expect(fetchMock.mock.calls[1]?.[1]).toEqual(
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({
          pid: "merchant",
          key: "",
          enabled_channels: ["alipay", "wxpay"],
          confirm: true,
          reason: "更新 ZPay 支付配置",
        }),
      }),
    );
    expect(
      new Headers(zpayRequest?.headers).get("Idempotency-Key"),
    ).toBeTruthy();
  });

  it("manages service and runtime settings through the protected control plane", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: "ok" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await updateControlProviderSettings("metaso", {
      api_key: SERVICE_KEY_TEXT,
    });
    await testControlProviderConnection("metaso");
    await updateControlRuntimeSettings({
      max_generation_count_per_batch: 2,
      max_concurrent_h3_tasks: 1,
      active_storage_provider: "cos",
    });

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://127.0.0.1:8000/api/control/settings/providers/metaso",
      "http://127.0.0.1:8000/api/control/settings/providers/metaso/connection-test",
      "http://127.0.0.1:8000/api/control/settings/runtime",
    ]);
    expect(fetchMock.mock.calls[0]?.[1]).toEqual(
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          config: { api_key: SERVICE_KEY_TEXT },
          confirm: true,
          reason: "更新 metaso 服务配置",
        }),
      }),
    );
    expect(
      new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get("Idempotency-Key"),
    ).toBeTruthy();
    expect(fetchMock.mock.calls[1]?.[1]).toEqual(
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock.mock.calls[2]?.[1]).toEqual(
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({
          max_generation_count_per_batch: 2,
          max_concurrent_h3_tasks: 1,
          active_storage_provider: "cos",
          confirm: true,
          reason: "更新后台运行参数",
        }),
      }),
    );
    expect(
      new Headers(fetchMock.mock.calls[2]?.[1]?.headers).get("Idempotency-Key"),
    ).toBeTruthy();
  });

  it("downloads read-only control CSV exports through the protected proxy", async () => {
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      () => undefined,
    );
    const createObjectURL = vi.fn(() => "blob:control-export");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers(),
      blob: async () => new Blob(["id\n1"], { type: "text/csv" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    // Older servers can omit export counts; the download still completes and
    // the caller receives an explicitly unknown total instead of a false zero.
    expect(await downloadControlRechargeOrdersCsv()).toBeNull();
    expect(await downloadControlWalletTransactionsCsv()).toBeNull();

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://127.0.0.1:8000/api/control/recharge-orders.csv",
      "http://127.0.0.1:8000/api/control/wallet-transactions.csv",
    ]);
    expect(createObjectURL).toHaveBeenCalledTimes(2);
  });
});
