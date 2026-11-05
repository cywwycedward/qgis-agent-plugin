"""Build readable documentation from QGIS processing declarations.

This module deliberately reports QGIS's own information instead of trying to
infer a complete schema for values such as ``list[dict]``.  The latter is not
available from every QGIS parameter type, while the declarations and their
documentation are stable enough to expose directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def parameter_documentation(definition: Any, parameter_type: Any, context: Any) -> dict[str, Any]:
    """Return the QGIS documentation attached to one parameter definition."""

    if parameter_type is None:
        return {
            "help": _text(_call(definition, "help")),
            "parameter_class": "",
            "type_description": "",
            "python_import": "",
            "accepted_string_values": [],
            "accepted_parameter_types": [],
            "accepted_output_types": [],
            "class_documentation": "",
            "default_python": _default_python(definition, context),
        }

    class_name = _text(_call(parameter_type, "className"))
    return {
        "help": _text(_call(definition, "help")),
        "parameter_class": class_name,
        "type_description": _text(_call(parameter_type, "description")),
        "python_import": _text(_call(parameter_type, "pythonImportString")),
        "accepted_string_values": _strings(_call(parameter_type, "acceptedStringValues")),
        "accepted_parameter_types": _strings(_call(parameter_type, "acceptedParameterTypes")),
        "accepted_output_types": _strings(_call(parameter_type, "acceptedOutputTypes")),
        "class_documentation": _class_documentation(class_name),
        "default_python": _default_python(definition, context),
    }


def render_algorithm_help(algorithm: Any, parameter_types: Mapping[str, Any]) -> str:
    """Render a text help page from QGIS declarations without using GUI code."""

    lines = [f"{algorithm.displayName()} ({algorithm.id()})", ""]
    _append_text(lines, algorithm.shortDescription())
    _append_text(lines, algorithm.shortHelpString())
    lines.extend(["", "----------------", "Input parameters", "----------------"])

    for definition in algorithm.parameterDefinitions():
        lines.extend(["", f"{definition.name()}: {definition.description()}"])
        _append_indented(lines, _call(definition, "help"))
        parameter_type = parameter_types.get(definition.type())
        class_name = _call(parameter_type, "className") if parameter_type else None
        lines.extend(
            [
                "",
                f"\tParameter type:\t{class_name or definition.__class__.__name__}",
            ]
        )

        options = _call(definition, "options")
        if options:
            lines.append("\n\tAvailable values:")
            lines.extend(f"\t\t- {index}: {option}" for index, option in enumerate(options))

        accepted = [] if parameter_type is None else _call(parameter_type, "acceptedPythonTypes")
        if accepted:
            lines.extend(["", "\tAccepted data types:"])
            lines.extend(f"\t\t- {value}" for value in accepted)

    lines.extend(["", "----------------", "Outputs", "----------------"])
    for output in algorithm.outputDefinitions():
        lines.extend(
            [
                "",
                f"{output.name()}:  <{output.__class__.__name__}>",
                f"\t{output.description()}" if output.description() else "",
            ]
        )

    return "\n".join(lines).rstrip()


def _append_text(lines: list[str], value: Any) -> None:
    text = _text(value)
    if text:
        lines.extend([text, ""])


def _append_indented(lines: list[str], value: Any) -> None:
    text = _text(value)
    if text:
        lines.extend(f"\t{line}" for line in text.splitlines())


def _call(obj: Any, name: str, *args: Any) -> Any:
    """Call an optional QGIS method, preserving failures from the method itself."""

    method = getattr(obj, name, None)
    if not callable(method):
        return None
    return method(*args)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _strings(value: Any) -> list[str]:
    if value is None or isinstance(value, (str, bytes)):
        return [] if value is None else [str(value)]
    try:
        return [str(item) for item in value]
    except TypeError:
        return []


def _class_documentation(class_name: str) -> str:
    if not class_name:
        return ""
    try:
        from qgis import core

        parameter_class = getattr(core, class_name, None)
        return (getattr(parameter_class, "__doc__", "") or "").strip()
    except (ImportError, AttributeError):
        return ""


def _default_python(definition: Any, context: Any) -> str | None:
    value = _call(definition, "defaultValue")
    if value is None:
        return None
    try:
        from qgis.PyQt.QtCore import QVariant

        if isinstance(value, QVariant) and value.isNull():
            return None
    except ImportError:
        pass

    rendered = _call(definition, "valueAsPythonString", value, context)
    return None if rendered is None else str(rendered)
