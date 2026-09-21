# 超管系统开发任务清单

**版本**: V1.0  
**状态**: 待执行  
**Worktree**: `feat/admin-rbac-superadmin-20260917`  
**参考文档**: 
- [超管系统 API 设计规范](../specs/superadmin-api-specification.md)
- [价格 UI 架构设计](../specs/superadmin-pricing-ui-architecture.md)
- [定价同步技术设计](../specs/superadmin-pricing-sync-tech-spec.md)

---

## 📋 任务分解总览

### Phase 1: 项目基础设施 (预估：1 天)

#### 1.1 Python 项目骨架
- [ ] **TASK-001**: 创建 FastAPI 应用入口 (`app/main.py`)
  - 配置 FastAPI 实例
  - 添加 CORS 中间件
  - 注册路由和异常处理器
  - 设置 Prometheus 监控端点
  
- [ ] **TASK-002**: 配置管理模块 (`app/config/settings.py`)
  - 环境变量加载（Pydantic Settings）
  - JWT 密钥生成与管理
  - 数据库连接字符串
  - Superadmin API 配置
  
- [ ] **TASK-003**: 数据库连接池配置 (`app/database.py`)
  - SQLAlchemy async engine 初始化
  - SessionLocal factory 函数
  - 连接池参数优化
  
- [ ] **TASK-004**: Alembic 初始迁移脚本生成
  - 生成基础迁移表（alembic_version）
  - 配置文件路径验证

---

### Phase 2: 数据模型层 (预估：1 天)

#### 2.1 SQLAlchemy ORM Models
- [ ] **TASK-005**: 定义租户模型 (`app/models/tenant.py`)
  ```python
  class Tenant(Base):
      id: str (PK, unique)
      name: str
      status: enum (active/suspended/closed)
      owner_admin_id: FK
      created_at, updated_at
  ```
  
- [ ] **TASK-006**: 定义定价配置模型 (`app/models/pricing.py`)
  ```python
  class PricingSnapshot(Base):
      tenant_id: str (FK + PK)
      version: int (incrementing)
      config_json: JSONB
      effective_from: datetime
      expires_at: datetime
  ```
  
- [ ] **TASK-007**: 定义管理员模型 (`app/models/admin_user.py`)
  ```python
  class AdminUser(Base):
      id: str (PK)
      tenant_id: str (nullable for super_admin)
      email: str (unique)
      role: enum (super_admin/admin/employee)
      hashed_password: str
  ```
  
- [ ] **TASK-008**: 定义充值订单模型 (`app/models/recharge_order.py`)
  ```python
  class RechargeOrder(Base):
      id: str (PK)
      tenant_id: str (FK)
      amount_fen: int
      status: enum (pending/completed/refunded)
  ```
  
- [ ] **TASK-009**: 创建 API Key 模型 (`app/models/api_key.py`)
  ```python
  class APIKey(Base):
      id: str (PK)
      user_id: FK
      key_hash: str (encrypted)
      scopes: list[Permission]
      expires_at: datetime
  ```

#### 2.2 Database Migrations
- [ ] **TASK-010**: 编写 Alembic 迁移脚本 (versions/001_initial_schema.py)
  - 创建 tenants 表
  - 创建 pricing_snapshots 表
  - 创建 admin_users 表
  - 创建 recharge_orders 表
  - 创建 api_keys 表
  - 添加索引和约束

---

### Phase 3: 认证授权系统 (预估：1 天)

#### 3.1 JWT 认证
- [ ] **TASK-011**: 实现 JWT 工具类 (`app/core/jwt.py`)
  - `create_access_token()` - 生成 JWT
  - `decode_access_token()` - 解析并验证
  - Token 过期检查
  
- [ ] **TASK-012**: 密码哈希工具 (`app/core/security.py`)
  - `hash_password()` - bcrypt hashing
  - `verify_password()` - 密码比对
  
- [ ] **TASK-013**: 认证依赖注入 (`app/api/deps.py`)
  - `get_current_super_admin()` - JWT 验证
  - `get_api_key_auth()` - API Key 验证
  - RBAC 权限装饰器

