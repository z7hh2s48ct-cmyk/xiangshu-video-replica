# Superadmin Frontend - React Application Setup

**Date**: 2026-09-20  
**Tech Stack**: React 19 + TypeScript 5.9 + Vite 8 + shadcn/ui  

---

## 📁 Project Structure

```
superadmin-client/
├── src/
│   ├── api/                    # API client & services
│   │   ├── client.ts           # Base API client configuration
│   │   ├── auth.api.ts         # Authentication endpoints
│   │   ├── tenant.api.ts       # Tenant management APIs
│   │   ├── pricing.api.ts      # Pricing configuration APIs
│   │   └── allowance.api.ts    # Allowance check (public) APIs
│   │
│   ├── components/             # Reusable UI components
│   │   ├── ui/                 # shadcn/ui primitive components
│   │   │   ├── button.tsx
│   │   │   ├── input.tsx
│   │   │   ├── dialog.tsx
│   │   │   ├── table.tsx
│   │   │   ├── dropdown-menu.tsx
│   │   │   └── ...
│   │   ├── layout/             # Layout components
│   │   │   ├── Sidebar.tsx
│   │   │   ├── Header.tsx
│   │   │   └── MainLayout.tsx
│   │   ├── pricing/            # Pricing-specific components
│   │   │   ├── PricingTable.tsx
│   │   │   ├── PriceEditDialog.tsx
│   │   │   └── BulkPricingUploader.tsx
│   │   └── common/             # Common components
│   │       ├── LoadingSpinner.tsx
│   │       └── ErrorBoundary.tsx
│   │
│   ├── hooks/                  # Custom React hooks
│   │   ├── useAuth.ts          # Auth state & token management
│   │   ├── useApi.ts           # API call helper with error handling
│   │   └── usePagination.ts    # Pagination logic
│   │
│   ├── pages/                  # Route pages
│   │   ├── Login.tsx
│   │   ├── Dashboard.tsx
│   │   ├── TenantsPage.tsx
│   │   ├── PricingPage.tsx
│   │   ├── RechargeOrders.tsx
│   │   └── ReportsPage.tsx
│   │
│   ├── store/                  # Global state (Zustand)
│   │   ├── authStore.ts        # Auth state slice
│   │   └── settingsStore.ts    # Settings/state slice
│   │
│   ├── types/                  # TypeScript type definitions
│   │   ├── auth.types.ts
│   │   ├── tenant.types.ts
│   │   ├── pricing.types.ts
│   │   └── api.types.ts
│   │
│   ├── utils/                  # Utility functions
│   │   ├── formatters.ts       # Date, number formatting
│   │   ├── validators.ts       # Form validation helpers
│   │   └── constants.ts        # App-wide constants
│   │
│   ├── App.tsx                 # Main app component
│   ├── main.tsx                # Entry point
│   └── vite-env.d.ts           # Vite type declarations
│
├── .env.example                # Environment variables template
├── index.html
├── tsconfig.json
├── package.json
├── tailwind.config.js
├── postcss.config.js
└── vitest.config.ts
```

---

## 🚀 Quick Start

### 1. Install dependencies
```bash
cd superadmin-client
npm install
```

### 2. Configure environment variables
Create `.env` file:
```env
VITE_API_BASE_URL=http://localhost:8001/api/v1
VITE_APP_TITLE=Superadmin Dashboard
```

### 3. Run development server
```bash
npm run dev
```

The app will be available at `http://localhost:5173`

---

## 🔧 Core Dependencies

```json
{
  "dependencies": {
    "react": "^19.0.0",
    "react-dom": "^19.0.0",
    "react-router-dom": "^7.0.0",
    "@tanstack/react-query": "^5.62.0",
    "zustand": "^5.0.0",
    "axios": "^1.7.0",
    "zod": "^3.24.0",
    "react-hook-form": "^7.54.0",
    "date-fns": "^4.1.0",
    "class-variance-authority": "^0.7.1",
    "clsx": "^2.1.1",
    "tailwind-merge": "^2.6.0"
  },
  "devDependencies": {
    "@vitejs/plugin-react": "^4.3.4",
    "typescript": "~5.9.0",
    "vite": "^6.0.0",
    "vitest": "^3.0.0",
    "@testing-library/react": "^16.0.0",
    "playwright": "^1.49.0"
  }
}
```

---

## 🎨 Design System Principles

Following **minimalist-ui** and **high-end-visual-design** skills:

- **Typography**: Clean 宋体 typography for Chinese content
- **Colors**: Single deep blue accent (#1e40af)
- **Spacing**: Consistent 8px grid system
- **Borders**: Fine gray borders (#e5e7eb)
- **Shadows**: Subtle shadows only on overlays/modals
- **Hierarchy**: One visual focus per page
- **No corporate PPT look**: Avoids generic card grids and emoji icons

---

## 📝 Development Guidelines

### 1. Component Structure
All components must follow atomic design principles:
- **Atoms**: Individual UI elements (Button, Input)
- **Molecules**: Compound components (SearchBox = Input + Button)
- **Organisms**: Complex sections (DataTable + Pagination)
- **Templates**: Page layouts
- **Pages**: Complete routes

### 2. State Management
- **Server State**: TanStack Query (automatic caching/syncing)
- **Client State**: Zustand (UI preferences, theme)
- **Form State**: React Hook Form + Zod validation

### 3. API Integration Pattern
```typescript
// In src/api/tenant.api.ts
import { apiClient } from './client';

export const getTenants = async (params?: TenantListParams) => {
  const response = await apiClient.get<TenantResponse[]>('/tenants', { params });
  return response.data;
};

export const createTenant = async (data: TenantCreate) => {
  const response = await apiClient.post<TenantResponse>('/tenants', data);
  return response.data;
};
```

### 4. Type Safety
Always define explicit interfaces:
```typescript
// In src/types/tenant.types.ts
export interface Tenant {
  id: string;
  tenant_id: string;
  name: string;
  company_name: string | null;
  contact_email: string | null;
  status: 'pending' | 'active' | 'suspended' | 'deactivated';
  balance_yuan: number;
  // ...
}
```

---

## ✅ Testing Strategy

1. **Unit Tests**: Vitest + React Testing Library
2. **E2E Tests**: Playwright
3. **Component Storybook**: Optional for isolated component testing

Run tests:
```bash
npm test              # Unit tests
npm run test:e2e      # E2E tests
```

---

## 🚢 Next Steps

See individual component files in `/src/components` directory for implementation details.
