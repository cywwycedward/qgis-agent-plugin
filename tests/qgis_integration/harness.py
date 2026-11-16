"""验收用例跑在其中的无 GUI QGIS 会话。

一个没有界面的 ``QgsApplication``、真实的 ``QgsProject``、每个插件会话一个真实的
``QgsTaskManager``、一个替身界面，以及几个能把任务按住足够久的探针操作。用例碰到的都是 ``src`` 里
真实的对象；只有界面和探针是测试替身，每条用例都在证据行里说明这一点。

应用跑在一次性的配置目录里（``QGIS_CUSTOM_CONFIG_PATH``），所以验收碰到的任何配置都离操作者自己的
``QGIS3.ini`` 很远；工作目录也是那个一次性目录，因为 QGIS 会把 ``symbology-style.db`` 丢在它启动
时所在的目录。

The headless QGIS session the acceptance cases run in.  One ``QgsApplication`` without a GUI,
the real ``QgsProject``, one real ``QgsTaskManager`` per plugin session, a stand-in interface
and a handful of probe operations that hold a task open long enough to look at it.  Everything
the cases exercise is the real object from ``src``; the interface and the probes are the only
test doubles, and every case says so in its evidence line.  The application runs inside a
throwaway profile (``QGIS_CUSTOM_CONFIG_PATH``), so any settings the acceptance run touches
live nowhere near the operator's own ``QGIS3.ini``, and the working directory is the
throwaway one because QGIS drops ``symbology-style.db`` into whatever directory it starts in.
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

# QGIS 的 processing 框架起不来时记下的原因，``processing.*`` 的用例据此跳过，而不是去量一
# 个空注册表。
# Why QGIS's processing framework is not registered, when it could not be; the ``processing.*``
# cases skip on it instead of measuring an empty registry.
PROCESSING_ERROR: str | None = None


class Skip(Exception):
    """这台机器跑不了这个用例；永远不会被当成通过。

    The environment cannot run this case; it is never reported as a pass.
    """


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def equal(got: Any, want: Any, what: str) -> None:
    require(got == want, f"{what}: expected {want!r}, got {got!r}")


# -------------------------------------------------------------------- 应用 / the app


def start_application(runtime: Path, profile: Path) -> Any:
    """起一个带自己配置目录的无界面应用；每次验收运行只调一次。

    A headless application with its own profile; call once per acceptance run.
    """

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
    """注册 QGIS 自己的 processing 提供者，裸 ``initQgis`` 不会做这件事。

    QGIS Desktop 是在 processing 插件加载时注册它们的，而启动器既没把 ``python/plugins`` 放进
    ``sys.path``，也没把提供者放进注册表；少了这一步，processing 用例只能跳过，不拿空表凑数。
    插件目录从 ``qgis`` 包自身推出来，所以这里不依赖 QGIS 装在哪。

    Register QGIS's own processing providers, which a bare ``initQgis`` does not.  QGIS Desktop
    registers them when its processing plugin loads, and the launcher puts neither
    ``python/plugins`` on ``sys.path`` nor the providers in the registry; without this the
    processing cases would measure an empty registry.  The plugin directory is derived from the
    ``qgis`` package itself, so nothing here depends on where QGIS was installed.
    """

    global PROCESSING_ERROR
    import qgis

    source = getattr(qgis, "__file__", None)
    if source:
        plugins = Path(source).resolve().parents[1] / "plugins"
        if str(plugins) not in sys.path:
            # 追加而不是插入：仓库自己的 ``src`` 仍留在搜索路径最前面。
            # Appended, not inserted: the repository's own ``src`` keeps its place at the front.
            sys.path.append(str(plugins))
    try:
        from processing.core.Processing import Processing

        Processing.initialize()
    except Exception as error:  # noqa: BLE001 - a missing framework is a skip, not a failure
        PROCESSING_ERROR = f"{type(error).__name__}: {error}"


def require_processing() -> None:
    """QGIS 的框架没注册上时，拒绕去量 processing 的结果。

    Refuse to measure processing when QGIS's framework is not registered here.
    """

    from qgis.core import QgsApplication

    if PROCESSING_ERROR is not None:
        raise Skip(f"QGIS Processing is not available here ({PROCESSING_ERROR})")
    if not QgsApplication.processingRegistry().algorithms():
        raise Skip("QGIS Processing registered no algorithm, so nothing can be measured")


def settings_file() -> Path:
    from qgis.core import QgsSettings

    return Path(QgsSettings().fileName())


def pump(application: Any, seconds: float = 0.0) -> None:
    """至少把事件循环转 ``seconds`` 秒。

    Turn the event loop for at least ``seconds`` of wall clock.
    """

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
    """把 ``deleteLater`` 队列投递出去，卸载就是靠它清理的。

    Deliver the ``deleteLater`` queue, which is how an unload cleans up.
    """

    from qgis.PyQt.QtCore import QEvent

    application.sendPostedEvents(None, QEvent.DeferredDelete)
    application.processEvents()


def qt_deleted(obj: Any) -> bool:
    from qgis.PyQt import sip

    return sip.isdeleted(obj)


class FakeIface:
    """操作真正用到的那一个界面调用：``newProject``。

    QGIS Desktop 里的实现会清空当前工程并报告是否清空；无界面时这就是它的全部内容。用例测的是调
    用它的那个操作，不是这个替身。``agent_engine`` 与 ``agent_registry`` 由 ``Plugin.initGui``
    挂上去，和真实界面一样。

    The one interface call the operations make: ``newProject``.  QGIS Desktop's own
    implementation clears the current project and reports whether it did; headless, that is the
    whole of it.  A case measures the operation that calls it, not this stand-in.
    ``agent_engine`` and ``agent_registry`` are published on it by ``Plugin.initGui``, exactly as
    on the real one.
    """

    def newProject(self, promptToSaveFlag: bool = False) -> bool:
        from qgis.core import QgsProject

        QgsProject.instance().clear()
        return True


# ----------------------------------------------------------------- 探针 / probes

# 与探针操作共用：它们由注册表实例化，所以用例要的证据不能存在某个实例里，只能放在旁边。
# Shared with the probe operations: they are instantiated by the registry, so the evidence a case
# wants has to live beside them rather than inside one instance.
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
    """一个活多久由用例决定的背景查询。

    ``gate`` 只给验收用的入口闸：worker 在做任何事之前先阻塞，这样才能复现“QGIS 拿着任务但它还没
    跑”。``report_progress=False`` 让记录停在 ``SCHEDULED`` 直到被取消，因为把记录推走的是第一次
    进度上报。

    A background query that stays alive exactly as long as a case needs.  ``gate`` is the
    acceptance-only entry stop: the worker blocks before doing any work, which is how "QGIS holds
    the task but it has not run" is reproducible.  ``report_progress=False`` keeps the record in
    ``SCHEDULED`` until it is cancelled, because the first progress report is what moves it.
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
    """一个会抛异常的 worker：执行失败的那种形状。"""

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
    """一个在收尾过程中要求取消自己的背景查询。

    ``finalize_main`` 跑在 ``FINALIZING`` 窗口里，引擎之外没有别的办法进到那里。

    A background query that asks to be cancelled while it is being finalised.  ``finalize_main``
    runs inside the ``FINALIZING`` window, which no caller outside the engine can otherwise reach.
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
    """会抛异常的 ``finalize_main``。"""

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
    """输出与声明的模型不符的 ``finalize_main``。"""

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
        return result  # pyright: ignore[reportReturnType] 拒绝即目的 / the refusal is the point


class SlowSum(LayerSumNumericField):
    """真实的字段求和，只是被放慢，方便在它跑着时删掉依赖图层。

    ``prepare_main``、``finalize_main`` 与 ``dependent_layer_ids`` 都是真的，只有 worker 被按住。

    The real field sum, slowed down so its dependent layer can be removed.  ``prepare_main``,
    ``finalize_main`` and ``dependent_layer_ids`` are the real ones; only the worker is held open.
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
            selected_only=snapshot.selected_only,
            project_epoch=snapshot.project_epoch,
        )


