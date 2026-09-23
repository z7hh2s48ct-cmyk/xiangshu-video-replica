/** Decimal-string conversion avoids changing sub-cent prices through floats. */
export function convertBillingAmount(
  value: string,
  multiplier: number,
  divisor: number,
  digits: number,
  round = false,
) {
  if (!/^\d+(\.\d+)?$/.test(value) || value.length > 80)
    throw new Error("请输入非负金额。");
  if (
    !Number.isSafeInteger(multiplier) ||
    multiplier < 1 ||
    !Number.isSafeInteger(divisor) ||
    divisor < 1
  )
    throw new Error("请先设置充值换算。");
  const [whole, fraction = ""] = value.split(".");
  const numerator =
    BigInt(whole + fraction) * BigInt(multiplier) * 10n ** BigInt(digits);
  const denominator = BigInt(divisor) * 10n ** BigInt(fraction.length);
  const scaled = (numerator + (round ? denominator / 2n : 0n)) / denominator;
  const padded = scaled.toString().padStart(digits + 1, "0");
  const decimals = padded.slice(-digits).replace(/0+$/, "");
  return {
    value: `${padded.slice(0, -digits)}${decimals ? `.${decimals}` : ""}`,
    exact: numerator % denominator === 0n,
  };
}

export function costInCredits(
  value: string | null,
  pointsPerYuan: number | null,
) {
  return value === null || !pointsPerYuan
    ? ""
    : convertBillingAmount(value, pointsPerYuan, 100, 8).value;
}

export function creditsToCost(value: string, pointsPerYuan: number) {
  const result = convertBillingAmount(value, 100, pointsPerYuan, 6, true);
  if (result.value === "0" && /[1-9]/.test(value))
    throw new Error("成本过小，超出支持的精度。");
  return result;
}
