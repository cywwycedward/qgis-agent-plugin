"""The engine: a refused call leaves no record, an accepted one ends in a record.

The Qt event queue is stood in for by a list the test drains, which is also how the
thread rule is checked; the last test starts a headless ``QgsApplication``, because
``aboutToBeCleared`` only exists for real (AT-04, AT-09, AT-16, AT-18, AT-24, AT-26).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel, field_validator

from qgis_agent_plugin.core.config import Settings
from qgis_agent_plugin.core.context import ProjectLifecycle
from qgis_agent_plugin.core.contracts import (
    BackgroundOperationMixin,
    BaseOperation,
    BaseQuery,
    ExecutionStrategy,
    MainThreadOperationMixin,
    OperationContext,
    TaskControl,
)
from qgis_agent_plugin.core.engine import BackgroundRunner, OperationEngine
from qgis_agent_plugin.core.execution_store import TERMINAL_STATUSES, ExecutionStore
from qgis_agent_plugin.core.models import (
    ENGINE_CLOSING,
    EXECUTION_NOT_FOUND,
    INTERNAL_ERROR,
    INVALID_INPUT,
    OPERATION_NOT_FOUND,
    OUTPUT_VALIDATION_FAILED,
    PATH_NOT_ALLOWED,
    TASK_SUBMISSION_FAILED,
    TOO_MANY_EXECUTIONS,
    UNSUPPORTED_EXECUTION_MODE,
    WRONG_THREAD,
    ExecutionRecord,
    ExecutionStatus,
    FrameworkError,
    OperationKind,
    Params,
)
from qgis_agent_plugin.core.registry import OperationRegistry
from qgis_agent_plugin.core.runners.main_thread import MainThreadRunner

QUEUED = ExecutionStatus.QUEUED
PREPARING = ExecutionStatus.PREPARING
SCHEDULED = ExecutionStatus.SCHEDULED
RUNNING = ExecutionStatus.RUNNING
CANCEL_REQUESTED = ExecutionStatus.CANCEL_REQUESTED
FINALIZING = ExecutionStatus.FINALIZING
SUCCEEDED = ExecutionStatus.SUCCEEDED
FAILED = ExecutionStatus.FAILED
CANCELLED = ExecutionStatus.CANCELLED

CALLED: list[str] = []


@pytest.fixture(autouse=True)
def _forget_calls() -> None:
    CALLED.clear()


class ValueParams(Params):
    value: str = "hello"


class ValueResult(BaseModel):
    value: str


class Echo(BaseQuery[ValueParams, ValueResult], MainThreadOperationMixin[ValueParams, ValueResult]):
    name = "test.echo"
    description = "return the value it was given"
    input_model = ValueParams
    output_model = ValueResult

    def execute_main(self, ctx: OperationContext, request: ValueParams) -> ValueResult:
        CALLED.append(self.name)
        return ValueResult(value=request.value)


class Refusing(
    BaseQuery[ValueParams, ValueResult], MainThreadOperationMixin[ValueParams, ValueResult]
):
    name = "test.refuse"
    description = "refuse the call with a business error code"
    input_model = ValueParams
    output_model = ValueResult

    def execute_main(self, ctx: OperationContext, request: ValueParams) -> ValueResult:
        CALLED.append(self.name)
        raise FrameworkError(PATH_NOT_ALLOWED, "path is not an allowed local file")


class Breaking(
    BaseQuery[ValueParams, ValueResult], MainThreadOperationMixin[ValueParams, ValueResult]
):
    name = "test.break"
    description = "raise something the framework did not expect"
    input_model = ValueParams
    output_model = ValueResult

    def execute_main(self, ctx: OperationContext, request: ValueParams) -> ValueResult:
        CALLED.append(self.name)
        raise RuntimeError("boom")


class Lying(
    BaseQuery[ValueParams, ValueResult], MainThreadOperationMixin[ValueParams, ValueResult]
):
    name = "test.lie"
    description = "return something that is not its declared output"
    input_model = ValueParams
    output_model = ValueResult

    def execute_main(self, ctx: OperationContext, request: ValueParams) -> ValueResult:
        CALLED.append(self.name)
        return cast(ValueResult, object())


class Sum(
    BaseQuery[ValueParams, ValueResult],
    BackgroundOperationMixin[ValueParams, ValueResult, str, str],
):
    name = "test.sum"
    description = "an operation whose work belongs in a worker"
    input_model = ValueParams
    output_model = ValueResult

    def prepare_main(self, ctx: OperationContext, request: ValueParams) -> str:
        return request.value

    def execute_worker(self, snapshot: str, control: TaskControl) -> str:
        return snapshot

    def finalize_main(
        self, ctx: OperationContext, request: ValueParams, result: str
    ) -> ValueResult:
        return ValueResult(value=result)


class Replacing(
    BaseQuery[ValueParams, ValueResult],
    MainThreadOperationMixin[ValueParams, ValueResult],
):
    """A main-thread call that replaces the project while it is still running."""

    name = "test.replace"
    description = "trigger a project replacement from inside execute_main"
    input_model = ValueParams
    output_model = ValueResult

    def __init__(self, replace: Callable[[], None]) -> None:
        self._replace = replace

    def execute_main(self, ctx: OperationContext, request: ValueParams) -> ValueResult:
        CALLED.append(self.name)
        self._replace()
        return ValueResult(value=request.value)


def no_adapter(*args: Any) -> None:
    """Accept the background hand-off and do nothing: these tests only drive states."""


class ExplodingParams(Params):
    value: str

    @field_validator("value")
    @classmethod
    def _explode(cls, value: str) -> str:
        # Pydantic only wraps ValueError and AssertionError, so this escapes validation.
        raise RuntimeError("validator exploded")


class Exploding(
    BaseQuery[ExplodingParams, ValueResult],
    MainThreadOperationMixin[ExplodingParams, ValueResult],
):
    name = "test.explode"
    description = "an operation whose input model is broken"
    input_model = ExplodingParams
    output_model = ValueResult

    def execute_main(self, ctx: OperationContext, request: ExplodingParams) -> ValueResult:
        CALLED.append(self.name)
        return ValueResult(value=request.value)


class RefusingParams(Params):
    value: str

    @field_validator("value")
    @classmethod
    def _refuse(cls, value: str) -> str:
        # An operation's own refusal must keep its code, not become INVALID_INPUT.
        raise FrameworkError(PATH_NOT_ALLOWED, "value is not an allowed path")


class RefusingInput(
    BaseQuery[RefusingParams, ValueResult],
    MainThreadOperationMixin[RefusingParams, ValueResult],
):
    name = "test.refuse_input"
    description = "an operation whose input model refuses with a business code"
    input_model = RefusingParams
    output_model = ValueResult

    def execute_main(self, ctx: OperationContext, request: RefusingParams) -> ValueResult:
        CALLED.append(self.name)
        return ValueResult(value=request.value)


class Queue:
    """The Qt event queue, stood in for by a list the test drains."""

    def __init__(self) -> None:
        self.callbacks: list[Callable[[], None]] = []

    def __call__(self, callback: Callable[[], None]) -> None:
        self.callbacks.append(callback)

    def tick(self) -> None:
        while self.callbacks:
            self.callbacks.pop(0)()


class Harness:
    """An engine, the registry it dispatches, and the queue it posts into."""

    def __init__(
        self,
        *operations: BaseOperation,
        settings: Settings | None = None,
        background_runner: BackgroundRunner | None = None,
    ) -> None:
        self.registry = OperationRegistry()
        for operation in operations:
            self.registry.register(operation)
        self.lifecycle = ProjectLifecycle()
        self.queue = Queue()
        self.engine = OperationEngine(
            self.registry,
            settings or Settings(),
            self.lifecycle,
            post=self.queue,
            background_runner=background_runner,
        )
        # The store is the engine's own; the tests read it to prove a refused call
        # left nothing behind and to reach states only F5 can produce.
        self.store: ExecutionStore = self.engine._store

    def records(self) -> list[ExecutionRecord]:
        return [
            self.store.get(execution_id) for execution_id in self.store.ids(set(ExecutionStatus))
        ]


def test_a_sync_query_succeeds_and_carries_its_output() -> None:
    harness = Harness(Echo())
    record = harness.engine.run_sync("test.echo", {"value": "world"})
    assert record.status is SUCCEEDED
    assert record.result == {"value": "world"}
    assert record.progress == 100
    assert record.error is None
    assert record.started_at is not None
    assert record.finished_at is not None
    assert record.project_epoch == 0
    assert CALLED == ["test.echo"]


def test_a_succeeded_record_is_json_safe() -> None:
    record = Harness(Echo()).engine.run_sync("test.echo", {})
    assert json.loads(json.dumps(record.model_dump(mode="json")))["status"] == "SUCCEEDED"


def test_a_sync_call_refuses_a_background_operation() -> None:
    harness = Harness(Sum())
    with pytest.raises(FrameworkError) as caught:
        harness.engine.run_sync("test.sum", {})
    assert caught.value.code == UNSUPPORTED_EXECUTION_MODE
    assert harness.records() == []


def test_an_unknown_operation_is_refused_without_a_record() -> None:
    harness = Harness(Echo())
    for call in (harness.engine.run_sync, harness.engine.submit):
        with pytest.raises(FrameworkError) as caught:
            call("test.missing", {})
        assert caught.value.code == OPERATION_NOT_FOUND
    assert harness.records() == []


@pytest.mark.parametrize("call_name", ["run_sync", "submit"])
def test_invalid_input_ends_as_a_failed_record_and_does_not_run(call_name: str) -> None:
    harness = Harness(Echo())
    call = getattr(harness.engine, call_name)
    record = call("test.echo", {"value": "x", "surprise": 1})
    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == INVALID_INPUT
    # The message names the offending field, and names no other field and no value.
    assert record.error.message == "test.echo: invalid input at surprise"
    # A non-success terminal record never claims to be complete (PRD 6.6).
    assert record.progress == 0
    assert CALLED == []
    assert harness.queue.callbacks == []


def test_a_validator_that_refuses_with_a_business_code_keeps_it() -> None:
    harness = Harness(RefusingInput())
    record = harness.engine.submit("test.refuse_input", {"value": "x"})
    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == PATH_NOT_ALLOWED
    assert record.error.message == "value is not an allowed path"


def test_an_input_model_that_raises_unexpectedly_ends_as_failed() -> None:
    harness = Harness(Exploding())
    for call in (harness.engine.run_sync, harness.engine.submit):
        record = call("test.explode", {"value": "x"})
        assert record.status is FAILED
        assert record.error is not None
        assert record.error.code == INTERNAL_ERROR
        assert "validator exploded" not in record.error.message
    # Nothing was left waiting: the call cannot eat a queue slot.
    assert harness.store.ids({QUEUED}) == []
    assert CALLED == []


def test_calls_whose_input_explodes_cannot_exhaust_the_queue() -> None:
    harness = Harness(Exploding(), Echo())
    for _ in range(33):  # one more than max_pending
        assert harness.engine.submit("test.explode", {"value": "x"}).status is FAILED

    queued = harness.engine.submit("test.echo", {})
    assert queued.status is QUEUED
    harness.queue.tick()
    assert harness.engine.get(queued.execution_id).status is SUCCEEDED


def test_a_submitted_call_waits_for_the_event_queue() -> None:
    harness = Harness(Echo())
    record = harness.engine.submit("test.echo", {"value": "queued"})
    assert record.status is QUEUED
    assert record.progress == 0
    assert record.started_at is None
    assert record.finished_at is None
    assert CALLED == []
    assert len(harness.queue.callbacks) == 1

    harness.queue.tick()
    finished = harness.engine.get(record.execution_id)
    assert finished.status is SUCCEEDED
    assert finished.result == {"value": "queued"}
    assert CALLED == ["test.echo"]


def test_a_queued_call_cancelled_before_its_turn_never_runs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = Harness(Echo())
    record = harness.engine.submit("test.echo", {})
    receipt = harness.engine.cancel(record.execution_id)
    assert receipt.accepted is True
    assert receipt.status is CANCELLED
    assert receipt.reason is None

    with caplog.at_level(logging.ERROR):
        harness.queue.tick()
    assert CALLED == []
    cancelled = harness.engine.get(record.execution_id)
    assert cancelled.status is CANCELLED
    assert cancelled.progress == 0
    # Skipped on purpose, not "started, refused by the state machine and logged":
    # a missing skip leaves an ERROR record from the Qt-slot guard.
    assert cancelled.started_at is None
    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []


def test_a_cancelled_background_call_is_never_handed_to_the_adapter() -> None:
    handed: list[ExecutionRecord] = []

    def adapter(
        engine: OperationEngine, record: ExecutionRecord, operation: BaseOperation, request: Any
    ) -> None:
        handed.append(record)

    harness = Harness(Sum(), background_runner=adapter)
    record = harness.engine.submit("test.sum", {})
    assert harness.engine.cancel(record.execution_id).status is CANCELLED

    harness.queue.tick()
    assert handed == []
    assert harness.store.get(record.execution_id).status is CANCELLED


def test_a_stale_callback_does_not_run_a_call_that_already_moved_on() -> None:
    harness = Harness(Echo())
    record = harness.engine.submit("test.echo", {})
    # Something else already started it, so this queued callback is a leftover.
    harness.store.transition(record.execution_id, RUNNING)

    harness.queue.tick()
    assert CALLED == []


def test_a_finished_call_cannot_be_cancelled() -> None:
    harness = Harness(Echo())
    record = harness.engine.run_sync("test.echo", {})
    receipt = harness.engine.cancel(record.execution_id)
    assert receipt.accepted is False
    assert receipt.reason == "ALREADY_TERMINAL"
    assert receipt.status is SUCCEEDED


def test_a_running_main_thread_call_is_not_interruptible() -> None:
    harness = Harness(Echo())
    record = harness.engine.submit("test.echo", {})
    harness.store.transition(record.execution_id, RUNNING)
    receipt = harness.engine.cancel(record.execution_id)
    assert receipt.accepted is False
    assert receipt.reason == "NOT_INTERRUPTIBLE"
    assert receipt.status is RUNNING


def test_a_scheduled_background_call_only_gets_a_cancel_request() -> None:
    harness = Harness(Sum(), background_runner=no_adapter)
    record = harness.engine.submit("test.sum", {})
    harness.store.transition(record.execution_id, PREPARING)
    harness.store.transition(record.execution_id, SCHEDULED)

    receipt = harness.engine.cancel(record.execution_id)
    assert receipt.accepted is True
    assert receipt.status is CANCEL_REQUESTED


def test_a_running_background_call_only_gets_a_cancel_request() -> None:
    harness = Harness(Sum(), background_runner=no_adapter)
    record = harness.engine.submit("test.sum", {})
    harness.store.transition(record.execution_id, PREPARING)
    harness.store.transition(record.execution_id, SCHEDULED)
    harness.store.transition(record.execution_id, RUNNING)
    receipt = harness.engine.cancel(record.execution_id)
    assert receipt.accepted is True
    assert receipt.status is CANCEL_REQUESTED
    # Asking twice is the same request, not a second transition.
    again = harness.engine.cancel(record.execution_id)
    assert again.accepted is True
    assert again.status is CANCEL_REQUESTED


def test_a_call_that_is_finalizing_refuses_a_cancel() -> None:
    harness = Harness(Sum(), background_runner=no_adapter)
    record = harness.engine.submit("test.sum", {})
    harness.store.transition(record.execution_id, PREPARING)
    harness.store.transition(record.execution_id, SCHEDULED)
    harness.store.transition(record.execution_id, RUNNING)
    harness.store.transition(record.execution_id, FINALIZING)
    receipt = harness.engine.cancel(record.execution_id)
    assert receipt.accepted is False
    assert receipt.reason == "FINALIZING"
    assert receipt.status is FINALIZING


def test_an_unknown_execution_id_is_not_found() -> None:
    harness = Harness(Echo())
    for call in (harness.engine.get, harness.engine.cancel):
        with pytest.raises(FrameworkError) as caught:
            call("missing")
        assert caught.value.code == EXECUTION_NOT_FOUND


def test_a_background_submit_reaches_the_injected_adapter_a_turn_later() -> None:
    handed: list[tuple[OperationEngine, ExecutionRecord, BaseOperation, Any]] = []

    def adapter(
        engine: OperationEngine, record: ExecutionRecord, operation: BaseOperation, request: Any
    ) -> None:
        handed.append((engine, record, operation, request))

    harness = Harness(Sum(), background_runner=adapter)
    record = harness.engine.submit("test.sum", {"value": "later"})
    assert record.status is QUEUED
    assert handed == []

    harness.queue.tick()
    assert len(handed) == 1
    engine, snapshot, operation, request = handed[0]
    assert engine is harness.engine
    assert snapshot.execution_id == record.execution_id
    assert snapshot.status is QUEUED
    assert operation is harness.registry.get("test.sum")
    assert request == ValueParams(value="later")


def test_a_background_submit_without_an_adapter_fails_the_call() -> None:
    harness = Harness(Sum())
    record = harness.engine.submit("test.sum", {})
    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == TASK_SUBMISSION_FAILED
    assert harness.queue.callbacks == []


def test_the_pending_limit_ends_the_call_as_failed() -> None:
    harness = Harness(Echo(), settings=Settings(max_pending=1))
    first = harness.engine.submit("test.echo", {})
    second = harness.engine.submit("test.echo", {})
    assert first.status is QUEUED
    assert second.status is FAILED
    assert second.error is not None
    assert second.error.code == TOO_MANY_EXECUTIONS

    harness.queue.tick()
    assert harness.engine.get(first.execution_id).status is SUCCEEDED


def test_a_call_from_another_thread_is_wrong_thread() -> None:
    harness = Harness(Echo())
    failures: list[FrameworkError] = []

    def call() -> None:
        try:
            harness.engine.run_sync("test.echo", {})
        except FrameworkError as error:
            failures.append(error)

    thread = threading.Thread(target=call)
    thread.start()
    thread.join()
    assert [error.code for error in failures] == [WRONG_THREAD]
    assert harness.records() == []
    assert CALLED == []


def test_a_closed_engine_refuses_new_work() -> None:
    harness = Harness(Echo())
    record = harness.engine.submit("test.echo", {})
    harness.engine.shutdown()
    calls = (
        lambda: harness.engine.run_sync("test.echo", {}),
        lambda: harness.engine.submit("test.echo", {}),
        lambda: harness.engine.cancel(record.execution_id),
    )
    for call in calls:
        with pytest.raises(FrameworkError) as caught:
            call()
        assert caught.value.code == ENGINE_CLOSING


def test_the_history_is_still_readable_after_shutdown() -> None:
    harness = Harness(Echo())
    record = harness.engine.run_sync("test.echo", {})
    harness.engine.shutdown()
    assert harness.engine.get(record.execution_id).status is SUCCEEDED


def test_shutdown_cancels_everything_that_never_started() -> None:
    harness = Harness(Echo())
    queued = [harness.engine.submit("test.echo", {}) for _ in range(3)]
    harness.engine.shutdown()
    harness.engine.shutdown()
    harness.queue.tick()
    assert CALLED == []
    assert [harness.store.get(record.execution_id).status for record in queued] == [
        CANCELLED,
        CANCELLED,
        CANCELLED,
    ]


def test_the_output_is_validated_before_it_becomes_a_result() -> None:
    harness = Harness(Lying())
    record = harness.engine.submit("test.lie", {})
    harness.queue.tick()
    failed = harness.store.get(record.execution_id)
    assert failed.status is FAILED
    assert failed.error is not None
    assert failed.error.code == OUTPUT_VALIDATION_FAILED
    assert failed.result is None


def test_an_operation_that_refuses_keeps_its_own_error_code() -> None:
    record = Harness(Refusing()).engine.run_sync("test.refuse", {})
    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == PATH_NOT_ALLOWED
    assert record.error.message == "path is not an allowed local file"


def test_an_unexpected_exception_is_internal_error_without_the_exception_text() -> None:
    record = Harness(Breaking()).engine.run_sync("test.break", {})
    assert record.status is FAILED
    assert record.error is not None
    assert record.error.code == INTERNAL_ERROR
    assert record.error.message == "RuntimeError: unexpected failure"
    assert "boom" not in record.error.message


def test_a_queued_call_is_cancelled_when_the_project_was_replaced() -> None:
    harness = Harness(Echo())
    record = harness.engine.submit("test.echo", {})
    harness.lifecycle.bump()

    harness.queue.tick()
    assert CALLED == []
    assert harness.store.get(record.execution_id).status is CANCELLED


def live_background(harness: Harness, *path: ExecutionStatus) -> ExecutionRecord:
    """A background call of this generation, moved along ``path``."""

    record = harness.engine.submit("test.sum", {})
    for status in path:
        record = harness.store.transition(record.execution_id, status)
    return record


@pytest.mark.parametrize("live", [SCHEDULED, RUNNING])
def test_a_project_replacement_leaves_a_running_main_thread_call_alone(
    live: ExecutionStatus,
) -> None:
    def replace() -> None:
        harness.lifecycle.bump()
        harness.engine.on_project_replaced()

    harness = Harness(Replacing(replace), Sum(), background_runner=no_adapter)
    background = live_background(harness, PREPARING, SCHEDULED, live)

    record = harness.engine.run_sync("test.replace", {})

    # PRD 9.2: the generation is dropped, not the call that is dropping it -- the
    # open/create that fires aboutToBeCleared comes from inside its own execute_main.
    assert record.status is SUCCEEDED
    assert record.result == {"value": "hello"}
    assert harness.lifecycle.current_epoch == 1
    # The old generation's live background task is still asked to stop (PRD 9.2).
    assert harness.store.get(background.execution_id).status is CANCEL_REQUESTED


def test_shutdown_leaves_a_running_main_thread_call_alone() -> None:
    def stop() -> None:
        harness.engine.shutdown()

    harness = Harness(Replacing(stop), Sum(), background_runner=no_adapter)
    background = live_background(harness, PREPARING, SCHEDULED, RUNNING)

    record = harness.engine.run_sync("test.replace", {})

    # PRD 9.3.2 cancels main-thread work that has not started; the call on this
    # stack is not it, and only the background task may get a cancel request.
    assert record.status is SUCCEEDED
    assert harness.store.get(background.execution_id).status is CANCEL_REQUESTED


def test_the_record_captures_the_epoch_of_the_call() -> None:
    harness = Harness(Echo())
    harness.lifecycle.bump()
    harness.lifecycle.bump()
    assert harness.engine.run_sync("test.echo", {}).project_epoch == 2


def test_the_engine_hands_out_snapshots_only() -> None:
    harness = Harness(Echo())
    record = harness.engine.run_sync("test.echo", {})
    record.status = FAILED
    assert record.result is not None
    record.result["value"] = "edited"
    fresh = harness.engine.get(record.execution_id)
    assert fresh.status is SUCCEEDED
    assert fresh.result == {"value": "hello"}


def test_an_accepted_call_records_the_operation_it_ran() -> None:
    record = Harness(Echo()).engine.run_sync("test.echo", {})
    assert record.operation == "test.echo"
    assert record.kind is OperationKind.QUERY
    assert record.strategy is ExecutionStrategy.MAIN_THREAD


def test_a_background_adapter_that_raises_ends_the_call_as_failed() -> None:
    def adapter(
        engine: OperationEngine, record: ExecutionRecord, operation: BaseOperation, request: Any
    ) -> None:
        raise RuntimeError("adapter blew up")

    harness = Harness(Sum(), background_runner=adapter)
    record = harness.engine.submit("test.sum", {})

    harness.queue.tick()
    failed = harness.store.get(record.execution_id)
    assert failed.status is FAILED
    assert failed.error is not None
    assert failed.error.code == INTERNAL_ERROR
    assert failed.error.message == "RuntimeError: unexpected failure"


def test_a_queued_callback_that_raises_is_logged_and_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    posted: list[Callable[[], None]] = []
    runner = MainThreadRunner(lambda execution_id: True, post=posted.append)

    def boom() -> None:
        raise RuntimeError("slot blew up")

    runner.post("x", boom)
    with caplog.at_level(logging.ERROR):
        posted[0]()
    assert "main-thread step for x failed" in caplog.text
    # The stack went to the log instead of into the Qt slot.
    assert "slot blew up" in caplog.text


def test_a_liveness_check_that_raises_cannot_escape_the_slot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def exploding_check(execution_id: str) -> bool:
        raise RuntimeError("liveness check blew up")

    posted: list[Callable[[], None]] = []
    runner = MainThreadRunner(exploding_check, post=posted.append)
    runner.post("x", lambda: pytest.fail("the callback must not run"))

    with caplog.at_level(logging.ERROR):
        posted[0]()
    assert "main-thread step for x failed" in caplog.text


# --- the real signal ---------------------------------------------------------


class HeadlessIface:
    """``iface.newProject`` without a GUI: QGIS's own ``clear()`` fires the signal."""

    def __init__(self, project: Any) -> None:
        self._project = project

    def newProject(self, promptToSaveFlag: bool = False) -> bool:  # noqa: N802 - the QGIS name
        self._project.clear()
        return True


