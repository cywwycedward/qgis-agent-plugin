# QGIS Agent 插件开发指南

本文档面向要修改或扩展 `qgis-agent-plugin` 的开发者，内容自包含：环境、目录、代码规范、
基类与协议、Operation 的写法、测试方式、提交规则都在这里给出，不需要再查其它文件。

文中路径默认相对仓库根目录（即包含 `package.json` 的 `qgis-agent-plugin` 目录）。命令都是
PowerShell。代码标识符保持英文原文，因为它们就是代码里的名字。

本文件住在 `docs/agents/`，同目录的 `TEST.md`、`ENVIRONMENT.md`、`DELEGATION.md` 是它的三份配套文档。

---

## 1. 一分钟概览

这是一个 QGIS Desktop 插件，同时是一个**进程内**的操作框架。它不做网络服务，不起端口，
没有远程调用方：所有能力都通过 Python 对象在 QGIS 主线程上被调用。

三个核心概念：

| 概念 | 是什么 | 代码位置 |
| --- | --- | --- |
| Operation | 一个具名能力：输入模型、输出模型、跑在哪个线程、是不是破坏性 | `src/qgis_agent_plugin/operations/` |
| Engine | 唯一的执行入口：校验、排队、调度、状态机、取消、记录仓库 | `src/qgis_agent_plugin/core/engine.py` |
| ExecutionRecord | 一次调用的全部可观察状态：id、状态、进度、结果、错误码 | `src/qgis_agent_plugin/core/models.py` |

插件加载后，引擎与注册表挂在 `iface` 上，Python Console 里直接用：

```python
iface.agent_registry.list_specs()  # 十七个 OperationSpec
iface.agent_engine.run_sync("project.get_info", {})
iface.agent_engine.submit("layer.sum_numeric_field", {"layer_id": "...", "field_name": "pop"})
```

两种执行方式，不是两种"是否异步"：

| 方式 | 适用 | 行为 |
| --- | --- | --- |
| `run_sync(name, arguments)` | 仅 `main_thread` 策略 | 在当前调用栈上立即执行，返回终态记录 |
| `submit(name, arguments)` | 两种策略都可以 | 返回 `QUEUED` 快照（被拒时返回 `FAILED` 记录）；主线程操作在下一个事件循环轮次执行，后台操作交给 `QgsTask` |

查询与取消：`get(execution_id)`、`cancel(execution_id)`、`shutdown()`。五个方法都只能在创建
引擎的那个线程（QGIS 主线程）调用。

本版本注册十七个 Operation：四个 `project.*`、十个 `layer.*`、三个 `processing.*`。
逐项的完整契约在 §7.6。

| Operation | kind | strategy | destructive | 必需参数 |
| --- | --- | --- | --- | --- |
| `project.create` | command | main_thread | 是 | 无 |
| `project.open` | command | main_thread | 是 | `path` |
| `project.save` | command | main_thread | 是 | 无 |
| `project.get_info` | query | main_thread | 否 | 无 |
| `layer.add` | command | main_thread | 否 | `kind`、`source_path`、`name` |
| `layer.list` | query | main_thread | 否 | 无 |
| `layer.get_info` | query | main_thread | 否 | `layer_id` |
| `layer.set_visibility` | command | main_thread | 否 | `layer_id`、`visible` |
| `layer.sum_numeric_field` | query | background | 否 | `layer_id`、`field_name` |
| `layer.list_style_categories` | query | main_thread | 否 | `layer_id` |
| `layer.get_qml` | query | main_thread | 否 | `layer_id` |
| `layer.list_available_renderers` | query | main_thread | 否 | `layer_id` |
| `layer.get_renderer` | query | main_thread | 否 | `layer_id` |
| `layer.set_renderer` | command | main_thread | 是 | `layer_id`、`renderer` |
| `processing.list_algorithms` | query | main_thread | 否 | 无 |
| `processing.get_algorithm_info` | query | main_thread | 否 | `algorithm_id` |
| `processing.run_algorithm` | command | main_thread | 是 | `algorithm_id`、`parameters` |

`run_sync` 只接受 16 个主线程 Operation：唯一的后台 Operation `layer.sum_numeric_field` 必须
`submit`，对它调 `run_sync` 会被拒绝 `UNSUPPORTED_EXECUTION_MODE`（控制面异常，不建记录）。

---

## 2. 运行环境与常用命令

### 2.1 运行时

解释器、依赖清单、uv 的用法与路径速查都在 `ENVIRONMENT.md`。改代码时只需要记住两条：

- 插件只能在 QGIS 自带的解释器里跑（`bin\python-qgis-ltr.bat`，Python 3.12），`pydantic` 必须是
  2.x；裸解释器的 `apps\Python312\python.exe` 能跑 lint 与不碰 QGIS 的单元测试，跑不了插件。
- 依赖装进 QGIS 全局 `site-packages`，安装前必须得到用户批准；不要升级 `PyQt5`、`osgeo`、
  `qgis` 或任何含二进制组件的包。`paths.json` 是解释器与插件目录的唯一记录处。

### 2.2 命令

```powershell
npm run lint          # ruff check .
npm run lint:fix      # ruff check --fix .
npm run format        # ruff format .
npm run test          # = npm run test:unit
npm run test:unit     # scripts\pytest.ps1，默认 "-q tests"
npm run test:verbose  # scripts\pytest.ps1 -vv tests
npm run deploy        # scripts\deploy.ps1，镜像 src\qgis_agent_plugin 到 profile
npm run deploy:list   # 只列会拷贝什么，不落盘
```

`scripts\pytest.ps1` 做的唯一一件事：从 `paths.json` 读出 QGIS 解释器，`Push-Location` 到插件
根目录，把剩余参数转给 `-m pytest`，用 pytest 的退出码退出。所以也可以直接跑：

```powershell
& 'D:\QGIS 3.44.13\bin\python-qgis-ltr.bat' -m pytest -q tests
& 'D:\QGIS 3.44.13\bin\python-qgis-ltr.bat' -m pytest -q tests\test_engine.py -k cancel
```

真实 QGIS 的验收跑（headless `QgsApplication`，自己往 `sys.path` 里插 `src`）：

```powershell
& 'D:\QGIS 3.44.13\bin\python-qgis-ltr.bat' tests\qgis_integration\run_acceptance.py
```

它接受一个可选的工作目录参数；不给就建一个临时目录，测试数据与临时 profile 都在里面，跑完
打印 `PASS` / `FAIL` / `SKIP` 行与汇总，退出码 0 表示无失败、1 表示有失败、2 表示拒绝使用给定
的工作目录。跳过不算通过。

`npm run deploy` 的规则（脚本已经实现，改脚本时要守住）：

- 源目录必须有 `metadata.txt`，否则直接 `exit 1`，不做镜像。
- 目标若是 junction/symlink，拒绝写入并 `exit 1`——镜像永远不能顺着链接写回源码树。
- `robocopy /MIR`，排除 `__pycache__`、`.ruff_cache`；robocopy 的 `1` 是"复制成功"，只有 `>= 8` 才当失败。
- 部署是镜像：目标里源没有的文件会被删掉。部署完要重启 QGIS。

### 2.3 改动之后的检查

单测分两组，跑法不同：

| 组 | 断的是什么 | 什么时候跑 | 命令 | 条数与耗时 |
| --- | --- | --- | --- | --- |
| **规范组**（`conformance`） | 整个仓库的成文规则：注释双语、导入位置、文档索引、注册面 | **每次改完代码都跑** | `npm run test:rules` | 12 条，约 2 s |
| **功能组** | 具体行为：契约、状态机、引擎、适配器、操作、坞窗 | 按需——改到那块逻辑，或合并前 | `npm run test` | 725 条，约 12 s |

一条测试属于规范组，当且仅当三件事都成立：它断言的是**对整个仓库都成立**的规则（不是某条行为）、
它**读源码或文档**而不是驱动代码、它**不需要 QGIS**。文件顶部写 `pytestmark = pytest.mark.conformance`
即入组（marker 已在 `pyproject.toml` 注册，`-m conformance` 就是这一组）。

**每次改完代码，跑这一条**（约 5 s，必须绿）：

```powershell
npm run check      # ruff check + ruff format --check + npm run test:rules
```

通过的判据（三行都要在输出里）：`All checks passed`、`N files already formatted`、
`12 passed, 713 deselected`。

**按需加跑**（碰了什么跑什么）：

| 改动 | 加跑 | 通过长什么样 |
| --- | --- | --- |
| `src/` 里任何逻辑 | `npm run test`（全量） | `725 passed` |
| 改了导入层、新增/移动模块 | 裸解释器 `& 'D:\QGIS 3.44.13\apps\Python312\python.exe' -m pytest -q tests` | `668 passed, 3 skipped`：多出的 `test_ui_dock.py` 与 `test_claim_root.py` 整模块、`test_engine.py` 一条真信号用例在裸解释器里 skip，属预期 |
| 碰了真实 PyQGIS 行为、耗时阈值、新增 Operation | 验收跑 `& 'D:\QGIS 3.44.13\bin\python-qgis-ltr.bat' tests\qgis_integration\run_acceptance.py` | `summary: PASS …, FAIL 0, SKIP n`，退出码 0 |
| 新增/移动模块 | 把模块名加进 `tests/test_package.py` 的导入列表——守卫只覆盖它列出的模块 | `test_package` 仍绿 |
| 新增 Operation | `tests/test_package.py` 的数量与名字断言、`metadata.txt` 的 description/about、本文档 §1 与 §7.6 的计数、`cases.py` 至少一条验收用例 | 规范组 + 验收跑都绿 |
| 改 `ui/` | `npm run test`（`test_ui_dock.py` 在裸解释器里整模块 skip，只有 QGIS 解释器能看见它） | `725 passed` |
| 改 `scripts/` | `npm run deploy:list` 干跑看清单，再 `npm run deploy`；部署是镜像，之后必须重启 QGIS | 清单不含 `__pycache__`，退出码 0 |
| 装依赖 | 先经用户批准（同一 QGIS 全局环境），装完同步 `ENVIRONMENT.md` 的清单 | — |

**不算通过的情形**（都踩过）：

- 裸解释器的绿色少三个用例，不能按条数比；**按解释器判断跑没跑过**。
- 验收跑退出码 `2` 是"拒绝使用给定的工作目录"，不是失败；换个空目录，或让脚本自己建。
- PowerShell 里 `$LASTEXITCODE` 会被管道上一条命令污染，以单独跑一次的输出为准。
- 机器忙时时间阈值会偶发红（`AT-40` / `AT-43` 那两条 20 s 的上限），先单跑那条，再判断是不是负载。
- "应该能过"不算结论：报告里贴实际输出，红的就是红的。

### 2.4 工具配置

| 文件 | 作用 |
| --- | --- |
| `paths.json` | QGIS 解释器路径、插件目标目录、包名与安装名 |
| `package.json` | npm script 入口（lint/format/test/deploy） |
| `pyproject.toml` | pytest（`testpaths=["tests"]`、`pythonpath=["src"]`）与 ruff 配置 |
| `.editorconfig` | 编码、换行、缩进 |
| `metadata.txt` | QGIS 插件管理器读的元数据（`src/qgis_agent_plugin/metadata.txt`，`qgisMinimumVersion=3.44`，`experimental=True`） |

---

## 3. 文件结构

### 3.1 目录树

```text
qgis-agent-plugin/
├── package.json                  npm script 入口
├── paths.json                    QGIS 解释器 / 插件目录 / 包名
├── pyproject.toml                pytest 与 ruff 配置（无 build backend）
├── .editorconfig                 编码与缩进规则
├── .gitattributes / .gitignore
├── .agents/ 与 skills-lock.json    代理工具的技能清单，不属于插件代码
├── AGENTS.md                     给编码代理的目录与分层索引（本文档自包含，不依赖它）
├── scripts/
│   ├── deploy.ps1                镜像 src/<包> 到 QGIS profile
│   └── pytest.ps1                用 QGIS 解释器跑 pytest
├── docs/                         面向操作者的说明与开发/协作规范
│   ├── INSTALL.md                安装、配置、排错、卸载
│   ├── CONSOLE.md                Python Console 的实测样例
│   ├── audit/                    生成的审计页面
│   └── agents/                   规范文档（本目录）
│       ├── DEVELOP.md            本文档：唯一的开发规范
│       ├── TEST.md               测试规范：两层测试、假对象、守卫、验收跑
│       ├── ENVIRONMENT.md        运行环境：两个解释器、依赖、uv、路径速查
│       ├── DELEGATION.md         怎么把活派给别的 agent、五步流程
│       └── domain.md / issue-tracker.md / triage-labels.md   领域文档、issue 与标签规则
├── src/qgis_agent_plugin/        插件包本体：这个目录就是 QGIS 看到的东西
│   ├── __init__.py               classFactory（QGIS 按名字找的入口）
│   ├── metadata.txt              插件元数据（必须与包根同级）
│   ├── plugin.py                 会话：initGui / unload / Supervisor / 设置读取
│   ├── core/
│   │   ├── __init__.py           只有 docstring
│   │   ├── models.py             错误码、枚举、FrameworkError、ExecutionRecord、OperationSpec、Params
│   │   ├── contracts.py          BaseOperation/BaseCommand/BaseQuery、两个策略 mixin、OperationContext、TaskControl、validate_operation
│   │   ├── registry.py           OperationRegistry：注册、按名查找、spec 导出
│   │   ├── engine.py             OperationEngine：run_sync/submit/get/cancel/shutdown/on_project_replaced
│   │   ├── execution_store.py    ExecutionStore：记录快照、状态机、保留策略
│   │   ├── context.py            ProjectLifecycle（epoch）与 require_main_thread
│   │   ├── config.py             Settings（四项配置）与 from_mapping
│   │   ├── security.py           absolute_path（只要求绝对路径）
│   │   └── runners/
│   │       ├── __init__.py       只有 docstring
│   │       ├── main_thread.py    MainThreadRunner：把回调投到事件循环，跳过已终止的执行
│   │       └── qgs_task.py       OperationTaskRunner：QgsTask 适配器（两并发、进度、取消、完成）
│   ├── operations/
│   │   ├── __init__.py           PROJECT_/LAYER_/PROCESSING_OPERATIONS、OPERATIONS、register_all
│   │   ├── project.py            四个 project.* 操作及其模型与深度守卫
│   │   ├── layer.py              十个 layer.* 操作及其模型、QML 与 renderer 构建
│   │   └── processing.py         三个 processing.* 操作及其 JSON 边界
│   └── ui/
│       ├── __init__.py           只有 docstring（保持空壳，裸解释器不会顺着它碰到 Qt）
│       ├── dock.py               OperationDock：按 input_schema 生成表单、运行、轮询、三块记录
│       └── theme.py              唯一配色来源：从 QPalette 派生颜色、对比度自检、生成 QSS
└── tests/
    ├── test_contracts.py         协议：策略与阶段声明、校验拒绝
    ├── test_models.py            记录状态组合、错误码集合、输入模型规则
    ├── test_registry.py          名字格式、重复注册、spec 快照不可编辑
    ├── test_security.py          绝对路径规则与 Settings 计数
    ├── test_context.py           epoch 与线程判定
    ├── test_execution_store.py   快照、状态机、保留与淘汰
    ├── test_engine.py            控制面异常 vs FAILED 记录、排队、取消、epoch、容量
    ├── test_operations_project.py  project.* 对着假 ctx / 假 PyQGIS
    ├── test_operations_layer.py    layer.* 对着假 ctx / 假 PyQGIS
    ├── test_operations_processing.py  processing.* 的 JSON 边界与反馈记录
    ├── test_qgs_task.py          适配器记账：两槽、task id、100 ms 进度窗、提交被拒
    ├── test_background.py        后台端到端：快照、取消、换代、卸载（假任务）
    ├── test_plugin_lifecycle.py  会话装配、信号连接、epoch、Supervisor、重复加载
    ├── test_ui_dock.py           坞窗：选择器、表单、状态、三块记录、配色对比度、入口、两条引擎路径
    ├── test_package.py           [规范组] 裸解释器导入守卫 + 十七个 Operation 注册 + dock 源码守则
    ├── test_comments.py          [规范组] 注释与 docstring：双语、不引用文档、shortcut 标记存活
    ├── test_source_rules.py      [规范组] 导入位置、__future__、日志器名、主题不写十六进制颜色
    ├── test_docs.py              [规范组] 文档索引与相对链接：每份规范都被索引、没指向已删文档
    └── qgis_integration/         真实 QGIS 验收跑（非 pytest 用例）
        ├── run_acceptance.py     入口：临时目录、临时 profile、打印逐条结果与汇总
        ├── cases.py              每条验收用例（返回一行证据，失败抛 AssertionError）
        ├── fixtures.py           用 PyQGIS 现场生成 GeoPackage/GeoTIFF/QGZ/损坏文件等
        ├── harness.py            headless QgsApplication、真实 QgsProject/QgsTaskManager、探针操作
        ├── tally.py              从判定字段统计 PASS/FAIL/SKIP，不从文本里数
        ├── probe_background_queue.py  后台排队卡死的复现脚本
        ├── test_claim_root.py    工作目录守卫（唯一会删目录的代码）
        └── test_tally.py         汇总只认判定字段
```

