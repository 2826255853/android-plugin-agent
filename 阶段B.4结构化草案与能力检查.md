# 阶段 B.4 结构化草案与能力检查

Android 发送需求并收到「电脑已接收」后，可以点「刷新规格会话」。电脑发布澄清问题时显示「规格会话等待补充」。通过检查的草案显示「规格等待审阅」，并列出不支持的请求及其原因和替代方案。

## 电脑端检查

- 请求使用 `spec-draft-v3`，包含当前需求、项目 ID、下一规格版本和阶段 2.1 的宿主能力清单。修订请求还包含上一轮草案及绑定问题事件和规格摘要的用户输入。模型提示使用从 `desktop-spec/spec.schema.json` 生成的草案结构，所有业务字段都要存在。待决定的内容可用空目标、空数组或 `storesData: null` 表示。
- 模型输出上限为 12 KiB UTF-8 和 1024 个输出 token。电脑严格解析 JSON，并用自身的项目、版本、Schema 版本和 `draft` 状态覆盖模型提供的同名字段。下一版本按当前项目已确认版本计算。
- 电脑执行规格 Schema 和阶段 2.1 规则。缺少业务决定时先发布 `desktop.draft`，再发布绑定同一版本和 SHA-256 的 `desktop.question`。未知权限必须有同名不支持项、原因和替代方案。缺少该说明时同样进入等待补充。
- 通过检查后，`desktop.review` 包含完整的草案 JSON、检查摘要和有效期。不支持项用于审阅，宿主权限仍以能力白名单与 Android 授权为准。结构错误和字节超限分别发布 `MODEL_INVALID_OUTPUT`、`MODEL_OUTPUT_TOO_LARGE`，会话进入失败状态。
- 调用结算、草案事件、游标和会话状态用同一 SQLite 事务保存。旧版 B.3 留在处理中的候选在重启时重新检查一次，沿用原调用费用。Android 的回答往返见 [阶段 B.5](阶段B.5手机澄清与草案修订.md)，已确认版本保存见 [阶段 B.6](阶段B.6手机审阅与规格确认.md)。

## 复现

```powershell
python scripts/verify-stage-b4.py
python scripts/verify-stage-b3.py --android --serial emulator-5554
```

接收服务沿用确定性模拟提供方。发送 `Save short notes locally with a list and editor. Keep notes after restart.` 可取得 Quick Notes 草案。在同一条需求末尾加上 ` Allow selecting a system contact.` 可审阅通讯录请求的原因和替代方案。其他需求得到待补充的模拟草案。桌面固定夹具另外验证格式、限长、阶段 2.1 结果、摘要关联、遗留候选和确认边界。[Android 验证记录](android-app/验证记录.md)
