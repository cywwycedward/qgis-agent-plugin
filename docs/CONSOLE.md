# Python Console 调用样例

结论：本插件在 Python Console 里只有两个入口——`iface.agent_engine`（调用）和
`iface.agent_registry`（看契约）。十六个 Operation 用 `run_sync` 同步执行，唯一的后台 Operation
（`layer.sum_numeric_field`）用 `submit` + `get`。下面每段样例都标了实测状态：标“已实测”的是真跑过的，输出是从 Console
里摘的摘要；标“未实测”的写明为什么没跑。

## 0. 实测环境与标注约定

| 项 | 值 |
| --- | --- |
| QGIS | 3.44.13-Solothurn（`Qgis.QGIS_VERSION`） |
| Python / Qt | 3.12.13 / 5.15.13 |
| Pydantic / pytest | 2.13.4 / 9.2.0 |
| 跑法 | `D:\QGIS 3.44.13\bin\python-qgis-ltr.bat` + headless `QgsApplication`（`QT_QPA_PLATFORM=offscreen`）+ 真 `QgsProject` + 真 `QgsTaskManager` |

标注规则：

- **已实测（headless）**：在 headless 里真跑过。headless 没有 `iface`，所以引擎是手工用
  `initGui` 里同样的类构造的（`OperationEngine(registry, Settings(...), ProjectLifecycle(),
  iface=..., project=QgsProject.instance(), background_runner=OperationTaskRunner(...))`）；
  样例里的 `engine` 在 GUI 里就是 `iface.agent_engine`，代码一样。
- **已实测（GUI 未跑）**：逻辑跑过，但依赖 `iface` 的那一步（目前只有 `project.create` 的
  `iface.newProject`）在 headless 里用替身。
- **未实测**：没跑，原因写在旁边。不把没跑过的写成跑过。

## 1. 找会话

```python
registry = iface.agent_registry
engine = iface.agent_engine
len(registry.list_specs())  # 17
[s.name for s in registry.list_specs()]
```

实测（headless，同一 registry）：

```text
17
['layer.add', 'layer.get_info', 'layer.get_qml', 'layer.get_renderer',
 'layer.list', 'layer.list_available_renderers', 'layer.list_style_categories',
 'layer.set_renderer', 'layer.set_visibility', 'layer.sum_numeric_field',
 'processing.get_algorithm_info', 'processing.list_algorithms', 'processing.run_algorithm',
 'project.create', 'project.get_info', 'project.open', 'project.save']
```

`unload` 之后这两个属性会被撤掉，`hasattr(iface, "agent_engine")` 变 `False`。

## 2. 同步读一次工程状态

```python
record = engine.run_sync("project.get_info", {})
record.status  # 'SUCCEEDED'
record.model_dump_json(indent=2)
```

已实测（headless）：

```json
{
  "execution_id": "df5781d5-8665-4ab1-b35e-39057bb83e54",
  "operation": "project.get_info",
  "kind": "query",
  "strategy": "main_thread",
  "status": "SUCCEEDED",
  "progress": 100.0,
  "created_at": "2026-11-07T11:11:08.210784+00:00",
  "started_at": "2026-11-07T11:11:08.211785+00:00",
  "finished_at": "2026-11-07T11:11:08.211785+00:00",
  "project_epoch": 0,
  "result": {
    "file_path": null,
    "crs_authid": "",
    "is_dirty": false,
    "layer_count": 0,
    "project_epoch": 0
  },
  "error": null
}
```

`crs_authid` 是工程自己的 CRS，空工程可能是 `""`；`project_epoch` 是插件本地的代数，不是
QGIS 的工程版本号。**控制面通过之后**，`run_sync` 返回终态（`SUCCEEDED` 或 `FAILED`）；控制面失败
（未知 Operation、非主线程、引擎已关闭、对后台 Operation 调用）直接抛 `FrameworkError`，不建记录。

## 3. 参数写错的样子

