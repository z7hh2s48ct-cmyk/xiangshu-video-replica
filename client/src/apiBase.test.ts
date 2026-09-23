import { beforeEach, describe, expect, it, vi } from "vitest";

import { resolveManagedMediaUrl } from "./apiBase";

beforeEach(() => {
  vi.stubEnv("VITE_API_BASE_URL", "http://127.0.0.1:8000");
  vi.stubEnv("PROD", false);
});

describe("resolveManagedMediaUrl", () => {
  it("把站内相对地址拼到 API base 上", () => {
    expect(resolveManagedMediaUrl("/api/assets/signed-objects/a?b=1")).toBe(
      "http://127.0.0.1:8000/api/assets/signed-objects/a?b=1",
    );
  });

  it("绝对地址原样返回", () => {
    expect(resolveManagedMediaUrl("https://cos.example.com/a.png")).toBe(
      "https://cos.example.com/a.png",
    );
  });

  it("服务端地址缺失时返回空串而不是抛错", () => {
    // 老版本服务端/降级响应可能不带 url 字段；此时应按"无图"渲染，
    // 不能让 TypeError 把整个素材网格或人物面板炸掉。
    expect(resolveManagedMediaUrl(undefined)).toBe("");
    expect(resolveManagedMediaUrl(null)).toBe("");
    expect(resolveManagedMediaUrl("")).toBe("");
  });
});
