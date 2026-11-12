# 变更记录

记录仓库中源代码、配置、依赖、资源、构建脚本和项目文档的每次变更。按时间倒序追加；每条记录说明改动、目的或影响，以及验证结果。详细规则见根目录 `AGENTS.md`。

## 2026-11-13

- **变更：**完成阶段 0.6 阶段门评审，新增 `阶段0评审.md`，汇总 0.1–0.5 证据、采用方案、安全/兼容/交付/成本风险、负责角色、复审与关闭标准、未决项及阶段 1.1–1.5 的准入和验收任务；同步 `计划文档.md`、`项目介绍.md`、两份实验说明和 `android-app/验证记录.md`。
- **原因/影响：**明确有条件进入阶段 1 的受限宿主开发，并保留真机/最低版本、动态隔离、完整包校验、来源信任及费用控制的阻塞点；澄清电脑端路径检查、HTTP 资源预览与正式沙箱/交互/导入验收的边界，避免把实验通过误当作交付资格。本次仅修改项目文档。
- **验证：**`python scripts/desktop_experiment.py all`、`node scripts/verify-bridge.mjs browser` 均退出 0，静态包 SHA-256 与既有记录一致；只读核对合法 ZIP 的 CRC、资源散列、文件清单、ID/版本正则及 4 个损坏夹具特征通过（不等于完整 Schema 或 Android 导入验证）；在 `android-app/` 使用会话级 JDK 17/SDK 配置执行 `.\gradlew.bat :app:assembleDebug` 退出 0，35 个任务均为最新状态；Markdown 本地链接检查及 `git diff --check` 通过。`adb devices -l` 为空，未重新安装或运行 Android 回归；本次未重跑 Lint，保留既有 targetSdk 错误和兼容性缺口。

- **变更：**完成阶段 0.5 电脑端实验：新增仅依赖 Python 标准库的 `scripts/desktop_experiment.py`，实现任务工作区边界检查、确定性静态 ZIP 构建、loopback 预览检查和稳定失败记录；新增 `电脑端实验.md`、实验产物与 Python 缓存忽略规则，并同步阶段计划和项目说明。
- **原因/影响：**为隔离工作区、可重复样例包、电脑端预览及构建失败处理提供可复现证据；实验不提前实现 AI 生成服务、动态 Android 导入、正式插件隔离或自动送达。
- **验证：**`python scripts/desktop_experiment.py all` 通过；合法包清单、资源散列和预览资源 HTTP 200 检查通过，工作区越界/缺少清单/截断 ZIP 分别返回 `WORKSPACE_ESCAPE`、`MISSING_MANIFEST`、`INVALID_ZIP`；连续两次构建 SHA-256 均为 `d9c5804252fbd3e7f89cdf8f26924ed53594b45d19201e62c53e5394efb6d679`；`python -m py_compile scripts/desktop_experiment.py`、`git diff --check` 通过；在 `android-app/` 执行 `.\gradlew.bat :app:assembleDebug` 退出 0，`BUILD SUCCESSFUL`，35 个任务均为最新状态。

- **变更：**完成阶段 0.4 固定内置页面的最小桥接：新增 `MinimalBridge.kt`、Browser/Android Adapter 与实验资源、原生 ui.toast 授权/撤销和执行计数；收紧 WebView 资源白名单与 CSP，按真实来源/主框架/精确入口/身份/版本/能力/授权/参数校验请求，并限制 JSON 大小、嵌套及重复字段。新增无 npm 依赖的 `scripts/verify-bridge.mjs` 与 `桥接实验.md`，同步计划、项目说明、协议草案和 Android 验证记录；保留此前未提交的构建补验记录。
- **原因/影响：**为受限请求往返和越权拒绝提供可重复证据，明确浏览器只模拟、Android 实际执行；实验限于宿主固定 ui.toast 身份，不提前实现或宣称动态导入、插件数据接口、多插件隔离与生成服务。
- **验证：**在 `android-app/` 单独执行 `.\gradlew.bat :app:assembleDebug` 退出 0，模拟器覆盖安装成功；浏览器与 Android 37/WebView 145 的验证脚本均退出 0，覆盖默认拒绝、原生授权往返、撤销、伪造身份/版本、未声明能力、非法/超大/重复 JSON、同源及 opaque-origin iframe、无副作用计数、错误页返回和重启保持/授权清空；适配器不可用、超时与并发关联用显式假传输测试。`lintDebug` 分析完成但因原有 `targetSdk = 28` 的 `ExpiredTargetSdkVersion` 退出 1（1 error、4 项原有 warnings），本次新增的监听器清理特性检查警告已修复；首次工具下载停滞后用命令级代理重试，最终离线复查，详情和 APK 摘要见验证记录。`git diff --check` 通过；真机、最低 API 23、旧 WebView 和动态插件能力未验证。

