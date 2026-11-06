"""What the layer commands share: how a source is named, and how a layer is found."""

from __future__ import annotations

from typing import Any

from ...protocol import CommandError
from ..shared import absolute_path


def layer_source(value: str) -> str:
    """Return ``value`` when it is a source QGIS can resolve, and refuse a relative path.

    A layer source is either a file or a provider URI (``type=xyz&url=...``,
    ``GPKG:...``), and only the file case has an absolute form to insist on: a
    relative file is a path resolved against the QGIS process's working
    directory, exactly like a project path.  A value that carries a URI marker
    (``=`` or a scheme's ``:``) is left alone, because the provider that will
    read it, not this check, knows how to parse it.
    """

    if "=" in value or ":" in value:
        return value
    return absolute_path(value)


def require_layer(project: Any, layer_id: str) -> Any:
    """Return the named layer, or fail with the code the callers expect."""

    layer = project.mapLayers().get(layer_id)
    if layer is None:
        raise CommandError("layer_not_found", f"Layer not found: {layer_id}")
    return layer
