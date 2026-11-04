from .plugin import classFactory

# QGIS looks the entry point up by name on the package, so the import is the export.
__all__ = ["classFactory"]
