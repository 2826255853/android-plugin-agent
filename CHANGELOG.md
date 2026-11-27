# 变更记录

## 2026-11-27

- **变更** 完成阶段 C.7。构建报告新增模型/提示版本、源码修订及结算账本快照；`plugin_delivery.py` 增加按包 SHA-256 反查与从不可变任务快照原字节导出。新增 C.7 端到端回归和阶段说明；B.6 模拟器验收可选导出确认规格快照，供 C.7 绑定并构建。同步后续规划、项目介绍和 Android 验证记录。
- **原因与影响** 将 Android 已确认规格、C.2 任务、C.3 修订、C.4/C.5 检查、C.6 模型调用结算和成功 ZIP 摘要关联成可查询记录。成功包仍为 `built / deliverable: false`，不得解释为已回传或安装。
- **验证** Android API 37 / WebView 145 模拟器 B.6 规格确认通过，确认快照交由 C.7 绑定并通过固定任务/产物回归；C.7 Quick Notes、手动联系人簿与修复产物的 C.4/QEMU/Chromium 验收通过。C.2（4 项）、C.3（6 项）、C.5 固定检查（3 项通过，C.7 复验两个模板隔离路径）、C.6（9 项）及 C.7 固定回归通过。Temurin 17 下 `:app:assembleDebug`、Python 编译及 `git diff --check` 通过。未调用真实模型，未在 Android 导入生成包，未进行真机验收。

- **变更** 完成阶段 C.6。新增自动修复协调器和 `plugin-development-v1` 模型提示，失败报告及当前受限源码快照经 C.3 校验后提交修订并重新运行 C.5。任务账本新增项目级预算、模型请求 ID、结算/不确定状态查询及未结算预留恢复。更新接收服务预算配置、阶段规划和项目介绍，新增 C.6 阶段说明及固定回归。
- **原因与影响** 每次模型调用前在 SQLite 事务内核对任务与项目剩余预算并持久预留，响应按调用 ID 结算。重复错误、修复轮次耗尽、费用不足和补丁路径/能力越权会停止任务。超时、取消或进程中断导致结果不确定时保留费用预留并停止，不自动重发。成功修复仍须通过 C.5 全部检查才保存 `built / deliverable: false` 快照。
- **验证** `python scripts/verify-stage-c6.py` 的 9 项固定故障/恢复回归、C.1（4 项）、C.2（4 项）、C.3（6 项）、C.5（固定回归及两模板 C.4/QEMU 构建、测试和 Chromium 交互）、任务仓储（29 项）和模型边界（11 项）回归通过。Python 编译、`git diff --check` 和 `android-app/gradlew.bat :app:assembleDebug` 通过。仅运行桌面隔离验收，未运行 Android 模拟器或真机；未调用真实模型提供方。

- **变更** 完成阶段 C.5。生成入口保留 C.3 已提交修订，校验并刷新受信模板已声明资源摘要，执行确定性 ZIP 打包和回读；新增 C.4 隔离构建、隔离测试及 Chromium 表单交互检查。新增 C.5 固定回归和阶段说明，更新后续规划与项目介绍。
- **原因与影响** 任务成功包仅在静态校验、打包、离线构建/测试和浏览器交互全部通过后提交。报告分列检查阶段、源码/包摘要和生成代码执行状态。清单错误、隔离失败或交互失败不会覆盖先前成功任务快照，状态仍为 `built / deliverable: false`。
- **验证** `python scripts/verify-stage-c5.py` 的固定失败/快照回归通过；传入本机 C.4 QEMU 11.1.0 与 Alpine 3.23 镜像后，Quick Notes 和手动联系人簿的隔离构建、测试及 Chromium 保存/列表交互均通过。C.1（4 项）、C.2（4 项）、C.3（6 项）、阶段 2.3（30 项）、3.1（15 项）、3.2、3.3（4 项）回归，Python 编译、`git diff --check` 和 `android-app/gradlew.bat :app:assembleDebug` 均通过。只做了桌面浏览器模拟，未运行 Android 模拟器或真机测试；浏览器模拟数据桥不代表 Android 宿主验收。

## 2026-11-26

- **变更** 完成阶段 C.4。新增 Windows/QEMU 一次性执行环境、可信 Alpine 工具链准备脚本和固定程序验收。为构建、测试和无头 Chromium 提供独立入口，同步执行隔离说明、后续规划、项目介绍及阶段 2.4 预览边界。镜像准备脚本在 Windows 检出时保留 LF 行尾。
- **原因与影响** 工作负载仅获得本轮只读输入和专用可写目录，默认只可使用客体 loopback。Windows Job 和客体 cgroup 限制 CPU、内存、磁盘、进程数和时长。超时或取消会终止整轮并清理，隔离初始化失败或镜像摘要不符时停止。Chromium 冷启动使用独立的 CPU 和墙钟限额。
- **验证** Windows 11 家庭版目标机通过 C.4 的 10 项固定程序验收和隔离 CLI 冒烟检查。C.1–C.3、阶段 2.2 与 2.3 的 73 项回归、Python 编译、Shell 语法检查、`gradlew.bat :app:assembleDebug` 和 `git diff --check` 通过。本阶段仅改动电脑端，未运行 Android 模拟器或真机测试。

- **变更：**完成阶段 C.3。新增 `plugin-development-v1` 响应 Schema、复用阶段 B 模型边界校验固定响应，并完成规格/路径/检查项/能力/基线摘要校验；任务运行时支持多文件原子补丁修订并保留前后摘要和修订事件。新增 C.3 回归与阶段说明，同步 C.2 阶段边界、后续规划和项目介绍。
- **原因/影响：**拒绝无效 JSON、超长输出、未知规格引用或检查项、模板白名单外路径、能力扩展及过期文件摘要。多文件提交中断会恢复原文件，失败修订不进入补丁历史或后续验证。本阶段使用固定响应，不调用模型提供方，不执行代码或构建。
- **验证：**`python scripts/verify-stage-c3.py` 的 6 项固定响应/故障回归、C.2 的 4 项回归、C.1 的 4 项回归、任务工具的 29 项回归、阶段 2.3 的 30 项回归及 B.1–B.7 桌面回归通过；Python 编译、响应 Schema JSON 解析、`android-app/gradlew.bat :app:assembleDebug` 和 `git diff --check` 通过。未运行 Android 模拟器，因为 C.3 不改变 Android 行为。

