/**
 * Main header component with user profile display
 */

import React from 'react';
import { UserCircle } from 'lucide-react';
import { useAuthStore } from '@/store/authStore';

export const Header: React.FC = () => {
  const { user, logout } = useAuthStore();

  return (
    <header className="h-16 bg-white border-b border-gray-200 flex items-center justify-between px-6">
      {/* Left side - Page title (passed as prop in real implementation) */}
      <div className="flex items-center gap-4">
        {/* Breadcrumb would go here */}
      </div>

      {/* Right side - User profile */}
      <div className="flex items-center gap-4">
        {user && (
          <>
            <div className="text-right hidden md:block">
              <p className="text-sm font-medium text-gray-900">
                {user.full_name || user.username}
              </p>
              <p className="text-xs text-gray-500">
                {user.is_superuser ? 'Super Admin' : 'Administrator'}
              </p>
            </div>
            
            <button className="flex items-center gap-2 px-3 py-1.5 rounded-lg hover:bg-gray-50 transition-colors">
              <UserCircle className="w-8 h-8 text-gray-700" />
            </button>

            <button
              onClick={logout}
              className="text-sm text-gray-600 hover:text-gray-900 px-3 py-1.5 rounded-lg hover:bg-gray-50 transition-colors"
            >
              Sign Out
            </button>
          </>
        )}
      </div>
    </header>
  );
};
