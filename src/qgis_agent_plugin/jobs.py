"""Job adapters that give ``Command.arun`` something the store can hold.

``Operations`` connects to a job's ``finished`` signal and assumes its callbacks
run on the QGIS main thread.  QGIS offers neither of those directly:
``QgsMapRendererJob.finished`` is a signal but there is nothing to connect it to
yet, while ``QgsTask.finished`` is the virtual method a Python subclass overrides
rather than a signal, so ``connect`` on it fails.  Every adapter here is a QObject
created on the main thread, so the signal it emits -- and every callback the
store runs from it -- lands on the main thread.

This module imports PyQt at import time: a command imports it inside ``arun``
only, because the command package has to load in a plain interpreter.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from qgis.core import QgsApplication, QgsProcessingAlgRunnerTask, QgsProcessingFeedback
from qgis.PyQt.QtCore import QObject, pyqtSignal

from .command import Result
from .protocol import CommandError

logger = logging.getLogger(__name__)


class OperationJob(QObject):
    """One running job as the store sees it.

    ``start`` and ``cancel`` are the store's calls into the job.  ``settle`` and
    ``fail`` are the job's own way out: either one records the outcome and emits
    ``finished`` once, so an operation always reaches a terminal state instead of
    staying ``running`` forever.  Two methods instead of one with optional
    fields, because "both a result and an error" is not a state an operation has.
    """

    finished = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.result: Any = None
        self.error: CommandError | None = None
        self._settled = False

    def settle(self, result: Result) -> None:
        """Record the answer and tell the store the job is done."""

        self._finish(result.model_dump(mode="json"), None)

    def fail(self, error: CommandError) -> None:
        """Record why it failed and tell the store the job is done."""

        self._finish(None, error)

    def _finish(self, result: Any, error: CommandError | None) -> None:
        if self._settled:
            return
        self._settled = True
        self.result = result
        self.error = error
        self.finished.emit()

    def settle_report(self, report: Callable[..., Result], *args: Any) -> None:
        """Settle with what the command shaped, or fail when shaping it raised.

        The command's own answer is built inside a Qt slot, and a slot that raises
        aborts QGIS, so a report that fails settles the operation as failed instead
        of escaping.  Every job that reports a command's answer goes through here,
        so the rule is written once.
        """

        try:
            answer = report(*args)
        except Exception as error:
            logger.exception("Asynchronous operation report failed")
            self.fail(CommandError("report_failed", str(error)))
            return
        self.settle(answer)

    def start(self) -> None:
        raise NotImplementedError("a job adapter has to start something")

    def cancel(self) -> None:
        raise NotImplementedError("a job adapter has to cancel something")

    def outcome(self) -> tuple[Any, CommandError | None]:
        """The wire-ready result and the failure, mutually exclusive."""

        return self.result, self.error

    @property
    def progress(self) -> float | None:
        """How far the job has come, 0-100, or None when it cannot say."""

        return None

    @property
    def progress_text(self) -> str | None:
        """What the job last said it was doing, or None when it cannot say."""

        return None


class RenderMapJob(OperationJob):
    """A ``QgsMapRendererParallelJob``: the render already runs on its own threads."""

    def __init__(self, job: Any, path: str, report: Callable[[], Result]) -> None:
        super().__init__()
        self._job = job
        self._path = path
        # What a written image is answered as, shaped by the command that asked
        # for it: this module never learns the render command's own field names,
        # which is the rule ``ProcessingJob``'s ``report`` follows too.
        self._report = report
        job.finished.connect(self._save)

    def _save(self) -> None:
        """Write the rendered image; this runs on the main thread when rendering is done."""

        try:
            image = self._job.renderedImage()
            saved = not image.isNull() and bool(image.save(self._path))
        except Exception as error:
            logger.exception("Rendered image could not be saved")
            self.fail(CommandError("render_failed", str(error)))
            return
        if not saved:
            self.fail(
                CommandError("render_failed", f"Failed to save rendered image to {self._path}")
            )
            return
        self.settle_report(self._report)

    def start(self) -> None:
        self._job.start()

    def cancel(self) -> None:
        # QGIS's blocking cancel would hold the main thread until the render stops.
        self._job.cancelWithoutBlocking()


class ProcessingFeedback(QgsProcessingFeedback):
    """A feedback that keeps the progress text where a poll can read it.

    QGIS reports progress by calling ``setProgress`` and ``setProgressText``.
    The first lands in a value the base class can be asked for, but the second
    only appends to the feedback's log -- there is no getter for the latest text
    -- so the text has to be kept here.  Overriding it also keeps progress
    chatter out of ``textLog()``, which is where a failed run reads its reason.
    Both are written from the thread the algorithm runs on and read by
    ``get_operation`` on the main thread: the percentage is a value QGIS owns,
    and the text is one attribute assignment of the latest string, which is all
    a reader wants.

    A run that failed says so only through the base class's own text log -- the
    reason an output could not be written, say -- which is where a failed job
    reads the message it reports.
    """

    def __init__(self) -> None:
        super().__init__()
        self.progress_text = ""

    def setProgressText(self, text: str) -> None:
        self.progress_text = text


class ProcessingJob(OperationJob):
    """A ``QgsProcessingAlgRunnerTask``, the QGIS way to run an algorithm off the main thread."""

    def __init__(
        self,
        algorithm: Any,
        parameters: dict[str, Any],
        context: Any,
        feedback: ProcessingFeedback,
        report: Callable[[dict[str, Any]], Result],
    ) -> None:
        super().__init__()
        # The task holds pointers, not owners: the algorithm, the context and the
        # feedback have to outlive it, or QGIS runs the algorithm through freed memory.
        self._algorithm = algorithm
        self._context = context
        self._feedback = feedback
        # What the algorithm produced, turned into the answer by the command that
        # ran it: the background path reports the same shape as the synchronous one.
        self._report = report
        self._task = AlgorithmTask(algorithm, parameters, context, feedback, self._task_finished)

    @property
    def progress(self) -> float | None:
        """How far the algorithm has come, in percent, as QGIS reports it."""

        return self._feedback.progress()

    @property
    def progress_text(self) -> str:
        """What the algorithm last said it was doing."""

        return self._feedback.progress_text

    def _task_finished(self, task: AlgorithmTask, _result: bool) -> None:
        # Called by QGIS on the main thread once the task has ended.
        if not task.succeeded:
            # A task says only whether it worked; why it did not is in the
            # feedback's own log, so that is what the failure carries.  It can be
            # empty: an algorithm is not obliged to say anything.
            self.fail(CommandError("processing_failed", self._feedback.textLog().strip()))
            return
        self.settle_report(self._report, task.results)

    def start(self) -> None:
        QgsApplication.taskManager().addTask(self._task)

    def cancel(self) -> None:
        self._task.cancel()


class AlgorithmTask(QgsProcessingAlgRunnerTask):
    """Report an algorithm task through one callback, whatever ended it.

    The base class emits ``executed`` from its own ``finished`` override, which
    QGIS calls on the main thread, so the outcome is recorded before the callback
    runs.  A task that was cancelled or failed reports that through the same
    signal with ``result=False``, which is why the record starts out unsuccessful
    and only a successful result lets the operation settle as succeeded.
    """

    def __init__(
        self,
        algorithm: Any,
        parameters: dict[str, Any],
        context: Any,
        feedback: Any,
        on_finished: Callable[[AlgorithmTask, bool], None],
    ) -> None:
        super().__init__(algorithm, parameters, context, feedback)
        self.succeeded = False
        self.results: dict[str, Any] = {}
        self._on_finished = on_finished
        self.executed.connect(self._remember)

    def _remember(self, successful: bool, results: dict[str, Any]) -> None:
        self.succeeded = successful
        self.results = results

    def finished(self, result: bool) -> None:
        super().finished(result)
        self._on_finished(self, result)
