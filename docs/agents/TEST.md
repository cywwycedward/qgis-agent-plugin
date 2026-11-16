# TEST.md

本项目的测试分两层，运行机制与它能证明的事情不同；两层都要跑。

| 层 | 入口 | 证明什么 | 典型耗时 |
| --- | --- | --- | --- |
| 单元测试 | `npm run test`（`scripts\pytest.ps1` → `python-qgis-ltr.bat -m pytest -q tests`） | 契约、状态机、适配器记账；QGIS 与 Qt 用假对象 | 秒级 |
| 验收跑 | `python-qgis-ltr.bat tests\qgis_integration\run_acceptance.py` | 真实 PyQGIS 行为：真 headless `QgsApplication`、真 `QgsProject`/`QgsTaskManager`、现场生成的数据 | 数十秒 |

单测证明契约，验收跑证明真实 QGIS 里也成立。**改完代码要跑哪些、按改动加跑哪些**，见
`DEVELOP.md` §2.3 的检查清单；本文只讲这两层怎么设计。

单测内部再分两组，按「是不是全仓规则」切：

| 组 | 成员 | 何时跑 | 命令 |
| --- | --- | --- | --- |
| 规范组 | `test_comments`、`test_source_rules`、`test_docs`、`test_package`（`pytestmark = pytest.mark.conformance`） | **每次改完代码** | `npm run test:rules`（2 s，12 条） |
| 功能组 | 其余全部（契约、状态机、引擎、适配器、操作、坞窗） | 按需，合并前必跑 | `npm run test`（14 s，721 条） |

新写测试默认为功能组；只有当它断言"对整个仓库都成立、读源码/文档、不需要 QGIS"时才加
`pytestmark = pytest.mark.conformance`，否则别把它拖进每次必跑的那 2 秒里。不要用替身断言真实 PyQGIS 行为（`read()` 是否真的
填充图层树、`QgsTask` 是否真的被调度），也不要用验收跑替代针对边界条件的单测。

## 1 解释器

| 解释器 | 能跑什么 |
| --- | --- |
| `D:\QGIS 3.44.13\bin\python-qgis-ltr.bat`（QGIS 自带，Python 3.12） | 全部测试。`npm run test` 用它 |
| `D:\QGIS 3.44.13\apps\Python312\python.exe`（裸解释器，无 QGIS） | 除三处 `importorskip` 之外的全部：`tests/test_ui_dock.py` 整模块、`tests/test_engine.py` 里那条需要真 `aboutToBeCleared` 的用例、`tests/qgis_integration/test_claim_root.py` 整模块 |

所以**按解释器判断跑没跑过，不要按通过条数判断**：裸解释器的绿色少三块，那三块正是真实过程。
依赖安装须经用户批准（见 `ENVIRONMENT.md`），`pytest` 已经装在 QGIS 环境里。

## 2 单元测试

- pytest，`pyproject.toml` 已配 `testpaths=["tests"]`、`pythonpath=["src"]`，测试里直接
  `import qgis_agent_plugin...`，不需要安装插件。
- QGIS 替身用 `monkeypatch.setitem(sys.modules, "qgis", package)` 注入：`operations/*` 在函数体
  里 import，所以运行时看到的就是替身；替身只实现被测代码真正调用的方法，不追求全 API。
- 引擎测试注入 `post=list.append` 这样的收集器，用例自己 drain（一个回调可能再投递下一个，
  循环到空）；不要 `time.sleep`，不要起真线程。
- 后台适配器测试用"dict + 信号"的假 manager、普通对象当 task、列表当事件队列。
- `Settings(...)` 直接构造，根目录用 `tmp_path`；不写用户 profile、不读真实 `QgsSettings`。
- 断言具体值：错误码、字段值、调用次数、传进去的参数。不断言日志文本与私有属性。
- 一个用例证明一个行为，名字是一句描述行为的短句（例：
  `test_a_queued_call_cancelled_before_its_turn_never_runs`）。
- 新用例先在缺陷存在时确认是红的，再让它绿。
- 新操作的用例进 `tests/test_operations_{project,layer,processing}.py`；框架层的进对应
  `test_{contracts,models,registry,context,engine,execution_store,security}.py`。
