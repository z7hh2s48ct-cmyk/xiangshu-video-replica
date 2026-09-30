import type { WalletTransaction } from "../api";

/** 入账来源（`credit_source`）→ 客户看得懂的名字；未知来源统一叫「后台入账」。 */
const CREDIT_SOURCE_LABEL: Record<string, string> = {
  FREE_GRANT: "积分赠送",
  CREDIT_COMPENSATION: "积分补偿",
  OFFLINE_PAYMENT: "套餐充值",
  zpay: "在线充值",
  wechat_native: "微信充值",
  activation_code: "账号激活",
  FINANCE_RECEIPT: "后台入账",
  COMPENSATION_APPROVAL: "后台调整",
};

/** 计费周期内的三种流水；其余（入账、转换、调账）不是「花钱」。 */
const CYCLE_TYPES = new Set<WalletTransaction["type"]>([
  "RESERVE",
  "SETTLE",
  "RELEASE",
]);

/** 业务描述：优先具体服务名，退到任务类型。 */
export function businessDescription(item: WalletTransaction): string {
  if (item.service_name) return item.service_name;
  if (item.type === "CONVERSION") return "历史余额";
  if (item.oral_task_id) return "数字人口播";
  if (item.task_id) return "视频生成";
  return "充值 / 赠送";
}

/**
 * 这一笔从哪里来：入账看入账来源；消费看是软件操作还是哪一枚 Token。
 *
 * 入账与调账行没有 Token / 会话来源，不能落到「早期版本消费」这个兜底上——
 * 那句话的意思是「早期没记来源的消费」，用在入账上会让客户以为自己的充值是消费。
 */
export function ledgerSourceLabel(item: WalletTransaction): string {
  if (!CYCLE_TYPES.has(item.type)) {
    if (item.credit_source) {
      return CREDIT_SOURCE_LABEL[item.credit_source] ?? "后台入账";
    }
    return item.type === "REFUND" ? "后台调整" : "后台入账";
  }
  if (item.credit_source) {
    return CREDIT_SOURCE_LABEL[item.credit_source] ?? "后台入账";
  }
  if (item.api_key_id) {
    return `${item.token_label || "Token"} · 第 ${item.credential_version ?? 1} 次更新`;
  }
  if (item.auth_source === "session") return "软件操作";
  if (item.auth_source === "internal") return "内部操作";
  return "早期版本消费";
}

/** 北京时间、24 小时制的完整时间；无法解析时原样返回，不显示成 Invalid Date。 */
export function formatLedgerTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false,
  });
}

/** 只要时分秒，给「提交于」这类同一天内的次要时间用。 */
export function formatLedgerClock(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleTimeString("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false,
  });
}

export function signedCredits(value: number): string {
  return `${value > 0 ? "+" : ""}${value} 积分`;
}