- **变更：**完成阶段 C.2。确认协调器从持久化 confirmed 规格重新读取并校验后，按确认事件 ID 幂等创建 `ready` 任务；任务仓储保存项目/版本/摘要映射和规格快照，电脑接收服务提供任务库位置及任务费用上限配置。新增 C.2 回归和阶段说明，并同步后续规划与项目介绍。
- **原因/影响：**重复确认、并发登记及保存规格后发生中断时，均复用同一个任务；草案、摘要不符和项目版本冲突不会启动生成。任务留在 `ready`，未触发模型调用或代码执行。
- **验证：**`python scripts/verify-stage-c2.py` 的 4 项绑定/恢复回归、`python scripts/verify-stage-b6.py` 的 6 项确认回归、`python scripts/verify-stage-c1.py` 的 4 项模板回归、`python scripts/verify-stage22.py` 的 29 项任务仓储回归、`python scripts/verify-stage-b8.py` 的 B.1–B.7 桌面回归、Python 编译、`android-app/gradlew.bat :app:assembleDebug` 和 `git diff --check` 均通过。B.8 Android 模拟器验收未运行，本阶段未改动 Android 行为。

## 2026-11-25

- **变更** 完成阶段 C.1。生成器新增版本化手动联系人簿模板和配套规格样例；新增合法/越界协议 ZIP 夹具、桌面回归及 Android 模拟器系统选包、数据授权和重启恢复验收。同步 C.1 阶段说明、后续规划、项目介绍及 Android 验证记录。
- **原因与影响** 为 B.8 通讯录替代方案提供可构建样例，联系人仅支持手动输入并使用宿主已有的本地数据读写授权。系统联系人访问保持不支持，越界规格和未知能力会被拒绝。
- **验证** `python scripts/verify-stage-c1.py` 的 4 项回归通过；`python scripts/verify-stage-c1.py --android --serial emulator-5554` 通过 Android API 37 / WebView 145 模拟器验收；19 项固定及 12 项边界协议夹具通过；阶段 2.3 的 30 项回归通过；`python scripts/verify-stage15.py --serial emulator-5554` 报告 68 项通过、0 项失败；`gradlew.bat :app:assembleDebug` 成功，35 项任务均为最新；Python 编译、模板及验收脚本 JS 语法检查通过。真机、旧 Android 和旧 WebView 未覆盖。

- **变更** 将后续端到端开发规划的阶段 C 拆分为 C.1–C.7，为每一步补充实施内容和退出条件。
- **原因与影响** 明确从已确认规格创建任务、校验模型补丁、验收执行隔离，到构建、修复及向阶段 D 提供可追溯 ZIP 的实施顺序。
- **验证** 已核对七个步骤的编号、退出条件及与阶段 B、D 的衔接，`git diff --check` 通过。本次仅修改规划文档和变更记录，未运行构建或设备测试。

- **变更** 完成阶段 B.8。新增一键桌面回归、两组 Android 往返及受限真实提供方核对脚本，复用配对和规格审阅验收工具。同步 B.8 验收说明、端到端规划、项目介绍、B.7 阶段说明及 Android 验证记录。
- **原因与影响** 验收从手机新建项目到电脑保存已确认规格的完整链路。核对手机、电脑和落盘规格的标识、版本及 SHA-256，确认阶段 B 的交付边界。
- **验证** Android API 37 模拟器两组需求与重启恢复、B.1–B.7 桌面回归及独立 Schema 检查、A.1–A.3 和阶段 2.1、3.3 桌面回归、66 项 Android 原生回归、Debug 构建、Lint、Python 编译及 git diff --check 通过。真实卡扣调用成功，提供方用量与账本一致，结算 0.0015834 美元。未覆盖真机、旧 Android、OpenAI 真实调用和真实模型多轮澄清。

- **变更** 完成阶段 B.7。接收服务在 SQLite 中按项目持久预留和结算模型费用，限制会话调用轮数。加入本机模型账本与用量核对命令、真实提供方显式配置及 Android 故障恢复提示。同步后续规划、双向对话契约、项目及阶段说明和 Android 验证记录。
- **原因与影响** 超时、断连或服务中断后的不确定调用保留整笔预留。重复回执及服务重启沿用原结果。核对用量只更新费用记录，继续处理由用户发起新需求。账本与日志只保存诊断所需元数据。
- **验证** B.7 六项桌面故障回归和 Android API 37 模拟器离线续发、跨重启预算停止通过。A.1 至 A.3、B.1 至 B.6 桌面回归、阶段 2.1 和 3.3 桌面回归、66 项 Android 原生回归、Debug 构建、Lint、Python 编译及 `git diff --check` 通过。真实提供方账单核对归入 B.8，真机和旧 Android 未覆盖。

- **变更** 完成阶段 B.6。Android 支持完整规格审阅、明确确认、拒绝和修订，保存结果显示版本和 SHA-256。电脑复核草案后原子保存规格，恢复中断的确认并按事件 ID 去重。同步端到端规划、对话契约、项目和阶段说明及 Android 验证记录。
- **原因与影响** 确认仅适用于用户实际审阅的事件、版本和摘要。过期或发生版本冲突时重新审阅。协议沿用 `cancelled` 作为拒绝终态，Android 持久保存已接受的操作类型以显示拒绝结果。
- **验证** B.6 的 6 项桌面回归和 Windows 与 Android API 37 模拟器验收通过。A.1、A.2/A.3、B.1（含独立 Schema 校验）、B.3 至 B.5 桌面回归、B.5 模拟器回归、阶段 2.1 与 3.3 桌面回归、66 项 Android 原生回归、Debug 构建、Lint、Python 编译及 `git diff --check` 通过。验收使用模拟模型，真机和旧 Android 未覆盖。

