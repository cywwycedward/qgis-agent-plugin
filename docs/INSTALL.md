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

插件启用后会多一个坞窗，标题 **QGIS Agent 操作**，停靠在主窗口右侧：`视图 → 面板` 里勾它，
或右键工具栏空白处。它给人工验证十七个 Operation 用，不属于 PRD 的框架面。

一次测试三步：**选操作 → 填必要的参数 → 运行并读懂结果**。界面自上而下：

| 区域 | 内容 |
| --- | --- |
| 空态 | 注册表为空时，下拉框显示“没有可用的操作”，结果区位置换成一段教学文字（为什么为空、先看哪里），不是一行灰字 |
| 选择器 | 一个下拉框，每项左边是 `name`，右边等宽小字是 `kind · strategy`；`destructive=true` 的操作在左侧带一个警示色点，选中后摘要行也会标“可破坏”。一个都没有时下拉框显示“没有可用的操作” |
| 摘要 | 选中操作的类型/策略一行（等宽小字），下面是 `description`（次要色，自动换行）。没有描述就不占行 |
| 参数 | 由 `input_schema` 生成，一字段一行：标签（次要色）+ 编辑器，必填的标签后面有小号“必填” |
| 动作条 | 主按钮“运行”（强调色，全坞窗唯一）、次按钮“取消”（仅后台调用运行中可点）、文字按钮“复制 JSON”（结果为空时禁用）、右侧状态胶囊 |
| 状态胶囊 | `就绪 / 运行中 / 完成 / 失败 / 已取消 / 无操作`，各有自己的颜色与文案；运行中下面还有一条进度条 |
| 错误区 | 只在失败时出现：等宽的 `CODE` + 一句中文“发生了什么” + 一句“接下来怎么办”。已知错误码的句子由坞窗给（引擎原文只在这个标签的鼠标悬停提示里）；映射不到的码才把引擎原文标成“引擎返回：…” |
| 结果 | 上方一行小字：运行中给 `进度 n%`，结束给 `耗时 12 ms`（取消过就追加回执）。状态词只在胶囊里出现一次；没有记录时这一行不占位。下面是只读、可选中、等宽的 `ExecutionRecord` JSON |

键盘：`Ctrl+Return`（或 `Ctrl+Enter`）运行；`Tab` 顺序是 选择器 → 各参数 → “运行”。
运行期间选择器与参数禁用（避免一次运行被另一个操作顶掉），后台调用可点“取消”。

两条容易误解的规则：

- **留空 = 用模型默认值**。文本/数字/JSON 字段清空就不提交，`schema` 里的 `default` 以灰色提示文字
  显示在空框里，所以“空着”看得见；下拉框第一项是 `默认（值）`，选中它同样不提交。两个例外：勾选框始终提交
  自己的状态（`add_to_legend` 的默认 `true` 会以“已勾选”出现）；数组/对象填 JSON 文本，解析失败时
  错误区给 `INVALID_INPUT` 且**不会**调用引擎。没有默认值的必填字段空着，标签后的“必填”是它唯一的标记（不在框里重复）。
- **默认配置下第一条错误就是 `PATH_NOT_ALLOWED`**。两个允许目录为空时，`layer.add`、
  `project.open`、`project.save` 都会被拒（见 §4），这不是坞窗的问题；错误区会直接提示去配
  `qgis_agent_plugin/allowed_data_roots`。

配色不写死：坞窗的全部颜色由 `ui/theme.py` 从 `QApplication.palette()` 推导（浅色与深色主题都成立，
正文/次要文字 ≥ 4.5:1，胶囊 ≥ 3:1，有测试逐对断言）。`src/qgis_agent_plugin/ui/` 里**一个十六进制
颜色都没有**，连 `theme.py` 也不写：全部由调色板角色算出。

`iface` 没有 `addDockWidget` 的宿主（例如只 fake 了接口的测试）会改用 `iface.mainWindow().addDockWidget(...)`；
连主窗口也没有就不建坞窗，会话其余部分照常；两处用的是同一个宿主对象，所以撤销也是对称的。
坞窗是 `QDockWidget`（`addDockWidget` 只收这个类型）。禁用插件时坞窗随 `unload` 一起撤掉（停轮询定时器、
`removeDockWidget`、`deleteLater`），重复启用/禁用不会留两个坞窗或两个定时器。

实测：选择器与表单生成、六种状态、`Ctrl+Return`、焦点顺序、两套调色板的对比度、两条执行路径、
轮询与取消、生命周期由 `tests/test_ui_dock.py` 在 `QT_QPA_PLATFORM=offscreen` 下覆盖（不需要 QGIS）。
**真 QGIS 里的外观与交互未实测**：offscreen 下没有字体库，截图要额外设 `QT_QPA_FONTDIR=C:\Windows\Fonts`，
所以截图只代表布局与配色，不代表真机上的字体渲染。

