# 开发安装

结论：三条命令就能在 QGIS Desktop 3.44 里装上本插件，再用两个配置键打开文件访问。

```powershell
cd <repo>; npm run deploy          # 把 src/qgis_agent_plugin 镜像到 QGIS 插件目录
npm run test                       # 在 QGIS 的 python 里跑 pytest
npm run lint
```

装好之后：插件管理器里启用 “QGIS Agent”；配置 `qgis_agent_plugin/allowed_data_roots` 与
`qgis_agent_plugin/allowed_project_roots`；Python Console 里用 `iface.agent_engine`
调用（样例见 `docs/CONSOLE.md`）。**默认两个允许目录都是空的，即默认拒绝一切文件访问**。

## 1. 前置条件

| 项 | 要求 | 本机实测值 |
| --- | --- | --- |
| QGIS Desktop | 3.44.x（`metadata.txt`: `qgisMinimumVersion=3.44`、`qgisMaximumVersion=3.44.99`） | 3.44.13-Solothurn |
| Python | QGIS 自带 3.12 | 3.12.13 |
| Qt | QGIS 自带 | 5.15.13 |
| Pydantic | **只支持 2.x**；非 2 在插件导入时直接 `RuntimeError` | 2.13.4 |
| 第三方依赖 | 无（只用 QGIS/Qt 自带的 PyQt 与 Pydantic） | — |
| Node/npm | 只是脚本入口（部署、测试、lint），不影响插件运行 | 已可用 |

查版本：

```powershell
& 'D:\QGIS 3.44.13\bin\python-qgis-ltr.bat' -c "import pydantic, sys; print(sys.version.split()[0], pydantic.VERSION)"
# 3.12.13 2.13.4
```

## 2. 部署到 QGIS 的插件目录

路径全部来自 `paths.json`，脚本里没有硬编码：

| 键 | 值 | 含义 |
| --- | --- | --- |
| `qgis.python` | `D:\QGIS 3.44.13\bin\python-qgis-ltr.bat` | 跑 pytest 的解释器 |
| `qgis.pluginsDir` | `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins` | QGIS 用户插件目录 |
| `plugin.package` | `qgis_agent_plugin` | 源包名（`src/<package>`） |
| `plugin.installAs` | `qgis-agent-plugin` | 目标目录名 |

```powershell
npm run deploy:list    # 干跑，只列文件，不写盘
npm run deploy         # 真部署
```

实测（干跑，2026-11-07）：

```text
Source : ...\src\qgis_agent_plugin\
Dest   : C:\Users\<user>\AppData\Roaming\QGIS\QGIS3\profiles\default\python\plugins\qgis-agent-plugin\
Files : 18  Copied 18  Extras 68
deployed ... -> ...\qgis-agent-plugin
restart QGIS; enable "QGIS Agent" under Plugins > Manage and Install Plugins if it is not enabled yet
```

部署语义（脚本里定死，改脚本前先读它）：

- 用 `robocopy /MIR` 做**镜像**：源里删掉的文件，目标里也会被删。干跑里那 68 个 `*EXTRA File`
  是上一次部署留下的旧表面（`qgis_actions/`、`jobs.py`、`qgis_socket_bridge.py` 等），真部署会
  一并清掉。
- 排除 `__pycache__`、`.ruff_cache`。
- 目标目录如果是 junction/symlink，脚本直接报错退出，避免把源目录覆盖掉。
- 部署不会重启 QGIS，也不会启用插件。

**未实测**：真正的 `npm run deploy` 写盘与随后在 GUI 里启用插件——本机另一条验收线（F7）正在用
同一个 QGIS profile，写盘会互相干扰，所以 F8 只跑了干跑。命令与输出格式就是上面这段。

## 3. 启用与验证

1. 重启 QGIS（Python 插件在启动时扫描）。
2. Plugins → Manage and Install Plugins → Installed，勾上 “QGIS Agent”。
   `metadata.txt` 里 `experimental=True`，如果列表里看不到，勾上 “Show experimental plugins”。
3. Python Console 里验证：

```python
hasattr(iface, "agent_engine"), hasattr(iface, "agent_registry")     # (True, True)
len(iface.agent_registry.list_specs())                               # 9
iface.agent_engine.run_sync("project.get_info", {}).status           # 'SUCCEEDED'
```

实测（headless，同一套类手工装配，因为 headless 没有 `iface`）：

```text
specs: 9
run_sync project.get_info -> SUCCEEDED
submit layer.sum_numeric_field -> QUEUED -> SCHEDULED -> SUCCEEDED
```

禁用插件后这两个属性会被撤掉（`unload` 里 `delattr`），不会留一个已关闭的引擎在 Console 上。

## 4. 配置（PRD 10.1 的七项）

键名一律是 `qgis_agent_plugin/<键名>`（一个 QSettings 组，避免和别的插件撞名）。

| 键 | 默认 | 语义 |
| --- | --- | --- |
| `allowed_data_roots` | 空列表 | 允许 `layer.add` 读取的绝对目录。空 = 一个都不许 |
| `allowed_project_roots` | 空列表 | 允许 `project.open` / `project.save` 的绝对目录。空 = 一个都不许 |
| `max_active_background` | 2 | 同时运行的后台 `QgsTask` 数；超出的排队 |
| `max_pending` | 32 | 排队上限（主线程 + 后台合计），超出返回 `TOO_MANY_EXECUTIONS` |
| `max_terminal_records` | 200 | 终态记录保留条数 |
| `terminal_ttl_seconds` | 1800 | 终态记录保留秒数（先到先淘汰） |
| `max_snapshot_features` | 10000 | `layer.sum_numeric_field` 快照上限，超出 `SNAPSHOT_LIMIT_EXCEEDED` |

