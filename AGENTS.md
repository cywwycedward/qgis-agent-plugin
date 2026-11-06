# qgis-agent-plugin

QGIS Agent 插件本体。构建/部署/测试入口见 `package.json`。

## 目录

三层，箭头单向：契约与管线 → 命令 → 界面。

| 位置 | 放什么 |
| --- | --- |
| `src/qgis_agent_plugin/*.py` | 契约（`command`）、协议（`protocol`）、管线（`dispatch`）、异步操作（`operations`）、Qt 适配（`jobs`）、传输（`socket_bridge`）、插件入口（`plugin`） |
| `src/qgis_agent_plugin/commands/` | 命令：一个文件一个命令，装着它的 `Params`、`Result` 和 `Command` |
| `src/qgis_agent_plugin/ui/` | 坞窗与两个页面，唯一有窗口的地方 |

放置规则：

1. 一个命令一个文件；同能力的共享规则放 `<能力>/shared.py`，领域模型跟它的领域走（图层模型在 `commands/layer/report.py`，即便 project 的命令也要用它）。
2. 被两个以上能力共享、又不属任何领域的规则，放 `commands/shared.py`。
3. 包根只放不是命令、也不是界面的东西。

导入层，由 `tests/` 在无 QGIS 的解释器里强制：契约、管线、命令声明必须能在裸解释器导入；QGIS 只能在函数体内 import（`plugin.context()`、各命令的 `run`、对 `jobs` 的引用）；只有 `plugin.py`、`jobs.py`、`ui/` 允许模块级 import Qt。

`tests/` 镜像机制层：`test_dispatch`、`test_operations`、`test_protocol`、`test_package`、`test_params_template`、`test_socket_bridge`、`test_project_gates`。

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
