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
from qgis_agent_plugin import operations, plugin
from qgis_agent_plugin.core import config, context, contracts, engine, execution_store
from qgis_agent_plugin.core import models, registry, security
from qgis_agent_plugin.core.runners import main_thread, qgs_task

assert callable(qgis_agent_plugin.classFactory)
assert callable(plugin.classFactory)
assert callable(operations.register_all)
assert callable(context.require_main_thread)
assert callable(engine.OperationEngine)
assert callable(execution_store.ExecutionStore)
assert callable(main_thread.MainThreadRunner)
assert callable(qgs_task.OperationTaskRunner)
assert callable(qgs_task.task_class)
leaked = sorted(
    name
    for name in sys.modules
    if name.split(".")[0] in {"qgis", "PyQt5", "PyQt6", "PySide2", "PySide6"}
)
assert not leaked, leaked
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
