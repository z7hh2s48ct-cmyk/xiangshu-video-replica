# 本目录的来历（2026-09-21 归并）

超管 / 租户 / 分销这条线此前散在两处，都没进 git 管理或没合入 main：

| 来源 | 形态 | 内容 |
|---|---|---|
| 分支 `feat/admin-rbac-superadmin-20260917` | 2 个提交，从未合并 | `docs/superpowers/specs/2026-09-17-超管与加盟商分销架构-design.md`（295 行）|
| worktree `main-local-20260920` | **61 个未跟踪文件，一个提交都没有** | 本目录全部内容 + 三份 specs + `server/app/logging_config.py` |

后者的风险是实打实的：未跟踪文件不在任何 commit 里，`git worktree remove` 会
直接抹掉，reflog 也救不回来——15786 行离"一次误操作就没了"只差一条命令。

归并后三样东西的关系：

```
2026-09-17-超管与加盟商分销架构-design.md   设计（9/17）
本目录 superadmin-server / superadmin-client   实现原型（9/20）
PR #174 feat/customer-center-v2               子账号体系（9/19–21，进行中）
```

前两个是 PR #174 那条线的前置设计与原型。本分支**只做保全，不合入 main**：原型
是独立 FastAPI + React 应用，与主仓的双泳道架构尚未对齐，合进去会引入一套平行的
models 与路由。正式采纳应在 #174 落地后单独评估。

## 已知与 main 的交集

- `server/app/logging_config.py`（134 行）main 没有，按原路径放置。
- `server/app/export_controller.py` main 已有 169 行的同步 CSV 实现并已上线；
  原型里 475 行的异步 + Excel 版本保留在 `proposals/`，**没有覆盖**现役实现。
  详见 `proposals/README.md`。