#### 3.2 API Key 管理
- [ ] **TASK-014**: API Key CRUD 接口 (`app/api/routers/api_keys.py`)
  - POST /api/v1/admins/{id}/api-keys - 创建密钥
  - GET /api/v1/admins/{id}/api-keys - 列表查询
  - DELETE /api/v1/api-keys/{key_id} - 撤销密钥

---

### Phase 4: 核心 API 端点开发 (预估：2 天)

#### 4.1 租户管理 (预估：4 小时)
- [ ] **TASK-015**: GET /api/v1/tenants (租户列表)
  - 分页支持 (skip, limit)
  - 过滤条件 (status, search)
  - 返回字段控制
  
- [ ] **TASK-016**: GET /api/v1/tenants/{tenant_id} (单个租户详情)
  - 关联查询 wallet + pricing
  
- [ ] **TASK-017**: POST /api/v1/tenants (创建新租户)
  - 输入验证 (Pydantic schema)
  - 自动创建关联 wallet + owner admin
  
- [ ] **TASK-018**: PATCH /api/v1/tenants/{tenant_id} (更新租户信息)
  - 部分更新 (PATCH semantics)
  - last super_admin 保护
  
- [ ] **TASK-019**: POST /api/v1/tenants/{tenant_id}/suspend (暂停/启用)
  - 状态流转逻辑
  - 审计日志记录

#### 4.2 定价配置 (预估：6 小时)
- [ ] **TASK-020**: PUT /api/v1/pricing/{tenant_id} (设置定价)
  - Pydantic 验证 pricing config
  - 版本号自增
  - 时间范围验证
  
- [ ] **TASK-021**: GET /api/v1/pricing/{tenant_id} (获取定价快照)
  - current/historical 两种模式
  - as_of 时间戳查询
  
- [ ] **TASK-022**: GET /api/v1/pricing (批量获取所有定价)
  - active_only 过滤选项
  - 批量导出格式
  
- [ ] **TASK-023**: POST /api/v1/pricing/bulk (批量导入 CSV)
  - File upload 处理
  - CSV parsing + validation
  - dry_run 模式支持
  - 错误汇总报告
  
- [ ] **TASK-024**: GET /api/v1/pricing/export (导出报表)
  - 支持 CSV/XLSX 格式
  - 历史版本 inclusion

#### 4.3 Allowance Check API (Client 系统调用) (预估：3 小时)
- [ ] **TASK-025**: GET /api/v1/allowance/{tenant_id} (查询可用额度)
  - API Key 认证
  - balance + credit_limit 计算
  - can_submit_new_tasks 判断
  
- [ ] **TASK-026**: POST /api/v1/allowance/reserve (预留额度)
  - reservation_id 生成
  - 临时 hold 机制
  - TTL 超时自动释放

- [ ] **TASK-027**: POST /api/v1/allowance/release (释放额度)
  - committed=true → 实际扣减
  - committed=false → 仅解除 hold

---

### Phase 5: 辅助功能与报表 (预估：1 天)

#### 5.1 利润统计
- [ ] **TASK-028**: GET /api/v1/profit/platform (平台总利润)
  - wholesale - upstream_cost 计算
  
- [ ] **TASK-029**: GET /api/v1/profit/{tenant_id} (单租户利润)
  - retail_price - wholesale_price 计算

#### 5.2 充值订单
- [ ] **TASK-030**: GET /api/v1/recharge-orders (订单列表)
  - tenant_id 过滤
  - status 过滤
  - date_range 过滤
  
- [ ] **TASK-031**: POST /api/v1/recharge-orders/{id}/refund (退款)
  - 审批流程集成
  - 财务审批记录

#### 5.3 审计日志
- [ ] **TASK-032**: GET /api/v1/audit-logs (审计日志查询)
  - pagination + filtering
  - append-only read only

---

### Phase 6: 前端应用开发 (预估：1 周)

#### 6.1 项目初始化 (预估：半天)
- [ ] **TASK-033**: 创建 React + TypeScript 项目
  ```bash
  npm create vite@latest superadmin-client -- --template react-ts
  ```
  
- [ ] **TASK-034**: 安装并配置 shadcn/ui
  - Tailwind CSS setup
  - Component library initialization
  
