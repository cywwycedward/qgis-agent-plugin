# 九个 Operation 的契约（以代码为准）

结论：本插件的全部能力就是下面九个 Operation。表里每一项都能在代码里找到出处，
默认值与校验规则来自 `src/qgis_agent_plugin/operations/*.py` 的输入模型，错误码来自
`core/models.py` 的 `ERROR_CODES`；与 PRD 的差异集中在 §12，不要照 PRD 的示例写调用方。

| # | Operation | kind | strategy | destructive | 必填参数 | 一句话 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `project.create` | command | main_thread | **是** | 无 | 换成指定 CRS 的空工程 |
| 2 | `project.open` | command | main_thread | **是** | `path` | 打开本地 `.qgs` / `.qgz` |
| 3 | `project.save` | command | main_thread | **是**（可能覆盖） | 无 | 写当前工程到本地文件 |
| 4 | `project.get_info` | query | main_thread | 否 | 无 | 读工程文件/CRS/dirty/图层数/epoch |
| 5 | `layer.add` | command | main_thread | 否 | `kind`、`source_path`、`name` | 把本地矢量/栅格加进工程 |
| 6 | `layer.list` | query | main_thread | 否 | 无 | 列出工程里的图层 |
| 7 | `layer.get_info` | query | main_thread | 否 | `layer_id` | 读一个图层的信息 |
| 8 | `layer.set_visibility` | command | main_thread | 否 | `layer_id`、`visible` | 勾选/取消一个图层的树节点 |
| 9 | `layer.sum_numeric_field` | query | **background** | 否 | `layer_id`、`field_name` | 用 `QgsTask` 求和一份快照 |

读这张表的三个前提：

- **只有 8 个能在 `run_sync` 里跑**。第 9 个必须 `submit`；`run_sync` 会被拒绝
  `UNSUPPORTED_EXECUTION_MODE`（控制面异常，不建记录，已实测）。
- **所有输入模型都是 `extra="forbid"`**（`core/models.py::Params`）：多写一个键不是被忽略，
  而是 `FAILED(INVALID_INPUT)`，`message` 里带出错字段名，例如
  `project.get_info: invalid input at discard_current`（已实测）。
- **所有布尔参数都是 `StrictBool`**：`"true"`、`1` 一律 `INVALID_INPUT`
  （`add_to_legend: "true"` 已实测被拒；`"true"`/`0`/`1` 由单测覆盖）。字符串参数是 `StrictStr`，不接受数字。

`OperationSpec`（`iface.agent_registry.get_spec(name)` / `list_specs()`）是同一份契约的
机器可读版本，字段为 `name`、`description`、`kind`、`strategy`、`destructive`、
`input_schema`、`output_schema`、`pydantic_major`（恒为 `2`）。JSON Schema 由 Pydantic 直接
生成，例如：

```python
spec = iface.agent_registry.get_spec("layer.add")
spec.input_schema["required"]  # ['kind', 'source_path', 'name']
spec.output_schema["required"]  # ['layer_id', 'name', 'kind', 'provider', 'crs_authid']
```

## 1. 通用约定

### 1.1 失败长什么样

两类失败不要混：

| 形态 | 何时 | 调用方拿到什么 |
| --- | --- | --- |
| 控制面异常 | 调用本身无效：未知 Operation、未知执行 ID、非主线程、引擎已关闭、对后台 Operation 调 `run_sync` | 抛 `FrameworkError`（有 `.code`、`.message`），**不建记录** |
| `FAILED` 记录 | 调用有效但执行失败：入参校验、路径策略、GIS 动作、输出校验、提交失败 | `ExecutionRecord.status == "FAILED"`，`record.error.code` 是 PRD 11.2 里的码 |

每个 Operation 的“错误码”表只列**业务码**（它自己会抛的那些）。控制面码、容量码、
输出校验码是所有 Operation 共用的，见 `docs/ERRORS.md`。

### 1.2 路径怎么判

`layer.add` 用 `allowed_data_roots`，`project.open` / `project.save` 用 `allowed_project_roots`；
两者都走 `core/security.resolve_path`：

