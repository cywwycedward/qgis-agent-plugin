"""操作代码真正的执行位置：主线程，或一个 ``QgsTask``。

两个适配器单独成包，这样引擎可以挑一个用，而它们彼此互不导入：``main_thread`` 只会在主线程
按事件循环的节拍执行，``qgs_task`` 把放行出来的工作交给 QGIS 的任务管理器。

Where an operation's code actually runs: the main thread, or a QgsTask.

The adapters live in their own package so the engine can pick one without either
of them importing the other: ``main_thread`` only defers a step to the event loop,
``qgs_task`` hands the work that survives preparation to QGIS' task manager.
"""
