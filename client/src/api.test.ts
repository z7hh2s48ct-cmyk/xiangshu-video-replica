import { webcrypto } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";

const nativeDownload = vi.hoisted(() => ({
  isTauri: vi.fn(() => false),
  invoke: vi.fn(),
  listen: vi.fn(),
}));
vi.mock("@tauri-apps/api/core", () => ({
  isTauri: nativeDownload.isTauri,
  invoke: nativeDownload.invoke,
}));
vi.mock("@tauri-apps/api/event", () => ({ listen: nativeDownload.listen }));

import {
  analysisFailureReference,
  applySavedGenerationPrompt,
  archiveGenerationTask,
  attachCustomerSessionToken,
  CUSTOMER_SESSION_EXPIRED_EVENT,
  CUSTOMER_SESSION_REPLACED_EVENT,
  CUSTOMER_SESSION_REVOKED_EVENT,
  cancelOralTask,
  cancelSourceFrameTask,
  chooseProjectMainCharacterVersion,
  clearMaterialCache,
  compileGenerationPrompt,
  completeMaterialUpload,
  completeVideoUpload,
  confirmOralVoice,
  confirmSourceFrame,
  createGenerationBatch,
  createGenerationResultPreviewUrl,
  createMaterialUploadIntent,
  createOralAvatarClone,
  createOralConsent,
  createOralTask,
  createOralVoiceClone,
  createProject,
  createScriptVersion,
  createVideoUploadIntent,
  createViralCopyExtraction,
  customerVisibleErrorMessage,
  downloadCharacterAsset,
  downloadGenerationResult,
  downloadGenerationTaskResult,
  downloadMaterialAsset,
  evictMaterialCachedPreview,
  extractSourceFrames,
  fetchViralCopy,
  generateFirstFrames,
  getAssetDownloadUrl,
  getCachedCharacterAssetUrl,
  getCharacterReferenceRecommendation,
  getCurrentUser,
  getGenerationBatch,
  getGenerationPriceQuote,
  getGenerationResultDownloadUrl,
  getGenerationRuntimeLimits,
  getHealth,
  getLatestGenerationPrompt,
  getLatestProjectFirstFrames,
  getLatestScriptRewriteTask,
  getLatestScriptVersion,
  getMaterialBatchPreviews,
  getMaterialCachedPreview,
  getMaterialCachedPreviews,
  getMaterialCacheUsage,
  getOralTask,
  getScriptFromAudioTask,
  getSettings,
  hideMaterial,
  listCharacterSceneLooksPage,
  listGenerationBatches,
  listMaterials,
  listOralAvatars,
  listOralTasksPage,
  listOralVoices,
  listProjectCharacterVersions,
  listProjects,
  listSavedGenerationPrompts,
  listSimpleCharacterLibraryPage,
  listViralVideos,
  lockGenerationPrompt,
  publishBrowserRequest,
  putMaterial,
  REQUEST_TIMEOUT_MS,
  readAnalysisPayload,
  readFirstFrameCandidates,
  reconcileUncertainTask,
  regenerateGenerationBatch,
  regenerateGenerationTask,
  resolveApiBaseUrl,
  resolveViralLink,
  retryGenerationTask,
  retryOralTaskArchive,
  reviseGenerationPrompt,
  rewriteProjectScript,
  SESSION_EXPIRED_EVENT,
  saveGenerationPrompt,
  selectCharacterReferences,
  setCustomerSessionToken,
  setInternalAccessToken,
  startVideoAnalysis,
  updateMaterial,
  updateSimpleCharacterProfile,
  uploadMaterial,
  uploadReferenceVideo,
  waitForAnalysisTask,
  waitForCharacterSheetTask,
  waitForFirstFrameTask,
  waitForGenerationReconcileOperation,
  waitForScriptRewriteTask,
  waitForSourceFrameTask,
} from "./api";

describe("素材持久缓存", () => {
  afterEach(() => {
    setCustomerSessionToken(null);
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function cacheFixture() {
    const entries = new Map<string, Response>();
    const cache = {
      keys: async () => [...entries.keys()].map((key) => new Request(key)),
      match: async (key: RequestInfo) =>
        entries.get(typeof key === "string" ? key : key.url)?.clone(),
      put: vi.fn(async (key: RequestInfo, value: Response) => {
        entries.set(typeof key === "string" ? key : key.url, value.clone());
      }),
      delete: async (key: RequestInfo) =>
        entries.delete(typeof key === "string" ? key : key.url),
    };
    const tails = new Map<string, Promise<unknown>>();
    vi.stubGlobal("navigator", {
      locks: {
        request: async (
          name: string,
          options: unknown,
          callback?: () => Promise<unknown>,
        ) => {
          const action = callback ?? (options as () => Promise<unknown>);
          const next = (tails.get(name) ?? Promise.resolve()).then(action);
          tails.set(
            name,
            next.catch(() => undefined),
          );
          return next;
        },
      },
    });
    vi.stubGlobal("caches", { open: vi.fn(async () => cache) });
    vi.stubGlobal("crypto", webcrypto);
    let blobId = 0;
    const create = vi.fn(() => `blob:material-${++blobId}`);
    const revoke = vi.fn();
    const NativeURL = URL;
    vi.stubGlobal(
      "URL",
      class extends NativeURL {
        static createObjectURL = create;
        static revokeObjectURL = revoke;
      },
    );
    const content = new Uint8Array([1, 2, 3, 4]);
    const sha = Array.from(
      new Uint8Array(await webcrypto.subtle.digest("SHA-256", content)),
    )
      .map((value) => value.toString(16).padStart(2, "0"))
      .join("");
    const metadata = {
      id: "asset",
      sha256: sha,
      size_bytes: content.length,
      content_type: "video/mp4",
    };
    const media = vi.fn(
      async () =>
        new Response(content, { headers: { "Content-Type": "video/mp4" } }),
    );
    const fetcher = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).endsWith("/download-url"))
        return Response.json({
          url: "https://media.example/video?secret-signature=private",
        });
      if (String(url).includes("/api/assets/")) return Response.json(metadata);
      return media();
    });
    vi.stubGlobal("fetch", fetcher);
    setCustomerSessionToken("test-cache-session");
    return {
      entries,
      cache,
      create,
      revoke,
      metadata,
      content,
      media,
      fetcher,
    };
  }

  it("首轮仅在线预览，主动填充后每次鉴权并独立释放 Blob URL", async () => {
    const fixture = await cacheFixture();
    const online = await getMaterialCachedPreview("user", "asset");
    expect(online.cached).toBe(false);
    expect(fixture.media).not.toHaveBeenCalled();
    const populated = await getMaterialCachedPreview("user", "asset", {
      populate: true,
    });
    const cached = await getMaterialCachedPreview("user", "asset");
    expect(populated.cached).toBe(true);
    expect(cached.cached).toBe(true);
    expect(cached.url).not.toBe(populated.url);
    expect(fixture.media).toHaveBeenCalledOnce();
    expect(
      fixture.fetcher.mock.calls.filter(([url]) =>
        String(url).endsWith("/download-url"),
      ),
    ).toHaveLength(3);
    expect([...fixture.entries.keys()].join(" ")).not.toMatch(
      /private|test-cache-session|secret-signature/,
    );
    populated.release();
    populated.release();
    cached.release();
    expect(fixture.revoke).toHaveBeenCalledTimes(2);
    expect(await getMaterialCacheUsage("user")).toMatchObject({
      bytes: 4,
      limitBytes: 256 * 1024 * 1024,
      available: true,
    });
    await evictMaterialCachedPreview("user", "asset");
    expect((await getMaterialCacheUsage("user")).bytes).toBe(0);
    await clearMaterialCache("user");
  });

  it("隔离用户、API来源和素材内容版本，清理仅当前用户", async () => {
    const f = await cacheFixture();
    (
      await getMaterialCachedPreview("first", "asset", { populate: true })
    ).release();
    expect((await getMaterialCachedPreview("second", "asset")).cached).toBe(
      false,
    );
    (
      await getMaterialCachedPreview("second", "asset", { populate: true })
    ).release();
    await clearMaterialCache("first");
    expect((await getMaterialCacheUsage("first")).bytes).toBe(0);
    expect((await getMaterialCacheUsage("second")).bytes).toBe(4);
    vi.stubEnv("VITE_API_BASE_URL", "https://other-api.example");
    expect((await getMaterialCachedPreview("second", "asset")).cached).toBe(
      false,
    );
    vi.stubEnv("VITE_API_BASE_URL", "http://127.0.0.1:8000");
    f.metadata.sha256 = "f".repeat(64);
    expect((await getMaterialCachedPreview("second", "asset")).cached).toBe(
      false,
    );
    expect(f.media).toHaveBeenCalledTimes(2);
  });

  it.each([401, 403, 404, 500])(
    "缓存命中时鉴权HTTP %s仍拒绝旧字节",
    async (status) => {
      const f = await cacheFixture();
      (
        await getMaterialCachedPreview("user", "asset", { populate: true })
      ).release();
      f.fetcher.mockResolvedValueOnce(
        Response.json(
          { detail: { code: "FORBIDDEN", message: "denied" } },
          { status },
        ),
      );
      await expect(getMaterialCachedPreview("user", "asset")).rejects.toThrow();
      expect(f.create).toHaveBeenCalledOnce();
    },
  );

  it.each(["sha", "size", "mime", "stream"])(
    "拒绝损坏%s且不存入持久缓存",
    async (failure) => {
      const f = await cacheFixture();
      if (failure === "sha") f.metadata.sha256 = "0".repeat(64);
      if (failure === "size") f.metadata.size_bytes = 5;
      if (failure === "mime")
        f.media.mockResolvedValueOnce(
          new Response(f.content, { headers: { "Content-Type": "text/html" } }),
        );
      if (failure === "stream")
        f.media.mockResolvedValueOnce(
          new Response(new Uint8Array(5), {
            headers: { "Content-Type": "video/mp4" },
          }),
        );
      const result = await getMaterialCachedPreview("user", "asset", {
        populate: true,
      });
      expect(result.cached).toBe(false);
      expect((await getMaterialCacheUsage("user")).bytes).toBe(0);
    },
  );

  it("缓存磁盘内容损坏则删除且回退在线", async () => {
    const f = await cacheFixture();
    (
      await getMaterialCachedPreview("user", "asset", { populate: true })
    ).release();
    const key = [...f.entries.keys()].find((value) =>
      value.includes("/media/"),
    );
    expect(key).toBeDefined();
    f.entries.set(
      key as string,
      new Response(new Uint8Array([9, 9, 9, 9]), {
        headers: { "Content-Type": "video/mp4" },
      }),
    );
    expect((await getMaterialCachedPreview("user", "asset")).cached).toBe(
      false,
    );
    expect((await getMaterialCacheUsage("user")).bytes).toBe(0);
  });

  it("同账号重新登录拒绝旧会话迟到数据", async () => {
    const f = await cacheFixture();
    let resolve!: (response: Response) => void;
    f.media.mockImplementationOnce(
      () =>
        new Promise<Response>((done) => {
          resolve = done;
        }),
    );
    const pending = getMaterialCachedPreview("user", "asset", {
      populate: true,
    });
    const rejected = expect(pending).rejects.toThrow("会话");
    await vi.waitFor(() => expect(f.media).toHaveBeenCalledOnce());
    setCustomerSessionToken("test-cache-session");
    resolve(
      new Response(f.content, { headers: { "Content-Type": "video/mp4" } }),
    );
    await rejected;
    expect((await getMaterialCacheUsage("user")).bytes).toBe(0);
    expect(f.create).not.toHaveBeenCalled();
  });

  it.each(["clear", "evict", "abort"])("%s阻止在途下载写回", async (action) => {
    const f = await cacheFixture();
    const controller = new AbortController();
    let resolve!: (response: Response) => void;
    f.media.mockImplementationOnce(
      () =>
        new Promise<Response>((done) => {
          resolve = done;
        }),
    );
    const pending = getMaterialCachedPreview("user", "asset", {
      populate: true,
      signal: controller.signal,
    });
    const rejected = expect(pending).rejects.toMatchObject({
      name: "AbortError",
    });
    await vi.waitFor(() => expect(f.media).toHaveBeenCalledOnce());
    if (action === "clear") await clearMaterialCache("user");
    else if (action === "evict")
      await evictMaterialCachedPreview("user", "asset");
    else controller.abort();
    resolve(
      new Response(f.content, { headers: { "Content-Type": "video/mp4" } }),
    );
    await rejected;
    expect((await getMaterialCacheUsage("user")).bytes).toBe(0);
  });

  it("同一素材并发填充只下载一次，取消一名调用者不影响另一名", async () => {
    const f = await cacheFixture();
    const controller = new AbortController();
    let resolve!: (response: Response) => void;
    f.media.mockImplementationOnce(
      () =>
        new Promise<Response>((done) => {
          resolve = done;
        }),
    );
    const first = getMaterialCachedPreview("user", "asset", {
      populate: true,
      signal: controller.signal,
    });
    const second = getMaterialCachedPreview("user", "asset", {
      populate: true,
    });
    const rejected = expect(first).rejects.toMatchObject({
      name: "AbortError",
    });
    await vi.waitFor(() =>
      expect(
        f.fetcher.mock.calls.filter(([url]) =>
          String(url).includes("/api/assets/"),
        ),
      ).toHaveLength(4),
    );
    await vi.waitFor(() => expect(f.media).toHaveBeenCalledOnce());
    controller.abort();
    resolve(
      new Response(f.content, { headers: { "Content-Type": "video/mp4" } }),
    );
    await rejected;
    const result = await second;
    expect(result.cached).toBe(true);
    result.release();
    expect(f.media).toHaveBeenCalledOnce();
  });

  it("无WebLocks或CacheStorage时只返回在线预览", async () => {
    const f = await cacheFixture();
    vi.stubGlobal("navigator", {});
    expect(
      (await getMaterialCachedPreview("user", "asset", { populate: true }))
        .cached,
    ).toBe(false);
    expect(await getMaterialCacheUsage("user")).toMatchObject({
      available: false,
      bytes: 0,
    });
    expect(f.media).not.toHaveBeenCalled();
    await clearMaterialCache("user");
  });

  it("配额异常或超50MiB文件回退在线播放", async () => {
    const f = await cacheFixture();
    f.cache.put.mockRejectedValueOnce(
      new DOMException("full", "QuotaExceededError"),
    );
    expect(
      (await getMaterialCachedPreview("user", "asset", { populate: true }))
        .cached,
    ).toBe(false);
    f.metadata.size_bytes = 50 * 1024 * 1024 + 1;
    expect(
      (await getMaterialCachedPreview("user", "asset", { populate: true }))
        .cached,
    ).toBe(false);
    expect(f.media).toHaveBeenCalledOnce();
  });

  it("其他标签更新持久清理代数后，旧填充不可复活", async () => {
    const f = await cacheFixture();
    (
      await getMaterialCachedPreview("user", "asset", { populate: true })
    ).release();
    const key = [...f.entries.keys()].find((value) =>
      value.includes("/media/"),
    ) as string;
    f.entries.delete(key);
    let resolve!: (response: Response) => void;
    f.media.mockImplementationOnce(
      () =>
        new Promise<Response>((done) => {
          resolve = done;
        }),
    );
    const pending = getMaterialCachedPreview("user", "asset", {
      populate: true,
    });
    const rejected = expect(pending).rejects.toMatchObject({
      name: "AbortError",
    });
    await vi.waitFor(() => expect(f.media).toHaveBeenCalledTimes(2));
    const stateKey = `${key.replace("/media/", "/state/").split("/asset/")[0]}/`;
    f.entries.set(
      stateKey,
      new Response(null, {
        headers: { "X-Material-Generation": "other-tab-clear" },
      }),
    );
    resolve(
      new Response(f.content, { headers: { "Content-Type": "video/mp4" } }),
    );
    await rejected;
    expect((await getMaterialCacheUsage("user")).bytes).toBe(0);
  });

  it("授权元数据请求挂起时其他标签清理也使旧请求失效", async () => {
    const f = await cacheFixture();
    (
      await getMaterialCachedPreview("user", "asset", { populate: true })
    ).release();
    const key = [...f.entries.keys()].find((value) =>
      value.includes("/media/"),
    ) as string;
    let resolve!: (response: Response) => void;
    f.fetcher.mockResolvedValueOnce(
      Response.json({ url: "https://media.example/video" }),
    );
    f.fetcher.mockImplementationOnce(
      () =>
        new Promise<Response>((done) => {
          resolve = done;
        }),
    );
    const pending = getMaterialCachedPreview("user", "asset", {
      populate: true,
    });
    const rejected = expect(pending).rejects.toMatchObject({
      name: "AbortError",
    });
    await vi.waitFor(() => expect(resolve).toBeDefined());
    const stateKey = `${key.replace("/media/", "/state/").split("/asset/")[0]}/`;
    f.entries.delete(key);
    f.entries.set(
      stateKey,
      new Response(null, {
        headers: { "X-Material-Generation": "other-tab-clear-before-metadata" },
      }),
    );
    resolve(Response.json(f.metadata));
    await rejected;
    expect(f.media).toHaveBeenCalledOnce();
    expect((await getMaterialCacheUsage("user")).bytes).toBe(0);
  });

  it("清理发生在Cache.put等待期间仍删除迟到写入", async () => {
    const f = await cacheFixture();
    let finishPut!: () => void;
    const putReady = new Promise<void>((resolve) => {
      finishPut = resolve;
    });
    f.cache.put.mockImplementationOnce(async (key, response) => {
      await putReady;
      f.entries.set(typeof key === "string" ? key : key.url, response.clone());
    });
    const pending = getMaterialCachedPreview("user", "asset", {
      populate: true,
    });
    const rejected = expect(pending).rejects.toMatchObject({
      name: "AbortError",
    });
    await vi.waitFor(() => expect(f.cache.put).toHaveBeenCalledOnce());
    const clearing = clearMaterialCache("user");
    finishPut();
    await rejected;
    await clearing;
    expect((await getMaterialCacheUsage("user")).bytes).toBe(0);
  });

  it("最多并发下载2个文件，精准evict不取消其他素材", async () => {
    const f = await cacheFixture();
    const resolvers: ((response: Response) => void)[] = [];
    f.media.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolvers.push(resolve);
        }),
    );
    const first = getMaterialCachedPreview("user", "one", { populate: true });
    const rejected = expect(first).rejects.toMatchObject({
      name: "AbortError",
    });
    const second = getMaterialCachedPreview("user", "two", { populate: true });
    const third = getMaterialCachedPreview("user", "three", { populate: true });
    await vi.waitFor(() => expect(f.media).toHaveBeenCalledTimes(2));
    await evictMaterialCachedPreview("user", "one");
    await rejected;
    await vi.waitFor(() => expect(f.media).toHaveBeenCalledTimes(3));
    for (const resolve of resolvers)
      resolve(
        new Response(f.content, { headers: { "Content-Type": "video/mp4" } }),
      );
    const results = await Promise.all([second, third]);
    expect(results.every((result) => result.cached)).toBe(true);
    for (const result of results) result.release();
    expect((await getMaterialCacheUsage("user")).bytes).toBe(8);
  });

  it("流读取超时会取消读取且不留下缓存", async () => {
    const f = await cacheFixture();
    let expire: (() => void) | undefined;
    const schedule = window.setTimeout.bind(window);
    vi.spyOn(window, "setTimeout").mockImplementation((handler, timeout) => {
      if (timeout === 60_000 && typeof handler === "function")
        expire = () => handler();
      return schedule(handler, timeout) as unknown as ReturnType<
        typeof setTimeout
      >;
    });
    const cancel = vi.fn();
    f.media.mockResolvedValueOnce(
      new Response(new ReadableStream({ cancel }), {
        headers: { "Content-Type": "video/mp4" },
      }),
    );
    const pending = getMaterialCachedPreview("user", "asset", {
      populate: true,
    });
    await vi.waitFor(() => expect(f.media).toHaveBeenCalledOnce());
    // Trigger the configured 60-second download timeout without waiting a minute.
    expect(expire).toBeDefined();
    expire?.();
    expect((await pending).cached).toBe(false);
    expect(cancel).toHaveBeenCalledOnce();
    expect((await getMaterialCacheUsage("user")).bytes).toBe(0);
  });

  it("跨用户总容量按LRU淘汰，清理不删除其他产品缓存键", async () => {
    const f = await cacheFixture();
    (
      await getMaterialCachedPreview("user", "asset", { populate: true })
    ).release();
    const key = [...f.entries.keys()].find((value) =>
      value.includes("/media/"),
    ) as string;
    f.entries.clear();
    for (let index = 0; index < 6; index += 1) {
      const oldKey = key.replace("/user/asset/", `/other/old-${index}/`);
      f.entries.set(
        oldKey,
        new Response(null, {
          headers: {
            "Content-Length": String(50 * 1024 * 1024),
            "X-Material-Accessed": String(index),
          },
        }),
      );
    }
    f.entries.set("https://unrelated.example/other-app", new Response("keep"));
    (
      await getMaterialCachedPreview("user", "asset", { populate: true })
    ).release();
    const oldKeys = [...f.entries.keys()].filter((value) =>
      value.includes("/other/"),
    );
    expect(oldKeys).toHaveLength(5);
    expect(oldKeys.some((value) => value.includes("/old-0/"))).toBe(false);
    await clearMaterialCache("user");
    expect(f.entries.has("https://unrelated.example/other-app")).toBe(true);
    expect((await getMaterialCacheUsage("other")).bytes).toBe(
      250 * 1024 * 1024,
    );
  });
});