### 3.2 各模块的公开面

新增代码时优先复用这些入口，不要绕过它们直接摸 QGIS 全局单例。

| 模块 | 公开名字 | 说明 |
| --- | --- | --- |
| `core/models.py` | 23 个错误码常量、`ERROR_CODES` | 见 §7.4 的完整表 |
| | `OperationKind`、`ExecutionStrategy`、`ExecutionStatus` | 三个 `StrEnum` |
| | `FrameworkError(code, message)` | 控制面异常；构造时校验 code 已知 |
| | `ErrorDetail`、`CancelReceipt`、`ExecutionRecord`、`OperationSpec` | Pydantic DTO，全部 `extra="forbid"` |
| | `Params`、`Result` | 输入/输出模型的基类，都 `extra="forbid"`；`Params` 另加 `revalidate_instances="always"` |
| | `utc_now()` | 记录时间戳的唯一格式：ISO 8601 + UTC + 偏移 |
| `core/contracts.py` | `BaseOperation`、`BaseCommand`、`BaseQuery` | 声明面 |
| | `MainThreadOperationMixin`、`BackgroundOperationMixin` | 策略面 |
| | `OperationContext`（frozen dataclass） | `iface`、`project`、`lifecycle`、`policy` |
| | `TaskControl`（Protocol） | `is_cancelled()`、`set_progress(value)` |
| | `validate_operation(operation)` | 注册时读一次声明，不合法就拒 |
| `core/registry.py` | `OperationRegistry` | `register` / `get` / `get_spec` / `list_specs` |
| `core/engine.py` | `OperationEngine` | 五个调用方法 + `on_project_replaced` |
| | `BackgroundRunner`、`BackgroundCanceller` | 后台适配器的两个注入点（类型别名） |
| `core/execution_store.py` | `ExecutionStore`、`TERMINAL_STATUSES` | 唯一的记录写入者 |
| `core/context.py` | `ProjectLifecycle`、`require_main_thread` | epoch 与线程 |
| `core/config.py` | `Settings` | 四项配置的 frozen dataclass |
| `core/security.py` | `absolute_path` | 路径参数的唯一规则：必须是绝对路径 |
| `core/runners/main_thread.py` | `MainThreadRunner` | `post(execution_id, callback)` |
| `core/runners/qgs_task.py` | `OperationTaskRunner`、`task_class()` | 后台适配器与 `QgsTask` 子类 |
| `operations/__init__.py` | `register_all(registry)`、`OPERATIONS` | 唯一的注册入口 |
| `operations/processing.py` | `PROCESSING_OPERATIONS` | 三个 `processing.*` 的清单元组（唯一自带元组的模块） |
| `plugin.py` | `Plugin`、`Supervisor`、`application_supervisor`、`read_settings` | 生命周期 |
| `ui/dock.py` | `OperationDock` | 手工测试坞窗（`QDockWidget` 子类） |
| `ui/theme.py` | `Theme`、`build(palette)`、`contrast`、`readable` | 配色与对比度 |

---

## 4. 分层与导入规则

### 4.1 三层，箭头单向

```text
契约与管线            core/contracts.py core/models.py core/registry.py core/engine.py
（不碰 QGIS 业务）     core/execution_store.py core/context.py core/config.py core/security.py
        ↑              core/runners/（main_thread 与 qgs_task）
        │
操作                  operations/project.py operations/layer.py
        ↑
        │
入口                  __init__.py（classFactory） → plugin.py → ui/dock.py
```

规则：

1. 上层可以 import 下层，下层不得 import 上层。`core/*` 不认识任何具体 Operation，`plugin.py`
   不按名字引用任何 Operation（它只调用 `register_all`）。
2. 一个能力族一个文件：`project.*` 在 `operations/project.py`，`layer.*` 在
   `operations/layer.py`。领域模型跟着领域走（`LayerSummary` 在 layer 模块），只有被两个以上
   能力共享、又不属于任何领域的规则才进 `core/`（工程代数、线程判定）。
3. 框架级 DTO、状态枚举、错误码只在 `core/models.py`。QGIS/Qt 对象永远不进 DTO。
4. `core/contracts.py` 与 `core/engine.py` 的公开签名是冻结面：主线程只有一个阶段
   `execute_main`，后台只有 `prepare_main` / `execute_worker` / `finalize_main` 三个阶段，
   执行策略只有两个（`main_thread`、`background`）。加第四个阶段或第三个策略必须先改设计，
   不能以"顺手加个钩子"的方式绕过。

### 4.2 裸解释器规则（硬约束）

`core/*`、`operations/*`、`plugin.py`、`ui/theme.py` 必须能在**没有 QGIS、没有 Qt** 的裸
Python 里 import。`tests/test_package.py` 在子进程里强制这条规则：它导入整包、全部 core 模块、
operations、plugin 与 ui 包（`ui/__init__.py` 是空壳，所以 `ui.theme` 不在它的导入列表里），
注册十七个 Operation，然后断言 `sys.modules` 里没有 `qgis`、`PyQt5`、`PyQt6`、`PySide2`、
`PySide6`。守卫只覆盖它列出的模块：新增模块时要把它加进那份导入列表，否则红灯不会亮。

因此：

| 位置 | QGIS 可以出现在 | Qt 可以出现在 |
| --- | --- | --- |
| `core/*`（runners 以外） | 只在函数体内 | 只在函数体内（实际上没有） |
| `core/runners/*` | 只在函数体内（`qgs_task.task_class()`、`main_thread._post_to_event_queue`） | 模块级也允许，但目前只在函数体内用 |
| `operations/*` | 只在函数体内 | 只在函数体内（三处：QML 文档、类别去重的 `QVariant`、栅格颜色的 `QColor`） |
| `plugin.py` | 只在函数体内 | 模块级允许，但目前只在函数体内用 |
| `ui/dock.py` | 不得 import `qgis.core`、`qgis.gui` 或任何其它 qgis 子模块 | 模块级允许，但只从 `qgis.PyQt` 取（`test_package.py` 的正则就是这条：`\b(?:import|from)\s+qgis\b(?!\.PyQt)`） |
| `ui/theme.py` | 不允许 | 只在函数体内 |
| `ui/__init__.py` | 不允许 | 不允许，且保持空壳（只有 docstring） |

三条推论：

- `ui/dock.py` 只认识注册表、引擎和 Qt，所以它不能 import qgis；`tests/test_package.py` 直接读
  这个文件的源码来守这条规则（在 QGIS 解释器里查 `sys.modules` 不可靠——import `qgis.PyQt`
  本身就会把 `qgis.core`、`qgis.gui` 带进来）。
- 结果与错误只出 Pydantic DTO / 基本类型；绝不返回 `QgsMapLayer`、`QgsProject`、QObject、
  Qt 枚举或 traceback。
- 新增模块时，先把它加进 `tests/test_package.py` 的导入列表；否则即使模块级写了
  `from qgis.core import ...`，守卫也不会失败。这是设计好的红灯，不要绕过。

---

## 5. 代码规范

### 5.1 格式化与静态检查

`pyproject.toml` 的 ruff 配置就是唯一标准：

| 项 | 值 |
| --- | --- |
| 行宽 | 100 |
| 目标版本 | `py312`（可以用 PEP 695 泛型语法：`class BaseOperation[InputT: BaseModel, ...]`） |
| 启用的规则 | `E`（pycodestyle error）、`F`（pyflakes）、`W`（pycodestyle warning）、`I`（import 顺序）、`UP`（pyupgrade）、`B`（bugbear）、`SIM`（simplify）、`C4`（comprehension） |
| 故意不启用 | `D`（docstring 规则）、`ANN`（注解完整性）、`ARG`（未使用参数，Qt slot 必须保留签名）、`RUF`（会把中文全角标点报成 ambiguous） |

`pytest`、`pydantic` 与 `qgis` 装在本机的 QGIS 解释器里，静态分析器看不到，所以那几行 import 带
`# pyright: ignore[reportMissingImports]`；真正的验证是测试里跑真解释器，不是分析器的绿点。

门禁是一行：`npm run check`（= `lint` + `format:check` + `test:rules`，只跑规范组）。`ruff format --check` 单独
跑不覆盖 Markdown 里的 ```python 代码块以外的散文，但会格式化这些代码块，所以文档也算门禁。

`.editorconfig` 的规则：UTF-8、LF、文件末尾保留一个换行、行尾空格删除、4 空格缩进；
`*.{json,yml,yaml,toml}` 用 2 空格；`*.md` 不删行尾空格（Markdown 的硬换行）；`*.{bat,cmd}`
用 ASCII + CRLF（cmd.exe 对 UTF-8 注释和非 CRLF 都会解析错乱）。

### 5.2 写法约定

- 每个有代码的模块以 `from __future__ import annotations` 开头（跟在模块 docstring 后面；只有
  docstring 的模块不需要）。`tests/test_source_rules.py` 逐文件断言这条。
- `from __future__` 之后按 ruff 的顺序：标准库、第三方（`pydantic` 等）、本包（相对 import）。
- 类型注解写全；`Any` 只出现在 QGIS/Qt 边界、Qt slot 签名与 JSON 载荷（`dict[str, Any]`）上。
- 模块 docstring 写"为什么这样设计"，不写"这个模块是什么"的复述；公开类与函数一句话说明其
  契约。pydantic 模型通常不写 docstring（字段名与 `Field` 已经说明）。
- 注释只写代码本身表达不了的原因；一行的取舍写清"为什么"，不写"做了什么"。格式与语言见 §5.5。
- 面向用户/操作者的界面文字用中文（`ui/dock.py` 的字段标签、错误解释、状态文字）；代码里的
  标识符保持英文。
- 不可变优先：`OperationContext`、`Settings`、`FieldValues`、`FieldSum` 都是 frozen
  dataclass；`OperationRegistry` 与 `ExecutionStore` 交出的是深拷贝，调用方改不动内部状态。
- 记录里的 `error.message` 是给调用方看的：不带原始路径、provider URI，也不带异常原文。异常
  文本进日志（`log.exception`），进记录只有类型名（`"ValueError: unexpected failure"`）。
- 不写"以后可能用到"的参数、配置、抽象。没有第二个调用方的包装层不要建。

### 5.3 日志

- 模块级日志器只有一个写法：`_LOGGER = logging.getLogger(__name__)`，`tests/test_source_rules.py`
  断言名字与参数。会打日志的模块用它（目前是 `plugin.py`、`engine.py`、
  `execution_store.py`、`runners/main_thread.py`、`runners/qgs_task.py`；`operations/` 里只有
  `processing.py` 打，其余模块不打日志）。
- 状态迁移由 `ExecutionStore` 统一记 `INFO`：`execution <id> <op> <strategy> <from> -> <to>`
  并附耗时与错误码；不要在 Operation 里重复打印状态。
- Qt slot（`finished`、`progressChanged`、`aboutToBeCleared`、drain 回调）里的异常必须
  `try/except` 吃掉：核心模块用 `_LOGGER.exception(...)`，坞窗把失败变成屏幕上的文字
  （`_show_failure`/`_show_problem`）。异常逃进 PyQt slot 会让整个 QGIS 进程退出。

### 5.4 命名

| 对象 | 规则 | 例子 |
| --- | --- | --- |
| Operation 名字 | `<group>.<verb>`，两段都小写 snake，只允许一个点 | `layer.set_visibility` |
| Operation 类 | CamelCase，模块内以能力族开头 | `LayerSetVisibility`、`ProjectOpen` |
| 输入/结果模型 | `<操作类名>Params` / `<操作类名>Result` | `LayerAddParams`、`ProjectOpenResult` |
| 输出里的嵌套 DTO | 普通 `BaseModel`，自己写 `model_config = ConfigDict(extra="forbid")`；**不**继承 `Result` | `LayerSummary`、`ProcessingMessage` |
| 后台快照/中间结果 | 描述数据的名词 + `Values` / `Sum` 等 | `FieldValues`、`FieldSum` |
| 测试 | 文件 `test_<被测模块>.py`；用例名是一句描述行为的短句（下划线连接，不是固定的三段式） | `test_a_queued_call_cancelled_before_its_turn_never_runs`、`test_progress_rewrites_the_record_at_most_once_per_interval` |
| 常量 | 全大写；上限类常量带单位或含义 | `MAX_PROJECT_DEPTH`、`PROGRESS_INTERVAL_SECONDS` |

历史例外：无。`layer.sum_numeric_field` 的两个模型曾经没有 `Layer` 前缀，已改名成
`LayerSumNumericFieldParams` / `LayerSumNumericFieldResult`。

### 5.5 注释与 docstring

代码库里的注释与 docstring 一律中英双语，中文在前、英文在后。理由是读者有两类：不懂英文的
GIS 开发者，和不懂中文的下一个 agent；两种人都要一句话看懂某段代码为什么长这样。

1. 双语，中文在前：每条 `#` 注释与每个 docstring 都要有中文和英文两部分。
2. 说人话：中文一句说清"这段代码在做什么、为什么这样设计"；英文是同一句话的英文表达，
   不是另一种解释，也不是逐字机翻。
