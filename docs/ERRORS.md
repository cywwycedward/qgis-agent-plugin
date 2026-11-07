# 错误码

结论：错误码只有 25 个，全部来自 PRD 11.2 的表（`src/qgis_agent_plugin/core/models.py::ERROR_CODES`），
代码里没有表外的码。它们按**什么时候产生**分三类，调用方的处理方式完全不同：

| 类 | 出现方式 | 有没有执行记录 | 码 |
| --- | --- | --- | --- |
| 注册期 | 插件加载时抛异常，插件起不来 | 无 | `INVALID_OPERATION_DEFINITION`、`DUPLICATE_OPERATION` |
| 控制面 | 调用本身无效，抛 `FrameworkError`（有 `.code`、`.message`） | **无** | `OPERATION_NOT_FOUND`、`WRONG_THREAD`、`ENGINE_CLOSING`、`UNSUPPORTED_EXECUTION_MODE`、`EXECUTION_NOT_FOUND` |
| 执行面 | 调用有效但失败，返回 `FAILED` 记录 | 有，`record.error.code` | 其余 18 个 |

`record.error` 的形状是 `{"code": "...", "message": "...", "details": null}`：
`details` 字段存在但**恒为 `null`**（`FrameworkError` 只带 code 和 message，没有 details 通路）。
`message` 是给人看的一句话，不含原始路径、provider URI 或异常文本；堆栈只进本机日志
（PRD 11.1、11.3）。

## 1. 注册期

插件 `initGui` 里注册九个 Operation 时抛出就是插件加载失败，不会出现“半个注册表”。

| 码 | 触发 | 调用方该做什么 |
| --- | --- | --- |
| `INVALID_OPERATION_DEFINITION` | 声明不合法：名字不匹配 `[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*`、`description` 为空、输入/输出不是 Pydantic 模型、`destructive` 不是 bool、`kind` 与 Command/Query 不符、`strategy` 缺失或两套阶段都实现 | 这是构建缺陷，不是用户输入问题；看 `message`（带 Operation 名），修代码后重装 |
| `DUPLICATE_OPERATION` | 同一个名字注册两次；先注册的那个不被覆盖 | 同上；`register_all()` 调两次就会命中 |

## 2. 控制面（抛异常，不建记录）

这五个码表示“这次调用根本没成立”，因此引擎不会留下记录，重试也没意义，
先改调用方式。

| 码 | 触发 | 调用方该做什么 |
| --- | --- | --- |
| `OPERATION_NOT_FOUND` | `run_sync`/`submit` 的名字不在注册表里 | 用 `iface.agent_registry.list_specs()` 取准确名字；错别字不会被猜 |
| `WRONG_THREAD` | 在非创建引擎的线程上调用任一引擎方法（`run_sync`/`submit`/`get`/`cancel`/`shutdown`） | 回主线程。后台线程只拿得到 `TaskControl`，没有别的通路 |
| `ENGINE_CLOSING` | 插件已卸载（`shutdown()` 之后）；`get` 例外，它仍可读历史 | 重新启用插件；历史记录在本次会话内仍可读 |
| `UNSUPPORTED_EXECUTION_MODE` | 对 background Operation 调 `run_sync`（当前只有 `layer.sum_numeric_field`） | 用 `submit()`，然后用 `get()` 看状态 |
| `EXECUTION_NOT_FOUND` | `get`/`cancel` 的 ID 不存在，或已超过 200 条 / 1800 秒的终态保留上限 | 记新 ID；不要假设旧 ID 永久有效 |

## 3. 执行面（`FAILED` 记录）