```python
engine.run_sync("project.get_info", {"discard_current": True})
```

已实测（headless）：

```text
status: FAILED | error: INVALID_INPUT | project.get_info: invalid input at discard_current
```

多余的键不会被忽略（所有输入模型 `extra="forbid"`），布尔也不接受字符串。这类失败在 GIS
侧没有任何副作用。

## 4. 加图层、看图层

```python
added = engine.run_sync(
    "layer.add",
    {
        "kind": "vector",
        "source_path": r"C:\temp\data\cities.geojson",
        "name": "Cities",
    },
)
layer_id = added.result["layer_id"]
engine.run_sync("layer.list", {}).result
engine.run_sync("layer.get_info", {"layer_id": layer_id}).result
```

已实测（headless，源是 4 个点的 GeoJSON，`pop` 字段里有一个 null）：

```text
added              -> SUCCEEDED
{"layer_id": "Cities_5945552f_c430_4aaf_b619_3b51a9de718a", "name": "Cities",
 "kind": "vector", "provider": "ogr", "crs_authid": "EPSG:4326"}

layer.list         -> {"layers": [{"layer_id": "Cities_5945552f_...", "name": "Cities",
                       "kind": "vector", "crs_authid": "EPSG:4326",
                       "in_layer_tree": true, "visible": true, "effective_visible": true}]}

layer.get_info     -> {"layer_id": "Cities_5945552f_...", ..., "provider": "ogr",
                       "valid": true, "in_layer_tree": true,
                       "visible": true, "effective_visible": true}
```

`provider` 由 `kind` 决定（vector→`ogr`，raster→`gdal`），调用方不能指定。图层的 `layer_id`
由 QGIS 生成，带随机后缀，所以上面只保证前缀一致。

## 5. 可见性

```python
engine.run_sync("layer.set_visibility", {"layer_id": layer_id, "visible": True})
engine.run_sync("layer.set_visibility", {"layer_id": layer_id, "visible": False})
engine.run_sync("layer.set_visibility", {"layer_id": layer_id, "visible": True})
```

已实测（headless）：

```text
{"visible": true,  "effective_visible": true,  "changed": false}   # 已经是 true：没动
{"visible": false, "effective_visible": false, "changed": true }   # 隐藏
{"visible": true,  "effective_visible": true,  "changed": true }   # 再显示
```

`visible` 是节点自己的勾选状态，`effective_visible` 是连父级组一起算的实际可见性；父组被隐藏时
会出现 `visible=true, effective_visible=false`（单测覆盖，本文件未实测该组合）。

## 6. 后台求和（唯一的 background Operation）

```python
submitted = engine.submit("layer.sum_numeric_field", {"layer_id": layer_id, "field_name": "pop"})
submitted.status  # 'QUEUED'
engine.get(submitted.execution_id).status  # 下一轮事件循环后
```

已实测（headless，4 个要素、1 个 null）：

```text
submit -> QUEUED
observed statuses (polling): QUEUED -> SCHEDULED -> SUCCEEDED    # RUNNING 落在两次采样之间
result: {"layer_id": "Cities_...", "field_name": "pop", "count": 3,
         "skipped_null": 1, "total": 225.0, "snapshot_project_epoch": 0}
progress: 100.0
```

几条实测到的事实：

- worker 在 Qt 线程池线程上跑（探针里线程名 `Dummy-1`），`finished` 回调在 `MainThread`。
- 记录里的进度不止两端：首条进度报告会立刻写一次（并把记录推进到 `RUNNING`），完成前还会
  补写最后一条被节流的值。实测（直接记录每次 `store.transition`，同一个 4 要素图层）：
  `PREPARING 0 → SCHEDULED 0 → RUNNING 0 → RUNNING 33.333 → RUNNING 66.667 → FINALIZING 66.667
  → SUCCEEDED 100`；9000 要素则是 `RUNNING 0 → RUNNING 1.0 → RUNNING 99.0 → FINALIZING 99.0
  → SUCCEEDED 100`。