describe("批量素材预览授权", () => {
  afterEach(() => {
    setCustomerSessionToken(null);
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function batchFixture(itemOverrides: Record<string, unknown> = {}) {
    const entries = new Map<string, Response>();
    const cache = {
      keys: async () => [...entries.keys()].map((key) => new Request(key)),
      match: async (key: RequestInfo) =>
        entries.get(typeof key === "string" ? key : key.url)?.clone(),
      put: vi.fn(async (key: RequestInfo, value: Response) => {
        entries.set(typeof key === "string" ? key : key.url, value.clone());
      }),
      delete: async (key: RequestInfo) =>
        entries.delete(typeof key === "string" ? key : key.url),
    };
    const tails = new Map<string, Promise<unknown>>();
    vi.stubGlobal("navigator", {
      locks: {
        request: async (
          name: string,
          options: unknown,
          callback?: () => Promise<unknown>,
        ) => {
          const action = callback ?? (options as () => Promise<unknown>);
          const next = (tails.get(name) ?? Promise.resolve()).then(action);
          tails.set(
            name,
            next.catch(() => undefined),
          );
          return next;
        },
      },
    });
    vi.stubGlobal("caches", { open: vi.fn(async () => cache) });
    vi.stubGlobal("crypto", webcrypto);
    let blobId = 0;
    const create = vi.fn(() => `blob:material-${++blobId}`);
    const revoke = vi.fn();
    const NativeURL = URL;
    vi.stubGlobal(
      "URL",
      class extends NativeURL {
        static createObjectURL = create;
        static revokeObjectURL = revoke;
      },
    );
    const content = new Uint8Array([5, 6, 7, 8]);
    const sha = Array.from(
      new Uint8Array(await webcrypto.subtle.digest("SHA-256", content)),
    )
      .map((value) => value.toString(16).padStart(2, "0"))
      .join("");
    const media = vi.fn(
      async () =>
        new Response(content, { headers: { "Content-Type": "image/png" } }),
    );
    const batchCalls: { asset_ids: string[] }[] = [];
    const fetcher = vi.fn(
      async (url: RequestInfo | URL, init?: RequestInit) => {
        if (String(url).endsWith("/api/assets/download-urls")) {
          batchCalls.push(JSON.parse(String(init?.body ?? "{}")));
          return Response.json({
            items: [
              {
                asset_id: "batch-a",
                url: "https://media.example/a?sig=1",
                sha256: sha,
                size_bytes: content.length,
                content_type: "image/png",
                error_code: null,
                ...itemOverrides,
              },
            ],
          });
        }
        return media();
      },
    );
    vi.stubGlobal("fetch", fetcher);
    setCustomerSessionToken("test-batch-session");
    return { batchCalls, create, fetcher, media, revoke, entries, sha };
  }

  it("一次批量授权返回在线预览，不再逐条请求授权与元数据", async () => {
    const f = await batchFixture();
    const results = await getMaterialCachedPreviews("user", [
      { id: "batch-a", populate: false },
    ]);
    expect(Object.keys(results)).toEqual(["batch-a"]);
    expect(results["batch-a"]).toMatchObject({
      url: "https://media.example/a?sig=1",
      cached: false,
    });
    expect(f.batchCalls).toEqual([{ asset_ids: ["batch-a"] }]);
    // 批量通道替代逐瓦片请求：不得再出现单资产授权或元数据往返。
    expect(
      f.fetcher.mock.calls.filter(([url]) =>
        String(url).endsWith("/download-url"),
      ),
    ).toHaveLength(0);
    expect(f.media).not.toHaveBeenCalled();
  });

  it("重复 id 去重为一次批量请求", async () => {
    const f = await batchFixture();
    const results = await getMaterialCachedPreviews("user", [
      { id: "batch-a", populate: false },
      { id: "batch-a", populate: true },
    ]);
    expect(f.batchCalls).toEqual([{ asset_ids: ["batch-a"] }]);
    expect(Object.keys(results)).toEqual(["batch-a"]);
  });

  it("populate 图片写入本机缓存并可在下次命中", async () => {
    const f = await batchFixture();
    const first = await getMaterialCachedPreviews("user", [
      { id: "batch-a", populate: true },
    ]);
    expect(first["batch-a"].cached).toBe(true);
    expect(f.media).toHaveBeenCalledOnce();
    const second = await getMaterialCachedPreviews("user", [
      { id: "batch-a", populate: false },
    ]);
    expect(second["batch-a"].cached).toBe(true);
    first["batch-a"].release();
    second["batch-a"].release();
  });

  it("授权失败的条目不出现在结果中，成功条目不受影响", async () => {
    const f = await batchFixture({ url: null, error_code: "ASSET_NOT_FOUND" });
    const results = await getMaterialCachedPreviews("user", [
      { id: "batch-a", populate: false },
    ]);
    expect(results["batch-a"]).toBeUndefined();
    expect(f.media).not.toHaveBeenCalled();
  });

  it("空入参不发起任何请求", async () => {
    const f = await batchFixture();
    expect(await getMaterialCachedPreviews("user", [])).toEqual({});
    expect(f.fetcher).not.toHaveBeenCalled();
  });

  it("视频条目附带缩略图 URL，无缩略图条目不出现", async () => {
    await batchFixture({
      thumbnail_url: "https://media.example/a-thumb?sig=2",
    });
    const { previews, thumbnails } = await getMaterialBatchPreviews("user", [
      { id: "batch-a", populate: false },
    ]);
    expect(thumbnails["batch-a"]).toBe("https://media.example/a-thumb?sig=2");
    expect(previews["batch-a"]).toMatchObject({
      url: "https://media.example/a?sig=1",
    });
    const plain = await batchFixture();
    expect(plain.batchCalls).toEqual([]);
    const again = await getMaterialBatchPreviews("user", [
      { id: "batch-a", populate: false },
    ]);
    expect(again.thumbnails).toEqual({});
  });

  // 服务端两条授权通道都签发站内相对路径（rbac_routes 的 signed-objects）。
  // 桌面端页面 origin 是 tauri://，与 API origin 必然不同，相对地址会打到
  // 应用自身而不是后端——素材本体与视频缩略图都会退化成空预览框。
  it("批量授权的本体与缩略图地址按 API 地址绝对化", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://studio.example.com/backend");
    await batchFixture({
      url: "/api/assets/signed-objects/a.png?expires=1&sig=1",
      thumbnail_url:
        "/api/assets/signed-objects/a.mp4.thumb.jpg?expires=1&sig=2",
    });
    const { previews, thumbnails } = await getMaterialBatchPreviews("user", [
      { id: "batch-a", populate: false },
    ]);
    expect(previews["batch-a"]).toMatchObject({
      url: "https://studio.example.com/backend/api/assets/signed-objects/a.png?expires=1&sig=1",
    });
    expect(thumbnails["batch-a"]).toBe(
      "https://studio.example.com/backend/api/assets/signed-objects/a.mp4.thumb.jpg?expires=1&sig=2",
    );
  });

  it("中止信号取消批量请求", async () => {
    const f = await batchFixture();
    const controller = new AbortController();
    controller.abort();
    await expect(
      getMaterialCachedPreviews("user", [{ id: "batch-a", populate: false }], {
        signal: controller.signal,
      }),
    ).rejects.toThrow();
    expect(f.batchCalls).toHaveLength(0);
  });
});

describe("下载签名模块级缓存", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function grantFetcher(calls: string[]) {
    return vi.fn(async (url: RequestInfo | URL) => {
      calls.push(String(url));
      if (String(url).endsWith("/download-url"))
        return Response.json({ url: `https://media.example/${calls.length}` });
      throw new Error(`unexpected ${String(url)}`);
    });
  }

  it("12 分钟内复用同一签名，过期后重新授权", async () => {
    vi.useFakeTimers();
    const calls: string[] = [];
    vi.stubGlobal("fetch", grantFetcher(calls));
    setCustomerSessionToken("sig-cache-session");
    const first = await getAssetDownloadUrl("asset-a");
    const second = await getAssetDownloadUrl("asset-a");
    expect(second.url).toBe(first.url);
    expect(calls).toHaveLength(1);
    vi.advanceTimersByTime(13 * 60 * 1000);
    await getAssetDownloadUrl("asset-a");
    expect(calls).toHaveLength(2);
  });

  it("fresh 选项绕过缓存（素材持久缓存通道要求每次新授权）", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", grantFetcher(calls));
    setCustomerSessionToken("sig-cache-session");
    await getAssetDownloadUrl("asset-a");
    await getAssetDownloadUrl("asset-a", { fresh: true });
    expect(calls).toHaveLength(2);
  });

  it("不同资产互不串用", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", grantFetcher(calls));
    setCustomerSessionToken("sig-cache-session");
    await getAssetDownloadUrl("asset-a");
    await getAssetDownloadUrl("asset-b");
    expect(calls).toHaveLength(2);
  });
});

describe("扫码请求会话兼容", () => {
  afterEach(() => {
    setCustomerSessionToken(null);
    vi.unstubAllGlobals();
  });

  it.each(["web-session:qr-fixture", "desktop-qr-fixture"])(
    "%s 保留认证与扫码流取消信号",
    async (token) => {
      setInternalAccessToken(null);
      setCustomerSessionToken(token);
      const fetchMock = vi.fn().mockResolvedValue(new Response("{}"));
      vi.stubGlobal("fetch", fetchMock);
      const controller = new AbortController();
      await publishBrowserRequest("/api/studio/publish/browser/accounts", {
        signal: controller.signal,
      });
      const init = fetchMock.mock.calls[0][1] as RequestInit;
      const headers = new Headers(init.headers);
      expect(headers.get("Authorization")).toBe(`Bearer ${token}`);
      expect(headers.get("X-Customer-Web")).toBe(
        token.startsWith("web-session:") ? "1" : null,
      );
      expect(init.signal).toBe(controller.signal);
      controller.abort();
      expect(init.signal?.aborted).toBe(true);
      expect(init.cache).toBe("no-store");
    },
  );
});

