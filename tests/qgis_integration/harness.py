"""The headless QGIS session the acceptance cases run in (PRD 12.1).

One ``QgsApplication`` without a GUI, the real ``QgsProject``, one real
``QgsTaskManager`` per plugin session, a stand-in interface and a handful of probe
operations that hold a task open long enough to look at it.  Everything the cases
exercise is the real object from ``src``; the interface and the probes are the only
test doubles, and every case says so in its evidence line.

The application runs inside a throwaway profile (``QGIS_CUSTOM_CONFIG_PATH``), so
the seven PRD 10.1 keys the acceptance run writes live nowhere near the operator's
own ``QGIS3.ini``, and the working directory is the throwaway one because QGIS
drops ``symbology-style.db`` into whatever directory it starts in.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from qgis_agent_plugin.core.contracts import (
    BackgroundOperationMixin,
    BaseQuery,
    MainThreadOperationMixin,
    OperationContext,
    TaskControl,
)
from qgis_agent_plugin.core.execution_store import TERMINAL_STATUSES
from qgis_agent_plugin.core.models import ExecutionRecord, ExecutionStatus, Params
from qgis_agent_plugin.operations.layer import (
    FieldSum,
    FieldValues,
    LayerSumNumericField,
)
from qgis_agent_plugin.operations.project import ProjectGetInfoParams, ProjectGetInfoResult

__all__ = [
    "Context",
    "FINALIZE_CANCEL",
    "GATES",
    "PROBE_LOG",
    "DuplicateProjectInfo",
    "FakeIface",
    "Skip",
    "configure_settings",
    "equal",
    "flush_deferred",
    "pump",
    "pump_until",
    "qt_deleted",
    "require",
    "require_processing",
    "start_application",
]

SETTINGS_GROUP = "qgis_agent_plugin"

# Why QGIS's processing framework is not registered, when it could not be; the
# ``processing.*`` cases skip on it instead of measuring an empty registry.
PROCESSING_ERROR: str | None = None


class Skip(Exception):
    """The environment cannot run this case; never reported as a pass (PRD 12.3)."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def equal(got: Any, want: Any, what: str) -> None:
    require(got == want, f"{what}: expected {want!r}, got {got!r}")


# --------------------------------------------------------------------------- the app


def start_application(runtime: Path, profile: Path) -> Any:
    """A headless application with its own profile; call once per acceptance run."""

    runtime.mkdir(parents=True, exist_ok=True)
    profile.mkdir(parents=True, exist_ok=True)
    os.chdir(runtime)
    os.environ["QGIS_CUSTOM_CONFIG_PATH"] = str(profile)

    from qgis.core import QgsApplication

    application = QgsApplication([], False)
    application.initQgis()
    start_processing()
    return application


def start_processing() -> None:
    """Register QGIS's own processing providers, which a bare ``initQgis`` does not.

    QGIS Desktop registers them when its processing plugin loads, and the launcher
    puts neither ``python/plugins`` on ``sys.path`` nor the providers in the
    registry; without this the P cases would measure an empty registry (PRD v2 3.1).
    The plugin directory is derived from the ``qgis`` package itself, so nothing here
    depends on where QGIS was installed.
    """

    global PROCESSING_ERROR
    import qgis

    source = getattr(qgis, "__file__", None)
    if source:
        plugins = Path(source).resolve().parents[1] / "plugins"
        if str(plugins) not in sys.path:
            # Appended, not inserted: the repository's own ``src`` keeps its place at
            # the front of the search path (run_acceptance.py puts it there).
            sys.path.append(str(plugins))
    try:
        from processing.core.Processing import Processing

        Processing.initialize()
    except Exception as error:  # noqa: BLE001 - a missing framework is a skip, not a failure
        PROCESSING_ERROR = f"{type(error).__name__}: {error}"


def require_processing() -> None:
    """Refuse to measure processing when QGIS's framework is not registered here."""

    from qgis.core import QgsApplication

    if PROCESSING_ERROR is not None:
        raise Skip(f"QGIS Processing is not available here ({PROCESSING_ERROR})")
    if not QgsApplication.processingRegistry().algorithms():
        raise Skip("QGIS Processing registered no algorithm, so nothing can be measured")


def configure_settings(data_root: Path, project_root: Path) -> None:
    """Point the two PRD 10.1 roots at the fixtures; nothing else is changed."""

    from qgis.core import QgsSettings

    store = QgsSettings()
    store.setValue(f"{SETTINGS_GROUP}/allowed_data_roots", [str(data_root)])
    store.setValue(f"{SETTINGS_GROUP}/allowed_project_roots", [str(project_root)])
    store.sync()


