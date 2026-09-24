/** API 地址解析与站内媒体地址绝对化。
 *
 * 独立成模块的原因：服务端所有签名媒体地址都是站内相对路径
 * （`/api/assets/signed-objects/...`、`/api/viral/covers/...`），而桌面端页面
 * 的 origin 是 `tauri://`、客户云版前端可与 API 分域名部署——相对地址会打到
 * 应用自身而不是后端，`img`/`video` 只能渲染成空预览框。凡是要交给浏览器加载
 * 的服务端地址，一律先过 `resolveManagedMediaUrl`。
 *
 * 放在 api.ts 之外，是为了让映射层（studio/live.ts）在整模块 mock 掉 `../api`
 * 的测试里仍然跑真实的绝对化逻辑，不必维护一份会漂移的副本。
 */

type ApiRuntimeLocation = Pick<Location, "origin" | "protocol">;

export function resolveApiBaseUrl(
  configuredUrl: string | undefined,
  isProduction: boolean,
  runtimeLocation: ApiRuntimeLocation,
): string {
  const normalizedUrl = configuredUrl?.trim().replace(/\/+$/, "");
  if (normalizedUrl) {
    return normalizedUrl;
  }
  if (isProduction && runtimeLocation.protocol === "https:") {
    return runtimeLocation.origin;
  }
  // CW-015: the customer cloud build has a single address source
  // (VITE_API_BASE_URL, validated as a routable non-loopback HTTPS origin at
  // build time by scripts/require_customer_api_base.mjs). A missing address is
  // a configuration error, so fail closed rather than silently falling back to
  // a loopback origin that would point a deployed customer client at localhost.
  throw new Error("API base URL is required (VITE_API_BASE_URL)");
}

export function apiBaseUrl(): string {
  return resolveApiBaseUrl(
    import.meta.env.VITE_API_BASE_URL,
    import.meta.env.PROD,
    window.location,
  );
}

/** 站内相对媒体地址 → 可加载的绝对地址；已经是绝对地址的原样返回。
 *
 * 老版本服务端/降级响应可能不带 url 字段（undefined/null/空串），此时按
 * "无地址"返回空串，由调用方渲染占位——绝不能抛 TypeError 把整个素材网格
 * 或人物面板的加载链炸掉。 */
export function resolveManagedMediaUrl(url: string | null | undefined): string {
  if (!url) return "";
  return url.startsWith("/") && !url.startsWith("//")
    ? `${apiBaseUrl()}${url}`
    : url;
}