- 必须是绝对路径，`Path.resolve()` 后的真实目标要落在允许根目录内（按路径组件比较，
  `..` 和软链接跳不出去）。
- 读（`layer.add`、`project.open`）：目标必须存在且是普通文件。
- 写（`project.save`）：目标可以不存在，但父目录必须存在且在根内；不创建父目录。
- URL、provider URI、`/vsi*`、以及 Windows UNC 路径（`\\host\share\…`、`//host/share/…`、
  `\\?\…`）都在**任何 I/O 之前**拒掉：UNC 的解析本身就会连主机并带上当前 Windows 凭据，
  所以它和 URL 一样不能先进白名单再判。错误消息里**不含**原始路径。
- 允许目录**默认为空**，即默认拒绝一切文件访问（已实测：`layer.add` 与 `project.save`
  在未配置时都是 `PATH_NOT_ALLOWED`）。配置方法见 `docs/INSTALL.md`。

### 1.3 调用面

| 方法 | 能调谁 | 返回 |
| --- | --- | --- |
| `run_sync(name, args)` | 只有 main_thread 的 8 个 | 终态记录（`SUCCEEDED` / `FAILED`） |
| `submit(name, args)` | 全部 9 个 | 刚创建的记录快照，通常是 `QUEUED` |
| `get(execution_id)` | — | 记录副本；未知 ID 抛 `EXECUTION_NOT_FOUND` |
| `cancel(execution_id)` | — | `CancelReceipt`，见 `docs/ERRORS.md` §4 |
| `shutdown()` | — | 无返回值；插件 `unload` 内部调用，之后新请求得 `ENGINE_CLOSING` |

全部五个方法（`run_sync`、`submit`、`get`、`cancel`、`shutdown`）都必须从 QGIS 主线程调用；其他线程抛 `WRONG_THREAD`（已实测）。

## 2. `project.create`

替换当前工程为一个指定 CRS 的空工程。

| 参数 | 类型 | 必填 | 默认 | 约束 |
| --- | --- | --- | --- | --- |
| `crs_authid` | StrictStr，长度 ≥ 1 | 否 | `"EPSG:4326"` | 必须能构造有效的 `QgsCoordinateReferenceSystem`；无效则在替换前拒绝 |
| `discard_current` | StrictBool | 否 | `false` | 只有显式 `true` 才允许替换“有内容”的工程 |

| 返回字段 | 类型 | 说明 |
| --- | --- | --- |
| `crs_authid` | string | 工程现在的**规范** authid（传 `epsg:4326` 也返回 `EPSG:4326`） |
| `file_path` | string \| null | 新工程没有关联文件时为 `null` |
| `layer_count` | int | 新工程的图层数，默认 0 |

| 错误码 | 触发 |
| --- | --- |
| `INVALID_CRS` | `crs_authid` 构造不出有效 CRS（在动当前工程之前，已实测） |
| `PROJECT_REPLACE_REQUIRED` | 当前工程有图层、有关联文件或是 dirty，且 `discard_current` 不是 `true`（已实测） |
| `INTERNAL_ERROR` | `iface.newProject(promptToSaveFlag=False)` 返回 `False`；PRD 7.2 没给这种情况指定码 |
| `INVALID_INPUT` | 未知字段 / 类型不符 / 空 `crs_authid` |

成功路径会触发 `QgsProject.aboutToBeCleared`（`iface.newProject` 会清空旧工程），因此
`project_epoch` 加 1，旧代数的排队调用立即 `CANCELLED`。**本次调用自己不受影响**：`_stop_background`
只对后台记录写取消请求，正在执行的主线程调用照常走到 `SUCCEEDED`（实测：`crs_authid` 被规范成
`EPSG:3857`、`file_path: null`、`layer_count: 0`，epoch 1 → 2）。新建之后 `project.get_info` 的
`is_dirty` 可能是 `true`（设置 CRS 就把工程标脏了），这是 QGIS 的事实，不是没保存成功。

## 3. `project.open`

用 `QgsProject.read(path)` 打开本地工程文件；不经过 `iface.addProject()`，所以不会弹任何对话框。

