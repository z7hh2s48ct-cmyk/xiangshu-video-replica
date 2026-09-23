/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * 视频生成批次的 Provider 路由："fake_h3" 走本地模拟（不产生付费调用），
   * 未设置或 "metaso" 走真实 MiniMax H3。默认 metaso。
   */
  readonly VITE_GENERATION_PROVIDER?: "fake_h3" | "metaso";
  /** 本地管理站入口，只用于整页环境切换。 */
  readonly VITE_LOCAL_ADMIN_ORIGIN?: string;
  /** 云端正式管理站入口，只用于整页环境切换。 */
  readonly VITE_CLOUD_ADMIN_ORIGIN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
