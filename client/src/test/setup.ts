import "@testing-library/jest-dom/vitest";

import { cleanup, configure } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

// The full suite runs many jsdom workers at once. Give async effects enough time
// to settle under CI load while preserving the default polling behavior.
// CI 实测：负载峰值下报价弹窗/重试调度可超过 3s（StudioWorkspace 口播家族
// 在零 client diff 的分支上连续失败），10s 为上限保护而非预期等待时长。
configure({ asyncUtilTimeout: 10000 });

// CW-015: 测试环境需要显式配置 API base URL，不再依赖 loopback fallback
beforeEach(() => {
  vi.stubEnv("VITE_API_BASE_URL", "http://127.0.0.1:8000");
});

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
});