| 参数 | 类型 | 必填 | 默认 | 约束 |
| --- | --- | --- | --- | --- |
| `path` | StrictStr | **是** | — | 绝对路径且后缀为 `.qgs` / `.qgz`（不区分大小写），否则 `INVALID_INPUT`（已实测）；文件必须存在、是普通文件、在 `allowed_project_roots` 内 |
| `discard_current` | StrictBool | 否 | `false` | 同 `project.create` 的保护条件 |

| 返回字段 | 类型 | 说明 |
| --- | --- | --- |
| `file_path` | string | 规范化（`resolve()`）后的绝对路径 |
| `crs_authid` | string | 工程的 authid；没有 CRS 时可能是 `""` |
| `layer_count` | int | 注册进来的图层数 |
| `invalid_layer_count` | int | 其中 `isValid() == False` 的数量（AT-18B：工程读成功但图层坏掉不算失败，只是不完整） |

**工程里的数据源也要过白名单。** `read()` 只认工程文件，工程文件里写的图层数据源是另一回事，
所以 `read()` 之后会逐个图层问 `allowed_data_roots`（用 provider 自己的 `decodeUri()` 取真实路径，
不切字符串）：

- 有数据源落在根外（或 provider 说不出一个本地路径，例如 provider metadata 缺失）→ 这些图层
  用 `removeMapLayer()` 从工程里移除，然后整次 `project.open` 以 `PATH_NOT_ALLOWED` 失败，
  `message` 里带被移除的图层数。**不是**“加载成功 + 只报个字段”：那等于白名单被绕过。
- 数据源文件**不存在**不算越界（它还在根内）：那是 AT-18B 的部分加载，照常 `SUCCEEDED` +
  `invalid_layer_count > 0`。
- 工程文件本身已经 `read()` 进去了，所以失败时**当前工程已经变成新工程的剩余部分**，不回滚。

| 错误码 | 触发 |
| --- | --- |
| `INVALID_INPUT` | 路径不是绝对路径、后缀不对、未知字段、布尔类型不符 |
| `PATH_NOT_ALLOWED` | 文件不存在 / 不在允许根目录内 / 是网络 URI 或目录（“不存在”也归这里，已实测）；或工程里某层的数据源不在 `allowed_data_roots` 内（该层会被移除，见上） |
| `PROJECT_REPLACE_REQUIRED` | 当前工程有内容且 `discard_current` 不是 `true`（已实测） |
| `PROJECT_OPEN_FAILED` | `project.read()` 返回 `False`。消息里明说“当前工程可能已经改变”，**不承诺回滚**（损坏文件已实测） |

成功路径同样会触发 `QgsProject.aboutToBeCleared`（`read()` 会清空旧工程），所以 `project_epoch` 加 1、
旧代数的排队调用立即 `CANCELLED`；**本次调用自己照常成功**（实测：打开一个含 1 个图层的工程得到
`layer_count: 1`、`invalid_layer_count: 0`，epoch 0 → 1，记录终态 `SUCCEEDED`）。四个失败路径
（`INVALID_INPUT`、`PATH_NOT_ALLOWED`、`PROJECT_REPLACE_REQUIRED`、`PROJECT_OPEN_FAILED`）与成功路径
互不干扰。

## 4. `project.save`

把当前工程写到本地文件。

| 参数 | 类型 | 必填 | 默认 | 约束 |
| --- | --- | --- | --- | --- |
| `path` | StrictStr \| null | 否 | `null` | 为空则用工程当前关联文件；给了就必须是绝对 `.qgs`/`.qgz` |
| `overwrite` | StrictBool | 否 | `false` | 目标已存在（**包括**关联文件本身）时必须显式 `true` |

| 返回字段 | 类型 | 说明 |
| --- | --- | --- |
| `file_path` | string | 实际写入的绝对路径 |
| `is_dirty` | bool | 写完之后工程的真实 dirty 状态；成功写入通常为 `false` |