- 有三条用例不测行为，而是把成文规则变成断言，改代码时要让它们继续绿：`test_comments.py`
  （注释与 docstring 双语、不引用文档）、`test_source_rules.py`（导入位置、`__future__`、
  日志器名、主题不写十六进制颜色）、`test_docs.py`（每份规范都被索引、相对链接能解析、
  不指向已删文档）。它们只读源码与文档，秒级完成，不需要 QGIS。

## 3 坞窗测试（Qt offscreen）

`tests/test_ui_dock.py` 要真 Qt，不需要 QGIS：模块开头
`os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")`，再 `pytest.importorskip("qgis.PyQt.QtWidgets")`，
所以裸解释器里整模块 skip。它断言的是操作者能看到、能做到的事：

- `input_schema` 变成哪些控件（复选框、数字框、JSON 文本、下拉框）与哪些字段会进参数；
- 六种状态（idle/running/ok/failed/cancelled/empty）下的控件、按钮与提示；
- `Ctrl+Return` 运行、焦点顺序、运行期间禁用；
- 浅色与深色两套 palette 的对比度（正文 4.5:1、胶囊 3:1，逐对断言）；
- `run_sync` 与 `submit`+轮询两条路径，以及关窗时停轮询。

失败必须变成屏幕上的文字（错误区），不能逃进 Qt slot。截图要另设 `QT_QPA_FONTDIR`（offscreen
没有字体库），所以截图只代表布局与配色，不代表真机字体。

## 4 裸解释器守卫（`tests/test_package.py`）

它在**子进程**里做四件事，任何一件失败都说明导入层被破坏：

1. 导入整包、全部 `core` 模块、`operations`、`plugin`、`ui`（`ui/__init__.py` 是空壳），
   注册全部 17 个 Operation，断言每个的 `(kind, strategy, destructive)`；
2. 断言 `sys.modules` 里没有 `qgis`、`PyQt5`、`PyQt6`、`PySide2`、`PySide6`；
3. 读 `ui/dock.py` 的源码，断言没有模块级 `import qgis`（`from qgis.PyQt...` 除外）。查
   `sys.modules` 不行：在 QGIS 解释器里 import `qgis.PyQt` 本身就把 `qgis.core`、`qgis.gui`
   带进来了，那样的断言永远不会失败；
4. Qt 存在时额外断言 `OperationDock` 是 `QDockWidget` 的子类（`addDockWidget` 只收这个类型）。

同一个文件还断言非 2.x 的 pydantic 会被 `plugin.require_supported_pydantic` 拒绝。

## 5 验收跑（headless QGIS）

```
& 'D:\QGIS 3.44.13\bin\python-qgis-ltr.bat' tests\qgis_integration\run_acceptance.py [工作目录]
```

- 不给工作目录时自己建一个**名字带空格**的临时目录（顺带验证带空格的路径）。给了就必须
  严格位于系统临时目录内，且要么为空、要么带着本次跑的标记；否则打印 `REFUSING: …` 并以
  **退出码 2** 结束，什么都不删。
- 退出码：`0` 无失败、`1` 有失败、`2` 拒绝工作目录。**skip 不算通过**，也不影响退出码，
  但每条 skip 都要带原因。
- 四道目录守卫由 `tests/qgis_integration/test_claim_root.py` 自己测：临时目录之外的根拒绝、
  临时目录本身拒绝、非空且没有标记的目录拒绝、`fixtures._fresh` 只动被认领的目录；还有
  runner 的 `REFUSING` 退出码。它是本交付里唯一会删目录的代码，所以是被测对象，不是信任对象。
- 一次性 profile：`QGIS_CUSTOM_CONFIG_PATH` 指向工作目录里的 `profile/`，跑完还检查 settings
  文件确实落在 profile 内，不碰操作者的 `QGIS3.ini`。
- 工作目录同时是进程的工作目录：QGIS 会把 `symbology-style.db` 写进启动目录，所以测试数据在
  那里生成，不落在仓库里。
