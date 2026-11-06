"""The params one command accepts, as a value a caller can start editing.

Built from the command's own JSON schema, so whoever shows it needs to know
nothing about any specific command.  Imports neither Qt nor QGIS: this is the
part of the debug page that a test can reach without a GUI.
"""

from __future__ import annotations

from typing import Any

# The JSON value a field with no default starts as, per schema type.
_EMPTY: dict[str, Any] = {
    "string": "",
    "integer": 0,
    "number": 0,
    "boolean": False,
    "array": [],
    "object": {},
}


def params_template(command: Any) -> dict[str, Any]:
    """The params of ``command`` as a starting point: one key per declared field.

    An optional field keeps the default it declared; a required one gets a
    placeholder of its type, so an empty string or a zero is a value the caller
    has to replace rather than a silent default.
    """

    template = {}
    for name, spec in command.params_model.model_json_schema().get("properties", {}).items():
        template[name] = spec["default"] if "default" in spec else _placeholder(spec)
    return template


def _placeholder(spec: dict[str, Any]) -> Any:
    """One value for a field the schema gives no default for."""

    if "anyOf" in spec:  # a union: the first branch is the one a caller fills in
        return _placeholder(spec["anyOf"][0])
    if "enum" in spec:
        return spec["enum"][0]
    if "const" in spec:
        return spec["const"]
    return _EMPTY.get(spec.get("type", ""))
