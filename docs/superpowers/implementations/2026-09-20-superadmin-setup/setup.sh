#!/bin/bash

# ========================================
# 超管系统快速启动脚本
# 2026-09-20 Implementation
# ========================================

set -e

echo "🚀 开始部署超管系统..."

# 设置路径
PROJECT_ROOT="/Users/honor.pei/Documents/订单项目/.worktrees/ADMIN-RBAC-SUPERADMIN-20260917/superadmin-server"
cd "$PROJECT_ROOT" || exit 1

# Step 1: 创建虚拟环境
echo "📦 Step 1: 创建 Python 虚拟环境..."
python3 -m venv .venv

# Step 2: 激活虚拟环境
echo "🔧 Step 2: 激活虚拟环境..."
source .venv/bin/activate

# Step 3: 安装依赖
echo "📚 Step 3: 安装项目依赖..."
pip install --upgrade pip
pip install -r pyproject.toml

# Step 4: 创建数据库
echo "💾 Step 4: 创建 PostgreSQL 数据库..."
createdb superadmin_dev || echo "⚠️  数据库已存在，跳过创建"

# Step 5: 生成 Alembic 初始迁移
echo "🔄 Step 5: 初始化 Alembic 迁移..."
if [ ! -d "alembic" ]; then
    alembic init alembic || echo "Alembic 可能已初始化"
fi

# Step 6: 复制配置文件（如果存在）
if [ -f ".env.example" ]; then
    cp .env.example .env.local
    echo "📝 已创建 .env.local 配置文件，请根据需要修改"
else
    echo "⚠️  未找到 .env.example，将使用默认配置"
fi

# Step 7: 运行数据库迁移
echo "🏗️ Step 6: 运行数据库迁移..."
alembic upgrade head

echo "✅ 所有步骤完成！"
echo ""
echo "📍 下一步操作："
echo "1. 检查 .env.local 中的数据库配置是否正确"
echo "2. 运行 'uvicorn app.main:app --reload' 启动开发服务器"
echo "3. 访问 http://localhost:8001/docs 查看 API 文档"
echo ""
echo "🎉 超管系统准备就绪！"
