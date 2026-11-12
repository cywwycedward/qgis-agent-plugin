# qgis-agent-plugin

QGIS Agent 插件本体。契约、协议与写法以 `qgis\DEVELOP.md` 为准（插件的上一层，不在本仓库里），
本文件只做目录索引与摘要；构建、部署、测试入口见 `package.json`。

## 目录

三层，箭头单向：契约与管线 → 操作 → 入口。

| 位置 | 放什么 |
| --- | --- |
| `src/qgis_agent_plugin/core/` | 契约与管线：`contracts`（Operation/Command/Query 与两个策略的协议）、`models`（DTO、状态枚举、25 个错误码）、`registry`、`engine`、`execution_store`（状态机）、`context`（工程代数 `project_epoch` 与主线程判定）、`config`（七项配置）、`security`（允许目录与路径规范化）、`runners/`（`main_thread` 事件队列、`qgs_task` 后台适配） |
| `src/qgis_agent_plugin/operations/` | 十七个操作，一个文件一个能力族：`project.py`（4）、`layer.py`（10）、`processing.py`（3）。每个模块装自己的 `Params`、`Result` 与 Operation 类；`__init__.py` 只汇总（`register_all`） |
| `src/qgis_agent_plugin/__init__.py` | 入口：QGIS 按名字在包上找的 `classFactory`（只在被调用时才 import `plugin`） |
| `src/qgis_agent_plugin/plugin.py` | 会话：`initGui`／`unload`、`Supervisor`（卸载后仍活着的任务的托管）、坞窗的建／挂／卸、pydantic 主版本闸门 |
| `src/qgis_agent_plugin/ui/dock.py` | 手工测试坞窗：列出 `OperationSpec`、按 `input_schema` 生成表单、`run_sync` 或 `submit`+轮询、把 `ExecutionRecord` 显示成 JSON。`OperationDock` 是 `QDockWidget` 子类——`addDockWidget` 只接受这个类型 |
| `src/qgis_agent_plugin/ui/theme.py` | 坞窗的唯一配色来源：从 `QApplication.palette()` 的角色算出颜色与 QSS，并提供对比度自检。不写任何十六进制颜色 |
| `src/qgis_agent_plugin/metadata.txt` | 插件元数据（QGIS 插件管理器读的那个），QGIS 版本上下限在这里 |

放置规则（完整版见 `qgis\DEVELOP.md` §4）：一个能力族一个文件；框架级 DTO、状态枚举、错误码只在
`core/models.py`；被两个以上能力共享、又不属任何领域的规则放 `core/`；`contracts.py` 与
`engine.py` 的公开签名是冻结面（两个策略、三阶段后台，加第四个必须先改设计并同步 `qgis\DEVELOP.md`）。

## 导入层摘要

`tests/test_package.py` 在无 QGIS 的子进程里强制这些规则：

- `core/*`、`operations/*`、`plugin.py` 必须能在裸解释器 import：**QGIS 只能在函数体内 import**。
- Qt 默认也在函数体内；模块级 Qt 的许可只给 `plugin.py`、`core/runners/*` 与 `ui/dock.py`
  （目前只有 `ui/dock.py` 真的用了）。`ui/dock.py` 可以模块级 import Qt，**不得**模块级 import
  qgis；`ui/__init__.py` 保持空壳。守卫因此读 `ui/dock.py` 的源码，而不是查 `sys.modules`。
- 结果与错误只出 Pydantic DTO／基本类型；绝不返回 `QgsMapLayer`、`QgsProject`、QObject、Qt 枚举
  或 traceback。
- 守卫同时断言十七个 Operation 能在裸解释器里注册成功，且每个的 `kind`/`strategy`/`destructive`
  与 `qgis\DEVELOP.md` §1 的表一致。

`tests/` 镜像机制层：`test_{contracts,models,registry,security}`、`test_{context,engine,execution_store}`、
`test_operations_{project,layer,processing}`、`test_{qgs_task,background}`、`test_plugin_lifecycle`、
`test_ui_dock`（Qt offscreen，裸解释器里整模块 skip）、`test_package`；`tests/qgis_integration/` 是
headless 验收跑，不是 pytest 用例。

运行时的两个观察面：Python Console 里 `iface.agent_registry` / `iface.agent_engine`（插件加载期间
存在，`unload` 撤掉）；配置从 `QgsSettings` 的 `qgis_agent_plugin/<键名>` 读，七项见 `qgis\DEVELOP.md` §6.12。

## Git branches

Git flow；完整说明见 `qgis\DEVELOP.md` §11。

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

Done means the change is on `develop` through a merge commit (`git log --merges -1 develop`) and the
branch that carried it is deleted.

## Agent skills

### Delegation (`hc` + `herdr-app`)

How to hand work to other agents, how they exchange results, and the five steps a feature goes through.
See `docs/agents/delegation.md`.

### Issue tracker

GitHub Issues via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Five state roles + two categories, label strings equal to role names. See `docs/agents/triage-labels.md`.

### Domain docs

single-context: `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
