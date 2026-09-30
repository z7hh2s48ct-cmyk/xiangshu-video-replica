import { expect, test } from "vitest";
import type { WalletTransaction } from "../api";
import {
  businessDescription,
  formatLedgerClock,
  formatLedgerTime,
  ledgerSourceLabel,
  signedCredits,
} from "./ledgerDisplay";

function tx(overrides: Partial<WalletTransaction>): WalletTransaction {
  return {
    id: "t",
    user_id: "u",
    type: "SETTLE",
    available_delta: 0,
    reserved_delta: 0,
    recharge_order_id: null,
    task_id: null,
    billing_round: 1,
    created_at: "2026-09-30T02:00:00Z",
    ...overrides,
  };
}

test("消费来源：软件操作 / Token 的第 N 次更新 / 内部操作 / 早期版本", () => {
  expect(ledgerSourceLabel(tx({ auth_source: "session" }))).toBe("软件操作");
  expect(ledgerSourceLabel(tx({ auth_source: "internal" }))).toBe("内部操作");
  expect(ledgerSourceLabel(tx({ auth_source: null }))).toBe("早期版本消费");
  expect(
    ledgerSourceLabel(
      tx({ api_key_id: "k", token_label: "剪辑脚本", credential_version: 3 }),
    ),
  ).toBe("剪辑脚本 · 第 3 次更新");
  // 没有名字的 Token 与缺失的版本号都有兜底，不显示 undefined。
  expect(ledgerSourceLabel(tx({ api_key_id: "k" }))).toBe(
    "Token · 第 1 次更新",
  );
});

test("入账与调账不走消费兜底：未知来源叫后台入账，调账叫后台调整", () => {
  expect(ledgerSourceLabel(tx({ type: "CHARGE", credit_source: "zpay" }))).toBe(
    "在线充值",
  );
  expect(
    ledgerSourceLabel(tx({ type: "CHARGE", credit_source: "mystery" })),
  ).toBe("后台入账");
  expect(ledgerSourceLabel(tx({ type: "CHARGE", auth_source: null }))).toBe(
    "后台入账",
  );
  expect(ledgerSourceLabel(tx({ type: "REFUND", auth_source: null }))).toBe(
    "后台调整",
  );
  expect(ledgerSourceLabel(tx({ type: "CONVERSION", auth_source: null }))).toBe(
    "后台入账",
  );
});

test("业务描述：服务名优先，其次口播 / 视频 / 转换，最后兜底", () => {
  expect(businessDescription(tx({ service_name: "语音转写" }))).toBe(
    "语音转写",
  );
  expect(businessDescription(tx({ oral_task_id: "o" }))).toBe("数字人口播");
  expect(businessDescription(tx({ task_id: "t" }))).toBe("视频生成");
  expect(businessDescription(tx({ type: "CONVERSION" }))).toBe("历史余额");
  expect(businessDescription(tx({ type: "CHARGE" }))).toBe("充值 / 赠送");
});

test("时间按北京时间 24 小时制显示，解析不了的原样返回而不是 Invalid Date", () => {
  expect(formatLedgerTime("2026-09-07T02:00:00Z")).toBe("2026/9/7 10:00:00");
  expect(formatLedgerClock("2026-09-07T02:00:05Z")).toBe("10:00:05");
  expect(formatLedgerTime("ledger-page-one")).toBe("ledger-page-one");
  expect(formatLedgerClock("not-a-time")).toBe("not-a-time");
});

test("带符号的积分：正数补加号，负数与零原样", () => {
  expect(signedCredits(5)).toBe("+5 积分");
  expect(signedCredits(-3)).toBe("-3 积分");
  expect(signedCredits(0)).toBe("0 积分");
});
