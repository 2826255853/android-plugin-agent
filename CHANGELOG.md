# 变更记录

记录仓库中源代码、配置、依赖、资源、构建脚本和项目文档的每次变更。按时间倒序追加；每条记录说明改动、目的或影响，以及验证结果。详细规则见根目录 `AGENTS.md`。

## 2026-11-14

- **变更：**完成阶段 1.4 插件数据接口：新增清单绑定的 `InstalledPluginBridge`、按插件 ID/集合分区的应用私有 `PluginDataStore`、Quick Notes 数据读写界面和临时验收包/Android 模拟器验收脚本；WebKit 依赖升至 1.13.0 以支持特性检测及按来源清理；更新 `插件协议草案.md`、`计划文档.md`、`项目介绍.md`、`阶段0评审.md` 和 `android-app/验证记录.md`。
- **原因/影响：**提供严格来源/身份/声明/参数校验、默认拒绝且分项授权的会话级读写、持久隔离数据和明确卸载数据生命周期；默认保留数据，用户确认删除时才清理，避免静默丢失。数据配额、未实现迁移/升级回滚和设备覆盖边界已记录。
- **验证：**在 `android-app/` 执行 `:app:assembleDebug` 成功；Android 37 / WebView 145 模拟器运行 `node scripts/verify-plugin-data.mjs` 全部通过，覆盖授权、拒绝、重启、隔离及卸载保留/删除；`node scripts/verify-bridge.mjs browser` 与 `node scripts/verify-bridge.mjs android`、`python plugin-protocol/fixtures/verify_fixtures.py`（17 固定、12 边界夹具）、Node/Python 脚本语法检查和 `git diff --check` 通过。`:app:lintDebug --offline` 分析后退出 1，仍为既有 `targetSdk = 28` 的 1 项错误和 4 项警告；未逐项实测数据配额数值边界，未进行真机测试。

## 2026-11-13

- **变更：**完成阶段 1.3 离线插件加载：`LocalPluginStore` 仅恢复清单和 SHA-256 校验均有效的安装记录，并支持确认移除；`MainActivity` 为每个插件建立独立 HTTPS 虚拟来源和受限 `WebViewAssetLoader`，只加载清单资源，限制外部导航、网络及 file/content 访问，提供可恢复错误页与插件运行/移除界面；新增 `scripts/build_stage13_fixtures.py` 用于生成临时双插件隔离验收包；同步阶段评审、计划、项目说明、验证记录，并更新桥接回归脚本使用错误页恢复链接。
- **原因/影响：**使已验证的静态包可离线运行，按来源隔离不同插件页面存储；移除确认会删除安装资源并保留该来源存储，数据删除/迁移由阶段 1.4 明确。错误页恢复不依赖 WebView 历史中可能重复的失败页面。
- **验证：**在 `android-app/` 执行 `.\gradlew.bat :app:assembleDebug` 通过并安装到 Android 37 / WebView 145 模拟器；两个临时合法插件包验证独立 HTTPS 来源、localStorage 隔离及重启保持，外部网络/file/content 请求与跨插件导航被拒绝，缺失资源错误页可恢复，确认移除后安装目录不可访问；`python -m py_compile scripts/build_stage13_fixtures.py` 和 `python scripts/build_stage13_fixtures.py --out-dir "$env:TEMP\stage13-generated"` 通过，生成 ZIP 清单和资源 SHA-256 自检匹配；`node scripts/verify-bridge.mjs browser` 与 `ANDROID_SERIAL=emulator-5554 node scripts/verify-bridge.mjs android` 均通过，`git diff --check` 通过。`.\gradlew.bat :app:lintDebug --offline` 分析完成但退出 1，保留既有 `targetSdk = 28` 的 1 项错误和 5 项警告；未进行真机测试。

- **变更：**在 `AGENTS.md` 的验证规范中补充 Android 测试顺序要求：先在虚拟机上测试，完成后如需真机测试须先告知开发者。
- **原因/影响：**统一开发验证流程，确保真机测试安排可被开发者及时知悉。
- **验证：**人工检查 Markdown 文档内容；未运行 Android 构建，因本次仅修改项目规范和变更记录。

## 2026-11-13

