# QGIS 3.44 的同步与异步，以及本插件怎么用

结论：QGIS 里“同步”就是调用线程一直执行到返回，“异步”就是把活交给 `QgsTask`，由
`QgsTaskManager` 在自己的 `QThreadPool` 上跑 `run()`，再在**主线程**调用 `finished(bool)`。
本插件把这两条落成两个执行策略：`main_thread`（`run_sync` 直接跑，或 `submit` 排队到下一轮事件
循环）与 `background`（`prepare_main` → `QgsTask` → `finalize_main`）。

三条不变式贯穿全文，也贯穿代码：

1. 后台线程不碰 `QgsProject`、`QgsMapLayer`、`iface`、Qt widget。
2. 执行状态（`ExecutionStore`）只在主线程写；worker 只拿到 `TaskControl`（`is_cancelled()` /
   `set_progress()`）。
3. `QgsTask` 结束后 QGIS 会删掉它的 C++ 对象：插件只保留 task id，不保留任务指针。

## 1. 同步：调用线程干活

- `processing.run(...)` → `Processing.runAlgorithm`：参数校验、执行、返回都在调用线程，失败以
  `QgsProcessingException` 抛出。没有线程被创建。
- `QgsTask.waitForFinished(int timeout = 30000)` 阻塞当前线程（`src/core/qgstaskmanager.h`）；
  `0` 等到底，超时返回 `false` 且任务**还在跑**。
- 渲染：`job.start()` 立即返回，`job.waitForFinished()` 阻塞。

阻塞主线程的副作用是 Qt 不再派发事件：所有排到主线程的东西都要等这次调用返回。本插件的
`run_sync` 就是这种情况——它在主线程同步调 `execute_main`，调用期间 GUI 不动。这也解释了为什么
只有八个 Operation 允许 `run_sync`：唯一的后台 Operation 用 `run_sync` 会被
`UNSUPPORTED_EXECUTION_MODE` 拒绝，避免“同步等一个 QgsTask”这种会死锁的写法。

## 2. 异步：`QgsTask` 与 `QgsTaskManager`

- `QgsTask::run()` 是**唯一**在别的线程跑的成员；manager 把它放到线程池，结束后在**主线程**
  调用 `finished(bool)`（`qgstaskmanager.cpp`，`QgsTaskManager::taskStatusChanged`）。
- `finished` 是 `protected` 的 **virtual**，不是 signal：`task.finished.connect(...)` 不能当信号用，
  只有 manager 会调它。所以要么子类 override，要么用 `QgsTask.fromFunction(on_finished=...)`。
- 没被加进 manager 的 `QgsTask` 永远收不到 `finished()`。
- 进度从 worker 走 `setProgress()`，主线程侧通过 manager 的 `progressChanged(int, double)` 读；
  也可以在 worker 里读 `task.progress()`。
- `cancel()` 只是请求：置 `isCanceled()` 标志，子类必须自己周期性检查并尽快退出。

## 3. 三种异步形态

| 形态 | 跑活的线程 | 完成怎么观察 | 本插件用吗 |
| --- | --- | --- | --- |
| `QgsTask` 子类 | manager 的 `QThreadPool` | override `finished(bool)`，主线程 | **用**（`core/runners/qgs_task.py`） |
| `QgsTask.fromFunction` | 同上 | `on_finished(exception, values)`，主线程 | 不用（本插件需要 `setDependentLayers` 和 `isCanceled()` 的实例方法，子类更直接） |
| `QgsProcessingAlgRunnerTask` | 同上 | `executed(bool, dict)` signal + `finished()` | 不用（见 §8） |
| `QgsMapRendererParallelJob` | Qt concurrent（`QFuture`） | `finished()` **signal** | 不用（本插件不渲染） |

## 4. 本插件怎么落在这两条上

| 层 | 代码 | 用什么机制 |
| --- | --- | --- |
| 主线程同步 | `core/engine.py::run_sync` | 直接调 `execute_main`，调用线程就是主线程 |
| 主线程异步 | `core/runners/main_thread.py` | `QTimer.singleShot(0, cb)` 推一轮事件循环，回调前先问“这条执行还活着吗” |
| 后台快照 | `operations/layer.py::LayerSumNumericField.prepare_main` | 主线程读要素，复制成 `tuple[float, ...]` |
| 后台计算 | `core/runners/qgs_task.py::OperationTask(QgsTask)` | `run()` 里只调 `execute_worker(snapshot, TaskControl)` |
| 完成回调 | `OperationTask.finished` → `OperationTaskRunner.task_finished` | 主线程：更新记录、`finalize_main`、校验 epoch |
| 状态机 | `core/execution_store.py` | 只有主线程写；PRD 8.2 的边逐条成表 |
| 取消 | `engine.cancel` → `runner.cancel` | 转 `CANCEL_REQUESTED` + `manager.task(id).cancel()` |
| 卸载 | `plugin.unload` → `engine.shutdown` | 只取消本插件那个 manager 的任务，**永不** `cancelAll()` |

