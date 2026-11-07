"""Which local paths an operation may read or write.

There is one entry point, and a path either normalises to a regular file inside
one of the operator's allowed roots or it is refused.  Roots are compared path
component by path component after resolution, so ``..`` and a symbolic link
cannot climb out of an allowed directory; the message never repeats the raw path,
because an error line may end up in a log or an agent's context.

An empty root list -- the default -- refuses everything: no directory is allowed
until an operator names one.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from .models import PATH_NOT_ALLOWED, FrameworkError

# A URI scheme is at least two characters and ends in a colon; a single letter
# followed by a colon is a Windows drive, which is a path, not a scheme.
_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]+:")
# GDAL's in-process virtual file systems are not local files at this layer.
_VSI = "/vsi"
# A UNC path is network I/O, not a local file: two leading separators name a host
# and a share, and the ``\\?\`` prefixes are the long-path spelling of the same.
# ``Path.resolve`` would reach that host before the allow-list is ever consulted.
_UNC = ("\\\\", "//", "\\\\?\\", "//?/")


def is_remote_uri(raw: str) -> bool:
    """Whether ``raw`` names network I/O or a virtual file system, not a local file.

    Public because the path policy answers one question for every path: a URL, a
    provider URI, a GDAL ``/vsi`` path and a UNC path are all I/O this plugin only
    performs after the allow-list has said yes (PRD 10.2).
    """

    return raw.lower().startswith(_VSI) or raw.startswith(_UNC) or bool(_URI.match(raw))


def resolve_path(
    raw: str,
    roots: Sequence[str | Path],
    *,
    must_exist: bool,
    allow_directory: bool = False,
) -> Path:
    """Return the normalised path, or raise ``PATH_NOT_ALLOWED``.

    ``must_exist=True`` is a read: the target has to be an existing regular file.
    ``must_exist=False`` is a save: the target may be absent, but its parent must
    exist and be inside the same roots, and the overwrite decision stays with the
    operation (``TARGET_EXISTS`` is not this module's concern).

    ``allow_directory=True`` turns the save shape into the one a data source needs:
    the target may be a directory and its parent does not have to exist -- a source
    whose directory was moved away is still a source inside the roots (AT-18B) --
    so only the resolved path is compared with the roots.  It says nothing about a
    read; a read is always a regular file.
    """

    _refuse_remote(raw)
    path = Path(raw)
    if not raw or "\0" in raw or not path.is_absolute():
        _refuse()
    # A relative root is a configuration mistake: it would make the policy depend
    # on the working directory, so it allows nothing.
    allowed = tuple(Path(root).resolve() for root in roots if Path(root).is_absolute())

    if must_exist:
        target = _resolve_existing(path)
        if not target.is_file():
            _refuse()
    elif allow_directory:
        # Non-strict on purpose: what does not exist cannot be outside the roots,
        # and ``resolve()`` still follows the links that do.  An unreadable path is
        # refused rather than reported as an engine failure, as a read would be.
        try:
            target = path.resolve()
        except OSError:
            _refuse()
    else:
        parent = _resolve_existing(path.parent)
        if not parent.is_dir():
            _refuse()
        # resolve() also normalises a trailing ``.``/``..``, which would otherwise
        # look like it lived inside the root while naming its parent.
        target = (parent / path.name).resolve()
        if target.is_dir():
            _refuse()

    if not any(target == root or root in target.parents for root in allowed):
        _refuse()
    return target


def _resolve_existing(path: Path) -> Path:
    """Resolve ``path`` through links; a missing or unreadable target is refused."""

    try:
        return path.resolve(strict=True)
    except OSError:
        _refuse()


def _refuse_remote(raw: str) -> None:
    """Refuse URLs, provider schemes and virtual file systems before any I/O."""

    if is_remote_uri(raw):
        _refuse()


def _refuse() -> NoReturn:
    # The raw path stays out of the message: it may carry credentials in a
    # provider URI, and the caller already knows what it asked for.
    raise FrameworkError(PATH_NOT_ALLOWED, "path is not an allowed local file")
