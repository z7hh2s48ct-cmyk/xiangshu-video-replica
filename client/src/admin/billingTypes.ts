export type BillingTariff = {
  enabled: boolean;
  unit_credits: string | null;
  unit_cost_fen: string | null;
  unit_rounding: "ceil" | "exact";
  version: number;
};

export type BillingService = {
  service: string;
  name: string;
  unit: "second" | "image" | "call";
  provider: string;
  module: string;
  customer_charge_allowed: boolean;
  zero_cost_platform: boolean;
  configured: boolean;
  /** 最近一次费率保存的作者与时间（仅管理端目录回显，未配置为 null）。 */
  updated_at?: string | null;
  updated_by?: string | null;
  tariff: BillingTariff;
  unit_price_fen?: string | null;
  gross_margin_percent?: string | null;
};

export const billingUnit = { second: "秒", image: "张", call: "次" };

export const billingStates: Record<string, string> = {
  PENDING: "处理中 / 待核对",
  SUCCEEDED: "已完成",
  FAILED: "失败已退回",
  CANCELLED: "取消已退回",
  ACTUAL: "已确认",
  UNKNOWN: "待核对",
};