describe("generation download media boundary", () => {
  afterEach(() => {
    nativeDownload.isTauri.mockReturnValue(false);
    vi.unstubAllGlobals();
  });

  function prepareDownload(
    blob: Blob,
    url = "https://provider.example/video.mp4",
  ) {
    nativeDownload.isTauri.mockReturnValue(true);
    nativeDownload.invoke
      .mockReset()
      .mockImplementation(async (command) =>
        command === "choose_video_download"
          ? { download_id: "boundary-1", path: "C:\\Downloads\\video.mp4" }
          : undefined,
      );
    let completed = () => {};
    nativeDownload.listen.mockImplementation(async (_event, callback) => {
      completed = () =>
        callback({
          payload: {
            download_id: "boundary-1",
            success: true,
            path: "C:\\Downloads\\video.mp4",
            error: null,
          },
        });
      return vi.fn();
    });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce({ ok: true, json: async () => ({ url }) })
        .mockResolvedValueOnce({ ok: true, blob: async () => blob }),
    );
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:test"),
      revokeObjectURL: vi.fn(),
    });
    return vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => completed());
  }

  // Only a container-header fixture; these tests do not assert decodability.
  const container = Uint8Array.from([
    0, 0, 0, 24, 102, 116, 121, 112, 105, 115, 111, 109, 0, 0, 0, 0, 105, 115,
    111, 109, 109, 112, 52, 50, 0, 0, 0, 9, 109, 100, 97, 116, 0,
  ]);
  const imageContainer = container.slice();
  for (const offset of [8, 16, 20])
    imageContainer.set([104, 101, 105, 99], offset);

  it.each([
    ["HTTP 200 HTML error", "text/html", "<html>Expired token</html>"],
    ["JSON error", "application/json", '{"error":"expired"}'],
    ["HTML disguised as MP4", "video/mp4", "<html>Expired token</html>"],
    [
      "HTML with generic MIME",
      "application/octet-stream",
      "<html>Expired token</html>",
    ],
    ["empty file", "video/mp4", ""],
    ["truncated file-type box", "video/mp4", container.slice(0, 20)],
    ["unsupported image container", "video/mp4", imageContainer],
  ])(
    "rejects %s without saving it as a video",
    async (_name, mime, content) => {
      const click = prepareDownload(
        new Blob([content], { type: mime as string }),
      );

      await expect(
        downloadGenerationTaskResult("task-boundary", "video.mp4"),
      ).rejects.toThrow("不是有效的 MP4");
      expect(click).not.toHaveBeenCalled();
      expect(nativeDownload.invoke).toHaveBeenCalledWith(
        "cancel_video_download",
        { downloadId: "boundary-1" },
      );
      expect(nativeDownload.invoke).not.toHaveBeenCalledWith(
        "start_video_download",
        expect.anything(),
      );
      expect(URL.createObjectURL).not.toHaveBeenCalled();
    },
  );

  it.each([
    "video/mp4",
    "application/mp4",
    "application/octet-stream",
    "",
    "VIDEO/MP4; charset=binary",
  ])(
    "allows an MP4 container with MIME %s and awaits native save confirmation",
    async (type) => {
      prepareDownload(new Blob([container], { type }));
      await expect(
        downloadGenerationTaskResult("task-boundary", "video.mp4"),
      ).resolves.toMatchObject({ status: "saved" });
      expect(nativeDownload.invoke).not.toHaveBeenCalledWith(
        "cancel_video_download",
        expect.anything(),
      );
      expect(URL.revokeObjectURL).toHaveBeenCalledOnce();
    },
  );

  it("rejects inline base64 that decodes to an error page", async () => {
    prepareDownload(
      new Blob(),
      `data:video/mp4;base64,${btoa("<html>Expired token</html>")}`,
    );
    await expect(
      downloadGenerationTaskResult("task-boundary", "video.mp4"),
    ).rejects.toThrow("内联视频数据无效");
    expect(URL.createObjectURL).not.toHaveBeenCalled();
    expect(nativeDownload.invoke).toHaveBeenCalledWith(
      "cancel_video_download",
      { downloadId: "boundary-1" },
    );
  });
});

describe("爆款列表 API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("将分页游标编码到查询参数并保持 GET 请求", async () => {
    const page = {
      platform: "douyin",
      sort: "latest",
      categories: [],
      items: [],
      hasMore: false,
      nextCursor: null,
      total: 0,
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => page,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      listViralVideos("douyin", "latest", {
        limit: 12,
        cursor: "page/2+=",
      }),
    ).resolves.toEqual(page);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/viral/videos?platform=douyin&sort=latest&limit=12&cursor=page%2F2%2B%3D",
      expect.objectContaining({
        headers: expect.any(Headers),
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it("提交链接和用途到受限时长的解析接口", async () => {
    const body = {
      item: { platform: "douyin", videoId: "source-1" },
      importIdempotencyKey: "stable-import-key",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => body,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      resolveViralLink("https://v.douyin.com/share/", "replica", "resolve-key"),
    ).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/viral/link-resolutions",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          url: "https://v.douyin.com/share/",
          purpose: "replica",
        }),
        signal: expect.any(AbortSignal),
      }),
    );
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe(
      "resolve-key",
    );
  });
});

describe("素材库 API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("使用服务端分页筛选并提交素材管理动作", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ items: [], page: 2, page_size: 6, total: 8 }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await listMaterials({ mediaType: "audio", page: 2, pageSize: 6 });
    await updateMaterial("asset:audio 1", { title: "新名称" });
    await hideMaterial("asset:audio 1");

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/studio/materials?media_type=audio&page=2&page_size=6",
    );
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "http://127.0.0.1:8000/api/studio/materials/asset%3Aaudio%201",
    );
    expect(fetchMock.mock.calls[1]?.[1]).toEqual(
      expect.objectContaining({ method: "PATCH", body: '{"title":"新名称"}' }),
    );
    expect(fetchMock.mock.calls[2]?.[1]).toEqual(
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("通过授权下载地址启动素材下载且不把文件整体读入内存", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ url: "http://127.0.0.1:8000/api/assets/signed" }),
    });
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    vi.stubGlobal("fetch", fetchMock);

    await downloadMaterialAsset("asset 1", "庭院.png");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/assets/asset%201/download-url",
      expect.objectContaining({ method: "POST" }),
    );
    expect(click).toHaveBeenCalledOnce();
    click.mockRestore();
  });
  it.each([
    getAssetDownloadUrl,
    getCachedCharacterAssetUrl,
    // 成片下载/播放走同一个 download-url 端点，绝对化口径必须一致：
    // 生成记录详情页的播放器 src 直接吃这个返回值。
    getGenerationResultDownloadUrl,
  ])(
    "resolves signed media through the configured API proxy",
    async (readUrl) => {
      vi.stubEnv("VITE_API_BASE_URL", "https://studio.example.com/backend");
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue({
          ok: true,
          json: async () => ({
            url: "/api/assets/signed-objects/test.png?expires=1&sig=test",
          }),
        }),
      );
      expect((await readUrl("asset-1")).url).toBe(
        "https://studio.example.com/backend/api/assets/signed-objects/test.png?expires=1&sig=test",
      );
    },
  );

  it("成片在线播放地址按 API 地址绝对化", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://studio.example.com/backend");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          url: "/api/assets/signed-objects/result.mp4?expires=1&sig=test",
        }),
      }),
    );
    expect(await createGenerationResultPreviewUrl("asset-1")).toBe(
      "https://studio.example.com/backend/api/assets/signed-objects/result.mp4?expires=1&sig=test",
    );
  });

  it("按扩展名规范化上传类型并完成素材上传", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          material_id: "asset:image-1",
          asset_id: "image-1",
          storage_key: "materials/user/image-1/original.jpg",
          method: "PUT",
          url: "https://storage.test/upload",
          headers: { "Content-Type": "image/jpeg" },
          expires_at: "2030-01-01T00:00:00Z",
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: "asset:image-1", status: "ready" }),
      });
    vi.stubGlobal("fetch", fetchMock);
    const file = new File(["image"], "房屋.JPEG", {
      type: "application/octet-stream",
    });

    const intent = await createMaterialUploadIntent(file);
    await completeMaterialUpload(intent.asset_id);

    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual(
      expect.objectContaining({
        filename: "房屋.JPEG",
        content_type: "image/jpeg",
        size_bytes: 5,
      }),
    );
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "http://127.0.0.1:8000/api/studio/materials/uploads/image-1/complete",
    );
  });

  it.each([
    ["m4a", "audio/mp4"],
    ["wav", "audio/wav"],
    ["wma", "audio/x-ms-wma"],
    ["wmv", "video/x-ms-wmv"],
    ["aac", "audio/aac"],
    ["flac", "audio/flac"],
    ["ogg", "audio/ogg"],
    ["opus", "audio/ogg"],
    ["aiff", "audio/aiff"],
    ["aif", "audio/aiff"],
    ["amr", "audio/amr"],
  ])("按扩展名规范化 %s 声音样本 MIME", async (extension, contentType) => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ asset_id: "audio-1" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await createMaterialUploadIntent(
      new File(["sample"], `声音.${extension.toUpperCase()}`, {
        type: "application/octet-stream",
      }),
      { audioPurpose: "voice_clone" },
    );
    expect(
      JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body)),
    ).toMatchObject({
      content_type: contentType,
      audio_purpose: "voice_clone",
    });
    expect(
      JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body)),
    ).not.toHaveProperty("duration_seconds");
  });

  it("取消完成素材请求时中止底层 fetch 而不误报超时", async () => {
    let requestSignal: AbortSignal | undefined;
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      requestSignal = init?.signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        requestSignal?.addEventListener("abort", () => {
          reject(new DOMException("Aborted", "AbortError"));
        });
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    const request = completeMaterialUpload("image-1", controller.signal);
    controller.abort();

    await expect(request).rejects.toMatchObject({ name: "AbortError" });
    expect(requestSignal?.aborted).toBe(true);
  });

  it("音频上传意图携带用途与浏览器读取的时长", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        upload_required: true,
        material_id: "asset:audio-1",
        asset_id: "audio-1",
        storage_key: null,
        method: "PUT",
        url: "https://storage.test/audio",
        headers: { "Content-Type": "audio/mpeg" },
        expires_at: "2030-01-01T00:00:00Z",
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await createMaterialUploadIntent(
      new File(["ID3audio"], "完整口播.mp3", { type: "audio/mpeg" }),
      { audioPurpose: "oral_audio", durationSeconds: 42 },
    );

    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual(
      expect.objectContaining({
        audio_purpose: "oral_audio",
        duration_seconds: 42,
      }),
    );
  });

  it("复用带本地鉴权和进度处理的上传通道", async () => {
    class MaterialUploadRequest {
      static latest: MaterialUploadRequest | null = null;
      headers = new Map<string, string>();
      onerror: (() => void) | null = null;
      onload: (() => void) | null = null;
      ontimeout: (() => void) | null = null;
      status = 204;
      timeout = 0;
      upload: { onprogress: ((event: ProgressEvent) => void) | null } = {
        onprogress: null,
      };

      constructor() {
        MaterialUploadRequest.latest = this;
      }
      open() {}
      setRequestHeader(name: string, value: string) {
        this.headers.set(name, value);
      }
      send() {
        this.upload.onprogress?.({
          lengthComputable: true,
          loaded: 3,
          total: 3,
        } as ProgressEvent);
        this.onload?.();
      }
    }
    vi.stubGlobal("XMLHttpRequest", MaterialUploadRequest);
    const progress = vi.fn();

    await uploadMaterial(
      {
        upload_required: true,
        material_id: "asset:audio-1",
        asset_id: "audio-1",
        storage_key: null,
        method: "PUT",
        url: "http://127.0.0.1:8000/api/studio/materials/uploads/audio-1/content",
        headers: { "Content-Type": "audio/mpeg" },
        expires_at: "2030-01-01T00:00:00Z",
      },
      new File(["ID3"], "voice.mp3", { type: "audio/mpeg" }),
      progress,
    );

    expect(progress).toHaveBeenLastCalledWith(100);
    expect(MaterialUploadRequest.latest?.headers.get("Content-Type")).toBe(
      "audio/mpeg",
    );
  });
});

describe("人物库 API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("使用服务端查询和不透明游标读取下一页", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ items: [], next_cursor: "next-cursor" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await listSimpleCharacterLibraryPage({
      limit: 12,
      cursor: "cursor/value",
      query: "林 夏",
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/simple-characters/library?limit=12&cursor=cursor%2Fvalue&query=%E6%9E%97+%E5%A4%8F",
    );
  });

  it("场景造型按人物使用 limit 和 offset 分页", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ items: [], total: 13, limit: 12, offset: 12 }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await listCharacterSceneLooksPage("person 1", { limit: 12, offset: 12 });

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/simple-characters/identities/person%201/scene-looks?limit=12&offset=12",
    );
  });
});

