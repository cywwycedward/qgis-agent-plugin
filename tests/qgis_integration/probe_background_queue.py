"""背景调用从另一个任务的 ``finished()`` 回调里启动时会不会卡住：复现这个形状。

每一轮通过真实的插件会话提交 ``per_round`` 个背景调用，数量超过两个槽位，所以每轮都有调用在等槽、
每次完成都得启动下一个。某一轮的调用没能在 ``ROUND_TIMEOUT`` 内全部走到终态就是卡住：探针把当时的
记录和 QgsTask 状态打印出来，再取消剩下的，好让下一轮能跑。没有任何一轮卡住时退出码才是 0。

QGIS 收下但从未启动的任务，``QgsTask.status()`` 是 0（``Queued``），在探针等待期间记录一直留在
``SCHEDULED``；3.44.13 上这是永久的。插件过去正是靠从另一个任务的 ``finished()`` 回调里调
``addTask`` 撞上它，这个探针用来区分两个版本。

A reproducer for a background call started from inside another task's ``finished()`` callback.
Each round submits ``per_round`` background calls through a real plugin session, more than the
two slots, so every round has calls waiting for a slot and every completion has to start the
next one.  A round whose calls do not all reach a terminal state inside ``ROUND_TIMEOUT`` is a
stall: the probe prints the records and the QgsTask statuses as they were, then cancels what is
left so the next round can run.  The exit code is 0 only when no round stalled.

A queued task that QGIS accepted but never started has ``QgsTask.status() == 0``
(``Queued``) and keeps the record in ``SCHEDULED`` for as long as the probe waits; in 3.44.13
that is permanent.  The plugin used to reach it by calling ``addTask`` from inside another
task's ``finished()`` callback, and this probe tells the two versions apart.

Run it with the QGIS interpreter, one process per run::

    D:\\QGIS 3.44.13\\bin\\python-qgis-ltr.bat tests/qgis_integration/probe_background_queue.py 100
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))
sys.path.insert(0, str(HERE))

import harness  # noqa: E402

from qgis_agent_plugin.core.execution_store import TERMINAL_STATUSES  # noqa: E402

ROUND_TIMEOUT = 15.0
RECOVERY_TIMEOUT = 5.0


def finish(ctx: harness.Context, ids: list[str], timeout: float) -> bool:
    """把事件循环转到每个执行都结束，或者 ``timeout`` 到点。

    Pump the event loop until every execution ended, or ``timeout`` passes.
    """

    deadline = time.monotonic() + timeout
    while True:
        ctx.app.processEvents()
        if all(ctx.status(execution_id) in TERMINAL_STATUSES for execution_id in ids):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.002)


def stall_report(ctx: harness.Context, ids: list[str]) -> dict[str, object]:
    """这一轮没跑完时，插件和真实管理器手里各握着什么。

    What the plugin and the real manager held when the round did not finish.
    """

    records = {}
    for execution_id in ids:
        record = ctx.record(execution_id)
        submission = ctx.runner._active.get(execution_id)
        task = ctx.manager.task(submission.task_id) if submission is not None else None
        records[execution_id] = {
            "status": str(record.status),
            "progress": record.progress,
            "task_id": submission.task_id if submission is not None else None,
            "task_status": None if task is None else int(task.status()),
            "task_progress": None if task is None else task.progress(),
        }
    return {
        "records": records,
        "waiting": len(ctx.runner._waiting),
        "active": len(ctx.runner._active),
        "manager_tasks": len(ctx.manager.tasks()),
    }


def recover(ctx: harness.Context, ids: list[str]) -> bool:
    """把卡住那一轮留下的东西取消掉，让下一轮从干净的会话开始。

    Cancel whatever a stalled round left live, so the next round starts clean.
    """

    for execution_id in ids:
        if ctx.status(execution_id) not in TERMINAL_STATUSES:
            ctx.engine.cancel(execution_id)
    if finish(ctx, ids, RECOVERY_TIMEOUT):
        return True
    # 取消没能让某条记录落定，会话就不能用了：换一个新会话，而不是让下一轮背上这一轮的残局。
    # A cancel that could not settle one record leaves the session unusable: start a new one rather
    # than blaming the next round for this one's leftovers.
    ctx.unload()
    ctx.load()
    ctx.register_probes()
    return False


def main(argv: list[str]) -> int:
    rounds = int(argv[1]) if len(argv) > 1 else 100
    per_round = int(argv[2]) if len(argv) > 2 else 6
    root = Path(tempfile.mkdtemp(prefix="a8 background queue "))
    runtime, profile = root / "runtime", root / "profile"
    application = harness.start_application(runtime, profile)
    harness.configure_settings(runtime, runtime)
    context = harness.Context(application, None)
    context.load()
    context.register_probes()

    stalls: list[dict[str, object]] = []
    started = time.monotonic()
    try:
        for number in range(1, rounds + 1):
            labels = [f"r{number:03d}-{index}" for index in range(per_round)]
            ids = [
                context.submit(
                    "probe.slow", {"label": label, "steps": 1, "step_seconds": 0.0}
                ).execution_id
                for label in labels
            ]
            if finish(context, ids, ROUND_TIMEOUT):
                continue
            report = stall_report(context, ids)
            report["round"] = number
            report["labels"] = labels
            stalls.append(report)
            print(f"STALL round {number}: {json.dumps(report, default=str)}", flush=True)
            if not recover(context, ids):
                print(f"RECOVERED round {number} by reloading the session", flush=True)
        elapsed = time.monotonic() - started
        summary = {
            "rounds": rounds,
            "per_round": per_round,
            "stalls": len(stalls),
            "seconds": round(elapsed, 1),
        }
        print(f"RESULT {json.dumps(summary)}", flush=True)
    finally:
        context.unload()
        application.exitQgis()
    return 1 if stalls else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