## 2026-11-24

- **变更** 完成阶段 B.5。Android 对话支持回答澄清、请求修订和取消，待提交回答持久化并幂等重试。电脑端按原始需求、上一轮草案及用户输入继续模型回合，逐轮重跑 B.4 校验，SQLite 调用记录迁移后保留历史。新增 B.5 桌面与模拟器验收及阶段说明，更新端到端规划、B.3/B.4 说明、项目介绍和 Android 验证记录。
- **原因与影响** 用户回答绑定当前问题/审阅事件及规格版本和 SHA-256。旧、过期或被拒绝的上下文不会继续推进模型，取消会停止当前调用或后续排队调用。回答不会自动确认规格。提示版本升级至 `spec-draft-v3`。
- **验证** `python scripts/verify-stage-b5.py` 的 4 项回归、B.1/B.2/B.3/B.4 回归及 Android API 37 模拟器两轮澄清和审阅修订通过。`:app:assembleDebug`、`:app:lint`、`python scripts/verify-stage15.py --serial emulator-5554`（65 项通过）及验收 APK 构建通过。真机、旧 Android 和真实模型多轮联调未覆盖。

- **变更** 将 Android 首页改为「插件工坊」对话界面，加入原创紫色插画、浅色与深色主题、灵感预填、会话历史、消息气泡和可展开的回执详情。更新应用图标和插件库，桥接实验移至插件库。同步项目说明、阶段操作文档和模拟器验收脚本。
- **原因与影响** 用户可从首页直接发起会话，首条需求和项目在同一次本地事务中保存。送达状态与电脑发布的规格回复分开展示，原有项目、待发队列和插件授权流程继续可用。
- **验证** Debug 构建、Lint、脚本语法检查和 `git diff --check` 通过。Android API 37 模拟器验证两种主题、输入、配对建项、规格回复、离线历史、插件导入与回退。A.5 恢复验收、桥接、插件数据、65 项原生回归和 B.4 的 6 项桌面回归通过。结果记录在 `android-app/验证记录.md`。真机和真实模型未覆盖。

- **变更** 完成阶段 B.4。为模型请求加入项目版本、宿主能力清单和版本化结构提示。电脑端严格校验草案，发布澄清、审阅或失败事件。Android 显示不支持项的原因和替代方案。同步后续规划、项目介绍、B.3/B.4 阶段说明和 Android 验证记录。
- **原因与影响** 电脑端决定项目、草案版本和状态，使用阶段 2.1 的规则决定是否进入审阅。草案事件与模型结算一并保存。B.3 的遗留候选重启后复核，保留原费用和调用记录。
- **验证** B.4 的 6 项回归、阶段 2.1 与 3.3 桌面回归、A.1、A.2/A.3 及 B.1–B.3 回归通过。Android API 37 模拟器完成澄清、重启和不支持能力审阅，另有 65 项 Android 原生回归通过。Debug 构建、Lint、Python 编译与 `git diff --check` 通过。本次使用模拟提供方，未进行真实模型或真机联调。

## 2026-11-23

- **变更** 完成阶段 B.3。加入 SQLite 对话协调器、模拟模型调用记录、认证事件拉取与确认、取消提交，以及 Android 会话游标和独立的规格状态。同步端到端规划、项目说明、收件箱清理说明和 Android 验证记录。
- **原因与影响** 相同设备与消息 ID 只创建一个规格回合，项目内后续回合排队。中断的调用记录保留费用并避免重放。Android 在保存事件后确认处理水位，模型候选等待 B.4 校验和发布。
- **验证** B.3 桌面与 Android API 37 模拟器验收通过，覆盖重启后分页补取、确认水位和离线重开。A.1、A.2/A.3、B.1 协议夹具、B.2 11 项模型回归、65 项 Android 原生回归、Debug 构建、Lint、Python 编译和 `git diff --check` 通过。B.1 可选的独立 Schema 检查缺少 `jsonschema`。真机和真实提供方联调未覆盖。
- **兼容回归** A.5 模拟器复跑通过离线排队、响应丢失、服务和应用重启及原 ID 重试。最后一次复跑在等待错误证书提示时超时。电脑端证书校验与 B.3 的配对撤销回归通过。

## 2026-11-22

- **变更** 完成 B.1 和 B.2。新增双向对话 v1 契约、Schema、共享夹具和 Python/Kotlin 校验，以及受预算约束的模拟、OpenAI 和卡扣模型适配器、Windows 凭据和调用测试入口。同步端到端规划、项目介绍、阶段说明及 Android 验证记录。
- **原因与影响** 规格确认绑定已审阅的事件、版本和摘要。真实模型只提供候选 JSON，电脑端在调用前预留项目费用，并在响应后按用量结算。
- **验证** B.1 双端 56 项协议场景、A.1、A.2/A.3、Android API 37 模拟器 64 项原生回归、Debug 构建和 Lint、B.2 11 项自动回归通过。卡扣在 0.05 美元项目预算下成功往返，129 个输入及 204 个输出 token 对应本地费用上界 0.00435 美元。共享账单未逐请求归因，OpenAI 官方直连超时，真机及 B.3 网络联调未覆盖。

- **变更：**细化《后续端到端开发规划》的阶段 B，拆分为双向对话契约、模型适配与密钥配置、持久化编排、规格草案与能力检查、手机澄清、规格审阅确认、预算与恢复、端到端验收八步，并补充阶段边界和 confirmed 会话状态。
- **原因与影响：**将阶段 B 从功能概述展开为有顺序、产物和退出条件的实施工作包，明确新增电脑到 Android 的事件通道、规格摘要绑定确认，以及本阶段不触发插件生成或交付。
- **验证：**对照 A 阶段消息契约、阶段 2.1 规格流程、阶段 2.2 任务边界和当前 Android 会话实现核对规划；`git diff --check` 通过。仅修改规划和变更记录，未运行 Android 构建、自动化测试或设备验证，也未调用真实模型。