| 错误码 | 触发 |
| --- | --- |
| `PROJECT_PATH_REQUIRED` | 没给 `path`，且工程没有关联文件（已实测） |
| `INVALID_INPUT` | `path` 形状不对 / 未知字段 / 布尔类型不符 |
| `PATH_NOT_ALLOWED` | 父目录不存在、不在允许根目录内、目标是目录 |
| `TARGET_EXISTS` | 目标已存在且没有 `overwrite=true`（已实测；重复保存同一路径即命中） |
| `PROJECT_SAVE_FAILED` | `project.write()` 返回 `False`；**文件可能已部分写入**，不承诺原子性 |

## 5. `project.get_info`

| 参数 | 说明 |
| --- | --- |
| 无 | 任何参数都 `INVALID_INPUT`（已实测） |

| 返回字段 | 类型 | 说明 |
| --- | --- | --- |
| `file_path` | string \| null | 关联文件；未保存过为 `null` |
| `crs_authid` | string | 工程 CRS；工程没有 CRS 时为 `""`（headless 空工程实测如此） |
| `is_dirty` | bool | 是否有未保存修改 |
| `layer_count` | int | 图层数 |
| `project_epoch` | int | 插件本地的工程代数，从 0 开始，每次 `aboutToBeCleared` 加 1 |

没有业务错误码。它不保存、不切图层、不改工程属性。

## 6. `layer.add`

把本地矢量或栅格文件加进当前工程。

| 参数 | 类型 | 必填 | 默认 | 约束 |
| --- | --- | --- | --- | --- |
| `kind` | enum `vector` \| `raster` | **是** | — | 决定 provider：vector 固定 `ogr`，raster 固定 `gdal`；调用方不能指定 provider |
| `source_path` | StrictStr，长度 ≥ 1 | **是** | — | 绝对、存在、普通文件、在 `allowed_data_roots` 内 |
| `name` | StrictStr | **是** | — | 1–128 字符 |
| `sub_layer` | StrictStr \| null | 否 | `null` | 仅 vector 允许；`^[A-Za-z0-9_-]{1,64}$`；URI 由 OGR provider 的 `encodeUri` 生成，不手拼 |
| `add_to_legend` | StrictBool | 否 | `true` | `false` 时不创建图层树节点 |

| 返回字段 | 类型 | 说明 |
| --- | --- | --- |
| `layer_id` | string | QGIS 分配的图层 ID，后续操作用它 |
| `name` | string | 实际名称（与入参一致） |
| `kind` | enum `vector` \| `raster` | 入参里的 kind |
| `provider` | string | `ogr` 或 `gdal` |
| `crs_authid` | string | 图层 CRS |

| 错误码 | 触发 |
| --- | --- |
| `INVALID_INPUT` | kind 不是两个值之一、空路径/空名字、名字超 128、`sub_layer` 字符不合规或配在 raster 上、布尔类型不符（raster + `sub_layer` 与字符串布尔已实测，其余靠单测） |
| `PATH_NOT_ALLOWED` | 路径不在允许根目录内 / 不存在 / 是 URI（已实测） |
| `INVALID_LAYER` | `QgsVectorLayer`/`QgsRasterLayer` 构造后 `isValid()` 为假（坏文件已实测） |

不修改源文件，也不对同名图层做任何处理。`add_to_legend=false` 的图层没有树节点，
之后 `layer.set_visibility` 会对它返回 `LAYER_NOT_IN_TREE`（已实测）。

## 7. `layer.list`

| 参数 | 说明 |
| --- | --- |
| 无 | 任何参数都 `INVALID_INPUT` |

返回 `{"layers": [LayerSummary, ...]}`，按 `layer_id` 字典序升序（不依赖 `mapLayers()` 的迭代顺序）。

| LayerSummary 字段 | 类型 | 说明 |
| --- | --- | --- |
| `layer_id` | string | |
| `name` | string | |
| `kind` | enum `vector` \| `raster` \| `other` | `other` 是既非矢量也非栅格的注册图层 |
| `crs_authid` | string | |
| `in_layer_tree` | bool | 当前工程图层树里有没有它的节点 |
| `visible` | bool \| null | 节点自身的 `itemVisibilityChecked()`；没有节点时为 `null` |
| `effective_visible` | bool \| null | 节点的 `isVisible()`（受祖先组影响）；没有节点时为 `null` |

