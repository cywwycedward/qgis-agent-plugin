"""The framework itself: protocol, records, registry, settings and path policy.

Nothing here imports QGIS or Qt at module level, so the core stays importable in
a plain interpreter and every rule in it is testable without a running QGIS.
"""