本机交接截图（`docs/delegate/` 已 gitignore，不入库）：

| 文件 | 内容 |
| --- | --- |
| `docs/delegate/F11-shots/light-360.png` | 浅色、宽 360：`layer.add` 的完整表单（`idle`） |
| `docs/delegate/F11-shots/light-460.png` | 浅色、宽 460：`PATH_NOT_ALLOWED` 失败态（错误区 + JSON） |
| `docs/delegate/F11-shots/dark-360.png` | 深色、宽 360：后台运行中（胶囊 + 42% 进度条 + 表单禁用） |
| `docs/delegate/F11-shots/dark-460.png` | 深色、宽 460：成功（胶囊 + 摘要 + JSON） |

## 4. 配置（PRD 10.1 的七项）

键名一律是 `qgis_agent_plugin/<键名>`（一个 QSettings 组，避免和别的插件撞名）。

| 键 | 默认 | 语义 |
| --- | --- | --- |
| `allowed_data_roots` | 空列表 | 允许 `layer.add` 读取的绝对目录，以及 `project.open` 检查工程里每层数据源用的目录（数据源可以是文件或目录）。空 = 一个都不许 |
| `allowed_project_roots` | 空列表 | 允许 `project.open` / `project.save` 的绝对目录。空 = 一个都不许。`project.open` **两个键都要配**：工程里的每层数据源还得在 `allowed_data_roots` 内，否则打开含图层的工程会 `PATH_NOT_ALLOWED`（且当前工程已被替换，见 `docs/OPERATIONS.md` §3） |
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

**未实测**：用 `QgsSettings().setValue(...)` 真正写盘并重载插件（会改用户的 `QGIS3.ini`，
F8 不做；F7 的 GUI 验收清单里有这一条）。

## 5. 开发日常

| 命令 | 作用 | 结果 |
| --- | --- | --- |
| `npm run test` | 用 `paths.json` 里的 QGIS python 跑 `pytest -q tests` | 全绿（当前输出见下） |
| `npm run test:verbose` | 同上，`-vv tests` | — |
| `npm run lint` | `ruff check .` | `All checks passed!` |
| `npm run format` | `ruff format .` | — |
| `npm run deploy` | 镜像到 QGIS 插件目录 | 干跑见 §2 |

全量用例**必须在 QGIS 解释器下跑**：需要真 `QgsApplication` 的用例不止一条（真信号、真
`QgsTaskManager`、真 provider 都在里面），跳过它们得到的绿色不算验收。裸解释器跑同一套会
**多三条 skip**（`tests/qgis_integration/test_claim_root.py` 整模块 `importorskip`、
`tests/test_engine.py` 里那条真信号的用例，以及 `tests/test_ui_dock.py` 整个模块——它要 offscreen 的 Qt），其余用例在裸解释器里也能跑（`qgis`/`PyQt5` 的
import 都在函数体内，`tests/test_package.py` 在子进程里强制这条规则）。所以别按条数判断“跑过了”，
按解释器判断。

实测一次（`fix/audit-a1` 时期，两个解释器各跑一遍；条数已过期，只看 pass/skip 的构成）：

```text
$ npm run test                                                       # QGIS 解释器
403 passed, 1 warning in 4.14s

$ & 'D:\QGIS 3.44.13\apps\Python312\python.exe' -m pytest -q tests   # 裸解释器
394 passed, 2 skipped in 2.10s
```

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

插件管理器里禁用即可从 `iface` 上撤掉会话，Python 侧的对象按 PRD 9.3 的六步收尾（活动任务
被请求取消，Supervisor 等最后一个任务真正终止后自删）。要彻底删除文件就删
`%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\qgis-agent-plugin`；或者删掉这个目标目录
再 `npm run deploy`（源不动，脚本会重建）。**不要靠“把 `src/qgis_agent_plugin` 清空再 deploy”来删
目标**：`deploy.ps1` 在 robocopy 之前就检查源里的 `metadata.txt`，源被清空时它直接报
`not a QGIS plugin package (metadata.txt missing)` 并 `exit 1`，镜像一行都不会跑。

## 8. 文档索引

| 文件 | 内容 |
| --- | --- |
| `docs/OPERATIONS.md` | 十七个 Operation 的契约（v2 的八个指向 PRD v2 §2–§7） |
| `docs/ERRORS.md` | 25 个错误码、`cancel` 回执、日志与排查 |
| `docs/CONSOLE.md` | Python Console 样例，标注实测状态 |
| `docs/qgis-sync-async.md` | QGIS 3.44 的线程模型，以及本插件怎么用它 |
| `docs/QGIS_3.44_Agent_Operation_Framework_MVP_PRD_v1.0.md` | 规格基线（PRD，不改） |
