/**
 * Pricing configuration API services
 */

import { apiClient } from './client';
import { PricingConfig, PricingCreate, PricingUpdate } from '../types/pricing.types';

export const pricingApi = {
  /**
   * List all pricing configurations with optional filters
   */
  async list(params?: {
    tenant_id?: string;
    subject?: string;
    is_active_only?: boolean;
  }): Promise<PricingConfig[]> {
    const response = await apiClient.get<PricingConfig[]>('/pricing', { params });
    return response.data;
  },

  /**
   * Get specific pricing configuration
   */
  async get(pricingId: string): Promise<PricingConfig> {
    const response = await apiClient.get<PricingConfig>(`/pricing/${pricingId}`);
    return response.data;
  },

  /**
   * Create new pricing configuration
   */
  async create(data: PricingCreate): Promise<PricingConfig> {
    const response = await apiClient.post<PricingConfig>('/pricing', data);
    return response.data;
  },

  /**
   * Update pricing configuration
   */
  async update(pricingId: string, data: PricingUpdate): Promise<PricingConfig> {
    const response = await apiClient.put<PricingConfig>(`/pricing/${pricingId}`, data);
    return response.data;
  },

  /**
   * Delete (soft) pricing configuration
   */
  async delete(pricingId: string): Promise<void> {
    await apiClient.delete(`/pricing/${pricingId}`);
  },
};