- **变更** 完成阶段 A.5。Android 区分离线排队、超时、连接中断和服务不可达，可核验同一台电脑的新地址并恢复待发队列。连接检查识别撤销并暂停发送，配对码输入框显式启用密码遮罩，会话自动定位到新消息。扩展电脑端回归，新增隔离的模拟器验收入口，并同步后续规划、项目说明和 Android 验证记录。
- **原因与影响** 恢复和重新配对均沿用待发消息原 ID。电脑已提交的消息在重试时返回原回执，Android 已确认的消息不会重新投递。
- **验证** Windows 与 Android API 37 模拟器端到端验收通过，8 条消息均逐条核对且无重复入库。A.1 协议、A.2/A.3 电脑端、63 项 Android 原生回归、Android 桥接、Debug 构建、Lint、Python 编译和 `git diff --check` 通过。真机、旧 Android 和跨物理设备局域网切换未覆盖。

## 2026-11-21

- **变更** 完成阶段 A.4。Android 宿主新增「开发会话」、电脑配对、项目和消息列表、文本发送及持久发送队列。加入独立的 TLS 1.3 证书指纹校验和 Android Keystore 配对存储。同步阶段说明、后续规划、项目介绍、消息契约及 Android 验证记录。
- **原因与影响** 需求可以从 Android 发到指定电脑的收件箱，应用重启后可沿用原消息 ID 重试。Android 在核验电脑回执后显示已接收，插件 WebView 继续使用本地资源。
- **验证** Android 模拟器通过错误指纹拒绝、配对批准、创建会话、原始正文与持久回执核对，以及电脑停服、应用重启后的同 ID 重试。Android 原生回归 63 项通过，A.1 协议、A.2/A.3 电脑端回归、Android 桥接及 3.3 模拟器回归通过。Android Debug 构建、Lint 和 `git diff --check` 通过。真机、旧 Android 和 A.5 完整恢复矩阵未覆盖。

## 2026-11-21

- **变更** 完成阶段 A.3。电脑接收服务用 SQLite 同一事务保存消息和回执，加入设备内消息 ID 去重、序号冲突检查、本机清理命令和接收状态日志。同步项目介绍、消息契约、A.2 操作说明和后续规划。
- **原因与影响** 已配对设备收到的 `received` 回执对应已提交的消息。服务中断后可查询已保存的状态，也可在重试窗口内取回原回执。桌面控制台通过 `inbox clear CONFIRM` 清空收件箱。
- **验证** A.2/A.3 回归在回环和本机以太网接口均通过，覆盖并发重发、内容与序号冲突、损坏及超限请求、提交前后进程中断、重启、设备隔离、日志和清理。A.1 协议回归通过 10 个合法、6 个非法及 4 个边界夹具。Python 编译、`git diff --check` 和 Android `.\gradlew.bat :app:assembleDebug` 通过。本阶段使用同机 HTTPS 客户端，Android 模拟器联调归 A.4，真机测试为可选补充。

## 2026-11-20

- **变更：**完成阶段 A.2。新增 Windows TLS 1.3 接收服务和本地管理控制台，支持显式绑定、查询、停止、一次性配对码、设备确认和单设备撤销。加入临时收件箱和认证状态查询、DPAPI 设备注册表和证书存储、连接/请求限时及配对限速。加固 Python 协议解析对异常 JSON、字段类型和非法 Unicode 的拒绝；同步接收服务操作文档、协议、项目介绍和后续规划。
- **原因/影响：**已批准设备可通过固定证书指纹和配对令牌提交消息、查询状态，停服后监听关闭，配对与撤销记录可跨重启恢复。当前消息保存在内存中并返回 `queued`，持久收件箱和 Android 客户端分别归入 A.3 和 A.4。
- **验证：**`python scripts/verify-stage-a2.py` 在回环和本机以太网接口均通过，覆盖配对拒绝和过期、有效配对、单设备撤销、同端口重启、日志无凭据或正文和畸形及超限请求。A.1 协议回归通过 10 个合法、6 个非法及 4 个边界夹具；Python 编译、`git diff --check` 和 `android-app/` 下的 `.\gradlew.bat :app:assembleDebug` 通过。接收端回归使用同机 TLS 客户端，未做 Android 模拟器或真机网络联调。

## 2026-11-19

- **变更：**完成阶段 A.1 的 Android instrumentation 设备验证；启动 `Medium_Phone_API_37.0` 模拟器并通过 `Stage15Instrumentation` 执行原生回归，确认新增消息协议夹具用例接入现有测试入口。
- **原因/影响：**补充 Android 端实际运行证据，确认 Kotlin 解析器能在目标模拟器中读取仓库共用夹具并执行合法/非法消息契约校验。
- **验证：**`python scripts/verify-stage15.py --serial emulator-5554` 通过，输出 `STAGE15 PASSED: 61 passed, 0 failed`；环境和覆盖边界已记录到 `android-app/验证记录.md`。未进行真机、API 23、旧 WebView或网络接收服务验证。

- **变更：**完成后续端到端规划阶段 A.1；新增 `message-protocol/` 下的消息与配对契约、Draft 2020-12 Schema、合法/非法夹具和稳定错误码；新增 Python 严格解析器与回归入口，Android 新增 `MessageProtocol.kt` 并让 instrumentation 读取同一组夹具。
- **原因/影响：**固化配对申请/确认/撤销、消息发送、接收回执、状态查询和错误响应的版本化结构，明确 TLS 1.3、证书指纹、凭据生命周期、24 小时重试窗口、幂等规则、限长和未知字段处理。A.1 不启动接收服务、任务、LLM 或插件生成。
- **验证：**`python scripts/verify-message-protocol.py` 通过（10 个合法、6 个非法和 4 个边界用例）；`python -m py_compile scripts/message_protocol.py scripts/verify-message-protocol.py` 通过；在 `android-app/` 执行 `./gradlew.bat :app:assembleDebug` 和 `./gradlew.bat :app:assembleAndroidTest` 均成功。当前未运行模拟器 instrumentation，`adb devices` 未发现在线设备；未进行真机测试。

