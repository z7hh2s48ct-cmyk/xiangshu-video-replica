# VENDOR-NOTES — weixinshipinhao_publisher

- 来源：https://github.com/phlong026/weixinshipinhao_publisher （commit `ee4babe`，2026-08-10）
- 授权背景：该仓库与本项目同属所有者 phlong026，作为 C5 发布能力的协议实现供应商源码引入。
- 接口性质：非官方协议复刻（视频号助手网页接口），平台改版即可能失效；登录态（Cookie）由
  用户在界面手工粘贴，服务端 Fernet 加密保存，永不入库明文/日志。

## ⚠️ 安全事件记录

原仓库 `main.py` 第 1 行硬编码了一段**真实视频号登录 Cookie 且已公开**。本次引入已彻底
排除该文件；该登录态必须在平台侧作废（退出助手登录/改密），原仓库历史需要 force-push
清理。此处仅记录事件，不复述凭据内容。

## 拷贝内容

| 文件 | 说明 |
| --- | --- |
| `channels_publisher.py` | ChannelsPublisher（视频/图文发布、登录探测、短链解析）；**已删除 `find_default_video()` 与 `main()`（硬编码业务文件名/示例）**；`china_city_centers` import 改为包内相对导入 |
| `china_city_centers.py` | 位置搜索去 IP 偏置的坐标表（本仓库 V1 未用位置，保留以免破坏模块导入） |

## 未拷贝（及原因）

- `main.py`（**泄露 Cookie 所在文件，禁止进入本仓库**）、README、DISCLAIMER.md、
  `.gitignore`、`requirements.txt`（依赖统一收口到 `server/pyproject.toml`）。

## 本仓库改造点

1. `channels_publisher.py`：删除 `find_default_video()` / `main()` / `__main__`；一行相对导入。
2. 运行依赖：`requests`（已在 pyproject）；`opencv-python-headless`（仅未指定封面时抽帧用，
   适配层会尽量自行传封面路径从而触发该路径）、`pillow`（图片尺寸解析，视频链路未用）。

## 已知运行约束

- 适配层总是给 `publish()` 传 `cover_path`（用户选择的封面或预抽帧的 tempfile），避免上游
  默认把抽帧临时文件写进模块目录导致并发互相覆盖。
- 发布短链通过 post_list 匹配解析（best-effort），拿不到时记录为已发布但无短链。

## PUBLISH-DELIVERY-20260917 复用增量

- 正式投递由 `app/publish_delivery.py` 经 `channels_adapter.publish_to_channels` 调用 `ChannelsPublisher.publish()`；Cookie 由
  `app/publish_credentials.py` 从扫码账号 `storage_state` 按 `weixin.qq.com` / `qq.com` 域过滤拼接，不再手工粘贴。
- `schedule` 参数未使用——定时由本仓 `publish_records.scheduled_at` 在 worker 侧到点投递。`post_list` / `get_object_short_link`
  的结果回收在 PR-B（PUBLISH-FALLBACK-SYNC）接入。