一次后台 `submit` 的状态序列（PRD 8.1/8.2）：

```text
QUEUED            submit 返回时；输入已经校验过，排队中
PREPARING         下一轮事件循环，主线程跑 prepare_main
SCHEDULED         addTask 返回非 0，QGIS 接手中（还没必然开始）
RUNNING           首个进度报告到达主线程时置；没报告时由完成路径 _running() 补写（轮询可能错过）
FINALIZING        finished(true) 且没被取消，主线程跑 finalize_main（先校验 project_epoch）
SUCCEEDED/FAILED  输出校验后落终态
CANCELLED         finished 报告已取消，或排队时就被取消
```

实测（headless，QGIS 3.44.13，9000 要素求和）：

```text
submit -> QUEUED
observed statuses: QUEUED -> SCHEDULED -> SUCCEEDED      # 轮询采样错过 RUNNING，记录仍经过它
worker 线程: Dummy-1 | finished 回调线程: MainThread
progressChanged 报告 100 次（1.0 … 100.0 各一次；1.0 的步进每次都会跨过 0.1 档，所以没被过滤器吞掉）
记录里的 progress: 0.0 -> 1.0 -> 99.0 -> 100.0
（首条报告、末条被节流的值、终态各写一次；轮询采样只看到两端）
```

## 5. 会咬人的地方（都写在适配器的注释里）

- **`finished` 不是信号**：只有 manager 调它。想接信号就 `fromFunction`，想控制生命周期就 override。
- **`addTask()` 返回 0 = 没接手**：此时任务的 Python 对象还在插件这边，可以丢；返回非 0 时所有权
  交给 manager（`SIP_TRANSFER`），但插件仍可 `cancel()`，只是别自己删。
- **任务结束后不能留指针**：QGIS 删掉 C++ 对象后再访问会 `RuntimeError`。本插件把
  `execution_id → task_id` 的映射只用于“活动任务定位”，任务结束就释放。
- **`cancel()` 不检查 `canCancel()`**：设置里那个检查在 GUI 层。
- **worker 里抛异常会 abort QGIS**（异常穿过 C++ 任务框架）：`OperationRun.run` 用
  `except BaseException` 兜住，转成 `FAILED(INTERNAL_ERROR)`，堆栈只进本机日志。
- **`waitForFinished(30000)` 默认 30 秒**，`0` 是无期限；超时只说明任务还在跑。本插件不用它。
- **进度节流要自己做**：`QgsTask.setProgress` 只在“十分之一”档位变化时才发 `progressChanged`
  （与 `QgsFeedback::setProgress` 是同一个过滤器，两个类一致）。实测：16 次调用
  （`0.01 / 0.02 / 0.05 / 0.09 / 0.1 / 0.15 / 0.19 / 0.2 / 0.25 / 1.0 / 1.01 / 1.05 / 1.1 / 2.0 /
  2.05 / 2.1`）只换来 6 次信号，落在 `0.1 / 0.2 / 1.0 / 1.1 / 2.0 / 2.1`；而 1.0 步进的 100 次
  调用恰好每次都跨档，所以 9000 要素那次求和的 100 次调用全都到了。任务结束时 QGIS 还会为成功的
  任务报一次 `100`（探针里最后显式值是 `2.1`，仍收到一次 `100.0`），所以适配器宁可忽略一切
  `>= 100` 的报告。这个粒度对写执行记录仍然太密，因此“写记录”被限制到每 100 ms 一次，100 只由
  `SUCCEEDED` 那一步写——否则一个已取消的任务也会留下 100% 的记录。
- **`finished(false)` 不等于有诊断**：`finished(false)` 且既没捕获到异常、也没被取消时，适配器记
  `FAILED(INTERNAL_ERROR: the task ended without a result)`。`finished(true)` 只表示 `run()` 返回了
  真值；结果能不能用由 `finalize_main` + 输出模型校验决定（`execute_worker` 真返回 `None` 时会在
  `finalize_main` 里抛，最终记 `INTERNAL_ERROR`，`message` 是 `AttributeError: unexpected failure`）。

## 6. PyQGIS 能碰什么、不能碰什么

只有 virtual 成员（public 或 protected）会让 C++ 回调进 Python——`QgsTask::run()` / `finished()`
就是 protected virtual（本插件的 `OperationTask` 正是 override 它们才能跑起来）；随 QGIS 附带的
`_core.pyi` stub 会列出方法，但不告诉你它是不是 virtual，只能看头文件的 `virtual` 与访问段。