- **变更：**细化 `后续端到端开发规划.md` 的阶段 A，拆为契约与配对、安全接收服务、持久收件箱、Android 会话与发送队列、恢复与端到端验收五步，并明确每步退出条件及阶段边界。
- **原因/影响：**为手机到电脑的本机消息链提供可按序实施的工作包和验收要求，明确阶段 A 不启动 LLM 或插件生成，并将阶段 B 的入口设为阶段 A 消息链验收完成。
- **验证：**对照 Android 宿主和现有桌面规格/任务/交付实现检查规划边界；`git diff --check` 通过。仅修改规划文档和变更记录，未运行 Android 构建、自动化测试或设备验证。

## 2026-11-19

- **变更：**新增 `后续端到端开发规划.md`，按当前 Android 宿主、电脑端生成工具和阶段 3.4 模拟器证据，规划手机对话/电脑配对、LLM 规格澄清与受控生成、插件构建验证、候选包回传、Android 导入及后续迭代。
- **原因/影响：**明确尚未实现的手机到电脑指令通道、真实 LLM、通用受控生成和自动回传，并给出分阶段目标、状态契约、安全边界及验收条件，作为后续功能开发路线。
- **验证：**人工对照 `项目介绍.md`、`计划文档.md`、`插件协议草案.md`、阶段 3.1 与阶段 3.4 文档核实当前能力和未实现范围；`git diff --check` 通过。仅新增规划文档，未运行 Android 构建、设备测试或模型调用。

- **变更：**修复 `scripts/launch-android-emulator.ps1` 复用无窗口 Android 模拟器的问题；遇到以 `-no-window` 或 `-qt-hide-window` 启动的在线 AVD 时，先正常关闭并等待退出，再启动可见的图形模拟器。更新 `android-app/验证记录.md` 记录定位和模拟器验证结果。
- **原因/影响：**ADB 会将无窗口模拟器报告为在线设备，导致原入口误以为模拟器界面已打开。修复后双击测试入口会显示 Android 手机界面，已经可见的模拟器仍可直接复用。
- **验证：**PowerShell AST 解析通过；两次运行 `启动模拟器测试.bat`，首次从无窗口实例切换到可见模拟器，`:app:assembleDebug` 成功（35 项最新）、APK 安装成功且 `com.example.pluginhost/.MainActivity` 在前台，桌面截图确认测试页面可见；第二次成功复用可见模拟器；`git diff --check` 通过。未进行真机测试。

## 2026-11-19

- **变更：**新增根目录 `启动模拟器测试.bat` 和 `scripts/launch-android-emulator.ps1`，可自动发现 Android SDK/AVD、复用或启动模拟器、构建 Debug APK、安装并打开宿主应用；支持通过 `-AvdName` 指定 AVD。更新 `android-app/验证记录.md` 记录模拟器复用路径验收。
- **原因/影响：**提供单入口的 Android 模拟器测试启动流程，减少手动执行构建、安装和启动命令。
- **验证：**PowerShell AST 解析通过；运行 `启动模拟器测试.bat` 后，` :app:assembleDebug` 成功（35 项最新），APK 在 Android API 37 / WebView 145 模拟器安装成功，`com.example.pluginhost/.MainActivity` 启动并处于前台；`git diff --check` 通过。此轮复用已运行的 `emulator-5554`，未触发模拟器冷启动分支。

## 2026-11-19

- **变更：**完成阶段 3.4 Android 模拟器端到端验收；新增 `阶段3.4端到端验收.md`，同步 `计划文档.md`、`项目介绍.md` 和 `android-app/验证记录.md`。
- **原因/影响：**以同一虚构 Quick Notes 项目贯通需求规格确认、1.0.0 首版生成/手动交付导入、两轮修改到 2.0.0/3.0.0、Android 实际运行、拒绝不兼容更新、显式回退及重启后的数据保持。阶段 3.4 在 Android API 37 / WebView 145 模拟器基线上通过；不表示真机、API 23、旧 WebView、自动送达或正式发布资格已验证。
- **验证：**`python scripts/verify-stage33.py --android --serial emulator-5554` 四项桌面检查及 Android 1.0.0 安装运行、2.0.0/3.0.0 两轮更新、4.0.0 不兼容更新拒绝、取消/确认回退和重启数据核对全部通过；`python scripts/verify-stage15.py --serial emulator-5554` 通过 60 项；`python scripts/verify-stage25.py`、`python scripts/verify-stage32.py` 通过；在 `android-app/` 执行 ` .\gradlew.bat :app:assembleDebug` 成功（35 项最新）。模拟器证据已写入 `android-app/验证记录.md`；`git diff --check` 在文档更新后通过。

- **变更：**完成阶段 3.3 修改与回退。规格工具支持从任一已确认版本派生草稿、追问、双基线差异展示与摘要确认；规格 Schema、生成器和项目历史支持修订来源。Android 宿主新增上一版本回退、完整性复核和切换恢复；同步协议、计划、阶段说明和 `android-app/验证记录.md`。
- **原因/影响：**同一插件可基于旧规格连续迭代。宿主在数据结构一致时可恢复紧邻的上一安装版本，保留当前全部笔记和页面来源存储。失败构建保留可用构建，失败导入保留当前安装及回退副本。
- **验证：**`python scripts/verify-stage33.py --android --serial emulator-5554` 通过 4 项桌面检查及 Android API 37 / WebView 145 的两轮更新、失败导入、取消和确认回退、资源核对和数据重启保持；`python scripts/verify-stage15.py --serial emulator-5554` 通过 60 项。阶段 2.5、阶段 3.1 桌面检查、阶段 3.2、协议夹具、命令行两轮生成和 Python/Node 语法检查通过；`.\gradlew.bat :app:assembleDebug` 和 `:app:lintDebug --offline` 成功（0 项错误、5 项既有警告）。`git diff --check` 通过。阶段 3.3 的设备验收采用模拟器，真机、最低 API、旧 WebView 和阶段 3.4 未验收。