- **变更：**补录阶段 1.2 真机验收结果，并同步 `计划文档.md` 阶段状态。
- **原因/影响：**在 vivo V2458A（Android 16/API 36，WebView 156.0.8078.47）验证合法包安装及列表显示、哈希篡改和路径穿越包拒绝且旧插件不变、强制停止后列表恢复；明确更新替换回滚和更广设备兼容仍未覆盖。
- **验证：**在 `android-app/` 执行 `.\gradlew.bat :app:installDebug` 安装成功；系统文件选择器实际导入 `valid-minimal-v1.zip`、`invalid-hash-mismatch.zip`、`invalid-path-traversal.zip`，界面结果符合预期；`am force-stop` 后重新打开列表仍显示 `org.example.quick-notes 1.0.0`。本次只同步验证/计划文档，未重跑构建。

- **变更：**完成阶段 1.2 本地包管理实现，新增 Android 系统 ZIP 选取、限额与中央目录安全校验、严格清单/资源引用/SHA-256 验证、应用私有暂存及原子安装/更新、数据模型兼容检查、启动中断恢复和持久化插件列表；同步更新 `计划文档.md` 与 `android-app/验证记录.md`。
- **原因/影响：**让已冻结的静态插件包契约具备本地导入和管理入口；所有包在验证完成前不写入安装目录，更新需显式确认，失败时保留旧安装。设备端选包、安装与重启恢复尚未验证。
- **验证：**在 `android-app/` 执行 `.\gradlew.bat :app:assembleDebug` 退出 0；仓库根目录执行 `python plugin-protocol/fixtures/verify_fixtures.py --report plugin-protocol/fixtures/stage-1.1-report.json`，17 个固定夹具和 12 个临时边界夹具全部符合预期；`adb devices -l` 无设备，未执行 Android 运行验收；`git diff --check` 通过。

- **变更：**新增 `plugin-protocol/fixtures/verify_fixtures.py` 阶段 1.1 契约验收器，统一执行 17 个固定负例、2 个合法包及 12 个临时上下限 ZIP 夹具；生成 `stage-1.1-report.json`，并同步协议、计划和阶段评审中的完成状态。
- **原因/影响：**将已冻结的清单、跨字段、资源闭合、错误码和 ZIP 限额矩阵转为可重复的运行证据；大体积边界包只在临时目录生成，避免把构建产物提交到仓库。Android 导入、原子安装和设备验收仍由阶段 1.2/1.5 负责。
- **验证：**`python plugin-protocol/fixtures/build_fixtures.py` 与 `python plugin-protocol/fixtures/verify_fixtures.py --report plugin-protocol/fixtures/stage-1.1-report.json` 通过，固定夹具 19 个、边界夹具 12 个全部符合预期；`python -m py_compile plugin-protocol/fixtures/build_fixtures.py plugin-protocol/fixtures/verify_fixtures.py`、`git diff --check` 及在 `android-app/` 执行的 `.\gradlew.bat :app:assembleDebug` 均通过（35 个任务为最新）。

- **变更：**冻结阶段 1.1 插件导入契约，补充严格 JSON、跨字段校验、错误码及阶段 0.5 实验码映射、未实现能力拒绝规则、版本升级/数据兼容策略；扩展 Schema 默认值类型校验；扩展夹具生成器，新增错误码索引和负例，合计 19 个 ZIP 夹具；同步阶段 0 评审与计划状态。
- **原因/影响：**关闭清单/跨字段规则、错误码映射和未实现能力处理的阶段 0 未决决策，为阶段 1.2 导入器提供明确基线。数据迁移、真实 Android 导入器、特殊 ZIP/全部限额边界执行、设备验收和其他阶段门未决项仍未完成。
- **验证：**夹具生成器语法检查通过；19 个 ZIP 均按预期检查（截断负例除外），17 个负例均有错误码索引，两个有效包资源摘要正确，重建字节一致；`python scripts/desktop_experiment.py all`、`node scripts/verify-bridge.mjs browser` 均通过；在 `android-app/` 执行 `:app:assembleDebug` 成功（35 项最新）；Schema 与错误码索引 JSON 解析及 `git diff --check` 通过。环境未安装 Draft 2020-12 Schema 校验器，未执行 Schema 元校验；尚无 Android 导入器可运行负例矩阵，当前无连接设备。

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
