/**
 * Recharge Orders management page - Full CRUD operations
 */

import React, { useState } from 'react';
import { Search, CheckCircle, XCircle, Eye, FileText } from 'lucide-react';
import { orderApi } from '@/api/order.api';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import type { RechargeOrder, OrderListParams } from '@/types/order.types';

export const RechargeOrdersPage: React.FC = () => {
  const queryClient = useQueryClient();
  
  // State
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [searchTerm, setSearchTerm] = useState('');
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [selectedOrder, setSelectedOrder] = useState<RechargeOrder | null>(null);
  const [isDetailModalOpen, setIsDetailModalOpen] = useState(false);

  // Query
  const { data, isLoading, error } = useQuery({
    queryKey: ['orders', page, pageSize, searchTerm, statusFilter],
    queryFn: () => orderApi.list({
      page,
      page_size: pageSize,
      search: searchTerm || undefined,
      status_filter: statusFilter === 'all' ? null : statusFilter as any,
    }),
  });

  // Verify payment mutation
  const verifyPaymentMutation = useMutation({
    mutationFn: (orderId: string) => orderApi.verifyPayment(orderId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['orders'] });
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

  const openDetailModal = (order: RechargeOrder) => {
    setSelectedOrder(order);
    setIsDetailModalOpen(true);
  };

  const handleVerifyPayment = (orderId: string) => {
    if (confirm('Confirm this payment?')) {
      verifyPaymentMutation.mutate(orderId);
    }
  };

  const getStatusColor = (status: string) => {
    const colors: Record<string, string> = {
      pending: 'bg-yellow-50 text-yellow-700',
      paid: 'bg-green-50 text-green-700',
      failed: 'bg-red-50 text-red-700',
      refunded: 'bg-purple-50 text-purple-700',
      cancelled: 'bg-gray-50 text-gray-700',
    };
    return colors[status] || 'bg-gray-50 text-gray-700';
  };

  const formatCurrency = (fen: number) => {
    return `¥${(fen / 100).toFixed(2)}`;
  };

  const formatDate = (dateStr: string | null) => {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleString('zh-CN');
  };

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-light text-gray-900">Recharge Orders</h1>
          <p className="text-sm text-gray-600 mt-1">Manage tenant credit recharge transactions</p>
        </div>
      </div>

      {/* Search and Filter Bar */}
      <div className="bg-white p-4 rounded-lg border border-gray-200 shadow-sm">
        <div className="flex gap-4">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-5 h-5 text-gray-400" />
            <input
              type="text"
              placeholder="Search by order number or tenant ID..."
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
            <option value="paid">Paid</option>
            <option value="failed">Failed</option>
            <option value="refunded">Refunded</option>
            <option value="cancelled">Cancelled</option>
          </select>
        </div>
      </div>

      {/* Loading & Error States */}
      {isLoading && (
        <div className="bg-white p-8 text-center rounded-lg border border-gray-200">
          <p className="text-gray-500">Loading orders...</p>
        </div>
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 p-4 rounded-lg">
          <p>Error loading orders. Please try again.</p>
        </div>
      )}

      {/* Data Table */}
      {!isLoading && !error && data && (
        <div className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden">
          <table className="w-full">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="text-left px-4 py-3 text-sm font-medium text-gray-700">Order No</th>
                <th className="text-left px-4 py-3 text-sm font-medium text-gray-700">Tenant ID</th>
                <th className="text-left px-4 py-3 text-sm font-medium text-gray-700">Amount</th>
                <th className="text-left px-4 py-3 text-sm font-medium text-gray-700">Payment Method</th>
                <th className="text-left px-4 py-3 text-sm font-medium text-gray-700">Status</th>
                <th className="text-left px-4 py-3 text-sm font-medium text-gray-700">Created At</th>
                <th className="text-right px-4 py-3 text-sm font-medium text-gray-700">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {data.items.map((order) => (
                <tr key={order.id} className="hover:bg-gray-50 transition-colors">
                  <td className="px-4 py-3 text-sm font-mono text-gray-900">{order.order_no}</td>
                  <td className="px-4 py-3 text-sm text-gray-900">{order.tenant_id}</td>
                  <td className="px-4 py-3 text-sm font-medium text-gray-900">{formatCurrency(order.amount_fen)}</td>
                  <td className="px-4 py-3 text-sm text-gray-600">
                    {order.payment_method 
                      ? order.payment_method.charAt(0).toUpperCase() + order.payment_method.slice(1)
                      : '-'
                    }
                  </td>
                  <td className="px-4 py-3">
                    <span className={`inline-block px-2 py-1 text-xs rounded-full ${getStatusColor(order.status)}`}>
                      {order.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-600">{formatDate(order.created_at)}</td>
                  <td className="px-4 py-3 text-right space-x-2">
                    <button
                      onClick={() => openDetailModal(order)}
                      className="text-blue-600 hover:text-blue-700"
                      title="View Details"
                    >
                      <Eye className="w-4 h-4" />
                    </button>
                    {order.status === 'pending' && (
                      <button
                        onClick={() => handleVerifyPayment(order.id)}
                        disabled={verifyPaymentMutation.isPending}
                        className="text-green-600 hover:text-green-700 disabled:opacity-50"
                        title="Verify Payment"
                      >
                        <CheckCircle className="w-4 h-4" />
                      </button>
                    )}
                    {order.status === 'failed' && (
                      <span className="text-gray-400">
                        <XCircle className="w-4 h-4" />
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {/* Empty State */}
          {data.items.length === 0 && (
            <div className="p-8 text-center">
              <p className="text-gray-500">No orders found.</p>
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

      {/* Detail Modal */}
      {selectedOrder && isDetailModalOpen && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-lg shadow-xl max-w-2xl w-full p-6">
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-xl font-medium text-gray-900">Order Details</h2>
              <button
                onClick={() => {
                  setIsDetailModalOpen(false);
                  setSelectedOrder(null);
                }}
                className="text-gray-500 hover:text-gray-700"
              >
                <XCircle className="w-5 h-5" />
              </button>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="p-4 bg-gray-50 rounded-lg">
                <p className="text-xs text-gray-500 mb-1">Order Number</p>
                <p className="font-mono text-sm text-gray-900">{selectedOrder.order_no}</p>
              </div>

              <div className="p-4 bg-gray-50 rounded-lg">
                <p className="text-xs text-gray-500 mb-1">Tenant ID</p>
                <p className="text-sm text-gray-900">{selectedOrder.tenant_id}</p>
              </div>

              <div className="p-4 bg-gray-50 rounded-lg">
                <p className="text-xs text-gray-500 mb-1">Amount (Fen)</p>
                <p className="font-medium text-lg text-gray-900">{selectedOrder.amount_fen.toLocaleString()}</p>
              </div>

              <div className="p-4 bg-gray-50 rounded-lg">
                <p className="text-xs text-gray-500 mb-1">Amount (Yuan)</p>
                <p className="font-medium text-lg text-blue-600">¥{selectedOrder.amount_yuan.toFixed(2)}</p>
              </div>

              <div className="p-4 bg-gray-50 rounded-lg">
                <p className="text-xs text-gray-500 mb-1">Bonus Points</p>
                <p className="text-sm text-gray-900">{selectedOrder.bonus_points_fen?.toLocaleString() || 0} fen</p>
              </div>

              <div className="p-4 bg-gray-50 rounded-lg">
                <p className="text-xs text-gray-500 mb-1">Points Ratio</p>
                <p className="text-sm text-gray-900">{selectedOrder.points_ratio}:1</p>
              </div>

              <div className="p-4 bg-gray-50 rounded-lg">
                <p className="text-xs text-gray-500 mb-1">Payment Method</p>
                <p className="text-sm text-gray-900 capitalize">{selectedOrder.payment_method || '-'}</p>
              </div>

              <div className="p-4 bg-gray-50 rounded-lg">
                <p className="text-xs text-gray-500 mb-1">Status</p>
                <span className={`inline-block px-2 py-1 text-xs rounded-full ${getStatusColor(selectedOrder.status)}`}>
                  {selectedOrder.status}
                </span>
              </div>

              <div className="p-4 bg-gray-50 rounded-lg col-span-2">
                <p className="text-xs text-gray-500 mb-1">Created At</p>
                <p className="text-sm text-gray-900">{formatDate(selectedOrder.created_at)}</p>
              </div>

              <div className="p-4 bg-gray-50 rounded-lg col-span-2">
                <p className="text-xs text-gray-500 mb-1">Payment Reference</p>
                <p className="text-sm font-mono text-gray-900 break-all">{selectedOrder.payment_reference || '-'}</p>
              </div>

              {selectedOrder.note && (
                <div className="p-4 bg-gray-50 rounded-lg col-span-2">
                  <p className="text-xs text-gray-500 mb-1">Notes</p>
                  <p className="text-sm text-gray-900">{selectedOrder.note}</p>
                </div>
              )}
            </div>

            <div className="mt-6 flex justify-end">
              <button
                onClick={() => {
                  setIsDetailModalOpen(false);
                  setSelectedOrder(null);
                }}
                className="px-6 py-2 bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