- 轮询 `engine.get()` 看不到这些中间值，因为它们落在两次轮询之间；`RUNNING` 也常常错过。
  那是轮询粒度问题，不是记录没经过：状态机不允许 `SCHEDULED -> SUCCEEDED`，完成路径也会先用
  `_running()` 把 `SCHEDULED` 补成 `RUNNING`。
- 快照里只剩一个非 null 值时，worker 唯一一次 `set_progress(100)` 会被适配器的 `>= 100` 守卫
  丢掉，记录就是 `PREPARING 0 → SCHEDULED 0 → RUNNING 0 → FINALIZING 0 → SUCCEEDED 100`（实测）：
  这里的 `RUNNING` 来自完成路径补写，不是进度报告。
- 9000 要素的同一调用：`count=9000`、`total=40495500.0`。
- 超过 `max_snapshot_features`（默认 10000）：10001 个要素 → `FAILED`
  `SNAPSHOT_LIMIT_EXCEEDED`，不做静默截断。
- 在 Console 里不要忙等：打完下一行代码就是一次事件循环，`get` 即可；等任务真正跑完再 `get`
  才会看到终态。

`run_sync` 调它会被拒绝（控制面，不建记录）：

```text
### run_sync layer.sum_numeric_field
FrameworkError: UNSUPPORTED_EXECUTION_MODE | layer.sum_numeric_field is a background operation; submit it instead
```

## 7. 取消

已经被接受但还没开始的调用，取消是立即的：

```python
queued = engine.submit("project.get_info", {})
engine.cancel(queued.execution_id)
```

已实测（headless）：

```text
submit -> QUEUED
{"execution_id":"7875fb2e-d005-4c4b-9e84-062856b59ce5","accepted":true,"status":"CANCELLED","reason":null}
after the event loop: CANCELLED      # 排队中的回调被跳过，没有再执行
```

后台任务一旦交给 QGIS，取消只是“请求”：

```python
receipt = engine.cancel(running_id)  # accepted=true, status='CANCEL_REQUESTED'
engine.get(running_id).status  # 等任务自己报告，才变 'CANCELLED'
```

**未实测（本文件）**：正在 `RUNNING` 的 `QgsTask` 的 `CANCEL_REQUESTED -> CANCELLED` 这一段。
4 要素/9000 要素的求和在毫秒级结束，构造不出稳定的取消窗口；这条路径由 `tests/test_qgs_task.py` 的
单测覆盖。六格取消矩阵见 `docs/agents/DEVELOP.md` §6.8。

`cancel` 对主线程正在执行的调用、以及 `FINALIZING` 的记录，返回 `accepted=false`
与 `reason="NOT_INTERRUPTIBLE"` / `"FINALIZING"`。

## 8. 保存与打开工程

```python
engine.run_sync("project.save", {"path": r"C:\temp\projects\demo.qgz"})
engine.run_sync("project.save", {"path": r"C:\temp\projects\demo.qgz"})  # 第二次
engine.run_sync("project.save", {"path": r"C:\temp\projects\demo.qgz", "overwrite": True})
engine.run_sync("project.save", {})  # Save：自己的关联文件
engine.run_sync("project.open", {"path": r"C:\temp\projects\missing.qgz"})
engine.run_sync("project.open", {"path": r"C:\temp\projects\broken.qgz"})
engine.run_sync("project.open", {"path": r"C:\temp\projects\demo.qgz"})
```

已实测（headless 真 QGIS，`demo.qgz` 里一个图层）：

