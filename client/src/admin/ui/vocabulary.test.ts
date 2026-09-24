import { describe, expect, it } from "vitest";

import {
  activationCodeStatusLabel,
  customerStatusLabel,
  deviceStatusLabel,
  formatDateTime,
  formatFen,
  formatYuanFromFen,
  labelFrom,
  ledgerExportMessage,
  rechargeOrderStatusLabel,
  shanghaiDate,
} from "./vocabulary";

describe("vocabulary", () => {
  it("uses Shanghai midnight and calendar offsets", () => {
    expect(shanghaiDate(0, new Date("2026-09-11T15:59:59Z"))).toBe(
      "2026-09-11",
    );
    expect(shanghaiDate(0, new Date("2026-09-11T16:00:00Z"))).toBe(
      "2026-09-12",
    );
    expect(shanghaiDate(1, new Date("2026-12-31T17:00:00Z"))).toBe(
      "2027-01-02",
    );
  });
  it("makes limited exports and unknown completeness explicit", () => {
    expect(
      ledgerExportMessage({ total: 5001, returned: 5000, truncated: true }),
    ).toContain("仅导出 5000 条");
    expect(
      ledgerExportMessage({ total: 3, returned: 3, truncated: false }),
    ).toContain("已全部导出");
    expect(ledgerExportMessage(null)).toContain("完整性待核对");
  });
  it("maps every known activation code status and falls back to the raw value", () => {
    expect(activationCodeStatusLabel("GENERATED")).toBe("待启用");
    expect(activationCodeStatusLabel("ISSUED")).toBe("可使用");
    expect(activationCodeStatusLabel("ACTIVE")).toBe("使用中");
    expect(activationCodeStatusLabel("SUSPENDED")).toBe("已暂停");
    expect(activationCodeStatusLabel("REVOKED")).toBe("已撤销");
    expect(activationCodeStatusLabel("EXPIRED")).toBe("已过期");
    expect(activationCodeStatusLabel("FUTURE_STATE")).toBe("FUTURE_STATE");
  });

  it("uses one REVOKED word per domain, not three", () => {
    // 激活码撤销与设备强制退出是两个动作：设备侧沿用操作动词，不再出现"已退出"。
    expect(deviceStatusLabel("REVOKED")).toBe("已强制退出");
    expect(deviceStatusLabel("BOUND")).toBe("已绑定");
    expect(deviceStatusLabel("UNBOUND")).toBe("已解绑");
    expect(customerStatusLabel("REVOKED")).toBe("已撤销");
    expect(customerStatusLabel("active")).toBe("活跃");
  });

  it("formats fen as an exact yuan amount with cents", () => {
    expect(formatFen(10050)).toBe("¥100.50");
    expect(formatFen(10000)).toBe("¥100.00");
    expect(formatFen(5)).toBe("¥0.05");
    expect(formatFen(99)).toBe("¥0.99");
  });

  it("keeps the yuan input helpers exact too", () => {
    expect(formatYuanFromFen(1050)).toBe("10.50");
  });

  it("formats timestamps in zh-CN and degrades gracefully", () => {
    const formatted = formatDateTime("2026-09-01T12:30:00+00:00");
    expect(formatted).not.toBe("—");
    expect(formatted).toMatch(/2026/);
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime("not-a-date")).toBe("—");
    expect(formatDateTime("2026-09-01 12:30:00")).toBe(
      formatDateTime("2026-09-01T12:30:00Z"),
    );
  });

  it("falls back to the raw value for unknown dictionary keys", () => {
    expect(labelFrom({ A: "甲" }, "B")).toBe("B");
    expect(labelFrom({ A: "甲" }, "A")).toBe("甲");
    expect(rechargeOrderStatusLabel("PENDING")).toBe("待支付");
  });
});
