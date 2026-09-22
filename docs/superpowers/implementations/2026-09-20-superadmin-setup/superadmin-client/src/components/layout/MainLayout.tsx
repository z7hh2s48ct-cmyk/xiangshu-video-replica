/**
 * Main layout container for authenticated pages
 */

import React, { useEffect, useState } from 'react';
import { Sidebar } from './Sidebar';
import { Header } from './Header';
import { useAuthStore } from '@/store/authStore';

export const MainLayout: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { logout, checkAuth, isAuthenticated } = useAuthStore();
  const [isAuthenticatedLocal, setIsAuthenticated] = useState(false);

  // Check auth on mount
  useEffect(() => {
    const initAuth = async () => {
      const authenticated = await checkAuth();
      setIsAuthenticated(authenticated);
      
      if (!authenticated) {
        window.location.href = '/login';
      }
    };

    initAuth();
  }, [checkAuth]);

  if (!isAuthenticatedLocal) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-gray-500">Loading...</div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50 flex">
      {/* Sidebar Navigation */}
      <aside className="w-64 flex-shrink-0 h-full">
        <Sidebar onLogout={logout} />
      </aside>

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Header */}
        <Header />

        {/* Page Content */}
        <main className="flex-1 overflow-auto">
          <div className="p-6">
            {children}
          </div>
        </main>
      </div>
    </div>
  );
};
