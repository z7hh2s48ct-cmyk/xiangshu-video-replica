# 超管系统前端价格 UI 架构与配置参数设计

**日期**: 2026-09-20  
**版本**: V1.0  
**状态**: Draft for Review  
**模块**: Superadmin Admin Portal - Pricing Configuration  

---

## 📋 目录

1. [总体架构](#1-总体架构)
2. [UI 组件树结构](#2-ui-组件树结构)
3. [状态管理规范](#3-状态管理规范)
4. [核心页面设计](#4-核心页面设计)
5. [配置参数体系](#5-配置参数体系)
6. [主题定制方案](#6-主题定制方案)
7. [国际化支持](#7-国际化支持)
8. [性能优化策略](#8-性能优化策略)

---

## 1. 总体架构

### 1.1 技术栈选型

```typescript
// Frontend Technology Stack
{
  "framework": "React 19.0.0",
  "stateManagement": {
    "global": "Zustand (轻量为 API 缓存设计)",
    "server": "TanStack Query (服务端状态同步)",
    "form": "React Hook Form + Zod (表单验证)"
  },
  "uiLibrary": {
    "base": "shadcn/ui (Tailwind CSS)",
    "dataGrid": "@tanstack/react-table",
    "charts": "Recharts"
  },
  "http": {
    "client": "axios",
    "cache": "@tanstack/react-query"
  },
  "buildTool": "Vite 5.x",
  "typeSystem": "TypeScript 5.9.x",
  "linting": "Biome (替代 ESLint/Prettier)",
  "testing": {
    "unit": "Vitest",
    "e2e": "Playwright"
  }
}
```

### 1.2 项目结构

```
superadmin-client/
├── src/
│   ├── components/
│   │   ├── ui/                  # shadcn/ui 基础组件库
│   │   │   ├── button.tsx
│   │   │   ├── dialog.tsx
│   │   │   ├── table.tsx
│   │   │   └── form.tsx
│   │   │
│   │   ├── pricing/             # 价格相关业务组件
│   │   │   ├── PriceCard.tsx
│   │   │   ├── PriceEditDialog.tsx
│   │   │   ├── PriceHistoryTable.tsx
│   │   │   ├── BulkPricingUploader.tsx
│   │   │   └── ProfitCalculator.tsx
│   │   │
│   │   ├── tenant/
│   │   │   ├── TenantList.tsx
│   │   │   ├── TenantDetailPanel.tsx
│   │   │   └── TenantStatusBadge.tsx
│   │   │
│   │   └── layout/
│   │       ├── Sidebar.tsx
│   │       ├── Header.tsx
│   │       └── Navigation.tsx
│   │
│   ├── pages/
│   │   ├── PricingPage.tsx      # 主定价管理页
│   │   ├── TenantsPage.tsx      # 租户列表页
│   │   ├── RechargeOrdersPage.tsx
│   │   ├── ProfitReportPage.tsx
│   │   └── SettingsPage.tsx     # 系统设置（服务商配置）
│   │
│   ├── hooks/
│   │   ├── usePricing.ts        # 定价数据查询 hook
│   │   ├── useTenantSearch.ts
│   │   └── useBulkImport.ts
│   │
│   ├── stores/
│   │   ├── authStore.ts         # 登录态存储
│   │   └── settingsStore.ts     # 全局配置开关
│   │
│   ├── api/
│   │   ├── client.ts            # Axios 实例配置
│   │   ├── pricingApi.ts        # 定价相关 API 调用封装
│   │   ├── tenantApi.ts
│   │   └── types.ts             # TypeScript 类型定义
│   │
│   ├── utils/
│   │   ├── formatters.ts        # 数字/日期格式化
│   │   ├── validators.ts        # Zod 验证规则
│   │   └── constants.ts         # 常量定义
│   │
│   ├── config/                  # 配置文件（详见第 5 节）
│   │   ├── pricing.config.ts
│   │   ├── theme.config.ts
│   │   └── featureFlags.config.ts
│   │
│   ├── App.tsx
│   └── main.tsx
│
├── public/
│   └── locales/                 # i18n 翻译文件
│       ├── zh-CN.json
│       └── en-US.json
│
├── .env.local                   # 环境变量
├── .env.example
├── vite.config.ts
├── tsconfig.json
└── package.json
```

---

## 2. UI 组件树结构

### 2.1 路由层级图

```
SuperadminApp
├── Layout (Sidebar + Header)
│   ├── Navigation Menu
│   │   ├── Dashboard (概览面板)
│   │   ├── 平台管理
│   │   │   ├── 加盟商管理 (/tenants)
│   │   │   │   └── TenantList
│   │   │   │       ├── TenantRow
│   │   │   │           └── Actions: Edit / View Details / Suspend
│   │   │   ├── 价格管理 (/pricing)
│   │   │   │   └── PricingPage
│   │   │   │       ├── SearchBar
│   │   │   │       ├── BulkActionToolbar
│   │   │   │       ├── PricingTable (主表格)
│   │   │   │       │   ├── PricingRow
│   │   │   │       │   │   ├── Video768pPrice
│   │   │   │       │   │   ├── Video2KPrice
│   │   │   │       │   │   ├── OralPrice
│   │   │   │       │   │   ├── EffectiveDate
│   │   │   │       │   │   └── Actions: Edit / History / Duplicate
│   │   │   │       │   └── Pagination
│   │   │   │       ├── PricingEditorDialog
│   │   │   │       │   ├── PriceInputField
│   │   │   │       │   ├── DiscountSlider
│   │   │   │       │   ├── PointsPerYuanToggle
│   │   │   │       │   ├── TimeRangePicker
│   │   │   │       │   └── SubmitButton
│   │   │   │       ├── BulkImportModal
│   │   │   │       │   ├── CSVUploadZone
│   │   │   │       │   ├── PreviewDataTable
│   │   │   │       │   └── ValidateAndCommitButton
│   │   │   │       └── ExportButton
│   │   │   ├── 充值订单 (/recharge-orders)
│   │   │   ├── 利润报表 (/profit)
│   │   │   └── 系统设置 (/settings)
│   │   │       ├── ProviderSettingsSection
│   │   │       ├── UpstreamCostConfig
│   │   │       └── AuditLogViewer
│   │   └── 用户中心 (/profile)
│
└── Modal Stack
    ├── ConfirmDialog (通用确认框)
    ├── PriceEditDialog
    ├── BulkImportModal
    └── ProfitExportModal
```

### 2.2 关键组件详细设计

#### **PricingTable Component**

```tsx
// src/components/pricing/PricingTable.tsx

import React from 'react';
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from '@tanstack/react-table';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { Search, Edit, Clock, Download } from 'lucide-react';
import { PricingSnapshot } from '@/api/types';
import { PriceCard } from './PriceCard';

interface PricingTableProps {
  data: PricingSnapshot[];
  isLoading?: boolean;
  onEdit: (tenant: PricingSnapshot) => void;
  onViewHistory: (tenant: PricingSnapshot) => void;
  onExport?: () => void;
  searchTerm?: string;
  onSearchChange: (term: string) => void;
}

export const PricingTable: React.FC<PricingTableProps> = ({
  data,
  isLoading,
  onEdit,
  onViewHistory,
  onExport,
  searchTerm,
  onSearchChange,
}) => {
  const columnHelper = createColumnHelper<PricingSnapshot>();

  const columns = [
    columnHelper.accessor('tenant_id', {
      header: '租户 ID',
      cell: info => (
        <div className="font-medium">{info.getValue()}</div>
      ),
    }),
    
    columnHelper.accessor('name', {
      header: '租户名称',
      cell: info => <div>{info.getValue()}</div>,
    }),

    columnHelper.accessor(
      d => ({ video_768p: d.pricing.video_768p, video_2k: d.pricing.video_2k, oral: d.pricing.oral }),
      {
        header: '价格配置',
        cell: info => {
          const pricing = info.getValue();
          return (
            <div className="flex gap-4">
              <PriceCard
                label="768P 视频"
                price={pricing.video_768p ?? undefined}
                currency="CNY"
                size="sm"
              />
              <PriceCard
                label="2K 视频"
                price={pricing.video_2k ?? undefined}
                currency="CNY"
                size="sm"
              />
              <PriceCard
                label="口播"
                price={pricing.oral ?? undefined}
                currency="CNY"
                unit="秒"
                size="sm"
              />
            </div>
          );
        },
      }
    ),

    columnHelper.accessor('points_per_yuan', {
      header: '积分汇率',
      cell: info => `${info.getValue()} 积分/RMB`,
    }),

    columnHelper.accessor(
      d => new Date(d.effective_from),
      {
        header: '生效时间',
        cell: info => (
          <span className="text-sm text-muted-foreground">
            {new Date(info.getValue()).toLocaleDateString('zh-CN')}
          </span>
        ),
      }
    ),

    columnHelper.accessor(
      d => d.status,
      {
        header: '状态',
        cell: info => {
          const status = info.getValue();
          const color = status === 'active' ? 'green' : status === 'expired' ? 'red' : 'gray';
          
          return (
            <span className={`px-2 py-1 rounded-full text-xs bg-${color}-100 text-${color}-700`}>
              {status === 'active' ? '生效中' : status === 'expired' ? '已过期' : '未激活'}
            </span>
          );
        },
      }
    ),

    columnHelper.display({
      id: 'actions',
      header: '操作',
      cell: info => (
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => onEdit(info.original)}
            title="编辑价格"
          >
            <Edit className="h-4 w-4" />
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => onViewHistory(info.original)}
            title="查看历史"
          >
            <Clock className="h-4 w-4" />
          </Button>
        </div>
      ),
    }),
  ];

  const table = useReactTable({
    data,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <div className="space-y-4">
      {/* Search & Toolbar */}
      <div className="flex justify-between items-center">
        <div className="relative max-w-md w-full">
          <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="搜索租户 ID 或名称..."
            value={searchTerm || ''}
            onChange={e => onSearchChange(e.target.value)}
            className="pl-10"
          />
        </div>
        
        <div className="flex gap-2">
          <Button variant="outline" onClick={onExport} disabled={!data.length}>
            <Download className="h-4 w-4 mr-2" />
            导出报表
          </Button>
        </div>
      </div>

      {/* Table */}
      <div className="border rounded-lg overflow-hidden">
        {isLoading ? (
          <div className="p-8 text-center text-muted-foreground">
            加载中...
          </div>
        ) : (
          <table className="min-w-full divide-y divide-border">
            <thead className="bg-muted">
              {table.getHeaderGroups().map(headerGroup => (
                <tr key={headerGroup.id}>
                  {headerGroup.headers.map(header => (
                    <th
                      key={header.id}
                      className="px-6 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider"
                    >
                      {flexRender(
                        header.column.columnDef.header,
                        header.getContext()
                      )}
                    </th>
                  ))}
                </tr>
              ))}
            </thead>
            <tbody className="bg-white divide-y divide-border">
              {table.getRowModel().rows.map(row => (
                <tr
                  key={row.id}
                  className="hover:bg-muted/50 transition-colors"
                >
                  {row.getVisibleCells().map(cell => (
                    <td
                      key={cell.id}
                      className="px-6 py-4 whitespace-nowrap text-sm"
                    >
                      {flexRender(
                        cell.column.columnDef.cell,
                        cell.getContext()
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Pagination */}
      <div className="flex justify-between items-center text-sm text-muted-foreground">
        <div>
          显示 {Math.min(table.getState().pagination.rowIndex + 1, data.length)} / {data.length} 条
        </div>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={!table.getCanPreviousPage()}
            onClick={() => table.previousPage()}
          >
            上一页
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={!table.getCanNextPage()}
            onClick={() => table.nextPage()}
          >
            下一页
          </Button>
        </div>
      </div>
    </div>
  );
};
```

#### **PriceCard Component**

```tsx
// src/components/pricing/PriceCard.tsx

import React from 'react';
import { cn } from '@/lib/utils';
import { DollarSign } from 'lucide-react';

interface PriceCardProps {
  label: string;
  price?: number; // in fen (分)
  currency?: string;
  unit?: string;
  size?: 'sm' | 'md' | 'lg';
  className?: string;
}

export const PriceCard: React.FC<PriceCardProps> = ({
  label,
  price,
  currency = '¥',
  unit,
  size = 'md',
  className,
}) => {
  const fontSizeClass = {
    sm: 'text-sm',
    md: 'text-base',
    lg: 'text-xl',
  }[size];

  const formatPrice = (fen: number) => {
    // Convert fen to RMB with 2 decimal places
    const rmb = (fen / 100).toFixed(2);
    return `${currency}${rmb}`;
  };

  return (
    <div
      className={cn(
        'inline-flex flex-col px-3 py-2 rounded-lg border bg-card',
        'transition-shadow hover:shadow-sm',
        fontSizeClass,
        className
      )}
    >
      <div className="text-muted-foreground mb-1">{label}</div>
      <div className="flex items-baseline gap-1">
        {price ? (
          <>
            <span className="font-semibold">{formatPrice(price)}</span>
            {unit && <span className="text-muted-foreground text-xs">/{unit}</span>}
          </>
        ) : (
          <span className="text-muted-foreground">未配置</span>
        )}
      </div>
    </div>
  );
};
```

#### **PriceEditDialog Component**

```tsx
// src/components/pricing/PriceEditDialog.tsx

import React, { useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Form, FormControl, FormDescription, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { Slider } from '@/components/ui/slider';
import { PricingConfig } from '@/api/types';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import * as z from 'zod';

const priceSchema = z.object({
  video_768p: z.number().min(0).max(1_000_000).optional().default(0),
  video_2k: z.number().min(0).max(1_000_000).optional().default(0),
  oral: z.number().min(0).max(1_000_000).optional().default(0),
  points_per_yuan: z.number().min(1).max(1000).default(100),
  discount_basis_points: z.number().min(1).max(10000).default(10000),
  consumption_rounding: z.enum(['ceil', 'floor']).default('ceil'),
});

type PriceFormData = z.infer<typeof priceSchema>;

interface PriceEditDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialData?: PricingConfig;
  onSave: (data: PriceFormData) => Promise<void>;
  isLoading?: boolean;
}

export const PriceEditDialog: React.FC<PriceEditDialogProps> = ({
  open,
  onOpenChange,
  initialData,
  onSave,
  isLoading,
}) => {
  const form = useForm<PriceFormData>({
    resolver: zodResolver(priceSchema),
    defaultValues: {
      video_768p: initialData?.video_768p ?? 0,
      video_2k: initialData?.video_2k ?? 0,
      oral: initialData?.oral ?? 0,
      points_per_yuan: initialData?.points_per_yuan ?? 100,
      discount_basis_points: initialData?.discount_basis_points ?? 10000,
      consumption_rounding: initialData?.consumption_rounding ?? 'ceil',
    },
  });

  const handleSubmit = async (values: PriceFormData) => {
    await onSave(values);
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <Form {...form}>
          <form onSubmit={form.handleSubmit(handleSubmit)}>
            <DialogHeader>
              <DialogTitle>编辑价格配置</DialogTitle>
            </DialogHeader>

            <div className="grid gap-6 py-4">
              {/* Price Inputs */}
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <FormField
                  control={form.control}
                  name="video_768p"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>768P 视频价格 (分)</FormLabel>
                      <FormControl>
                        <Input
                          type="number"
                          min={0}
                          max={1_000_000}
                          placeholder="例如：50000"
                          {...field}
                          onChange={e => field.setValue(parseInt(e.target.value) || 0)}
                        />
                      </FormControl>
                      <FormDescription>1 RMB = 100 分</FormDescription>
                      <FormMessage />
                    </FormItem>
                  )}
                />

                <FormField
                  control={form.control}
                  name="video_2k"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>2K 视频价格 (分)</FormLabel>
                      <FormControl>
                        <Input
                          type="number"
                          min={0}
                          max={1_000_000}
                          placeholder="例如：100000"
                          {...field}
                          onChange={e => field.setValue(parseInt(e.target.value) || 0)}
                        />
                      </FormControl>
                      <FormDescription />
                      <FormMessage />
                    </FormItem>
                  )}
                />

                <FormField
                  control={form.control}
                  name="oral"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>口播价格 (分/秒)</FormLabel>
                      <FormControl>
                        <Input
                          type="number"
                          min={0}
                          max={1_000_000}
                          placeholder="例如：1000"
                          {...field}
                          onChange={e => field.setValue(parseInt(e.target.value) || 0)}
                        />
                      </FormControl>
                      <FormDescription>每秒语音时长</FormDescription>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              </div>

              {/* Advanced Settings */}
              <div className="space-y-4 pt-4 border-t">
                <h4 className="text-sm font-medium">高级设置</h4>
                
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <FormField
                    control={form.control}
                    name="points_per_yuan"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>积分汇率 (积分/RMB)</FormLabel>
                        <FormControl>
                          <Input
                            type="number"
                            min={1}
                            max={1000}
                            {...field}
                            onChange={e => field.setValue(parseInt(e.target.value) || 0)}
                          />
                        </FormControl>
                        <FormDescription>默认 100 积分=RMB 1 元</FormDescription>
                        <FormMessage />
                      </FormItem>
                    )}
                  />

                  <FormField
                    control={form.control}
                    name="discount_basis_points"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>折扣力度 (基准点)</FormLabel>
                        <FormControl>
                          <Input
                            type="number"
                            min={1}
                            max={10000}
                            {...field}
                            onChange={e => field.setValue(parseInt(e.target.value) || 10000)}
                          />
                        </FormControl>
                        <FormDescription>10000 = 无折扣，5000 = 5 折</FormDescription>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                </div>

                <FormField
                  control={form.control}
                  name="consumption_rounding"
                  render={({ field }) => (
                    <FormItem className="flex flex-row items-center justify-between rounded-lg border p-4">
                      <div className="space-y-0.5">
                        <FormLabel className="text-base">消费取整方式</FormLabel>
                        <FormDescription>
                          ceil = 向上取整（对客户略贵），floor = 向下取整
                        </FormDescription>
                      </div>
                      <FormControl>
                        <Switch
                          checked={field.value === 'ceil'}
                          onCheckedChange={checked => field.onChange(checked ? 'ceil' : 'floor')}
                        />
                      </FormControl>
                    </FormItem>
                  )}
                />
              </div>
            </div>

            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
                取消
              </Button>
              <Button type="submit" disabled={isLoading}>
                {isLoading ? '保存中...' : '保存配置'}
              </Button>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
};
```

---

## 3. 状态管理规范

### 3.1 Global State (Zustand)

```typescript
// src/stores/settingsStore.ts

import { create } from 'zustand';
import { persist } from 'zustand/middleware';

interface PricingSettings {
  defaultCurrency: string;
  defaultPointsPerYuan: number;
  autoRefreshInterval: number; // minutes
  enableRealTimeSync: boolean;
}

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  userRole: 'super_admin' | 'admin' | 'employee';
  setAccessToken: (token: string | null) => void;
  setRefreshToken: (token: string | null) => void;
  setUserRole: (role: string) => void;
  logout: () => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      accessToken: null,
      refreshToken: null,
      userRole: 'super_admin',
      
      setAccessToken: (token) => set({ accessToken: token }),
      setRefreshToken: (token) => set({ refreshToken: token }),
      setUserRole: (role) => set({ userRole: role as AuthState['userRole'] }),
      
      logout: () => set({
        accessToken: null,
        refreshToken: null,
        userRole: 'super_admin',
      }),
    }),
    {
      name: 'auth-storage',
      partialize: (state) => ({
        accessToken: state.accessToken,
        userRole: state.userRole,
      }),
    }
  )
);

export const useSettingsStore = create<PricingSettings>()(
  persist(
    {
      defaultCurrency: 'CNY',
      defaultPointsPerYuan: 100,
      autoRefreshInterval: 5,
      enableRealTimeSync: true,
    },
    {
      name: 'settings-storage',
    }
  )
);
```

### 3.2 Server State (TanStack Query)

```typescript
// src/hooks/usePricing.ts

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { pricingApi } from '@/api/pricingApi';
import { PricingSnapshot, PricingFormData } from '@/api/types';

export const PRICING_QUERY_KEYS = {
  all: ['pricing'] as const,
  list: () => [...PRICING_QUERY_KEYS.all, 'list'] as const,
  details: () => [...PRICING_QUERY_KEYS.all, 'details'] as const,
};

export function usePricingList(searchTerm?: string) {
  return useQuery({
    queryKey: PRICING_QUERY_KEYS.list(),
    queryFn: () => pricingApi.getAllPricing(),
    staleTime: 1000 * 60 * 5, // 5 minutes
    refetchOnWindowFocus: false,
  });
}

export function usePricingDetail(tenantId: string) {
  return useQuery({
    queryKey: [...PRICING_QUERY_KEYS.details(), tenantId],
    queryFn: () => pricingApi.getPricingByTenant(tenantId),
    enabled: !!tenantId,
  });
}

export function useUpdatePricing() {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: (data: { tenantId: string; pricing: PricingFormData }) =>
      pricingApi.updatePricing(data.tenantId, data.pricing),
    
    onSuccess: (newPricing, variables) => {
      // Invalidate and refetch affected queries
      queryClient.invalidateQueries({
        queryKey: [...PRICING_QUERY_KEYS.list()],
      });
      
      queryClient.setQueryData(
        [...PRICING_QUERY_KEYS.details(), variables.tenantId],
        newPricing
      );
    },
  });
}
```

---

## 4. 核心页面设计

### 4.1 PricingPage.tsx (主定价管理页)

```tsx
// src/pages/PricingPage.tsx

import React, { useState, useCallback } from 'react';
import { usePricingList, useUpdatePricing } from '@/hooks/usePricing';
import { PricingTable } from '@/components/pricing/PricingTable';
import { PriceEditDialog } from '@/components/pricing/PriceEditDialog';
import { BulkPricingUploader } from '@/components/pricing/BulkPricingUploader';
import { ToastProvider, toast } from '@/components/ui/toast';

export const PricingPage: React.FC = () => {
  const [searchTerm, setSearchTerm] = useState<string>('');
  const [editingTenant, setEditingTenant] = useState<string | null>(null);
  const [showBulkImporter, setShowBulkImporter] = useState(false);

  const { data: pricingList, isLoading } = usePricingList(searchTerm);
  const { mutateAsync: updatePricing } = useUpdatePricing();

  const handleEditClick = useCallback((tenant: any) => {
    setEditingTenant(tenant.tenant_id);
  }, []);

  const handleSavePrice = async (formData: any) => {
    if (!editingTenant) return;
    
    try {
      await updatePricing({
        tenantId: editingTenant,
        pricing: formData,
      });
      
      toast.success('价格配置已更新');
      setEditingTenant(null);
    } catch (error) {
      toast.error(`更新失败：${error.message}`);
    }
  };

  const handleExport = useCallback(() => {
    // Implement export logic
    console.log('Export pricing data...');
  }, []);

  return (
    <div className="container mx-auto p-6 space-y-6">
      <ToastProvider />
      
      {/* Header */}
      <div className="flex justify-between items-start">
        <div>
          <h1 className="text-2xl font-bold">价格管理</h1>
          <p className="text-muted-foreground mt-1">
            配置各加盟商的零售价与积分汇率
          </p>
        </div>
        
        <div className="flex gap-2">
          <Button onClick={() => setShowBulkImporter(true)}>
            批量导入
          </Button>
          <Button variant="outline" onClick={handleExport}>
            导出报表
          </Button>
        </div>
      </div>

      {/* Main Table */}
      <PricingTable
        data={pricingList ?? []}
        isLoading={isLoading}
        onEdit={handleEditClick}
        onViewHistory={(tenant) => {
          // Navigate to history page or show modal
          console.log('View history:', tenant);
        }}
        onExport={handleExport}
        searchTerm={searchTerm}
        onSearchChange={setSearchTerm}
      />

      {/* Edit Dialog */}
      <PriceEditDialog
        open={!!editingTenant}
        onOpenChange={() => setEditingTenant(null)}
        onSave={handleSavePrice}
        isLoading={false}
      />

      {/* Bulk Importer */}
      <BulkPricingUploader
        open={showBulkImporter}
        onOpenChange={setShowBulkImporter}
      />
    </div>
  );
};
```

---

## 5. 配置参数体系

### 5.1 价格表配置 (`pricing.config.ts`)

```typescript
// src/config/pricing.config.ts

export interface PriceSchema {
  subject: string;
  label: string;
  description: string;
  minimum: number;
  maximum: number;
  defaultValue: number;
  currency: string;
  unit: string;
  rounding: 'ceil' | 'floor';
}

export interface ExchangeRateConfig {
  defaultPointsPerYuan: number;
  minPointsPerYuan: number;
  maxPointsPerYuan: number;
  availableRates: number[];
}

export interface DiscountConfig {
  minBasisPoints: number;
  maxBasisPoints: number;
  defaultBasisPoints: number;
  presetDiscounts: { label: string; basisPoints: number }[];
}

export const PRICE_SUBJECTS: PriceSchema[] = [
  {
    subject: 'video_768p',
    label: '768P 短视频',
    description: '标准分辨率视频生成单次任务价格',
    minimum: 0,
    maximum: 1_000_000, // 10,000 RMB
    defaultValue: 50_000, // 500 RMB
    currency: 'CNY',
    unit: '任务',
    rounding: 'ceil',
  },
  {
    subject: 'video_2k',
    label: '2K 高清视频',
    description: '高分辨率视频生成单次任务价格',
    minimum: 0,
    maximum: 2_000_000, // 20,000 RMB
    defaultValue: 100_000, // 1000 RMB
    currency: 'CNY',
    unit: '任务',
    rounding: 'ceil',
  },
  {
    subject: 'oral',
    label: '数字人口播',
    description: '每秒钟语音合成的价格',
    minimum: 0,
    maximum: 50_000, // 500 RMB/秒
    defaultValue: 1_000, // 10 RMB/秒
    currency: 'CNY',
    unit: '秒',
    rounding: 'floor',
  },
];

export const EXCHANGE_RATE_CONFIG: ExchangeRateConfig = {
  defaultPointsPerYuan: 100,
  minPointsPerYuan: 1,
  maxPointsPerYuan: 1000,
  availableRates: [10, 50, 100, 200, 500, 1000],
};

export const DISCOUNT_CONFIG: DiscountConfig = {
  minBasisPoints: 1,
  maxBasisPoints: 10000,
  defaultBasisPoints: 10000,
  presetDiscounts: [
    { label: '原价', basisPoints: 10000 },
    { label: '9 折', basisPoints: 9000 },
    { label: '8 折', basisPoints: 8000 },
    { label: '7 折', basisPoints: 7000 },
    { label: '5 折', basisPoints: 5000 },
    { label: '特价', basisPoints: 3000 },
  ],
};

// Validation rules
export const PRICE_VALIDATORS = {
  mustBeInteger: true,
  allowZero: true,
  requiredSubjects: ['video_768p', 'video_2k', 'oral'],
};

// Cache configuration
export const PRICING_CACHE_CONFIG = {
  ttlSeconds: 5 * 60, // 5 minutes
  maxConcurrentRequests: 10,
};
```

### 5.2 Theme Config (`theme.config.ts`)

```typescript
// src/config/theme.config.ts

export const THEME_CONFIG = {
  colors: {
    primary: {
      foreground: '#ffffff',
      DEFAULT: '#6366f1', // Indigo-500
    },
    danger: {
      foreground: '#ffffff',
      DEFAULT: '#ef4444', // Red-500
    },
    warning: {
      foreground: '#ffffff',
      DEFAULT: '#f59e0b', // Amber-500
    },
    success: {
      foreground: '#ffffff',
      DEFAULT: '#10b981', // Emerald-500
    },
    muted: {
      foreground: '#64748b',
      DEFAULT: '#f1f5f9',
    },
  },
  spacing: {
    cardPadding: '1rem',
    tableCellPadding: '0.75rem 1.5rem',
    sectionGap: '1.5rem',
  },
  typography: {
    headingFontSize: '1.5rem',
    bodyFontSize: '0.875rem',
    labelFontSize: '0.75rem',
  },
  shadows: {
    card: '0 1px 3px 0 rgba(0, 0, 0, 0.1), 0 1px 2px -1px rgba(0, 0, 0, 0.1)',
    dialog: '0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.1)',
  },
  borderRadius: {
    default: '0.5rem',
    small: '0.25rem',
    large: '1rem',
  },
};

export const LAYOUT_CONFIG = {
  sidebarWidth: '250px',
  headerHeight: '64px',
  tableMaxHeight: 'calc(100vh - 280px)',
};

export const FEATURE_FLAGS = {
  enableBulkImport: true,
  enableExport: true,
  enableAutoRefresh: true,
  enableAuditTrail: true,
  darkMode: false,
};
```

### 5.3 Feature Flags (`featureFlags.config.ts`)

```typescript
// src/config/featureFlags.config.ts

import { lazy } from 'react';

export const FEATURE_FLAGS = {
  // Enable/disable bulk import functionality
  enableBulkImport: {
    enabled: true,
    rolloutPercentage: 100,
    lastUpdated: '2026-09-20T10:00:00Z',
  },
  
  // Enable real-time sync with superadmin API
  enableRealTimeSync: {
    enabled: true,
    refreshIntervalMs: 5 * 60 * 1000, // 5 minutes
  },
  
  // Show/hold profit analytics dashboard
  enableProfitAnalytics: {
    enabled: true,
    requiredRole: 'super_admin',
  },
  
  // Experimental features
  experimental: {
    enableDarkMode: false,
    enableKeyboardShortcuts: true,
  },
};

// Dynamically load components based on feature flags
export const BULK_IMPORT_COMPONENTS = {
  UploadZone: lazy(() => import('@/components/pricing/BulkPricingUploader')),
  PreviewTable: lazy(() => import('@/components/pricing/BulkPricingPreview')),
};
```

---

## 6. 主题定制方案

### 6.1 CSS Variables Definition

```css
/* src/index.css */

@tailwind base;
@tailwind components;
@tailwind utilities;

@layer base {
  :root {
    /* Primary Brand Colors */
    --primary: 222 47% 44%;
    --primary-foreground: 210 40% 98%;
    
    /* Semantic Colors */
    --success: 142 76% 36%;
    --warning: 45 93% 47%;
    --danger: 0 84% 60%;
    --muted: 215 20% 91%;
    
    /* Typography */
    --font-sans: 'Inter var', system-ui, sans-serif;
    --font-mono: 'JetBrains Mono', monospace;
    
    /* Spacing Scale */
    --spacing-xs: 0.25rem;
    --spacing-sm: 0.5rem;
    --spacing-md: 1rem;
    --spacing-lg: 1.5rem;
    --spacing-xl: 2rem;
    --spacing-2xl: 3rem;
    
    /* Shadows */
    --shadow-sm: 0 1px 2px 0 rgb(0 0 0 / 0.05);
    --shadow-md: 0 4px 6px -1px rgb(0 0 0 / 0.1);
    --shadow-lg: 0 10px 15px -3px rgb(0 0 0 / 0.1);
  }
  
  .dark {
    --primary: 217 91% 60%;
    --muted: 222 47% 11%;
  }
}

@layer base {
  * {
    @apply border-border;
  }
  
  body {
    @apply bg-background text-foreground font-sans antialiased;
  }
}
```

---

## 7. 国际化支持

### 7.1 i18n 配置 (`i18n.config.ts`)

```typescript
// src/config/i18n.config.ts

import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import zhCN from '../locales/zh-CN.json';
import enUS from '../locales/en-US.json';

const resources = {
  'zh-CN': { translation: zhCN },
  'en-US': { translation: enUS },
};

i18n
  .use(initReactI18next)
  .init({
    resources,
    lng: localStorage.getItem('locale') || 'zh-CN',
    fallbackLng: 'en-US',
    interpolation: {
      escapeValue: false,
    },
    compatibilityJSON: 'v4',
  });

export default i18n;
```

### 7.2 Translation JSON Schema

```json
// src/locales/zh-CN.json
{
  "common": {
    "save": "保存",
    "cancel": "取消",
    "delete": "删除",
    "edit": "编辑",
    "view": "查看",
    "loading": "加载中...",
    "noData": "暂无数据",
    "confirm": "确认",
    "close": "关闭"
  },
  "pricing": {
    "title": "价格管理",
    "description": "配置各加盟商的零售价与积分汇率",
    "subjects": {
      "video_768p": "768P 视频价格",
      "video_2k": "2K 视频价格",
      "oral": "口播价格"
    },
    "units": {
      "task": "任务",
      "second": "秒"
    },
    "labels": {
      "effectiveFrom": "生效时间",
      "expiresAt": "失效时间",
      "discountRate": "折扣力度",
      "exchangeRate": "积分汇率"
    }
  }
}
```

---

## 8. 性能优化策略

### 8.1 Code Splitting

```typescript
// vite.config.ts

import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          'vendor': ['react', 'react-dom', 'react-router-dom'],
          'utils': ['dayjs', 'lodash-es', 'nanoid'],
          'api': ['@tanstack/react-query', 'axios'],
          'ui': ['@radix-ui/react-dialog', '@radix-ui/react-slider'],
        },
      },
    },
  },
});
```

### 8.2 Memoization Strategy

```tsx
// Optimize expensive calculations
import React, { useMemo } from 'react';

const PriceSummary = memo(({ pricing }: { pricing: any }) => {
  const summary = useMemo(() => {
    return {
      totalTasks: Object.values(pricing).filter(v => v > 0).length,
      averagePrice: calculateAverage(pricing),
      maxPrice: Math.max(...Object.values(pricing)),
      minPrice: Math.min(...Object.values(pricing)),
    };
  }, [pricing]);
  
  return (
    <div>{/* Display summary */}</div>
  );
}, (prev, next) => prev.pricing === next.pricing);

export default PriceSummary;
```

### 8.3 Virtual Scrolling for Large Tables

```tsx
// Use tanstack virtual for performance
import { useVirtualizer } from '@tanstack/react-virtual';

const VirtualizedTable = ({ data }) => {
  const parentRef = useRef<HTMLDivElement>(null);
  
  const virtualizer = useVirtualizer({
    count: data.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 50,
    overshoot: 200,
  });
  
  return (
    <div ref={parentRef} style={{ height: '500px', overflow: 'auto' }}>
      <div style={{ height: `${virtualizer.getTotalSize()}px`, position: 'relative' }}>
        {virtualizer.getVirtualItems().map((virtualRow) => (
          <div
            key={virtualRow.key}
            style={{
              position: 'absolute',
              top: 0,
              transform: `translateY(${virtualRow.start}px)`,
            }}
          >
            {/* Row content */}
          </div>
        ))}
      </div>
    </div>
  );
};
```

---

## 📝 Appendix

### A. Design System References

- [shadcn/ui Component Library](https://ui.shadcn.com/)
- [Tailwind CSS Utility Classes](https://tailwindcss.com/docs)
- [Radix UI Primitives](https://www.radix-ui.com/)

### B. Testing Recommendations

```bash
# Unit tests (Vitest)
npm run test

# Integration tests (Playwright)
npm run test:e2e

# Visual regression tests
npm run test:visual
```

### C. Build Optimization Checklist

- ✅ Tree-shaking enabled (ES modules only)
- ✅ Gzip/Brotli compression
- ✅ Image optimization (WebP format)
- ✅ Lazy loading for routes
- ✅ Dynamic imports for heavy components
- ✅ Minification with source maps (dev only)

---

**最后更新**: 2026-09-20  
**维护者**: Frontend Team