describe("人物 IP 口播资产 API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("按人物读取分身和声音记录", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => [] })
      .mockResolvedValueOnce({ ok: true, json: async () => [] });
    vi.stubGlobal("fetch", fetchMock);

    await listOralAvatars("person 1");
    await listOralVoices("person 1");

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/oral/avatars?identity_id=person%201",
    );
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "http://127.0.0.1:8000/api/oral/voices?identity_id=person%201",
    );
  });

  it("口播任务使用独立 limit 和 offset 历史范围", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ items: [], total: 21, limit: 20, offset: 20 }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await listOralTasksPage({ limit: 20, offset: 20 });

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/oral/tasks?limit=20&offset=20",
    );
  });

  it("口播详情按编码后的原生任务 ID 读取", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "oral 1", status: "SUCCEEDED" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await getOralTask("oral 1");

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/oral/tasks/oral%201",
    );
  });

  it("先存证授权，再带 consent_id 提交分身和声音克隆", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: "consent-avatar" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: "avatar-1", status: "RUNNING" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: "consent-voice" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: "voice-1", status: "RUNNING" }),
      });
    vi.stubGlobal("fetch", fetchMock);

    const avatarConsent = await createOralConsent({
      identityId: "person-1",
      sourceAssetId: "scene-1",
      purpose: "AVATAR",
    });
    await createOralAvatarClone({
      identityId: "person-1",
      title: "庭院讲解分身",
      sourceAssetId: "scene-1",
      sourceKind: "VIDEO",
      consentId: avatarConsent.id,
      idempotencyKey: "avatar-clone-key",
    });
    const voiceConsent = await createOralConsent({
      identityId: "person-1",
      sourceAssetId: "audio-1",
      purpose: "VOICE",
    });
    await createOralVoiceClone({
      identityId: "person-1",
      title: "张工音色",
      sourceAssetId: "audio-1",
      consentId: voiceConsent.id,
      idempotencyKey: "voice-clone-key",
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/oral/consents",
    );
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      identity_id: "person-1",
      source_asset_id: "scene-1",
      purpose: "AVATAR",
    });
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "http://127.0.0.1:8000/api/oral/avatars",
    );
    expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toEqual({
      identity_id: "person-1",
      title: "庭院讲解分身",
      source_asset_id: "scene-1",
      source_kind: "VIDEO",
      consent_id: "consent-avatar",
      idempotency_key: "avatar-clone-key",
    });
    expect(fetchMock.mock.calls[2]?.[0]).toBe(
      "http://127.0.0.1:8000/api/oral/consents",
    );
    expect(fetchMock.mock.calls[3]?.[0]).toBe(
      "http://127.0.0.1:8000/api/oral/voices",
    );
    expect(JSON.parse(String(fetchMock.mock.calls[3]?.[1]?.body))).toEqual({
      identity_id: "person-1",
      title: "张工音色",
      source_asset_id: "audio-1",
      consent_id: "consent-voice",
      idempotency_key: "voice-clone-key",
    });
  });

  it("显式确认 READY 声音", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "voice-1", status: "READY", confirmed: true }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await confirmOralVoice("voice 1");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/oral/voices/voice%201/confirm",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("口播任务按选择序列化供应商原生字幕配置", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "oral-1", status: "QUEUED" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const common = {
      identityId: "person-1",
      avatarId: "avatar-1",
      voiceId: "voice-1",
      mode: "TTS" as const,
      title: "口播",
      scriptText: "文案",
    };

    await createOralTask({ ...common, idempotencyKey: "oral-key-off" });
    await createOralTask({
      ...common,
      subtitle: { st_show: true, st_font_size: 30 },
      idempotencyKey: "oral-key-on",
    });

    const disabledBody = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body));
    const enabledBody = JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body));
    expect(disabledBody.subtitle).toBeNull();
    expect(enabledBody.subtitle).toEqual({ st_show: true, st_font_size: 30 });
  });

  it("调用口播任务取消与归档重试合同", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "oral-1", status: "QUEUED" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await cancelOralTask("oral 1");
    await retryOralTaskArchive("oral 1");

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "http://127.0.0.1:8000/api/oral/tasks/oral%201/cancel",
      "http://127.0.0.1:8000/api/oral/tasks/oral%201/archive-retry",
    ]);
    for (const call of fetchMock.mock.calls) {
      expect(call[1]).toEqual(expect.objectContaining({ method: "POST" }));
    }
  });

  it("保存人物 IP 定位合同", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ identity_id: "person-1" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const profile = {
      display_name: "张工",
      role: "乡墅项目经理",
      service_scope: "建房全流程",
      target_audience: "返乡建房家庭",
      expression_style: "专业直白",
    };

    await updateSimpleCharacterProfile("person-1", profile);

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://127.0.0.1:8000/api/simple-characters/identities/person-1/profile",
    );
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual(
      profile,
    );
  });
});

describe("generation payload readers", () => {
  it("uses owner-scoped saved prompt and external quote endpoints", async () => {
    const version = { id: "saved-1" };
    const quote = {
      resolution: "2K",
      duration_seconds: 15,
      quantity: 4,
      unit_price_fen_per_second: 25,
      estimated_seconds: 60,
      estimated_price_fen: 1500,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => version })
      .mockResolvedValueOnce({ ok: true, json: async () => [version] })
      .mockResolvedValueOnce({ ok: true, json: async () => version })
      .mockResolvedValueOnce({ ok: true, json: async () => quote });
    vi.stubGlobal("fetch", fetchMock);

    await saveGenerationPrompt("project 1", {
      name: "庭院推镜",
      prompt_text: "庭院日景，镜头缓慢推进。",
      base_prompt_version_id: "prompt-1",
    });
    await listSavedGenerationPrompts("project 1");
    await applySavedGenerationPrompt("project 1", "saved 1", "prompt-1");
    await expect(
      getGenerationPriceQuote({
        resolution: "2K",
        duration_seconds: 15,
        quantity: 4,
      }),
    ).resolves.toEqual(quote);

    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://127.0.0.1:8000/api/projects/project%201/saved-prompts/saved%201/apply",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ base_prompt_version_id: "prompt-1" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://127.0.0.1:8000/api/generation/price-quote?resolution=2K&duration_seconds=15&quantity=4",
      expect.any(Object),
    );
  });

  it("reads project appearance and reconstruction metadata compatibly", () => {
    const parsed = readFirstFrameCandidates({
      id: "first-frame-v1",
      project_id: "project-1",
      asset_id: "source-1",
      kind: "first_frame_candidates",
      version_number: 1,
      payload: {
        provider: "fake",
        model: "gpt-image-2",
        prompt: "完整人物重构",
        reconstruction_mode: "full_person_replace.v1",
        character_contract: { body_reconstruction: true },
        project_character_appearance_version_id: "appearance-v1",
        project_appearance: {
          category: "BUSINESS",
          scene: "商务会议室",
          subject: "企业负责人",
          outfit_description: "简洁商务休闲装",
          selection_reason: "按场景自动匹配",
        },
        candidates: [
          {
            asset_id: "first-frame-1",
            storage_key: "projects/project-1/first-frame-1.png",
            storage_uri: "cos://bucket/first-frame-1.png",
            sha256: "hash",
            size_bytes: 123,
            content_type: "image/png",
            quality: {
              passed: true,
              attempt: 2,
              issue_codes: [],
              inspection: { head_only_replacement_detected: false },
            },
          },
        ],
      },
      created_by_user_id: "employee-1",
      created_at: "2030-01-01T00:00:00Z",
    });

    expect(parsed?.reconstruction_mode).toBe("full_person_replace.v1");
    expect(parsed?.project_appearance?.category).toBe("BUSINESS");
    expect(parsed?.candidates[0]?.quality?.passed).toBe(true);
    expect(parsed?.candidates[0]?.quality?.attempt).toBe(2);
  });

  it("reads action-beat metadata while keeping the shots compatibility field", () => {
    const parsed = readAnalysisPayload({
      id: "analysis-v1",
      project_id: "project-1",
      asset_id: "video-1",
      kind: "analysis",
      version_number: 1,
      payload: {
        analysis: {
          summary: "连续镜头动作拆解",
          duration_seconds: 12,
          original_script: "",
          shots: [
            {
              shot_id: "S01",
              start_time: 0,
              end_time: 12,
              shot_type: "中景",
              composition: "人物居中",
              camera_motion: "固定",
              subject: "主讲人",
              action: "口播",
              scene: "室内",
              spoken_text: "",
              transition: "连续",
              segment_kind: "ACTION_BEAT",
              boundary_reason: "表达重点变化",
            },
          ],
        },
      },
      created_by_user_id: "employee-1",
      created_at: "2030-01-01T00:00:00Z",
    });

    expect(parsed?.shots[0]?.segment_kind).toBe("ACTION_BEAT");
    expect(parsed?.shots[0]?.boundary_reason).toBe("表达重点变化");
  });

  it("falls back to shot spoken text when original_script is an empty string", () => {
    // 已落库的空串拆解版本：空串不能直用，否则爆款复刻页文案栏空白。
    const parsed = readAnalysisPayload({
      id: "analysis-v1",
      project_id: "project-1",
      asset_id: "video-1",
      kind: "analysis",
      version_number: 1,
      payload: {
        analysis: {
          summary: "空原文但分段台词齐全",
          duration_seconds: 12,
          original_script: "",
          shots: [
            {
              shot_id: "S01",
              start_time: 0,
              end_time: 6,
              shot_type: "中景",
              composition: "人物居中",
              camera_motion: "固定",
              subject: "主讲人",
              action: "口播",
              scene: "室内",
              spoken_text: "这栋房子的采光设计非常好",
              transition: "连续",
              segment_kind: "ACTION_BEAT",
              boundary_reason: "表达重点变化",
            },
            {
              shot_id: "S02",
              start_time: 6,
              end_time: 12,
              shot_type: "近景",
              composition: "人物偏右",
              camera_motion: "固定",
              subject: "主讲人",
              action: "口播",
              scene: "室内",
              spoken_text: "下午三点还有阳光",
              transition: "连续",
              segment_kind: "ACTION_BEAT",
              boundary_reason: "表达重点变化",
            },
          ],
        },
      },
      created_by_user_id: "employee-1",
      created_at: "2030-01-01T00:00:00Z",
    });

    expect(parsed?.original_script).toBe(
      "这栋房子的采光设计非常好下午三点还有阳光",
    );
  });

  it("keeps a whitespace-only original_script from being displayed verbatim", () => {
    const parsed = readAnalysisPayload({
      id: "analysis-v1",
      project_id: "project-1",
      asset_id: "video-1",
      kind: "analysis",
      version_number: 1,
      payload: {
        analysis: {
          summary: "空白原文",
          duration_seconds: 12,
          original_script: "   ",
          shots: [
            {
              shot_id: "S01",
              start_time: 0,
              end_time: 12,
              shot_type: "中景",
              composition: "人物居中",
              camera_motion: "固定",
              subject: "主讲人",
              action: "口播",
              scene: "室内",
              spoken_text: "回填后的文案",
              transition: "连续",
            },
          ],
        },
      },
      created_by_user_id: "employee-1",
      created_at: "2030-01-01T00:00:00Z",
    });

    expect(parsed?.original_script).toBe("回填后的文案");
  });
});

describe("API base URL resolution", () => {
  it("uses the serving HTTPS origin for a production web build", () => {
    expect(
      resolveApiBaseUrl(undefined, true, {
        origin: "https://video.example.com",
        protocol: "https:",
      }),
    ).toBe("https://video.example.com");
  });

  it("throws an error when no API base URL is configured (CW-015: remove loopback fallback)", () => {
    // CW-015: 正式客户构建必须有唯一地址来源，缺地址时 fail-closed
    expect(() =>
      resolveApiBaseUrl(undefined, true, {
        origin: "tauri://localhost",
        protocol: "tauri:",
      }),
    ).toThrow("API base URL is required");
    expect(() =>
      resolveApiBaseUrl(undefined, false, {
        origin: "http://127.0.0.1:5173",
        protocol: "http:",
      }),
    ).toThrow("API base URL is required");
  });

  it("fails closed at runtime when VITE_API_BASE_URL is missing (CW-015 n1: integration path)", async () => {
    // n1: 覆盖“运行时 env 缺失 → 真实 API 调用抛错”的端到端 fail-closed 路径。
    // 上面的用例直接调用 resolveApiBaseUrl；这里经 getHealth() → requestJson()
    // → apiBaseUrl() 的真实调用链验证：客户构建缺唯一地址来源时，业务请求在发出
    // 前就抛错，而不是静默回退到 loopback origin。全局 beforeEach 已 stub 地址，
    // 这里临时置空以进入缺地址路径。
    vi.stubEnv("VITE_API_BASE_URL", "");
    await expect(getHealth()).rejects.toThrow("API base URL is required");
  });

  it("prefers and normalizes an explicitly configured API origin", () => {
    expect(
      resolveApiBaseUrl(" https://api.example.com/ ", true, {
        origin: "https://video.example.com",
        protocol: "https:",
      }),
    ).toBe("https://api.example.com");
  });
});

describe("customer-visible service errors", () => {
  it.each([
    [
      "RESULT_ARCHIVE_IN_PROGRESS",
      "成片正在保存，请稍后刷新任务核对；不会重新生成或扣费。",
    ],
    [
      "RESULT_ARCHIVE_LEASE_LOST",
      "本次保存已中断，请刷新任务核对后再试；不会重新生成或扣费。",
    ],
  ])(
    "archive conflict %s explains the safe next step",
    async (code, message) => {
      const fetchMock = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: { code, message: "backend archive conflict" },
          }),
          {
            status: 409,
            headers: { "Content-Type": "application/json" },
          },
        ),
      );
      vi.stubGlobal("fetch", fetchMock);
      await expect(
        archiveGenerationTask("archive-test-task"),
      ).rejects.toMatchObject({
        message,
        code,
        status: 409,
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);
    },
  );

  it("cloud frame validation explains how to repair the input", () => {
    expect(
      customerVisibleErrorMessage({
        code: "H3_REQUIRES_CLOUD_STORAGE",
        message: "METASO H3 requires an HTTPS first-frame URL",
      }),
    ).toBe(
      "所选素材尚未存入当前云端素材库。请选择已归档的素材，或联系管理员完成云端存储配置后重新上传。",
    );
  });
  it("preserves actionable analysis storage guidance before generic provider branding", () => {
    expect(
      customerVisibleErrorMessage({
        code: "ANALYSIS_VIDEO_URL_UNAVAILABLE",
        message: "请在设置中切换至腾讯云 COS 后重新上传。",
      }),
    ).toBe(
      "当前视频尚未就绪，无法交给云端分析。请联系管理员配置云端素材存储，再重新上传视频。",
    );
  });
  it.each([
    [
      { code: "METASO_UPSTREAM_TIMEOUT", message: "MiniMax H3 timeout" },
      "视频生成服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
    ],
    [
      { code: "APILIO_REQUEST_FAILED", message: "Gemini unavailable" },
      "视频拆解服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
    ],
    [
      {
        code: "APILIO_SETTINGS_UNAVAILABLE",
        message: "生成人物置换首帧失败：Apilio key missing",
      },
      "首帧生成服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
    ],
    [
      { code: "IMAGE_FAILED", message: "GPT Image returned no output" },
      "首帧生成服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
    ],
    [
      { code: "COS_UPLOAD_FAILED", message: "腾讯云 COS denied" },
      "素材库暂时不可用，请稍后重试；如持续失败，请联系客服。",
    ],
    [
      { code: "SCRIPT_FAILED", message: "DeepSeek timeout" },
      "文案优化服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
    ],
    [
      { code: "ZPAY_UNAVAILABLE", message: "gateway rejected" },
      "在线支付暂时不可用，请稍后重试；如已扣款，请勿重复支付并联系客服。",
    ],
    // 上线前检查 P2-4：补齐缺口的四个供应商 pattern（纵深防御——服务端
    // 文案当前全中性，此处保证未来任何一处带供应商名的报错也被拦下）。
    [
      { code: "ORAL_TASK_FAILED", message: "hifly render error" },
      "数字人口播服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
    ],
    [
      { code: "VIRAL_FETCH_FAILED", message: "TikHub upstream timeout" },
      "爆款数据服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
    ],
    [
      { code: "ASR_FAILED", message: "DashScope quota exceeded" },
      "音频转写服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
    ],
    [
      { code: "LINK_RESOLUTION_FAILED", message: "douyidou解析超时" },
      "链接解析服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
    ],
  ])("maps a branded provider failure to neutral copy", (error, expected) => {
    expect(customerVisibleErrorMessage(error)).toBe(expected);
  });

  it("keeps actionable non-branded errors and adds request ids only after mapping", () => {
    expect(
      customerVisibleErrorMessage({
        code: "METASO_FAILED",
        message: "upstream failed",
        requestId: "request-123",
      }),
    ).toBe(
      "视频生成服务暂时不可用，请稍后重试；如持续失败，请联系客服。 问题编号：request-123",
    );
    expect(customerVisibleErrorMessage("参考视频时长必须为 4–15 秒")).toBe(
      "参考视频时长必须为 4–15 秒",
    );
  });

  it("turns transport failures into the caller's customer-safe fallback", () => {
    expect(
      customerVisibleErrorMessage(
        new TypeError("Failed to fetch"),
        "项目列表暂不可用，请检查网络连接后重试。",
      ),
    ).toBe("项目列表暂不可用，请检查网络连接后重试。");
  });

  it.each([
    [
      "ACTIVATION_UNAVAILABLE",
      "该激活码当前无法使用，请确认激活码仍在有效期内。",
    ],
    [
      "PAIRING_UNAVAILABLE",
      "该激活码当前无法用于设备配对，请联系服务人员处理。",
    ],
    [
      "SUB_ACCOUNT_QUOTA_EXCEEDED",
      "本月额度已用完，请联系母账号调整额度后重试。",
    ],
  ])("localizes customer account error %s", (code, expected) => {
    expect(
      customerVisibleErrorMessage({
        code,
        message: "The activation code cannot be used.",
      }),
    ).toBe(expected);
  });
});

