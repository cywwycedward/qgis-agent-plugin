# qgis-agent-plugin

QGIS Agent 插件本体。本文件只做索引与摘要，规范在 `docs/agents/` 下，动手前读对应那份。

| 文档 | 何时读 | 内容 |
| --- | --- | --- |
| `docs/agents/DEVELOP.md` | 动手改代码之前（唯一的开发规范） | 分层与导入规则、代码与注释规范、协议与状态机、十七个 Operation 的契约、错误码、Git、已知边界 |
| `docs/agents/TEST.md` | 写或跑测试时 | 两层测试、假对象写法、坞窗与裸解释器守卫、验收跑的目录守卫与退出码 |
| `docs/agents/ENVIRONMENT.md` | 跑任何 Python、装依赖、找路径之前 | 两个解释器各自的职责、依赖现状、uv 的坑、路径与版本 |
| `docs/agents/DELEGATION.md` | 要把活派给别的 agent 时 | `hc` + `herdr-app`、一个功能的五步、交接文档命名 |
| `GLOSSARY.md` | 命名任何东西之前（Operation、issue 标题、测试名、注释里的术语） | 领域词汇的正牌说法与要避免的同义词；查不到的词说明是在自造语言 |
| `docs/adr/` | 想问"为什么这么定"，或想改一条既有边界时 | 已成文的架构决定：路径无白名单、冻结执行协议、协作式取消、读前扫描 |

构建、部署、测试入口都在 `package.json`：`npm run deploy` 把 `src/qgis_agent_plugin` 镜像进
QGIS profile。

## 改完代码必跑

```powershell
npm run check      # lint + 格式 + 规范组测试（12 条，约 5 s）
```

规范组断言的是全仓规则（注释双语、导入位置、文档索引、注册面），**每次改完都要绿**。其余按需：
`npm run test`（全量 721）、裸解释器 `pytest`（导入层）、`run_acceptance.py`（真实 QGIS，93 条）。
判据、按改动类型的对应表、以及"裸解释器少 3 个用例属预期"这类坑，见 `docs/agents/DEVELOP.md` §2.3。

## 目录

三层，箭头单向：契约与管线 → 操作 → 入口。

| 位置 | 放什么 |
| --- | --- |
| `src/qgis_agent_plugin/core/` | 契约与管线：`contracts`（Operation/Command/Query 与两个策略的协议）、`models`（DTO、状态枚举、23 个错误码）、`registry`、`engine`、`execution_store`（状态机）、`context`（工程代数 `project_epoch` 与主线程判定）、`config`（四项配置）、`security`（只要求绝对路径）、`runners/`（`main_thread` 事件队列、`qgs_task` 后台适配） |
| `src/qgis_agent_plugin/operations/` | 十七个操作，一个文件一个能力族：`project.py`（4）、`layer.py`（10）、`processing.py`（3）。每个模块装自己的 `Params`、`Result` 与 Operation 类；`__init__.py` 只汇总（`register_all`） |
| `src/qgis_agent_plugin/__init__.py` | 入口：QGIS 按名字在包上找的 `classFactory`（只在被调用时才 import `plugin`） |
| `src/qgis_agent_plugin/plugin.py` | 会话：`initGui`／`unload`、`Supervisor`（卸载后仍活着的任务的托管）、坞窗的建／挂／卸、pydantic 主版本闸门 |
| `src/qgis_agent_plugin/ui/dock.py` | 手工测试坞窗：列出 `OperationSpec`、按 `input_schema` 生成表单、`run_sync` 或 `submit`+轮询、把 `ExecutionRecord` 显示成 JSON。`OperationDock` 是 `QDockWidget` 子类——`addDockWidget` 只接受这个类型 |
| `src/qgis_agent_plugin/ui/theme.py` | 坞窗的唯一配色来源：从 `QApplication.palette()` 的角色算出颜色与 QSS，并提供对比度自检。不写任何十六进制颜色 |
| `src/qgis_agent_plugin/metadata.txt` | 插件元数据（QGIS 插件管理器读的那个），QGIS 版本上下限在这里 |

放置规则（完整版见 `docs/agents/DEVELOP.md` §4）：一个能力族一个文件；框架级 DTO、状态枚举、错误码
只在 `core/models.py`；被两个以上能力共享、又不属任何领域的规则才进 `core/`；`contracts.py` 与
`engine.py` 的公开签名是冻结面（两个策略、三阶段后台，加第四个必须先改设计）。

## 导入层

两条守卫：`tests/test_package.py`（子进程里真导入，证明裸解释器能加载）与
`tests/test_source_rules.py`（读源码，证明没人把 import 放错位置）。

- `core/*`、`operations/*`、`plugin.py` 必须能在裸解释器 import：**QGIS 只能在函数体内 import**。
- Qt 的模块级许可只给 `plugin.py`、`core/runners/*`、`ui/dock.py`；`ui/dock.py` 只能从 `qgis.PyQt`
  取，不得 import `qgis.core`／`qgis.gui`；`ui/__init__.py` 保持空壳（只有 docstring）。
- 结果与错误只出 Pydantic DTO／基本类型；不返回 `QgsMapLayer`、`QgsProject`、QObject、Qt 枚举或
  traceback。

## 测试结构

`tests/` 镜像机制层：`test_{contracts,models,registry,security}`、`test_{context,engine,execution_store}`、
`test_operations_{project,layer,processing}`、`test_{qgs_task,background}`、`test_plugin_lifecycle`、
`test_ui_dock`（Qt offscreen，裸解释器里整模块 skip）、`test_package`（裸解释器守卫）；
`test_comments`／`test_source_rules`／`test_docs` 把 `docs/agents/` 里的成文规则变成断言；
`tests/qgis_integration/` 是 headless 验收跑，不是 pytest 用例。

运行时的两个观察面：Python Console 里 `iface.agent_registry` / `iface.agent_engine`（插件加载期间
存在，`unload` 撤掉）；配置从 `QgsSettings` 的 `qgis_agent_plugin/<键名>` 读，四项见
`docs/agents/DEVELOP.md` §6.12。

## Git branches

Git flow；分支表与发布路径见 `docs/agents/DEVELOP.md` §11。一次改动：从 `develop` 切分支 →
提交 → `git merge --no-ff` 进 `develop` → 删分支；`develop` 只靠合并前进，不直接提交。

## Agent skills

### Delegation (`hc` + `herdr-app`)

How to hand work to other agents, how they exchange results, and the five steps a feature goes through.
See `docs/agents/DELEGATION.md`.

### Issue tracker

GitHub Issues via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Five state roles + two categories, label strings equal to role names. See `docs/agents/triage-labels.md`.

### Domain docs

single-context: `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
