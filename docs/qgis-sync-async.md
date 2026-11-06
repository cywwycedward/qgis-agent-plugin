# Sync and async in QGIS 3.44.13

How work runs on or off the main thread in QGIS 3.44.13 (`final-3_44_13`), and what this
plugin's `qgis_actions/jobs.py` adds on top. Read this before adding an `Action` that takes
more than a few milliseconds. Where a claim sits deep inside a long file the citation names
the member instead of a line range — see [Sources](#sources).

## Summary

In 3.44 synchronous means *block the calling thread*: `processing.run` validates the
parameters and runs the algorithm on whatever thread called it
(`python/plugins/processing/core/Processing.py`, `Processing.runAlgorithm`), and the
renderer's synchronous flavour is `job.start()` + `job.waitForFinished()`. Asynchronous
means handing the work to a `QgsTask`, which a `QgsTaskManager` runs on its own
`QThreadPool` (`src/core/qgstaskmanager.cpp`, `QgsTaskManager::processQueue`), and whose
outcome comes back through the virtual `finished(bool)` that the manager calls on the main
thread. The renderer is the exception that already owns threads:
`QgsMapRendererParallelJob` renders on Qt's concurrent framework and reports through the
`finished()` **signal**.

`QgsTask` is declared in `src/core/qgstaskmanager.h` at this tag — `src/core/qgstask.h` does
not exist (raw fetch returns 404), the two files having been merged.

## Sync: the calling thread does the work

- `processing.run(...)` → `processing.tools.general.run` → `Processing.runAlgorithm`, which
  calls `alg.checkParameterValues(...)`, then `execute(...)`, then `alg.run(...)`. No thread
  is created; the call returns when the algorithm is done and a failure arrives as a
  `QgsProcessingException`.
- `QgsTask::waitForFinished( int timeout = 30000 )` blocks the current thread
  (`src/core/qgstaskmanager.h:L210-L219`); `0` blocks forever, a timeout returns `false` with
  the task still running.
- A renderer job: `void start()` — "Start the rendering job and immediately return"
  (`qgsmaprendererjob.h`) — then `waitForFinished()`.

Blocking the main thread also stops Qt from delivering queued signals, so nothing posted to
the main thread runs until the call returns. *Researcher inference* from the event-loop
model, not a QGIS comment.

## Async: QgsTask and QgsTaskManager

`QgsTask::run()` is the only member that runs off the main thread; the manager starts it on
its thread pool and calls `finished(bool)` back on the main thread. Progress is reported by
calling `setProgress()` from `run()`, which emits `progressChanged` and is readable through
`progress()` (`src/core/qgstaskmanager.h:L124-L127`, `:L295-L300`). A task that is never
added to a manager never gets `finished()`: "If the task is managed by a QgsTaskManager, this
will be called after the task has finished" (`src/core/qgstaskmanager.h:L274-L284`).

## Comparison

| | Thread that runs the work | Completion is observed as | Cancel | Progress | Waited on synchronously |
| --- | --- | --- | --- | --- | --- |
| `processing.run(...)` | the calling thread | return value / raised `QgsProcessingException` | no handle to cancel | none | it is the synchronous form |
| `QgsTask` subclass | the manager's `QThreadPool` | virtual `finished(bool)`, main thread | `cancel()` sets a flag the subclass must poll | `setProgress()` → `progressChanged`, `progress()` | `waitForFinished(30000)` |
| `QgsProcessingAlgRunnerTask` | the manager's `QThreadPool` | `executed(bool, dict)` signal, emitted from its own `finished()` override | `cancel()` cancels the feedback, then the task flag | feedback `progressChanged` → task `setProgress` (`qgsprocessingalgrunnertask.cpp`, `run()`) | same as `QgsTask` |
| `QgsMapRendererParallelJob` | Qt concurrent threads (`QFuture`/`QFutureWatcher`) | `finished()` **signal** | `cancelWithoutBlocking()`, or blocking `cancel()` | none | `waitForFinished()` |

## Minimal shapes

Synchronous algorithm run, as `_checked`/`run` already do it
(`qgis_actions/processing/execute_processing.py:L66-L86`):

```python
from qgis.core import QgsProcessingFeedback
import processing

feedback = QgsProcessingFeedback()
results = processing.run(algorithm, parameters, context=context, feedback=feedback,
                         is_child_algorithm=True)   # returns when done; raises on failure
```

`QgsTask` subclass — the base contract (`run` off-thread, `finished` on-thread):

```python
from qgis.core import QgsTask

class Work(QgsTask):
    def run(self) -> bool:
        for i in range(100):
            if self.isCanceled():
                return False
            self.setProgress(i)          # emits progressChanged
        return True

    def finished(self, result: bool) -> None:   # main thread; result carries no reason
        ...
```

`QgsTask.fromFunction`, the shortest asynchronous form
(`python/core/additions/fromfunction.py:L31-L38`):

```python
# QgsTask.fromFunction(description, function, *args, on_finished=None,
#                      flags=QgsTask.Flag.AllFlags, **kwargs)
task = QgsTask.fromFunction("my task", calculate, on_finished=calculation_finished)
QgsApplication.taskManager().addTask(task)
```

`on_finished` is called from `QgsTaskWrapper.finished()`
(`python/core/additions/qgstaskwrapper.py`), i.e. on the main thread, with
`(exception, returned_values)`; `run()` turns any Python exception into `False` plus
`self.exception`, and a cancelled run synthesises `Exception("Task canceled")`.

`QgsProcessingAlgRunnerTask` — the runner the plugin uses (`qgis_actions/jobs.py:L216-L246`):

```python
from qgis.core import QgsProcessingAlgRunnerTask, QgsApplication

task = QgsProcessingAlgRunnerTask(algorithm, parameters, context, feedback)
task.executed.connect(lambda ok, results: ...)   # executed is a real signal
QgsApplication.taskManager().addTask(task)       # returns 0 on failure
```

Renderer job — asynchronous because the job already has threads
(`qgis_actions/view/render_map.py:L68-L79`):

```python
from qgis.core import QgsMapRendererParallelJob

job = QgsMapRendererParallelJob(settings)
job.finished.connect(save_rendered_image)   # signal, delivered on the main thread
job.start()
```

## Cancellation

`QgsTask::cancel()` only asks (`src/core/qgstaskmanager.h:L138-L147`): "Calling this is not
guaranteed to immediately end the task, rather it sets the isCanceled() flag which task
subclasses can check and terminate their operations at an appropriate time… Derived classes
must ensure that the base class implementation is called from any overridden version."

The implementation (`qgstaskmanager.cpp`, `QgsTask::cancel`) sets `mShouldTerminate` under a
mutex and terminates immediately only if the task has not started yet. It does not consult
`canCancel()`; the prompt-and-check lives in the GUI, not here.

`QgsProcessingAlgRunnerTask::cancel()` cancels the feedback *and* the task
(`qgsprocessingalgrunnertask.cpp`):

```cpp
void QgsProcessingAlgRunnerTask::cancel()
{
  if ( mFeedback )
    mFeedback->cancel();
  QgsTask::cancel();
}
```

and its `run()` reports cancellation as failure: `return ok && !mFeedback->isCanceled();`.
Algorithms that honour `QgsFeedback::isCanceled()` stop on their next check; those that do
not run to the end and their result is discarded.

Renderer jobs have two different cancels (`qgsmaprendererjob.h`): `cancel()` "does not
return until the job has terminated", while `cancelWithoutBlocking()` "will continue to
operate until it is able to cancel, at which stage the finished() signal will be emitted".
Anything that must return immediately uses the second.

## Progress

- `QgsFeedback::progress()` returns a percentage 0–100 and is a plain member read
  (`src/core/qgsfeedback.h:L77-L84`); `mProgress` starts at `0.0` and `mCanceled` at `false`
  (same file, private members). **There is no "unknown = -1" convention in 3.44.13**:
  `QgsProcessingFeedback` does not override `progress()` and nothing sets a negative value.
  `jobs.py`'s `>= 0` guard is defensive, not a QGIS contract.
- `setProgress()` only emits `progressChanged` when the value moves by a tenth
  (`qgsfeedback.h`, `setProgress`), so poll `progress()` rather than counting signals.
- `setProgressText()` is not a stored field: `QgsProcessingFeedback::setProgressText`
  appends the text to the HTML and text logs (`qgsprocessingfeedback.cpp`), and there is no
  getter. A poller that wants the latest text must capture it itself — which is what
  `ProcessingFeedback.setProgressText` does (`qgis_actions/jobs.py:L158-L159`), and that
  override also keeps progress chatter out of the log.
- `textLog()` is the accumulated message log: every `pushInfo`, `pushWarning`, `reportError`
  and — by default — every progress text lands there. It is the only place a failed
  processing run says *why*.

## What PyQGIS can and cannot touch

Three tiers, and only the virtual public ones let C++ call back into Python. The stub shipped
in `apps/qgis-ltr/python/qgis/_core.pyi` lists a method whether or not C++ ever dispatches
into an override, so the stub cannot answer this — the header's `virtual` and its access
section can.

| Member | In C++ (`final-3_44_13`) | In PyQGIS |
| --- | --- | --- |
| `QgsFeedback::progress()`, `setProgress()` | `CORE_EXPORT`, public, inline, **not virtual**, `SIP_HOLDGIL` | callable; overriding them changes nothing, C++ never dispatches into the override |
| `QgsFeedback::mProgress`, `mCanceled` | `private` | not exposed; read through `progress()` / `isCanceled()` |
| `QgsProcessingFeedback::setProgressText()`, `reportError()`, `pushInfo()`, `textLog()` | `CORE_EXPORT`, public **virtual** | overridable, and C++ does call the override |
| `QgsProcessingFeedback::mTextLog`, `log()` | `private` | not exposed; `textLog()` is the only read |
| `QgsTask::run()`, `finished()` | `CORE_EXPORT`, **protected virtual** | overridable, and the manager dispatches into them |
| `QgsTaskManager::taskStatusChanged`, `QgsTask::mShouldTerminate` | `private` slot / `private` member | not callable, not readable; they are the manager's own plumbing |
| `QgsProcessingAlgRunnerTask::executed` | signal | real `pyqtSignal` |

The `private:` members matter for reading this document, not for writing a plugin: anything
this file says about `mProgress`, `mTextLog` or `mShouldTerminate` is how QGIS works inside,
and the only handles PyQGIS gets are the public methods in the same table.

## Measured on 3.44.13

Four checks against the installed build. The first two drive the virtual through C++ only —
a `QgsProcessingMultiStepFeedback` forwards to its wrapped feedback in C++, so a Python
override is reached only if the virtual really exists.

- **`setProgressText` reaches Python, and the base implementation really is skipped.** A C++
  proxy forwarding one string landed in the override, and `textLog()` afterwards was `''`:
  the C++ append never runs, so an override suppresses log growth as well as capturing the
  text. This is the measured form of the claim `jobs.py:L158-L159` now makes.
- **`setProgress` never reaches Python.** The same C++ proxy reported `0` calls to a Python
  override: the method is not virtual, so overriding it is dead code. Read `progress()`
  instead, which is what `ProcessingJob.progress` does.
- **A native algorithm may never report progress text at all.** A full `native:centroids` run
  over 20,000 features made `0` `setProgressText` calls and left `textLog()` empty. Progress
  text is per-algorithm, not a guarantee, so `progress_text` being `None` or `''` is normal
  and says nothing about the run.
- **The job layer and the manager layer disagree on a cancelled task, on purpose.** Cancel a
  `ProcessingJob` and the job settles `error_code="processing_failed"` with an empty message,
  because a cancelled algorithm task simply reports `finished(False)`. Only
  `OperationRegistry` turns that into `terminal_outcome="cancelled"`, since it read
  `cancel_requested` first (`operation_registry.py`, `_finished`). Without that one writer a
  user-initiated cancel would arrive as a failure.

## Errors

`finished(bool)` carries one bit and no reason. When an algorithm fails, the exception text
and the algorithm's own messages go through `QgsProcessingFeedback::reportError`, which
writes them to the log (`qgsprocessingfeedback.cpp`). Hence:

```python
error_message = self._feedback.textLog().strip() or None   # jobs.py:L196-L207
```

`QgsProcessingAlgRunnerTask::finished(bool)` runs `mAlgorithm->postProcess(...)` and then
emits `executed(result, results)`, so the results arrive through a signal, after
post-processing, on the main thread. `AlgorithmTask` connects to `executed` to record success
*before* the `finished()` override runs (`qgis_actions/jobs.py:L226-L246`).

A renderer job reports errors through the job's own error list; `finished()` only means "the
render stopped", cancelled or not.

## Two constraints this plugin depends on

### (a) `finished` is a virtual on the main thread, not a signal

Source comment, `src/core/qgstaskmanager.h:L274-L284`:

> If the task is managed by a QgsTaskManager, this will be called after the task has
> finished (whether through successful completion or via early termination). The result
> argument reflects whether the task was successfully completed or not. This method is
> always called from the main thread, so it is safe to create widgets and perform other
> operations which require the main thread. However, the GUI will be blocked for the
> duration of this method so tasks should avoid performing any lengthy operations here.

It is declared under `protected:` (`:L262`) and is absent from the `signals:` block
(`:L221-L260`), which is why `task.finished.connect(...)` cannot work and why `jobs.py` wraps
every task in a `QObject` with its own `finished` signal (`qgis_actions/jobs.py:L29-L98`). The
manager is what calls it: `QgsTaskManager::taskStatusChanged` invokes `task->finished( result )`
on `Complete`/`Terminated` (`qgstaskmanager.cpp`), reached through the task's `statusChanged`
signal.

### (b) A runner task must not be handed a context that already resolved parameters

This is **observed behaviour in this plugin**, not a documented QGIS rule. `_checked` runs
`checkParameterValues(...)` against a throwaway context and says why
(`qgis_actions/processing/execute_processing.py:L122-L139`): "a task must be given a context
nothing has resolved parameters with: QGIS keeps what the check left there alive past the
run, so handing a context that has already resolved parameters to the task crashes the next
one."

What the source does support: the context is stateful and thread-bound — `thread()` is the
temporary layer store's affinity, `pushToThread()` asserts the current thread matches, and
`takeResultsFrom()` is "only safe to call when both this context and the other context share
the same thread() affinity" (`qgsprocessingcontext.h`). And the runner task calls `prepare()`
again in its own constructor (`qgsprocessingalgrunnertask.cpp`), so a context one
`prepare`/`checkParameterValues` pass has touched gets a second pass on another thread.
`unverified:` no source comment states the crash; it is the plugin's own observation.

The other half of the constraint is lifetime: the task holds raw pointers to the algorithm,
the context and the feedback, not copies — keep them alive on the job
(`qgis_actions/jobs.py:L162-L181`).

## Pitfalls verified on 3.44.13

- `finished()` is a protected virtual, never a signal, and only a manager calls it — a bare
  task object has no completion callback (`qgstaskmanager.h:L274-L284`).
- `QgsTaskManager::addTask` returns "unique task ID, or 0 if task could not be added"
  (`qgstaskmanager.h`, `addTask`) — check it.
- `addTask` takes `SIP_TRANSFER`: ownership moves to the manager, so keep your own reference
  if you still want to call `cancel()` later (`jobs.py` does: `self._task`).
- `waitForFinished( int timeout = 30000 )` defaults to 30 s, `0` waits forever, and a timeout
  means the task is *still running* (`qgstaskmanager.h:L210-L219`). It calls
  `QCoreApplication::sendPostedEvents( this )` after the wait, so a posted `finished()` still
  arrives after it returns (`qgstaskmanager.cpp`).
- `QgsTask::cancel()` does not check `canCancel()`; only the GUI does
  (`qgstaskmanager.cpp`, `QgsTask::cancel`).
- `QgsProcessingAlgRunnerTask` strips `CanCancel` when the algorithm lacks
  `ProcessingAlgorithmFlag::CanCancel`, so `task.canCancel()` can be `False`
  (`qgsprocessingalgrunnertask.cpp`, constructor).
- The manager lookup is `task( id )` and `taskId( task )` returns `-1` when unknown; there is
  no `taskFromId` on this class (`qgstaskmanager.h`, `task`/`taskId`).
- `QgsFeedback::progress()` starts at `0.0`; nothing in `qgsfeedback.h` or
  `qgsprocessingfeedback.cpp` sets `-1` for "unknown".
- `QgsProcessingFeedback::setProgressText()` appends to `textLog()`/`htmlLog()` and cannot be
  read back (`qgsprocessingfeedback.cpp`).
- `QgsMapRendererJob::cancel()` blocks until the job has terminated; use
  `cancelWithoutBlocking()` from anything that must return (`qgsmaprendererjob.h`).

## How this plugin maps onto it

| Plugin class | QGIS mechanism | Why the wrapper exists |
| --- | --- | --- |
| `OperationJob` (`jobs.py:L29`) | none | One `finished` **signal**, one `settle()` that fires once, and `progress`/`progress_text` that may be `None`. |
| `RenderMapJob` (`jobs.py:L104`) | `QgsMapRendererParallelJob` | The job already has threads and a real `finished` signal; the wrapper only saves the image and settles. No progress, because the job reports none. |
| `ProcessingFeedback` (`jobs.py:L154-L159`) | `QgsProcessingFeedback` | Keeps the latest `setProgressText` string where a poll can read it, and keeps it out of the log. |
| `AlgorithmTask` (`jobs.py:L216-L246`) | `QgsProcessingAlgRunnerTask` | Turns `executed(bool, results)` plus the `finished` override into one callback, so a cancelled task and a failed task both settle. |
| `ProcessingJob` (`jobs.py:L162-L213`) | `QgsProcessingAlgRunnerTask` | Holds the algorithm/context/feedback alive, maps progress, and takes the failure reason from `textLog()`. |
| `OperationRegistry` (`operation_registry.py`, `start`/`cancel`/`status`) | none | The only writer of a terminal outcome, and the store a poll reads: the job itself is released at the boundary, because QGIS keeps neither the finished task nor its result. |
| `Action.execution` / `.modes` (`qgis_actions/base.py:L109-L121`) | none | A declaration states whether `run`, `arun` or both exist; the gateway picks the method (`qgis_gateway.py:L93-L94`). |

A new asynchronous action declares `execution = "both"` (or `"async"`), writes an `arun` that
builds a job and ends with `ctx.operations.start(self.name, job)`, and reuses `RenderMapJob` or
`ProcessingJob` when the mechanism is one of those two. Only a new mechanism needs a new adapter.

## Sources

Raw files fetched at tag `final-3_44_13` (`https://raw.githubusercontent.com/qgis/QGIS/final-3_44_13/…`):

- `src/core/qgstaskmanager.h` / `.cpp` — `QgsTask` (merged in from `qgstask.h`), `cancel()`,
  `setProgress()`, `waitForFinished()`, and `QgsTaskManager::taskStatusChanged` calling
  `task->finished()`.
- `src/core/qgsfeedback.h` — `progress()`, `setProgress()`, `cancel()`, member defaults.
- `src/core/processing/qgsprocessingfeedback.h` / `.cpp` — `setProgressText`, `reportError`,
  `textLog`.
- `src/core/processing/qgsprocessingalgrunnertask.h` / `.cpp` — constructor, `cancel()`,
  `run()`, `finished()`.
- `src/core/processing/qgsprocessingcontext.h` — `thread()`, `pushToThread()`,
  `copyThreadSafeSettings()`, `takeResultsFrom()`.
- `src/core/maprenderer/qgsmaprendererjob.h`, `qgsmaprendererparalleljob.h` — `start()`,
  `cancel()`, `cancelWithoutBlocking()`, `waitForFinished()`, `finished()` signal,
  `QFuture` members.
- `src/core/CMakeLists.txt` — only `qgstaskmanager.cpp` is compiled from the task sources.
- `python/core/additions/fromfunction.py`, `qgstaskwrapper.py` — `fromFunction` signature and
  which thread `on_finished` runs on.
- `python/plugins/processing/tools/general.py`, `python/plugins/processing/core/Processing.py`
  — what `processing.run` does before and after the algorithm runs.

Also fetched: `api.github.com` listings for `src/core` and `python/core/additions`;
`github.com/qgis/QGIS/blob/final-3_44_13/src/core/qgsfeedback.h` (page only). Read locally:
`D:\QGIS 3.44.13\apps\qgis-ltr\python\qgis\_core.pyi` (the shipped stub — it lists public
members but not which of them are `virtual`) and the installed `processing` plugin under
`apps/qgis-ltr/python/plugins`.

The "Measured on 3.44.13" checks ran in one throwaway script under
`python-qgis-ltr.bat` with `QT_QPA_PLATFORM=offscreen`, `QgsApplication([], False)` +
`initQgis()`, `Processing.initialize()`, a 20,000-feature memory layer, and
`QApplication.processEvents()` as the event loop. Nothing was installed and the plugin
profile was not touched. `QgsProcessingMultiStepFeedback` was used as the C++ caller because
it forwards `setProgressText` to its wrapped feedback from C++ — a Python-to-Python call could
not prove that the virtual exists.

Line numbers are counted on the fetched text, not taken from a line-numbered view — treat the
quoted text as the authoritative locator. Signature checks were run against the installed
build (`D:\QGIS 3.44.13\bin\python-qgis-ltr.bat`; `Qgis.QGIS_VERSION` = 3.44.13-Solothurn,
Qt 5.15.13) and agree with the tagged source on every claim that rests on them:
`QgsTask.finished` is a `builtin_function_or_method` while
`QgsProcessingAlgRunnerTask.executed` is a `pyqtSignal`; `QgsTaskManager` has `task(id)` and
`taskId(task)` but no `taskFromId`; `addTask` is documented to return `0` on failure;
`QgsTask.cancel()` leaves `isCanceled()` true; and `progress()` reads `0.0` on a fresh
`QgsFeedback` and a fresh `QgsTask` — nothing reports `-1`.
