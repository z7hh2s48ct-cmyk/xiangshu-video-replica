/**
 * Tenant management API services
 */

import { apiClient } from './client';
import { 
  Tenant, 
  TenantCreate, 
  TenantUpdate, 
  TenantListParams, 
  TenantListResponse 
} from '../types/tenant.types';

export const tenantApi = {
  /**
   * Get paginated list of tenants with optional filtering
   */
  async list(params?: TenantListParams): Promise<TenantListResponse> {
    const response = await apiClient.get<TenantListResponse>('/tenants', { params });
    return response.data;
  },

  /**
   * Get detailed information about a specific tenant
   */
  async get(tenantId: string): Promise<Tenant> {
    const response = await apiClient.get<Tenant>(`/tenants/${tenantId}`);
    return response.data;
  },

  /**
   * Create a new tenant
   */
  async create(data: TenantCreate): Promise<Tenant> {
    const response = await apiClient.post<Tenant>('/tenants', data);
    return response.data;
  },

  /**
   * Update tenant basic information
   */
  async update(tenantId: string, data: TenantUpdate): Promise<Tenant> {
    const response = await apiClient.put<Tenant>(`/tenants/${tenantId}`, data);
    return response.data;
  },

  /**
   * Change tenant status
   */
  async updateStatus(
    tenantId: string, 
    data: { status: string; note?: string }
  ): Promise<Tenant> {
    const response = await apiClient.patch<Tenant>(`/tenants/${tenantId}/status`, data);
    return response.data;
  },

  /**
   * Soft delete a tenant
   */
  async delete(tenantId: string): Promise<void> {
    await apiClient.delete(`/tenants/${tenantId}`);
  },

  /**
   * Bulk import tenants from CSV/Excel data
   */
  async bulkImport(data: Buffer | FormData): Promise<{ created: number; failed: number }> {
    const formData = new FormData();
    formData.append('file', data);
    
    const response = await apiClient.post<{ created: number; failed: number }>(
      '/tenants/bulk-import', 
      formData,
      { headers: { 'Content-Type': 'multipart/form-data' } }
    );
    
    return response.data;
  },
};