3. 只讲代码本身：不出现对文档的引用（`PRD`、`§`、`AT-数字`、`docs/`、任何 `.md` 文件名、
   "按设计"、"按报告"）。需要理由就用代码里的事实重写一遍，例如"取消是协作式的：只有任务
   自己的报告才会写 `CANCELLED`"。
4. 位置：块注释写在被说明的代码上方，中文行在前、英文行在后；字段、枚举成员、字典键这类
   标签型短注释可以留在行尾，写成 `# 中文 / English`，整行 ≤ 100 列。
5. `shortcut:` 标记保留（`ponytail-debt` 靠它检索），格式：
   `# shortcut: <中文限制>, <中文升级条件>` 换行 `# English: <English limit>, <English upgrade trigger>`。
6. docstring：第一段中文（契约或设计原因），第二段英文；模块 docstring 说"为什么这样设计"，
   不复述模块名。已有的准确内容不为了凑格式改写语义。
7. 宽度：任何一行 ≤ 100 列（`ruff check` 的 `E501`；`ruff format` 只管代码缩进，不管中文
   注释的长度）。
8. 不翻译：标识符、错误码、QGIS/Qt API 名（`QgsTask`、`project_epoch`、`INVALID_INPUT`）。

这七条不是约定而是断言：`tests/test_comments.py` 逐文件检查双语与来源引用，并把 `shortcut:`
标记当作必须存活的东西（`ponytail-debt` 靠它检索）。它只扫 `src/` 与 `tests/`，所以别把 Python
放进 `docs/`。
9. 术语统一：词汇与要避免的同义词以 `GLOSSARY.md` 为唯一来源，不在注释里另立说法；在它里查不到的词，要么换成它已有的词，要么先把它补上。
10. 删掉把代码再念一遍的注释（"初始化""设置""处理数据"）；但原因型注释不因为"读代码能
    看懂"就删。

### 5.6 行为准则

1. 先想再写（Think Before Coding）：不确定就说明假设、列出选项，不默默选一个；有更简单的
   做法要主动提；不清楚就停下来问。
2. 简单优先（Simplicity First）：只写解决问题需要的代码；不做没被要求的抽象、配置与灵活性；
   200 行能写成 50 行就重写。
3. 外科式改动（Surgical Changes）：只碰必须碰的行；不顺手"优化"相邻代码；发现无关的死代码
   只说、不删；自己造成的孤儿 import/变量要清掉。
4. 目标驱动执行（Goal-Driven Execution）：把任务变成可验证的目标（先写会红的测试，再让它
   绿）；多步任务先列计划与每步的验证方式。
5. 改动必须覆盖它的全部落点：调用方、测试、fixture、配置、导出的名字。新增 Operation 时
   `tests/test_package.py` 的数量与名字断言、`metadata.txt` 里的计数、本文档 §1 与 §7.6 的
   表格必须同步改，否则文档与代码开始互相矛盾。

---

## 6. 基类与协议设计

### 6.1 三个枚举

```python
class OperationKind(StrEnum):  # 业务意图
    COMMAND = "command"  # 会改变用户可见状态
    QUERY = "query"  # 只读；副作用只有缓存与日志


class ExecutionStrategy(StrEnum):  # 代码在哪里跑
    MAIN_THREAD = "main_thread"
    BACKGROUND = "background"


class ExecutionStatus(StrEnum):
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    FINALIZING = "FINALIZING"
    SUCCEEDED = "SUCCEEDED"  # 终态
    FAILED = "FAILED"  # 终态
    CANCELLED = "CANCELLED"  # 终态
```

`kind` 与 `strategy` 是两个独立的维度：`project.get_info` 是 query/main_thread，
`layer.sum_numeric_field` 是 query/background，两者都不是"同步/异步"的同义词。

### 6.2 声明面：`BaseOperation`

```python
class BaseOperation[InputT: BaseModel, OutputT: BaseModel](ABC):
    name: str
    description: str
    input_model: type[InputT]
    output_model: type[OutputT]
    kind: OperationKind  # BaseCommand 固定 COMMAND，BaseQuery 固定 QUERY
    strategy: ExecutionStrategy
    destructive: bool = False
```

- `BaseCommand`：`kind = COMMAND`，意图是改变状态。
- `BaseQuery`：`kind = QUERY`，意图是只读。
- `destructive`：只有 `project.create` / `project.open` / `project.save` 为 `True`。它不影响
  引擎行为，是给调用方看风险用的标记：true 意味着"可能丢掉当前工程或覆盖已有文件"。
- `description`：一行英文，会出现在 `OperationSpec`、`QgsTask` 的描述和坞窗的选择器里。

### 6.3 策略面：两个 mixin

```python
class MainThreadOperationMixin[InputT, OutputT](ABC):
    strategy = ExecutionStrategy.MAIN_THREAD

    @abstractmethod
    def execute_main(self, ctx: OperationContext, request: InputT) -> OutputT: ...


class BackgroundOperationMixin[InputT, OutputT, SnapshotT, WorkerT](ABC):
    strategy = ExecutionStrategy.BACKGROUND

    @abstractmethod
    def prepare_main(self, ctx: OperationContext, request: InputT) -> SnapshotT: ...

    @abstractmethod
    def execute_worker(self, snapshot: SnapshotT, control: TaskControl) -> WorkerT: ...

    @abstractmethod
    def finalize_main(self, ctx: OperationContext, request: InputT, result: WorkerT) -> OutputT: ...
```

后台操作可选的第四件事（不是协议的一部分，是适配器的约定）：

```python
def dependent_layer_ids(self, request: InputT) -> list[str]: ...
```

声明后，适配器把这些 id 解析成图层并调用 `QgsTask.setDependentLayers()`，图层被移除时 QGIS
自己取消任务。三个事件阶段是冻结面，依赖是"可选附加"，不是第四个阶段。

### 6.4 注册时的声明校验：`validate_operation`

`OperationRegistry.register()` 先检查名字格式，再调用 `validate_operation()`。任何一条不满足都
抛 `FrameworkError(INVALID_OPERATION_DEFINITION, ...)`，插件在加载时就失败——不拖到第一次调用。
消息有三种形状：名字格式不对是 `invalid operation name: <名字>`（注册表抛）；声明里的问题是
`<名字>: <原因>`（校验器抛）；传入的对象根本不是 `BaseOperation` 时是
`an operation must inherit BaseOperation`。

校验清单（逐条都要成立）：

1. 是 `BaseOperation` 的实例。
2. `name` 是非空 `str`；并且匹配 `^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$`（恰好一个点，两段都
   小写 snake，字母开头）。
3. `description` 是非空的、去掉空白后不为空的字符串。
4. `input_model` 与 `output_model` 都是 `BaseModel` 的子类。
5. `input_model` 还必须是 `Params` 的子类（`extra="forbid"` 与实例重校验的来源；只满足第 4 条仍会
   被拒：`<名字>: input_model must inherit Params`）。
6. `destructive` 是 `bool`（不是 `"true"`、不是 `1`）。
7. 恰好属于 `BaseCommand` 与 `BaseQuery` 之一（不能都是，也不能都不是），且 `kind` 与之一致。
8. `strategy` 必须是两个枚举值之一：
   - `main_thread`：`execute_main` 可调用，且三个后台阶段不能同时都可调用（三个都在才判为
     "同时声明了两种策略"）；
   - `background`：三个后台阶段全部可调用，且**不能**定义 `execute_main`。

第 8 条用 `getattr(...)` 判断，所以名字本身就是声明：一个类只要同时具备 `execute_main` 与三个
后台阶段，注册就被拒，不看你有没有真的去调用它们。

重复注册同一个名字抛 `FrameworkError(DUPLICATE_OPERATION, ...)`，原注册不被覆盖。

### 6.5 运行上下文与数据契约

```python
@dataclass(frozen=True)
class OperationContext:
    iface: Any  # QGIS 的 iface，或验收用的替身；headless 里可能是 None
    project: Any  # 当前 QgsProject 实例；headless 里是替身
    lifecycle: Any  # ProjectLifecycle 实例（工程代数）；契约层只透传，不依赖它的具体类型
    policy: Settings  # 四项配置（frozen）
```

`OperationContext` 只交给主线程阶段，**永远不进 worker**。

`TaskControl`（Protocol，worker 唯一的对外能力）：

```python
def is_cancelled(self) -> bool: ...
def set_progress(self, value: float) -> None: ...  # 0–100，适配器负责 clamp
```

`Params` 是所有输入模型的基类，做两件事：`model_config = ConfigDict(extra="forbid",
revalidate_instances="always")`——拒未知键，并且传进来的已构造实例永远重新校验一遍（调用方可能
自己造了模型再改字段，不能信任）。字段的严格性由字段自己声明：`StrictBool`、`StrictStr`、
`Field(min_length=...)` 等。所以：

- 多写一个键不是被忽略，而是 `FAILED(INVALID_INPUT)`，消息里带出错字段名；
- `"true"`、`1` 不能当布尔用；数字不能当字符串用。

