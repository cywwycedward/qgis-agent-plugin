"""工作根目录的守卫：这批代码里唯一会删目录的地方。

一次运行只允许毁掉它自己的测试数据，所以这条守卫要被测掉，而不是被信任。四项检查任一项被放松，
这里就有一条测试会失败：临时目录检查、``claim_root`` 里的标记检查、``_fresh`` 里的标记检查，以及
运行器的 ``REFUSING`` 退出码。这里不启动 ``QgsApplication``：测试数据的构造只需要 QGIS 的库。

The work-root guard: the only code in this delivery that deletes a directory.  A run may only
destroy its own test data, so the guard is tested rather than trusted.  Each test here fails
when one of its four checks is weakened: the temporary-directory check, the marker check in
``claim_root``, the marker check in ``_fresh``, and the runner's ``REFUSING`` exit code.
Nothing here starts a ``QgsApplication``: the fixture builder only needs the QGIS libraries.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

pytest.importorskip("qgis", reason="the fixture builder and the runner import qgis")

import fixtures  # noqa: E402
import run_acceptance  # noqa: E402


def test_a_root_outside_the_temp_directory_is_refused(tmp_path: Path) -> None:
    temporary = tmp_path / "temp"
    temporary.mkdir()

    with pytest.raises(fixtures.UnsafeRoot):
        fixtures.claim_root(tmp_path / "elsewhere", temporary)

    assert not (tmp_path / "elsewhere").exists(), "the refused root was created anyway"


def test_the_temp_directory_itself_is_refused(tmp_path: Path) -> None:
    with pytest.raises(fixtures.UnsafeRoot):
        fixtures.claim_root(tmp_path, tmp_path)


def test_a_non_empty_directory_this_run_did_not_create_is_refused(tmp_path: Path) -> None:
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep me", encoding="utf-8")

    with pytest.raises(fixtures.UnsafeRoot):
        fixtures.claim_root(precious, tmp_path)

    assert (precious / "keep.txt").read_text(encoding="utf-8") == "keep me"
    assert not (precious / fixtures.MARKER).exists(), "a refused directory was marked as ours"


def test_an_empty_directory_is_adopted_and_marked(tmp_path: Path) -> None:
    root = fixtures.claim_root(tmp_path / "run", tmp_path)

    assert (root / fixtures.MARKER).is_file()
    assert fixtures.claim_root(root, tmp_path) == root, "a rerun of a marked root must be allowed"


def test_fresh_refuses_a_directory_whose_parent_was_not_claimed(tmp_path: Path) -> None:
    unowned = tmp_path / "unowned"
    (unowned / "data").mkdir(parents=True)
    (unowned / "data" / "keep.txt").write_text("keep me", encoding="utf-8")

    with pytest.raises(fixtures.UnsafeRoot):
        fixtures._fresh(unowned / "data")

    assert (unowned / "data" / "keep.txt").exists()


def test_build_replaces_only_the_two_directories_it_owns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # QGIS 会把 symbology-style.db 写进工作目录，所以把工作目录关在沙箱里。
    # QGIS writes symbology-style.db into the working directory, so keep it in the sandbox.
    monkeypatch.chdir(tmp_path)
    root = fixtures.claim_root(tmp_path / "run", tmp_path)
    sibling = root / "sibling"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("keep me", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "stale.gpkg").write_text("stale", encoding="utf-8")

    built = fixtures.build(root)

    assert built.cities.is_file() and built.raster.is_file() and built.project.is_file()
    assert not (root / "data" / "stale.gpkg").exists(), "the previous fixture was not replaced"
    assert (sibling / "keep.txt").read_text(encoding="utf-8") == "keep me"
    assert built.outside.is_dir() and built.outside.parent == root
    assert fixtures.build(root).missing.is_file(), "a second build in the same root must work"


def test_the_runner_refuses_a_work_directory_outside_the_temp_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "temp"))
    (tmp_path / "temp").mkdir()

    code = run_acceptance.main(["run_acceptance.py", str(tmp_path / "elsewhere")])

    assert code == 2, "an unsafe work directory must not run anything"
    assert "REFUSING" in capsys.readouterr().out
    assert not (tmp_path / "elsewhere").exists()


def test_the_runner_refuses_a_directory_holding_files_it_did_not_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep me", encoding="utf-8")

    code = run_acceptance.main(["run_acceptance.py", str(precious)])

    assert code == 2
    assert "REFUSING" in capsys.readouterr().out
    assert (precious / "keep.txt").read_text(encoding="utf-8") == "keep me"
