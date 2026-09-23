import { expect, test } from "vitest";
import {
  passwordStrength,
  RECOMMENDED_PASSWORD_LENGTH,
} from "./passwordStrength";

test("空串不显示任何强度文案", () => {
  expect(passwordStrength("")).toEqual({ score: 0, label: "", hint: "" });
});

test("短于服务端下限（6 位）判为太短，并给出下限与推荐长度", () => {
  const result = passwordStrength("12345");
  expect(result.score).toBe(0);
  expect(result.label).toBe("太短");
  expect(result.hint).toContain("6");
  expect(result.hint).toContain(String(RECOMMENDED_PASSWORD_LENGTH));
});

test("刚好到下限但结构单薄判为弱", () => {
  expect(passwordStrength("abcdef").score).toBe(1);
});

test("够长且字符种类多判为中/强", () => {
  expect(passwordStrength("abcdefgh").score).toBe(1); // 8 位但只有小写
  expect(passwordStrength("abcdefg1").score).toBe(2); // 8 位 + 两种字符
  expect(passwordStrength("Abcdefg1!xyz").score).toBe(3); // 12 位 + 四种字符
});

test("强度只升不降：同样字符种类下加长不会变弱", () => {
  const short = passwordStrength("abc123");
  const long = passwordStrength("abc123456789");
  expect(long.score).toBeGreaterThanOrEqual(short.score);
});
