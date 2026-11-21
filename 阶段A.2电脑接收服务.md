# 阶段 A.2 电脑端接收服务

## 启动与管理

在 Windows 的仓库根目录运行。需要 Python 3.12 或更新版本，且其 SSL 模块支持 TLS 1.3。首次启动时使用 PATH 中的 OpenSSL 3 或 Git for Windows 自带的 OpenSSL 生成本机证书。

```powershell
Get-NetIPAddress -AddressFamily IPv4
python scripts/desktop_receiver.py start --bind 192.168.1.23 --port 8443
```

将示例地址换成电脑选定的局域网接口 IPv4 地址。本机验收也可显式选择 `127.0.0.1`。启动后控制台显示 HTTPS 地址和服务器证书 SHA-256 指纹。服务在当前控制台前台运行。

| 控制台命令 | 操作 |
| --- | --- |
| `status` | 查看监听状态、地址和服务器证书指纹。 |
| `pair` | 显示一个有效期最多 10 分钟的六位配对码。下一次执行会替换未使用的码。 |
| `pending` | 查看待确认的申请 ID、设备名称、设备 ID、客户端提供的密钥指纹和过期时间。 |
| `approve REQUEST_ID` / `reject REQUEST_ID` | 核对设备信息后，明确批准或拒绝对应申请。 |
| `devices` | 查看各设备的最近配对状态和有效期。 |
| `revoke DEVICE_ID [REASON]` | 撤销指定设备。原因可选 `user_requested`（默认）、`credential_compromised` 或 `device_removed`。 |
| `inbox` | 查看临时收件箱的消息 ID、项目 ID、电脑接收时间、状态和正文。 |
| `stop` | 关闭监听和当前连接。Ctrl+C 或关闭标准输入也可结束服务。 |

`pair` 和 `inbox` 的输出是供操作者查看的控制台内容，分别包含临时配对码和用户正文。应在本机交互式控制台中查看。普通错误日志只输出固定错误分类。

## HTTPS 传输

客户端先在 TLS 1.3 握手后核对证书 DER 的 SHA-256 指纹，再发送配对码或凭据。请求为 UTF-8 JSON，通过下列路径以 HTTP/1.1 POST 发送，携带 `Content-Type: application/json` 和单个 `Content-Length`。操作字段与响应结构沿用 [v1 消息契约](message-protocol/消息与配对契约.md)。

| 路径 | 请求 | 响应 |
| --- | --- | --- |
| `/v1/pair` | `pair.request` | 使用配对码提交后，原 HTTPS 请求等待电脑确认，最长至申请到期。批准后返回 `pair.confirm`，令牌只出现一次。客户端等待超时需覆盖该申请剩余有效期。 |
| `/v1/messages` | `message.send` | 验证凭据后将消息放入内存收件箱，返回 HTTP 202 和 `status.response` 的 `queued` 状态。 |
| `/v1/status` | `status.query` | 验证凭据后返回本设备指定消息的状态。找不到消息时返回 HTTP 404 和 `MESSAGE_NOT_FOUND`。 |

消息和查询请求通过 `Authorization: Bearer <credential>` 提交令牌，在 JSON 中携带 `deviceId` 和 `credentialId`。缺少或不匹配的凭据返回 `AUTH_REQUIRED`。已撤销设备使用原凭据发起请求时收到 HTTP 403 和 `pair.revoked`。撤销即时生效，重新配对签发新凭据。所有 HTTP 响应关闭连接，并使用 `Cache-Control: no-store`。

服务最多处理 16 条活动连接，TLS 握手和单次 I/O 各限 5 秒，请求头和请求体的读取合计最多 10 秒。配对端点全局限每 60 秒 10 次尝试，等待确认的申请最多 8 条。请求体上限 128 KiB，正文上限 12,000 个 Unicode 字符。

## 本地状态和阶段边界

- TLS 身份和设备注册表保存在 `%LOCALAPPDATA%\AndroidPluginAgentReceiver`，由当前 Windows 帐户的 DPAPI 加密。注册表仅保存随机令牌的 SHA-256 验证摘要及配对元数据。凭据有效期为 90 天，设备状态、撤销和服务器指纹可跨重启恢复。
- 收件箱最多保留 256 条消息，随进程退出清空。满载后新消息返回可重试的 HTTP 503。HTTP 202 的 `queued` 只表示进入临时收件箱。A.3 将实现事务持久化和持久回执，A.4 将接入 Android 配对界面和发送队列。

## 验收入口

```powershell
python scripts/verify-message-protocol.py
python scripts/verify-stage-a2.py
# 也可显式绑定到本机选定的局域网接口
python scripts/verify-stage-a2.py --bind 192.168.1.23
```

A.2 回归使用虚构设备和独立临时存储，通过实际 TLS 请求验证显式绑定、证书核对、配对过期和拒绝、批准、限流、凭据授权、临时消息及状态、撤销、停止后端口关闭和重启。回归核对 DPAPI 存储中的验证摘要，以及日志中没有配对码、令牌和正文。

2026-11-20，Windows / Python 3.15 / OpenSSL 3.5.9 上的回环和本机以太网接口回归均通过。两轮均验证同一端口重启、撤销仅影响目标设备、另一台虚构设备仍可通过认证。A.1 协议夹具及 Android Debug 构建通过。本轮客户端运行在同一台电脑，Android 网络联调由 A.4 和 A.5 覆盖。
