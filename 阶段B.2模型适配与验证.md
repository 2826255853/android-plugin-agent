# 阶段 B.2 模型适配与验证

电脑端通过 `ModelBoundary.call` 接收 v1 请求，参数包括请求 UUID、项目和会话 ID、提示版本 `spec-draft-v1`、输入文本、超时和输出 token 限额。返回独立的候选 JSON 与 v1 回执。回执只含请求标识、模型、提示版本、耗时、输出摘要、token 用量、费用预留、结算额、结算精度和稳定错误码。模型候选由阶段 B.4 接入规格 Schema 和确定性审查。

## 调用和预算

- 配置需指定提供方、固定模型和项目累计预算。输入最多 24 KiB UTF-8，候选输出最多 12 KiB UTF-8，单次最多 1,024 个输出 token 和 30 秒。全局最多两次并发，每个项目同时只有一次调用。每个项目会话最多一次首次调用和四次后续调用，包含失败尝试。
- 调用前按最坏 token 用量预留，以纳美元（十亿分之一美元）做整数结算。项目累计预算最大 0.10 美元，额度不足直接返回。重复请求 UUID 拒绝再次调用。超时或取消会通知当前适配器停止后续请求并丢弃迟到的候选。已经发送的网络请求仍可能产生费用，没有可靠用量时保守记入整笔预留。接收服务使用 B.7 的 SQLite 账本持久保存预留、费用和不确定调用状态。
- `mock / fixture-v1` 提供确定性候选和固定用量。OpenAI 官方适配器支持 `openai / gpt-4o-mini-2024-07-18`，已覆盖协议夹具。真实验收采用 `kakou / gpt-6.1-sol`。适配器只发送无工具的文本 JSON 请求。
- 卡扣适配器固定 HTTPS 目标和模型。实际调用前核查公开价目、凭据的美元硬额度、公开费率变体的上界及相同输入的 token 计数。超出 4,096 个输入 token、缺少上界或不符合额度条件会拒绝发起付费请求。保守计费上界为每输入 token 10,000 纳美元（覆盖缓存读写价格），每输出 token 15,000 纳美元（包含推理 token）。按此计算，512 个输出 token 的预留额为 0.04864 美元。
- 卡扣按单次响应报告的用量计算本地费用。仅在凭据用量变化恰好对应这一请求的次数和 token 数时，使用供应商额度扣减额。共享凭据账单无法单独归因时，使用已验证费率上界按实测用量结算，回执标记 `exact_charge: false`。[卡扣公开价目](https://api.kakouai.com/pricing)

## Windows 凭据和复现

接收服务的跨进程预算、调用结算和故障恢复见 [阶段 B.7](阶段B.7预算故障恢复与隐私.md)。本页的 `smoke` 命令使用独立的单次调用预算。

在 PowerShell 使用无回显配置命令。运行时从 Windows 当前用户凭据管理器读取目标 `AndroidPluginAgent/Kakou` 或 `AndroidPluginAgent/OpenAI`。标准输出只显示回执元数据。

```powershell
python scripts/model_cli.py set-kakou-key
python scripts/model_cli.py smoke --provider kakou --model gpt-6.1-sol --budget-nano-usd 50000000
python scripts/model_cli.py smoke --provider mock --model fixture-v1 --budget-nano-usd 25000000
python scripts/verify-stage-b2.py
```

## 2026-11-22 验收

Windows / Python 3.15 上 11 项回归通过，覆盖模拟调用、额度不足、重复、超时、取消、并发、用量结算、JSON 限制、提供方错误和密钥脱敏。凭据测试使用虚构值写入临时的 Windows 凭据目标，完毕后删除测试项。

卡扣模型目录认证返回 200，凭据额度返回 `quota_limited` 和 100 美元上限。使用独立的 0.05 美元项目验收预算发起真实调用。首次取得 122 个输入 token 和 459 个输出 token，当时的结算要求共享凭据账单可精确归因，因而记录费用不确定并保留 0.04864 美元。补齐共享凭据的保守结算后，同样预算下成功返回候选 JSON。提供方报告 129 个输入及 204 个输出 token，0 个缓存 token，本地按上界记入 0.00435 美元。该笔标记为估算上界，实际账单未逐请求归因。两次测试的本地预留合计小于 0.10 美元。OpenAI 官方适配器通过模拟 HTTP 回归，官方服务的直接连接探测超时。
