/**
 * Auth state management using Zustand
 * Handles user authentication state and token lifecycle
 */

import { create } from 'zustand';
import { authService } from '../api/auth.api';
import { CurrentUserResponse } from '../types/auth.types';

interface AuthState {
  // State
  isAuthenticated: boolean;
  user: CurrentUserResponse | null;
  isLoading: boolean;
  error: string | null;

  // Actions
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
  setCurrentUser: (user: CurrentUserResponse) => void;
  clearError: () => void;
  checkAuth: () => Promise<boolean>;
}

export const useAuthStore = create<AuthState>((set) => ({
  // Initial state
  isAuthenticated: false,
  user: null,
  isLoading: false,
  error: null,

  // Login action
  login: async (username: string, password: string) => {
    set({ isLoading: true, error: null });

    try {
      const response = await authService.login({ username, password, remember_me: false });
      
      // Store tokens
      authService.setTokens(response.access_token, response.refresh_token);
      
      // Set user info
      set({
        isAuthenticated: true,
        user: response.user,
        isLoading: false,
      });
    } catch (error: any) {
      set({
        isLoading: false,
        error: error.response?.data?.detail || 'Login failed',
      });
      throw error;
    }
  },

  // Logout action
  logout: () => {
    authService.logout().catch(console.error);
    authService.clearTokens();
    set({
      isAuthenticated: false,
      user: null,
      error: null,
    });
  },

  // Set current user (for auth check)
  setCurrentUser: (user: CurrentUserResponse) => {
    set({
      isAuthenticated: true,
      user,
      isLoading: false,
    });
  },

  // Clear error message
  clearError: () => {
    set({ error: null });
  },

  // Check if user is authenticated
  checkAuth: async (): Promise<boolean> => {
    const accessToken = authService.getAccessToken();
    
    if (!accessToken) {
      set({ isAuthenticated: false, user: null });
      return false;
    }

    try {
      const user = await authService.getCurrentUser();
      set({ isAuthenticated: true, user });
      return true;
    } catch (error) {
      authService.clearTokens();
      set({ isAuthenticated: false, user: null });
      return false;
    }
  },
}));
