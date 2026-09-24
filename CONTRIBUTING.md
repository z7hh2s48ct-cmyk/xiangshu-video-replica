# 贡献指南

## 开发环境

见 [README.md](README.md) 的「环境要求」与「快速开始」。

## 分支与提交

- 每个任务一个分支，从最新的 `main` 创建；分支名用 `<类型>/<短横线描述>-<日期>`，
  例如 `feat/viral-local-cache-20260923`、`fix/session-logout-20260923`。
- 一个 PR 只承载一个任务；不要把多个不相关改动混进同一个 PR。
- 合并走 **squash merge**，`main` 受保护。

提交信息建议使用 Conventional Commits 前缀（`feat:` / `fix:` / `docs:` / `refactor:` / `chore:`），
正文用中文说明**为什么**这样改，以及被否决的替代方案（如果有）。

```
feat(viral): 文案提取改由客户端本地抽音轨上传

决策 #15 要求音频抽取在客户端完成，平台不再留存原片……
```

## 提交前必须通过

```bash
npm run check:static     # secret 扫描 + 前端 Biome/tsc/vitest + e2e lint + Tauri cargo + Ruff/mypy
npm run check:sharded    # 四分片并行 pytest（每片独立 PG 容器）
```

两条合起来等价于 CI 的 Linux 质量门。**未运行、失败或跳过的门禁不得记为通过。**

- 新增测试文件后必须重生成分片清单：`python scripts/ci/build-test-shards.py --shards 4`。
- 只跑服务端专项时用 `uv run python -m pytest tests/test_<受影响文件>.py -q`。

## 测试约定

- 服务端测试按用途分三档：纯单元测试、PG 集成测试（用 `tests/pg_test_kit.py` 的隔离库）、
  浏览器 E2E（`e2e/`）。新用例尽量放进已有文件，避免文件数量膨胀。
- 客户端测试与被测组件同目录（`*.test.ts` / `*.test.tsx`），用 Vitest + Testing Library。
- 测试要钉住**行为契约**（尤其是计费、幂等、并发与不确定态），不要断言实现细节。

## 代码风格

- 服务端：Ruff + `ruff format` + mypy strict（`app/` 全量类型检查）。
- 客户端：Biome 统一格式与 lint；`client/src/generated/api.ts` 由 OpenAPI 生成，不手工修改。
- 注释写中文，解释「为什么」而不是复述代码；错误文案面向用户、可读、不出现供应商名称。

## 红线

- 禁止引入 ORM、Redis、消息队列框架；禁止 SQLite/PG 双真源与双写。
- 已发布的 Alembic revision 只可追加修复，不得篡改。
- 任何真实 API key、激活码明文、设备/session token 不得进入代码、日志、测试夹具或 PR。
