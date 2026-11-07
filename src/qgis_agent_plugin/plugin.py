"""The plugin QGIS loads: one session per load, and the six steps of ``unload``.

``initGui`` assembles a session -- the PRD 10.1 settings, the project generation,
this plugin's own ``QgsTaskManager``, the adapter over it, the registry filled with
the nine PRD 7.1 operations, and the engine -- and publishes the engine and the
registry on ``iface``, which is where the Python Console finds them (PRD 1.3).

``unload`` is PRD 9.3 and nothing else: the engine stops accepting work and cancels
what never started, every task this plugin started is asked to stop, and whatever is
still running is handed to a supervisor the QGIS application hosts, because 9.3.4
forbids keeping the tasks' Python callbacks on the ``Plugin`` instance QGIS is
dropping.  The supervisor removes itself on the main thread once the last task has
really terminated (9.3.5); nothing here waits for a task to end.

The pydantic major is checked here, before the DTO layer can be imported: the DTOs
are written against pydantic 2, so any other major has to read as a dependency
error rather than as an import failure inside a model module.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import fields
from typing import TYPE_CHECKING, Any

import pydantic

if TYPE_CHECKING:
    from .core.config import Settings
    from .core.context import ProjectLifecycle
    from .core.engine import OperationEngine
    from .core.registry import OperationRegistry

__all__ = [
    "Plugin",
    "Supervisor",
    "application_supervisor",
    "classFactory",
    "read_settings",
]

# The seven PRD 10.1 keys live under one group: nothing this plugin reads can be
# confused with another plugin's setting, and a default install reads nothing at all.
SETTINGS_GROUP = "qgis_agent_plugin"

# Where the session is published on ``iface``; the Python Console reads these (PRD 1.3).
ENGINE_ATTRIBUTE = "agent_engine"
REGISTRY_ATTRIBUTE = "agent_registry"


def require_supported_pydantic(version: str) -> None:
    """Refuse a pydantic major the DTO layer cannot import from (PRD 6.1)."""

    if int(version.split(".", 1)[0]) != 2:
        raise RuntimeError(f"qgis-agent-plugin requires pydantic 2.x, but {version} is installed")


require_supported_pydantic(pydantic.VERSION)


def read_settings(store: Any = None) -> Settings:
    """The seven PRD 10.1 keys as ``QgsSettings`` holds them, defaults for the rest.

    A key nobody wrote comes back as ``None``, which ``Settings.from_mapping`` reads
    as "not set", so an untouched profile denies all file access instead of granting
    anything.  The store is a parameter only so this mapping can be tested without
    writing into the user's profile.
    """

    from .core.config import Settings

    if store is None:
        from qgis.core import QgsSettings

        store = QgsSettings()
    return Settings.from_mapping(
        {field.name: store.value(f"{SETTINGS_GROUP}/{field.name}") for field in fields(Settings)}
    )


class Supervisor:
    """Keeps a session's live work reachable after the plugin instance is gone.

    PRD 9.3.4 forbids holding the tasks' Python callbacks on the ``Plugin`` instance
    QGIS is unloading, so the engine and the adapter are held here instead, in an
    object :func:`application_supervisor` parents on the QGIS application.  The
    accounting is plain Python and that is deliberate: ``attach`` and ``release``
    follow the tasks this plugin's own manager adds and deletes, ``close`` is one
    call from ``unload``, and the last ``release`` is what ends the wait (9.3.5).
    Nothing blocks, and ``release`` may only be trusted because QGIS deletes a task
    after it has really terminated.
    """

    def __init__(self, engine: Any, runner: Any, *, dispose: Callable[[], None]) -> None:
        self._engine = engine
        self._runner = runner
        self._dispose = dispose
        self._live: set[int] = set()
        self._closed = False
        self._disposed = False

    def attach(self, task_id: int) -> None:
        """A task of this plugin has reached QGIS and is now held (PRD 9.3.4)."""

        self._live.add(task_id)

    def release(self, task_id: int) -> None:
        """A task has ended and QGIS is deleting it; the last one ends the wait."""

        self._live.discard(task_id)
        if self._closed and not self._live:
            self._finish()

    def close(self) -> None:
        """Stop holding work, and dispose now if no task is live (PRD 9.3.5)."""

        self._closed = True
        if not self._live:
            self._finish()

    @property
    def live(self) -> frozenset[int]:
        """The task ids still alive; empty once this supervisor is done."""

        return frozenset(self._live)

    def _finish(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        # Letting go of these two is the point of the whole class: from here the
        # engine, the adapter and the tasks' Python callbacks are unreachable.
        self._engine = None
        self._runner = None
        self._dispose()


def application_supervisor(manager: Any, engine: Any, runner: Any) -> Supervisor:
    """The supervisor as a child of the QGIS application (PRD 9.3.4, 9.3.5).

    Qt is imported inside this function, so ``plugin`` still imports in a bare
    interpreter.  The shell is parented to the application rather than to the plugin,
    which is exactly what 9.3.4 asks for, and ``deleteLater`` is the main-thread
    mechanism that removes it once the last task has ended.
    """

    from qgis.core import QgsApplication
    from qgis.PyQt.QtCore import QObject

    class ApplicationSupervisor(Supervisor, QObject):
        """A supervisor QGIS deletes with the application, not with the plugin."""

        def __init__(self) -> None:
            QObject.__init__(self, QgsApplication.instance())
            Supervisor.__init__(self, engine, runner, dispose=self.deleteLater)
            # This manager takes only this plugin's tasks, and QGIS deletes each task
            # after it has terminated: those two signals are the whole bookkeeping,
            # and neither of them is a poll or a wait.
            manager.taskAdded.connect(self.attach)
            manager.taskAboutToBeDeleted.connect(self.release)

    return ApplicationSupervisor()


class Plugin:
    """The instance QGIS keeps alive between ``initGui`` and ``unload``."""

    def __init__(self, iface: Any) -> None:
        self.iface = iface
        self.registry: OperationRegistry | None = None
        self.engine: OperationEngine | None = None
        self.lifecycle: ProjectLifecycle | None = None
        self.supervisor: Supervisor | None = None

    def initGui(self) -> None:
        from qgis.core import QgsProject, QgsTaskManager

        from .core.context import ProjectLifecycle
        from .core.engine import OperationEngine
        from .core.registry import OperationRegistry
        from .core.runners.qgs_task import OperationTaskRunner
        from .operations import register_all

        settings = read_settings()
        lifecycle = ProjectLifecycle()
        # One manager for this plugin and its tasks: PRD 9.3.2 rules out the session
        # wide cancelAll(), and a manager nothing else submits to is what makes that
        # unreachable.  The supervisor watches this same object for its task endings.
        manager = QgsTaskManager()
        runner = OperationTaskRunner(task_manager=manager)
        registry = OperationRegistry()
        register_all(registry)
        project = QgsProject.instance()
        self.registry = registry
        self.lifecycle = lifecycle
        self.engine = OperationEngine(
            registry,
            settings,
            lifecycle,
            iface=self.iface,
            project=project,
            background_runner=runner,
            background_cancel=runner.cancel,
        )
        self.supervisor = application_supervisor(manager, self.engine, runner)
        # The only place the epoch is bumped: PRD 9.1 counts ``aboutToBeCleared`` once
        # and forbids a second counter such as ``cleared``.
        project.aboutToBeCleared.connect(self._project_about_to_be_cleared)
        self._publish()

    def unload(self) -> None:
        from qgis.core import QgsProject

        # Nothing of ours connected: a repeated unload, or no load at all.
        with suppress(TypeError, RuntimeError):
            QgsProject.instance().aboutToBeCleared.disconnect(self._project_about_to_be_cleared)
        engine, supervisor = self.engine, self.supervisor
        self._withdraw()
        self.engine = self.registry = self.lifecycle = self.supervisor = None
        if engine is not None:
            # 9.3.1 then 9.3.2: refuse new work first, then cancel what never started
            # and ask every task of this plugin to stop -- never a global cancel.
            engine.shutdown()
        if supervisor is not None:
            # 9.3.4 and 9.3.5: what is still running stays reachable until the last
            # task has terminated, then the shell clears itself on the main thread.
            supervisor.close()

    def _project_about_to_be_cleared(self) -> None:
        """One new project generation, then the old one's work is dropped (PRD 9.1)."""

        lifecycle, engine = self.lifecycle, self.engine
        if lifecycle is None or engine is None:
            # A queued signal may arrive after unload; a slot may not raise.
            return
        lifecycle.bump()
        engine.on_project_replaced()

    def _publish(self) -> None:
        """Put the session where the Python Console looks for it (PRD 1.3)."""

        setattr(self.iface, ENGINE_ATTRIBUTE, self.engine)
        setattr(self.iface, REGISTRY_ATTRIBUTE, self.registry)

    def _withdraw(self) -> None:
        """Take those handles back, so a disabled plugin leaves no session behind."""

        for name in (ENGINE_ATTRIBUTE, REGISTRY_ATTRIBUTE):
            if hasattr(self.iface, name):
                delattr(self.iface, name)


def classFactory(iface: Any) -> Plugin:
    return Plugin(iface)
