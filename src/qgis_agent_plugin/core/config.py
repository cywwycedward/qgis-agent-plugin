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

        A key may be absent or ``None`` -- that is what a QSettings-like store
        returns for something nobody wrote -- and both mean "not set", so the
        dataclass default stays.  A count may arrive as a string and a root list
        as the single value the store returns, which is why both are converted
        here, once, instead of in every caller.
        """

        set_values = {key: value for key, value in values.items() if value is not None}
        resolved: dict[str, Any] = {}
        for field in fields(cls):
            if field.name not in set_values:
                continue
            value = set_values[field.name]
            resolved[field.name] = (
                _roots(value) if field.name in _ROOT_KEYS else _count(field.name, value)
            )
        return cls(**resolved)


def _count(key: str, value: Any) -> int:
    """One count, refused rather than clamped when it cannot work.

    A count below one is a configuration mistake with a silent failure mode:
    ``max_active_background=0`` would leave every background call waiting for a
    slot that never frees, so the plugin refuses to load instead of queueing
    forever.  The error names the key and, for an unparseable value, the value too.
    """

    try:
        count = int(value)
    except (TypeError, ValueError):
        raise RuntimeError(f"{key} must be a whole number, not {value!r}") from None
    if count < 1:
        raise RuntimeError(f"{key} must be at least 1, not {count}")
    return count


def _roots(value: Any) -> tuple[Path, ...]:
    """One root may arrive as a single path: QSettings returns one value that way."""

    if not value:
        return ()
    if isinstance(value, (str, Path)):
        return (Path(value),)
    return tuple(Path(root) for root in value)