- [ ] **TASK-035**: 配置状态管理
  - Zustand store setup
  - TanStack Query setup

#### 6.2 基础布局组件 (预估：半天)
- [ ] **TASK-036**: 实现 Sidebar 导航组件
- [ ] **TASK-037**: 实现 Header 组件（登录态显示）
- [ ] **TASK-038**: 主布局模板 (Layout.tsx)

#### 6.3 定价管理页面 (预估：2 天)
- [ ] **TASK-039**: 实现 PricingTable 组件
  - TanStack Table integration
  - PriceCard 渲染
  - Pagination control
  
- [ ] **TASK-040**: 实现 PriceEditDialog 表单
  - React Hook Form + Zod 验证
  - PriceInputField + DiscountSlider
  
- [ ] **TASK-041**: 实现 BulkPricingUploader 组件
  - CSV 文件上传拖拽区
  - Preview DataTable
  - Validate and Commit 按钮

- [ ] **TASK-042**: 整合 PricingPage.tsx 主页面
  - usePricingList hook
  - query client management

#### 6.4 租户管理页面 (预估：1 天)
- [ ] **TASK-043**: 实现 TenantList 表格组件
- [ ] **TASK-044**: 实现 TenantDetailPanel 侧边抽屉
- [ ] **TASK-045**: 实现 Suspend/Activate modal 对话框

#### 6.5 充值订单管理页面 (预估：半天)
- [ ] **TASK-046**: 实现 RechargeOrdersTable 组件
- [ ] **TASK-047**: 实现 Refund modal 对话框

#### 6.6 利润报表页面 (预估：1 天)
- [ ] **TASK-048**: 实现 ProfitOverviewCharts 图表组件
  - Recharts bar chart
  - Profit trend line chart
  
- [ ] **TASK-049**: 实现 ExportReport modal 对话框
  - CSV/XLSX下载

#### 6.7 主题国际化 (预估：1 天)
- [ ] **TASK-050**: 配置 i18n 多语言支持
  - zh-CN.json / en-US.json翻译文件
  
- [ ] **TASK-051**: 实现 ThemeProvider (暗黑模式切换)
  - Tailwind dark mode variants
  - CSS variables dynamic switching

---

### Phase 7: 测试与质量保证 (预估：2 天)

#### 7.1 单元测试
- [ ] **TASK-052**: 编写后端测试用例 (pytest)
  - tests/test_tenants.py (20+ cases)
  - tests/test_pricing.py (15+ cases)
  - tests/test_auth.py (10+ cases)
  - tests/test_allowance.py (8+ cases)
  
- [ ] **TASK-053**: 覆盖率目标 >80%
  - pytest-cov configuration
  - GitHub Actions coverage badge

#### 7.2 E2E 测试
- [ ] **TASK-054**: 配置 Playwright E2E 框架
- [ ] **TASK-055**: 编写关键流程测试
  - login_flow.spec.ts
  - pricing_crud.spec.ts
  - bulk_import.spec.ts

#### 7.3 压力测试
- [ ] **TASK-056**: 配置 k6/JMeter 压力测试脚本
- [ ] **TASK-057**: 定义 SLA 标准
  - P95 latency < 200ms
  - Error rate < 0.1%

---

## 🎯 执行顺序建议

```
Day 1: TASK-001 ~ TASK-010 (基础设施 + 数据模型)
Day 2: TASK-011 ~ TASK-014 (认证授权)
Day 3-4: TASK-015 ~ TASK-027 (核心 API 开发)
Day 5: TASK-028 ~ TASK-032 (辅助功能)
Week 2: TASK-033 ~ TASK-051 (前端应用开发)
Week 3: TASK-052 ~ TASK-057 (测试与质量保障)
```

---

## ✅ 完成标准

每个任务必须满足：
- [ ] Code Review 通过
- [ ] 单元测试覆盖率 ≥80%
- [ ] 本地运行测试全部 Pass
- [ ] 符合 Biome 代码规范
- [ ] Git commit message 符合规范
- [ ] 相关文档已更新

---

**下一步**: 请输入 `"开始第一个任务"` 或具体任务编号（如 "TASK-001"）以立即开始实施！