## 2026-11-19

- **变更：**完成阶段 3.2 项目与版本历史查询：新增 `scripts/project_history.py` 聚合确认规格、关联任务、构建测试结果和最近可用构建摘要；新增 `scripts/verify-stage32.py` 和 `阶段3.2项目与版本.md`，同步阶段计划及项目介绍。
- **原因/影响：**用户可按项目和规格版本追踪生成状态与包结果，较新失败版本不会覆盖历史中的可用构建；Android 当前安装版本仍由宿主列表提供，电脑端明确标记为未同步，所有历史查询保持 `deliverable: false`。
- **验证：**`python scripts/verify-stage32.py` 通过，覆盖两版规格/任务/构建关联、测试结果、较新失败版本不替代旧版成功构建及设备安装状态未同步；`python scripts/verify-stage25.py` 的阶段 2.1（5 项）、2.2（29 项）、2.3（30 项）、包追溯和 Edge 预览回归全部通过；Python 编译和 `git diff --check` 通过；在 `android-app/` 执行 `.\gradlew.bat :app:assembleDebug` 成功（35 项任务均为最新，保留既有 SDK XML 版本提示）。本阶段不修改 Android 源码，未执行模拟器或真机验收；设备当前版本继续由 Android 宿主列表显示并人工核对，未宣称电脑端已同步。

## 2026-11-18

- **变更：**完成阶段 3.1：新增 `scripts/plugin_delivery.py`，从 SQLite 成功构建快照导出 ZIP/核对单，复核任务、规格、模板、固定检查、身份/版本与源码/包摘要；提供复制后复核及显式设备选择、默认拒绝真机、设备端 SHA-256 校验的 ADB 传输。新增 `scripts/verify-stage31.py` 的 15 项交付检查和 `scripts/verify-stage31.mjs` 的实际生成包模拟器选包验收；新增 `阶段3.1交付与导入.md`，同步计划、项目说明及 `android-app/验证记录.md`。
- **原因/影响：**补齐电脑到 Android 的可操作本地开发导入流程，拒绝失败任务、异包/篡改、错误目标和摘要不一致，保留现有文件及已安装插件；导出/传输不自动安装、授权或授予发布资格，任务仍为 `built / deliverable: false`。不新增依赖或 Android 权限，不更改协议和宿主实现。
- **验证：**`python scripts/verify-stage31.py --android --serial emulator-5554` 最终通过 15 项桌面检查及 Android API 37 / WebView 145 的真实生成、导出、传输/设备摘要、系统选包安装、安装后 ID/版本/全部源码摘要、默认拒绝/分项授权、笔记读写/重启保持、损坏包拒绝及清理；初轮选择器/列表脚本超时的原因与修正记录在 Android 验证记录。`python scripts/verify-stage25.py` 的 5/29/30 项回归、产物追溯和 Edge 预览通过；在 `android-app/` 执行 `.\gradlew.bat :app:assembleDebug` 成功（35 项最新，既有 SDK XML 提示）；Python 编译、Node 语法及 `git diff --check` 通过。未重跑 Lint（无 Android 源码/构建变更），未做可选真机、最低 API/旧 WebView、USB/MTP、真实模型或阶段 3.2–3.4 验收。

## 2026-11-17

- **变更：**完成阶段 2.5：新增 `scripts/verify-stage25.py` 串行运行阶段 2.1–2.4 回归，并对临时任务执行规格版本/摘要、模板版本、构建快照及 ZIP 摘要的持久化追溯检查；新增 `阶段2.5阶段回归.md`，同步 `计划文档.md` 与 `项目介绍.md`。
- **原因/影响：**为 AT-01/02/03/08、构建失败/重复修复/超时/费用/包校验和任务产物关联提供单一可重复验收入口；临时数据自动清理，构建仍保持 `deliverable: false`。
- **验证：**`python scripts/verify-stage25.py` 在 Windows / Python 3.15 / Node.js 24 / Microsoft Edge 通过阶段 2.1（5 项）、2.2（29 项）、2.3（30 项）、2.5 追溯断言及阶段 2.4 浏览器验收；`python -m py_compile scripts/verify-stage25.py` 与 `git diff --check` 通过。未运行 Android 构建或模拟器/真机测试，因为本次仅修改桌面回归脚本和 Markdown 文档，未改 Android 源码或构建配置。
- **变更：**完成阶段 2.4：新增 `scripts/browser_preview.py`，只接受阶段 2.3 严格校验的 ZIP、仅绑定 loopback 提供清单资源，并在内存注入浏览器模拟桥；插件页通过 localhost 别名源与 127.0.0.1 控制页隔离；新增 `desktop-generator/browser-preview/` 控制条、适配器和样式，以及 `scripts/verify-stage24.mjs` Edge 端到端验收。Quick Notes 在读取权限拒绝/撤销时清空已显示列表。新增 `阶段2.4浏览器预览.md`，同步阶段计划、项目说明及阶段 2.3 的后续状态。
- **原因/影响：**提供实际生成包页面和基本交互预览，数据读写各自默认拒绝并由用户显式模拟授权；浏览器数据隔离在当前源 localStorage，设备能力不误判通过，成功构建与预览仍无交付资格。当前预览仅适用于固定 Quick Notes 模板，不作为任意不可信代码的隔离环境。
- **验证：**`node scripts/verify-stage24.mjs <built-package.zip>` 在 Windows / Node.js 24 / Microsoft Edge 通过预览横幅、插件无法访问控制页、默认拒绝、分项授权、笔记保存与读取、撤销清空和包外资源拒绝；`python scripts/verify-stage21.py`（5 项）、`python scripts/verify-stage22.py`（29 项）、`python scripts/verify-stage23.py`（30 项）通过；Python/Node 语法检查、`git diff --check` 通过；在 `android-app/` 使用 JDK 17 执行 `:app:assembleDebug` 成功（35 项任务均为最新）。未运行模拟器或真机交互测试，原因是本次交付为桌面浏览器预览；未调用真实模型或设备能力。