class DuplicateProjectInfo(
    BaseQuery[ProjectGetInfoParams, ProjectGetInfoResult],
    MainThreadOperationMixin[ProjectGetInfoParams, ProjectGetInfoResult],
):
    """重复声明一个已经注册过的名字，注册必须拒绝它。

    A duplicate declaration of a registered name, which registration must refuse.
    """

    name = "project.get_info"
    description = "A duplicate declaration, which registration must refuse"
    input_model = ProjectGetInfoParams
    output_model = ProjectGetInfoResult

    def execute_main(
        self, ctx: OperationContext, request: ProjectGetInfoParams
    ) -> ProjectGetInfoResult:
        raise AssertionError("a refused declaration never runs")


PROBES = (SlowProbe, BoomProbe, FinalizeCancelProbe, BadFinalizeProbe, BadOutputProbe, SlowSum)


# ------------------------------------------------------------- 会话 / the session


class TaskWatch:
    """插件自己的任务管理器报了些什么，记在旁边。

    ``taskAdded``/``taskAboutToBeDeleted`` 是两个能证明真有一个 ``QgsTask`` 存在、并且真的被删掉的
    时刻。

    What the plugin's own task manager reported, recorded on the side.  ``taskAdded`` and
    ``taskAboutToBeDeleted`` are the two moments that prove a real ``QgsTask`` existed and was
    really deleted.
    """

    def __init__(self, manager: Any) -> None:
        self.added: list[int] = []
        self.deleted: list[int] = []
        manager.taskAdded.connect(self.added.append)
        manager.taskAboutToBeDeleted.connect(self.deleted.append)


class Context:
    """一个已加载的插件会话，以及用例要看的全部东西。

    One loaded plugin session, and everything a case needs to look at it.
    """

    def __init__(self, application: Any, fixtures: Any) -> None:
        self.app = application
        self.fixtures = fixtures
        self.iface = FakeIface()
        self.plugin: Any = None
        self.probes: dict[str, Any] = {}
        self._probes_registered_for: Any = None

    # -- 生命周期 / lifecycle ---------------------------------------------

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
        """把探针操作注册进当前会话，只注册一次。

        Register the probe operations into the current session, once.
        """

        if self._probes_registered_for is self.plugin:
            return
        require(self.plugin is not None, "no plugin session is loaded")
        self.probes = {}
        for probe_class in PROBES:
            probe = probe_class()
            self.registry.register(probe)
            self.probes[probe.name] = probe
        self._probes_registered_for = self.plugin

    # -- 会话的各个部件 / the session's parts ----------------------------

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

    # -- 调用 / calls ----------------------------------------------------

    def run(self, name: str, arguments: dict[str, Any] | None = None) -> ExecutionRecord:
        return self.engine.run_sync(name, arguments if arguments is not None else {})

    def submit(self, name: str, arguments: dict[str, Any] | None = None) -> ExecutionRecord:
        return self.engine.submit(name, arguments if arguments is not None else {})

    def add_layer(self, source: Path | str, name: str, **extra: Any) -> str:
        """经真实操作加一个矢量图层，返回它的 id。"""

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
        """一个执行经历过的状态序列，以 store 报告的样子为准。"""

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
