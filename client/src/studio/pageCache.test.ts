import { afterEach, describe, expect, it } from "vitest";
import { clearPageCache, readPageCache, writePageCache } from "./pageCache";

describe("页间 SWR 缓存", () => {
  afterEach(() => clearPageCache());

  it("写入后可读取，未写入返回 undefined", () => {
    expect(readPageCache("k1")).toBeUndefined();
    writePageCache("k1", { total: 7 });
    expect(readPageCache("k1")).toEqual({ total: 7 });
  });

  it("前缀清理只删除同账号键", () => {
    writePageCache("publish-summary:user-1", 3);
    writePageCache("publish-summary:user-2", 5);
    writePageCache("viral:user-1", []);
    clearPageCache("publish-summary:user-1");
    expect(readPageCache("publish-summary:user-1")).toBeUndefined();
    expect(readPageCache("publish-summary:user-2")).toBe(5);
    expect(readPageCache("viral:user-1")).toEqual([]);
  });

  it("容量上限时淘汰最早的键", () => {
    for (let index = 0; index < 64; index += 1) {
      writePageCache(`key-${index}`, index);
    }
    writePageCache("key-64", 64);
    expect(readPageCache("key-0")).toBeUndefined();
    expect(readPageCache("key-64")).toBe(64);
    expect(readPageCache("key-1")).toBe(1);
  });
});