- 测试数据用 PyQGIS 现场生成（GeoPackage、GeoTIFF、QGZ、损坏文件、指向根外的目录等），
  生成在 `initQgis()` **之后**：provider 注册之前只有 16 个 provider，Delimited Text 不在其中，
  `file:./points.csv` 那种工程写不出来。
- `initQgis()` 不带 Processing 的 provider，所以 harness 显式注册它们；注册失败时
  `processing.*` 的用例 `Skip` 并说明原因，而不是对着空注册表报错。
- `CASES` 元组（`cases.py` 末尾）共 93 条，按前缀分六组：`AT*` 48、`P*` 19、`S*` 6、`A*` 7、`V*` 8、
  `R*` 5；用例函数名跟着 id 走（`at_01_*`、`p13_*`…），所以**不能按 `at_` 前缀数用例**。每条返回
  一行证据；判定是 `tally.Row` 的字段，**不从打印文本里数 `PASS`/`FAIL`**（证据里出现这些词不改变
  统计）。用例抛 `AssertionError` 是 FAIL，抛 `harness.Skip` 是 SKIP，其它异常同样记 FAIL 并带文件名
  与行号。
- 结果同时写 `results.txt` 到工作目录，控制台与文件里都有一行 `summary:`。

## 6 坑与收尾

| 现象 | 处理 |
| --- | --- |
| 控制台中文乱码（GBK） | 结果写 UTF-8 JSON/文本，不解析控制台文本 |
| PowerShell `Start-Process -NoNewWindow` 的退出码可能为空 | 以 stdout 标记行或结果文件为准 |
| `.bat` 里写 UTF-8 中文注释导致 cmd 解析错乱 | 脚本保持 ASCII + CRLF（`.gitattributes` 已钉住 `*.bat`） |
| 强杀后残留 `qgis-ltr-bin` 进程 | `Get-Process qgis-ltr-bin \| Stop-Process -Force` |
| 报"超时" | 先看结果文件与日志里有没有模态框记录，不要直接调大超时 |

超时只是兜底：进程级 90 s（真实 QGIS）与 30 s（纯 Python）只在 harness 自身失效时触发，
触发后先清理孤儿进程再判定失败。

## 附录 A 真实 GUI 注入（现在不用）

早期为了验证"GUI 里也能跑"，用过向真实 QGIS 进程注入探针脚本的路子。现在由 §5 的 headless
harness 覆盖同一批插件行为，这条路不用了；下面这些事实是当时花时间换来的，留着避免重走。

- 启动器必须是 `bin\qgis-ltr-bin.exe`：`qgis-ltr.bat` 用 `start /B` 起进程，拿不到 stdout 与
  退出码（实测无任何输出）。启动前 `call o4w_env.bat`，并设 `QGIS_PREFIX_PATH`、`PYTHONPATH`、
  `QT_PLUGIN_PATH`（照抄 `.bat`，去掉 `start`）。
- `--code <file>`：脚本在 GUI 启动后执行，但命名空间里没有 `iface`，要自己
  `from qgis.utils import iface`。`--noplugins`、`--noversioncheck`、`--profile <name>` 可用；
  `--profiles-path` 三次实测全部挂起（不存在的目录、空目录、完整 profile 副本），不要用；
  `--help` 不打印帮助而是直接启动 GUI（20 s 无输出，需强杀）。
- GUI 进程里 `QGIS_CUSTOM_CONFIG_PATH` 无效（settings 仍在 `QGIS3\profiles\default`），
  隔离只能靠 `--profile`。
- 四道防线：开场把 `QMessageBox.*`、`QDialog.exec`、`QFileDialog.*` 全部打补丁并返回固定值；
  `QTimer` 看门狗检查 `activeModalWidget()`，命中就关窗并非 0 退出；脚本主体整体
  `try/except` 记 traceback（未捕获异常会弹模态框且进程挂死）；最后写 JSON + 显式
  `QCoreApplication.exit(code)` + `QTimer.singleShot(3000, os._exit)` 兜底，并在退出前
  `QgsProject.instance().clear()` + `setDirty(False)`（脏工程会弹保存询问，实测与看门狗叠在一起
  会以 `0xC0000005` 崩）。
