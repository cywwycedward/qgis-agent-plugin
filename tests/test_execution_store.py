"""记录存储：每次读都给出独立快照、迁移只走一个状态机、终结记录会过期。

Every read hands back an independent snapshot, transitions follow one state machine, and
finished records expire on their own TTL.
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from qgis_agent_plugin.core.config import Settings
from qgis_agent_plugin.core.contracts import ExecutionStrategy
from qgis_agent_plugin.core.execution_store import TERMINAL_STATUSES, ExecutionStore
from qgis_agent_plugin.core.models import (
    EXECUTION_NOT_FOUND,
    INTERNAL_ERROR,
    ErrorDetail,
    ExecutionRecord,
    ExecutionStatus,
    FrameworkError,
    OperationKind,
    utc_now,
)

QUEUED = ExecutionStatus.QUEUED
PREPARING = ExecutionStatus.PREPARING
SCHEDULED = ExecutionStatus.SCHEDULED
RUNNING = ExecutionStatus.RUNNING
CANCEL_REQUESTED = ExecutionStatus.CANCEL_REQUESTED
FINALIZING = ExecutionStatus.FINALIZING
SUCCEEDED = ExecutionStatus.SUCCEEDED
FAILED = ExecutionStatus.FAILED
CANCELLED = ExecutionStatus.CANCELLED

# 状态机按边列举：每对 (起始, 目标) 是一条允许的迁移
# the state machine written as a list of edges: each (start, target) pair is one allowed
# transition
LEGAL = [
    (QUEUED, RUNNING),
    (QUEUED, PREPARING),
    (QUEUED, CANCELLED),
    (QUEUED, FAILED),
    (PREPARING, SCHEDULED),
    (PREPARING, FAILED),
    (SCHEDULED, RUNNING),
    (SCHEDULED, CANCEL_REQUESTED),
    (SCHEDULED, FAILED),
    (RUNNING, SUCCEEDED),
    (RUNNING, FINALIZING),
    (RUNNING, FAILED),
    (RUNNING, CANCEL_REQUESTED),
    (CANCEL_REQUESTED, CANCELLED),
    (CANCEL_REQUESTED, FAILED),
    (FINALIZING, SUCCEEDED),
    (FINALIZING, FAILED),
]

ILLEGAL = [
    (QUEUED, SUCCEEDED),
    (QUEUED, SCHEDULED),
    (QUEUED, CANCEL_REQUESTED),
    (QUEUED, FINALIZING),
    (PREPARING, RUNNING),
    (PREPARING, CANCELLED),
    (PREPARING, CANCEL_REQUESTED),
    (PREPARING, SUCCEEDED),
    (SCHEDULED, PREPARING),
    (SCHEDULED, SUCCEEDED),
    (SCHEDULED, FINALIZING),
    (SCHEDULED, CANCELLED),
    (RUNNING, QUEUED),
    (RUNNING, PREPARING),
    (RUNNING, SCHEDULED),
    (RUNNING, CANCELLED),
    (CANCEL_REQUESTED, QUEUED),
    (CANCEL_REQUESTED, RUNNING),
    (CANCEL_REQUESTED, SUCCEEDED),
    (FINALIZING, QUEUED),
    (FINALIZING, RUNNING),
    (FINALIZING, CANCELLED),
    (FINALIZING, CANCEL_REQUESTED),
]


class Clock:
    """手工推进的单调时钟，这样 TTL 才可度量。

    A monotonic clock the test moves by hand, so the TTL can be measured.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_record(
    status: ExecutionStatus = QUEUED, execution_id: str = "x", **fields: object
) -> ExecutionRecord:
    return ExecutionRecord.model_validate(
        {
            "execution_id": execution_id,
            "operation": "test.echo",
            "kind": OperationKind.QUERY,
            "strategy": ExecutionStrategy.MAIN_THREAD,
            "status": status,
            "created_at": utc_now(),
            "project_epoch": 0,
            **fields,
        }
    )


