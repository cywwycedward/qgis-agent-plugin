"""Every execution record in the session, and the only code that moves them.

A record is a snapshot type: whoever asks for one gets a deep copy, so no caller
can edit the store by holding a reference and the store cannot be read in a state
the engine never wrote.  Transitions are checked against the PRD 8.2 table here
rather than in the engine, because this is the one place a status is written: an
edge the table does not list is a defect, and hiding it would leave a record in a
state nobody can explain (PRD 8.2, 12.3).

Retention is applied on access and whenever a record finishes, never by a timer:
reading a record past its TTL has to look like the record is gone (PRD 8.5, AT-24).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection
from datetime import datetime
from typing import Any

from .config import Settings
from .models import (
    EXECUTION_NOT_FOUND,
    INTERNAL_ERROR,
    ErrorDetail,
    ExecutionRecord,
    ExecutionStatus,
    FrameworkError,
    utc_now,
)

_LOGGER = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset(
    {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}
)

# PRD 8.2, edge for edge.  A status absent from a row has no legal successor.
_TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.QUEUED: frozenset(
        {
            ExecutionStatus.RUNNING,
            ExecutionStatus.PREPARING,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.PREPARING: frozenset({ExecutionStatus.SCHEDULED, ExecutionStatus.FAILED}),
    ExecutionStatus.SCHEDULED: frozenset(
        {
            ExecutionStatus.RUNNING,
            ExecutionStatus.CANCEL_REQUESTED,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.RUNNING: frozenset(
        {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FINALIZING,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCEL_REQUESTED,
        }
    ),
    ExecutionStatus.CANCEL_REQUESTED: frozenset(
        {ExecutionStatus.CANCELLED, ExecutionStatus.FAILED}
    ),
    ExecutionStatus.FINALIZING: frozenset({ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED}),
    ExecutionStatus.SUCCEEDED: frozenset(),
    ExecutionStatus.FAILED: frozenset(),
    ExecutionStatus.CANCELLED: frozenset(),
}


class ExecutionStore:
    """Activity reads and terminal history, capped and snapshotted (PRD 6.6, 8.5)."""

    def __init__(self, settings: Settings, *, clock: Callable[[], float] | None = None) -> None:
        # shortcut: retention runs on access and on each finished record, so an expired
        # record still occupies memory until something touches the store; upgrade when
        # an idle session has to shrink on its own.
        self._max_terminal_records = settings.max_terminal_records
        self._terminal_ttl_seconds = settings.terminal_ttl_seconds
        self._clock = clock or time.monotonic
        self._records: dict[str, ExecutionRecord] = {}
        self._terminal_at: dict[str, float] = {}

    def create(self, record: ExecutionRecord) -> ExecutionRecord:
        """Store a freshly built record; a terminal one counts as history at once."""

        self._records[record.execution_id] = record
        if record.status in TERMINAL_STATUSES:
            self._terminal_at[record.execution_id] = self._clock()
            self._prune()
        _LOGGER.info(
            "execution %s %s %s %s",
            record.execution_id,
            record.operation,
            record.strategy,
            record.status,
        )
        return record.model_copy(deep=True)

    def get(self, execution_id: str) -> ExecutionRecord:
        """An independent copy of the record, or ``EXECUTION_NOT_FOUND``.

        Retention is applied first, so a record read past its TTL is already gone
        instead of being returned one last time (PRD 8.5, AT-24).
        """

        self._prune()
        try:
            record = self._records[execution_id]
        except KeyError:
            raise FrameworkError(
                EXECUTION_NOT_FOUND, f"unknown execution: {execution_id}"
            ) from None
        return record.model_copy(deep=True)

    def ids(self, statuses: Collection[ExecutionStatus]) -> list[str]:
        """The ids of every retained record in one of ``statuses``, oldest first."""

        self._prune()
        return [
            record.execution_id
            for record in self._records.values()
            if record.status in statuses
        ]

    def transition(
        self,
        execution_id: str,
        status: ExecutionStatus | None = None,
        *,
        progress: float | None = None,
        result: dict[str, Any] | None = None,
        error: ErrorDetail | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> ExecutionRecord:
        """Move a record along one PRD 8.2 edge and return the new snapshot.

        ``status=None`` keeps the current status and only writes the fields given,
        which is how progress is recorded while an execution runs.  Passing
        ``None`` for a field leaves it alone; nothing ever needs to clear a field.
        A terminal record cannot be written again, by any argument.  ``finished_at``
        is filled in for a terminal status so no caller can forget it.
        """

        current = self.get(execution_id)
        if current.status in TERMINAL_STATUSES:
            raise FrameworkError(
                INTERNAL_ERROR, f"{execution_id}: {current.status} is final and cannot change"
            )

        changes: dict[str, Any] = {}
        if status is not None and status is not current.status:
            if status not in _TRANSITIONS[current.status]:
                raise FrameworkError(
                    INTERNAL_ERROR,
                    f"{execution_id}: {current.status} -> {status} is not a transition",
                )
            changes["status"] = status
        for field, value in (
            ("progress", progress),
            ("result", result),
            ("error", error),
            ("started_at", started_at),
            ("finished_at", finished_at),
        ):
            if value is not None:
                changes[field] = value
        if not changes:
            return current

        if changes.get("status", current.status) in TERMINAL_STATUSES:
            changes.setdefault("finished_at", utc_now())
        # Rebuilt through validation: an impossible combination fails here instead of
        # being stored.  model_copy/attribute writes would skip that check (F1 §2.2).
        record = ExecutionRecord.model_validate({**current.model_dump(), **changes})
        self._records[execution_id] = record
        if record.status in TERMINAL_STATUSES and current.status not in TERMINAL_STATUSES:
            self._terminal_at[execution_id] = self._clock()
            self._prune()
        _LOGGER.info(
            "execution %s %s %s %s -> %s%s",
            execution_id,
            record.operation,
            record.strategy,
            current.status,
            record.status,
            _log_details(record),
        )
        return record.model_copy(deep=True)

    def _prune(self) -> None:
        """Drop expired terminal records, then the oldest, while over the cap."""

        now = self._clock()
        for execution_id, finished in list(self._terminal_at.items()):
            if now - finished >= self._terminal_ttl_seconds:
                self._forget(execution_id)
        # Insertion order is finish order, so the oldest terminal record goes first.
        while len(self._terminal_at) > self._max_terminal_records:
            self._forget(next(iter(self._terminal_at)))

    def _forget(self, execution_id: str) -> None:
        self._records.pop(execution_id, None)
        self._terminal_at.pop(execution_id, None)


def _log_details(record: ExecutionRecord) -> str:
    """Elapsed time and error code for the last transition (PRD 11.3)."""

    details = [
        f"{_elapsed_seconds(record):.3f}s" if record.finished_at is not None else "",
        record.error.code if record.error is not None else "",
    ]
    names = [detail for detail in details if detail]
    return f" ({', '.join(names)})" if names else ""


def _elapsed_seconds(record: ExecutionRecord) -> float:
    """How long the call took, read from its own wall-clock stamps.

    The stamps are plain strings the model does not validate (F1 §5), and a log line
    must never turn a finished write into an exception.
    """

    try:
        created = datetime.fromisoformat(record.created_at)
        finished = datetime.fromisoformat(record.finished_at or record.created_at)
        return (finished - created).total_seconds()
    except (TypeError, ValueError):
        return 0.0