def test_a_real_project_replacement_does_not_cancel_the_call_that_replaces_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AT-16 and AT-18's success half, on the signal a real project emits.

    ``project.open`` and ``project.create`` fire ``aboutToBeCleared`` from inside
    their own ``execute_main``, which the fakes above cannot reproduce.
    """

    pytest.importorskip("qgis", reason="a real project signal needs a QGIS interpreter")

    from qgis.core import QgsApplication, QgsProject

    from qgis_agent_plugin.operations.project import ProjectCreate, ProjectOpen

    # A headless application is enough for the real signal.  QGIS keeps its profile
    # and style databases under QGIS_CUSTOM_CONFIG_PATH and stray working-directory
    # files next to the cwd, so both are pointed at tmp_path (qgis/TEST.md).
    monkeypatch.setenv("QGIS_CUSTOM_CONFIG_PATH", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    application = QgsApplication([], False)
    application.initQgis()
    project = QgsProject.instance()
    try:
        project.clear()
        fixture = tmp_path / "empty.qgs"
        assert project.write(str(fixture))

        lifecycle = ProjectLifecycle()
        registry = OperationRegistry()
        registry.register(ProjectOpen())
        registry.register(ProjectCreate())
        engine = OperationEngine(
            registry,
            Settings(allowed_project_roots=(tmp_path,)),
            lifecycle,
            iface=HeadlessIface(project),
            project=project,
        )

        def replaced() -> None:
            # What plugin.Plugin._project_about_to_be_cleared does, on the real signal.
            lifecycle.bump()
            engine.on_project_replaced()

        project.aboutToBeCleared.connect(replaced)
        try:
            opened = engine.run_sync(
                "project.open", {"path": str(fixture), "discard_current": True}
            )
            created = engine.run_sync("project.create", {"discard_current": True})

            assert opened.status is SUCCEEDED
            assert created.status is SUCCEEDED
            # Each replacement bumped the generation, and a record keeps the epoch
            # it was created under: one behind the project its own call made.
            assert lifecycle.current_epoch > 0
            assert opened.project_epoch < created.project_epoch < lifecycle.current_epoch
            assert [
                execution_id
                for execution_id in engine._store.ids(set(ExecutionStatus))
                if engine._store.get(execution_id).status not in TERMINAL_STATUSES
            ] == []
        finally:
            project.aboutToBeCleared.disconnect(replaced)
    finally:
        application.exitQgis()