`OperationSpec` 是同一份契约的机器可读版本：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` / `description` | str | 注册时的声明 |
| `kind` / `strategy` / `destructive` | 枚举 / bool | 同上 |
| `input_schema` / `output_schema` | dict | Pydantic 生成的 JSON Schema |

`registry.get_spec()` 返回深拷贝，`list_specs()` 按名字排序，所以接口面稳定、可比较。

### 6.6 记录与错误

`ExecutionRecord` 的字段：

| 字段 | 说明 |
| --- | --- |
| `execution_id` | UUID 字符串，`submit`/`run_sync` 立即分配 |
| `operation` | 注册名 |
| `kind` / `strategy` | 来自 Operation 声明 |
| `status` | 九个状态之一 |
| `progress` | 0–100；`SUCCEEDED` 必须恰好 100 |
| `created_at` / `started_at` / `finished_at` | `utc_now()` 的 ISO 8601 字符串；非终态没有 `finished_at` |
| `project_epoch` | 接受这次调用时的工程代数 |
| `result` | `dict` 或 `None`；只有 `SUCCEEDED` 有 |
| `error` | `ErrorDetail` 或 `None`；只有 `FAILED` 有 |

模型自身带状态组合校验：`SUCCEEDED` 必有结果且进度 100；`FAILED` 必有错误、不能有结果；
`CANCELLED` 两者都不能有；非终态不能有 `finished_at` 或 `result`；只有 `FAILED` 能有 `error`。
一份"状态机到不了的组合"会在构造时就 `ValueError`，不会被静默修好。

`ErrorDetail` 有 `code`、`message`、`details`；`code` 在构造时校验必须在 23 个已知错误码里；
`details` 目前恒为 `None`（没有通路的字段不要假装有）。

两类失败，不要混：

| 形态 | 什么时候 | 调用方拿到 |
| --- | --- | --- |
| 控制面异常 | 调用本身无效：未知 Operation、未知执行 id、非主线程、引擎已关闭、对后台操作调 `run_sync` | 抛 `FrameworkError`，有 `.code`/`.message`，**不建记录** |
| `FAILED` 记录 | 调用有效但执行失败：入参校验、路径策略、GIS 动作、输出校验、任务提交失败 | `ExecutionRecord.status == "FAILED"`，`record.error.code` 是 23 个码之一 |

### 6.7 状态机（`ExecutionStore._TRANSITIONS`）

| 当前 | 合法后继 |
| --- | --- |
| `QUEUED` | `RUNNING`、`PREPARING`、`CANCELLED`、`FAILED` |
| `PREPARING` | `SCHEDULED`、`FAILED` |
| `SCHEDULED` | `RUNNING`、`CANCEL_REQUESTED`、`FAILED` |
| `RUNNING` | `SUCCEEDED`、`FINALIZING`、`FAILED`、`CANCEL_REQUESTED` |
| `CANCEL_REQUESTED` | `CANCELLED`、`FAILED` |
| `FINALIZING` | `SUCCEEDED`、`FAILED` |
| `SUCCEEDED` / `FAILED` / `CANCELLED` | 无（终态不能再写） |

规则：

- 状态只在 `ExecutionStore.transition()` 里写；不在表里的边是插件缺陷，抛
  `INTERNAL_ERROR("<id>: <旧状态> -> <新状态> is not a transition")`。
- `transition()` 可以只更新 `progress`（`status=None`）；终态记录连进度都不能再写。
- 进入终态时若没给 `finished_at`，存储层自动补 `utc_now()`。
- 每次写入都通过 `ExecutionRecord.model_validate(...)` 重建，组合不合法会在这里失败，而不是被
  存进去。

### 6.8 引擎：`OperationEngine`

构造签名（生产路径由 `plugin.py` 装配，测试直接调）：

```python
OperationEngine(
    registry, settings, lifecycle,
    *, iface=None, project=None,
    background_runner=None,        # (engine, record, operation, request) -> None
    background_cancel=None,        # (engine, execution_id) -> None
    post=None,                     # 默认 QTimer.singleShot(0, callback)
    clock=None,                    # 默认 time.monotonic，测试用
)
```

方法：

| 方法 | 前置校验 | 行为 |
| --- | --- | --- |
| `run_sync(name, args)` | 主线程 + 引擎未关闭 + 名字存在 + 策略是 `main_thread` | 建 `QUEUED` 记录 → 校验入参 → 同栈执行 → `RUNNING` → 终态；校验失败返回 `FAILED` 记录（不是抛异常） |
| `submit(name, args)` | 主线程 + 引擎未关闭 + 名字存在 | 建记录 → 数 `QUEUED` 是否超过 `max_pending` → 校验入参 → 投递回调 → 返回 `QUEUED` 快照；超出容量 (`TOO_MANY_EXECUTIONS`)、入参不合法 (`INVALID_INPUT`)、没装后台适配器 (`TASK_SUBMISSION_FAILED`) 时返回 `FAILED` 记录，不抛异常 |
| `get(id)` | 主线程（**不要求引擎未关闭**） | 记录快照；过期或未知 → `EXECUTION_NOT_FOUND` |
| `cancel(id)` | 主线程 + 引擎未关闭 | 见下面的取消矩阵 |
| `shutdown()` | 主线程；可重复调用 | 关闭后拒绝新的 `run_sync`/`submit`/`cancel`；取消所有 `QUEUED`，对活动后台任务发取消请求 |
| `on_project_replaced()` | 主线程 | 取消旧代数的 `QUEUED`，对活动后台任务发取消请求；末尾在 `finally` 里确保停止请求发出 |

取消矩阵（`cancel` 的返回值是 `CancelReceipt(execution_id, accepted, status, reason)`）：

| 记录状态 | 策略 | 结果 |
| --- | --- | --- |
| 任意终态 | 任意 | `accepted=False, reason="ALREADY_TERMINAL"`，状态不变 |
| `QUEUED` | 任意 | 立即 `CANCELLED`，`accepted=True`；后台的还要把等待队列项删掉 |
| `FINALIZING` | 任意 | `accepted=False, reason="FINALIZING"`（结果正在落地，无法撤回） |
| `SCHEDULED` / `RUNNING` | background | 先写 `CANCEL_REQUESTED`，再让适配器调 `QgsTask.cancel()`；`accepted=True`，但**只有任务自己的报告才会变成 `CANCELLED`** |
| `CANCEL_REQUESTED` | background | `accepted=True`（重复请求是幂等的） |
| 其它（`RUNNING` 的主线程、`PREPARING`） | — | `accepted=False, reason="NOT_INTERRUPTIBLE"` |

入参校验规则（`_validated_input`）：Operation 自己抛的 `FrameworkError` 保留原码；Pydantic 的
`ValidationError` 变成 `INVALID_INPUT`，消息只带**字段路径**（最多前三个，没有字段时是
`<root>`），不带调用方的值；其它异常变成 `INTERNAL_ERROR`，消息是
`"<异常类型名>: unexpected failure"`，异常文本只进日志。

输出校验（`_serialised_output`）：`operation.output_model.model_validate(output)` 之后
`model_dump(mode="json")`。任何不符合声明的地方都是
`FAILED(OUTPUT_VALIDATION_FAILED, "<op>: output does not match <模型名>")`。因此输出模型必须能
用 JSON 表达——`NaN`、`Infinity`、QGIS 对象都不行。

资源与保留：

| 配置 | 作用 |
| --- | --- |
| `max_pending` | `submit` 时 `QUEUED` 记录数上限；计数包含本次刚创建的记录，所以最多同时有 `max_pending` 个排队调用，再多一个就是 `FAILED(TOO_MANY_EXECUTIONS)` |
| `max_active_background` | 适配器活动表里的任务数：`SCHEDULED`、`RUNNING`、`CANCEL_REQUESTED`（已交给 QGIS、还没结束）；再多一个就在等待队列里按提交顺序排。引擎里的 `_ACTIVE_BACKGROUND` 只有 `SCHEDULED`/`RUNNING`，那是"发取消请求"的集合，不用于限流（见 §6.10） |
| `max_terminal_records` | 终态记录保留条数，超出先淘汰最旧的 |
| `terminal_ttl_seconds` | 终态记录保留秒数，超时即淘汰 |

保留策略没有定时器：`get` 与 `ids` 每次都 prune；`create`/`transition` 只在写入终态记录时
prune。一条超过 TTL 的记录在被问到之前仍占内存，被问到时已经不存在。

工程代数（epoch）：

- `ProjectLifecycle` 从 0 开始，只在 `QgsProject.aboutToBeCleared` 时 `bump()` 一次，没有第二个
  计数器。
- `submit`/`run_sync` 创建的记录带当时的 epoch。
- 主线程排队回调执行前会比对：epoch 已变则直接 `CANCELLED`，不碰新工程。
- 后台在 `_start`（取快照前）与 `_finish_results`（落地前）各比对一次：前者直接 `CANCELLED`，
  后者 `FAILED(STALE_PROJECT)`。
- 换工程时 `on_project_replaced()` 取消旧代数的排队项，并对活动后台任务发取消请求；正在执行的
  **主线程**调用不受影响（它就在触发 `aboutToBeCleared` 的那条栈上）。

### 6.9 主线程投递器：`MainThreadRunner`

```python
MainThreadRunner(is_live, post=None)
post(execution_id, callback)
```

- `post` 发包一层 `run()`：先问 `is_live(execution_id)`（记录仍非终态）再执行 `callback`；两者
  都在 `try/except` 里，任何异常只记日志，不逃进 Qt。
- 默认 `post` 是 `QTimer.singleShot(0, callback)`，即下一个事件循环轮次；测试注入一个列表的
  `append`，自己 drain。
- `run_sync` 不走这个投递器：它在调用栈上同步执行。

### 6.10 后台适配器：`OperationTaskRunner`

引擎只认两个注入点：

```python
background_runner(engine, record, operation, request)  # 已接受的后台调用，记录仍是 QUEUED
background_cancel(engine, execution_id)  # 请求停止一个后台执行
```

适配器从 `background_runner` 被调用的那一刻接手：

1. 若 `len(active) >= max_active_background`，把这次调用放进等待队列（记录保持 `QUEUED`），等
   有槽位再开始；否则立即 `_start`。
2. `_start`：epoch 比对（变了 → `CANCELLED`）→ `PREPARING` + `started_at` → 主线程调
   `prepare_main` → 构造 `QgsTask`（必要时 `setDependentLayers`）→ `task_manager.addTask()`。
   - `prepare_main` 抛 `FrameworkError` 保留码；其它异常 `INTERNAL_ERROR`。
   - `addTask` 抛异常或返回 0 → `FAILED(TASK_SUBMISSION_FAILED)`。
   - 成功 → `SCHEDULED`，把 `(execution_id → _Submission)` 记进活动表。
3. worker 线程：`OperationTask.run()` 调 `OperationRun.run(control)`，后者只调
   `execute_worker`，并**捕获 `BaseException`** 存成诊断信息、返回 `False`——C++ 任务层里逃出
   异常会让整个 QGIS 退出。
4. 主线程 `finished(result)`：`task_finished()` 先把活动项 pop 掉，然后 `_complete`：
   - worker 有异常 → 先确保 `RUNNING`，再 `FAILED`；`FrameworkError` 保留原码，其它
     `INTERNAL_ERROR`。
   - 取消（`task.isCanceled()` 或记录是 `CANCEL_REQUESTED`）→ `SCHEDULED`/`RUNNING` 先转
     `CANCEL_REQUESTED`，再 `CANCELLED`。
   - `result=False` 且没有异常 → `RUNNING` → `FAILED(INTERNAL_ERROR, "the task ended without a result")`。
   - 成功 → epoch 比对（变了 → `FAILED(STALE_PROJECT)`）→ `FINALIZING` → 调 `finalize_main`
     → 校验并序列化输出 → `SUCCEEDED` + `progress=100`。
   - `_complete` 自己抛异常 → `_abandon()` 把非终态记录落到 `FAILED(INTERNAL_ERROR, "the completion of this execution failed")`。
   - 正常与异常路径都在 `finally` 里 post 一次 drain（认不出的执行 id 在进入 `try` 之前就
     早退，不 drain）。
5. drain 在**下一个事件循环轮次**执行（不能在上一个任务的 `finished()` 里直接 `addTask`——那样
   提交的任务可能永远停在 `Queued`）。它重新读槽位数与每条记录状态，按 FIFO 启动等待项；已被
   取消或 epoch 变了的项被丢弃。

进度规则（`progressChanged` 信号，Qt 在主线程送达）：

- `progress >= 100` 一律忽略：只有 `SUCCEEDED` 那一步能写 100（QGIS 对"`run()` 返回 True"的
  任务都会报 100，取消的和失败的也报，100 不能证明成功）。
- 首次非 100 的报告把 `SCHEDULED` 推成 `RUNNING`（worker 在别的线程，`run()` 的开始没有信号）。
- 记录最多每 100 ms 写一次；窗口内到达的值先暂存，任务结束时补写最后一次，避免"结束时的进度
  比中途还小"。
- worker 侧通过 `_TaskControl` 读写：`is_cancelled()` 读 `QgsTask.isCanceled()`，
  `set_progress()` 把值 clamp 到 0–100 后交给 `QgsTask.setProgress()`。

`QgsTask` 子类（`task_class()` 第一次使用时才构建）：

```python
class OperationTask(QgsTask):
    def __init__(self, runner, execution_id, operation, snapshot):
        super().__init__(operation.description, QgsTask.CanCancel)
        ...

    def run(self) -> bool: ...  # worker 线程
    def finished(self, result: bool) -> None: ...  # 主线程
```

适配器只在任务存活期间持有它的 id，从不保存 `QgsTask` 对象：QGIS 在任务结束后会删掉 C++ 侧。

`QgsTask` 的硬事实（QGIS 3.44.13 实测，都直接决定了上面的写法）：

| 事实 | 后果 |
| --- | --- |
| `finished(bool)` 是 protected virtual，不是 signal | 只有 manager 会调它；要接信号只能用 `QgsTask.fromFunction`，本插件用子类 |
| `addTask()` 返回 0 表示 manager 没接手 | 任务对象仍归插件，可以丢；返回非 0 后所有权归 QGIS，仍可 `cancel()`，但不要自己删 |
| 任务结束后 QGIS 删掉 C++ 对象 | 只留 task id，不留任务指针；访问已删对象会 `RuntimeError` |
| `cancel()` 只置 `isCanceled()`，不检查 `canCancel()` | worker 必须自己周期性检查；这也是 `CANCEL_REQUESTED` 不是终态的原因 |
| worker 里抛出的异常会 abort QGIS | `OperationRun.run` 用 `except BaseException` 兜住并转成失败记录，堆栈只进日志 |
| `setProgress()` 只在"十分之一"档位变化时发 `progressChanged` | 实测 16 次调用（0.01 … 2.1）只换来 6 次信号；1.0 步进的 100 次调用每次都跨档，所以求和任务的 100 次调用全到了 |
| 任务成功结束时 QGIS 还会额外报一次 100 | 所以适配器忽略一切 ≥ 100 的报告，100 只由 `SUCCEEDED` 那一步写 |
| 在另一个任务的 `finished()` 里 `addTask`，任务可能被收下却永不启动 | 实测 100 个任务的链里 45 轮出现；改成 `QTimer.singleShot(0, …)` 后 0/100，所以 drain 只投递、不直调 |
| `waitForFinished(timeout=30000)` 阻塞调用线程，`0` 是无期限 | 本插件不用它：主线程等任务就是死锁 |
| 只有 virtual 成员会让 C++ 回调进 Python | `QgsTask::run()`/`finished()` 可以 override；`QgsFeedback::setProgress()` 不是 virtual，override 不会被调 |

没有用到、也不要随手引入的两种机制：`QgsTask.fromFunction`（拿不到 `setDependentLayers` 这类
实例方法）与 `QgsProcessingAlgRunnerTask`/`QgsMapRendererParallelJob`（自带一套生命周期或自带
线程；`processing.run_algorithm` 是在主线程直接调 `qgis.processing.run`）。

### 6.11 插件生命周期：`Plugin`

`plugin.py` 在模块级先过一道依赖闸门：`require_supported_pydantic(pydantic.VERSION)` 在 import
时执行，不是 2.x 就抛 `RuntimeError`——DTO 层是照 pydantic 2 写的，别的 major 要让调用方看到
依赖错误，而不是某个模型模块里的 import 失败。

`initGui()` 的顺序是固定的：

1. `read_settings()`：从 `QgsSettings` 读 `qgis_agent_plugin/<四项键>`，`Settings.from_mapping`
   转成 frozen dataclass。没写的键保持默认。
2. `ProjectLifecycle()`。
3. 为本插件单独建一个 `QgsTaskManager()`（不用全局的，`cancelAll()` 会波及别的插件）。
4. `OperationTaskRunner(task_manager=manager)`。
5. `OperationRegistry()` + `register_all(registry)`。
6. `QgsProject.instance()` 与 `OperationEngine(...)`（注入 `background_runner=runner`、
   `background_cancel=runner.cancel`）。
7. `application_supervisor(manager, engine, runner)`：一个以 `QgsApplication` 为 parent 的
   `Supervisor`+`QObject`，连接 `manager.taskAdded` / `taskAboutToBeDeleted` 做记账。
8. `project.aboutToBeCleared.connect(self._project_about_to_be_cleared)`：唯一的 epoch 递增点，
   随后调 `engine.on_project_replaced()`；slot 里异常只记日志，不向外抛。
9. `_create_dock()`：优先 `iface.addDockWidget`，否则 `iface.mainWindow().addDockWidget`；没有
   可停靠的宿主就不建，这种情况下连 Qt 都不会被 import。
10. `_create_entry()`：插件菜单（`&QGIS Agent`）下挂一项、工具栏加一个 `mActionTerminal` 开关，
   两者指向同一个坞窗（`checkable`，坞窗自己的关闭按钮会把开关弹回来）。界面没有
   `addPluginToMenu`/`addToolBarIcon` 时坞窗照建，入口不建。
11. `_publish()`：`iface.agent_engine = engine`、`iface.agent_registry = registry`。

`unload()` 的顺序也是固定的：

1. 断开 `aboutToBeCleared`（重复卸载或从未加载时用 `suppress` 吞掉）。
2. 先卸坞窗（它的轮询会碰引擎），`dock.shutdown()` + `removeDockWidget` + `deleteLater()`。
3. `_remove_entry()`：`removePluginMenu` + `removeToolBarIcon` + `action.deleteLater()`。
4. 从 `iface` 撤掉两个属性（`_withdraw`）。
5. `engine.shutdown()`：拒绝新工作 → 取消所有排队项 → 对活动后台任务发停止请求（从不全局取消）。
6. `supervisor.close()`：把还在跑的任务交给 Supervisor；最后一个任务真正终止后，它在主线程上
   `deleteLater()` 自删，并释放 engine/runner 引用。没有任何一步阻塞等待。

`Supervisor` 的记账只有三个动作：`attach(task_id)`（任务进了本插件的 manager）、
`release(task_id)`（QGIS 即将删除该任务）、`close()`（会话结束）；`live` 暴露还活着的 task id。
这样任务结束前，它的 Python 回调（属于 engine/runner）不会被卸载流程提前释放。

### 6.12 配置项

全部在 `QgsSettings` 的 `qgis_agent_plugin/<键>` 下：

| 键 | 默认 | 语义 |
| --- | --- | --- |
| `max_active_background` | 2 | 同时活动的后台任务数 |
| `max_pending` | 32 | 排队上限（主线程 + 后台合计） |
| `max_terminal_records` | 200 | 终态记录保留条数 |
| `terminal_ttl_seconds` | 1800 | 终态记录保留秒数 |

`Settings.from_mapping` 的规则：键缺失或为 `None` 都算"没设置"，保留默认；这个版本不认识的键
（旧配置里留下的 `allowed_data_roots` 之类）直接忽略，不让加载失败；计数接受数字字符串，但
**拒绝小于 1 的值**（`max_active_background=0` 会让后台调用永远等不到槽位），所以插件会直接
加载失败并报出键名，而不是静默排队。



---

## 7. 十七个 Operation 的契约

### 7.1 通用约定：调用面

| 方法 | 能调谁 | 返回 |
| --- | --- | --- |
| `run_sync(name, args)` | 只有 `main_thread` 的 16 个 | 终态记录（`SUCCEEDED` / `FAILED`） |
| `submit(name, args)` | 全部 17 个 | 刚创建的记录快照，通常是 `QUEUED`（被拒时是 `FAILED`） |
| `get(execution_id)` | — | 记录副本；未知或已淘汰抛 `EXECUTION_NOT_FOUND` |
| `cancel(execution_id)` | — | `CancelReceipt`，矩阵见 §6.8 |
| `shutdown()` | — | 无返回值；`unload` 内部调用，之后新请求得 `ENGINE_CLOSING` |

五个方法都必须从创建引擎的那个线程（QGIS 主线程）调用，否则 `WRONG_THREAD`。失败分两类
（控制面异常 / `FAILED` 记录），见 §6.6。

输入模型的共同规则：`extra="forbid"`（多写一个键是 `FAILED(INVALID_INPUT)`，不是被忽略）、
布尔与字符串用 `StrictBool`/`StrictStr`（`"true"`、`1` 一律拒）、没有参数的操作也有一个空模型
用来拒键。`registry.get_spec(name)` 与 `list_specs()` 给出同一份契约的机器可读版本（`name`、
`description`、`kind`、`strategy`、`destructive`、`input_schema`、`output_schema`），schema 由
Pydantic 生成：

```python
spec = iface.agent_registry.get_spec("layer.add")
spec.input_schema["required"]  # ['kind', 'source_path', 'name']
spec.output_schema["required"]  # ['layer_id', 'name', 'kind', 'provider', 'crs_authid']
```

### 7.2 路径

路径只有一条规则，在 `core/security.absolute_path` 里：

```python
absolute_path(raw: str) -> Path
```

| 情形 | 规则 |
| --- | --- |
| 任何情形 | `raw` 必须非空、无 NUL；否则 `PATH_NOT_ALLOWED` |
| 绝对字形 | `C:\…`、`\\host\share\…` 与 GDAL 的 `/vsi…` 都算绝对，原样返回 |
| 相对字形 | `cities.gpkg`、`C:cities.gpkg`、`\cities.gpkg` 与 `https://…` 都被拒：它们要么依赖工作目录，要么根本不是路径 |
| 不做的判断 | 存在、是不是文件、在不在某个目录内、本地还是远程——由 `read()` 与 provider 回答，它们的答案比预检新 |

