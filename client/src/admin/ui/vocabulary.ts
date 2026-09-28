// 管理端统一词典 —— 状态、角色、金额与时间的唯一翻译来源。
//
// 规则（2026-09-02 管理端评估 §交互规范）：
// 1. 页面不得再写字面量状态映射表；新状态先在这里登记。
// 2. credits 统一按积分展示，任务计数仍用条。
// 3. 金额一律 `¥xx.xx`（分位保留）——此前的 `Math.floor` 会把 100.50 元
//    显示成 100 元，属数据失真，已修复。
// 4. REVOKED 按域区分动词：激活码"已撤销"、设备"已强制退出"（沿用操作
//    动词），客户沿用其激活码口径"已撤销"。

type LabelMap = Record<string, string>;

export const ACTIVATION_CODE_STATUS_LABELS: LabelMap = {
  GENERATED: "待启用",
  ISSUED: "可使用",
  ACTIVE: "使用中",
  SUSPENDED: "已暂停",
  REVOKED: "已撤销",
  EXPIRED: "已过期",
};

export const DEVICE_STATUS_LABELS: LabelMap = {
  ONLINE: "在线",
  OFFLINE: "离线",
  BOUND: "已绑定",
  UNBOUND: "已解绑",
  REVOKED: "已强制退出",
};

export const CUSTOMER_STATUS_LABELS: LabelMap = {
  ACTIVE: "活跃",
  SUSPENDED: "已暂停",
  REVOKED: "已撤销",
};

export const RECHARGE_ORDER_STATUS_LABELS: LabelMap = {
  PENDING: "待支付",
  PAID: "已支付",
  FAILED: "失败",
  CLOSED: "已关闭",
};

export const ROLE_LABELS: LabelMap = {
  admin: "管理员",
  auditor: "审计员",
  customer: "客户",
  employee: "员工",
};

export const TRANSACTION_TYPE_LABELS: LabelMap = {
  CONVERSION: "历史积分转换",
  CHARGE: "充值到账",
  RESERVE: "冻结",
  SETTLE: "结算",
  RELEASE: "释放",
  // B1：审计调账的反向记账类型（20260923T1200），金额为负、不挂充值单。
  REFUND: "退款调账",
};

export const ADJUSTMENT_SOURCE_LABELS: LabelMap = {
  CS_TICKET: "客服工单",
  REFUND_APPROVAL: "退款审批",
  COMPENSATION_APPROVAL: "补偿审批",
  LEDGER_CORRECTION: "账本更正",
  FREE_GRANT: "积分赠送",
  CREDIT_COMPENSATION: "积分补偿",
  OFFLINE_PAYMENT: "线下收款开通套餐",
};

export const PLATFORM_LABELS: LabelMap = {
  windows: "Windows",
  macos: "macOS",
  ios: "iOS",
  android: "Android",
  linux: "Linux",
};

export const GENERATION_RECORD_TYPE_LABELS: LabelMap = {
  VIDEO: "视频生成",
  ORAL_VIDEO: "口播视频",
  FIRST_FRAME_IMAGE: "人物置换首帧",
  CHARACTER_SHEET_IMAGE: "人物五视图",
  CHARACTER_VIEW_IMAGE: "人物单视图",
  SOURCE_FRAME_AI_SCORE: "源画面 AI 评分",
  SOURCE_FRAME_PROCESS: "源画面处理",
  ANALYSIS: "视频拆解",
};

/** 拆解失败的环节，与 analysis_tasks.failure_phase 取值一一对应。 */
export const FAILURE_PHASE_LABELS: LabelMap = {
  request: "请求准备",
  network: "网络连接",
  http: "上游拒绝（HTTP）",
  response: "上游返回不可用",
};

/** 单次拆解尝试的终局，与 analysis_task_attempts.status 一一对应。 */
export const ANALYSIS_ATTEMPT_STATUS_LABELS: LabelMap = {
  FAILED: "失败",
  INTERRUPTED: "执行中断",
  SUPERSEDED: "已被重试取代",
};

export const GENERATION_STATUS_LABELS: LabelMap = {
  CREATED: "已创建",
  QUEUED: "排队中",
  PENDING: "待处理",
  SUBMITTING: "提交中",
  SUBMITTED: "已提交",
  RUNNING: "生成中",
  SUCCEEDED: "成功",
  FAILED: "失败",
  CANCELED: "已取消",
  CANCELLED: "已取消",
  RETRYING: "重试中",
  UNKNOWN: "待核对",
  SUBMISSION_UNCERTAIN: "提交结果待核对",
  ARCHIVING: "归档中",
  ARCHIVE_FAILED: "归档失败",
};