def succeeded(execution_id: str) -> ExecutionRecord:
    return make_record(
        SUCCEEDED,
        execution_id=execution_id,
        progress=100,
        result={"value": execution_id},
        finished_at=utc_now(),
    )


def move(store: ExecutionStore, execution_id: str, status: ExecutionStatus) -> ExecutionRecord:
    """按目标状态补齐它必需的字段后再迁移。

    Transition with whatever fields the target status requires.
    """

    if status is SUCCEEDED:
        return store.transition(execution_id, status, result={"value": "ok"}, progress=100)
    if status is FAILED:
        return store.transition(
            execution_id, status, error=ErrorDetail(code=INTERNAL_ERROR, message="boom")
        )
    return store.transition(execution_id, status)


def test_a_stored_record_comes_back_as_an_independent_copy() -> None:
    store = ExecutionStore(Settings())
    store.create(succeeded("done"))
    copy = store.get("done")
    copy.status = FAILED
    assert copy.result is not None
    copy.result["value"] = "edited"
    fresh = store.get("done")
    assert fresh.status is SUCCEEDED
    assert fresh.result == {"value": "done"}


def test_create_hands_back_a_snapshot_too() -> None:
    store = ExecutionStore(Settings())
    stored = store.create(make_record(QUEUED))
    stored.status = CANCELLED
    assert store.get("x").status is QUEUED


def test_an_unknown_id_is_not_found() -> None:
    with pytest.raises(FrameworkError) as caught:
        ExecutionStore(Settings()).get("missing")
    assert caught.value.code == EXECUTION_NOT_FOUND


@pytest.mark.parametrize(("start", "target"), LEGAL)
def test_every_declared_transition_is_accepted(
    start: ExecutionStatus, target: ExecutionStatus
) -> None:
    store = ExecutionStore(Settings())
    store.create(make_record(start))
    assert move(store, "x", target).status is target


@pytest.mark.parametrize(("start", "target"), ILLEGAL)
def test_an_undeclared_transition_is_a_defect(
    start: ExecutionStatus, target: ExecutionStatus
) -> None:
    store = ExecutionStore(Settings())
    store.create(make_record(start))
    with pytest.raises(FrameworkError) as caught:
        move(store, "x", target)
    assert caught.value.code == INTERNAL_ERROR
    assert store.get("x").status is start


@pytest.mark.parametrize("status", [SUCCEEDED, FAILED, CANCELLED])
def test_a_terminal_record_cannot_change_again(status: ExecutionStatus) -> None:
    store = ExecutionStore(Settings())
    record = make_record(
        status,
        progress=100 if status is SUCCEEDED else 0,
        result={"value": "done"} if status is SUCCEEDED else None,
        error=ErrorDetail(code=INTERNAL_ERROR, message="boom") if status is FAILED else None,
        finished_at=utc_now(),
    )
    store.create(record)
    for write in (
        lambda: store.transition("x", RUNNING),
        lambda: store.transition("x", progress=50),
    ):
        with pytest.raises(FrameworkError) as caught:
            write()
        assert caught.value.code == INTERNAL_ERROR
    assert store.get("x").status is status


def test_progress_is_written_without_moving_the_status() -> None:
    store = ExecutionStore(Settings())
    store.create(make_record(QUEUED))
    record = store.transition("x", progress=40)
    assert record.status is QUEUED
    assert record.progress == 40


def test_asking_for_the_current_status_changes_nothing() -> None:
    store = ExecutionStore(Settings())
    store.create(make_record(QUEUED))
    record = store.transition("x", QUEUED)
    assert record.status is QUEUED
    assert record.finished_at is None


def test_a_succeeded_record_needs_its_result_and_full_progress() -> None:
    store = ExecutionStore(Settings())
    store.create(make_record(RUNNING))
    with pytest.raises(ValidationError):
        store.transition("x", SUCCEEDED, progress=100)
    with pytest.raises(ValidationError):
        store.transition("x", SUCCEEDED, result={"value": "ok"})
    assert store.get("x").status is RUNNING