没有根目录白名单，也没有 `allowed_*_roots` 配置：一条路径可以是这台机器上进程能读写的任何位置。
这是刻意的边界，记在 `12。

`project.open` 在 `read()` 之前只做一件额外的事：拒掉两种会让**进程直接消失**的输入
（P3 防死）。这是本插件唯一比 GUI 严的地方，所以两条都有本机 QGIS 3.44.13 上的实测，而且界限
只由"崩"定，不由"慢"定：

- **嵌套深度**由标准库 `expat` 的事件数算（开标签 +1、闭标签 −1；注释与 CDATA 不是元素）。
  上限 `4096` 层。实测：`read()` 在 6000 层以 `0xC00000FD`（栈溢出）结束进程，5500 层 0.10 s
  读完；元素名叫 `a` / `b` / `maplayer` / `properties` 的崩溃点一模一样，所以那是 DOM 深度而不是
  某个标签的性质。900 层要 30 s 才读完（200 层 0.30 s、600 层 8.62 s、2000 层 518 s），但"慢"
  不是拒绝的理由：GUI 同样慢。QGIS 自己写出的 100 层嵌套分组是 105 层深。深度只数一个文档：
  `QgsProjectArchive::projectFile()` 只返回归档里第一个 `.qgs`（`src/core/qgsarchive.cpp:147`–`157`），
  `QgsProject::unzip` 也只把它交给 `readProjectFile()`（`src/core/project/qgsproject.cpp:4677`），
  第二个 `.qgs` 再深也进不了 DOM。
- **`.qgz` 里的加密成员**（`flag_bits & 0x1`）：`read()` 解开整个归档，碰到它直接以
  `0xC0000005` 结束进程，连异常都没有（实测）。成员表只看头部字段，一个成员都不解压。

被量过、但**不**杀进程的形状一个都不留，全部交给 `read()`：

| 形状 | 本机实测 | 现在 |
| --- | --- | --- |
| 远程数据源（`/vsicurl`、UNC、`<datasource>` 里任意位置的 `://`、布局图片 URL） | GUI 自己去取，`read()` 照样读 | 不判，加载不出来的层只让 `invalid_layer_count` 变大 |
| XML 实体声明（`<!ENTITY`、带 `[` 的 DOCTYPE） | `read()` 0.53 s 读完、`ok=True`（QDomDocument 不展开自定义实体） | 不判；扫描流式解析、不累积，实体展开也不进内存 |
| 体积（工程文件与 `.qgz` 成员） | 64 MiB 的 `.qgs` 在 1.80 s 内读完、`ok=True` | 没有上限，GUI 也是 `readAll()` |
| 标签数（标签炸弹） | 40 万个标签解析 0.12–0.19 s | 没有计数 |
| `.qgz` 成员数 | 10 万个空成员要 1.75 s | 没有上限 |
| 压缩方法不是 stored/deflate（如 bzip2） | `read()` 读完、`ok=True` | 不判 |
| 成员的 CRC 对不上 | `read()` 读完、`ok=True` | 不判 |
| 成员声明的解压尺寸撒谎 | `read()` 返回假 → `PROJECT_OPEN_FAILED`，与拒掉同码 | 不判 |
| 字节里有 NUL / 不是 UTF-8 / XML 不合法 | `read()` 返回假 → `PROJECT_OPEN_FAILED` | 不判；扫描解析失败就交给 `read()` |

扫描读不懂的文件不是扫描的结论：`expat` 报错就返回，`read()` 用自己的码回答。`read()` 之后也
不再有逐层检查：工程点名的数据源可以是根外路径、目录、不存在的源或远程源，插件不判它们。

### 7.3 destructive 标记

| 值 | 含义 | 当前 |
| --- | --- | --- |
| `False` | 不丢用户数据；最坏是新增图层、改可见性或改样式 | 12 个 |
| `True` | 可能替换当前工程（丢掉未保存内容）或覆盖已有文件 | `project.create`、`project.open`、`project.save`、`layer.set_renderer`、`processing.run_algorithm` |

判断标准是"用户是否可能因此丢掉没保存的东西"，不是"是否写了磁盘"。`processing.run_algorithm`
的 `True` 是保守标法：一个名字背后是一组算法，风险不能依赖具体调用。标记不影响引擎行为，只出现
在 `OperationSpec` 与坞窗的风险提示里。

### 7.4 错误码

23 个码是完整词表，多一个少一个都是缺陷（`FrameworkError` 与 `ErrorDetail` 构造时都校验）。
按"什么时候产生"分三类，调用方的处理方式完全不同：

| 类 | 出现方式 | 有没有记录 | 码 |
| --- | --- | --- | --- |
| 注册期 | 插件加载时抛异常，插件起不来 | 无 | `INVALID_OPERATION_DEFINITION`、`DUPLICATE_OPERATION` |
| 控制面 | 调用本身无效，抛 `FrameworkError` | **无** | `OPERATION_NOT_FOUND`、`WRONG_THREAD`、`ENGINE_CLOSING`、`UNSUPPORTED_EXECUTION_MODE`、`EXECUTION_NOT_FOUND` |
| 执行面 | 调用有效但失败，返回 `FAILED` 记录 | 有 | 其余 16 个 |

`record.error` 的形状是 `{"code": "…", "message": "…", "details": null}`：`details` 存在但
**恒为 `null`**（`FrameworkError` 只带 code 与 message，没有 details 通路）；`message` 是给人看的
一句话，堆栈只进本机日志。

`message` 默认不复述路径、不贴异常文本、不带调用方传的值。§7.6 的个别条目是这条规则的例外：GUI
自己把那句话拼给用户的地方（加图层时点名源、统计时点名字段、样式导出失败），消息就是 GUI 那一句，
包括它嵌进去的路径与字段名。

| 码 | 何时用 | 谁用 |
| --- | --- | --- |
| `OPERATION_NOT_FOUND` | 名字不在注册表 | 注册表 `get` / `get_spec` |
| `INVALID_OPERATION_DEFINITION` | 声明不合法、名字格式不对 | 注册表 `register` |
| `DUPLICATE_OPERATION` | 同名重复注册 | 注册表 `register` |
| `WRONG_THREAD` | 不在创建引擎的线程调用 | `require_main_thread` |
| `ENGINE_CLOSING` | 引擎已 `shutdown` | 引擎 |
| `UNSUPPORTED_EXECUTION_MODE` | 对后台操作调 `run_sync` | 引擎 |
| `EXECUTION_NOT_FOUND` | 未知或已淘汰的执行 id | 记录仓库 |
| `INVALID_INPUT` | 入参不合法（未知键、类型、形状、未知算法 id） | 引擎校验、操作内 validator |
| `PROJECT_REPLACE_REQUIRED` | GUI 的四道问句任一命中（图层编辑、memory 或临时图层、工程脏、栅格 RAT，见 §7.6）且没有 `discard_current=true` | `project.create/open` |
| `PROJECT_OPEN_FAILED` | 工程读不出来（含嵌套超上限、加密的 `.qgz` 成员） | `project.open` |
| `PROJECT_SAVE_FAILED` | `write()` 返回假；文件可能已部分写入 | `project.save` |
| `PROJECT_PATH_REQUIRED` | 没给路径且工程没有关联文件 | `project.save` |
| `TARGET_EXISTS` | Save As 的目标已存在且没有 `overwrite=true`（Save 自己的关联文件不查） | `project.save` |
| `PATH_NOT_ALLOWED` | 路径不是绝对路径 | `absolute_path` |
| `INVALID_LAYER` | provider 的清单为空（没有认得出的子图层）、源里有好几个子图层而没点名、不是矢量/栅格图层、没有渲染器、非空间图层的可见性 | `layer.add`、`layer.*` 的样式与渲染器操作、`layer.set_visibility` |
| `LAYER_NOT_FOUND` | 工程里没有这个 id | `layer.get_info/set_visibility/sum_numeric_field` 与样式读 |
| `LAYER_NOT_IN_TREE` | 图层已注册但不在图层树里（加载来的工程可以这样；`layer.add` 已经造不出这种图层） | `layer.set_visibility` |
| `INVALID_FIELD_TYPE` | 没有该字段、字段非数值、值或和不是有限数 | `layer.sum_numeric_field` |
| `TOO_MANY_EXECUTIONS` | `QUEUED` 数超过 `max_pending` | 引擎 `submit` |
| `TASK_SUBMISSION_FAILED` | 没装后台适配器，或 `addTask` 返回 0 | 引擎 / 适配器 |
| `STALE_PROJECT` | 结果落地时工程代数已变 | 后台 `finalize_main` / 适配器 |
| `OUTPUT_VALIDATION_FAILED` | 返回值不合自己声明的输出模型 | 引擎 / 适配器 |
| `INTERNAL_ERROR` | 未预期异常、非法状态迁移、终态再写、完成处理本身失败、`newProject` 返回假、QGIS 拒绝任务 | 引擎 / 适配器 / 个别操作 |

使用规则：

- 能归到具体码的，不要用 `INTERNAL_ERROR`；它是"插件缺陷或环境异常"的兜底。
- 不要新增码来区分同一种失败的不同位置；调用方只该按语义分支。
- 消息是英文短句、面向调用方：默认不复述路径、不贴异常文本、不带调用方传的值；GUI 自己把那句话
  拼给用户的地方照抄它（见上）。

### 7.5 常见错误

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 插件加载即失败，日志 `requires pydantic 2.x` | 解释器里的 pydantic 不是 2.x | 用 QGIS 自带解释器，不要装 pydantic 1 |
| `INVALID_OPERATION_DEFINITION`，消息 `<名字>: main_thread requires execute_main` | 漏写阶段或混用两个策略的 mixin | 只继承一个策略 mixin，并实现它要求的阶段 |
| `INVALID_OPERATION_DEFINITION`，消息 `<名字>: declares both execution strategies` | 一个类同时具备 `execute_main` 与三个后台阶段 | 只保留一个策略 mixin |
| `INVALID_OPERATION_DEFINITION`，消息 `invalid operation name: '<名字>'` | 名字不是 `<group>.<verb>` | 小写 snake，两段，一个点 |
| 测试报 `ModuleNotFoundError: qgis` 而代码看起来没错 | 在模块级 import 了 QGIS | 挪进函数体；`test_package` 会先红 |
| 记录停在 `QUEUED` 不前进 | 没推 Qt 事件循环，或后台适配器没注入 | 生产进程里等事件循环；测试里 drain 注入的 poster |
| `WRONG_THREAD` | 在工作线程调 `get`/`cancel`/`submit` | 回主线程（可用 `QTimer.singleShot(0, …)`） |
| 对 `layer.sum_numeric_field` 调 `run_sync` 抛 `UNSUPPORTED_EXECUTION_MODE` | 后台操作只能 `submit` | 改用 `submit` + drain + `get` |
| `INVALID_OPERATION_DEFINITION`，消息 `<名字>: input_model must inherit Params` | 输入模型没继承 `Params`（只做了第 4 条的 `BaseModel`） | 改成继承 `Params`，不要自己写 `model_config` |
| 记录 `FAILED(OUTPUT_VALIDATION_FAILED)` | 返回值与 `output_model` 不符：缺字段、多字段、类型错、含 `NaN`/`Infinity` | 按模型构造返回值 |
| 记录 `FAILED(INTERNAL_ERROR)` 且日志有 traceback | 操作里抛了裸异常 | 用 `FrameworkError` 表达可预期失败 |
| `PATH_NOT_ALLOWED` 且消息是 `the path must be an absolute path` | 路径是相对的，或者是个 URL | 给一条带盘符的完整路径 |

### 7.6 各 Operation 的契约

每个 op 一张表：参数（含默认值与约束）、返回字段、业务错误码；再几条只有这一个 op 才成立的
行为。控制面码、容量码、输出校验码不在每张表里重复，见 §7.4。