def settings_file() -> Path:
    from qgis.core import QgsSettings

    return Path(QgsSettings().fileName())


def pump(application: Any, seconds: float = 0.0) -> None:
    """Turn the event loop for at least ``seconds`` of wall clock."""

    deadline = time.monotonic() + seconds
    while True:
        application.processEvents()
        if time.monotonic() >= deadline:
            return
        time.sleep(0.004)


def pump_until(application: Any, predicate: Any, *, timeout: float = 20.0, what: str) -> None:
    deadline = time.monotonic() + timeout
    while True:
        application.processEvents()
        if predicate():
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(0.004)


def flush_deferred(application: Any) -> None:
    """Deliver the ``deleteLater`` queue, which is how PRD 9.3.5 cleans up."""

    from qgis.PyQt.QtCore import QEvent

    application.sendPostedEvents(None, QEvent.DeferredDelete)
    application.processEvents()


def qt_deleted(obj: Any) -> bool:
    from qgis.PyQt import sip

    return sip.isdeleted(obj)


class FakeIface:
    """The one interface call the operations make: ``newProject``.

    QGIS Desktop's own implementation clears the current project and reports
    whether it did; headless, that is the whole of it (PRD 7.2 is the operation
    calling it, not this stand-in).  ``agent_engine`` and ``agent_registry`` are
    published on it by ``Plugin.initGui``, exactly as on the real one.
    """

    def newProject(self, promptToSaveFlag: bool = False) -> bool:
        from qgis.core import QgsProject

        QgsProject.instance().clear()
        return True


# --------------------------------------------------------------------------- probes

# Shared with the probe operations: they are instantiated by the registry, so the
# evidence a case wants has to live beside them rather than inside one instance.
GATES: dict[str, Any] = {}
FINALIZE_CANCEL: dict[str, Any] = {}


@dataclass
class ProbeLog:
    prepared: list[str] = field(default_factory=list)
    starts: list[str] = field(default_factory=list)
    finalizes: list[str] = field(default_factory=list)


PROBE_LOG = ProbeLog()


class EmptyParams(Params):
    pass


class LabelResult(BaseModel):
    label: str


class SlowParams(Params):
    label: str = Field(min_length=1)
    steps: int = Field(default=50, ge=1, le=100000)
    step_seconds: float = Field(default=0.02, ge=0, le=5)
    report_progress: bool = True
    gate: str | None = None


class SlowResult(BaseModel):
    label: str
    steps_done: int
    project_epoch: int


@dataclass(frozen=True)
class SlowSnapshot:
    label: str
    steps: int
    step_seconds: float
    report_progress: bool
    gate: str | None
    project_epoch: int


@dataclass(frozen=True)
class SlowWork:
    label: str
    steps_done: int
    project_epoch: int


class SlowProbe(
    BaseQuery[SlowParams, SlowResult],
    BackgroundOperationMixin[SlowParams, SlowResult, SlowSnapshot, SlowWork],
):
    """A background query that stays alive exactly as long as a case needs.

    ``gate`` is the acceptance-only entry stop: the worker blocks before doing any
    work, which is how "QGIS holds the task but it has not run" is reproducible
    (AT-08B).  ``report_progress=False`` keeps the record in ``SCHEDULED`` until it
    is cancelled, because PRD 8.2 says the first progress report is what moves it.
    """

    name = "probe.slow"
    description = "Hold one QgsTask open so its states can be observed"
    input_model = SlowParams
    output_model = SlowResult

    def prepare_main(self, ctx: OperationContext, request: SlowParams) -> SlowSnapshot:
        PROBE_LOG.prepared.append(request.label)
        return SlowSnapshot(
            label=request.label,
            steps=request.steps,
            step_seconds=request.step_seconds,
            report_progress=request.report_progress,
            gate=request.gate,
            project_epoch=ctx.lifecycle.current_epoch,
        )

    def execute_worker(self, snapshot: SlowSnapshot, control: TaskControl) -> SlowWork:
        PROBE_LOG.starts.append(snapshot.label)
        gate = GATES.get(snapshot.gate) if snapshot.gate else None
        if gate is not None:
            gate.wait(120)
        done = 0
        for step in range(1, snapshot.steps + 1):
            if control.is_cancelled():
                break
            time.sleep(snapshot.step_seconds)
            done = step
            if snapshot.report_progress:
                control.set_progress(100.0 * step / snapshot.steps)
        return SlowWork(snapshot.label, done, snapshot.project_epoch)

    def finalize_main(
        self, ctx: OperationContext, request: SlowParams, result: SlowWork
    ) -> SlowResult:
        PROBE_LOG.finalizes.append(result.label)
        return SlowResult(
            label=result.label,
            steps_done=result.steps_done,
            project_epoch=result.project_epoch,
        )


