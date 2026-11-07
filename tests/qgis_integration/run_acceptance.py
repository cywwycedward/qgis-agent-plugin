"""Run every PRD 12.2 case against a headless QGIS and print one line per AT id.

``python run_acceptance.py [work directory]``.  Without an argument a temporary
directory whose own name contains a space is created, the fixtures are generated
into it, and the run uses a throwaway QGIS profile inside it, so no operator
setting and no user project is touched (PRD 12.1).  A work directory given as an
argument is only used when it is strictly inside the system temporary directory and
either empty or already carries this run's marker; anything else is refused with
exit code 2 rather than emptied.  The exit code is 1 if any case failed, 0
otherwise; a skipped case never counts as a pass (PRD 12.3).
"""

from __future__ import annotations

import os
import platform
import sys
import tempfile
import traceback
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))
sys.path.insert(0, str(HERE))

# GDAL (bundled with QGIS) prints an import-time FutureWarning about exception mode;
# it is noise in an evidence log and nothing here changes that setting.
warnings.simplefilter("ignore", FutureWarning)

import cases  # noqa: E402
import fixtures  # noqa: E402
import harness  # noqa: E402


def environment() -> str:
    import pydantic
    from qgis.core import Qgis
    from qgis.PyQt.QtCore import PYQT_VERSION_STR, QT_VERSION_STR

    return (
        f"QGIS {Qgis.version()} ({Qgis.QGIS_VERSION_INT}) | Python {platform.python_version()} | "
        f"Qt {QT_VERSION_STR} / PyQt {PYQT_VERSION_STR} | pydantic {pydantic.VERSION}"
    )


def main(argv: list[str]) -> int:
    temporary = Path(tempfile.gettempdir())
    root = Path(argv[1]) if len(argv) > 1 else Path(tempfile.mkdtemp(prefix="f7 acceptance "))
    try:
        root = fixtures.claim_root(root, temporary)
    except fixtures.UnsafeRoot as error:
        print(f"REFUSING: {error}")
        return 2

    profile = root / "profile"
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    # QGIS drops symbology-style.db into the working directory, so the fixtures are
    # generated from the throwaway one rather than from the repository.
    os.chdir(runtime)
    generated = fixtures.build(root)
    application = harness.start_application(runtime, profile)

    settings_file = harness.settings_file()
    if profile.resolve() not in settings_file.resolve().parents:
        print(f"REFUSING: QGIS settings would land outside the profile: {settings_file}")
        application.exitQgis()
        return 2
    harness.configure_settings(generated.root, generated.root)

    print(f"environment: {environment()}")
    print(f"fixtures: {generated.root}")
    print(f"settings: {settings_file}")

    context = harness.Context(application, generated)
    context.load()
    lines: list[str] = []
    try:
        for case_id, case in cases.CASES:
            verdict, evidence = "PASS", ""
            try:
                evidence = case(context)
            except harness.Skip as skip:
                verdict, evidence = "SKIP", str(skip)
            except AssertionError as error:
                verdict, evidence = "FAIL", str(error)
            except Exception as error:  # noqa: BLE001 - any other failure is a failed case
                verdict = "FAIL"
                frame = traceback.extract_tb(error.__traceback__)[-1]
                evidence = (
                    f"{type(error).__name__}: {error} "
                    f"(at {Path(frame.filename).name}:{frame.lineno})"
                )
            lines.append(f"{case_id} {verdict} {evidence}")
            print(lines[-1])
            if context.plugin is None:
                context.load()
    finally:
        context.unload()
        application.exitQgis()

    counts = {
        verdict: [line for line in lines if f" {verdict} " in line]
        for verdict in ("PASS", "FAIL", "SKIP")
    }
    summary = ", ".join(f"{verdict} {len(found)}" for verdict, found in counts.items())
    print(f"summary: {summary} of {len(lines)} cases")
    print(f"artifacts: {root}")
    (root / "results.txt").write_text("\n".join(lines) + "\n" + summary + "\n", encoding="utf-8")
    return 0 if not counts["FAIL"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