#### project.create

| 项 | 内容 |
| --- | --- |
| 参数 | `crs_authid`（StrictStr ≥1 \| null，默认 `null`）；`discard_current`（StrictBool，默认 `false`） |
| 返回 | `crs_authid`（工程现在持有的规范 authid，构造不出 CRS 时是 `""`）、`file_path`（新工程无关联文件时为 `null`）、`layer_count` |
| 业务错误码 | `PROJECT_REPLACE_REQUIRED`（下面四道问句任一命中且没有 `discard_current=true`）、`INTERNAL_ERROR`（`iface.newProject(promptToSaveFlag=False)` 返回假） |
| 行为 | 换工程之前先回答 GUI 的四道问句，顺序就是 GUI 的顺序（`qgisapp.cpp:5820`）：① 非 memory 矢量图层 `isEditable() && isModified()`（`:13560`、`:13573`、`:13578`）；② 带要素的 memory 图层，设了 `skipMemoryLayersCheck` 的除外（`:13684`），或临时图层（`:13690`，两者都有时 GUI 先问临时图层）；③ 工程 `isDirty()`（`:13522`，`saveDirty()` 会先为 ① 的编辑 `markDirty()`，`:13509`）；④ 栅格波段的 RAT `isDirty()`，循环是 `bandNo = 1; bandNo < bandCount()`（`:13615`，最高那一带 GUI 不问，这里也不问）。`discard_current=true` 就是这四句共同的 Discard 答案；两个设置（`qgis/askToSaveProjectChanges`、`askToSaveMemoryLayers`）不读，它们只决定 GUI 问不问。GUI 在问之前还先做 `checkTasksDependOnProject()`（`qgisapp.cpp:5815`），operation 的顺序相反：先答问句，再让 `iface.newProject` 做那一步（它自己会返回假，于是 `INTERNAL_ERROR`）。之后才等价于 GUI 的 New Project 动作（`iface.newProject(False)` 就是 `QgisApp::fileNew(false)`）：它自己会给新工程一个 CRS（设置里的 `projections/defaultProjectCrs`，以及默认模板）并读默认模板；`crs_authid` 不给就不碰那个值，给了就 `setCrs` 覆盖它（无效值也照装，结果是空 authid，GUI 的新建动作不拒这个）；最后 `setDirty(false)`，与 `fileNew` 的最后一步一致；成功路径触发 `aboutToBeCleared`，`project_epoch` +1，旧代数的排队调用立即 `CANCELLED`，本次调用照常 `SUCCEEDED` |

#### project.open

| 项 | 内容 |
| --- | --- |
| 参数 | `path`（StrictStr ≥1，绝对路径，**不判后缀**）；`discard_current`（默认 `false`） |
| 返回 | `file_path`（调用方给的绝对路径）、`crs_authid`（可能为 `""`）、`layer_count`、`invalid_layer_count` |
| 业务错误码 | `PATH_NOT_ALLOWED`（不是绝对路径）、`PROJECT_REPLACE_REQUIRED`（与 `create` 同一四道问句，`qgisapp.cpp:6943`）、`PROJECT_OPEN_FAILED`（三条消息分得很清：`read()` 为假说 "the current project was cleared"；嵌套超过 `MAX_PROJECT_DEPTH` 与加密成员说 "it was not opened and the current project was not changed"，因为那两条拒在 `clear()` 之前） |
| 行为 | 用 `QgsProject.read()`，不用 `iface.addProject()`（后者会弹框）；不判后缀：GUI 的打开对话框收下你打的任何文件名，`read()` 只把 `.qgz` 当压缩包、剩下的交给解析器（`x.qgs~` 备份也是这样打开的）；`read()` **之前**先 `ctx.project.clear()`（GUI 的 `addProject` 先 `closeProject()`），所以 `read()` 失败时眼前这个工程是空的、`file_path` 指向那个读不出来的文件；P3 那两条拒在 `clear()` 之前，不碰当前工程，消息也这么说（见 §7.2）；GUI 的 `openProject(QString)` 还先做 `checkTasksDependOnProject()`（`qgisapp.cpp:6939`），operation 不做这一步：这里的后台任务由引擎的协作式取消处理（换工程会取消旧代数的排队调用与活动后台任务），不在读前弹框；读成功但有坏图层算成功（`invalid_layer_count > 0`） |

#### project.save

| 项 | 内容 |
| --- | --- |
| 参数 | `path`（StrictStr \| null，为空则用工程当前关联文件；给了就必须是 `.qgs`/`.qgz`，大小写不敏感）；`overwrite`（默认 `false`） |
| 返回 | `file_path`、`is_dirty`（写完之后工程的真实状态） |
| 业务错误码 | `PROJECT_PATH_REQUIRED`、`INVALID_INPUT`（后缀不是 `.qgs`/`.qgz`）、`PATH_NOT_ALLOWED`（不是绝对路径）、`TARGET_EXISTS`（**只有 Save As 才查**）、`PROJECT_SAVE_FAILED`（文件可能已部分写入，不承诺原子性；只读、目录不存在、目标是目录都落在这里） |
| 行为 | 给了 `path` 就是 Save As：GUI 的保存对话框会补后缀并问覆盖，所以 `overwrite` 就是那个答案；不给 `path` 就是 Save，写关联文件、**不问覆盖**（GUI 那时不弹对话框）；不创建父目录；`write()` 成功会清 dirty，所以返回的是 QGIS 的真实状态，不是写死的 false |

#### project.get_info

| 项 | 内容 |
| --- | --- |
| 参数 | 无（任何键都 `INVALID_INPUT`） |
| 返回 | `file_path`（未保存过为 `null`）、`crs_authid`（无 CRS 时为 `""`）、`is_dirty`、`layer_count`、`title`（工程标题，没设置时是 `""`）、`project_epoch` |
| 业务错误码 | 无 |
| 行为 | 不保存、不改工程属性。这六个字段就是 GUI 会显示的工程事实：标题栏的标题、`*` 脏标记与文件名（`title`/`is_dirty`/`file_path`），工程属性里的 CRS，图层树里的图层数；`lastSaveDateTime`、`saveUser` 只存不显示，模板是设置不是工程状态，所以不漏它们；`project_epoch` 是插件本地的代数（从 0 开始，每次 `aboutToBeCleared` +1），不是 QGIS 的版本号 |

#### layer.add

| 项 | 内容 |
| --- | --- |
| 参数 | `kind`（`vector`/`raster`）、`source_path`（绝对路径）、`name`（≥1，无长度上限）、`sub_layer`（≥1，名字是 provider 的自由文本；矢量与栅格都行） |
| 返回 | `layer_id`、`name`、`kind`、`provider`（`ogr` 或 `gdal`）、`crs_authid`（图层无效时为 `""`） |
| 业务错误码 | `INVALID_INPUT`、`PATH_NOT_ALLOWED`、`INVALID_LAYER`（栅格文件不是 raster、provider 的清单为空、`sub_layer` 不在清单里、源里有好几个子图层而调用方没点名） |
| 行为 | provider 由 `kind` 决定，调用方不能指定。栅格先过 `QgsRasterLayer.isValidRasterFileName`，不像栅格就一句 `"<path> is not a supported raster data source"`（这个绑定吞掉原生的 `retError`，所以没有后半句）。然后问 provider 的 `querySublayers`（带 `IncludeSystemTables`，与加图层的对话框同一个调用）并只留声明类型那几条：清单为空是 `"<path> is not a valid or recognized data source."`；正好一条且细节完整时用那条自己的 `uri()` 建图层（不自己拼，GDAL 的子数据集是别的形状）；点名 `sub_layer` 时用那条的 `uri()`，不在清单里就报出这个名字；清单里好几条（或细节需要更深扫描）而没点名时把名字列出来交回去，不静默挑一个——那是加图层对话框会问用户的地方。认出来的图层不查 `isValid()`：读不出来的照样进图例，靠坏图层图标提示；每次调用都进图例 |

#### layer.list

| 项 | 内容 |
| --- | --- |
| 参数 | 无 |
| 返回 | `layers`: `LayerSummary`（`layer_id`、`name`、`kind`(vector/raster/other)、`crs_authid`、`in_layer_tree`、`visible`、`effective_visible`）的列表，顺序 = 图例从上到下 |
| 业务错误码 | 无 |
| 行为 | 顺序是图层树的深度优先顺序（面板看到的就是它），工程里有而树里没有的图层接在后面、保持 `mapLayers()` 的顺序；`visible` 是节点自己的勾选状态，`effective_visible` 是连祖先组一起算的结果；没有树节点时两者都是 `null`；非空间图层（属性表）在图例里没有勾选框，两者也是 `null` |

#### layer.get_info

| 项 | 内容 |
| --- | --- |
| 参数 | `layer_id`（≥1） |
| 返回 | `layer_id`、`name`、`kind`、`provider`、`crs_authid`、`valid`、`in_layer_tree`、`visible`、`effective_visible` |
| 业务错误码 | `INVALID_INPUT`、`LAYER_NOT_FOUND` |
| 行为 | 可见性三个字段与 `layer.list` 用同一个函数算，两处不会不一致；不返回原始数据源 URI、连接串、要素或字段列表 |

#### layer.set_visibility

| 项 | 内容 |
| --- | --- |
| 参数 | `layer_id`（≥1）、`visible`（StrictBool） |
| 返回 | `layer_id`、`visible`（设置后的勾选状态）、`effective_visible` |
| 业务错误码 | `INVALID_INPUT`、`LAYER_NOT_FOUND`、`LAYER_NOT_IN_TREE`、`INVALID_LAYER`（非空间图层没有勾选框） |
| 行为 | 勾上时连祖先分组一起勾，取消只写这一个节点；写入口对同值本来就是空操作，所以实现不需要先判断。真的改了东西才 `setDirty(True)`（图例的勾选会置脏工程，同值重写不会） |

#### layer.sum_numeric_field

| 项 | 内容 |
| --- | --- |
| 参数 | `layer_id`（≥1）、`field_name`（≥1，必须是整型或浮点字段）、`selected_only`（StrictBool，默认 `false`） |
| 返回 | `layer_id`、`field_name`、`count`（参与求和的非 null 要素数）、`skipped_null`、`total`、`selected_only`（本次是否只算了选择集）、`snapshot_project_epoch` |
| 业务错误码 | `INVALID_INPUT`（在 `submit` 返回前就判掉）、`LAYER_NOT_FOUND`、`INVALID_LAYER`、`INVALID_FIELD_TYPE`（没有该字段用统计算法那句 `Invalid field for statistics: “<name>” does not exist`；非数值字段、值或和不是有限数）、`STALE_PROJECT`、`TASK_SUBMISSION_FAILED`；控制面 `UNSUPPORTED_EXECUTION_MODE` |
| 行为 | 唯一的后台 op：`prepare_main` 主线程拷成 `tuple[float, ...]`，`execute_worker` 后台求和，`finalize_main` 只比对 epoch。选择集只在 `selected_only=true` 时用（那是 Processing 面板上要用户勾的开关，默认不勾：有选择集而没勾时算法照样算整层）。NaN / Infinity 与溢出都拒：记录必须是 RFC 8259 解析器读得懂的 JSON，所以这是传输约束，不是统计口径。要素数没有上限；结果描述快照，不是结束时的实时值；声明 `dependent_layer_ids`，图层被删时 QGIS 自己取消任务 |
| 实测量级 | 4 个要素（1 个 null）→ `count=3`、`skipped_null=1`、`total=225.0`、`selected_only=false`；同一图层选中 2 个后 `selected_only=true` → `count=2`、`total=160.0`；9000 要素 → `total=40495500.0`，状态观测到 `QUEUED → SCHEDULED → SUCCEEDED`，worker 在 Qt 线程池线程、`finished` 回调在 `MainThread` |

#### layer.list_style_categories

| 项 | 内容 |
| --- | --- |
| 参数 | `layer_id`（≥1） |
| 返回 | `layer_id`、`categories`（样式对话框的勾选框名单；矢量 18 个按枚举声明顺序，栅格 9 个按分类模型的顺序，不含 `Labeling`） |
| 业务错误码 | `INVALID_INPUT`、`LAYER_NOT_FOUND`、`INVALID_LAYER`（不是矢量/栅格，含别人的图层类型） |
| 行为 | 报的是"这类图层适用哪些 category"，不是"有没有内容"；名单由代码固定，不从 XML 反推（`QgsMapLayer.StyleCategory` 是位标记枚举，Python 侧不能按声明顺序迭代，实测）；没有几何的矢量表也算数；无效图层照样答（样式读写没有有效性这道门）；位标记只在请求时构建，所以模块仍能在裸解释器里加载 |

#### layer.get_qml

| 项 | 内容 |
| --- | --- |
| 参数 | `layer_id`（≥1）、`categories`（可省略；省略=全部位标记；重复的名字就是一个位，不适用的分类只是一个没人写的位，都不拒；空列表=一个都不导出） |
| 返回 | `layer_id`、`qml`（XML 字符串，只在内存里，2 空格缩进） |
| 业务错误码 | `INVALID_INPUT`（不是 `StyleCategoryName`）、`LAYER_NOT_FOUND`、`INVALID_LAYER`、`INTERNAL_ERROR`（原生的导出消息；那是保存样式对话框弹给用户的那句话） |
| 行为 | QML 来自图层自己的 `exportNamedStyle`；不写文件；分类是按位或出来的一个标志，默认是 `AllStyleCategories`，导出只写这门图层类型有的那几类 |

#### layer.list_available_renderers

| 项 | 内容 |
| --- | --- |
| 参数 | `layer_id`（≥1） |
| 返回 | `renderer_types`（下拉框里的名字，按下拉框顺序，不去重不排序） |
| 业务错误码 | `INVALID_INPUT`、`LAYER_NOT_FOUND`、`INVALID_LAYER`（不是矢量/栅格，或图层没有渲染器） |
| 行为 | 矢量走 `QgsApplication.rendererRegistry().renderersList(layer)`（按几何过滤），栅格走注册表清单再按图层的 `rasterType()` 过滤（普通栅格里没有 `singlebandcolordata`，`SingleBandColorData` 栅格里只有它）；前置条件是符号页存在，即图层有渲染器。栅格那份清单来自新建的 `QgsRasterRendererRegistry()`（3.44 没把应用级那个访问器绑给 Python，见 `shortcut:`），所以第三方注册在 C++ 单例里的栅格渲染器既列不出来、也会被 `layer.set_renderer` 的同一份清单拒——这张表同时是写入的前置条件 |

#### layer.get_renderer