## 2026-11-16

- **变更：**完成阶段 2.3：新增 `scripts/plugin_generation.py`、`scripts/plugin_package.py`、Quick Notes 受限模板和 `scripts/verify-stage23.py`；实现确认规格到离线 Web 源码、严格清单/资源/ZIP 校验、确定性静态构建及固定静态检查。扩展任务仓储，原子启动并占用构建轮次，保存规格/源码/包摘要、分阶段失败报告、成功包快照及 `built` 终态，支持导出失败后恢复；继续执行取消/超时/限额/修复停止。新增 `阶段2.3生成与构建.md`，同步计划、项目介绍、2.2 边界及阶段 0 风险补充。
- **原因/影响：**满足固定样例重复生成可验证静态包、失败定位阶段的门槛；限定支持的规格并明确拒绝其他需求，修正 Android UTF-16 名称/默认值长度契约，按确认版本推进模型 minor 版本。纯数据构建不执行生成代码、不启动构建脚本/浏览器/模型、不增加依赖；成功仍为 `deliverable: false`，预览和设备交互验收留待后续阶段。
- **验证：**`python scripts/verify-stage23.py` 30 项、阶段 2.2 的 29 项与阶段 2.1 的 5 项回归全部通过；覆盖协议固定正负例、确定性、资源/CRC/引用/配额、阶段诊断、修复成功/重复停止、取消/超时报告、并发占用、中断恢复、UTF-16/转义及导出失败恢复。标准样例实建为 8,321 字节，包 SHA-256 为 `f6bb44bf6e771b1611ea294028906ecbafebb8795cf93c3d486e82a2c6a7abc5`；Python 编译、模板 `node --check` 及 `git diff --check` 通过。在 `android-app/` 配置会话 JDK 17/SDK 后执行 `.\gradlew.bat :app:assembleDebug` 成功（35 项任务均为最新，保留既有 SDK XML 提示）。本轮为桌面静态生成/构建，未运行模拟器/真机或浏览器交互、真实模型和不可信脚本，不宣称这些行为已验证。

- **变更：**完成阶段 2.2：新增 `scripts/task_runtime.py` 本地任务/事件/费用/文件修订 SQLite 仓储、确认规格快照和摘要绑定、受限文件/规格工具及固定检查、任务身份和路径/链接防护、状态/取消/中断恢复与费用/时长/轮次/资源停止控制；新增 `scripts/verify-stage22.py` 和 `阶段2.2任务与工具.md`，同步计划、项目介绍及阶段 0 风险复审，忽略本地 `desktop-tasks/` 运行数据。
- **原因/影响：**为后续生成与构建提供可追溯且受限的任务基础；越权和限额触发停止，保留诊断、修改前后修订和费用，所有结果均无正式发布资格。不新增第三方依赖；当前不执行生成代码、不连接模型，真实进程沙箱、模型费用上界和外部进程强制取消须在后续接入前验证。
- **验证：**`python scripts/verify-stage22.py` 29 项通过（含实际 Windows junction/硬链接、跨任务隔离、并发费用预留、超时与停止恢复）；`python scripts/verify-stage21.py` 5 项规格回归通过；Python 语法检查与 `git diff --check` 通过。在 `android-app/` 执行 `.\gradlew.bat :app:assembleDebug` 成功（35 项任务均为最新，保留既有 SDK XML 提示）。未执行 Android 设备测试，本次为桌面端工具和文档变更；费用验证为虚构账本，未调用真实模型。

## 2026-11-15

- **变更：**完成阶段 2.1 规格与能力基线：新增版本 1 规格 Schema、Quick Notes 草稿、确定性规格审阅/澄清与确认版本保存 CLI，以及 5 项标准库验收；新增 `阶段2.1规格流程.md` 并同步阶段计划和项目说明。
- **原因/影响：**将目标、页面、数据字段、操作流程、权限理由和验收条件固定为可校验结构；缺项生成明确追问，超出当前宿主白名单的能力必须附原因和替代方案；确认规格按项目连续版本原子保存且不覆盖旧版本。未接入模型、服务端、生成或预览，避免把规格确认误认为插件交付。
- **验证：**`python scripts/verify-stage21.py` 通过（5 项）；`python -m py_compile scripts/spec_workflow.py scripts/verify-stage21.py` 和 Schema/样例 JSON 解析通过；在 `android-app/` 执行 `.\gradlew.bat :app:assembleDebug` 成功（35 项任务均为最新）；`git diff --check` 通过。未执行 Android 设备测试，本次变更为桌面端规格工具与文档。

## 2026-11-15

- **变更：**完成阶段 1.5：新增无第三方测试依赖的 `Stage15Instrumentation` 和 `scripts/verify-stage15.py`，通过 Gradle `stage15Assets` 仅向测试 APK 注入临时包矩阵；修复 `LocalPluginStore` 的 CRC32 校验、Android 路径穿越错误分类、升级约束方向和模型版本/语义一致性判断。新增 `阶段1评审.md`，同步协议、计划、项目介绍、阶段 0 评审及 `android-app/验证记录.md`。
- **原因/影响：**补齐实际 Android 导入、升级和数据配额证据，拒绝 CRC 损坏及不兼容更新，放宽约束时保留旧数据；阶段 1 模拟器验收通过，可进入阶段 2。保留 API 23/旧 WebView 等兼容风险及可选真机补验边界；不增加应用依赖、不调整 SDK。
- **验证：**Android API 37 / WebView 145.0.7632.218 模拟器上，`python scripts/verify-stage15.py` 最终 57 项通过、0 项失败（首轮 9 项失败均已修复）；`node scripts/verify-plugin-data.mjs` 和指定该模拟器的 `node scripts/verify-bridge.mjs android` 通过，Browser Adapter 回归另通过。`android-app/` 下 `.\gradlew.bat :app:assembleDebug :app:lintDebug --offline` 成功，Lint 0 errors、5 项既有 warnings；测试 APK 构建、桌面 17 固定负例/12 边界、Python/Node 语法检查及 `git diff --check` 通过。未运行 API 23、旧 WebView 或本轮真机测试，原因及其他未覆盖项已记录。