class BoomProbe(
    BaseQuery[EmptyParams, LabelResult],
    BackgroundOperationMixin[EmptyParams, LabelResult, str, str],
):
    """A worker that raises: PRD 12.2 AT-07's shape."""

    name = "probe.boom"
    description = "Raise inside the worker thread on purpose"
    input_model = EmptyParams
    output_model = LabelResult

    def prepare_main(self, ctx: OperationContext, request: EmptyParams) -> str:
        return "probe.boom"

    def execute_worker(self, snapshot: str, control: TaskControl) -> str:
        raise RuntimeError("probe.boom fails on purpose")

    def finalize_main(
        self, ctx: OperationContext, request: EmptyParams, result: str
    ) -> LabelResult:
        return LabelResult(label=result)


class FinalizeCancelProbe(
    BaseQuery[EmptyParams, LabelResult],
    BackgroundOperationMixin[EmptyParams, LabelResult, str, str],
):
    """A background query that asks to be cancelled while it is being finalised.

    ``finalize_main`` runs inside the ``FINALIZING`` window, which no caller outside
    the engine can otherwise reach (PRD 12.2 AT-10).
    """

    name = "probe.finalize_cancel"
    description = "Ask for a cancel while this execution is finalising"
    input_model = EmptyParams
    output_model = LabelResult

    def prepare_main(self, ctx: OperationContext, request: EmptyParams) -> str:
        return "probe.finalize_cancel"

    def execute_worker(self, snapshot: str, control: TaskControl) -> str:
        return snapshot

    def finalize_main(
        self, ctx: OperationContext, request: EmptyParams, result: str
    ) -> LabelResult:
        engine = FINALIZE_CANCEL["engine"]
        FINALIZE_CANCEL["receipt"] = engine.cancel(FINALIZE_CANCEL["execution_id"])
        return LabelResult(label=result)


class BadFinalizeProbe(
    BaseQuery[EmptyParams, LabelResult],
    BackgroundOperationMixin[EmptyParams, LabelResult, str, str],
):
    """A ``finalize_main`` that raises (PRD 12.2 AT-23)."""

    name = "probe.bad_finalize"
    description = "Raise while finalising on the main thread"
    input_model = EmptyParams
    output_model = LabelResult

    def prepare_main(self, ctx: OperationContext, request: EmptyParams) -> str:
        return "probe.bad_finalize"

    def execute_worker(self, snapshot: str, control: TaskControl) -> str:
        return snapshot

    def finalize_main(
        self, ctx: OperationContext, request: EmptyParams, result: str
    ) -> LabelResult:
        raise RuntimeError("probe.bad_finalize fails on purpose")


class BadOutputProbe(
    BaseQuery[EmptyParams, LabelResult],
    BackgroundOperationMixin[EmptyParams, LabelResult, str, object],
):
    """A ``finalize_main`` whose output does not match the declared model."""

    name = "probe.bad_output"
    description = "Return an output the declared model refuses"
    input_model = EmptyParams
    output_model = LabelResult

    def prepare_main(self, ctx: OperationContext, request: EmptyParams) -> str:
        return "probe.bad_output"

    def execute_worker(self, snapshot: str, control: TaskControl) -> object:
        return object()

    def finalize_main(
        self, ctx: OperationContext, request: EmptyParams, result: object
    ) -> LabelResult:
        return result  # pyright: ignore[reportReturnType] - the refusal is the case


class SlowSum(LayerSumNumericField):
    """The real field sum, slowed down so its dependent layer can be removed.

    ``prepare_main``, ``finalize_main`` and ``dependent_layer_ids`` are the real
    ones; only the worker is held open (PRD 8.4 / AT-12).
    """

    name = "probe.slow_sum"
    description = "A field sum held open so its dependent layer can be removed"

    def execute_worker(self, snapshot: FieldValues, control: TaskControl) -> FieldSum:
        for _ in range(100):
            if control.is_cancelled():
                break
            time.sleep(0.02)
            control.set_progress(50.0)
        return FieldSum(
            count=len(snapshot.values),
            total=sum(snapshot.values),
            skipped_null=snapshot.skipped_null,
            project_epoch=snapshot.project_epoch,
        )


class DuplicateProjectInfo(
    BaseQuery[ProjectGetInfoParams, ProjectGetInfoResult],
    MainThreadOperationMixin[ProjectGetInfoParams, ProjectGetInfoResult],
):
    """A second declaration of a name the build already registered (PRD 12.2 AT-01)."""

    name = "project.get_info"
    description = "A duplicate declaration, which registration must refuse"
    input_model = ProjectGetInfoParams
    output_model = ProjectGetInfoResult

    def execute_main(
        self, ctx: OperationContext, request: ProjectGetInfoParams
    ) -> ProjectGetInfoResult:
        raise AssertionError("a refused declaration never runs")