| 码 | 产生方 | 触发 | 调用方该做什么 |
| --- | --- | --- | --- |
| `INVALID_INPUT` | 引擎 | 入参 Pydantic 校验失败：未知字段、类型不符（含 `"true"` 当布尔）、空字符串、超长、`sub_layer` 字符不合规 | 按 `message` 里的字段名修参数；GIS 侧没有发生任何副作用 |
| `INVALID_CRS` | `project.create` | `crs_authid` 构造不出有效 CRS | 换一个 authid；当前工程未被替换 |
| `PROJECT_REPLACE_REQUIRED` | `project.create`、`project.open` | 当前工程有图层、有关联文件或 dirty，且没传 `discard_current=true` | 先 `project.save` 或 `project.get_info` 确认，再显式传 `true` |
| `PROJECT_OPEN_FAILED` | `project.open` | `project.read()` 返回 `False`（文件损坏/不是工程文件），工程 XML 嵌套超过 256 层（按 `read()` 的耗时标定：900 层要 30 s）、标签超过 400000 个、`.qgz` 成员超过 1000 个或不是合法 XML（都在 `read()` 之前拒绝：第一项会让 QGIS 解析器爆栈且停住主线程），或 `.qgz` 里有取不出来的成员（加密、不支持的压缩方法、流对不上清单；这种档案不会交给 `read()`） | 重开失败**不保证**原工程还在；重新 `project.get_info` 或 `project.open` 一次 |
| `PROJECT_SAVE_FAILED` | `project.save` | `project.write()` 返回 `False`（磁盘/权限/编码问题） | 目标文件可能已部分写入，不要假装保存成功；换路径或修权限 |
| `PROJECT_PATH_REQUIRED` | `project.save` | 没给 `path`，且工程没有关联文件 | 传绝对 `.qgs`/`.qgz` 路径 |
| `TARGET_EXISTS` | `project.save` | 目标已存在（含工程自己的关联文件）且 `overwrite` 不是 `true` | 确认要覆盖再传 `overwrite=true`；不接受隐式覆盖 |
| `PATH_NOT_ALLOWED` | 路径策略 | 路径不存在、不是普通文件、软链接跳出、不在允许根目录内、是 URL/provider URI/`/vsi*`/Windows UNC（`\\host\share\…`）、父目录不合法；`project.open` 时工程文件不是 UTF-8、声明了 XML 实体，或工程文件 / `.qgz` 档案本身 / 每个成员解压后的总量超过 16 MB，或工程文件里任何元素的文本以 `/vsi`/UNC 前缀**开头**（或 `<datasource>` 的整段文本拼接后带 `://`、是远程 URI、以 `file://host` 开头），或某层的数据源不在 `allowed_data_roots` 内（这些层会被移除，整次 open 失败）；**默认空允许目录下所有文件访问都命中它** | 先配 `qgis_agent_plugin/allowed_*_roots`（见 `docs/INSTALL.md`）；消息里不会回显你的路径 |
| `INVALID_LAYER` | `layer.add`、`layer.sum_numeric_field` | 图层构造后 `isValid()` 为假；求和的对象不是有效矢量图层 | 检查数据源本身；工程图层数不会变 |
| `LAYER_NOT_FOUND` | `layer.get_info`、`layer.set_visibility`、`layer.sum_numeric_field` | 工程里没有这个 `layer_id` | 用 `layer.list` 拿真实 ID；同名的不同图层 ID 不同 |
| `LAYER_NOT_IN_TREE` | `layer.set_visibility` | 图层已注册但没有图层树节点（`add_to_legend=false` 加进来的） | 不要对这样的图层调可见性；只能 `layer.get_info` |
| `INVALID_FIELD_TYPE` | `layer.sum_numeric_field` | 没有这个字段，或字段不是整型/浮点，或字段里存在 NaN/Infinity（这种值让结果不是合法 JSON），或求和溢出成 Infinity（两个有限值相加也可以） | 换字段名；文本字段不能求和；非有限值与溢出都需要先清洗数据 |
| `SNAPSHOT_LIMIT_EXCEEDED` | `layer.sum_numeric_field` | 要素数超过 `max_snapshot_features`（默认 10000） | 该 Operation 只用于线程/取消验收，不为大图层设计；不要指望它静默截断 |
| `TOO_MANY_EXECUTIONS` | 引擎 `submit` | 排队数超过 `max_pending`（默认 32）。实测：第 33 个 `submit` 拿到 `FAILED` | 等已有调用结束（`get` 轮询）再提交；请求不会被静默丢弃，而是明确失败 |
| `TASK_SUBMISSION_FAILED` | 引擎 / 后台适配器 | 没有装后台适配器，或 `QgsTaskManager.addTask()` 返回 0（QGIS 拒绝接手） | 重试一次；持续失败说明 QGIS 任务系统有问题，`message` 里有原因类别 |
| `STALE_PROJECT` | 后台适配器 / `finalize_main` | 任务运行期间工程被替换（`project_epoch` 变了），结果被拒绝 | 结果作废；在新工程上重新提交 |
| `OUTPUT_VALIDATION_FAILED` | 引擎 | Operation 的返回值不符它自己声明的输出模型 | 构建缺陷，不是调用方的问题；报 bug |
| `INTERNAL_ERROR` | 引擎 / 适配器 | 未预期异常，或状态机拒绕。`message` 是下列几种之一：`<异常类型名>: unexpected failure`（异常原文只进日志）、`QGIS did not create a new project`（`iface.newProject()` 返回 `False`）、`the task ended without a result`（`finished(false)` 且无异常、无取消）、`the completion of this execution failed`（完成回调自己抛了）、`<execution-id>: <旧状态> -> <新状态> is not a transition` 或 `<execution-id>: <状态> is final and cannot change`（`ExecutionStore` 拒绝的写入） | 先看本机日志里的堆栈；`message` 里带 execution id 的两条是状态机拒绕，属于引擎缺陷（不是调用方的问题） |

