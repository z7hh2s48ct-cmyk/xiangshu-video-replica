/**
 * Recharge Order type definitions
 */

export interface RechargeOrder {
  id: string;
  order_no: string;
  tenant_id: string;
  amount_fen: number;
  amount_yuan: number;
  actual_payment_cents: number;
  points_ratio: number;
  bonus_points_fen: number;
  status: 'pending' | 'paid' | 'failed' | 'refunded' | 'cancelled';
  payment_method: 'wechat' | 'alipay' | 'bank_transfer' | null;
  payment_reference: string | null;
  note: string | null;
  paid_at: string | null;
  expired_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface OrderCreate {
  order_no: string;
  tenant_id: string;
  amount_fen: number;
  amount_yuan: number;
  actual_payment_cents?: number;
  points_ratio?: number;
  bonus_points_fen?: number;
  status?: 'pending' | 'paid' | 'failed' | 'refunded' | 'cancelled';
  payment_method?: 'wechat' | 'alipay' | 'bank_transfer' | null;
  payment_reference?: string | null;
  note?: string | null;
}

export interface OrderListParams {
  page?: number;
  page_size?: number;
  search?: string | null;
  status_filter?: 'pending' | 'paid' | 'failed' | 'refunded' | 'cancelled' | null;
  tenant_id?: string | null;
  date_from?: string | null;
  date_to?: string | null;
}

export interface OrderListResponse {
  items: RechargeOrder[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}
