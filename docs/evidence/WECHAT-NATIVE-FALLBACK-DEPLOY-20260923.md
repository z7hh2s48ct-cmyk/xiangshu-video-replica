# WECHAT-NATIVE-FALLBACK-DEPLOY-20260923 · 掉单兜底的部署要求（回补）

> 本文回补 PR #181（`b69e891a`，微信 Native 支付链路加固与掉单兜底）缺失的
> 交付物文档：该 PR 改了 systemd 单元但没留证据文件，发布 runbook 也只在首次
> 安装时写 daemon-reload，升级场景的重装要求无处可查。2026-09-23 上线前检查
> （管理端两天改动评审）发现此缺口，随 `fix/admin-preflight-20260923` 一并补上。

## 变更内容

`deploy/systemd/video-replica-maintenance.service` 新增两条 ExecStart（顺序敏感，
先结算后关单）：

1. `python -m scripts.reconcile_pending_native_orders` —— 主动向微信查询超过回调
   宽限期仍 PENDING 的 Native 单，已支付的走与回调相同的幂等资金路径结算
   （`confirm_recharge_payment`），未支付的留给关单清扫。
2. `python -m scripts.close_expired_native_orders` —— 关闭支付窗口内无人支付的
   单，停止其占据订单页与对账报表；CLOSED 后迟到的回调仍可结算。

## 部署要求（升级场景必做）

滚动发布只替换代码，**不会**自动更新 systemd 单元。凡候选改动
`deploy/systemd/` 下任何文件，必须：

```bash
install -m 0644 deploy/systemd/video-replica-maintenance.service /etc/systemd/system/
systemctl daemon-reload
```

timer 下个触发周期自动生效。漏掉这步时**无报错、无告警**，掉单兜底静默缺失，
掉单继续掉。该步骤已写入 `docs/客户版部署与灰度手册.md` §7 步骤 3。

## 验证方法

- `systemctl cat video-replica-maintenance.service | grep ExecStart` 应列出新增
  的两条命令。
- 手工执行 `python -m scripts.reconcile_pending_native_orders`（在 server 目录、
  带 `VIDEO_REPLICA_DATABASE_URL` 环境），输出 counts 即为扫过的单数；零单也应
  正常退出 0。
- 对账页不再出现长期滞留 PENDING 的 Native 单。

## 边界

- 两个脚本全部配置走 DB settings，不引入新环境变量；无真实微信凭据时脚本按
  现有 provider 配置语义跳过，不误伤其它支付渠道。
- 结算复用回调的幂等路径：同一单重复触发不会重复入账。
