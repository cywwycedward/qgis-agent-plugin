# ENVIRONMENT.md

本项目的 Python 运行时为 QGIS 自带 Python。不创建 venv，不安装独立解释器，依赖直接安装到 QGIS 环境。

## 安装信息

以下为实测环境。

| 项 | 值 |
| --- | --- |
| 安装根目录 | `D:\QGIS 3.44.13`（OSGeo4W 布局：`apps/`、`bin/`、`lib/`、`etc/`） |
| QGIS | 3.44.13-Solothurn，LTR 分支，程序位于 `apps\qgis-ltr` |
| Python | 3.12.13，位于 `apps\Python312` |
| site-packages | `D:\QGIS 3.44.13\apps\Python312\Lib\site-packages`，可写 |
| pip | 26.1.2，随 QGIS 提供 |

## 解释器

本机有两个可用的 Python，职责不同，不要混用：

| 解释器 | 能做什么 | 不能做什么 |
| --- | --- | --- |
| `D:\QGIS 3.44.13\bin\python-qgis-ltr.bat` | 跑插件、跑全部测试（`npm run test` 用它）、`-m pip` 装依赖 | — |
| `D:\QGIS 3.44.13\apps\Python312\python.exe` | 跑 lint、跑不碰 QGIS 的那部分测试（`import qgis` 的三处会 skip）、`uv pip` 的 `--python` 目标 | 不能 `import qgis`（没有 `PYTHONHOME`/`PYTHONPATH`/Qt 的 DLL 搜索路径） |

跑插件的入口只有一个：`bin\python-qgis-ltr.bat`。该脚本先调用 `o4w_env.bat`，配置
`PYTHONHOME`、`QGIS_PREFIX_PATH`、`QT_PLUGIN_PATH` 与 `PYTHONPATH`（含 `apps\qgis-ltr\python`），
再启动 Python。

```
$qgis = 'D:\QGIS 3.44.13\bin\python-qgis-ltr.bat'

& $qgis -c "from qgis.core import Qgis; print(Qgis.version())"   # 3.44.13-Solothurn
& $qgis -m pip list
```

直接调用 `bin\python.exe` 不可用：

| 条件 | 结果 |
| --- | --- |
| 默认环境变量 | `ModuleNotFoundError: No module named 'encodings'`（缺 `PYTHONHOME`） |
| 仅设置 `PYTHONHOME` | 标准库可用；`import qgis` 报 `ModuleNotFoundError`（另缺 `PYTHONPATH` 与 Qt、GDAL 的 DLL 搜索路径） |

本机提供的启动脚本为 `python-qgis-ltr.bat` 与 `python-grass85.bat`，不存在 `python-qgis.bat`。

## 依赖管理

**安装须经用户批准。** 不得自行安装依赖：需要新增依赖时，先报告包名与版本、必要性、以及对 QGIS 全局环境的影响范围，得到明确许可后再执行安装。

依赖安装到 QGIS 全局环境，由同一实例下的所有插件共享：

```
& $qgis -m pip install pytest      # 仅在用户批准后执行
```

- 已安装：`pip`、`setuptools`、`numpy`、`lxml`、`PyQt5`、`yaml`、`osgeo`（GDAL）、`pydantic`（2.13.4）、`pytest`（9.2.0，仅测试用）。`pytest` 在两个解释器里都能用（实测 `pytest --version` 都是 9.2.0）。
- 未安装：`pytest-qgis`、`wheel`
- `pydantic` 只支持 2.x：插件 `import` 时就会检查主版本，非 2 直接 `RuntimeError`。
- 以上只列与本项目相关的包；完整清单用 `-m pip list`
- 仅安装纯 Python 的轻量依赖。升级或替换 `PyQt5`、`osgeo`、`qgis` 及任何含二进制组件的包会破坏 QGIS 安装。

## 使用 uv 代替 pip

本机存在 `uv 0.8.15`（`C:\Users\Administrator\.local\bin\uv.exe`），可作为 `-m pip` 的替代，但必须指向真实解释器：

```
uv pip install --python "D:\QGIS 3.44.13\apps\Python312\python.exe" pytest
```

- `apps\Python312\python.exe` 可用；`bin\python.exe` 与 `bin\python-qgis-ltr.bat` 都不可用——uv 以隔离模式（`-I`）探测解释器、忽略 `PYTHONHOME`，实测报 `Failed to inspect Python interpreter`。
- 不建 venv，装进 QGIS 全局 `site-packages`，与 `-m pip` 等价。
- 项目模式不适用：`uv lock` 会解析到 uv 自带的 Python（实测 3.13）而非 QGIS 的 3.12，`uv sync` 会创建 `.venv`；要锁版本用 `uv pip compile` + `uv pip install -r`。

若需环境隔离，可改用 conda-forge 的 `qgis` 包，当前未采用。

## 路径速查

| 用途 | 路径 |
| --- | --- |
| 仓库根目录 | `D:\core\workspace\mine\github_mine\auto-carto\qgis` |
| 解释器入口 | `D:\QGIS 3.44.13\bin\python-qgis-ltr.bat` |
| 用户 profile | `%APPDATA%\QGIS\QGIS3\profiles\default` |
| 用户插件目录 | `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins` |
| QGIS 日志 | `%APPDATA%\QGIS\QGIS3\profiles\default\qgis.log` |

用户级已安装插件：`qgis-mcp-plugin`、`QuickOSM`、`quick_map_services`、`tianditu-tools`。

## 版本

QGIS 版本由 `src/qgis_agent_plugin/metadata.txt` 钉住：`qgisMinimumVersion=3.44`、
`qgisMaximumVersion=3.44.99`。本机实测 3.44.13-Solothurn。文中的耗时、字节数、TCP 次数都是在这个
版本上测的；换版本要先重测再改阈值。
