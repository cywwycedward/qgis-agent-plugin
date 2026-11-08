# qgis-agent-plugin

QGIS Agent 插件本体。构建/部署/测试入口见 `package.json`。

## 目录

三层，箭头单向：契约与管线 → 操作 → 入口。

| 位置 | 放什么 |
| --- | --- |
| `src/qgis_agent_plugin/core/` | 契约与管线：`contracts`（Operation/Command/Query 与两个执行策略的协议）、`models`（DTO、状态枚举、错误码）、`registry`、`engine`、`execution_store`、`context`（工程代数 `project_epoch` 与主线程判定）、`config`（PRD 10.1 的七项配置）、`security`（允许目录与路径规范化）、`runners/`（`main_thread` 事件队列、`qgs_task` 后台适配） |
| `src/qgis_agent_plugin/operations/` | 操作：一个文件一个能力族，装着它的 `Params`、`Result` 和 Operation 类，外加自己的清单元组；`__init__.py` 只做汇总（`register_all`） |
| `src/qgis_agent_plugin/__init__.py` | 入口：QGIS 按名字在包上找的 `classFactory`（它只在被调用时才 import `plugin`） |
| `src/qgis_agent_plugin/plugin.py` | 会话：`initGui`/`unload`（PRD 9.3 六步）、`Supervisor`（卸载后仍活着的任务的托管），以及坞窗的建/挂/卸 |
| `src/qgis_agent_plugin/ui/dock.py` | 手工测试坞窗：列出 `OperationSpec`、按 `input_schema` 生成表单、`run_sync` 或 `submit`+轮询、把 `ExecutionRecord` 显示成 JSON（用户新增的测试工具，不在 PRD 里）。`OperationDock` 是 `QDockWidget` 子类——`iface.addDockWidget`/`QMainWindow.addDockWidget` 只接受这个类型 |
| `src/qgis_agent_plugin/ui/theme.py` | 坞窗的唯一配色来源：从 `QApplication.palette()` 角色算出文字/边框/强调/语义色，生成 QSS，并提供对比度自检（浅色与深色主题都成立）。不写任何十六进制颜色 |
| `src/qgis_agent_plugin/metadata.txt` | 插件元数据（QGIS 插件管理器读的那个） |

放置规则：

1. 一个能力族一个文件；领域模型跟它的领域走（`LayerSummary` 在 `operations/layer.py`，project 的操作也用它的 `layer_count`）。
2. 框架级 DTO、状态枚举、错误码只在 `core/models.py`；QGIS/Qt 对象永远不进 DTO。
3. 被两个以上能力共享、又不属任何领域的规则放 `core/`（路径白名单是 `core/security.resolve_path`，工程代数与线程判定是 `core/context`）。
4. `core/contracts.py` 与 `core/engine.py` 的公开签名是冻结面：后台只有 `prepare_main`/`execute_worker`/`finalize_main` 三个阶段，执行策略只有两个。要加第四个阶段或第三个策略，先改 PRD。

导入层，由 `tests/test_package.py` 在无 QGIS 的子进程里强制：

- `core/*`、`operations/*`、`plugin.py` 必须能在裸解释器导入：QGIS **只能在函数体内 import，没有例外**（`plugin.initGui`/`unload`/`application_supervisor`/`read_settings`、`qgs_task.task_class`、`main_thread._post_to_event_queue`、各操作需要它的那一步）。
- Qt 同样默认在函数体内 import；**模块级** Qt import 的许可只给 `plugin.py`、`core/runners/*` 与 `ui/dock.py`。目前只有 `ui/dock.py` 真的用了它（坞窗要继承 `QDockWidget`，在函数体内建一个类只是绕路）；`plugin.py` 与 runners 仍写在函数体内。`core/` 其余文件与 `operations/*` 只能在函数体内 import——守卫强制的是「模块级 import 会让裸解释器崩」，不是「文件里不许出现 QGIS/Qt 的 import」。
- `ui/dock.py` 可以模块级 import Qt，**不得**模块级 import qgis：它只认识注册表、引擎和 Qt，前两者与 QGIS 无关。`ui/__init__.py` 保持空壳（只有 docstring），所以裸解释器永远不会顺着包入口碰到 Qt。`tests/test_package.py` 因此**读 `ui/dock.py` 的源码**守这条规则（任何 `import qgis` / `from qgis.xxx` 都算违规，`from qgis.PyQt...` 除外）：import 之后查 `sys.modules` 不行——QGIS 解释器里 import `qgis.PyQt` 这个动作本身就把 `qgis.core`/`qgis.gui` 带进来了，那样的断言永远不会失败（F10 评审 r1）。Qt 存在时它还额外断言 `OperationDock` 是 `QDockWidget` 的子类。
- 结果与错误只出 Pydantic DTO / 基本类型；绝不返回 `QgsMapLayer`、`QgsProject`、QObject、Qt 枚举或 traceback。
- 守卫同时断言九个 Operation 能在裸解释器里注册成功（PRD 7.1）。

`tests/` 镜像机制层：`test_{contracts,models,registry,security}`（契约）、`test_{context,engine,execution_store}`（管线与状态机）、`test_{operations_project,operations_layer}`（操作）、`test_{qgs_task,background}`（后台适配）、`test_plugin_lifecycle`（加载/卸载、工程换代、Supervisor）、`test_ui_dock`（坞窗表单、运行、生命周期；Qt offscreen，裸解释器里整模块 skip）、`test_package`（导入层）。

运行时的两个观察面：Python Console 里 `iface.agent_registry` / `iface.agent_engine`（插件加载期间存在，`unload` 撤掉）；配置从 `QgsSettings` 的 `qgis_agent_plugin/<键名>` 读，默认七项见 `core/config.py`。

## Git branches

Git flow. `main` is the releasable line and moves only by merging `develop` into it; `develop` is the integration line and takes no commits of its own, only merges of a branch cut from it.

| Branch | Cut from | Merged into |
| --- | --- | --- |
| `feature/<name>`, `fix/<name>`, `docs/<name>` | `develop` | `develop` |
| `release/<version>` | `develop` | `main`, then back into `develop` |
| `hotfix/<name>` | `main` | `main`, then into `develop` |

A change lands like this:

1. Cut a branch from `develop`: `git switch -c feature/<name> develop`.
2. Commit there, and merge with `--no-ff` so the branch stays visible in `git log`.
3. Merge it into `develop` and delete the branch.
4. For a release, merge `develop` into `main` (`--no-ff`) and push `main`.

Done means the change is on `develop` through a merge commit (`git log --merges -1 develop`) and the branch that carried it is deleted.

## Agent skills

### Issue tracker

GitHub Issues via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Five state roles + two categories, label strings equal to role names. See `docs/agents/triage-labels.md`.

### Domain docs

single-context: `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
