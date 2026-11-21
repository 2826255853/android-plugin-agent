# 阶段 A.3 持久收件箱

## 接收与恢复

启动 Windows HTTPS 接收服务后，在本机控制台输入 `inbox` 查看消息 ID、项目 ID、接收时间、状态和正文。启动命令见 [A.2 电脑接收服务](阶段A.2电脑接收服务.md)。

- `POST /v1/messages` 在 SQLite 提交消息及回执后返回 HTTP 200 和 `message.receipt`，状态为 `received`。该状态表示电脑已保存消息。
- 已配对设备在 24 小时重试窗口内重发同一条消息时，服务返回原回执 ID、状态和接收时间。响应的 `inReplyTo` 指向本次请求。以同一消息 ID 发送不同内容会收到 HTTP 409 和 `MESSAGE_ID_CONFLICT`，原消息仍可查询。
- `POST /v1/status` 只允许已认证设备查询自己的消息。已提交的消息在重启后返回 `received` 及原接收时间。若进程在提交前中断，查询返回 `MESSAGE_NOT_FOUND`，设备可以在窗口内重发。若进程在提交后、回执送达前中断，重发会取回原回执。

## 本机存储与清理

- 数据库位于 `%LOCALAPPDATA%\AndroidPluginAgentReceiver\inbox.sqlite3`，继承服务数据目录的 Windows ACL，仅限当前帐户和 SYSTEM 访问。正文以明文存于数据库及 SQLite 事务文件，配对凭据验证材料和 TLS 身份由同目录内独立的 DPAPI 文件保护。网络侧只允许设备查询自己的状态，正文通过本机控制台查看。
- 收件箱最多保存 256 条消息，由操作者清理。满载时，新消息收到可重试的 HTTP 503 和 `INTERNAL_ERROR`，已保存消息仍可查询和重发。输入 `inbox clear CONFIRM` 会清空全部消息及回执，并显示删除条数。清理后，旧消息的状态和去重记录也随之移除。命令仅操作收件箱数据库。
- 接收日志写入标准错误，记录消息 ID、时间、接收或重发状态及固定错误分类。正文通过 `inbox` 显式查看。

## 桌面端验收

```powershell
python scripts/verify-message-protocol.py
python scripts/verify-stage-a2.py
# 可绑定本机选定的局域网 IPv4 地址
python scripts/verify-stage-a2.py --bind 192.168.1.23
```

2026-11-21 在 Windows、Python 3.15.0、SQLite 3.53.4 和 OpenSSL 3.5.9 上通过回环与本机以太网接口回归。回归用虚构设备和临时数据库，通过实际 HTTPS 请求验证持久回执、并发去重、内容和序号冲突、设备隔离、损坏及超限请求和清理。测试分别在 SQLite 执行提交前、提交完成但发出回执前强制退出接收进程，随后重启并核对数据库表现和回执。Android 发送联调归入 A.4。
