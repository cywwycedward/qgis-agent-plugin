"""Rules a command's parameters may use, where no one command owns them.

A rule belongs here when commands of two or more capabilities need it and it
describes no domain of its own: one about layers goes to ``layer/shared.py``,
one about project files to ``project/shared.py``.  Nothing here imports QGIS --
a params rule runs at the boundary, before any QGIS state is read.
"""

from __future__ import annotations

import os


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
