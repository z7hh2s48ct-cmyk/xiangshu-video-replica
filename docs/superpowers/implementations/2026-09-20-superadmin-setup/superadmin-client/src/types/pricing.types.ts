/**
 * Pricing configuration type definitions
 */

export interface PricingConfig {
  id: string;
  tenant_id: string;
  name: string | null;
  subject: 'video_768p' | 'video_2k' | 'oral';
  base_price_fen: number;
  effective_price_fen: number;
  effective_price_yuan: number;
  discount_percent: number;
  currency: string;
  exchange_rate: number;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface PricingCreate {
  tenant_id: string;
  subject: 'video_768p' | 'video_2k' | 'oral';
  base_price_fen: number;
  discount_percent?: number;
  currency?: string;
  exchange_rate?: number;
  notes?: string | null;
}

export interface PricingUpdate {
  base_price_fen?: number;
  discount_percent?: number;
  exchange_rate?: number;
  is_active?: boolean;
  notes?: string | null;
}
