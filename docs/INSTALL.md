# 开发安装

结论：三条命令就能在 QGIS Desktop 3.44 里装上本插件，再用两个配置键打开文件访问。

```powershell
cd <repo>; npm run deploy          # 把 src/qgis_agent_plugin 镜像到 QGIS 插件目录
npm run test                       # 在 QGIS 的 python 里跑 pytest
npm run lint
```

装好之后：插件管理器里启用 “QGIS Agent”；配置 `qgis_agent_plugin/allowed_data_roots` 与
`qgis_agent_plugin/allowed_project_roots`；Python Console 里用 `iface.agent_engine`
调用（样例见 `CONSOLE.md`）。**默认两个允许目录都是空的，即默认拒绝一切文件访问**。

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

**未实测**：真正的 `npm run deploy` 写盘与随后在 GUI 里启用插件——写盘会覆盖同一个 QGIS profile，
如果有别的验收跑正在用那个 profile 会互相干扰，所以这里只跑了干跑。命令与输出格式就是上面这段。

## 3. 启用与验证

1. 重启 QGIS（Python 插件在启动时扫描）。
2. Plugins → Manage and Install Plugins → Installed，勾上 “QGIS Agent”。
   `metadata.txt` 里 `experimental=True`，如果列表里看不到，勾上 “Show experimental plugins”。
3. Python Console 里验证：

```python
hasattr(iface, "agent_engine"), hasattr(iface, "agent_registry")  # (True, True)
len(iface.agent_registry.list_specs())  # 17
iface.agent_engine.run_sync("project.get_info", {}).status  # 'SUCCEEDED'
```

实测（headless，同一套类手工装配，因为 headless 没有 `iface`）：

```text
specs: 17
run_sync project.get_info -> SUCCEEDED
submit layer.sum_numeric_field -> QUEUED -> SCHEDULED -> SUCCEEDED
```

禁用插件后这两个属性会被撤掉（`unload` 里 `delattr`），不会留一个已关闭的引擎在 Console 上。

### 操作坞窗（手工测试入口）

插件启用后多一个坞窗，标题 **QGIS Agent 操作**，停靠在主窗口右侧（视图 → 面板里勾它）。给人工
验证十七个 Operation 用，不属于框架面；控件、状态与两条属于坞窗的合同见 `qgis\DEVELOP.md` §10。

一次测试三步：**选操作 → 填必要的参数 → 运行并读懂结果**。留空表示不提交、用模型默认值；
`Ctrl+Return` 运行；后台操作运行期间可点“取消”。失败时错误区给 `CODE` + “发生了什么 +
接下来怎么办”，下面是 `ExecutionRecord` 的 JSON。

**默认配置下第一条错误就是 `PATH_NOT_ALLOWED`**：两个允许目录为空时，`layer.add`、
`project.open`、`project.save` 都会被拒（见 §4），这不是坞窗的问题。

## 4. 配置（七项）

键名一律是 `qgis_agent_plugin/<键名>`（一个 QSettings 组，避免和别的插件撞名）。七个键的语义、
默认值与"计数必须 ≥ 1"的规则只有一份，在 `qgis\DEVELOP.md` §6.12。这里只说怎么写、怎么生效。

`project.open` **两个键都要配**：工程文件用 `allowed_project_roots`，工程里每层数据源用
`allowed_data_roots`，否则打开含图层的工程会 `PATH_NOT_ALLOWED`（且当前工程已被替换，见
`qgis\DEVELOP.md` §7.2）。

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

五个计数键（`max_active_background`、`max_pending`、`max_terminal_records`、
`terminal_ttl_seconds`、`max_snapshot_features`）必须 **≥ 1**：写 `0` 或负数、或者写成
`32 (default)` 这类非数字，`initGui` 会直接报一个带键名（非法值时也带原值）的错误，插件
不加载。故意不夹取：`max_active_background=0` 会让每个后台调用永久 `QUEUED`，这种配置
失误必须当场暴露。

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

**未实测**：用 `QgsSettings().setValue(...)` 真正写盘并重载插件（会改用户的 `QGIS3.ini`，本条只
验证了读：默认空根、缺失键返回 `None`、越界值拒绝）。

## 5. 开发日常

| 命令 | 作用 | 结果 |
| --- | --- | --- |
| `npm run test` | 用 `paths.json` 里的 QGIS python 跑 `pytest -q tests` | 本机最近一次 1 failed / 774 passed，见下 |
| `npm run test:verbose` | 同上，`-vv tests` | — |
| `npm run lint` | `ruff check .` | `All checks passed!` |
| `npm run format` | `ruff format .` | — |
| `npm run deploy` | 镜像到 QGIS 插件目录 | 干跑见 §2 |