def test_a_terminal_transition_fills_in_the_finish_time() -> None:
    store = ExecutionStore(Settings())
    store.create(make_record(QUEUED))
    assert store.transition("x", CANCELLED).finished_at is not None
    store.create(make_record(RUNNING, execution_id="y"))
    failed = move(store, "y", FAILED)
    assert failed.finished_at is not None
    assert failed.error is not None and failed.error.code == INTERNAL_ERROR


def test_the_finished_history_is_capped_at_the_configured_count() -> None:
    store = ExecutionStore(Settings(max_terminal_records=3), clock=Clock())
    for index in range(4):
        store.create(succeeded(f"done-{index}"))
    assert store.ids(TERMINAL_STATUSES) == ["done-1", "done-2", "done-3"]
    with pytest.raises(FrameworkError) as caught:
        store.get("done-0")
    assert caught.value.code == EXECUTION_NOT_FOUND


def test_an_execution_in_progress_is_never_evicted() -> None:
    store = ExecutionStore(Settings(max_terminal_records=1), clock=Clock())
    store.create(make_record(RUNNING, execution_id="live"))
    store.create(succeeded("done-1"))
    store.create(succeeded("done-2"))
    assert store.get("live").status is RUNNING
    assert store.ids(TERMINAL_STATUSES) == ["done-2"]


def test_a_finished_record_expires_after_its_ttl() -> None:
    clock = Clock()
    store = ExecutionStore(
        Settings(terminal_ttl_seconds=1800, max_terminal_records=200), clock=clock
    )
    store.create(succeeded("old"))
    clock.advance(1800)
    store.create(succeeded("new"))
    assert store.ids(TERMINAL_STATUSES) == ["new"]
    with pytest.raises(FrameworkError) as caught:
        store.get("old")
    assert caught.value.code == EXECUTION_NOT_FOUND


def test_reading_a_finished_record_past_its_ttl_is_not_found() -> None:
    clock = Clock()
    store = ExecutionStore(Settings(terminal_ttl_seconds=1800), clock=clock)
    store.create(succeeded("old"))

    # 只有时钟在走，``get`` 是第一次读取，所以没有更早的调用能把它清掉
    # only the clock moves, and ``get`` is the first read, so no earlier call may have expired
    # it
    clock.advance(1800)
    with pytest.raises(FrameworkError) as caught:
        store.get("old")
    assert caught.value.code == EXECUTION_NOT_FOUND


def test_listing_records_past_their_ttl_returns_nothing() -> None:
    clock = Clock()
    store = ExecutionStore(Settings(terminal_ttl_seconds=1800), clock=clock)
    store.create(succeeded("old"))

    clock.advance(1800)
    assert store.ids(TERMINAL_STATUSES) == []
    with pytest.raises(FrameworkError) as caught:
        store.get("old")
    assert caught.value.code == EXECUTION_NOT_FOUND


def test_the_finish_log_carries_the_duration_and_the_error_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = ExecutionStore(Settings())
    with caplog.at_level("INFO"):
        store.create(make_record(RUNNING))
        move(store, "x", FAILED)
    # 创建与迁移都留痕 / both the birth and the transition are recorded
    assert "execution x test.echo main_thread RUNNING" in caplog.text
    assert re.search(r"-> FAILED \(\d+\.\d{3}s, INTERNAL_ERROR\)", caplog.text)


def test_an_unreadable_timestamp_does_not_break_the_finished_write(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = ExecutionStore(Settings())
    store.create(make_record(RUNNING, created_at="yesterday"))

    with caplog.at_level("INFO"):
        record = move(store, "x", FAILED)
    assert record.status is FAILED
    assert store.get("x").status is FAILED
    assert " (0.000s, INTERNAL_ERROR)" in caplog.text


def test_ids_lists_only_the_requested_statuses() -> None:
    store = ExecutionStore(Settings())
    store.create(make_record(QUEUED, execution_id="waiting"))
    store.create(make_record(RUNNING, execution_id="busy"))
    store.create(succeeded("done"))
    assert store.ids({QUEUED}) == ["waiting"]
    assert store.ids({RUNNING, SUCCEEDED}) == ["busy", "done"]
