# 导出报表的异步 + Excel 方案（未采用）

`server/app/export_controller.py` 在 main 上是 169 行的**同步 CSV** 实现，随
PR #172 合入并已上线。这里保留的是同一功能的另一套设计，来自 superadmin 原型：

| | main 现役（169 行） | 本目录（475 行） |
|---|---|---|
| 路由 | `POST /reports/export` | `POST /api/admin/reports/export` + `GET .../{export_id}/status` |
| 响应 | 直接回 `Response` | `ExportResponse` 模型 + 异步状态轮询 |
| 格式 | 仅 CSV | CSV + `generate_excel_sheet_data` |

**两者不是新旧关系，是两个方向。** 之所以不直接覆盖 main 的版本：后者正在线上
服务，而本方案的异步任务、状态存储与 Excel 依赖都还没有对应的落地设计。

要采纳时需要先回答：导出任务存哪（复用 generation_tasks 还是新表）、Excel 由谁
生成（进程内还是 worker）、状态轮询与现有任务轮询如何统一。

配套测试是 `test_export_controller.async-excel.py`（463 行），对应 main 上已合入
的 `tests/test_export_controller.py`（377 行）。

---

# structlog 结构化日志（未采用）

`logging_config.structlog.py`（134 行）原本放在 `server/app/logging_config.py`。
它 `import structlog`，而 **structlog 既不在 `server/pyproject.toml` 里、虚拟环境
里也没装**——放进 `server/app/` 会让 `mypy server/app` 直接失败，CI 必红。

采纳前需要：把 structlog 加进依赖并锁定、确认与现有 `logging.getLogger(__name__)`
调用点的迁移路径、以及 worker 与 API 两侧的输出格式是否统一。