```text
save #1     -> SUCCEEDED  {"file_path": "...\\demo.qgz", "is_dirty": false}
save #2     -> FAILED     TARGET_EXISTS | the target file exists; pass overwrite=true to replace it
save #3     -> SUCCEEDED  {"file_path": "...\\demo.qgz", "is_dirty": false}   # overwrite=true
save assoc  -> SUCCEEDED  {"file_path": "...\\demo.qgz", "is_dirty": false}   # 不给 path
open 缺失   -> FAILED     PROJECT_OPEN_FAILED | the project file could not be read; the current project was cleared
               之后工程里 0 个图层
open 损坏   -> FAILED     PROJECT_OPEN_FAILED | the project file could not be read; the current project was cleared
open demo   -> SUCCEEDED  {"file_path": "...\\demo.qgz", "crs_authid": "", "layer_count": 1,
                           "invalid_layer_count": 0}
open 已脏   -> FAILED     PROJECT_REPLACE_REQUIRED | the current project has unsaved changes; pass
                          discard_current=true to replace it
open 已脏 + discard -> SUCCEEDED  {"...", "layer_count": 1, "invalid_layer_count": 0}
```

两个判据与 GUI 是同一个：

- **覆盖**只由 `Save As` 问：给了 `path` 且文件已存在才要 `overwrite=true`；`project.save` 不给
  `path` 写的是工程自己的关联文件，那条路不再问覆盖（`save assoc` 上面就是第二次写同一个文件），
  因为 GUI 的 Save 不弹对话框。
- **替换**只看工程有没有未保存内容（`is_dirty()`）。一个有图层但没改过的工程不需要
  `discard_current`；“缺失文件”归 `PROJECT_OPEN_FAILED`：插件不预检存在性，`read()` 打不开就是
  打不开。失败时当前工程**已被清空**（GUI 的 `addProject` 也是先 `closeProject()` 再 `read()`），
  消息里写的是这一点，不是“可能变了”。

```python
engine.run_sync("project.open", {"path": r"C:\temp\projects\demo.qgz", "discard_current": True})
engine.run_sync("project.create", {})  # 用 QGIS 自己的默认 CRS
engine.run_sync("project.create", {"crs_authid": "epsg:3857", "discard_current": True})
engine.run_sync("project.create", {"crs_authid": "NOT:A_CRS", "discard_current": True})
engine.run_sync("project.get_info", {})
```

已实测（headless 真 QGIS；`project.create` 的 `iface.newProject(promptToSaveFlag=False)` 用等价替身
“`QgsProject.instance().clear()` + 返回 `True`”，`project.open` 不经过 `iface`）：

```text
open demo.qgz（数据源在 C:\temp\data，与工程文件不同目录）
              -> SUCCEEDED  {"file_path": "...\\demo.qgz", "crs_authid": "", "layer_count": 1,
                             "invalid_layer_count": 0}
create epsg   -> SUCCEEDED  {"crs_authid": "EPSG:3857", "file_path": null, "layer_count": 0}
get_info      -> SUCCEEDED  {"file_path": null, "crs_authid": "EPSG:3857", "is_dirty": false,
                             "layer_count": 0, "title": "", "project_epoch": 0}
create 乱写 CRS -> SUCCEEDED {"crs_authid": "", "file_path": null, "layer_count": 0}
get_info      -> SUCCEEDED  {"file_path": null, "crs_authid": "", "is_dirty": false,
                             "layer_count": 0, "title": "", "project_epoch": 0}
```

工程点名的数据源不再由插件审：在任何目录、不存在、甚至写成远程，工程照样打开，加载不出来的层
只让 `invalid_layer_count` 变大。`read()` 之前只剩两条防死检查（嵌套超过 4096 层、`.qgz` 里有加密
成员），见 `docs/agents/DEVELOP.md` §7.2。

四条要注意的事：

- 替换工程会让 `project_epoch` 加 1（旧代数的排队调用因此 `CANCELLED`），但**记录里保存的是接受调用
  时的那一代**。上面那份样例脚本没有接 `aboutToBeCleared`，所以 `project_epoch` 一直是 0；插件装配
  好之后每换一次工程才加 1。
- `crs_authid` 为 `""` 不是错误：`QgsProject` 没有 CRS 时就是这个值。`create` 传一个构造不出 CRS 的
  字符串不再被拒（GUI 的新建动作也不拒），工程留下的是空 CRS，结果如实报 `""`。