补充：`unload()` 期间引擎先把每条活动后台记录写成 `CANCEL_REQUESTED`，所以卸载之后
这些调用只会归档成 `CANCELLED` 或 `FAILED`，不会再进 `finalize_main` 去改工程（PRD 9.3.3）。

## 4. `cancel()` 的回执

`cancel` 不抛业务错（只有控制面的 `EXECUTION_NOT_FOUND` / `WRONG_THREAD` / `ENGINE_CLOSING`），
返回 `{"execution_id", "accepted", "status", "reason"}`。`accepted=true` 只表示请求被接受，
**不等于**已经停下：

| 当时的状态 | 结果 |
| --- | --- |
| `QUEUED`（主线程或后台） | `accepted=true`，记录**立即** `CANCELLED`；之后的事件回调会跳过它；后台的排队调用还会被从适配器的等待队列里摘掉，不占位 |
| 后台 `SCHEDULED` / `RUNNING` | `accepted=true`，记录转 `CANCEL_REQUESTED`，并调 `QgsTask.cancel()`；等任务自己报告终止才变 `CANCELLED` |
| `FINALIZING` | `accepted=false`，`reason="FINALIZING"`：结果正在提交，不允许出现“写完了却报取消” |
| 已是终态 | `accepted=false`，`reason="ALREADY_TERMINAL"` |
| 已是 `CANCEL_REQUESTED` | `accepted=true`，状态不变：重复取消只是再确认一次请求，等任务自己报告终止才变 `CANCELLED` |
| 主线程已 `RUNNING` | `accepted=false`，`reason="NOT_INTERRUPTIBLE"`：同步调用没法从外部打断 |

已实测：`submit` 之后立刻 `cancel` 一个主线程调用，得到
`{"accepted":true,"status":"CANCELLED"}`，事件循环之后仍是 `CANCELLED`。

## 5. 日志与排查

- 每次状态迁移写一条标准 `logging` 记录（logger 名 `qgis_agent_plugin.core.execution_store`），
  含 `execution_id`、`operation`、`strategy`、`旧状态 -> 新状态`、耗时和错误码（PRD 11.3）。
  落到哪里由 QGIS 的日志配置决定；在 Console 里可以先 `import logging; logging.basicConfig(level=logging.INFO)`。
- 排查顺序：`record.status` → `record.error.code` → `record.error.message` → 本机日志里的堆栈
  （`INTERNAL_ERROR` 的 `message` 故意只给类型名或固定短语，异常原文只进日志）。
- 不要从 `message` 里找路径：路径与 provider URI 一律不回显（PRD 10.2、11.1）。
