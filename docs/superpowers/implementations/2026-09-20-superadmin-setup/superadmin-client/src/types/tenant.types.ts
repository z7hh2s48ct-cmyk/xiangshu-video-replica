/**
 * Tenant-related type definitions
 */

export interface Tenant {
  id: string;
  tenant_id: string;
  name: string;
  company_name: string | null;
  contact_email: string | null;
  contact_phone: string | null;
  address: string | null;
  status: 'pending' | 'active' | 'suspended' | 'deactivated';
  balance_fen: number;
  balance_yuan: number;
  total_recharge_fen: number;
  settings: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface TenantCreate {
  tenant_id: string;
  name: string;
  company_name?: string | null;
  contact_email?: string | null;
  contact_phone?: string | null;
  address?: string | null;
  initial_balance_fen?: number;
  notes?: string | null;
}

export interface TenantUpdate {
  name?: string;
  company_name?: string | null;
  contact_email?: string | null;
  contact_phone?: string | null;
  address?: string | null;
  notes?: string | null;
}

export interface TenantStatusUpdate {
  status: 'pending' | 'active' | 'suspended' | 'deactivated';
  note?: string | null;
}

export interface TenantListParams {
  page?: number;
  page_size?: number;
  search?: string | null;
  status_filter?: 'pending' | 'active' | 'suspended' | 'deactivated' | null;
}

export interface TenantListResponse {
  items: Tenant[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}
