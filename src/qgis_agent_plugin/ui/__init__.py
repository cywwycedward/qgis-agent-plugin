"""The plugin's Qt surface: the operation dock (see :mod:`qgis_agent_plugin.ui.dock`).

Kept empty on purpose.  ``initGui`` imports ``ui.dock`` itself, so a bare interpreter
(and ``tests/test_package.py``) never reaches Qt through this package, and the dock
stays the one module allowed to import Qt at module level.
"""