/**
 * 生成状态筛选项：同一中文标签只出现一次，值为逗号拼接的全部底层状态。
 *
 * CANCELED / CANCELLED 两种拼写都映射为「已取消」，逐项渲染会让下拉出现两个
 * 「已取消」（方案 P0-6）；服务端状态筛选接受逗号分隔的多值。
 */
export const GENERATION_STATUS_FILTERS: Array<{
  value: string;
  label: string;
}> = (() => {
  const grouped = new Map<string, string[]>();
  for (const [value, label] of Object.entries(GENERATION_STATUS_LABELS)) {
    grouped.set(label, [...(grouped.get(label) ?? []), value]);
  }
  return [...grouped].map(([label, values]) => ({
    value: values.join(","),
    label,
  }));
})();

/** 查词典并回退到原始值——未知状态原样展示，便于发现新枚举。 */
export function labelFrom(labels: LabelMap, value: string): string {
  return labels[value] ?? value;
}

export function activationCodeStatusLabel(status: string): string {
  return labelFrom(ACTIVATION_CODE_STATUS_LABELS, status);
}

export function deviceStatusLabel(status: string): string {
  return labelFrom(DEVICE_STATUS_LABELS, status);
}

export function customerStatusLabel(status: string): string {
  return labelFrom(CUSTOMER_STATUS_LABELS, status.toUpperCase());
}

export function rechargeOrderStatusLabel(status: string): string {
  return labelFrom(RECHARGE_ORDER_STATUS_LABELS, status);
}

export function roleLabel(role: string): string {
  return labelFrom(ROLE_LABELS, role);
}

export function transactionTypeLabel(type: string): string {
  return labelFrom(TRANSACTION_TYPE_LABELS, type);
}

export function platformLabel(platform: string): string {
  return labelFrom(PLATFORM_LABELS, platform);
}

/** 精确的分为元展示：不丢分位（¥10050 → "¥100.50"）。 */
export function formatFen(fen: number): string {
  return `¥${formatYuanFromFen(fen)}`;
}

/**
 * 带符号的积分展示（"+5 积分" / "-8 积分"）。
 *
 * B1 起调账可以是负向（反向调账），原先写死的 `+{credits} 积分` 会把一笔扣减
 * 渲染成"+-8 积分"——方向和数字各说各话，运营无法一眼分辨。
 */
export function signedCredits(credits: number): string {
  return `${credits > 0 ? "+" : ""}${credits} 积分`;
}

/** 分转数字元字符串，保留两位小数（供金额列与输入框回显使用）。 */
export function formatYuanFromFen(fen: number): string {
  return (fen / 100).toFixed(2);
}

/** 统一时间列格式；null/无法解析的值显示 "—" 而不是抛错。 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  const date = new Date(parseUtcTimestamp(value));
  if (Number.isNaN(date.getTime())) {
    return "—";
  }
  return date.toLocaleString("zh-CN", {
    hour12: false,
    timeZone: "Asia/Shanghai",
  });
}

/** 服务端旧时间列按 UTC 解释，日期展示与耗时计算共用该契约。 */
export function parseUtcTimestamp(value: string): number {
  const timestamp = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(
    value,
  )
    ? `${value.replace(" ", "T")}Z`
    : value;
  return Date.parse(timestamp);
}

/** 钱包额度展示统一后缀。 */
export function formatCredits(count: number | null | undefined): string {
  return `${count ?? 0} 积分`;
}

/** Shanghai calendar date, including day offsets independent of the host timezone. */
export function shanghaiDate(days = 0, now = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    Number(parts.find((item) => item.type === type)?.value);
  return new Date(Date.UTC(part("year"), part("month") - 1, part("day") + days))
    .toISOString()
    .slice(0, 10);
}

export function ledgerExportMessage(
  summary: { total: number; returned: number; truncated: boolean } | null,
): string {
  if (!summary) return "文件已下载，服务端未提供记录统计，完整性待核对。";
  return summary.truncated
    ? `当前筛选共 ${summary.total} 条，本次仅导出 ${summary.returned} 条。请缩小日期范围后分批导出。`
    : `当前筛选共 ${summary.total} 条，已全部导出。`;
}
