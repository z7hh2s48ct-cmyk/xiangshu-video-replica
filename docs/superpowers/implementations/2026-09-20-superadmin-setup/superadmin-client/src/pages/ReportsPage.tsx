/**
 * Reports & Analytics dashboard page
 */

import React from 'react';
import { TrendingUp, DollarSign, Users, ShoppingBag, Clock, AlertCircle } from 'lucide-react';

// Mock data for demonstration (replace with actual API calls)
const revenueStats = [
  { label: 'Total Revenue (This Month)', value: '¥284,560', change: '+18%', trend: 'up' },
  { label: 'Active Tenants', value: '156', change: '+12%', trend: 'up' },
  { label: 'Recharge Orders', value: '892', change: '+24%', trend: 'up' },
  { label: 'Avg. Order Value', value: '¥318.75', change: '-3%', trend: 'down' },
];

const recentOrders = [
  { tenant_id: 'tenant_001', amount: '¥500.00', status: 'paid', time: '2 minutes ago' },
  { tenant_id: 'tenant_023', amount: '¥1,200.00', status: 'pending', time: '15 minutes ago' },
  { tenant_id: 'tenant_045', amount: '¥350.00', status: 'failed', time: '1 hour ago' },
  { tenant_id: 'tenant_012', amount: '¥800.00', status: 'paid', time: '2 hours ago' },
  { tenant_id: 'tenant_067', amount: '¥150.00', status: 'refunded', time: '3 hours ago' },
];