`visible` 与 `effective_visible` 是两个字段：父级组被隐藏时 `visible=true`、
`effective_visible=false`，不合并成一个数。没有业务错误码。

## 8. `layer.get_info`

| 参数 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `layer_id` | StrictStr | **是** | 长度 ≥ 1 |

| 返回字段 | 类型 | 说明 |
| --- | --- | --- |
| `layer_id`、`name`、`kind`、`crs_authid` | — | 同 `LayerSummary` |
| `provider` | string | 图层的 provider 类型 |
| `valid` | bool | `layer.isValid()`：数据源现在还读得到吗 |
| `in_layer_tree`、`visible`、`effective_visible` | — | 与 `layer.list` **同一套计算**，不会两处不一致 |

| 错误码 | 触发 |
| --- | --- |
| `INVALID_INPUT` | `layer_id` 缺失/为空/不是字符串 |
| `LAYER_NOT_FOUND` | 工程里没有这个 ID（已实测） |

不返回原始数据源 URI、连接串、要素或字段列表。

## 9. `layer.set_visibility`

| 参数 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `layer_id` | StrictStr | **是** | 长度 ≥ 1 |
| `visible` | StrictBool | **是** | 目标是节点自身的勾选状态 |

| 返回字段 | 类型 | 说明 |
| --- | --- | --- |
| `layer_id` | string | |
| `visible` | bool | 设置之后节点的勾选状态 |
| `effective_visible` | bool | `node.isVisible()`；父级组隐藏时仍可能是 `false` |
| `changed` | bool | 本次是否真的改了（同状态重复调用为 `false`） |

| 错误码 | 触发 |
| --- | --- |
| `INVALID_INPUT` | `layer_id` 为空 / `visible` 不是真布尔 |
| `LAYER_NOT_FOUND` | 工程里没有这个 ID（已实测） |
| `LAYER_NOT_IN_TREE` | 图层已注册但没有图层树节点，典型是 `layer.add(add_to_legend=false)`（已实测） |

只动这一个节点，不碰父级组、不碰其他图层、不改数据源。

## 10. `layer.sum_numeric_field`

唯一的后台 Operation：主线程拷一份数值快照，`QgsTask` 在别的线程求和，主线程校验代数后返回。

| 参数 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `layer_id` | StrictStr | **是** | 长度 ≥ 1 |
| `field_name` | StrictStr | **是** | 长度 ≥ 1；图层上要有这个字段，且是整型或浮点 |

| 返回字段 | 类型 | 说明 |
| --- | --- | --- |
| `layer_id`、`field_name` | string | 入参原样返回 |
| `count` | int | 参与求和的**非 null** 要素数 |
| `skipped_null` | int | 跳过的 null 数 |
| `total` | float | 快照的求和结果 |
| `snapshot_project_epoch` | int | 快照取自哪一代工程 |

| 错误码 | 触发 |
| --- | --- |
| `INVALID_INPUT` | 任一参数缺失/为空/类型不符（在 `submit` 返回前就判掉，不排队） |
| `LAYER_NOT_FOUND` | 工程里没有这个 ID |
| `INVALID_LAYER` | 不是 `QgsVectorLayer`，或图层 `isValid()` 为假 |
| `INVALID_FIELD_TYPE` | 没有该字段，或字段不是数值型（文本字段已实测） |
| `SNAPSHOT_LIMIT_EXCEEDED` | 要素数超过 `max_snapshot_features`（默认 10000）；**不做静默截断**（10001 个要素已实测） |
| `STALE_PROJECT` | 任务结束时 `project_epoch` 已经变了：结果算完也不交 |
| `TASK_SUBMISSION_FAILED` | 没装后台适配器，或 `QgsTaskManager.addTask()` 返回 0 |
| `UNSUPPORTED_EXECUTION_MODE` | 用 `run_sync` 调它（控制面异常，已实测） |

行为细节：

- 只有这一段是后台的：`prepare_main`（主线程读要素、跳过 null、拷成 float 元组）、
  `execute_worker`（后台求和）、`finalize_main`（主线程只比对 epoch）。
