# 阶段 C.3 模型补丁协议与校验

## 协议与校验

模型开发响应使用 `plugin-development-v1`，Schema 位于 `desktop-spec/schemas/plugin-development-response-v1.schema.json`。每个补丁包含简要计划、规格条目类型与 ID、模板相对路径、原文件 SHA-256、完整替换文本和预期检查项。响应限制为 12 KiB，最多 16 个文件补丁；单文件仍遵守任务运行时 256 KiB 限额。

`scripts/generation_response.py` 使用阶段 B 的严格 JSON 对象解析，拒绝重复键、错误 JSON 和过长输出。协调器只允许当前受限模板中的 HTML、JavaScript 和 CSS 文件，不允许改写清单或生成任意路径。规格引用必须能在 confirmed 快照中找到；检查项必须来自生成协调器的静态检查白名单；补丁中的宿主能力标识必须属于规格已确认权限。写入前重新读取并核对所有基线摘要。

`TaskStore.apply_patch_batch` 在一轮任务工作区中先准备全部临时文件，再提交整组替换，并在同一任务事务中记录补丁前后摘要及修订事件。中途写入或记录失败时恢复此前文件，未提交的补丁不进入历史，也不能进入后续验证。各补丁的 `expectedChecks` 只声明需要运行的既有检查，不代表 C.3 已执行构建或页面交互检查。

## 验收

在仓库根目录运行：

```powershell
python scripts/verify-stage-c3.py
python scripts/verify-stage-c2.py
python scripts/verify-stage23.py
python -m py_compile scripts/generation_response.py scripts/task_runtime.py scripts/verify-stage-c3.py
```

固定响应回归覆盖阶段 B `ModelBoundary` 往返和错误分类、合法响应及前后摘要、重复键/无效 JSON/超长输出、未知检查、未知规格引用、清单越权、能力扩展、基线摘要过期和多文件提交中途失败。验证使用固定提供方和临时任务仓储，不访问真实模型、不运行生成代码、不执行构建或浏览器预览。

## 阶段边界

C.3 定义并校验模型补丁协议，支持将已校验补丁原子写入本轮任务工作区。当前没有将该入口接入真实或模拟提供方调用，也没有触发生成任务自动领取。操作系统隔离、补丁后的构建/测试和预览仍由 C.4–C.5 实施。固定响应只证明协议校验和受限文件写入行为。
