"""The plugin package must load in an interpreter that has no QGIS and no Qt.

The contract layer, the operations and the plugin entry are imported in a fresh
process, so the guard proves the rule instead of accidentally relying on a QGIS
import another test already made.  The plugin is also what QGIS loads by name,
which is why the entry point is checked here rather than in a unit test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_GUARD = """
import sys

import qgis_agent_plugin
from qgis_agent_plugin import operations, plugin, ui
from qgis_agent_plugin.core import config, context, contracts, engine, execution_store
from qgis_agent_plugin.core import models, registry, security
from qgis_agent_plugin.core.runners import main_thread, qgs_task

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

# The nine operations of PRD 7.1 register in an interpreter that has no QGIS.
filled = registry.OperationRegistry()
operations.register_all(filled)
assert len(filled.list_specs()) == 9, filled.list_specs()
assert {spec.name for spec in filled.list_specs()} == {
    "project.create",
    "project.open",
    "project.save",
    "project.get_info",
    "layer.add",
    "layer.list",
    "layer.get_info",
    "layer.set_visibility",
    "layer.sum_numeric_field",
}

leaked = sorted(
    name
    for name in sys.modules
    if name.split(".")[0] in {"qgis", "PyQt5", "PyQt6", "PySide2", "PySide6"}
)
assert not leaked, leaked

# The dock is the one module allowed to import Qt at module level, so it is asked
# for last, and only where Qt exists: it may reach ``qgis.PyQt``, never the QGIS
# API.  The Qt surface is imported first, so anything the dock pulls in after it is
# a QGIS module it had no business importing.
try:
    import qgis.PyQt.QtWidgets  # noqa: F401 - the Qt surface the dock may use
except ImportError:
    pass
else:
    qt_loaded = set(sys.modules)
    from qgis_agent_plugin.ui import dock

    assert callable(dock.OperationDock)
    reached = sorted(
        name
        for name in sys.modules
        if (name == "qgis" or name.startswith("qgis.")) and name not in qt_loaded
    )
    assert not reached, reached
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
    from qgis_agent_plugin import plugin

    with pytest.raises(RuntimeError, match="requires pydantic 2.x"):
        plugin.require_supported_pydantic("1.10.26")
    plugin.require_supported_pydantic("2.13.4")