- **变更：**更新 `AGENTS.md`、`项目介绍.md`、`计划文档.md`、`阶段0评审.md` 和 `桥接实验.md`，明确真机测试属于可选补充，不是开发、阶段验收或项目推进的普遍前置条件；同步调整 R-04 和设备验收描述，保留未覆盖设备范围的兼容性边界。
- **原因/影响：**避免将真机补验或设备资源不可用误作项目进度阻塞，同时继续准确区分模拟器与真机证据；具体验收明确依赖某类设备或权限时，仍须完成相应验证。
- **验证：**人工检查相关文档中的测试政策、阶段门和风险描述；`git diff --check` 通过。仅修改 Markdown 文档，未运行 Android 构建或设备测试。

- **变更：**阶段 1.5 增加 vivo V2458A / Android 16 真机桥接回归记录；`scripts/verify-plugin-data.mjs` 增加显式 `ANDROID_ALLOW_PHYSICAL=1` 真机运行开关、真机临时 ZIP 清理及厂商选择器导航适配尝试。
- **原因/影响：**补充真实 WebView/设备上的安全桥接证据，同时保留数据回归脚本默认拒绝真机的保护。真机桥接回归通过；vivo 选择器仍使数据/升级脚本无法进入导入阶段，阶段 1.5 / R-04 继续开放。
- **验证：**`.\gradlew.bat :app:assembleDebug` 通过；`ANDROID_SERIAL=10AFC52CMP00985 node scripts/verify-bridge.mjs android` 通过（`ALL ANDROID CHECKS PASSED`）；真机 `node scripts/verify-plugin-data.mjs` 未完成，等待 Downloads 导航超时；`node --check scripts/verify-plugin-data.mjs` 与 `git diff --check` 通过。

记录仓库中源代码、配置、依赖、资源、构建脚本和项目文档的每次变更。按时间倒序追加；每条记录说明改动、目的或影响，以及验证结果。详细规则见根目录 `AGENTS.md`。

- **变更：**`scripts/verify-plugin-data.mjs` 改为从脚本自身路径解析仓库根目录，固定定位夹具生成器和 Debug APK，并增加 `--preflight` 路径检查。
- **原因/影响：**从仓库根目录、`android-app/` 或其他工作目录启动时都使用同一组项目路径；预检可在连接设备前直接发现缺失的 APK 或夹具脚本。
- **验证：**从 `C:\Users\ASUS\AppData\Local\Temp` 执行 `node C:\Codes\android-plugin-agent\scripts\verify-plugin-data.mjs --preflight`，正确解析并找到仓库内两项文件；`node --check scripts/verify-plugin-data.mjs` 和 `git diff --check` 通过。

## 2026-11-14

- **变更：**完成阶段 1.5 可用模拟器回归：修复 `LocalPluginStore` 中断更新后旧安装无法按插件 ID 恢复的问题；扩展临时 Quick Notes 夹具和 `scripts/verify-plugin-data.mjs`，覆盖兼容升级、拒绝降级、不兼容数据模型拒绝及目录切换中断恢复；更新阶段计划、评审、项目说明和验证记录。
- **原因/影响：**升级过程中进程中断时保留并恢复原安装；验证兼容更新保留 SQLite 记录，且未实现字段迁移时拒绝不兼容模型，避免丢失或错误读取现有数据。阶段记录明确 API 23、真机、旧 WebView 和数值配额边界仍未覆盖，R-04 阶段门保持开放。
- **验证：**在 `android-app/` 执行 `.\gradlew.bat :app:assembleDebug :app:lintDebug --offline` 通过，Lint 0 errors、5 warnings；`node scripts/verify-plugin-data.mjs`、`ANDROID_SERIAL=emulator-5554 node scripts/verify-bridge.mjs android`、`node --check scripts/verify-plugin-data.mjs`、`python -m py_compile scripts/build_stage14_fixture.py`、`python plugin-protocol/fixtures/verify_fixtures.py`（17 固定、12 边界夹具）及 `git diff --check` 通过。Android API 37 / WebView 145 模拟器；未测试真机/API 23/旧 WebView，未逐项执行 Android 导入 ZIP 完整负例矩阵或存储/分页配额数值边界。

- **变更：**将 Android `targetSdk` 从 28 升至 35；`MainActivity` 启用边到边布局，并为列表/插件页设置安全绘制边距，Manifest 配置输入法 `adjustResize`。同步 `项目介绍.md`、`计划文档.md`、`阶段0评审.md` 和 `android-app/验证记录.md` 中的 SDK/Lint 状态，保留历史验证结果。
- **原因/影响：**修复 `ExpiredTargetSdkVersion` 错误，并适配升级后的系统栏/输入法布局；与现有 compileSdk 35 对齐，保持 minSdk 23 和现有工具链/依赖，不增加 Lint 抑制或基线。
- **验证：**在 `android-app/` 执行 `.\gradlew.bat :app:assembleDebug :app:lintDebug --offline` 成功，Lint 为 0 errors、5 warnings（4 项既有警告和升级后新增的 `DataExtractionRules` 提示）；`aapt dump badging` 确认 targetSdk 35、minSdk 23。Android API 37 / WebView 145 模拟器的 `node scripts/verify-plugin-data.mjs` 全部通过，覆盖 ZIP 导入、权限、读写、重启、隔离及移除保留/删除；`ANDROID_SERIAL=emulator-5554 node scripts/verify-bridge.mjs android` 和 `git diff --check` 通过。未进行真机、最低版本、旧 WebView 或备份/迁移验证，具体边界见验证记录。

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