| 成员 | 在 C++（3.44.13） | 在 PyQGIS |
| --- | --- | --- |
| `QgsFeedback::progress()`、`setProgress()` | public、inline、**非 virtual** | 可调用；override 不会被执行 |
| `QgsFeedback::mProgress`、`mCanceled` | `private` | 不可见；只能读 `progress()` / `isCanceled()` |
| `QgsProcessingFeedback::setProgressText()`、`reportError()`、`pushInfo()`、`textLog()` | public **virtual** | 可 override，且 C++ 会调到 |
| `QgsTask::run()`、`finished()` | **protected virtual** | 可 override，manager 会调到 |
| `QgsTaskManager::taskStatusChanged`、`QgsTask::mShouldTerminate` | `private` slot / `private` 成员 | 不可调用、不可读 |
| `QgsProcessingAlgRunnerTask::executed` | signal | 真的是 `pyqtSignal` |

## 7. 3.44.13 上实测过什么

前三条是上一版本文档在本机同一版本上做的实测（方法见本节末尾），本次重写没有重跑；
第四条与 §4 的观察是 F8 在 headless 探针里跑的。

在一个 `QT_QPA_PLATFORM=offscreen` 的 `QgsApplication` 里跑的探针（不装任何东西、不碰用户 profile）：

- **`setProgressText` 会进 Python，且 C++ 的默认实现被跳过**：C++ 代理转发一个字符串，Python 的
  override 收到了，事后 `textLog()` 是空的。用 C++ 侧转发才证明得了这件事，Python 调 Python
  证明不了。
- **`QgsFeedback::setProgress` 不会进 Python**：同一个代理报告 0 次调用——它不是 virtual。要读进度只能读
  `progress()`。
- **原生算法可能一直不报进度文本**：20000 个要素跑 `native:centroids`，`setProgressText` 0 次、
  `textLog()` 为空。所以“进度文本为空”不代表出错。
- **取消的确认来自任务自己的报告**：`QgsTask.finished(bool)` 只有一个布尔值，没有原因；本插件据此
  把已请求取消的任务归档为 `CANCELLED`，把真实异常归档为 `FAILED`（失败优先于取消，PRD 8.2）。
  这也是为什么 `CANCEL_REQUESTED` 不是终态：在任务真的报告之前，谁也不知道它停没停。

## 8. 本插件没有用到的机制

- **Processing**（`QgsProcessingAlgRunnerTask`）：有自己的一套 `prepare/runPrepared/postProcess`
  与 context/feedback 生命周期；MVP 的九个 Operation 里没有 Processing 调用，后台那个是纯 Python
  求和（PRD 7.10）。接 Processing 属于 P1。
- **渲染 job**（`QgsMapRendererParallelJob`）：自带线程与 `finished()` **signal**，本插件不渲染。
- **`QgsTask.fromFunction`**：够短，但拿不到 `setDependentLayers` 那类实例方法；本插件用子类。

## 9. 来源

在 tag `final-3_44_13` 上取的原始文件（`https://raw.githubusercontent.com/qgis/QGIS/final-3_44_13/…`）：

- `src/core/qgstaskmanager.h` / `.cpp` — `QgsTask`（`qgstask.h` 在该 tag 已并入此文件）、
  `cancel()`、`setProgress()`、`waitForFinished()`，以及 `QgsTaskManager::taskStatusChanged`
  调用 `task->finished()`。
- `src/core/qgsfeedback.h` — `progress()`、`setProgress()`、`cancel()`、成员默认值。
- `src/core/processing/qgsprocessingfeedback.h` / `.cpp` — `setProgressText`、`reportError`、`textLog`。
- `src/core/processing/qgsprocessingalgrunnertask.h` / `.cpp` — 构造、`cancel()`、`run()`、`finished()`。
- `src/core/processing/qgsprocessingcontext.h` — `thread()`、`pushToThread()`、`takeResultsFrom()`。
- `src/core/maprenderer/qgsmaprendererjob.h`、`qgsmaprendererparalleljob.h` — `start()`、`cancel()`、
  `cancelWithoutBlocking()`、`waitForFinished()`、`finished()` signal。
- `python/core/additions/fromfunction.py`、`qgstaskwrapper.py` — `fromFunction` 的签名与
  `on_finished` 所在线程。
- `python/plugins/processing/tools/general.py`、`python/plugins/processing/core/Processing.py` —
  `processing.run` 在跑算法前后做了什么。

另读了本机安装的 `D:\QGIS 3.44.13\apps\qgis-ltr\python\qgis\_core.pyi`（stub 只列公开成员，
不标 virtual）。本文件“实测”一节的探针在 `D:\QGIS 3.44.13\bin\python-qgis-ltr.bat` 下运行，
Qt 5.15.13、`Qgis.QGIS_VERSION` = 3.44.13-Solothurn。
