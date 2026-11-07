"""The three pydantic calls the framework makes, under either major version.

Input validation, JSON dumping and schema export are the only places where the
version difference shows, so they are wrapped once here instead of being branched
on in every caller.  A runtime whose pydantic is neither 1 nor 2 is a broken
install: it is reported as a dependency error at import, not half-supported.
"""

from __future__ import annotations

from typing import Any

import pydantic
from pydantic import BaseModel


def _detect_major() -> int:
    major = int(pydantic.VERSION.split(".", 1)[0])
    if major not in (1, 2):
        raise RuntimeError(
            "qgis-agent-plugin requires pydantic 1.x or 2.x, "
            f"but {pydantic.VERSION} is installed"
        )
    return major


_MAJOR = _detect_major()


def major() -> int:
    """The detected pydantic major version, exposed through ``OperationSpec``."""

    return _MAJOR


def validate[ModelT: BaseModel](model: type[ModelT], data: Any) -> ModelT:
    """Build ``model`` from caller input, rejecting whatever the model rejects."""

    if _MAJOR == 1:
        return model.parse_obj(data)
    return model.model_validate(data)


def dump(model: BaseModel) -> dict[str, Any]:
    """Serialize a model the way JSON spells it, not the way Python holds it."""

    if _MAJOR == 1:
        return model.dict()
    return model.model_dump(mode="json")


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The model's JSON Schema, in whichever draft ``model``'s pydantic produces."""

    if _MAJOR == 1:
        return model.schema()
    return model.model_json_schema()
