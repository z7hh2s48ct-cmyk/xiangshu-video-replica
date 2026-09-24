import { waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  cancelLocalPublishLogin,
  canUseLocalPublishAccounts,
  checkLocalPublishLogin,
  focusLocalPublishLogin,
  listLocalPublishAccounts,
  removeLocalPublishAccount,
  startLocalPublishLogin,
} from "./localPublishAccounts";

const request = vi.hoisted(() => vi.fn());
const native = vi.hoisted(() => ({ enabled: false, invoke: vi.fn() }));
vi.mock("../api", () => ({ publishBrowserRequest: request }));
vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => native.enabled,
  invoke: native.invoke,
}));
afterEach(() => {
  request.mockReset();
  native.enabled = false;
  native.invoke.mockReset();
  vi.restoreAllMocks();
});

function desktop(userAgent: string) {
  native.enabled = true;
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue(userAgent);
}

it("Mac 桌面使用云端账号列表与解绑，不调用 Windows 专用命令", async () => {
  desktop("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)");
  request.mockResolvedValue(
    new Response(JSON.stringify([{ id: "cloud-mac" }])),
  );
  expect(canUseLocalPublishAccounts()).toBe(false);
  expect(await listLocalPublishAccounts("owner")).toEqual([
    { id: "cloud-mac" },
  ]);
  await removeLocalPublishAccount("owner", "cloud-mac");
  expect(request).toHaveBeenCalledWith(
    "/api/studio/publish/browser/accounts/cloud-mac",
    expect.objectContaining({ method: "DELETE" }),
  );
  await expect(focusLocalPublishLogin("owner", "login")).rejects.toThrow(
    "Windows 桌面客户端",
  );
  expect(native.invoke).not.toHaveBeenCalled();
});

it.each(["douyin", "wechat_channels", "xiaohongshu"] as const)(
  "Mac 桌面通过云端 %s 扫码流接收二维码、账号与取消结果",
  async (platform) => {
    desktop("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)");
    const stream = new TransformStream<Uint8Array, Uint8Array>();
    const writer = stream.writable.getWriter();
    request.mockImplementation(async (_path, init) =>
      init.method === "DELETE"
        ? new Response("{}")
        : new Response(stream.readable),
    );
    const id = await startLocalPublishLogin("owner", platform);
    await writer.write(
      new TextEncoder().encode(
        '{"phase":"qr_ready","image":"data:image/png;base64,cXI=","account":null,"login_id":"mac-server-login"}\n',
      ),
    );
    await waitFor(async () =>
      expect(await checkLocalPublishLogin("owner", id)).toMatchObject({
        phase: "qr_ready",
        image: "data:image/png;base64,cXI=",
      }),
    );
    await writer.write(
      new TextEncoder().encode(
        '{"phase":"connected","image":null,"account":{"id":"mac-cloud-account"}}\n',
      ),
    );
    await writer.close();
    await waitFor(async () =>
      expect(await checkLocalPublishLogin("owner", id)).toMatchObject({
        phase: "connected",
        account: { id: "mac-cloud-account" },
      }),
    );
    await cancelLocalPublishLogin("owner", id);
    expect(request).toHaveBeenCalledWith(
      "/api/studio/publish/browser/logins",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ platform, account_id: null }),
      }),
    );
    expect(request).toHaveBeenCalledWith(
      "/api/studio/publish/browser/logins/mac-server-login",
      expect.objectContaining({ method: "DELETE" }),
    );
    expect(native.invoke).not.toHaveBeenCalled();
  },
);