| 项 | 内容 |
| --- | --- |
| 参数 | `layer_id`（≥1） |
| 返回 | `layer_id`、`renderer_type`（图层当前渲染器的类型名）、`renderer_xml`（那个渲染器自己的文档，见下） |
| 业务错误码 | `INVALID_INPUT`、`LAYER_NOT_FOUND`、`INVALID_LAYER`（不是矢量/栅格，或图层没有渲染器） |
| 行为 | 文档是渲染器自己写出来的那一份：矢量是 `QgsFeatureRenderer.save()` 的元素（根名 `renderer-v2`），栅格是 `writeXml()` 写进管道的 `rasterrenderer` 元素；`renderer_xml` 可以直接喂回 `layer.set_renderer`，读与写是同一件事的两个方向 |

#### layer.set_renderer

| 项 | 内容 |
| --- | --- |
| 参数 | `layer_id`（≥1）、`renderer`（≥1 的 XML 字符串，形状与 `layer.get_renderer` 的 `renderer_xml` 相同） |
| 返回 | `layer_id`、`renderer_type`（安装后的类型名） |
| 业务错误码 | `INVALID_INPUT`（XML 解析失败——消息是解析器那句带行列号的话；解析出来了但没有这门图层类型认得的渲染器；类型不在这门图层的候选清单里）、`LAYER_NOT_FOUND`、`INVALID_LAYER`（不是矢量/栅格，或图层没有渲染器）、`INTERNAL_ERROR`（未预期异常，由引擎兜底） |
| 行为 | 整份渲染器先建好再一次性 `setRenderer`，所以被拒的配置不会留下半个样式，旧渲染器还在；矢量用 `QgsFeatureRenderer.load()`（QGIS 读样式文件时同一次调用），栅格用各类型自己的 `create(element, provider)`（注册表里那个建渲染器的函数，Python 拿不到函数指针所以按类型名写明）。只装候选清单里的类型，理由分两半：栅格那边实测有类型会被 QGIS 默不作声地保留（普通栅格上的 `singlebandcolordata`，报告成功就是谎报），矢量那边是下拉框已经按几何筛过、GUI 表达不出那个组合。类型名解析完就在文档根元素上，所以这道检查在建渲染器之前（建那一步会进注册表调对应元数据；清单里的类型照旧建）。成功后 `triggerRepaint()` + `setDirty(True)`，与属性对话框的 Apply 两步相同；只有这一个样式 op 是 destructive |

文档里没有的东西一律不装：符号类型、类别、断点、规则、波段、色带都在 XML 里，由 QGIS 自己读，所以能装的东西就等于注册表能建的东西（`heatmapRenderer`、第三方注册的矢量渲染器都装得上），没有本地的一份白名单。

#### processing.list_algorithms

| 项 | 内容 |
| --- | --- |
| 参数 | 无 |
| 返回 | `algorithms`（按（分组名、算法名、id）排序的 `{id, name, group}`；这是扁平投影，不是工具箱的 provider/分组树序——顺序不是判据） |
| 业务错误码 | 无 |
| 行为 | 列桌面版工具箱能看到的算法：`HideFromToolbox` 总是挡掉，`KnownIssues` 只在"显示有已知问题的算法"开关关着时挡掉（默认关着）；没有 allow-list、没有"能不能跑"的字段；`get_algorithm_info` 仍能读被挡掉的算法（与 `algorithmHelp()` 一致） |

#### processing.get_algorithm_info

| 项 | 内容 |
| --- | --- |
| 参数 | `algorithm_id`（≥1） |
| 返回 | `algorithm`（`id`、`name`、`short_description`、`help`）；`parameters[]`（`name`、`description`、`help`、`type`、`options`、`accepted_types`）；`outputs[]`（`name`、`type`、`description`） |
| 业务错误码 | `INVALID_INPUT`（算法未注册——是调用方输入错误，不是未知插件操作；消息是 `algorithmHelp()` 打印的那句 `Algorithm "<id>" not found.`） |
| 行为 | 隐藏参数直接跳过（与 `algorithmHelp()` 一致）；`options` 只有枚举才有，其余是 `null`；字段取值来自原生定义，不在这里翻译或校验 |

#### processing.run_algorithm

| 项 | 内容 |
| --- | --- |
| 参数 | `algorithm_id`（≥1）、`parameters`（以原生参数名为键的 JSON 树）、`continue_on_crs_mismatch`（StrictBool，默认 `false`；桌面版在参数 CRS 不一致时问的那句"要继续吗"，默认按钮是"否"） |
| 返回 | `algorithm_id`、`outputs`（原生键下的输出，结果图层报 id）、`messages`（`level`/`text`/`fatal`，七种级别） |
| 业务错误码 | `INVALID_INPUT`（参数不是有限 JSON：非字符串键、NaN、Infinity、bytes、QGIS/Qt 对象；未知算法 id，消息是运行路径那句 `Error: Algorithm <id> not found`（含结尾换行）；参数值过不了 QGIS 的 `checkValueIsAcceptable`；输出值过不了 `isSupportedOutputValue`；`checkParameterValues` 拒绝；CRS 不一致且 `continue_on_crs_mismatch=false`）、`INTERNAL_ERROR`（`qgis.processing.run` 抛异常、结果无无损 JSON 形式、结果图层没加进工程、结果键不是字符串） |
| 行为 | 不重新实现 QGIS，但运行前问的每个问题与桌面版窗口一样，而且都是 QGIS 自己的函数：`createAlgorithmById` 取实例，补上参数面板会交出的值（非输出参数缺席时用它自己的 `defaultValueForGui()`，输出参数没有默认值就是临时输出），逐个参数问 `checkValueIsAcceptable`、对每个有值的输出问 `isSupportedOutputValue`、问 CRS 不一致的确认（受桌面版"参数 CRS 不一致就先警告"开关约束），最后问 `checkParameterValues`；任一条不过就是 `INVALID_INPUT`，消息就是桌面版对同一问题说的那一句。通过后才把参数、一个记录消息的 feedback 与同一个 `processing.createContext(feedback)` 上下文交给 `qgis.processing.run()`。七个 `push*`/`reportError` 覆写都先调 `super()`，保留 QGIS 自己的日志，再把同一条文本记进 `messages`；未声明的参数键、远程 URI 一律原样透传；异常文本与 feedback 消息只进日志，调用方只拿到引擎的未预期失败码；结果图层会加进当前工程并报 id，结果无法返回时只移除这次调用加的那几个（算法自己的外部副作用不声称回滚） |
| 输出目标 | 输出参数是调用方的回答，缺席不是"随便"：桌面版参数面板对每个输出参数都自带一个值，默认就是临时输出，所以缺席的必填输出会被补成 `QgsProcessing.TEMPORARY_OUTPUT`（字符串，JSON 安全），可选且不默认创建的输出保持缺席（面板的"跳过输出"），显式 `None` 也是跳过输出。远程字形先过 `isSupportedOutputValue`，再由 QGIS 打开。实测：`native:buffer` + `TEMPORARY_OUTPUT`（或缺席）→ 结果图层进工程；`gdal:convertformat` + 磁盘路径 → 文件写出且 `outputs` 里就是该路径；`gdal:buffervectors` + `memory:` → `INVALID_INPUT`、消息 `This algorithm only supports disk-based outputs`（桌面版输出控件同一判据；QGIS 自己会误报 `SUCCEEDED` 并把字面量 `memory:` 当输出值）；`.zzz` 这种扩展名在桌面版会被拒，插件同样拒（不然 QGIS 会自己补成 `.zzz.gpkg` 写出去）；`/vsicurl/…` 目标过判据后由 QGIS 报错 |

### 7.7 一次调用会碰到的其它码

不看 Operation 是谁、只看怎么调的码已在 §7.4 的分组表里；这里补充三类容易误判的：

| 码 | 何时 |
| --- | --- |
| `TOO_MANY_EXECUTIONS` | `submit` 时排队数已达 `max_pending`（实测：32 时第 33 个 `submit` 得到 `FAILED`） |
| `OUTPUT_VALIDATION_FAILED` | Operation 的返回值不合它自己声明的输出模型 |
| `INTERNAL_ERROR` | 未预期异常；`message` 是下面几种之一：`<异常类型名>: unexpected failure`、`QGIS did not create a new project`、`the task ended without a result`、`the completion of this execution failed`、`<id>: <旧状态> -> <新状态> is not a transition`、`<id>: <状态> is final and cannot change` |

---

## 8. Operation 开发规范

### 8.1 放置与注册

1. 放进对应能力族的模块：`operations/project.py`、`operations/layer.py` 或
   `operations/processing.py`；新能力族才新建文件。能力模块里只装 `Params`、`Result` 与
   Operation 类，外加它自己的清单元组（`operations/processing.py` 是唯一自带元组的模块）。
2. `operations/__init__.py` 里做三件事：import 新类、把它加进对应元组、`OPERATIONS` 自然带上
   （`OPERATIONS = (*PROJECT_OPERATIONS, *LAYER_OPERATIONS, *PROCESSING_OPERATIONS)`）。
   `plugin.py` 不需要改——它只调 `register_all`。
3. 顺序保持"四个 project、十个 layer、三个 processing"的既有分组习惯；注册顺序就是
   `OPERATIONS` 的顺序，而 `list_specs()` 会按名字排序，所以顺序不影响接口面，只影响阅读。
4. 新增操作时同步改 `tests/test_package.py` 的两处断言（数量与名字集合），以及本文档的计数
   （见 §8.6 第 4 步）。这不是可选项，是守卫。

### 8.2 输入模型

```python
class LayerRenameParams(Params):
    layer_id: StrictStr = Field(min_length=1)
    name: StrictStr = Field(min_length=1, max_length=128)
```

规则：

- 一律继承 `Params`（`extra="forbid"` 已继承，不要自己再写 `model_config`）。
- 字符串用 `StrictStr`，布尔用 `StrictBool`，数字用 `int`/`float`（不要用 `StrictInt` 之外
  的宽松类型去"兼容"字符串）。没有参数的操作也建一个空模型，它负责拒绝任何键。
- 默认值写在模型里，不要在 `execute_main` 里"如果没给就……"。
- 形状校验（绝对路径、后缀、枚举值、互斥字段）放 `Annotated[..., AfterValidator(...)]` 或
  `model_validator(mode="after")`，让错误变成 `INVALID_INPUT` 而不是到 GIS 层才炸。
- 能用纯校验表达的，不要进 `execute_main`；能进 `execute_main` 前置检查的（比如 CRS 是否有效、
  路径是不是绝对的），不要留到改动状态之后。
- 模型的校验函数只做校验，不做 I/O、不读环境、不依赖调用顺序。
- 后台操作与主线程操作一样：入参在 `submit` 返回前就校验完，被拒的调用不会占排队名额。

### 8.3 输出模型

```python
class LayerRenameResult(Result):
    layer_id: StrictStr
    name: StrictStr
```

规则：

- 只放 JSON 能表达的值：`str`、`bool`、`int`、`float`（有限）、`None`、list/dict。
- 不放 QGIS 对象、provider URI、连接串、要素、整张字段表、traceback。
- 一律继承 `Result`（`extra="forbid"` 已继承，不要自己再写 `model_config`，也不要直接继承
  `BaseModel`）。
- 可选值用 `| None` 并给默认值；不要用 `""` 冒充"没有"（例外：`crs_authid` 在工程没有 CRS 时
  就是 `""`，这是 QGIS 的返回值）。
- 结果里的计数字段说清楚口径（例如 `count` 是参与求和的非 null 要素数，不是要素总数）。

### 8.4 主线程 Operation

骨架：

```python
class LayerRename(
    BaseCommand[LayerRenameParams, LayerRenameResult],
    MainThreadOperationMixin[LayerRenameParams, LayerRenameResult],
):
    name = "layer.rename"
    description = "Rename one layer in the current project"
    input_model = LayerRenameParams
    output_model = LayerRenameResult
    # destructive 默认 False；只有可能丢用户数据时才写 True

    def execute_main(self, ctx: OperationContext, request: LayerRenameParams) -> LayerRenameResult:
        # QGIS 只在函数体内 import；这里用不到具体类型，就不用 import
        layer = ctx.project.mapLayer(request.layer_id)
        if layer is None:
            raise FrameworkError(LAYER_NOT_FOUND, "layer does not exist")
        layer.setName(request.name)
        return LayerRenameResult(layer_id=layer.id(), name=layer.name())
```

要求：

- 用 `ctx.project`，不要用 `QgsProject.instance()`：引擎持有会话的工程句柄，headless 与测试里
  它是替身。
- 不要自己弹对话框、不要调 `QMessageBox`/`QFileDialog`/`.exec()`；一切给调用方看的信息都通过
  返回值或 `FrameworkError` 表达（`project.open` 因此用 `project.read()` 而不是
  `iface.addProject()`）。
- 主线程操作的"业务失败"用 `FrameworkError(<已知码>, <英文短句>)`；不要抛裸异常来表达可预期的
  错误（裸异常会变成 `INTERNAL_ERROR`，调用方无法分支）。
- 不要捕获 `FrameworkError` 再包装成别的码；引擎会原样保留。
- 返回 `output_model` 的实例。dict 也能通过 `model_validate`，但那等于放弃类型检查，别这么做。
- 操作实例是注册时创建一次、之后无状态复用：不要在 `self` 上放请求相关的状态。

### 8.5 后台 Operation

```python
@dataclass(frozen=True)
class FieldValues:  # 主线程拷出来的快照：纯 Python 数据
    layer_id: str
    field_name: str
    values: tuple[float, ...]
    skipped_null: int
    project_epoch: int


@dataclass(frozen=True)
class FieldSum:  # worker 返回的中间结果：同样只有纯数据
    count: int
    total: float
    skipped_null: int
    project_epoch: int


class LayerSumNumericField(
    BaseQuery[LayerSumNumericFieldParams, LayerSumNumericFieldResult],
    BackgroundOperationMixin[
        LayerSumNumericFieldParams, LayerSumNumericFieldResult, FieldValues, FieldSum
    ],
):
    name = "layer.sum_numeric_field"
    description = "Sum one numeric field of a vector layer over a copied snapshot"
    input_model = LayerSumNumericFieldParams
    output_model = LayerSumNumericFieldResult

    def prepare_main(self, ctx, request) -> FieldValues: ...  # 读 QGIS，拷数据
    def execute_worker(self, snapshot, control) -> FieldSum: ...  # 不碰 QGIS
    def finalize_main(self, ctx, request, result) -> LayerSumNumericFieldResult: ...
    def dependent_layer_ids(self, request) -> list[str]: ...  # 可选
```

三个阶段的硬规则：

