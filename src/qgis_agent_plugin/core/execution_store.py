"""本会话里的全部执行记录，以及唯一会移动它们的代码。

记录是快照类型：谁要一份记录就拿到一份深拷贝，所以调用方持有引用也改不到存储，存储也不会被读到
引擎从未写过的状态。状态转移在这里对着转移表校验，而不是在引擎里，因为这里是唯一写入状态的地方：
表里没列的边就是缺陷，藏起来只会留下一条谁也解释不了的记录。

保留期在访问时和每条记录结束时执行，从不用定时器：读到一条过了 TTL 的记录，
看起来必须就像它已经没了。

Every execution record in the session, and the only code that moves them.

A record is a snapshot type: whoever asks for one gets a deep copy, so no caller
can edit the store by holding a reference and the store cannot be read in a state
the engine never wrote.  Transitions are checked against the transition table
here rather than in the engine, because this is the one place a status is written:
an edge the table does not list is a defect, and hiding it would leave a record in
a state nobody can explain.

Retention is applied on access and whenever a record finishes, never by a timer:
reading a record past its TTL has to look like the record is gone.
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

# 状态转移表，逐条边：某一行里没出现的状态没有合法后继。
# The transition table, edge for edge: a status absent from a row has no legal successor.
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
    """活跃执行与终态历史，有上限、且只给快照。

    Activity reads and terminal history, capped and snapshotted.
    """

    def __init__(self, settings: Settings, *, clock: Callable[[], float] | None = None) -> None:
        # shortcut: 保留期只在访问与记录结束时执行，过期记录会一直占内存，直到有东西
        # 碰存储；等空闲会话必须自己收缩时再升级。
        # shortcut: retention runs on access and when a record finishes, so an expired record
        # stays in memory until something touches the store; upgrade when an idle session must
        # shrink on its own.
        self._max_terminal_records = settings.max_terminal_records
        self._terminal_ttl_seconds = settings.terminal_ttl_seconds
        self._clock = clock or time.monotonic
        self._records: dict[str, ExecutionRecord] = {}
        self._terminal_at: dict[str, float] = {}

    def create(self, record: ExecutionRecord) -> ExecutionRecord:
        """存一条刚构造好的记录；终态记录立即计入历史。

        Store a freshly built record; a terminal one counts as history at once.
        """

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
        """一条记录的独立副本，否则 ``EXECUTION_NOT_FOUND``。

        先执行保留期清理，所以过了 TTL 才读的记录已经没了，而不是再返回它最后一次。

        An independent copy of the record, or ``EXECUTION_NOT_FOUND``.

        Retention is applied first, so a record read past its TTL is already gone
        instead of being returned one last time.
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
        """``statuses`` 中任一状态的保留记录的 id，从旧到新。

        The ids of every retained record in one of ``statuses``, oldest first.
        """

        self._prune()
        return [
            record.execution_id for record in self._records.values() if record.status in statuses
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
        """让一条记录沿一条合法边前进，并返回新的快照。

        ``status=None`` 保留当前状态，只写给出的字段，运行中的进度就是这么记的。给某个字段传
        ``None`` 表示不动它；没有任何地方需要把字段清空。终态记录用任何参数都写不了第二次。终态会
        自动补上 ``finished_at``，免得哪个调用方忘了。

        Move a record along one legal edge and return the new snapshot.

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
        # 通过校验重建：不可能的组合在这里失败而不是被存下；
        # 直接改属性的 model_copy 会跳过这项检查。
        # Rebuilt through validation: an impossible combination fails here instead of being
        # stored; model_copy and attribute writes would skip that check.
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
        """先丢掉过期的终态记录，再在超上限时从最旧的开始丢。

        Drop expired terminal records, then the oldest, while over the cap.
        """

        now = self._clock()
        for execution_id, finished in list(self._terminal_at.items()):
            if now - finished >= self._terminal_ttl_seconds:
                self._forget(execution_id)
        # 插入顺序就是结束顺序，所以最旧的终态记录最先被丢掉。
        # Insertion order is finish order, so the oldest terminal record goes first.
        while len(self._terminal_at) > self._max_terminal_records:
            self._forget(next(iter(self._terminal_at)))

    def _forget(self, execution_id: str) -> None:
        self._records.pop(execution_id, None)
        self._terminal_at.pop(execution_id, None)


def _log_details(record: ExecutionRecord) -> str:
    """上一次状态转移的耗时与错误码。

    Elapsed time and error code for the last transition.
    """

    details = [
        f"{_elapsed_seconds(record):.3f}s" if record.finished_at is not None else "",
        record.error.code if record.error is not None else "",
    ]
    names = [detail for detail in details if detail]
    return f" ({', '.join(names)})" if names else ""


def _elapsed_seconds(record: ExecutionRecord) -> float:
    """这次调用花了多久，从它自己的墙上时钟时间戳算出。

    时间戳是模型不校验的普通字符串，一行日志永远不能把一次完成的写入变成异常。

    How long the call took, read from its own wall-clock stamps.

    The stamps are plain strings the model does not validate, and a log line must
    never turn a finished write into an exception.
    """

    try:
        created = datetime.fromisoformat(record.created_at)
        finished = datetime.fromisoformat(record.finished_at or record.created_at)
        return (finished - created).total_seconds()
    except (TypeError, ValueError):
        return 0.0
