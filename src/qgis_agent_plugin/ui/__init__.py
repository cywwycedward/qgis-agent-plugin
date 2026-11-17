"""插件的 Qt 表面：操作坞窗（见 :mod:`qgis_agent_plugin.ui.dock`）。

故意保持空壳。``initGui`` 自己 import ``ui.dock``，所以裸解释器（以及 ``tests/test_package.py``）
永远不会顺着这个包碰到 Qt，坞窗也仍然是唯一允许在模块级 import Qt 的模块。

Kept empty on purpose.  ``initGui`` imports ``ui.dock`` itself, so a bare interpreter
(and ``tests/test_package.py``) never reaches Qt through this package, and the dock
stays the one module allowed to import Qt at module level.
"""