PROBES = (SlowProbe, BoomProbe, FinalizeCancelProbe, BadFinalizeProbe, BadOutputProbe, SlowSum)


# ------------------------------------------------------------------------ the session


class TaskWatch:
    """What the plugin's own task manager reported, recorded on the side.

    ``taskAdded``/``taskAboutToBeDeleted`` are the two moments the acceptance cases
    need to prove a real ``QgsTask`` existed and was really deleted.
    """

    def __init__(self, manager: Any) -> None:
        self.added: list[int] = []
        self.deleted: list[int] = []
        manager.taskAdded.connect(self.added.append)
        manager.taskAboutToBeDeleted.connect(self.deleted.append)


class Context:
    """One loaded plugin session, and everything a case needs to look at it."""

    def __init__(self, application: Any, fixtures: Any) -> None:
        self.app = application
        self.fixtures = fixtures
        self.iface = FakeIface()
        self.plugin: Any = None
        self.probes: dict[str, Any] = {}
        self._probes_registered_for: Any = None

    # -- lifecycle ---------------------------------------------------------

    def load(self) -> Any:
        from qgis_agent_plugin import classFactory

        plugin = classFactory(self.iface)
        plugin.initGui()
        self.plugin = plugin
        return plugin

    def unload(self) -> None:
        plugin, self.plugin = self.plugin, None
        if plugin is not None:
            plugin.unload()
        flush_deferred(self.app)

    def register_probes(self) -> None:
        """Register the probe operations into the current session, once."""

        if self._probes_registered_for is self.plugin:
            return
        require(self.plugin is not None, "no plugin session is loaded")
        self.probes = {}
        for probe_class in PROBES:
            probe = probe_class()
            self.registry.register(probe)
            self.probes[probe.name] = probe
        self._probes_registered_for = self.plugin

    # -- the session's parts ----------------------------------------------

    @property
    def engine(self) -> Any:
        engine = getattr(self.plugin, "engine", None)
        require(engine is not None, "no engine is loaded")
        return engine

    @property
    def registry(self) -> Any:
        registry = getattr(self.plugin, "registry", None)
        require(registry is not None, "no registry is loaded")
        return registry

    @property
    def lifecycle(self) -> Any:
        return self.plugin.lifecycle

    @property
    def runner(self) -> Any:
        return self.engine._background_runner

    @property
    def manager(self) -> Any:
        manager = self.runner._manager
        require(manager is not None, "this session has no QgsTaskManager yet")
        return manager

    def watch_tasks(self) -> TaskWatch:
        return TaskWatch(self.manager)

    # -- calls -------------------------------------------------------------

    def run(self, name: str, arguments: dict[str, Any] | None = None) -> ExecutionRecord:
        return self.engine.run_sync(name, arguments if arguments is not None else {})

    def submit(self, name: str, arguments: dict[str, Any] | None = None) -> ExecutionRecord:
        return self.engine.submit(name, arguments if arguments is not None else {})

    def add_layer(self, source: Path | str, name: str, **extra: Any) -> str:
        """A vector layer added through the real operation, returning its id."""

        record = self.run(
            "layer.add",
            {"kind": "vector", "source_path": str(source), "name": name, **extra},
        )
        require(
            record.status is ExecutionStatus.SUCCEEDED,
            f"layer.add({name}) failed: {record.error}",
        )
        return str((record.result or {})["layer_id"])

    def record(self, execution_id: str) -> ExecutionRecord:
        return self.engine.get(execution_id)

    def status(self, execution_id: str) -> ExecutionStatus:
        return self.record(execution_id).status

    def wait_terminal(self, execution_id: str, *, timeout: float = 30.0) -> ExecutionRecord:
        pump_until(
            self.app,
            lambda: self.status(execution_id) in TERMINAL_STATUSES,
            timeout=timeout,
            what=f"{execution_id} to end",
        )
        return self.record(execution_id)

    def trace(self, execution_id: str, *, timeout: float = 30.0) -> list[str]:
        """The statuses one execution went through, as the store reported them."""

        statuses = [str(self.status(execution_id))]
        deadline = time.monotonic() + timeout
        while ExecutionStatus(statuses[-1]) not in TERMINAL_STATUSES:
            self.app.processEvents()
            current = str(self.status(execution_id))
            if current != statuses[-1]:
                statuses.append(current)
            if time.monotonic() >= deadline:
                raise AssertionError(f"timed out tracing {execution_id}: {statuses}")
            time.sleep(0.004)
        return statuses