- `create` 不给 `crs_authid` 就不碰 QGIS 自己给新工程的 CRS（设置里的 `projections/defaultProjectCrs`
  与默认模板）。
- `create` 之后 `is_dirty` 是 `false`：GUI 的 `fileNew` 最后一步就是 `setDirty(false)`。

## 9. 常见错误速览

已实测（headless），一条一行：

| 调用 | 结果 |
| --- | --- |
| `run_sync("no.such", {})` | 抛 `FrameworkError: OPERATION_NOT_FOUND` |
| `get("no-such-id")` | 抛 `FrameworkError: EXECUTION_NOT_FOUND` |
| 在工作线程里 `engine.run_sync(...)` / `engine.get(...)` | 抛 `FrameworkError: WRONG_THREAD` |
| `layer.add` 传 `add_to_legend="true"` | `FAILED` `INVALID_INPUT` at `add_to_legend` |
| `layer.add` 的路径是相对路径 | `FAILED` `PATH_NOT_ALLOWED` |
| `layer.add` 指向一个不是数据的文件 | `FAILED` `INVALID_LAYER` |
| `layer.add(kind="raster", sub_layer="x", ...)` | `FAILED` `INVALID_INPUT` at `<root>` |
| `layer.get_info` 传不存在的 ID | `FAILED` `LAYER_NOT_FOUND` |
| `layer.set_visibility` 对一个 `add_to_legend=false` 的图层 | `FAILED` `LAYER_NOT_IN_TREE` |
| `layer.sum_numeric_field` 对文本字段 | `FAILED` `INVALID_FIELD_TYPE` |
| `project.save` 不给路径且工程无关联文件 | `FAILED` `PROJECT_PATH_REQUIRED` |
| `project.create` 传 `crs_authid="NOT:A_CRS"` | `SUCCEEDED`，`crs_authid` 是 `""`（GUI 的新建动作也不拒） |
| `project.create` / `project.open` 在有未保存内容的工程上不传 `discard_current` | `FAILED` `PROJECT_REPLACE_REQUIRED` |
| `project.save` 给了 `path` 且文件已存在、不传 `overwrite` | `FAILED` `TARGET_EXISTS`；不给 `path` 写关联文件不问覆盖 |
| 连发 34 个 `submit("project.get_info", {})`（不推事件循环） | 前 32 个 `QUEUED`，第 33、34 个 `FAILED` `TOO_MANY_EXECUTIONS` |
| 全新安装（允许目录为空）调 `layer.add` / `project.save` | `FAILED` `PATH_NOT_ALLOWED` |

## 10. 未实测清单

| 样例 | 为什么没跑 |
| --- | --- |
| `iface.agent_engine` / `iface.agent_registry` 本身 | 需要 QGIS Desktop 的 `iface`；headless 里用同一套类手工装配，`initGui` 的发布逻辑由单测覆盖 |
| `project.create` 的 `iface.newProject(promptToSaveFlag=False)` 这一步 | 只有 GUI 里有它；headless 里用等价替身（`QgsProject.instance().clear()` + 返回 `True`），其余逻辑已实测 |
| 运行中后台任务的 `CANCEL_REQUESTED -> CANCELLED` | 现有后台 Operation 太快，构造不出稳定窗口 |
| 父级组隐藏时的 `visible=true / effective_visible=false` | 需要 GUI 图层树里的组；单测覆盖，Console 未跑 |
| 多个后台任务排队（`max_active_background=2` 的 FIFO） | 探针里三次 9000 要素求和都在三轮事件循环内跑完，看不到第三个停在 `QUEUED`；单测覆盖该路径 |
| 配置键写入（`QgsSettings().setValue(...)`） | 会改用户 `QGIS3.ini`，只验证了读：默认空根、缺失键返回 `None`、越界拒绝 |