两种写法，二选一：

```python
# A. Python Console（写进当前 profile 的 QGIS3.ini）
from qgis.core import QgsSettings
s = QgsSettings()
s.setValue("qgis_agent_plugin/allowed_data_roots", [r"C:\temp\data"])
s.setValue("qgis_agent_plugin/allowed_project_roots", [r"C:\temp\projects"])
```

```text
B. 设置 → 选项 → 高级，搜索 qgis_agent_plugin，逐键填。
   列表键可以写成一条路径，也可以写成多条（QSettings 里存成数组）。
```

生效时机：`initGui` 读一次（`plugin.read_settings()`），所以**改完要在插件管理器里禁用再启用**
（或重启 QGIS）。

实测（只读，没有写用户 profile）：

```text
allowed_data_roots: () | allowed_project_roots: ()
max_active_background: 2 | max_pending: 32
raw QgsSettings value for a key nobody set: None
layer.add  -> FAILED PATH_NOT_ALLOWED
project.save -> FAILED PATH_NOT_ALLOWED
```

即：没配之前所有文件访问都被拒，这是默认安全姿态，不是配置缺失的 bug。键缺失/为 `None` 时
`Settings.from_mapping` 保留默认值；字符串形式的数字也会被转成 `int`。

**未实测**：用 `QgsSettings().setValue(...)` 真正写盘并重载插件（会改用户的 `QGIS3.ini`，
F8 不做；F7 的 GUI 验收清单里有这一条）。

## 5. 开发日常

| 命令 | 作用 | 实测结果（基 `74e7077`） |
| --- | --- | --- |
| `npm run test` | 用 `paths.json` 里的 QGIS python 跑 `pytest -q tests` | `343 passed` |
| `npm run test:verbose` | 同上，`-vv tests` | — |
| `npm run lint` | `ruff check .` | `All checks passed!` |
| `npm run format` | `ruff format .` | — |
| `npm run deploy` | 镜像到 QGIS 插件目录 | 干跑见 §2 |

测试不依赖 QGIS 运行环境：契约层、管线、操作和插件生命周期都能在**裸解释器**里跑（
`qgis`/`PyQt5` 的 import 都在函数体内，`tests/test_package.py` 在子进程里强制这条规则）。

```powershell
& 'D:\QGIS 3.44.13\apps\Python312\python.exe' -m pytest -q tests   # 343 passed
```

## 6. 排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| QGIS 启动时 “qgis-agent-plugin requires pydantic 2.x, but 1.x is installed” | 解释器里的 pydantic 不是 2.x | 用 QGIS 自带解释器；不要往里面装 pydantic 1 |
| 插件管理器里看不到 “QGIS Agent” | 部署没跑到 profile，或没显示实验性插件 | 重跑 `npm run deploy`，勾 “Show experimental plugins”，重启 QGIS |
| Console 里 `iface.agent_engine` 不存在 | 插件没启用，或已卸载 | 启用插件；看 Console/日志里的加载异常 |
| 所有文件操作都 `PATH_NOT_ALLOWED` | 两个允许目录是空的（默认） | 见 §4；确认路径是绝对路径 |
| `layer.set_visibility` 返回 `LAYER_NOT_IN_TREE` | 该图层是 `layer.add(add_to_legend=false)` 加进来的 | 用 `true` 重加，或只做 `layer.get_info` |
| `run_sync` 报 `UNSUPPORTED_EXECUTION_MODE` | 对 `layer.sum_numeric_field` 用了 `run_sync` | 用 `submit` + `get` |
| `project.create` / `project.open` 抛 `INTERNAL_ERROR ... CANCEL_REQUESTED -> SUCCEEDED` | 已知缺陷 F9 | 见 `docs/ERRORS.md` §5；等修复 |

## 7. 卸载

插件管理器里禁用即可从 `iface` 上撤掉会话，Python 侧的对象按 PRD 9.3 的六步收尾（活动任务
被请求取消，Supervisor 等最后一个任务真正终止后自删）。要彻底删除文件就删
`%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\qgis-agent-plugin`；或者删掉这个目标目录
再 `npm run deploy`（源不动，脚本会重建）。**不要靠“把 `src/qgis_agent_plugin` 清空再 deploy”来删
目标**：`deploy.ps1` 在 robocopy 之前就检查源里的 `metadata.txt`，源被清空时它直接报
`not a QGIS plugin package (metadata.txt missing)` 并 `exit 1`，镜像一行都不会跑。

## 8. 文档索引

| 文件 | 内容 |
| --- | --- |
| `docs/OPERATIONS.md` | 九个 Operation 的参数/返回/错误码契约 |
| `docs/ERRORS.md` | 25 个错误码、`cancel` 回执、已知缺陷 |
| `docs/CONSOLE.md` | Python Console 样例，标注实测状态 |
| `docs/qgis-sync-async.md` | QGIS 3.44 的线程模型，以及本插件怎么用它 |
| `docs/QGIS_3.44_Agent_Operation_Framework_MVP_PRD_v1.0.md` | 规格基线（PRD，不改） |