describe("customer workspace session lifecycle", () => {
  const customerSessionText = "customer-session-fixture";

  afterEach(() => {
    setCustomerSessionToken(null);
    vi.unstubAllGlobals();
  });

  it.each([
    ["SESSION_REPLACED", CUSTOMER_SESSION_REPLACED_EVENT],
    ["DEVICE_REVOKED", CUSTOMER_SESSION_REVOKED_EVENT],
  ])(
    "dispatches the exact %s lifecycle event for shared workspace requests",
    async (code, eventName) => {
      const detail = { code, message: "session unavailable" };
      const response = () => ({
        ok: false,
        status: 401,
        headers: new Headers({ "X-Request-Id": "request-lifecycle-1" }),
        json: vi.fn().mockResolvedValue({ detail }),
      });
      const fetchResponse = {
        ...response(),
        clone: vi.fn(() => response()),
      };
      const listener = vi.fn();
      window.addEventListener(eventName, listener, { once: true });
      setCustomerSessionToken(customerSessionText);
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(fetchResponse));

      await expect(listProjects()).rejects.toThrow();

      expect(listener).toHaveBeenCalledTimes(1);
      window.removeEventListener(eventName, listener);
    },
  );

  it("ignores an older workspace 401 whose body finishes after a new session attaches", async () => {
    let resolveErrorBody:
      | ((value: { detail: { code: string; message: string } }) => void)
      | undefined;
    const delayedErrorBody = new Promise<{
      detail: { code: string; message: string };
    }>((resolve) => {
      resolveErrorBody = resolve;
    });
    const cloneJson = vi.fn(() => delayedErrorBody);
    const fetchResponse = {
      ok: false,
      status: 401,
      headers: new Headers({ "X-Request-Id": "request-delayed-401" }),
      json: vi.fn().mockResolvedValue({
        detail: { code: "SESSION_EXPIRED", message: "older session expired" },
      }),
      clone: vi.fn(() => ({
        ok: false,
        status: 401,
        headers: new Headers({ "X-Request-Id": "request-delayed-401" }),
        json: cloneJson,
      })),
    };
    const listener = vi.fn();
    window.addEventListener(CUSTOMER_SESSION_EXPIRED_EVENT, listener);
    const releaseOlder = attachCustomerSessionToken("older-session");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(fetchResponse));

    const pendingRequest = listProjects();
    await vi.waitFor(() => expect(cloneJson).toHaveBeenCalledTimes(1));
    const releaseCurrent = attachCustomerSessionToken("current-session");
    resolveErrorBody?.({
      detail: { code: "SESSION_EXPIRED", message: "older session expired" },
    });

    await expect(pendingRequest).rejects.toThrow();
    expect(listener).not.toHaveBeenCalled();

    releaseOlder();
    releaseCurrent();
    window.removeEventListener(CUSTOMER_SESSION_EXPIRED_EVENT, listener);
  });

  it("does not expire a newly attached session for a request sent during the credential handoff", async () => {
    let finishRequest: ((response: Response) => void) | undefined;
    const response = new Promise<Response>((resolve) => {
      finishRequest = resolve;
    });
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) => response);
    vi.stubGlobal("fetch", fetchMock);
    setCustomerSessionToken(null);
    setInternalAccessToken(null);
    const listener = vi.fn();
    window.addEventListener(CUSTOMER_SESSION_EXPIRED_EVENT, listener);
    const pendingRequest = listProjects();
    const failure = expect(pendingRequest).rejects.toThrow();
    expect(
      new Headers(fetchMock.mock.calls[0]?.[1]?.headers).has("Authorization"),
    ).toBe(false);
    const releaseCurrent = attachCustomerSessionToken(customerSessionText);
    try {
      finishRequest?.(
        new Response(
          JSON.stringify({
            detail: { code: "SESSION_EXPIRED", message: "missing session" },
          }),
          { status: 401, headers: { "Content-Type": "application/json" } },
        ),
      );
      await failure;
      expect(listener).not.toHaveBeenCalled();
    } finally {
      releaseCurrent();
      window.removeEventListener(CUSTOMER_SESSION_EXPIRED_EVENT, listener);
    }
  });
});

const generationVersion = {
  id: "version-1",
  project_id: "project-1",
  asset_id: null,
  kind: "script",
  version_number: 1,
  payload: {},
  created_by_user_id: "employee_1",
  created_at: "2030-01-01T00:00:00Z",
};

describe("generation workflow API", () => {
  afterEach(() => {
    setCustomerSessionToken(null);
    setInternalAccessToken(null);
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("archives an existing direct result and starts an HTTP download without reading MP4 bytes", async () => {
    setCustomerSessionToken("test-customer-session");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ result_asset_id: "asset 1" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ url: "https://signed.example/result.mp4" }),
      });
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        expect(this.href).toBe("https://signed.example/result.mp4");
        expect(this.download).toBe("direct-task.mp4");
      });
    vi.stubGlobal("fetch", fetchMock);
    const result = await downloadGenerationTaskResult(
      "task 1",
      "direct-task.mp4",
    );
    expect(result).toEqual({ status: "started" });
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "http://127.0.0.1:8000/api/generation-tasks/task%201/archive",
      "http://127.0.0.1:8000/api/assets/asset%201/download-url",
    ]);
    for (const call of fetchMock.mock.calls) {
      expect(call[1].method).toBe("POST");
      expect(new Headers(call[1].headers).get("Authorization")).toBe(
        "Bearer test-customer-session",
      );
    }
    expect(click).toHaveBeenCalledOnce();
  });

  it.each([403, 404, 409, 503])(
    "does not download or resubmit generation when archive returns %i",
    async (status) => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: false,
        status,
        json: async () => ({ detail: { code: "RESULT_NOT_AVAILABLE" } }),
      });
      const click = vi
        .spyOn(HTMLAnchorElement.prototype, "click")
        .mockImplementation(() => undefined);
      vi.stubGlobal("fetch", fetchMock);
      await expect(
        downloadGenerationTaskResult("task-other", "video.mp4"),
      ).rejects.toThrow();
      expect(fetchMock).toHaveBeenCalledOnce();
      expect(click).not.toHaveBeenCalled();
    },
  );

  it("does not claim download started before archive provides an asset", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ result_asset_id: null }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(
      downloadGenerationTaskResult("task-pending", "video.mp4"),
    ).rejects.toThrow("成片尚未保存完成");
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("starts a signed result download without a second fetch or an in-memory blob", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ url: "https://signed.example/result.mp4" }),
    });
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        expect(this.href).toBe("https://signed.example/result.mp4");
        expect(this.download).toBe("task-1.mp4");
      });
    vi.stubGlobal("fetch", fetchMock);
    await expect(
      downloadGenerationResult("asset 1", "task-1.mp4"),
    ).resolves.toEqual({ status: "started" });
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/assets/asset%201/download-url",
    );
    expect(click).toHaveBeenCalledOnce();
  });

  it("rejects a missing download URL instead of navigating to the current page", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ url: "" }) }),
    );
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    await expect(
      downloadGenerationResult("asset 1", "video.mp4"),
    ).rejects.toThrow("下载链接");
    expect(click).not.toHaveBeenCalled();
  });

  it("uses the local character cache for previews and manual downloads", async () => {
    vi.useFakeTimers();
    const characterBlob = new Blob(["character"], { type: "image/png" });
    const cachedUrl =
      "http://127.0.0.1:8000/api/assets/character-cache/cache.png?expires=1&sig=test";
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ url: cachedUrl }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ url: cachedUrl }),
      })
      .mockResolvedValueOnce({
        ok: true,
        blob: async () => characterBlob,
      });
    const createObjectUrl = vi.fn(() => "blob:character");
    const revokeObjectUrl = vi.fn();
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", {
      createObjectURL: createObjectUrl,
      revokeObjectURL: revokeObjectUrl,
    });

    await expect(getCachedCharacterAssetUrl("asset 1")).resolves.toEqual({
      url: cachedUrl,
    });
    await downloadCharacterAsset("asset 1", "人物.png");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8000/api/assets/asset%201/cached-url",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8000/api/assets/asset%201/cached-url",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      cachedUrl,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(createObjectUrl).toHaveBeenCalledWith(characterBlob);
    expect(anchorClick).toHaveBeenCalledOnce();

    await vi.advanceTimersByTimeAsync(1_000);
    expect(revokeObjectUrl).toHaveBeenCalledWith("blob:character");
  });

  it("returns the signed streaming url directly for in-player preview", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: true,
      json: async () => ({ url: "https://signed.example/preview.mp4" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(createGenerationResultPreviewUrl("asset 1")).resolves.toBe(
      "https://signed.example/preview.mp4",
    );

    // 预签名 URL 直连 video src：只签发地址，不再二次拉取 blob。
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/assets/asset%201/download-url",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("rejects a preview response without a signed url", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: true,
      json: async () => ({}),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(createGenerationResultPreviewUrl("asset 1")).rejects.toThrow(
      "预览链接获取失败，请重试。",
    );
  });

  it("covers script, prompt, runtime, batch, retry and result download routes", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => generationVersion })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          version: generationVersion,
          stale: false,
          stale_reasons: [],
        }),
      })
      .mockResolvedValueOnce({ ok: true, json: async () => generationVersion })
      .mockResolvedValueOnce({ ok: true, json: async () => generationVersion })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          version: generationVersion,
          stale: false,
          stale_reasons: [],
        }),
      })
      .mockResolvedValueOnce({ ok: true, json: async () => generationVersion })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          min_quantity: 1,
          max_quantity: 4,
          estimated_cost_per_task: null,
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          id: "batch-1",
          project_id: "project-1",
          prompt_version_id: "prompt-1",
          status: "QUEUED",
          quantity: 2,
          stale: false,
          progress: {
            total_count: 2,
            terminal_count: 0,
            progress_percent: 0,
            counts: {},
          },
          tasks: [],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ status: "accepted" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ url: "https://download.example/result.mp4" }),
      });
    vi.stubGlobal("fetch", fetchMock);

    await createScriptVersion("project 1", {
      source: "custom",
      text: "口播稿",
      shot_card_version_id: "shot-1",
    });
    await getLatestScriptVersion("project 1");
    await compileGenerationPrompt("project 1", {
      script_version_id: "script-1",
      shot_card_version_id: "shot-1",
      first_frame_asset_id: "frame-1",
      output_duration_seconds: 10,
      resolution: "768P",
      ratio: "adaptive",
    });
    await reviseGenerationPrompt("project 1", {
      base_prompt_version_id: "prompt-1",
      prompt_text: "修订 Prompt",
    });
    await getLatestGenerationPrompt("project 1");
    await lockGenerationPrompt("project 1", "prompt 1");
    await getGenerationRuntimeLimits();
    await createGenerationBatch("project 1", {
      quantity: 2,
      prompt_version_id: "prompt-1",
      first_frame_asset_id: "frame-1",
      output_duration_seconds: 10,
      resolution: "768P",
      ratio: "adaptive",
      idempotency_key: "key-1",
      provider: "fake_h3",
      fake_audio_quality: "ok",
    });
    await retryGenerationTask("task 1", {
      idempotency_key: "retry-key-1",
      retry_reason: "重新归档",
    });
    await getGenerationResultDownloadUrl("asset 1");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8000/api/projects/project%201/scripts",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          source: "custom",
          text: "口播稿",
          shot_card_version_id: "shot-1",
        }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://127.0.0.1:8000/api/projects/project%201/prompts/revise",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      6,
      "http://127.0.0.1:8000/api/projects/project%201/prompts/prompt%201/lock",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      8,
      "http://127.0.0.1:8000/api/projects/project%201/generation-batches",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      9,
      "http://127.0.0.1:8000/api/generation-tasks/task%201/retry",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          idempotency_key: "retry-key-1",
          retry_reason: "重新归档",
        }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      10,
      "http://127.0.0.1:8000/api/assets/asset%201/download-url",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("posts wallet-backed regeneration contracts for batches and tasks", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "replacement-batch" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const input = {
      idempotency_key: "paid-regeneration-key",
      estimated_cost_snapshot: 2.5,
      generation_reason: "人工确认重新生成",
    };

    await regenerateGenerationBatch("batch 1", input);
    await regenerateGenerationTask("task 1", input);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8000/api/generation-batches/batch%201/regenerate",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(input),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8000/api/generation-tasks/task%201/regenerate",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(input),
      }),
    );
  });

  it.each([
    [401, "登录已失效，请重新进入工作台"],
    [403, "当前账号无权执行此操作"],
    [409, "上游内容已变化，请重新确认后再试"],
    [
      422,
      "保存口播稿失败：参数校验未通过，请核对文案、首帧、时长和画幅（HTTP 422）",
    ],
    [429, "请求过于频繁，请稍后重试"],
    [500, "生成服务暂不可用，请稍后重试"],
  ])("maps generation HTTP %s to a Chinese error", async (status, message) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status,
        json: async () => ({}),
      }),
    );

    await expect(
      createScriptVersion("project-1", {
        source: "custom",
        text: "口播稿",
        shot_card_version_id: "shot-1",
      }),
    ).rejects.toThrow(message);
  });

  it("配额超限的 403 保留业务代码，可翻译为额度提示而非泛化的无权操作", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 403,
        json: async () => ({
          detail: {
            code: "SUB_ACCOUNT_QUOTA_EXCEEDED",
            message:
              "本月额度已用完（已用 8 / 限额 10 积分），请联系母账号调整额度。",
          },
        }),
      }),
    );
    const error = await createScriptVersion("project-1", {
      source: "custom",
      text: "口播稿",
      shot_card_version_id: "shot-1",
    }).catch((cause: Error) => cause);
    expect(error).toBeInstanceOf(Error);
    expect((error as { code?: string }).code).toBe(
      "SUB_ACCOUNT_QUOTA_EXCEEDED",
    );
    expect(customerVisibleErrorMessage(error)).toBe(
      "本月额度已用完，请联系母账号调整额度后重试。",
    );
  });

  it("参考时长拒绝保留可执行的修复提示", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: async () => ({
          detail: {
            code: "INDEPENDENT_REFERENCE_DURATION_INVALID",
            message: "internal detail",
          },
        }),
      }),
    );
    await expect(
      createScriptVersion("project-1", {
        source: "custom",
        text: "口播稿",
        shot_card_version_id: "shot-1",
      }),
    ).rejects.toThrow(
      "参考视频/音频每段须为2–15秒；缺少时长的历史素材请重新上传后选取。",
    );
  });

  it("参数校验错误指出具体字段和约束，不泄露提交内容", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        headers: new Headers({ "X-Request-Id": "request-invalid-input" }),
        json: async () => ({
          detail: [
            {
              loc: ["body", "opening_action"],
              type: "string_too_long",
              ctx: { max_length: 1000 },
              input: "private script",
            },
            {
              loc: ["body", "ratio"],
              type: "literal_error",
              input: "private value",
            },
          ],
        }),
      }),
    );
    const error = await createScriptVersion("project-1", {
      source: "custom",
      text: "口播稿",
      shot_card_version_id: "shot-1",
    }).catch((cause: Error) => cause);
    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).toContain("开场衔接：最多 1000 个字符");
    expect((error as Error).message).toContain("画幅：请选择支持的选项");
    expect((error as Error).message).toContain("request-invalid-input");
    expect((error as Error).message).not.toContain("private");
  });

  it("保留业务参数拒绝的具体原因及错误编号", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: async () => ({
          detail: {
            code: "SCRIPT_TEXT_REQUIRED",
            message: "口播文案不能为空，请填写后重新确认。",
          },
        }),
      }),
    );
    await expect(
      createScriptVersion("project-1", {
        source: "custom",
        text: "",
        shot_card_version_id: "shot-1",
      }),
    ).rejects.toThrow("口播文案不能为空，请填写后重新确认。");
  });

  it("maps generation timeout and offline failures to Chinese errors", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new DOMException("aborted", "AbortError"))
      .mockRejectedValueOnce(new TypeError("Failed to fetch"));
    vi.stubGlobal("fetch", fetchMock);
    const input = {
      source: "custom" as const,
      text: "口播稿",
      shot_card_version_id: "shot-1",
    };

    await expect(createScriptVersion("project-1", input)).rejects.toThrow(
      "保存口播稿失败：请求超时，请重试",
    );
    const offlineError = await createScriptVersion("project-1", input).catch(
      (caught: unknown) => caught,
    );
    // CW-018: symmetric guard with the analysis-side test — lock the corrected
    // cloud wording AND assert the misleading local-service prompt is gone.
    expect((offlineError as Error).message).toBe(
      "保存口播稿失败：网络连接失败，请检查网络后重试",
    );
    expect((offlineError as Error).message).not.toMatch(/本地服务/);
  });

  it("preserves the server error code on generation failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: async () => ({
          detail: {
            code: "PROMPT_STALE",
            message: "Upstream inputs changed.",
          },
        }),
      }),
    );

    const error = await createGenerationBatch("project-1", {
      quantity: 1,
      prompt_version_id: "prompt-1",
      first_frame_asset_id: "frame-1",
      output_duration_seconds: 10,
      resolution: "768P",
      ratio: "adaptive",
      idempotency_key: "key-1",
      provider: "fake_h3",
      fake_audio_quality: "ok",
    }).catch((requestError: unknown) => requestError);

    expect(error).toMatchObject({
      status: 409,
      code: "PROMPT_STALE",
      // 送生成的 PROMPT_STALE 也改成可行动文案：不再丢出“上游内容已变化”。
      message:
        "上游（拆解、分镜、文案或首帧）已更新，最终提示词已失效：请重新合成后再继续。",
    });
  });

  // 合成最终提示词的 409 各自对应一个不同的自救动作（补开场衔接、勾压缩、改文案、
  // 重存分镜）。统一兜底成"上游内容已变化"会把服务端已经给出的可行动原因丢掉，
  // 用户既不知道哪一步出错，也找不到修正入口。
  it.each([
    ["FIRST_FRAME_ALIGNMENT_REQUIRED", /开场衔接/],
    ["TIMELINE_CONFIRMATION_REQUIRED", /压缩/],
    ["SCRIPT_TAG_INVALID", /纯文本/],
    ["DIALOGUE_MISMATCH", /台词/],
    ["SCRIPT_STALE", /文案/],
    ["SHOT_CARD_STALE", /分镜/],
    ["SCRIPT_SHOT_CARD_MISMATCH", /分镜/],
    ["FIRST_FRAME_CONFIRMATION_REQUIRED", /首帧/],
    ["SHOT_CARD_TIMELINE_INVALID", /拆解/],
    ["FIRST_FRAME_QUALITY_NOT_VERIFIED", /首帧/],
  ])(
    "explains compile conflict %s instead of the generic 409 copy",
    async (code, pattern) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue({
          ok: false,
          status: 409,
          json: async () => ({ detail: { code, message: "server detail" } }),
        }),
      );

      const error = await compileGenerationPrompt("project-1", {
        script_version_id: "script-1",
        shot_card_version_id: "shot-1",
        first_frame_asset_id: "frame-1",
        output_duration_seconds: 15,
        resolution: "768P",
        ratio: "adaptive",
      }).catch((requestError: unknown) => requestError);

      expect(error).toMatchObject({ status: 409, code });
      expect((error as Error).message).toMatch(pattern);
      expect((error as Error).message).not.toBe(
        "上游内容已变化，请重新确认后再试",
      );
    },
  );

  // 服务端在 409 详情里带上 stale_reasons（到底是哪一环变旧）。只报“上游内容已变化”
  // 等于没说：用户不知道该回哪一步，这里必须把原因翻成环节名和下一步动作。
  // 动作对 stale_reasons 敏感：拆解更新→必须先重存分镜；提示词被取代→改用最新版。
  it.each([
    [
      "SCRIPT_STALE",
      ["SHOT_CARD_SUPERSEDED"],
      "分镜已更新，请重新保存文案后再合成。",
    ],
    [
      "SCRIPT_STALE",
      ["ANALYSIS_SUPERSEDED"],
      "视频拆解已更新，请重新保存分镜、再重新保存文案后合成。",
    ],
    [
      "SHOT_CARD_STALE",
      ["ANALYSIS_SUPERSEDED"],
      "视频拆解已更新，请重新保存分镜、再重新保存文案后合成。",
    ],
    [
      "PROMPT_STALE",
      ["PROMPT_SUPERSEDED"],
      "最终提示词已更新，请改用最新一版，或重新合成后再继续。",
    ],
  ])(
    "names the changed stage for %s with %j",
    async (code, staleReasons, expectedMessage) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue({
          ok: false,
          status: 409,
          json: async () => ({
            detail: {
              code,
              message: "server detail",
              stale_reasons: staleReasons,
            },
          }),
        }),
      );

      const error = await compileGenerationPrompt("project-1", {
        script_version_id: "script-1",
        shot_card_version_id: "shot-1",
        first_frame_asset_id: "frame-1",
        output_duration_seconds: 15,
        resolution: "768P",
        ratio: "adaptive",
      }).catch((requestError: unknown) => requestError);

      expect(error).toMatchObject({ status: 409, code, staleReasons });
      expect((error as Error).message).toBe(expectedMessage);
    },
  );

  it("names the changed stage when generation submission finds a stale prompt", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: async () => ({
          detail: {
            code: "PROMPT_STALE",
            message: "Upstream inputs changed.",
            stale_reasons: ["SCRIPT_SUPERSEDED"],
          },
        }),
      }),
    );

    const error = await createGenerationBatch("project-1", {
      quantity: 1,
      prompt_version_id: "prompt-1",
      first_frame_asset_id: "frame-1",
      output_duration_seconds: 10,
      resolution: "768P",
      ratio: "adaptive",
      idempotency_key: "key-1",
      provider: "fake_h3",
      fake_audio_quality: "ok",
    }).catch((requestError: unknown) => requestError);

    expect(error).toMatchObject({ status: 409, code: "PROMPT_STALE" });
    expect((error as Error).message).toBe(
      "口播文案已更新，请重新合成最终提示词后再继续。",
    );
  });

  it("appends the error code when a 409 conflict is not yet mapped", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: async () => ({
          detail: { code: "FUTURE_CONFLICT_CODE", message: "server detail" },
        }),
      }),
    );

    const error = await compileGenerationPrompt("project-1", {
      script_version_id: "script-1",
      shot_card_version_id: "shot-1",
      first_frame_asset_id: "frame-1",
      output_duration_seconds: 15,
      resolution: "768P",
      ratio: "adaptive",
    }).catch((requestError: unknown) => requestError);

    expect(error).toMatchObject({ status: 409, code: "FUTURE_CONFLICT_CODE" });
    expect((error as Error).message).toBe(
      "上游内容已变化，请重新确认后再试（错误代码：FUTURE_CONFLICT_CODE）",
    );
  });

  it("keeps the generic 409 copy when the server sends no known code", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: async () => ({}),
      }),
    );

    const error = await compileGenerationPrompt("project-1", {
      script_version_id: "script-1",
      shot_card_version_id: "shot-1",
      first_frame_asset_id: "frame-1",
      output_duration_seconds: 15,
      resolution: "768P",
      ratio: "adaptive",
    }).catch((requestError: unknown) => requestError);

    expect((error as Error).message).toBe("上游内容已变化，请重新确认后再试");
  });
});