| 阶段 | 线程 | 可以做 | 不可以做 |
| --- | --- | --- | --- |
| `prepare_main` | 主线程 | 读 `ctx.project`、读要素、读字段、算小规模前置统计、把状态拷成纯数据 | 把 QGIS 对象放进快照；做没有取消检查的长循环 |
| `execute_worker` | Qt 线程池 | 纯 Python 计算；用 `control.is_cancelled()` / `control.set_progress()` | 碰 `ctx`、碰 `QgsProject`、碰任何 QGIS/Qt 对象；假设自己在主线程 |
| `finalize_main` | 主线程 | 比对 epoch、把 worker 结果包装成 `output_model`、必要时做少量主线程校验 | 写工程状态（本框架的后台操作都是 query）；重新读一遍大数据 |

其它要求：

- 快照必须是**纯数据**（`dataclass(frozen=True)` + 基本类型 / tuple），因为它是跨线程传递的；
  不要传 `QgsFeature`、`QgsGeometry`、`NULL` 这类对象。
- 快照里带上 `project_epoch`，`finalize_main` 里与 `ctx.lifecycle.current_epoch` 比对；不一致抛
  `STALE_PROJECT`。适配器在落地前也会比对一次，两处都要有（一处防"算完了工程已换"，一处防
  "调用方忘了"）。
- 拷贝要素时也要看取消，不要把一个几十万要素的列一口气读完；上限类阈值不要再发明（要素数就是没有上限，实测 100 万要素 3.14 s）。
- 长循环里周期性检查取消并报进度；不要在每个元素上都报进度（适配器有 100 ms 节流，但 worker
  自己也不该制造无意义的调用）。进度不要主动写 100，成功由适配器写。
- `execute_worker` 里可以抛 `FrameworkError` 表达业务失败（适配器保留错误码，例如求和溢出为
  `inf` 时抛 `INVALID_FIELD_TYPE`）；其它异常会被记成 `INTERNAL_ERROR`。
- 结果里不得有 `NaN`/`Infinity`：它们不是合法 JSON。逐项检查（`math.isfinite`）并拒绝，不要
  把坏值交给记录层。
- 声明 `dependent_layer_ids` 后，图层被删时 QGIS 会取消任务；没有依赖就别声明。

### 8.6 新增 Operation 的完整流程

以新增一个主线程 `layer.rename` 为例：

1. 从 `develop` 切分支：`git switch -c feature/layer-rename develop`。
2. 在 `operations/layer.py` 写 `LayerRenameParams`、`LayerRenameResult`、`LayerRename`。
3. 在 `operations/__init__.py` 的 import 行加类名，再把它追加进 `LAYER_OPERATIONS` 元组。
4. 改 `tests/test_package.py`：数量从 17 改成 18，名字集合加 `"layer.rename"`，否则守卫会红。
   同一处把 `metadata.txt` 的 `description`/`about` 里的计数改掉（现在是 "seventeen"），并搜一遍
   本文档的计数（§1 的表、§3.1 的目录树注释、§7.6 的契约），否则文字和代码开始互相矛盾。
5. 写测试（见 §9）：至少覆盖成功路径、一个输入校验失败、一个业务失败（`LAYER_NOT_FOUND`）。
   如果操作有路径参数，再加一个相对路径被拒的用例。
6. 如果是后台操作，`test_qgs_task.py`/`test_background.py` 的假适配器模式可直接复用；补一个
   "换代 → `STALE_PROJECT`" 与一个"取消 → `CANCELLED`"的用例。
7. `npm run lint`、`npm run test:unit` 全绿。若操作触到真实 PyQGIS 行为，在
   `tests/qgis_integration/cases.py` 加一条验收用例（用 `fixtures.py` 生成的数据，不碰用户工程）。
8. `npm run deploy`，重启 QGIS，在坞窗里手工跑一次，确认表单、结果、错误提示都对。
9. 提交并按 §11 合并；合并后删分支。

对照检查清单：

- [ ] 名字符合 `^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$`，且描述是一行英文。
- [ ] 输入继承 `Params`；严格类型；默认值在模型里。
- [ ] 输出只含 JSON 安全值；新模型带 `extra="forbid"`。
- [ ] QGIS/Qt 只在函数体内 import（`ui/dock.py` 除外）。
- [ ] 路径参数只走 `absolute_path`，不自己做存在性预检。
- [ ] 可预期失败用 `FrameworkError` + 既有错误码。
- [ ] 后台操作：快照纯数据、带 epoch、执行中检查取消、结果无 NaN/Inf。
- [ ] 新增了模块的话，它已加进 `tests/test_package.py` 的导入列表。
- [ ] `metadata.txt` 与本文档里的 Operation 计数已同步。
- [ ] 测试覆盖成功、输入失败、业务失败；需要真实 QGIS 的部分进验收跑。
- [ ] `npm run check`（lint + 格式 + 规范组）与 `npm run test`（全量）通过；碰了真实 PyQGIS
      行为时再加验收跑。

---

## 9. 测试

细节在 `TEST.md`：两层怎么跑、假对象怎么写、坞窗与裸解释器守卫、验收跑的目录守卫与退出码。
这里只留两条规则和一份检查点。

### 9.1 两层都要跑

| 层 | 命令 | 证明什么 |
| --- | --- | --- |
| 单元测试 | `npm run test`（= `scripts\pytest.ps1` → `-m pytest -q tests`） | 契约、状态机、适配器记账；用假 QGIS / 假任务管理器 / 列表式 poster |
| 验收跑 | `python-qgis-ltr.bat tests\qgis_integration\run_acceptance.py` | 真实 PyQGIS 行为：headless `QgsApplication`、真 `QgsProject`/`QgsTaskManager`、现场生成的数据 |

两条都要跑：单元测试证明契约，验收跑证明真实 QGIS 里也成立。不要用替身断言真实 PyQGIS 行为
（`read()` 是否真的填充图层树、`QgsTask` 是否真的被调度），也不要用验收跑替代针对边界条件的
单测。

### 9.2 检查点

- 新操作覆盖成功路径、一个输入校验失败、一个业务失败；有路径参数再加一个相对路径的用例。
- 后台操作补一条"换代 → `STALE_PROJECT`"与一条"取消 → `CANCELLED`"。
- 断言落在错误码、字段值、调用次数上；不断言日志文本与私有属性。
- 触到真实 PyQGIS 行为的部分进 `tests/qgis_integration/cases.py`，用 `fixtures.py` 现场生成数据，
  不碰用户工程。
- 新用例先在缺陷存在时确认是红的。

---

## 10. 界面约定（坞窗与主题）

坞窗是**手工测试工具**，不是框架的一部分。它只认两样东西：注册表里的 `OperationSpec` 和引擎的
五个方法；不按操作名写分支，所以新增操作不需要改坞窗就能出现。

界面自上而下（括号里的东西是它读的）：

| 区域 | 内容 |
| --- | --- |
| 空态 | 注册表为空时，选择器显示"没有可用的操作"，结果区换成一段教学文字（为什么为空、先看哪里） |
| 选择器 | 左边 `name`，右边等宽小字 `kind · strategy`；`destructive=true` 带警示色点，选中后摘要行标"可破坏" |
| 摘要 | 类型/策略一行（等宽小字），下面是 `description`（次要色，自动换行） |
| 参数 | 由 `input_schema` 生成，一字段一行：标签（次要色，有中文映射表）+ 编辑器；必填字段标签后有小号"必填" |
| 动作条 | 主按钮"运行"（强调色，全坞窗唯一）、次按钮"取消"（仅后台调用运行中可点）、文字按钮"复制 JSON"（整条记录）、右侧状态胶囊 |
| 状态胶囊 | `就绪 / 运行中 / 完成 / 失败 / 已取消 / 无操作`；运行中下方有进度条 |
| 错误区 | 只在失败时出现：等宽的 `CODE` + 一句"发生了什么" + 一句"接下来怎么办"；引擎原文只放在鼠标悬停提示里 |
| 结果区 | 没记录时是一句话（该跑一次 / 本次没有产生记录）。有记录时是三个标签页：`执行信息`（`result` 以外的每个字段，两列等宽值；没有值的字段整行不出现）/ `结果`（`result` 的 JSON；失败与取消时没有这一页）/ `原始 JSON`（整条记录）。上方一行小字：运行中给 `进度 n%`，结束给 `耗时 n ms`（取消过就追加回执） |

入口属于界面的一半：`initGui` 把坞窗挂进右侧，同时在插件菜单下挂一项、工具栏加一个开关，两者
都指向同一个坞窗。关掉的坞窗只是隐藏，入口把它要回来——不重建，记录还在。界面承载不了入口
（无头验收的假 `iface`）时只建坞窗。

两条属于坞窗的合同：

- **留空 = 不提交，用模型默认值**。文本/数字/JSON 字段清空就不提交，schema 里的 `default` 以灰色
  提示文字显示在空框里；下拉框第一项是 `默认（值）`。两个例外：勾选框始终提交自己的状态；
  数组/对象填 JSON 文本，解析失败时错误区给 `INVALID_INPUT` 且**不调用引擎**。
- **路径参数写相对路径会被拒**（`PATH_NOT_ALLOWED`），错误区提示给一条带盘符的完整路径。

键盘：`Ctrl+Return`/`Ctrl+Enter` 运行；`Tab` 顺序是 选择器 → 各参数 → "运行"；运行期间选择器
与参数禁用，后台调用可点"取消"。

其它必须遵守：

- 只读 `OperationSpec` 的 `input_schema` 生成表单。`boolean` → 复选框、`integer`/`number` →
  带校验的输入框、`array`/`object` → JSON 文本、其它 → 单行文本，`enum` → 下拉框。
- 提示文本是中文，字段标签有中文映射表（没映射的用 schema 的 `title`），错误码有"发生了什么 +
  下一步怎么做"的对照表，引擎原文放进 tooltip。
- 主线程操作用 `run_sync`；后台操作 `submit` 后每 200 ms（`POLL_MS`）`get` 一次，到终态停止；
  关闭坞窗时先停轮询再让引擎继续卸载。
- 颜色只有一个来源：`ui/theme.py`。它从 `QApplication.palette()` 的角色派生颜色，遵守 WCAG
  对比度下限（正文 4.5、大字号与状态胶囊 3.0），浅色/深色主题共用一份 QSS 模板。**任何地方
  都不写十六进制颜色**（`tests/test_source_rules.py` 扫 `ui/` 的字符串常量）；语义色
  （ok/warn/danger）只用于状态胶囊与错误条，不用于装饰。
- `ui/dock.py` 可以模块级 import Qt，但**不得**模块级 import qgis；`ui/theme.py` 连 Qt 都只在
  函数体内 import；`ui/__init__.py` 保持空壳。

---

## 11. 提交与分支流程

Git flow：

| 分支 | 从哪切 | 合到哪 |
| --- | --- | --- |
| `feature/<name>`、`fix/<name>`、`docs/<name>` | `develop` | `develop` |
| `release/<version>` | `develop` | `main`，然后回 `develop` |
| `hotfix/<name>` | `main` | `main`，然后进 `develop` |

一次改动落地的方式：

1. `git switch -c feature/<name> develop`。
2. 在分支上提交；合并时用 `--no-ff`，让分支在 `git log` 里可见。
3. 合进 `develop` 后删除分支。
4. 发布时把 `develop` 以 `--no-ff` 合进 `main` 并推送。

完成的标准：改动通过合并提交进入 `develop`（`git log --merges -1 develop`），承载它的分支已
删除。

---

## 12. 已知边界与约束

这些是当前实现的真实边界，不是待办清单；踩到它们时应先确认是不是设计如此。每条为什么这么定、被拒过
哪些做法，见 `docs/adr/`；本表只记"现在是什么"。

| 边界 | 现状 | 影响 |
| --- | --- | --- |
| 路径策略 | 只要求绝对路径，没有根目录白名单 | 操作能读写进程能读写的任何位置；调用方是 agent 时这就是一次能力决定，不是遗漏 |
| 远程图层 | `layer.add` 接受 `/vsi…` 的绝对字形，也接受 provider 自己能解的其它源 | URL（`https://…`）不算 Windows 绝对路径，因此被拒；要加远程支持是新的设计，不是放宽一条规则 |
| 工程内的数据源 | 不判：根外路径、目录、不存在的源、远程源都随工程读进来 | 加载不出来的层只让 `invalid_layer_count` 变大 |
| `project.open` 的深度守卫 | 只拒两种实测能杀掉进程的输入：``read()`` 会解析的那个文档嵌套超过 `4096` 层、`.qgz` 里有加密成员 | 深度只数归档里第一个 `.qgs`（`read()` 只解析它）；其余形状（远程源、实体、体积、标签数、成员数、压缩方式、CRC）一律不判，交给 `read()`；代价是一个远程源的工程会在主线程上取一次远端 |
| 未保存工程的判断 | GUI 的四道问句都要查：非 memory 矢量图层的 `isEditable() && isModified()`、带要素的 memory 图层或临时图层、`is_dirty()`、栅格波段的 RAT `isDirty()` | 任一条成立就要 `discard_current=true`；图层数与关联文件名不是条件，一个没改过的工程（哪怕加了图层）照样被换掉 |
| `project.save` 的覆盖判断 | 只有给了 `path`（Save As）才查 `TARGET_EXISTS` | 写自己的关联文件不需要 `overwrite`，与 GUI 的 Save 一致 |
| `project.create` 之后的 CRS 与 dirty | `crs_authid` 不给就不动 QGIS 给的 CRS；最后一律 `setDirty(false)` | 与 `QgisApp::fileNew` 的两步一致；`is_dirty` 在新建后是 `false` |
| 后台结果的口径 | 是快照，不是结束时的实时值 | 期间有人编辑，返回的仍是旧数 |
| 进度 | 最多每 100 ms 写一次；从不写 100 除成功 | 记录里的 `RUNNING` 可能来自完成路径补写 |
| 记录保留 | 按条数与 TTL，在访问与写入终态时 prune | 超时记录在被访问前仍占内存；被访问时已经不存在 |
| 取消的语义 | 协作式；`CANCEL_REQUESTED` 只有任务自己的报告才变 `CANCELLED` | 主线程已开始的操作不可中断 |
| 后台名额的计数口径 | 适配器数的是自己活动表里的任务（`SCHEDULED`/`RUNNING`/`CANCEL_REQUESTED`），引擎里名为 `_ACTIVE_BACKGROUND` 的集合只有前两个 | 两者不同义：限流看适配器，发取消请求看引擎；`FINALIZING` 不占名额 |
| 依赖版本 | pydantic 只支持 2.x（导入时拒绝其他 major），QGIS 3.44（`metadata.txt`） | 换 pydantic major 或 QGIS 大版本前要重新验证；非 2.x 是加载期 `RuntimeError`，不是运行期错误 |
| 测得的数字 | 本文档里的耗时、崩溃码都是在 QGIS 3.44.13 / Windows x64 上测的 | 它们支撑判据（尤其是 §7.2 那两条 P3），不是承诺；换环境要重测再改阈值 |
