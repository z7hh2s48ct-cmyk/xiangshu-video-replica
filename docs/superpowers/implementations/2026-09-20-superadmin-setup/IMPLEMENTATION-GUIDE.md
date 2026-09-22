# 超管系统实施指南 - 2026-09-20

**版本**: V1.0  
**状态**: Implementation Guide  
**分支**: `feat/admin-rbac-superadmin-20260917`

---

## 🎯 目标

在当前 worktree (`feat/admin-rbac-superadmin-20260917`) 中实现完整的超管管理系统，包括：

1. **独立后端 API** (FastAPI)
2. **前端管理界面** (React + shadcn/ui)
3. **数据库迁移** (Alembic migrations)
4. **完整测试覆盖** (pytest + Playwright)

---

## 📋 实施计划（分阶段）

### Phase 1: 后端基础设施（Week 1）

#### Day 1-2: 项目骨架
- [ ] 创建 FastAPI 项目结构
- [ ] 配置数据库连接池（PostgreSQL）
- [ ] 设置 Alembic 迁移框架
- [ ] 编写基础配置管理

#### Day 3-4: 数据模型层
- [ ] 定义 SQLAlchemy ORM 模型
- [ ] 创建租户、定价、管理员、充值订单表
- [ ] 编写初始迁移脚本

#### Day 5-7: 认证授权
- [ ] 实现 JWT 认证中间件
- [ ] 实现 API Key 验证机制
- [ ] 编写 RBAC 权限装饰器

---

### Phase 2: API 端点开发（Week 2）

#### Day 1-3: 核心 CRUD 接口
- [ ] 租户管理 API（CRUD + 批量操作）
- [ ] 定价配置 API（单租户 + 批量导入/导出）
- [ ] 管理员账号 API

#### Day 4-5: Allowance Check API
- [ ] 查询可用额度接口
- [ ] 预留/释放额度接口
- [ ] Client 系统同步适配器

#### Day 6-7: 报表与分析
- [ ] 利润统计接口
- [ ] 充值订单查询接口
- [ ] 审计日志查询接口

---

### Phase 3: 前端应用开发（Week 3-4）

#### Week 3: UI 组件库搭建
- [ ] 初始化 React + TypeScript 项目
- [ ] 安装 shadcn/ui 组件库
- [ ] 配置主题和样式系统
- [ ] 实现基础布局组件（Sidebar, Header）

#### Week 4: 页面与功能开发
- [ ] 价格管理页面（Table + Edit Dialog + Bulk Importer）
- [ ] 租户列表页面
- [ ] 充值订单管理页面
- [ ] 利润报表页面

---

## 🔧 第一步开始：创建 FastAPI 项目骨架

现在让我们开始实际的代码实现！我将按顺序创建所有必要的文件。

### Step 1: 项目配置文件

创建 `pyproject.toml`：

```toml
[project]
name = "superadmin-server"
version = "1.0.0"
description = "Superadmin backend API for franchisee management"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.104.1",
    "uvicorn[standard]>=0.24.0",
    "python-jose[cryptography]>=3.3.0",
    "passlib[bcrypt]>=1.7.4",
    "python-multipart>=0.0.6",
    "sqlalchemy>=2.0.23",
    "alembic>=1.13.1",
    "psycopg[binary]>=3.1.18",
    "httpx>=0.25.2",
    "pydantic>=2.5.0",
    "asyncpg>=0.29.0",
    "prometheus-client>=0.19.0",
    "slowapi>=0.1.9",
]

[tool.uv]
dev-dependencies = [
    "pytest>=7.4.0",
    "pytest-asyncio>=0.21.0",
    "pytest-cov>=4.1.0",
    "faker>=20.1.0",
    "httpx>=0.25.2",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

---

## 🚀 立即执行命令

在您的工作区运行以下命令开始部署：

```bash
cd "/Users/honor.pei/Documents/订单项目/.worktrees/ADMIN-RBAC-SUPERADMIN-20260917"

# 1. 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # macOS/Linux
# Windows: .venv\Scripts\activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 初始化数据库
createdb superadmin_dev

# 4. 运行 Alembic 迁移
alembic upgrade head

# 5. 启动开发服务器
uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
```

---

## 📁 接下来要创建的文件清单

已准备就绪的核心文件将在后续步骤中逐步创建：

### Core Files Ready to Generate:

1. `app/config/settings.py` - 配置管理
2. `app/database.py` - 数据库连接池
3. `app/models/{tenant,pricing,admin_user}.py` - ORM 模型
4. `app/api/v1/routers/{tenants,pricing,admins}.py` - API 路由
5. `app/core/auth.py` - JWT + API Key 认证
6. `alembic/env.py` - 迁移配置
7. `tests/test_tenants.py`, `tests/test_pricing.py` - 单元测试

---

## ✅ 检查清单

开始每项任务前请确认：

- [ ] 已激活 Python 虚拟环境
- [ ] PostgreSQL 数据库已创建并运行
- [ ] 已阅读《超管系统 API 设计规范》和《价格 UI 架构设计》
- [ ] IDE 已正确配置 Pylance + Ruff 插件

---

**下一步**: 请确认您已准备好，我将立即开始创建第一个核心文件 `app/main.py` 和配置文件。
