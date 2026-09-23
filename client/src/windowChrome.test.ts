import { describe, expect, it } from "vitest";
import tauriConfig from "../src-tauri/tauri.conf.json";

describe("desktop window chrome", () => {
  it("keeps the native title bar unnamed", () => {
    expect(tauriConfig.app.windows[0]?.title).toBe("");
  });
});
