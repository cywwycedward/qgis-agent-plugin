"""Helpers used by more than one action.

The PyQGIS ones import QGIS inside the helper, never at module import time: the
action package has to load in a plain interpreter so its declarations and
validation stay usable without a QGIS runtime.
"""

from __future__ import annotations

import os
from typing import Any

from .base import ActionError


def absolute_path(value: str) -> str:
    """Return ``value`` when it is absolute, and refuse a relative one.

    QGIS resolves a relative path against its own process's working directory,
    which is not the caller's, so a relative path is a path that means something
    else.  Raised as ``ValueError`` so pydantic reports it on the field it
    belongs to, like every other declared rule.
    """

    if not os.path.isabs(value):
        raise ValueError(
            "must be an absolute path: QGIS resolves a relative one against the "
            "QGIS process's working directory, not the caller's"
        )
    return value


def qgz_path(value: str) -> str:
    """Return ``value`` when it names a ``.qgz`` file, and refuse anything else.

    The extension is the format: QGIS writes a compressed project for ``.qgz``
    and plain XML for every other name, so a path that is not ``.qgz`` would
    quietly produce something other than what the command says it writes.  QGIS
    reads the extension without regard to case, and this check does too.
    """

    if not value.lower().endswith(".qgz"):
        raise ValueError("must end in .qgz, the compressed project format")
    return value


def layer_source(value: str) -> str:
    """Return ``value`` when it is a source QGIS can resolve, and refuse a relative path.

    A layer source is either a file or a provider URI (``type=xyz&url=...``,
    ``GPKG:...``), and only the file case has an absolute form to insist on: a
    relative file is a path resolved against the QGIS process's working
    directory, exactly like the project paths above.  A value that carries a URI
    marker (``=`` or a scheme's ``:``) is left alone, because the provider that
    will read it, not this check, knows how to parse it.
    """

    if "=" in value or ":" in value:
        return value
    return absolute_path(value)


def require_layer(project: Any, layer_id: str) -> Any:
    """Return the named layer, or fail with the code the callers expect."""

    layer = project.mapLayers().get(layer_id)
    if layer is None:
        raise ActionError("layer_not_found", f"Layer not found: {layer_id}")
    return layer