describe("character reference and first-frame binding", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reads recommendations without creating a selection", async () => {
    const recommendation = { recommended_asset_ids_json: ["asset-1"] };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => recommendation,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      getCharacterReferenceRecommendation("project 1"),
    ).resolves.toEqual(recommendation);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects/project%201/character-reference-recommendation",
      expect.objectContaining({
        headers: expect.any(Headers),
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it("sends explicit source features and selected character references", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "saved" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const features = {
      orientation: "FRONT" as const,
      shot_size: "HALF_BODY" as const,
      face_visible: true,
      body_completeness: "UPPER_BODY" as const,
    };

    await confirmSourceFrame("project-1", "source-1", features);
    await selectCharacterReferences("project-1", {
      selected_asset_ids: ["reference-1"],
      source_frame_selection_version_id: "source-selection-1",
      character_version_id: "character-version-1",
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8000/api/projects/project-1/source-frames/confirm",
      expect.objectContaining({
        body: JSON.stringify({
          source_frame_asset_id: "source-1",
          character_features: features,
        }),
        method: "POST",
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8000/api/projects/project-1/character-reference-selection",
      expect.objectContaining({
        body: JSON.stringify({
          selected_asset_ids: ["reference-1"],
          source_frame_selection_version_id: "source-selection-1",
          character_version_id: "character-version-1",
        }),
        method: "POST",
      }),
    );
  });

  it("lets the server apply safe source-frame defaults during automatic confirmation", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "saved" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await confirmSourceFrame("project-1", "source-1");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects/project-1/source-frames/confirm",
      expect.objectContaining({
        body: JSON.stringify({ source_frame_asset_id: "source-1" }),
        method: "POST",
      }),
    );
  });

  it("maps a stale latest generation and sends the frozen binding on regeneration", async () => {
    const generatedVersion = {
      id: "first-frame-candidates-1",
      project_id: "project-1",
      asset_id: "source-1",
      kind: "first_frame_candidates",
      version_number: 1,
      payload: { candidates: [] },
      created_by_user_id: "employee-1",
      created_at: "2030-01-01T00:00:00Z",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 409 })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: "first-frame-task-1" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          id: "first-frame-task-1",
          status: "SUCCEEDED",
          result_version_id: generatedVersion.id,
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => generatedVersion,
      });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getLatestProjectFirstFrames("project-1")).resolves.toEqual({
      version: null,
      stale: true,
    });
    await generateFirstFrames("project-1", {
      model: "nano-banana-pro-2k",
      prompt: "replace",
      quantity: 1,
      replace_scene: false,
      character_version_id: "character-version-1",
      character_reference_selection_id: "reference-selection-1",
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8000/api/projects/project-1/first-frame-tasks",
      expect.objectContaining({
        method: "POST",
      }),
    );
    const submitted = JSON.parse(
      String(fetchMock.mock.calls[1]?.[1]?.body),
    ) as Record<string, unknown>;
    expect(submitted).toMatchObject({
      model: "nano-banana-pro-2k",
      prompt: "replace",
      quantity: 1,
      character_version_id: "character-version-1",
      character_reference_selection_id: "reference-selection-1",
    });
    expect(submitted.idempotency_key).toMatch(/^first-frame-/);
  });

  it("shares one character task poller across concurrent recovery callers", async () => {
    vi.useFakeTimers();
    const pending = { id: "character-task-shared", status: "RUNNING" };
    const succeeded = {
      id: "character-task-shared",
      status: "SUCCEEDED",
      result: { persona_id: "persona-1" },
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => pending })
      .mockResolvedValueOnce({ ok: true, json: async () => succeeded });
    vi.stubGlobal("fetch", fetchMock);

    const firstUpdates: string[] = [];
    const recoveredUpdates: string[] = [];
    const first = waitForCharacterSheetTask("character-task-shared", (task) =>
      firstUpdates.push(task.status),
    );
    await vi.advanceTimersByTimeAsync(0);
    const recovered = waitForCharacterSheetTask(
      "character-task-shared",
      (task) => recoveredUpdates.push(task.status),
    );
    await vi.advanceTimersByTimeAsync(1_500);

    await expect(Promise.all([first, recovered])).resolves.toEqual([
      succeeded,
      succeeded,
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(firstUpdates).toEqual(["RUNNING", "SUCCEEDED"]);
    expect(recoveredUpdates).toEqual(["RUNNING", "SUCCEEDED"]);
  });

  it("shares one first-frame task poller and broadcasts progress to recovery callers", async () => {
    vi.useFakeTimers();
    const pending = {
      id: "first-frame-task-shared",
      status: "PENDING",
      stage: "QUEUED",
    };
    const succeeded = {
      id: "first-frame-task-shared",
      status: "SUCCEEDED",
      stage: "SUCCEEDED",
      result_version_id: "version-shared",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => pending })
      .mockResolvedValueOnce({ ok: true, json: async () => succeeded });
    vi.stubGlobal("fetch", fetchMock);

    const firstUpdates: string[] = [];
    const recoveredUpdates: string[] = [];
    const first = waitForFirstFrameTask("first-frame-task-shared", (task) =>
      firstUpdates.push(task.stage),
    );
    const recovered = waitForFirstFrameTask("first-frame-task-shared", (task) =>
      recoveredUpdates.push(task.stage),
    );
    await vi.advanceTimersByTimeAsync(1_500);

    await expect(Promise.all([first, recovered])).resolves.toEqual([
      succeeded,
      succeeded,
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(firstUpdates).toEqual(["QUEUED", "SUCCEEDED"]);
    expect(recoveredUpdates).toEqual(["QUEUED", "SUCCEEDED"]);
  });
});

describe("project character version selection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads only project-approved immutable character versions", async () => {
    const versions = [{ character_version_id: "character-version-3" }];
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => versions,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(listProjectCharacterVersions("project 1")).resolves.toEqual(
      versions,
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects/project%201/character-versions/available",
      expect.objectContaining({
        headers: expect.any(Headers),
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it("selects by immutable character version id", async () => {
    const selection = {
      project_id: "project-1",
      character_version_id: "character-version-3",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => selection,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      chooseProjectMainCharacterVersion("project-1", "character-version-3"),
    ).resolves.toEqual(selection);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects/project-1/main-character",
      expect.objectContaining({
        body: JSON.stringify({ character_version_id: "character-version-3" }),
        headers: expect.any(Headers),
        method: "PUT",
        signal: expect.any(AbortSignal),
      }),
    );
  });
});

describe("getHealth", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("rejects a non-success response from the local API", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 503 }),
    );

    await expect(getHealth()).rejects.toThrow("本地服务暂不可用（503）");
  });
});

describe("API error details", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the server precheck message instead of only the HTTP status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: async () => ({
          detail: {
            code: "VIDEO_DURATION_OUT_OF_RANGE",
            message: "检测到 16.20 秒，参考视频需为 4–15 秒。",
          },
        }),
      }),
    );

    await expect(completeVideoUpload("asset-1")).rejects.toThrow(
      "参考视频预检失败：检测到 16.20 秒，参考视频需为 4–15 秒。（422）",
    );
  });
});

describe("getGenerationBatch", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads a generation batch by id from the local API", async () => {
    const batch = {
      id: "batch 1",
      status: "RUNNING",
      quantity: 2,
      progress: {
        total_count: 2,
        terminal_count: 1,
        progress_percent: 50,
        counts: { succeeded: 1, running: 1, needs_attention: 0 },
      },
      tasks: [],
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => batch,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getGenerationBatch("batch 1")).resolves.toEqual(batch);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/generation-batches/batch%201",
      expect.objectContaining({
        headers: expect.any(Headers),
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it("rejects a non-success batch response from the local API", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 404 }),
    );

    await expect(getGenerationBatch("missing")).rejects.toThrow(
      "任务批次暂不可用（404）",
    );
  });
});

