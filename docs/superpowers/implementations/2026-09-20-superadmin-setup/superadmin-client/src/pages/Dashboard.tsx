/**
 * Dashboard homepage - Summary statistics and quick actions
 */

import React from 'react';
import { Users, Tag, ShoppingBag, DollarSign, TrendingUp, AlertCircle } from 'lucide-react';

const stats = [
  {
    name: 'Active Tenants',
    value: '156',
    change: '+12%',
    icon: Users,
    color: 'blue',
  },
  {
    name: 'Pricing Configs',
    value: '47',
    change: '+3%',
    icon: Tag,
    color: 'green',
  },
  {
    name: 'Recharge Orders',
    value: '892',
    change: '+18%',
    icon: ShoppingBag,
    color: 'purple',
  },
  {
    name: 'Total Balance (Fen)',
    value: '¥1.2M',
    change: '+7%',
    icon: DollarSign,
    color: 'orange',
  },
];

export const DashboardPage: React.FC = () => {
  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-light text-gray-900">Dashboard</h1>
          <p className="text-sm text-gray-600 mt-1">Welcome back! Here's what's happening today.</p>
        </div>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        {stats.map((stat) => {
          const Icon = stat.icon;
          
          return (
            <div
              key={stat.name}
              className="bg-white rounded-lg p-6 border border-gray-200 shadow-sm hover:shadow-md transition-shadow"
            >
              <div className="flex items-center justify-between mb-4">
                <Icon className={`w-6 h-6 text-${stat.color}-600`} />
                <span className={`text-xs font-medium text-${stat.color}-600 bg-${stat.color}-50 px-2 py-1 rounded`}>
                  {stat.change}
                </span>
              </div>
              <p className="text-2xl font-semibold text-gray-900">{stat.value}</p>
              <p className="text-sm text-gray-600 mt-1">{stat.name}</p>
            </div>
          );
        })}
      </div>

      {/* Quick Actions */}
      <div className="bg-white rounded-lg p-6 border border-gray-200 shadow-sm">
        <h2 className="text-lg font-medium text-gray-900 mb-4">Quick Actions</h2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <button className="flex items-center gap-3 px-4 py-3 bg-blue-50 text-blue-700 rounded-lg hover:bg-blue-100 transition-colors">
            <Users className="w-5 h-5" />
            <span className="text-sm font-medium">Add New Tenant</span>
          </button>
          <button className="flex items-center gap-3 px-4 py-3 bg-green-50 text-green-700 rounded-lg hover:bg-green-100 transition-colors">
            <Tag className="w-5 h-5" />
            <span className="text-sm font-medium">Configure Pricing</span>
          </button>
          <button className="flex items-center gap-3 px-4 py-3 bg-purple-50 text-purple-700 rounded-lg hover:bg-purple-100 transition-colors">
            <ShoppingBag className="w-5 h-5" />
            <span className="text-sm font-medium">View Recent Orders</span>
          </button>
        </div>
      </div>

      {/* Recent Activity */}
      <div className="bg-white rounded-lg p-6 border border-gray-200 shadow-sm">
        <h2 className="text-lg font-medium text-gray-900 mb-4">Recent Activity</h2>
        <div className="space-y-3">
          {[1, 2, 3, 4, 5].map((i) => (
            <div key={i} className="flex items-center justify-between py-3 border-b border-gray-100 last:border-none">
              <div className="flex items-center gap-3">
                <div className="w-2 h-2 bg-blue-500 rounded-full" />
                <div>
                  <p className="text-sm text-gray-900">New tenant registered</p>
                  <p className="text-xs text-gray-500">Tenant ID: example_00{i}</p>
                </div>
              </div>
              <span className="text-xs text-gray-500">2 hours ago</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};
