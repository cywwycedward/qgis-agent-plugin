"""插件包必须能在一个没有 QGIS、也没有 Qt 的解释器里导入成功。

契约层、操作和插件入口都在一个全新进程里导入，所以守卫证明的是这条规则本身，而不是恰好依赖了别的
测试已经做过的 QGIS 导入。QGIS 按名字加载的也正是这个包，所以入口在这里查，而不放在单元测试里。

The plugin package must load in an interpreter that has no QGIS and no Qt.  The contract layer,
the operations and the plugin entry are imported in a fresh process, so the guard proves the
rule instead of accidentally relying on a QGIS import another test already made.  The plugin is
also what QGIS loads by name, which is why the entry point is checked here, not in a unit test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest  # pyright: ignore[reportMissingImports]

ROOT = Path(__file__).resolve().parents[1]

# 这一组是成文规则本身，每次改完代码都要跑。
# This group is the written rules themselves; it runs after every change.
pytestmark = pytest.mark.conformance

_GUARD = """
import re
import sys
from pathlib import Path

import qgis_agent_plugin
from qgis_agent_plugin import operations, plugin, ui
from qgis_agent_plugin.core import config, context, contracts, engine, execution_store
from qgis_agent_plugin.core import models, registry, security
from qgis_agent_plugin.core.runners import main_thread, qgs_task
from qgis_agent_plugin.operations import processing

assert callable(qgis_agent_plugin.classFactory)
assert callable(plugin.Plugin)
assert callable(plugin.Supervisor)
assert callable(plugin.application_supervisor)
assert callable(plugin.read_settings)
assert callable(operations.register_all)
assert callable(context.require_main_thread)
assert callable(engine.OperationEngine)
assert callable(execution_store.ExecutionStore)
assert callable(main_thread.MainThreadRunner)
assert callable(qgs_task.OperationTaskRunner)
assert callable(qgs_task.task_class)

# 十七个 Operation 在一个没有 QGIS 的解释器里注册成功，并且每个都声明了本文件列出的
# kind、strategy 与风险标记。
# The seventeen operations register in an interpreter without QGIS, and each one declares
# the kind, strategy and risk label listed here.
filled = registry.OperationRegistry()
operations.register_all(filled)
specs = filled.list_specs()
assert len(specs) == 17, specs
assert {spec.name: (spec.kind.value, spec.strategy.value, spec.destructive) for spec in specs} == {
    "project.create": ("command", "main_thread", True),
    "project.open": ("command", "main_thread", True),
    "project.save": ("command", "main_thread", True),
    "project.get_info": ("query", "main_thread", False),
    "layer.add": ("command", "main_thread", False),
    "layer.list": ("query", "main_thread", False),
    "layer.get_info": ("query", "main_thread", False),
    "layer.set_visibility": ("command", "main_thread", False),
    "layer.sum_numeric_field": ("query", "background", False),
    "layer.list_style_categories": ("query", "main_thread", False),
    "layer.get_qml": ("query", "main_thread", False),
    "layer.list_available_renderers": ("query", "main_thread", False),
    "layer.get_renderer": ("query", "main_thread", False),
    "layer.set_renderer": ("command", "main_thread", True),
    "processing.list_algorithms": ("query", "main_thread", False),
    "processing.get_algorithm_info": ("query", "main_thread", False),
    "processing.run_algorithm": ("command", "main_thread", True),
}
# 唯一自带元组的模块，在这里再拄一份就会走样。
# The one module that carries its own tuple, so a copy here would drift.
assert len(processing.PROCESSING_OPERATIONS) == 3

leaked = sorted(
    name
    for name in sys.modules
    if name.split(".")[0] in {"qgis", "PyQt5", "PyQt6", "PySide2", "PySide6"}
)
assert not leaked, leaked

# ``ui/dock.py`` 是唯一允许模块级 import Qt 的模块，而它能 import 的 Qt 只有 ``qgis.PyQt``。
# 这条规则读它的源码来判断：import 它需要 Qt，而 ``sys.modules`` 回答不了这个问题——
# 在这个解释器里 import ``qgis.PyQt`` 本身就已经加载了 ``qgis.core`` 与 ``qgis.gui``。
# ``ui/dock.py`` is the one module allowed to import Qt at module level, and the only Qt it
# may import is ``qgis.PyQt``.  The rule is read off its source: importing it would need Qt,
# and ``sys.modules`` cannot answer the question -- importing ``qgis.PyQt`` already loads
# ``qgis.core`` and ``qgis.gui`` in this interpreter.
dock_source = (Path(ui.__file__).parent / "dock.py").read_text(encoding="utf-8")
offenders = [
    line.strip()
    for line in dock_source.splitlines()
    if re.search(r"\\b(?:import|from)\\s+qgis\\b(?!\\.PyQt)", line)
]
assert not offenders, offenders

# 并且它确实是 QMainWindow 能停靠的控件——这一点在 Qt 存在时才问得了。
# And it is really a widget a QMainWindow can dock, where Qt exists to ask.
try:
    from qgis.PyQt.QtWidgets import QDockWidget
except ImportError:
    pass
else:
    from qgis_agent_plugin.ui import dock

    assert issubclass(dock.OperationDock, QDockWidget)
"""


def test_package_and_contracts_import_without_qgis() -> None:
    env = {**os.environ}
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), env.get("PYTHONPATH", "")])
    result = subprocess.run(
        [sys.executable, "-c", _GUARD],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr


def test_a_pydantic_major_other_than_two_is_a_dependency_error() -> None:
    from qgis_agent_plugin import plugin  # pyright: ignore[reportMissingImports]

    with pytest.raises(RuntimeError, match="requires pydantic 2.x"):
        plugin.require_supported_pydantic("1.10.26")
    plugin.require_supported_pydantic("2.13.4")
