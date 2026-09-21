/**
 * Tenant management page - Full CRUD operations
 * Features: List, Search, Filter, Pagination, Add/Edit/Delete
 */

import React, { useState, useEffect } from 'react';
import { Plus, Search, Edit, Trash2, X, Save } from 'lucide-react';
import { tenantApi } from '@/api/tenant.api';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import type { Tenant, TenantCreate, TenantUpdate } from '@/types/tenant.types';

export const TenantsPage: React.FC = () => {
  const queryClient = useQueryClient();
  
  // State
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [searchTerm, setSearchTerm] = useState('');
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [isAddModalOpen, setIsAddModalOpen] = useState(false);
  const [isEditModalOpen, setIsEditModalOpen] = useState(false);
  const [selectedTenant, setSelectedTenant] = useState<Tenant | null>(null);
  
  // Form state
  const [formData, setFormData] = useState<TenantCreate | TenantUpdate>({
    tenant_id: '',
    name: '',
    company_name: null,
    contact_email: null,
    contact_phone: null,
    address: null,
    notes: null,
  });

  // Query
  const { data, isLoading, error } = useQuery({
    queryKey: ['tenants', page, pageSize, searchTerm, statusFilter],
    queryFn: () => tenantApi.list({
      page,
      page_size: pageSize,
      search: searchTerm || undefined,
      status_filter: statusFilter === 'all' ? null : statusFilter as any,
    }),
  });

  // Create mutation
  const createMutation = useMutation({
    mutationFn: (data: TenantCreate) => tenantApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tenants'] });
      setIsAddModalOpen(false);
      resetForm();
    },
  });

  // Update mutation
  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: TenantUpdate }) => 
      tenantApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tenants'] });
      setIsEditModalOpen(false);
      resetForm();
    },
  });

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: (id: string) => tenantApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tenants'] });
    },
  });

  // Handlers
  const handleSearch = (term: string) => {
    setSearchTerm(term);
    setPage(1);
  };

  const handleStatusChange = (filter: string) => {
    setStatusFilter(filter);
    setPage(1);
  };

  const openEditModal = (tenant: Tenant) => {
    setSelectedTenant(tenant);
    setFormData({
      name: tenant.name,
      company_name: tenant.company_name,
      contact_email: tenant.contact_email,
      contact_phone: tenant.contact_phone,
      address: tenant.address,
      notes: tenant.notes,
    });
    setIsEditModalOpen(true);
  };

  const openAddModal = () => {
    resetForm();
    setIsAddModalOpen(true);
  };

  const resetForm = () => {
    setFormData({
      tenant_id: '',
      name: '',
      company_name: null,
      contact_email: null,
      contact_phone: null,
      address: null,
      notes: null,
    });
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    
    if (selectedTenant) {
      updateMutation.mutate({ id: selectedTenant.id, data: formData });
    } else {
      createMutation.mutate(formData as TenantCreate);
    }
  };

  const handleDelete = (tenantId: string) => {
    if (confirm('Are you sure you want to soft delete this tenant?')) {
      deleteMutation.mutate(tenantId);
    }
  };

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-light text-gray-900">Tenants</h1>
          <p className="text-sm text-gray-600 mt-1">Manage your franchisee accounts</p>
        </div>
        <button
          onClick={openAddModal}
          className="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg flex items-center gap-2 transition-colors"
        >
          <Plus className="w-4 h-4" />
          <span>Add New Tenant</span>
        </button>
      </div>

      {/* Search and Filter Bar */}
      <div className="bg-white p-4 rounded-lg border border-gray-200 shadow-sm">
        <div className="flex gap-4">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-5 h-5 text-gray-400" />
            <input
              type="text"
              placeholder="Search by name, tenant ID, or email..."
              value={searchTerm}
              onChange={(e) => handleSearch(e.target.value)}
              className="w-full pl-10 pr-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          
          <select
            value={statusFilter}
            onChange={(e) => handleStatusChange(e.target.value)}
            className="px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <option value="all">All Statuses</option>
            <option value="pending">Pending</option>
            <option value="active">Active</option>
            <option value="suspended">Suspended</option>
            <option value="deactivated">Deactivated</option>
          </select>
        </div>
      </div>

      {/* Loading & Error States */}
      {isLoading && (
        <div className="bg-white p-8 text-center rounded-lg border border-gray-200">
          <p className="text-gray-500">Loading tenants...</p>
        </div>
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 p-4 rounded-lg">
          <p>Error loading tenants. Please try again.</p>
        </div>
      )}

      {/* Data Table */}
      {!isLoading && !error && data && (
        <div className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden">
          <table className="w-full">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="text-left px-4 py-3 text-sm font-medium text-gray-700">Tenant ID</th>
                <th className="text-left px-4 py-3 text-sm font-medium text-gray-700">Name</th>
                <th className="text-left px-4 py-3 text-sm font-medium text-gray-700">Contact Email</th>
                <th className="text-left px-4 py-3 text-sm font-medium text-gray-700">Status</th>
                <th className="text-left px-4 py-3 text-sm font-medium text-gray-700">Balance (Yuan)</th>
                <th className="text-right px-4 py-3 text-sm font-medium text-gray-700">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {data.items.map((tenant) => (
                <tr key={tenant.id} className="hover:bg-gray-50 transition-colors">
                  <td className="px-4 py-3 text-sm text-gray-900">{tenant.tenant_id}</td>
                  <td className="px-4 py-3 text-sm text-gray-900">{tenant.name}</td>
                  <td className="px-4 py-3 text-sm text-gray-600">{tenant.contact_email || '-'}</td>
                  <td className="px-4 py-3">
                    <span
                      className={`inline-block px-2 py-1 text-xs rounded-full ${
                        tenant.status === 'active'
                          ? 'bg-green-50 text-green-700'
                          : tenant.status === 'pending'
                          ? 'bg-yellow-50 text-yellow-700'
                          : tenant.status === 'suspended'
                          ? 'bg-red-50 text-red-700'
                          : 'bg-gray-50 text-gray-700'
                      }`}
                    >
                      {tenant.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-900 font-medium">
                    ¥{tenant.balance_yuan.toFixed(2)}
                  </td>
                  <td className="px-4 py-3 text-right space-x-2">
                    <button
                      onClick={() => openEditModal(tenant)}
                      className="text-blue-600 hover:text-blue-700"
                    >
                      <Edit className="w-4 h-4" />
                    </button>
                    <button
                      onClick={() => handleDelete(tenant.id)}
                      className="text-red-600 hover:text-red-700"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {/* Empty State */}
          {data.items.length === 0 && (
            <div className="p-8 text-center">
              <p className="text-gray-500">No tenants found.</p>
            </div>
          )}

          {/* Pagination */}
          {data.total_pages > 1 && (
            <div className="flex items-center justify-between px-4 py-3 bg-gray-50 border-t border-gray-200">
              <p className="text-sm text-gray-600">
                Showing {((page - 1) * pageSize) + 1}-{Math.min(page * pageSize, data.total)} of {data.total}
              </p>
              <div className="flex gap-2">
                <button
                  onClick={() => setPage(p => Math.max(1, p - 1))}
                  disabled={page === 1}
                  className="px-3 py-1 text-sm border border-gray-300 rounded disabled:opacity-50"
                >
                  Previous
                </button>
                <span className="px-3 py-1 text-sm text-gray-600">
                  Page {page} of {data.total_pages}
                </span>
                <button
                  onClick={() => setPage(p => Math.min(data.total_pages, p + 1))}
                  disabled={page >= data.total_pages}
                  className="px-3 py-1 text-sm border border-gray-300 rounded disabled:opacity-50"
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Add/Edit Modal */}
      {(isAddModalOpen || isEditModalOpen) && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-lg shadow-xl max-w-2xl w-full p-6">
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-xl font-medium text-gray-900">
                {selectedTenant ? 'Edit Tenant' : 'Add New Tenant'}
              </h2>
              <button
                onClick={() => {
                  setIsAddModalOpen(false);
                  setIsEditModalOpen(false);
                  resetForm();
                }}
                className="text-gray-500 hover:text-gray-700"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Tenant ID *
                  </label>
                  <input
                    type="text"
                    required
                    disabled={!!selectedTenant}
                    value={formData.tenant_id}
                    onChange={(e) => setFormData({...formData, tenant_id: e.target.value})}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                    placeholder="unique_identifier"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Name *
                  </label>
                  <input
                    type="text"
                    required
                    value={formData.name}
                    onChange={(e) => setFormData({...formData, name: e.target.value})}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                    placeholder="Company name"
                  />
                </div>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Company Name
                </label>
                <input
                  type="text"
                  value={formData.company_name || ''}
                  onChange={(e) => setFormData({...formData, company_name: e.target.value || null})}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                  placeholder="Legal company name"
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Contact Email
                  </label>
                  <input
                    type="email"
                    value={formData.contact_email || ''}
                    onChange={(e) => setFormData({...formData, contact_email: e.target.value || null})}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                    placeholder="contact@example.com"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Contact Phone
                  </label>
                  <input
                    type="tel"
                    value={formData.contact_phone || ''}
                    onChange={(e) => setFormData({...formData, contact_phone: e.target.value || null})}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                    placeholder="+86 123 4567 8900"
                  />
                </div>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Address
                </label>
                <textarea
                  rows={3}
                  value={formData.address || ''}
                  onChange={(e) => setFormData({...formData, address: e.target.value || null})}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                  placeholder="Business address"
                />
              </div>

              <div className="flex gap-3 pt-4">
                <button
                  type="submit"
                  disabled={createMutation.isPending || updateMutation.isPending}
                  className="bg-blue-600 hover:bg-blue-700 text-white px-6 py-2 rounded-lg flex items-center gap-2 disabled:opacity-50"
                >
                  <Save className="w-4 h-4" />
                  <span>{selectedTenant ? 'Save Changes' : 'Create Tenant'}</span>
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setIsAddModalOpen(false);
                    setIsEditModalOpen(false);
                    resetForm();
                  }}
                  className="border border-gray-300 text-gray-700 px-6 py-2 rounded-lg hover:bg-gray-50"
                >
                  Cancel
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};