export const ReportsPage: React.FC = () => {
  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-light text-gray-900">Reports & Analytics</h1>
          <p className="text-sm text-gray-600 mt-1">Overview of system performance and revenue metrics</p>
        </div>
      </div>

      {/* Key Metrics Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        {revenueStats.map((stat, idx) => {
          const Icon = idx === 0 ? DollarSign : idx === 1 ? Users : idx === 2 ? ShoppingBag : TrendingUp;
          const isUp = stat.trend === 'up';
          
          return (
            <div key={idx} className="bg-white rounded-lg p-6 border border-gray-200 shadow-sm hover:shadow-md transition-shadow">
              <div className="flex items-center justify-between mb-4">
                <Icon className={`w-6 h-6 ${isUp ? 'text-green-600' : 'text-red-600'}`} />
                <span className={`text-xs font-medium px-2 py-1 rounded-full ${
                  isUp ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'
                }`}>
                  {stat.change}
                </span>
              </div>
              <p className="text-2xl font-semibold text-gray-900">{stat.value}</p>
              <p className="text-sm text-gray-600 mt-1">{stat.label}</p>
            </div>
          );
        })}
      </div>

      {/* Revenue Overview Chart Placeholder */}
      <div className="bg-white rounded-lg p-6 border border-gray-200 shadow-sm">
        <h2 className="text-lg font-medium text-gray-900 mb-4">Revenue Overview (Last 30 Days)</h2>
        <div className="h-64 bg-gradient-to-r from-blue-50 to-green-50 rounded-lg flex items-center justify-center">
          <div className="text-center text-gray-500">
            <DollarSign className="w-12 h-12 mx-auto mb-2 opacity-50" />
            <p className="text-sm">Interactive chart component would render here</p>
            <p className="text-xs mt-1">Integrate with Recharts or Chart.js for production</p>
          </div>
        </div>
      </div>

      {/* Recent Orders Table */}
      <div className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden">
        <div className="px-6 py-4 border-b border-gray-200">
          <h2 className="text-lg font-medium text-gray-900">Recent Transactions</h2>
        </div>
        <table className="w-full">
          <thead className="bg-gray-50 border-b border-gray-200">
            <tr>
              <th className="text-left px-6 py-3 text-sm font-medium text-gray-700">Tenant ID</th>
              <th className="text-left px-6 py-3 text-sm font-medium text-gray-700">Amount</th>
              <th className="text-left px-6 py-3 text-sm font-medium text-gray-700">Status</th>
              <th className="text-left px-6 py-3 text-sm font-medium text-gray-700">Time</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {recentOrders.map((order, idx) => (
              <tr key={idx} className="hover:bg-gray-50 transition-colors">
                <td className="px-6 py-3 text-sm text-gray-900">{order.tenant_id}</td>
                <td className="px-6 py-3 text-sm font-medium text-gray-900">{order.amount}</td>
                <td className="px-6 py-3">
                  <span
                    className={`inline-block px-2 py-1 text-xs rounded-full ${
                      order.status === 'paid'
                        ? 'bg-green-50 text-green-700'
                        : order.status === 'pending'
                        ? 'bg-yellow-50 text-yellow-700'
                        : order.status === 'failed'
                        ? 'bg-red-50 text-red-700'
                        : 'bg-purple-50 text-purple-700'
                    }`}
                  >
                    {order.status}
                  </span>
                </td>
                <td className="px-6 py-3 text-sm text-gray-600">{order.time}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* System Health & Alerts */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* System Status */}
        <div className="bg-white rounded-lg p-6 border border-gray-200 shadow-sm">
          <h2 className="text-lg font-medium text-gray-900 mb-4">System Health</h2>
          <div className="space-y-4">
            <div className="flex items-center justify-between p-3 bg-green-50 rounded-lg">
              <div className="flex items-center gap-3">
                <CheckCircle className="w-5 h-5 text-green-600" />
                <span className="text-sm text-gray-900">Database Connection</span>
              </div>
              <span className="text-xs text-green-700 font-medium">Healthy</span>
            </div>

            <div className="flex items-center justify-between p-3 bg-green-50 rounded-lg">
              <div className="flex items-center gap-3">
                <CheckCircle className="w-5 h-5 text-green-600" />
                <span className="text-sm text-gray-900">API Service</span>
              </div>
              <span className="text-xs text-green-700 font-medium">Operational</span>
            </div>

            <div className="flex items-center justify-between p-3 bg-green-50 rounded-lg">
              <div className="flex items-center gap-3">
                <CheckCircle className="w-5 h-5 text-green-600" />
                <span className="text-sm text-gray-900">Authentication Server</span>
              </div>
              <span className="text-xs text-green-700 font-medium">Running</span>
            </div>
          </div>
        </div>

        {/* Pending Actions */}
        <div className="bg-white rounded-lg p-6 border border-gray-200 shadow-sm">
          <h2 className="text-lg font-medium text-gray-900 mb-4">Pending Actions</h2>
          <div className="space-y-3">
            <div className="flex items-start gap-3 p-3 bg-yellow-50 rounded-lg">
              <Clock className="w-5 h-5 text-yellow-600 mt-0.5" />
              <div className="flex-1">
                <p className="text-sm font-medium text-gray-900">Pending Payments</p>
                <p className="text-xs text-gray-600 mt-1">2 orders awaiting verification</p>
              </div>
              <button className="text-sm text-yellow-700 hover:text-yellow-800 font-medium">
                View
              </button>
            </div>

            <div className="flex items-start gap-3 p-3 bg-red-50 rounded-lg">
              <AlertCircle className="w-5 h-5 text-red-600 mt-0.5" />
              <div className="flex-1">
                <p className="text-sm font-medium text-gray-900">Failed Transactions</p>
                <p className="text-xs text-gray-600 mt-1">3 orders need attention</p>
              </div>
              <button className="text-sm text-red-700 hover:text-red-800 font-medium">
                Review
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Quick Export Options */}
      <div className="bg-white rounded-lg p-6 border border-gray-200 shadow-sm">
        <h2 className="text-lg font-medium text-gray-900 mb-4">Export Data</h2>
        <div className="flex gap-4">
          <button className="flex items-center gap-2 px-4 py-2 bg-blue-50 text-blue-700 rounded-lg hover:bg-blue-100 transition-colors">
            <FileText className="w-4 h-4" />
            <span className="text-sm font-medium">Export CSV</span>
          </button>
          <button className="flex items-center gap-2 px-4 py-2 bg-green-50 text-green-700 rounded-lg hover:bg-green-100 transition-colors">
            <FileText className="w-4 h-4" />
            <span className="text-sm font-medium">Export Excel</span>
          </button>
          <button className="flex items-center gap-2 px-4 py-2 bg-purple-50 text-purple-700 rounded-lg hover:bg-purple-100 transition-colors">
            <FileText className="w-4 h-4" />
            <span className="text-sm font-medium">Generate PDF Report</span>
          </button>
        </div>
      </div>
    </div>
  );
};

// Import CheckCircle icon at the top
import { CheckCircle } from 'lucide-react';