describe("listGenerationBatches", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads a filtered cursor page without sending a request body", async () => {
    const page = {
      items: [],
      next_cursor: "next-cursor",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => page,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      listGenerationBatches({
        projectId: "project 1",
        createdByUserId: "admin 1",
        status: "NEEDS_ATTENTION",
        needsAttention: true,
        limit: 10,
        cursor: "cursor/value",
      }),
    ).resolves.toEqual(page);

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/generation-batches?project_id=project+1&created_by_user_id=admin+1&status=NEEDS_ATTENTION&needs_attention=true&limit=10&cursor=cursor%2Fvalue",
      expect.objectContaining({
        headers: expect.any(Headers),
        signal: expect.any(AbortSignal),
      }),
    );
    const request = fetchMock.mock.calls[0][1] as RequestInit;
    expect(request.method).toBeUndefined();
    expect(request.body).toBeUndefined();
  });
});

describe("createProject", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("creates a project through the authenticated API without X-Dev-User-Id (CW-015)", async () => {
    const project = {
      id: "project-1",
      owner_user_id: "employee_1",
      name: "参考视频复刻",
      status: "ACTIVE",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => project,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(createProject("参考视频复刻")).resolves.toEqual(project);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ name: "参考视频复刻" }),
        headers: expect.any(Headers),
        signal: expect.any(AbortSignal),
      }),
    );
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    // CW-015: 正式客户构建不再使用开发身份路径
    expect((options.headers as Headers).has("X-Dev-User-Id")).toBe(false);
  });
});

describe("startVideoAnalysis", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("only enqueues analysis with the normal API timeout", async () => {
    const timeoutSpy = vi.spyOn(window, "setTimeout");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "analysis-1" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await startVideoAnalysis("project-1", "asset-1");

    // MATERIAL-PERF-C：默认超时 5s→10s；本用例钉住分析入队走普通超时而非云操作超时。
    expect(timeoutSpy).toHaveBeenCalledWith(
      expect.any(Function),
      REQUEST_TIMEOUT_MS,
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/projects/project-1/analysis-tasks",
      expect.objectContaining({
        body: JSON.stringify({
          asset_id: "asset-1",
          reuse_existing: true,
        }),
      }),
    );
  });

  it("shares one analysis task poller across concurrent recovery callers", async () => {
    vi.useFakeTimers();
    const pending = { id: "analysis-task-shared", status: "RUNNING" };
    const succeeded = {
      id: "analysis-task-shared",
      status: "SUCCEEDED",
      result_version_id: "analysis-version-shared",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => pending })
      .mockResolvedValueOnce({ ok: true, json: async () => succeeded });
    vi.stubGlobal("fetch", fetchMock);

    const first = waitForAnalysisTask("analysis-task-shared");
    const recovered = waitForAnalysisTask("analysis-task-shared");
    await vi.advanceTimersByTimeAsync(1_500);

    await expect(Promise.all([first, recovered])).resolves.toEqual([
      succeeded,
      succeeded,
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("backs off polling instead of hammering a fixed 1.5s interval", async () => {
    vi.useFakeTimers();
    const running = { id: "analysis-task-backoff", status: "RUNNING" };
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => running });
    vi.stubGlobal("fetch", fetchMock);

    const pending = waitForAnalysisTask("analysis-task-backoff");
    // 先挂好终局断言：推进到死线时 rejection 必须已有 handler，否则会报 unhandled。
    const timeoutExpectation = expect(pending).rejects.toThrow("仍在后台");
    // 60 秒窗口：固定 1.5s 间隔会打约 40 次；指数退避后应骤降到 10 次上下。
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(12);

    // 推进到 20 分钟死线：总量仍应远低于固定间隔的约 800 次，并以可读错误收尾。
    await vi.advanceTimersByTimeAsync(20 * 60_000);
    await timeoutExpectation;
    expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(80);
  });

  it("backs off script rewrite polling with the same policy", async () => {
    vi.useFakeTimers();
    const running = { id: "script-rewrite-backoff", status: "RUNNING" };
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => running });
    vi.stubGlobal("fetch", fetchMock);

    const pending = waitForScriptRewriteTask("script-rewrite-backoff");
    // 先挂好终局断言：推进到死线时 rejection 必须已有 handler，否则会报 unhandled。
    const timeoutExpectation = expect(pending).rejects.toThrow("仍在后台");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(12);

    // 改写任务死线 10 分钟：走完仍应保持低频，并以可读错误收尾。
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    await timeoutExpectation;
    expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(70);
  });

  it("returns a success from a backed-off poll within the early window", async () => {
    vi.useFakeTimers();
    const running = { id: "analysis-task-backoff-ok", status: "RUNNING" };
    const succeeded = {
      id: "analysis-task-backoff-ok",
      status: "SUCCEEDED",
      result_version_id: "av-backoff-ok",
    };
    let calls = 0;
    const fetchMock = vi.fn(async () => {
      calls += 1;
      return { ok: true, json: async () => (calls < 6 ? running : succeeded) };
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = waitForAnalysisTask("analysis-task-backoff-ok");
    await vi.advanceTimersByTimeAsync(30_000);

    await expect(result).resolves.toEqual(succeeded);
    // 前五次退避间隔累计约 20 秒，第 6 次查询即可在 30 秒窗口内看到终态。
    expect(calls).toBe(6);
  });

  it("reports analysis status without allowing a UI callback to stop polling", async () => {
    vi.useFakeTimers();
    const running = { id: "analysis-task-status", status: "RUNNING" };
    const succeeded = {
      id: "analysis-task-status",
      status: "SUCCEEDED",
      result_version_id: "analysis-version-status",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => running })
      .mockResolvedValueOnce({ ok: true, json: async () => succeeded });
    vi.stubGlobal("fetch", fetchMock);
    const observer = vi.fn().mockImplementationOnce(() => {
      throw new Error("unmounted UI");
    });

    const result = waitForAnalysisTask("analysis-task-status", observer);
    await vi.advanceTimersByTimeAsync(1_500);

    await expect(result).resolves.toEqual(succeeded);
    expect(observer).toHaveBeenNthCalledWith(1, running);
    expect(observer).toHaveBeenNthCalledWith(2, succeeded);
  });

  it("keeps the task and request numbers on a failed analysis poll (P1-4)", async () => {
    // 失败卡片要能直接展示「任务编号 + 问题编号」：轮询抛出的错误必须逐字段
    // 携带，而不是只剩一句 message。
    const failed = {
      id: "analysis-task-ref",
      status: "FAILED",
      error_code: "ANALYSIS_PROVIDER_FAILED",
      error_message: "视频拆解失败，请稍后重新拆解。",
      request_id: "req-support-42",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => failed }),
    );

    const failure = await waitForAnalysisTask("analysis-task-ref").catch(
      (cause: unknown) => cause,
    );

    expect(failure).toMatchObject({
      message: "视频拆解失败，请稍后重新拆解。",
      code: "ANALYSIS_PROVIDER_FAILED",
      taskId: "analysis-task-ref",
      requestId: "req-support-42",
    });
    expect(analysisFailureReference(failure)).toBe(
      "任务编号：analysis-task-ref；问题编号：req-support-42",
    );
  });

  it("shows only the numbers that exist for a legacy failed task (P1-4)", async () => {
    // 存量任务与未带请求头的调用没有 request_id：只展示任务编号，不拼空括号。
    const failed = {
      id: "analysis-task-legacy",
      status: "FAILED",
      error_code: null,
      error_message: "视频拆解失败，请稍后重新拆解。",
      request_id: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => failed }),
    );

    const failure = await waitForAnalysisTask("analysis-task-legacy").catch(
      (cause: unknown) => cause,
    );

    expect(failure).toMatchObject({ taskId: "analysis-task-legacy" });
    expect((failure as { requestId?: string }).requestId).toBeUndefined();
    expect(analysisFailureReference(failure)).toBe(
      "任务编号：analysis-task-legacy",
    );
    // 两个编号都缺失时返回空串，调用方据此决定不加括号。
    expect(analysisFailureReference(new Error("plain failure"))).toBe("");
  });

  it("enqueues source-frame extraction and shares its durable poller", async () => {
    vi.useFakeTimers();
    const queued = {
      id: "source-frame-task-shared",
      project_id: "project-1",
      asset_id: "asset-1",
      timestamps_seconds: [2.4, 6, 9.6],
      status: "PENDING",
    };
    const running = { ...queued, status: "RUNNING" };
    const succeeded = {
      ...queued,
      status: "SUCCEEDED",
      result_version_id: "source-frame-version-shared",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => queued })
      .mockResolvedValueOnce({ ok: true, json: async () => running })
      .mockResolvedValueOnce({ ok: true, json: async () => succeeded });
    vi.stubGlobal("fetch", fetchMock);

    const task = await extractSourceFrames(
      "project-1",
      "asset-1",
      queued.timestamps_seconds,
    );
    const first = waitForSourceFrameTask(task.id);
    const recovered = waitForSourceFrameTask(task.id);
    await vi.advanceTimersByTimeAsync(1_500);

    await expect(Promise.all([first, recovered])).resolves.toEqual([
      succeeded,
      succeeded,
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const enqueueBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(enqueueBody).toEqual(
      expect.objectContaining({
        asset_id: "asset-1",
        timestamps_seconds: [2.4, 6, 9.6],
        idempotency_key: expect.any(String),
      }),
    );
  });

  it("preserves the failed source-frame task for safe UI recovery", async () => {
    const failed = {
      id: "source-frame-task-failed",
      project_id: "project-1",
      asset_id: "asset-1",
      timestamps_seconds: [2.4],
      status: "FAILED",
      attempt: 1,
      result_version_id: null,
      error_code: "SOURCE_FRAME_TASK_RECOVERY_REQUIRED",
      error_message: "取帧任务执行中断，请重新开始。",
      retryable: true,
      created_at: "2026-09-02T00:00:00Z",
      updated_at: "2026-09-02T00:01:00Z",
      started_at: "2026-09-02T00:00:01Z",
      completed_at: "2026-09-02T00:01:00Z",
    } as const;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => failed }),
    );

    await expect(waitForSourceFrameTask(failed.id)).rejects.toMatchObject({
      name: "SourceFrameTaskFailedError",
      task: failed,
    });
  });

  it("cancels a source-frame task through its recovery endpoint", async () => {
    const cancelled = {
      id: "source-frame-task-cancelled",
      project_id: "project-1",
      asset_id: "asset-1",
      timestamps_seconds: [2.4],
      status: "FAILED",
      error_code: "SOURCE_FRAME_TASK_CANCELLED",
      retryable: true,
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => cancelled,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      cancelSourceFrameTask("source-frame-task-cancelled"),
    ).resolves.toEqual(cancelled);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/source-frame-tasks/source-frame-task-cancelled/cancel",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("enqueues script rewrite and shares its durable recovery poller", async () => {
    vi.useFakeTimers();
    const queued = {
      id: "script-rewrite-task-shared",
      project_id: "project-1",
      status: "PENDING",
      result: null,
    };
    const running = { ...queued, status: "RUNNING" };
    const succeeded = {
      ...queued,
      status: "SUCCEEDED",
      result: {
        rewritten_text: "新的二创稿。",
        provider: "deepseek",
        model: "deepseek-chat",
      },
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => queued })
      .mockResolvedValueOnce({ ok: true, json: async () => running })
      .mockResolvedValueOnce({ ok: true, json: async () => succeeded });
    vi.stubGlobal("fetch", fetchMock);

    const task = await rewriteProjectScript(
      "project-1",
      "待改写原稿。",
      "identity-1",
      "source-1",
      "rewrite-key-1",
    );
    const first = waitForScriptRewriteTask(task.id);
    const recovered = waitForScriptRewriteTask(task.id);
    await vi.advanceTimersByTimeAsync(1_500);

    await expect(Promise.all([first, recovered])).resolves.toEqual([
      succeeded,
      succeeded,
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const enqueueBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(enqueueBody).toEqual({
      text: "待改写原稿。",
      identity_id: "identity-1",
      source_asset_id: "source-1",
      idempotency_key: "rewrite-key-1",
    });
  });

  it("读取本次音频转写任务使用任务 ID 并编码路径", async () => {
    const task = { id: "asr/one", status: "RUNNING" };
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => task });
    vi.stubGlobal("fetch", fetchMock);
    await expect(getScriptFromAudioTask("asr/one")).resolves.toEqual(task);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/script-from-audio-tasks/asr%2Fone",
    );
  });

  it("按人物或无人物作用域读取最新改写任务", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => null });
    vi.stubGlobal("fetch", fetchMock);

    await getLatestScriptRewriteTask("project-1", "identity 1", "source/1");
    await getLatestScriptRewriteTask("project-1");

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "http://127.0.0.1:8000/api/projects/project-1/script-rewrite-tasks/latest?identity_scope=identity&identity_id=identity+1&source_asset_id=source%2F1",
      "http://127.0.0.1:8000/api/projects/project-1/script-rewrite-tasks/latest?identity_scope=none",
    ]);
  });

  it("enqueues H3 reconciliation and shares its durable recovery poller", async () => {
    vi.useFakeTimers();
    const queued = {
      id: "reconcile-operation-shared",
      task_id: "generation-task-1",
      status: "PENDING",
    };
    const running = { ...queued, status: "RUNNING" };
    const succeeded = { ...queued, status: "SUCCEEDED" };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => queued })
      .mockResolvedValueOnce({ ok: true, json: async () => running })
      .mockResolvedValueOnce({ ok: true, json: async () => succeeded });
    vi.stubGlobal("fetch", fetchMock);

    const operation = await reconcileUncertainTask("generation-task-1", {
      idempotency_key: "reconcile-operation-key",
    });
    const first = waitForGenerationReconcileOperation(operation.id);
    const recovered = waitForGenerationReconcileOperation(operation.id);
    await vi.advanceTimersByTimeAsync(1_500);

    await expect(Promise.all([first, recovered])).resolves.toEqual([
      succeeded,
      succeeded,
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/generation-tasks/generation-task-1/reconcile",
    );
  });

  it("tells a temporary network failure apart from an unusable model response", async () => {
    const unreachable = vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({
        detail: {
          code: "ANALYSIS_PROVIDER_UNREACHABLE",
          message: "无法连接视频拆解服务，请检查网络后重试。",
          failure_phase: "network",
          retryable: true,
        },
      }),
    });
    vi.stubGlobal("fetch", unreachable);

    await expect(startVideoAnalysis("project-1", "asset-1")).rejects.toThrow(
      /网络/,
    );

    const invalidResponse = vi.fn().mockResolvedValue({
      ok: false,
      status: 502,
      json: async () => ({
        detail: {
          code: "ANALYSIS_PROVIDER_FAILED",
          message: "视频拆解服务返回了无法解析的结果，请重试或更换参考视频。",
          failure_phase: "response",
          retryable: false,
        },
      }),
    });
    vi.stubGlobal("fetch", invalidResponse);

    await expect(startVideoAnalysis("project-1", "asset-1")).rejects.toThrow(
      /无法解析的结果/,
    );
  });

  it("explains a request rejected by server-side validation instead of showing a bare status", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 422,
      json: async () => ({
        detail: [
          {
            type: "less_than_equal",
            loc: ["body", "duration_seconds"],
            msg: "Input should be less than or equal to 15.1",
          },
        ],
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(startVideoAnalysis("project-1", "asset-1")).rejects.toThrow(
      /视频时长：格式不正确/,
    );
  });

  it("maps an analysis transport failure to a cloud hint, never a local-service prompt (CW-018)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("Failed to fetch")),
    );

    const error = await startVideoAnalysis("project-1", "asset-1").catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(Error);
    // CW-018: the customer product is cloud-only (CW-015 fail-closed base,
    // CW-021 removes the local backend), so a transport failure must guide the
    // customer to check their network — matching the server's own cloud wording
    // (“请检查网络后重试”) — and must never tell them to check a local service.
    expect((error as Error).message).toBe(
      "启动视频拆解失败：网络连接失败，请检查网络后重试",
    );
    expect((error as Error).message).not.toMatch(/本地服务/);
  });
});

