/**
 * Pricing configuration management page (Simplified version)
 */

import React from 'react';
import { pricingApi } from '@/api/pricing.api';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import type { PricingConfig } from '@/types/pricing.types';

export const PricingPage: React.FC = () => {
  const queryClient = useQueryClient();

  const { data: prices, isLoading, error } = useQuery({
    queryKey: ['pricing'],
    queryFn: () => pricingApi.list(),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => pricingApi.delete(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['pricing'] }),
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-light text-gray-900">Pricing Configuration</h1>
        <button className="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg flex items-center gap-2">
          <span className="inline-block w-4 h-4 text-white" style={{content:'+'}} />Add Price
        </button>
      </div>

      {isLoading && <p className="text-gray-500">Loading...</p>}
      
      {!isLoading && !error && prices && (
        <div className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden">
          <table className="w-full">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="text-left px-4 py-3 text-sm font-medium">Subject</th>
                <th className="text-left px-4 py-3 text-sm font-medium">Tenant ID</th>
                <th className="text-left px-4 py-3 text-sm font-medium">Base Price (Fen)</th>
                <th className="text-left px-4 py-3 text-sm font-medium">Discount %</th>
                <th className="text-left px-4 py-3 text-sm font-medium">Effective Price (Yuan)</th>
                <th className="text-right px-4 py-3 text-sm font-medium">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {prices.map((price) => (
                <tr key={price.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 text-sm">{price.subject}</td>
                  <td className="px-4 py-3 text-sm">{price.tenant_id}</td>
                  <td className="px-4 py-3 text-sm">{price.base_price_fen.toLocaleString()}</td>
                  <td className="px-4 py-3 text-sm">{price.discount_percent}%</td>
                  <td className="px-4 py-3 text-sm font-medium">¥{price.effective_price_yuan.toFixed(2)}</td>
                  <td className="px-4 py-3 text-right space-x-2">
                    <button className="text-blue-600">Edit</button>
                    <button 
                      onClick={() => deleteMutation.mutate(price.id)}
                      className="text-red-600"
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};
