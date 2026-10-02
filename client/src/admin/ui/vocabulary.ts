// 管理端统一词典 —— 状态、角色、金额与时间的唯一翻译来源。
//
// 规则（2026-09-02 管理端评估 §交互规范）：
// 1. 页面不得再写字面量状态映射表；新状态先在这里登记。
// 2. credits 统一按积分展示，任务计数仍用条。
// 3. 金额一律 `¥xx.xx`（分位保留）——此前的 `Math.floor` 会把 100.50 元
//    显示成 100 元，属数据失真，已修复。
// 4. REVOKED 按域区分：激活码和客户为“已撤销”，设备为“永久禁用”。
// 5. 管理端按原方案显示“生成冻结 / 生成扣费 / 失败退回 / 退款扣减”；
//    只统一展示用语，存储类型、积分精度和客户端词典保持原有契约。
//    金额不足 1 分显示“< ¥0.01”，不得四舍五入成 ¥0.01 虚报。
// 6. 最近活动按分钟、小时、天显示相对时间，其余时间使用北京时间。

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
  REVOKED: "永久禁用",
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
  CONVERSION: "历史转换",
  CHARGE: "充值到账",
  // P2-1：冻结/结算/释放太抽象，运营要能一眼看出这笔钱是哪一步产生的。
  // 管理端依照改造方案展示生成冻结、生成扣费和失败退回。
  RESERVE: "生成冻结",
  SETTLE: "生成扣费",
  RELEASE: "失败退回",
  // B1：审计调账的反向记账类型（20260923T1200），金额为负、不挂充值单。
  REFUND: "退款扣减",
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
  ORAL_AVATAR: "口播分身",
  ORAL_VOICE: "声音克隆",
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

/** 失败的原因分类，与服务端 failure_runbook.FailureCategory 一一对应。
 *
 * 回答的是「这条失败该往哪边归类」——比建议文本更适合先筛一遍再分工。
 */
export const FAILURE_CATEGORY_LABELS: LabelMap = {
  UNCLASSIFIED: "未归类",
  CUSTOMER_ASSET: "客户素材",
  CONTENT_REVIEW: "内容审核",
  PROVIDER_BUSY: "系统繁忙",
  PROVIDER_FAULT: "服务商故障",
  CONFIG: "配置问题",
  DEFECT: "系统缺陷",
  // 主动取消、检查点自愈、对账已恢复这类**流程状态**：不是故障，不需要按故障处理。
  NOT_A_FAILURE: "无需处理（流程状态）",
};

/** 失败的处理人，与服务端 failure_runbook.FailureOwner 一一对应。 */
export const FAILURE_OWNER_LABELS: LabelMap = {
  SUPPORT: "客服告知客户",
  OPS: "运营重试",
  ENGINEERING: "技术处理",
};

/** 单次拆解尝试的终局，与 analysis_task_attempts.status 一一对应。 */
export const ANALYSIS_ATTEMPT_STATUS_LABELS: LabelMap = {
  FAILED: "失败",
  INTERRUPTED: "执行中断",
  SUPERSEDED: "已被重试取代",
};

/**
 * 内容状态（方案 P1 内容模块）：归档×首页×可见性三套底层状态收敛为运营
 * 只看的 6 态。键是状态 id，值是界面文案；判定函数在视频库页
 * （contentStateOf），词典只管文案，避免循环依赖。
 */
export const CONTENT_STATE_LABELS: LabelMap = {
  pending_prepare: "待准备",
  prepare_failed: "准备失败",
  ready: "可上首页",
  featured: "首页展示中",
  removed: "已下架",
  blocked: "已屏蔽",
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
 * 生成状态筛选项：15 个底层状态收敛为运营可读的 5 组（方案 P1 生成记录
 * 改造：排队中 / 生成中 / 成功 / 失败 / 需人工核对），值为逗号拼接的全部
 * 底层状态——服务端状态筛选接受逗号分隔的多值，两种「已取消」拼写与
 * UNKNOWN / SUBMISSION_UNCERTAIN 分别在组内合并（P0-6 的口径延续）。
 */
export const GENERATION_STATUS_FILTERS: Array<{
  value: string;
  label: string;
}> = [
  { value: "CREATED,QUEUED,PENDING", label: "排队中" },
  {
    value: "SUBMITTING,SUBMITTED,RUNNING,RETRYING,ARCHIVING",
    label: "生成中",
  },
  { value: "SUCCEEDED", label: "成功" },
  {
    value: "FAILED,CANCELED,CANCELLED,ARCHIVE_FAILED",
    label: "失败",
  },
  { value: "UNKNOWN,SUBMISSION_UNCERTAIN", label: "需人工核对" },
];

/** Main generation view uses the same five groups as its filter. */
export function generationStatusGroupLabel(status: string): string {
  return (
    GENERATION_STATUS_FILTERS.find(({ value }) =>
      value.split(",").includes(status),
    )?.label ?? "需人工核对"
  );
}

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

/**
 * 精确的分为元展示：不丢分位（¥10050 → "¥100.50"）。
 *
 * 按售价折合等场景会出现不足 1 分的非零值（0 < fen < 1）：四舍五入成
 * "¥0.01" 会虚报金额，显示 "¥0.00" 会谎称零成本，用 "< ¥0.01" 如实表达量级。
 */
export function formatFen(fen: number): string {
  if (fen > 0 && fen < 1) return "< ¥0.01";
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

/**
 * 列表里的"最近活动"用相对时间；超过 7 天回退到绝对时间（formatDateTime）。
 *
 * 活动间隔几秒到几天都有，绝对时间串要运营自己做减法；相对时间一眼可读。
 * 但太久远的记录（> 7 天）相对数字反而失去意义，回退绝对时间更精确。
 */
export function formatRelativeTime(
  value: string | null | undefined,
  now: number = Date.now(),
): string {
  if (!value) {
    return "—";
  }
  const timestamp = parseUtcTimestamp(value);
  if (Number.isNaN(timestamp)) {
    return "—";
  }
  const seconds = Math.floor((now - timestamp) / 1000);
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
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

/** 账户里正在暂扣、尚未结算的额度叫法，与客户端 HELD_CREDITS_LABEL 一致。 */
export const HELD_CREDITS_LABEL = "生成冻结";

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
