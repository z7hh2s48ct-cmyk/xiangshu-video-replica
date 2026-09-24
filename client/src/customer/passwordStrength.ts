/**
 * 密码强度提示（审计 P1 清单 #12）。
 *
 * **不改服务端策略**：`password_hashing` 的下限仍是 6 位、上限 128，这条只是把
 * 「你现在这串大概有多结实」实时告诉用户，并推荐 ≥8 位。改下限会动共享常量与一片
 * 既有测试，属另一件事（本轮刻意排除，见进度文档）。
 */
export type PasswordStrength = {
  /** 0 = 太短（还不满足下限）, 1 = 弱, 2 = 中, 3 = 强 */
  score: 0 | 1 | 2 | 3;
  label: string;
  hint: string;
};

export const RECOMMENDED_PASSWORD_LENGTH = 8;

/** 纯函数：长度为主、字符种类为辅，够用即可（不做字典/泄露库检查）。 */
export function passwordStrength(password: string): PasswordStrength {
  if (password.length === 0) {
    return { score: 0, label: "", hint: "" };
  }
  if (password.length < 6) {
    return {
      score: 0,
      label: "太短",
      hint: "至少 6 位，推荐 8 位以上",
    };
  }
  const kinds = [
    /[a-z]/.test(password),
    /[A-Z]/.test(password),
    /\d/.test(password),
    /[^A-Za-z0-9]/.test(password),
  ].filter(Boolean).length;

  if (password.length < RECOMMENDED_PASSWORD_LENGTH || kinds <= 1) {
    return {
      score: 1,
      label: "弱",
      hint: "再加长一些，或混入大小写、数字、符号",
    };
  }
  if (password.length < 12 || kinds === 2) {
    return { score: 2, label: "中", hint: "可以用；再长一点会更稳" };
  }
  return { score: 3, label: "强", hint: "很好" };
}