## 2026-11-12

- **变更：**在 `android-app/验证记录.md` 补录此前因缺少 Java 环境变量而未启动的 Android 构建结果，并提供使用本机 JDK 17 和 Android SDK 的会话级配置命令；未修改源码、构建配置或全局环境变量。
- **原因/影响：**确认 Temurin 17.0.21+9 已安装，阻塞源于当前会话未设置 `JAVA_HOME` 和 PATH；完成阶段 0.3 变更后的构建补验并保留原始失败记录。
- **验证：**在 `android-app/` 设置会话环境后执行 `.\gradlew.bat :app:assembleDebug`，退出码 0、`BUILD SUCCESSFUL`，35 个任务均为最新状态；核对 APK 大小、SHA-256、`aapt dump badging` 和测试页 ZIP 条目通过，产物仍被 Git 忽略；`git diff --check` 通过。本次未重新执行设备安装或运行测试。

- **变更：**完成阶段 0.3 插件协议草案，新增 `插件协议草案.md`、Draft 2020-12 清单 Schema、最小静态插件源文件及 1 个合法和 4 个损坏 ZIP 固定夹具；更新 `计划文档.md` 的阶段状态。
- **原因/影响：**明确 manifest 字段、资源路径与 SHA-256、ZIP 大小/条目/压缩比限制、宿主接口版本兼容策略、导入错误分类和后续验收准则，为阶段 1.1 导入契约提供可复核基线。协议仍是草案，校验/安装器与设备行为尚未实现。
- **验证：**夹具脚本可重复生成相同归档；通过 Schema JSON/本地引用检查、合法包 CRC/清单/资源 SHA-256 核对，以及缺清单、路径穿越、重复条目和截断包触发检查；`git diff --check` 通过。按规范尝试在 `android-app/` 执行 `.\gradlew.bat :app:assembleDebug`，但当前会话未配置 `JAVA_HOME` 且 PATH 无 `java`，因此构建未启动；此前记录的 JDK 17 不在当前可用环境中。

- **变更：**补录 `Medium_Phone_API_37.0` 上阶段 0.2 的 APK 安装、WebView 资源加载、进程重启计数保持及缺失资源错误页/返回流程实测结果；更新计划文档中的阶段 0.1/0.2 状态。
- **原因/影响：**ADB 授权恢复后完成此前未能执行的设备验收，为宿主加载原型提供 Android 17/API 37 与 WebView 145.0.7632.218 的实际运行证据，并列明未覆盖设备范围。
- **验证：**在 `android-app/` 运行 `.\gradlew.bat :app:assembleDebug` 成功；`adb install -r` 成功；模拟器启动、进程重启和错误页交互通过。未验证真机、Android 23 及其他 WebView 版本。

- **变更：**在 `AGENTS.md` 增加 GitHub 推送规则，直连失败时使用本机 `127.0.0.1:7897` HTTP 代理，并通过命令级配置避免影响全局 Git 设置。
- **原因/影响：**为 GitHub 连接受限时提供明确的推送回退方式。
- **验证：**`git diff --check`；本次仅修改文档，未运行 Android 构建。

- **变更：**更新 Android 宿主原型，使用 `WebViewAssetLoader` 加载 APK 内置资源，关闭 WebView file/content 访问并限制页面来源；增加明确的资源错误页，以及用于复核加载和重启状态的测试页计数与错误触发入口。忽略 Kotlin 构建缓存目录，并同步更新阶段 0.1/0.2 状态和验证记录。
- **原因/影响：**补齐宿主资源加载、导航边界和错误呈现的可验证实现；记录环境基线，并准确标注模拟器 ADB 未授权导致的设备验收缺口。
- **验证：**在 `android-app/` 使用 Temurin 17.0.21+9、Gradle Wrapper 8.9 执行 `:app:assembleDebug --no-daemon`，`BUILD SUCCESSFUL`；尝试安装/启动时模拟器返回 `unauthorized`，因此未完成页面、重启和错误交互验证，详情见 `android-app/验证记录.md`。

- **变更：**新增 `计划文档.md`，按参考文档的主要章节组织本项目的目标范围、需求、技术方案、阶段任务、验收与风险。
- **原因/影响：**为 Android 插件宿主和 AI Agent 生成链路建立规划基线，区分现有原型与后续目标。
- **验证：**人工对照 `项目介绍.md`、现有 Android 工程和 `android-app/验证记录.md` 检查计划内容；仅修改 Markdown 文档，未运行 Android 构建或设备测试。

- **变更：**新增 `AGENTS.md` 项目开发规范和本变更记录文件。
- **原因/影响：**统一 Android 原型的开发、验证和文档要求，并确保后续每项仓库变更都有记录。
- **验证：**人工检查文件内容；本次仅新增 Markdown 文档，未运行 Android 构建。