- 进度走 `QgsTask.setProgress`，记录最多每 100 ms 写一次，且**不写 100**——100 只由
  `SUCCEEDED` 那一步写，否则一个 3 ms 的任务会留下“100% 但已取消”的记录。首条报告会立刻写
  （并推进到 `RUNNING`），完成前还会补写最后一次被节流的值；一条可用报告都没有时（快照里只剩
  一个非 null 值），`RUNNING` 由完成路径补写。
- 任务声明了 `dependent_layer_ids`，适配器调用 `setDependentLayers`：图层被删时 QGIS 自己
  取消任务。
- 结果描述的是快照，不是任务结束那一刻图层里的值。
- 实测量级：4 要素（1 个 null）→ `count=3`、`skipped_null=1`、`total=225.0`；9000 要素 →
  `total=40495500.0`，状态观察到 `QUEUED → SCHEDULED → SUCCEEDED`，worker 在 Qt 线程池线程
  （`Dummy-1`）上跑，`finished` 回调在 `MainThread`。

## 11. 一次调用会碰到的其它码

这些不看 Operation 是谁，只看你怎么调（细节见 `docs/ERRORS.md`）：

| 码 | 何时 |
| --- | --- |
| `OPERATION_NOT_FOUND` | `name` 不在注册表里（控制面） |
| `WRONG_THREAD` | 不在创建引擎的那个线程上调（控制面） |
| `ENGINE_CLOSING` | 插件正在卸载（控制面） |
| `EXECUTION_NOT_FOUND` | `get`/`cancel` 的 ID 不存在或已被淘汰（控制面） |
| `TOO_MANY_EXECUTIONS` | `submit` 时排队数已达上限（实测：`max_pending=32` 时第 33 个 `submit` 得到 `FAILED`） |
| `OUTPUT_VALIDATION_FAILED` | Operation 的返回值不合它自己声明的输出模型 |
| `INTERNAL_ERROR` | 未预期异常；`message` 是下面几种之一（详见 `docs/ERRORS.md` §3）：`<异常类型名>: unexpected failure`、`QGIS did not create a new project`、`the task ended without a result`、`the completion of this execution failed`，或状态机拒绝的 `<id>: <旧状态> -> <新状态> is not a transition` |

## 12. 与 PRD 的差异（代码是事实）

| PRD | 代码 | 说明 |
| --- | --- | --- |
| 7.3 打开含坏图层的工程应 `PROJECT_OPEN_FAILED` | 返回 `SUCCEEDED` + `invalid_layer_count > 0` | 按 AT-18B 实现：读成功就是成功，但不谎称全部数据源可用 |
| 7.4 成功样例 `is_dirty: false` | 返回 `project.write()` 之后的真实值 | 通常情况下仍是 `false`，但这是读出来的，不是写死的 |
| 6.1 “兼容 Pydantic v1/v2” | 只支持 v2，`pydantic_major` 恒为 2 | 已记录偏差；非 v2 的 pydantic 在插件导入时直接 `RuntimeError` |
| 11.1 `error.details` | 字段存在但永远是 `null` | `FrameworkError` 只带 `code` + `message`，没有 details 通路 |
| 7.3 / 11.2 “含无法加载的图层 → `PROJECT_OPEN_FAILED`” | `SUCCEEDED` + `invalid_layer_count > 0` | 同一份 PRD 里这两句与 AT-18B（`read()` 为真但有坏图层 → 成功 + 计数）互斥。**以 AT-18B 与这里的代码为准**：读成功就是成功，坏图层只计数、不谎称可用。评审时不要按 7.3/11.2 那两句改代码 |
| 7.3 未提工程内数据源 | `project.open` 逐层校验数据源，越界则移除该层并整体 `PATH_NOT_ALLOWED` | 更严，不是更松：PRD 2.3/10.2 要的是数据白名单，而数据源是工程文件替调用方写的路径，所以它们也得过 `allowed_data_roots`（已实测，见 `PROJECT_OPEN_FAILED` 之外的这条码） |
| 未定义：provider 说不出本地路径 | 这种图层算越界（移除 + 失败） | 宁可拒绝：白名单无法为一个说不出来源的层担保；工程里出现 `memory` 等非文件 provider 时 `project.open` 会失败 |
