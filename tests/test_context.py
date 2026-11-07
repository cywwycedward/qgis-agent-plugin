"""The project epoch and the thread rule the engine enforces (PRD 9.1, 8.3)."""

from __future__ import annotations

import threading

from qgis_agent_plugin.core.context import ProjectLifecycle, require_main_thread
from qgis_agent_plugin.core.models import WRONG_THREAD, FrameworkError


def test_a_new_lifecycle_starts_at_epoch_zero() -> None:
    assert ProjectLifecycle().current_epoch == 0


def test_bump_returns_the_new_epoch_and_only_counts_up() -> None:
    lifecycle = ProjectLifecycle()
    assert [lifecycle.bump(), lifecycle.bump()] == [1, 2]
    assert lifecycle.current_epoch == 2


def test_the_owner_thread_passes_the_check() -> None:
    require_main_thread(threading.get_ident())


def test_another_thread_is_refused_with_wrong_thread() -> None:
    owner = threading.get_ident()
    failures: list[FrameworkError] = []

    def call() -> None:
        try:
            require_main_thread(owner)
        except FrameworkError as error:
            failures.append(error)

    thread = threading.Thread(target=call)
    thread.start()
    thread.join()
    assert [error.code for error in failures] == [WRONG_THREAD]
