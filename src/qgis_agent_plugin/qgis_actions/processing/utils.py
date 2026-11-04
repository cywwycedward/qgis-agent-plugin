"""What the processing commands share: how an algorithm identifier is resolved.

A caller names an algorithm the same way to both commands, so both refuse an
identifier QGIS does not have with the same code and the same words -- a caller
that misreads one answer has to be able to read the other as the same answer.
"""

from __future__ import annotations

from typing import Any

from ..base import ActionError


def require_algorithm(identifier: str) -> Any:
    """Return the algorithm that identifier names, or refuse it by name.

    QGIS resolves an exact id, and the ``qgis:``-prefixed alias some providers
    register; anything else -- a missing provider prefix, a different case, a
    stray space -- is refused here rather than guessed at.  The refusal says how
    to find one too: the listing command is where an id comes from, and a caller
    holding the wrong one has nowhere else to look.
    """

    from qgis.core import QgsApplication

    algorithm = QgsApplication.processingRegistry().algorithmById(identifier)
    if algorithm is None:
        raise ActionError(
            "algorithm_unavailable",
            f"Unknown algorithm: {identifier}. List them with list_processing_algorithms, "
            'e.g. provider="native", group="vectorgeometry"',
        )
    return algorithm