describe("createVideoUploadIntent", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends a SHA-256 fingerprint so the server can skip duplicate uploads", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        asset_id: "asset-1",
        project_id: "project-1",
        storage_key: "projects/project-1/video.mp4",
        method: null,
        url: null,
        headers: {},
        expires_at: null,
        upload_required: false,
      }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const file = new File(["same-video"], "reference.mp4", {
      type: "video/mp4",
    });

    await createVideoUploadIntent("project-1", file);

    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const body = JSON.parse(String(options.body));
    expect(body.sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(body.size_bytes).toBe(file.size);
  });
});

describe("getCurrentUser", () => {
  afterEach(() => {
    setCustomerSessionToken(null);
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("uses the customer session for shared workspace requests", async () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("PROD", true);
    const user = {
      id: "customer-1",
      username: "customer-1",
      display_name: "客户",
      role: "customer",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => user,
    });
    vi.stubGlobal("fetch", fetchMock);
    setCustomerSessionToken("customer-session-1");

    await expect(getCurrentUser()).resolves.toEqual(user);

    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const headers = options.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer customer-session-1");
    expect(headers.has("X-Dev-User-Id")).toBe(false);
  });

  it("does not let an older workspace cleanup clear the active session", async () => {
    const releaseOlder = attachCustomerSessionToken("older-session");
    const releaseCurrent = attachCustomerSessionToken("current-session");
    releaseOlder();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        id: "customer-1",
        username: "customer-1",
        display_name: "Customer One",
        role: "customer",
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await getCurrentUser();

    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(options.headers).get("Authorization")).toBe(
      "Bearer current-session",
    );
    releaseCurrent();
  });

  it("loads the current user from auth/me without X-Dev-User-Id (CW-015)", async () => {
    const user = {
      id: "employee_1",
      username: "employee_1",
      display_name: "林夏",
      role: "employee",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => user,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getCurrentUser()).resolves.toEqual(user);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/auth/me",
      expect.objectContaining({
        headers: expect.any(Headers),
        signal: expect.any(AbortSignal),
      }),
    );
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    // CW-015: 正式客户构建不再使用开发身份路径
    expect((options.headers as Headers).has("X-Dev-User-Id")).toBe(false);
  });

  it("does not fallback to a development identity in production builds", async () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("PROD", true);
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ detail: { message: "missing identity" } }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getCurrentUser()).rejects.toThrow(
      "身份验证失败：missing identity（401）",
    );
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((options.headers as Headers).has("X-Dev-User-Id")).toBe(false);
  });

  it("ignores an explicitly configured development identity in production builds", async () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("PROD", true);
    vi.stubEnv("VITE_DEV_USER_ID", "admin_1");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ detail: { message: "missing identity" } }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getCurrentUser()).rejects.toThrow(
      "身份验证失败：missing identity（401）",
    );
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((options.headers as Headers).has("X-Dev-User-Id")).toBe(false);
  });
});

describe("admin API authentication", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("does not use X-Dev-User-Id for settings (CW-015: remove development identity path)", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ providers: {}, runtime: {} }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await getSettings();

    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    // CW-015: 正式客户构建不再使用开发身份路径
    expect((options.headers as Headers).has("X-Dev-User-Id")).toBe(false);
  });

  it("does not use VITE_DEV_USER_ID for settings (CW-015: remove development identity path)", async () => {
    vi.stubEnv("VITE_DEV_USER_ID", "admin_1");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ providers: {}, runtime: {} }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await getSettings();

    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    // CW-015: 正式客户构建不再使用开发身份路径，即使设置了 VITE_DEV_USER_ID
    expect((options.headers as Headers).has("X-Dev-User-Id")).toBe(false);
  });
});

describe("uploadReferenceVideo", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it.each([
    ["http://127.0.0.1:5199", "/api/assets/local-objects/source.mp4"],
    [
      "https://studio.example.com/backend",
      "/api/studio/materials/uploads/a/content",
    ],
  ])(
    "resolves managed uploads through the configured API base %s",
    async (base, path) => {
      const headers = new Map<string, string>();
      const open = vi.fn();
      class ProxyUploadRequest {
        upload = { onprogress: null };
        status = 204;
        onload: (() => void) | null = null;
        open = open;
        setRequestHeader(name: string, value: string) {
          headers.set(name, value);
        }
        send() {
          this.onload?.();
        }
      }
      vi.stubEnv("VITE_API_BASE_URL", base);
      vi.stubGlobal("XMLHttpRequest", ProxyUploadRequest);
      setCustomerSessionToken("customer-proxy-upload-test");
      try {
        await uploadReferenceVideo(
          {
            asset_id: "a",
            project_id: "p",
            storage_key: "source.mp4",
            method: "PUT",
            url: path,
            headers: { "Content-Type": "video/mp4" },
            expires_at: "2030-01-01T00:00:00Z",
          },
          new File(["video"], "source.mp4", { type: "video/mp4" }),
          vi.fn(),
        );
        expect(open).toHaveBeenCalledWith("PUT", `${base}${path}`);
        expect(headers.get("Authorization")).toBe(
          "Bearer customer-proxy-upload-test",
        );
      } finally {
        setCustomerSessionToken(null);
      }
    },
  );

  it.each(["test-cloud-customer-token", "web-session:test-cloud-csrf"])(
    "does not send customer authentication headers to a cloud presigned URL (%s)",
    async (sessionToken) => {
      class CloudUploadRequest {
        static latest: CloudUploadRequest | null = null;
        headers = new Map<string, string>();
        onerror: (() => void) | null = null;
        onload: (() => void) | null = null;
        ontimeout: (() => void) | null = null;
        status = 200;
        timeout = 0;
        upload: { onprogress: ((event: ProgressEvent) => void) | null } = {
          onprogress: null,
        };

        constructor() {
          CloudUploadRequest.latest = this;
        }

        open() {}
        setRequestHeader(name: string, value: string) {
          this.headers.set(name, value);
        }
        send() {
          this.onload?.();
        }
      }

      vi.stubGlobal("XMLHttpRequest", CloudUploadRequest);
      setCustomerSessionToken(sessionToken);

      await uploadReferenceVideo(
        {
          asset_id: "asset-1",
          project_id: "project-1",
          storage_key: "projects/project-1/reference.mp4",
          method: "PUT",
          url: "https://cos.example.com/presigned-upload",
          headers: { "Content-Type": "video/mp4" },
          expires_at: "2030-01-01T00:00:00Z",
        },
        new File(["video"], "reference.mp4", { type: "video/mp4" }),
        vi.fn(),
      );

      expect(
        CloudUploadRequest.latest?.headers.get("X-Dev-User-Id"),
      ).toBeUndefined();
      setCustomerSessionToken(null);
      expect(CloudUploadRequest.latest?.headers.has("Authorization")).toBe(
        false,
      );
      expect(CloudUploadRequest.latest?.headers.has("X-Customer-Web")).toBe(
        false,
      );
      expect(CloudUploadRequest.latest?.headers.get("Content-Type")).toBe(
        "video/mp4",
      );
    },
  );

  it("does not send X-Dev-User-Id header for API uploads (CW-015: remove development identity path)", async () => {
    class LocalUploadRequest {
      static latest: LocalUploadRequest | null = null;
      headers = new Map<string, string>();
      onerror: (() => void) | null = null;
      onload: (() => void) | null = null;
      ontimeout: (() => void) | null = null;
      status = 204;
      timeout = 0;
      upload: { onprogress: ((event: ProgressEvent) => void) | null } = {
        onprogress: null,
      };

      constructor() {
        LocalUploadRequest.latest = this;
      }

      open() {}
      setRequestHeader(name: string, value: string) {
        this.headers.set(name, value);
      }
      send() {
        this.onload?.();
      }
    }

    vi.stubGlobal("XMLHttpRequest", LocalUploadRequest);

    await uploadReferenceVideo(
      {
        asset_id: "asset-1",
        project_id: "project-1",
        storage_key: "projects/project-1/reference.mp4",
        method: "PUT",
        url: "http://127.0.0.1:8000/api/assets/local-objects/projects/project-1/reference.mp4",
        headers: { "Content-Type": "video/mp4" },
        expires_at: "2030-01-01T00:00:00Z",
      },
      new File(["video"], "reference.mp4", { type: "video/mp4" }),
      vi.fn(),
    );

    // CW-015: 正式客户构建不再使用开发身份路径
    expect(LocalUploadRequest.latest?.headers.has("X-Dev-User-Id")).toBe(false);
  });

  it.each(["customer-session-token-1", "web-session:test-managed-csrf"])(
    "uses the customer credential and browser transport when needed for API uploads (%s)",
    async (sessionToken) => {
      class ManagedUploadRequest {
        static latest: ManagedUploadRequest | null = null;
        headers = new Map<string, string>();
        onerror: (() => void) | null = null;
        onload: (() => void) | null = null;
        ontimeout: (() => void) | null = null;
        status = 204;
        timeout = 0;
        upload: { onprogress: ((event: ProgressEvent) => void) | null } = {
          onprogress: null,
        };

        constructor() {
          ManagedUploadRequest.latest = this;
        }

        open() {}
        setRequestHeader(name: string, value: string) {
          this.headers.set(name, value);
        }
        send() {
          this.onload?.();
        }
      }

      vi.stubGlobal("XMLHttpRequest", ManagedUploadRequest);
      // CW-015: 即使设置了 internalAccessToken，也不应该使用它
      setInternalAccessToken("internal-token-1");
      setCustomerSessionToken(sessionToken);

      try {
        await uploadReferenceVideo(
          {
            asset_id: "asset-1",
            project_id: "project-1",
            storage_key: "projects/project-1/reference.mp4",
            method: "PUT",
            url: "http://127.0.0.1:8000/api/assets/local-objects/projects/project-1/reference.mp4",
            headers: { "Content-Type": "video/mp4" },
            expires_at: "2030-01-01T00:00:00Z",
          },
          new File(["video"], "reference.mp4", { type: "video/mp4" }),
          vi.fn(),
        );
      } finally {
        setInternalAccessToken(null);
        setCustomerSessionToken(null);
      }

      // CW-015: 正式客户构建只使用 customerSessionToken
      expect(ManagedUploadRequest.latest?.headers.get("Authorization")).toBe(
        `Bearer ${sessionToken}`,
      );
      expect(ManagedUploadRequest.latest?.headers.get("X-Customer-Web")).toBe(
        sessionToken.startsWith("web-session:") ? "1" : undefined,
      );
      expect(ManagedUploadRequest.latest?.headers.has("X-Dev-User-Id")).toBe(
        false,
      );
    },
  );

  it("emits the unified session-expired event when a local upload returns 401", async () => {
    class UnauthorizedUploadRequest {
      static latest: UnauthorizedUploadRequest | null = null;
      headers = new Map<string, string>();
      onerror: (() => void) | null = null;
      onload: (() => void) | null = null;
      ontimeout: (() => void) | null = null;
      status = 401;
      timeout = 0;
      upload: { onprogress: ((event: ProgressEvent) => void) | null } = {
        onprogress: null,
      };

      constructor() {
        UnauthorizedUploadRequest.latest = this;
      }

      open() {}
      setRequestHeader(name: string, value: string) {
        this.headers.set(name, value);
      }
      send() {
        this.onload?.();
      }
    }

    const onSessionExpired = vi.fn();
    window.addEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
    vi.stubGlobal("XMLHttpRequest", UnauthorizedUploadRequest);

    await expect(
      uploadReferenceVideo(
        {
          asset_id: "asset-1",
          project_id: "project-1",
          storage_key: "projects/project-1/reference.mp4",
          method: "PUT",
          url: "http://127.0.0.1:8000/api/assets/local-objects/projects/project-1/reference.mp4",
          headers: { "Content-Type": "video/mp4" },
          expires_at: "2030-01-01T00:00:00Z",
        },
        new File(["video"], "reference.mp4", { type: "video/mp4" }),
        vi.fn(),
      ),
    ).rejects.toThrow("登录已失效，请重新进入工作台。");

    expect(onSessionExpired).toHaveBeenCalledOnce();
    window.removeEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
  });
});

describe("deduplicated oral materials", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });
  it.each(["video/mp4", "audio/mpeg"])(
    "resolves %s without an empty-method upload or duplicate completion",
    async (type) => {
      const item = { id: "asset:reused", asset_id: "reused", status: "ready" };
      const fetchMock = vi
        .fn()
        .mockResolvedValue({ ok: true, json: async () => ({ items: [item] }) });
      const xhr = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      vi.stubGlobal("XMLHttpRequest", xhr);
      const intent = {
        material_id: "asset:reused",
        asset_id: "reused",
        storage_key: "reused",
        method: "",
        url: "",
        headers: {},
        expires_at: "",
        upload_required: false,
      };
      expect(
        await putMaterial(
          intent,
          new File(["source"], "source", { type }),
          vi.fn(),
        ),
      ).toEqual(item);
      expect(xhr).not.toHaveBeenCalled();
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(String(fetchMock.mock.calls[0][0])).toContain(
        "/materials/resolve",
      );
    },
  );
});

describe("爆款文案提取（本地抽音轨上传）", () => {
  it("先读共享文案缓存，命中与否都由同一端点回答", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        text: "共享缓存里的文案",
        updatedAt: "2026-09-23T00:00:00",
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      fetchViralCopy("wechat_channels", "wx/id?x=1"),
    ).resolves.toEqual({
      text: "共享缓存里的文案",
      updatedAt: "2026-09-23T00:00:00",
    });
    // 视频号 id 是可含斜杠的 opaque ID，必须整体编码进查询串。
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/viral/search/copy?platform=wechat_channels&videoId=wx%2Fid%3Fx%3D1",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it("上传音轨时按 multipart 提交并带上幂等键", async () => {
    const accepted = {
      text: null,
      updatedAt: null,
      projectId: "project-1",
      sourceAssetId: "asset-1",
      taskId: "task-1",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => accepted,
    });
    vi.stubGlobal("fetch", fetchMock);

    const audio = new Uint8Array([0, 0, 0, 32, 102, 116, 121, 112]);
    await expect(
      createViralCopyExtraction("douyin", "native-1", audio, "copy-key"),
    ).resolves.toEqual(accepted);

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/viral/videos/copy",
      expect.objectContaining({
        method: "POST",
        signal: expect.any(AbortSignal),
      }),
    );
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe("copy-key");
    // 手写 multipart 会把 Content-Type 的 boundary 弄丢，所以必须交给浏览器；
    // 上传的是音轨字节而不是逐元素 JSON 数组，否则一个几百 KB 的音轨会膨胀成 MB 级。
    expect(new Headers(init.headers).get("Content-Type")).toBeNull();
    const form = init.body as FormData;
    expect(form).toBeInstanceOf(FormData);
    expect(form.get("platform")).toBe("douyin");
    expect(form.get("videoId")).toBe("native-1");
    const file = form.get("file") as File;
    expect(file.type).toBe("audio/mp4");
    expect(new Uint8Array(await file.arrayBuffer())).toEqual(audio);
  });
});
