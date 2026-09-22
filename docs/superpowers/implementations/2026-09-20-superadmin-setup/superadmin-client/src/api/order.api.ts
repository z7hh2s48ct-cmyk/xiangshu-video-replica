/**
 * Recharge Order API services
 */

import { apiClient } from './client';
import { RechargeOrder, OrderCreate, OrderListParams, OrderListResponse } from '../types/order.types';

export const orderApi = {
  /**
   * Get paginated list of recharge orders with filtering
   */
  async list(params?: OrderListParams): Promise<OrderListResponse> {
    const response = await apiClient.get<OrderListResponse>('/recharge-orders', { params });
    return response.data;
  },

  /**
   * Get detailed information about a specific order
   */
  async get(orderId: string): Promise<RechargeOrder> {
    const response = await apiClient.get<RechargeOrder>(`/recharge-orders/${orderId}`);
    return response.data;
  },

  /**
   * Create a new recharge order
   */
  async create(data: OrderCreate): Promise<RechargeOrder> {
    const response = await apiClient.post<RechargeOrder>('/recharge-orders', data);
    return response.data;
  },

  /**
   * Update order status (e.g., mark as paid)
   */
  async updateStatus(
    orderId: string, 
    data: { status: string; payment_reference?: string; note?: string }
  ): Promise<RechargeOrder> {
    const response = await apiClient.patch<RechargeOrder>(`/recharge-orders/${orderId}/status`, data);
    return response.data;
  },

  /**
   * Manually verify and confirm payment
   */
  async verifyPayment(orderId: string): Promise<RechargeOrder> {
    const response = await apiClient.post<RechargeOrder>(`/recharge-orders/${orderId}/verify-payment`);
    return response.data;
  },

  /**
   * Cancel a pending order
   */
  async cancel(orderId: string): Promise<RechargeOrder> {
    const response = await apiClient.patch<RechargeOrder>(`/recharge-orders/${orderId}/cancel`);
    return response.data;
  },
};
