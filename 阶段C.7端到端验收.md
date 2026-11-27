# 阶段 C.7 端到端验收与构建产物登记

## 实现范围

Android 规格确认回执可导出为一次性验收快照，包含确认事件 ID、规格 SHA-256 和已保存规格。C.7 验收器将该快照传给 C.2 任务仓储，校验确认事件幂等绑定后运行 C.5/C.6 生成链。此快照只用于验收，不改变正式接收服务的数据流。

任务报告在成功快照中登记模型提供方、模型版本、提示版本、累计已结算费用和模型调用状态，并保留 C.3 源码修订摘要、静态检查、C.4 隔离构建/测试/预览报告、确认规格摘要及成功 ZIP SHA-256。`plugin_delivery.py lookup <sha256>` 可从包摘要反查登记记录；`export-sha <sha256> --output <path>` 从 SQLite 不可变构建快照导出原始 ZIP 字节。导出目标已存在时拒绝覆盖。构建状态保持 `built / deliverable: false`。

## 复现

以下命令在 Windows PowerShell 中运行。模拟器确认输出文件仅作为本轮验收的中间文件：

```powershell
$confirmed = Join-Path $env:TEMP 'stage-c7-confirmed.json'
python scripts/verify-stage-b6.py --android --serial emulator-5554 --confirmed-output $confirmed
python scripts/verify-stage-c7.py --confirmed-snapshot $confirmed `
  --qemu "$env:LOCALAPPDATA/AndroidPluginAgent-C4/qemu-runtime/qemu-system-x86_64.exe" `
  --image "$env:LOCALAPPDATA/AndroidPluginAgent-C4/image"
```

无模拟器快照时，C.7 固定桌面回归仍可单独运行：

```powershell
python scripts/verify-stage-c7.py
```

成功任务的摘要登记入口可直接检查：

```powershell
python scripts/plugin_delivery.py --store <任务库目录> lookup <ZIP的SHA-256>
python scripts/plugin_delivery.py --store <任务库目录> export-sha <ZIP的SHA-256> --output <新ZIP路径>
```

## 验收结果

- Android API 37 / WebView 145 模拟器完成 Quick Notes 规格审阅与确认。验收导出的确认事件 ID 和保存摘要被 C.2 任务仓储接受，生成任务绑定相同项目、版本和规格摘要，随后通过 C.4 离线 QEMU 构建、测试及 Chromium 交互预览。
- Quick Notes 与手动联系人簿均通过成功构建、SHA-256 登记、按摘要反查和 ZIP 字节一致导出。固定模型响应修复用例另核对模型/提示版本、源码补丁修订、隔离检查及已结算费用。
- 清单/隔离检查失败不会创建成功产物登记；非法摘要和已存在的导出目标被拒绝。C.2、C.3、C.5、C.6 固定回归通过。
- C.7 使用固定模型提供方，没有调用真实模型；费用记录来自固定响应账本。浏览器交互使用 C.4 隔离夹具及合成数据桥。C.7 未在 Android 导入或运行生成包，未执行真机验证，候选包回传和导入验收留给阶段 D。