全量用例**必须在 QGIS 解释器下跑**：需要真 `QgsApplication` 的用例不止一条（真信号、真
`QgsTaskManager`、真 provider 都在里面），跳过它们得到的绿色不算验收。裸解释器跑同一套会
**多三条 skip**（`tests/qgis_integration/test_claim_root.py` 整模块 `importorskip`、
`tests/test_engine.py` 里那条真信号的用例，以及 `tests/test_ui_dock.py` 整个模块——它要 offscreen
的 Qt），其余用例在裸解释器里也能跑（`qgis`/`PyQt5` 的 import 都在函数体内，`tests/test_package.py`
在子进程里强制这条规则）。所以别按条数判断"跑过了"，按解释器判断。

本机最近一次（QGIS 3.44.13，`fix/drop-pydantic-major` 的文档收敛轮）：

```text
$ npm run test                                                       # QGIS 解释器
1 failed, 774 passed, 1 warning in 164.65s
```

失败的是 `tests/test_operations_project.py::test_a_qgz_with_too_many_members_is_refused_before_any_is_unpacked`：
它断言 ".qgz 成员太多要在 1.5 s 内被拒"，本机实测 3.39 s。那是性能敏感的时限断言（磁盘/病毒扫描
快慢决定），不是逻辑错误，也不是这两层测试的问题；调阈值或放宽到有序性断言是测试侧的事。

## 6. 排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| QGIS 启动时 “qgis-agent-plugin requires pydantic 2.x, but 1.x is installed” | 解释器里的 pydantic 不是 2.x | 用 QGIS 自带解释器；不要往里面装 pydantic 1 |
| 插件管理器里看不到 “QGIS Agent” | 部署没跑到 profile，或没显示实验性插件 | 重跑 `npm run deploy`，勾 “Show experimental plugins”，重启 QGIS |
| Console 里 `iface.agent_engine` 不存在 | 插件没启用，或已卸载 | 启用插件；看 Console/日志里的加载异常 |
| 所有文件操作都 `PATH_NOT_ALLOWED` | 两个允许目录是空的（默认） | 见 §4；确认路径是绝对路径 |
| 插件加载失败，日志里是 `max_active_background must be at least 1` 或 `… must be a whole number` | 某个计数键写成了 0/负数/非数字 | 按报错里的键名改回 ≥ 1 的整数；见 §4 |
| `layer.set_visibility` 返回 `LAYER_NOT_IN_TREE` | 该图层是 `layer.add(add_to_legend=false)` 加进来的 | 用 `true` 重加，或只做 `layer.get_info` |
| `run_sync` 报 `UNSUPPORTED_EXECUTION_MODE` | 对 `layer.sum_numeric_field` 用了 `run_sync` | 用 `submit` + `get` |

## 7. 卸载

插件管理器里禁用即可从 `iface` 上撤掉会话：坞窗先卸，然后引擎拒绝新请求、取消没开始的调用、
请本插件的后台任务停止，最后一个任务真正终止后 Supervisor 在主线程自删（没有一步阻塞等待，
也从不全局 `cancelAll()`）。要彻底删除文件就删
`%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\qgis-agent-plugin`；或者删掉这个目标目录
再 `npm run deploy`（源不动，脚本会重建）。**不要靠“把 `src/qgis_agent_plugin` 清空再 deploy”来删
目标**：`deploy.ps1` 在 robocopy 之前就检查源里的 `metadata.txt`，源被清空时它直接报
`not a QGIS plugin package (metadata.txt missing)` 并 `exit 1`，镜像一行都不会跑。

## 8. 文档索引

| 文件 | 内容 |
| --- | --- |
| `qgis\DEVELOP.md` | 唯一的开发规范：架构、协议、17 个 Operation 的契约、错误码、注释规范、Git 流程、已知边界 |
| `qgis\TEST.md` | 两层测试怎么跑、假对象写法、坞窗与裸解释器守卫、验收跑的退出码 |
| `qgis\ENVIRONMENT.md` | 两个解释器、依赖现状、uv、路径速查 |
| `CONSOLE.md` | Python Console 样例，标注实测状态 |
| `docs/agents/` | 委派、issue tracker、triage 标签、领域文档的 agent 规则 |
