"""The settings the engine and the path policy read, and their product defaults.

The two root lists start empty on purpose: an empty list denies every file, so a
fresh install cannot read or write anything until an operator names a directory.
That default is the security posture, not a missing value, which is why it lives
here next to the numbers it is read with.

Reading the settings out of ``QgsSettings`` belongs to the plugin lifecycle; this
module only turns a plain mapping into the values, so it stays importable in a
bare interpreter.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

# The names a mapping may carry; the two roots are paths, the rest are counts.
_ROOT_KEYS = ("allowed_data_roots", "allowed_project_roots")


@dataclass(frozen=True)
class Settings:
    """One resolved configuration; ``allowed_*_roots=()`` means no file access."""

    allowed_data_roots: tuple[Path, ...] = ()
    allowed_project_roots: tuple[Path, ...] = ()
    max_active_background: int = 2
    max_pending: int = 32
    max_terminal_records: int = 200
    terminal_ttl_seconds: int = 1800
    max_snapshot_features: int = 10000

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> Settings:
        """Build the settings from a mapping, keeping every declared default.

        Values may arrive as strings -- that is what a QSettings-like store
        returns -- so the counts are converted and the roots are turned into
        paths here, once, instead of in every caller.
        """

        resolved: dict[str, Any] = {key: _roots(values.get(key, ())) for key in _ROOT_KEYS}
        resolved.update(
            {
                field.name: int(values.get(field.name, getattr(cls, field.name)))
                for field in fields(cls)
                if field.name not in _ROOT_KEYS
            }
        )
        return cls(**resolved)


def _roots(value: Any) -> tuple[Path, ...]:
    """One root may arrive as a single string: QSettings returns one value that way."""

    if isinstance(value, str):
        return (Path(value),)
    return tuple(Path(root) for root in value)