it("Windows 桌面保留原生账号与扫码路径", async () => {
  desktop("Mozilla/5.0 (Windows NT 10.0; Win64; x64)");
  native.invoke
    .mockResolvedValueOnce([])
    .mockResolvedValueOnce("windows-login");
  expect(canUseLocalPublishAccounts()).toBe(true);
  expect(await listLocalPublishAccounts("owner")).toEqual([]);
  expect(await startLocalPublishLogin("owner", "douyin")).toBe("windows-login");
  await checkLocalPublishLogin("owner", "windows-login");
  await focusLocalPublishLogin("owner", "windows-login");
  await cancelLocalPublishLogin("owner", "windows-login");
  await removeLocalPublishAccount("owner", "windows-account");
  expect(native.invoke.mock.calls.map(([name]) => name)).toEqual([
    "list_local_publish_accounts",
    "start_local_publish_login",
    "check_local_publish_login",
    "focus_local_publish_login",
    "cancel_local_publish_login",
    "remove_local_publish_account",
  ]);
  expect(request).not.toHaveBeenCalled();
});

it("网页使用云端账号接口，不要求 Tauri", async () => {
  request.mockResolvedValue(new Response(JSON.stringify([{ id: "cloud-1" }])));
  expect(await listLocalPublishAccounts("owner")).toEqual([{ id: "cloud-1" }]);
  expect(request.mock.calls[0][0]).toBe("/api/studio/publish/browser/accounts");
});

it("网页按分块流接收二维码和账号，处理半个 JSON 数据包", async () => {
  const stream = new TransformStream<Uint8Array, Uint8Array>();
  const source = stream.writable.getWriter();
  request.mockResolvedValue(new Response(stream.readable));
  const id = await startLocalPublishLogin("owner", "wechat_channels");
  const encode = new TextEncoder();
  await source.write(
    encode.encode('{"phase":"qr_ready","image":"data:image/png;'),
  );
  await source.write(
    encode.encode('base64,cXI=","account":null,"login_id":"server-1"}\n'),
  );
  await waitFor(async () =>
    expect(await checkLocalPublishLogin("owner", id)).toMatchObject({
      phase: "qr_ready",
    }),
  );
  await source.write(
    encode.encode(
      `${JSON.stringify({
        phase: "connected",
        image: null,
        account: { id: "account-1" },
      })}\n`,
    ),
  );
  await source.close();
  await waitFor(async () =>
    expect(await checkLocalPublishLogin("owner", id)).toMatchObject({
      phase: "connected",
      account: { id: "account-1" },
    }),
  );
  await cancelLocalPublishLogin("owner", id);
});

it("扫码取消先通知服务器，再中止流，且不能访问其他用户会话", async () => {
  const stream = new TransformStream<Uint8Array, Uint8Array>();
  const source = stream.writable.getWriter();
  const aborted = vi.fn();
  request.mockImplementation(async (_path, init) => {
    if (init.method === "DELETE") return new Response("{}");
    init.signal.addEventListener("abort", () => {
      aborted();
      void source.abort(new DOMException("abort", "AbortError"));
    });
    return new Response(stream.readable);
  });
  const id = await startLocalPublishLogin("owner", "douyin");
  await source.write(
    new TextEncoder().encode(
      '{"phase":"loading","image":null,"account":null,"login_id":"server-2"}\n',
    ),
  );
  await waitFor(async () =>
    expect(await checkLocalPublishLogin("owner", id)).toMatchObject({
      phase: "loading",
    }),
  );
  expect(() => checkLocalPublishLogin("other", id)).toThrow("扫码会话不存在");
  await cancelLocalPublishLogin("owner", id);
  expect(aborted).toHaveBeenCalledOnce();
  expect(request).toHaveBeenCalledWith(
    "/api/studio/publish/browser/logins/server-2",
    expect.objectContaining({ method: "DELETE" }),
  );
});

it("扫码服务错误回到可重新获取状态", async () => {
  request.mockRejectedValue(new Error("扫码服务繁忙，请稍后重试。"));
  const id = await startLocalPublishLogin("owner", "xiaohongshu");
  await waitFor(async () =>
    expect(await checkLocalPublishLogin("owner", id)).toMatchObject({
      phase: "closed",
      message: "扫码服务繁忙，请稍后重试。",
    }),
  );
  await cancelLocalPublishLogin("owner", id);
});
