# 阶段 C.5 生成、构建、测试与预览

## 实现范围

`scripts/plugin_generation.py run` 现在作为 C.5 的任务入口。任务没有源码时，协调器从 C.2 绑定的确认规格选择 Quick Notes 或手动联系人簿模板；已有工作区则直接使用 C.3 原子修订后的文件，不重新渲染覆盖补丁。打包前仅允许协调器重算模板已声明资源的 SHA-256，清单身份、规格、宿主 API、能力、数据模型和文件集合必须与可信模板一致。

协调器依次执行本机静态校验、确定性 ZIP 构建及回读、C.4 隔离构建、C.4 隔离测试和 C.4 Chromium 交互预览。构建和测试在离线虚拟机内检查 JavaScript 语法与入口资源；预览从刚生成的 ZIP 解包，在一次性页面夹具中提供仅用于验收的模拟数据桥，实际点击新建、提交表单、保存记录并检查列表及状态。浏览器回归覆盖模板当前实现的保存/返回列表流程，不代表 Android 宿主能力授权或设备验收。

成功报告分别记录 `staticChecks`、`packaging` 和 `isolatedChecks.build/test/preview`，并记录源码摘要、包摘要和 `executedGeneratedCode`。只有全部必需检查通过才会提交 SQLite 成功包快照。隔离故障或交互断言失败会保存稳定错误码、失败阶段和检查标识，不提交新的成功包。导出状态仍为 `built / deliverable: false`。

## 复现

Windows 目标机需要 C.4 的可信 QEMU 可执行文件和镜像目录。先运行桌面固定回归：

```powershell
python scripts/verify-stage-c5.py
```

传入 C.4 运行时后，再执行两个模板的实际隔离构建、测试与浏览器交互：

```powershell
python scripts/verify-stage-c5.py --qemu <QEMU绝对路径> --image <C.4镜像目录绝对路径>
```

运行持久任务时使用同一组隔离配置：

```powershell
python scripts/plugin_generation.py --store <任务库目录> run <task-id> --qemu <QEMU绝对路径> --image <C.4镜像目录绝对路径>
```

阶段说明记录的目标机 C.4 镜像默认位于 `$env:LOCALAPPDATA/AndroidPluginAgent-C4/image`，QEMU 位于其 `qemu-runtime` 子目录。每次隔离运行都由 C.4 协调器校验镜像摘要，结束后清理 Windows Job 和临时目录。

## 回归与边界

`verify-stage-c5.py` 覆盖 C.3 修订保留、确定性打包、隔离构建失败、浏览器交互失败、未生成失败包，以及失败新版本不影响先前成功快照。传入 QEMU 参数后还会对两个正式模板执行真实一次性虚拟机与 Chromium 验收。

页面数据桥只存在于预览夹具，使用合成记录，不连接 Android、电脑接收服务或用户数据。当前仍不自动领取任务、不调用模型、不自动修复失败、不向 Android 传包；这些由 C.6、C.7 和阶段 D 处理。模拟器行为沿用 C.1 的独立验收证据。
