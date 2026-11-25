# 阶段 C.1：扩充受限模板与规格样例

2026-11-25 完成。C.1 从 B.8 已确认的通讯录替代方案规格扩展一个可信模板，保留 Quick Notes 作为已有生成回归基线。模板范围只包含本机联系人列表、手动录入姓名及可选电话/邮箱、保存和返回列表。数据仅经宿主 `data.read`、`data.write` 能力读写，并分别由用户授权。

## 规格边界

`desktop-spec/examples/manual-contacts-v1.json` 描述联系人列表和手动编辑页、联系人字段、保存流程、两项数据能力及验收条件。`device.contacts` 明确记录为不支持项，并给出手动录入替代方案。规格 v1 的流程文字不具备任意执行语义，生成器只接受与该样例一致的结构，不会因文本提到系统联系人访问而扩展能力。

版本化实现位于 `desktop-generator/manual-contacts-v1/`，模板版本为 `manual-contacts-v1.0.0`。生成器对确认规格执行精确配置匹配，产出声明 `contacts` 数据集合和 `data.read` / `data.write` 的插件包。联系人姓名必填，电话和邮箱可选；字段长度分别为 120、80 和 254。页面通过 `textContent` 呈现记录，并采用本地资源与限制性 CSP，不访问网络或读取系统通讯录。

超出字段、页面、工作流或授权范围的规格返回 `GENERATOR_UNSUPPORTED_SPEC`。固定协议夹具 `valid-manual-contacts-v1.zip` 可通过校验；`invalid-manual-contacts-unknown-capability-v1.zip` 因添加 `device.contacts` 能力而以 `CAPABILITY_UNKNOWN` 拒绝。协议总夹具检查也纳入这两个包。

## 回归与模拟器验收

桌面回归及可选设备验收入口：

```powershell
python scripts/verify-stage-c1.py
python scripts/verify-stage-c1.py --android --serial emulator-5554
```

2026-11-25，Windows、Python 3.15、Gradle Wrapper 8.9、Temurin 17、`Medium_Phone_API_37.0`（`emulator-5554`，Android API 37，WebView 145.0.7632.218）。4 项 C.1 桌面回归通过，覆盖 Quick Notes 兼容、联系人模板结构/字段/清单、确定性打包、越界规格和未知能力协议拒绝。Android 模拟器通过系统选包安装可信包，验证未授权读取被拒、写入和读取分别授权、联系人姓名/电话/邮箱显示，以及宿主进程重启后授权重置而数据仍可读取。脚本结束时移除测试插件并删除对应测试数据。

`.\gradlew.bat :app:assembleDebug` 构建成功，35 项任务均为 `UP-TO-DATE`。Android 测试仅覆盖 API 37 / WebView 145 模拟器；真机、旧 Android 与旧 WebView 未覆盖。该阶段未新增系统联系人权限，也未执行不可信生成代码。
