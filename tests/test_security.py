"""Path policy: only absolute local files inside an allowed root are usable."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from qgis_agent_plugin.core.config import Settings
from qgis_agent_plugin.core.models import PATH_NOT_ALLOWED, FrameworkError
from qgis_agent_plugin.core.security import resolve_path


def refused(
    raw: str,
    roots: list[Path],
    *,
    must_exist: bool = True,
    allow_directory: bool = False,
) -> FrameworkError:
    with pytest.raises(FrameworkError) as caught:
        resolve_path(raw, roots, must_exist=must_exist, allow_directory=allow_directory)
    assert caught.value.code == PATH_NOT_ALLOWED
    return caught.value


def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root


def readable_file(directory: Path, name: str = "roads.gpkg") -> Path:
    path = directory / name
    path.write_text("x", encoding="utf-8")
    return path


def test_a_file_inside_an_allowed_root_resolves(tmp_path: Path) -> None:
    root = data_root(tmp_path)
    layer = readable_file(root)
    assert resolve_path(str(layer), [root], must_exist=True) == layer.resolve()


def test_roots_may_be_given_as_strings(tmp_path: Path) -> None:
    root = data_root(tmp_path)
    layer = readable_file(root)
    assert resolve_path(str(layer), [str(root)], must_exist=True) == layer.resolve()


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    root = data_root(tmp_path)
    refused(str(root / "missing.gpkg"), [root])


def test_a_directory_is_not_a_readable_file(tmp_path: Path) -> None:
    root = data_root(tmp_path)
    refused(str(root), [root])


def test_a_directory_may_be_a_data_source(tmp_path: Path) -> None:
    # An ESRI FileGDB is a directory; the data-source shape is what allows one.
    # A read is always a regular file, so ``allow_directory`` says nothing there.
    root = data_root(tmp_path)
    source = root / "roads.gdb"
    source.mkdir()
    assert resolve_path(str(source), [root], must_exist=False, allow_directory=True) == (
        source.resolve()
    )
    refused(str(source), [root], must_exist=True, allow_directory=True)


def test_a_data_source_whose_directory_is_gone_is_still_inside_the_root(tmp_path: Path) -> None:
    # A2 #4's promise for the case where the directory itself moved away: the source
    # is not there any more, but it is still a path inside the root (AT-18B), so the
    # save shape's "the parent must exist" is not the question asked here.
    root = data_root(tmp_path)
    source = root / "moved-away" / "roads.gpkg"
    assert resolve_path(str(source), [root], must_exist=False, allow_directory=True) == (
        source.resolve()
    )


def test_a_data_source_whose_directory_is_gone_outside_the_root_is_refused(
    tmp_path: Path,
) -> None:
    root = data_root(tmp_path)
    refused(
        str(tmp_path / "moved-away" / "roads.gpkg"),
        [root],
        must_exist=False,
        allow_directory=True,
    )


def test_a_data_source_shape_does_not_follow_a_link_out_of_the_root(tmp_path: Path) -> None:
    # The non-strict resolve still follows what exists, so a source under a link that
    # leaves the root cannot be vouched for just because its directory is gone.
    root = data_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = escape_link(root, outside)
    refused(
        str(link / "moved-away" / "roads.gpkg"),
        [root],
        must_exist=False,
        allow_directory=True,
    )


def test_a_data_source_that_cannot_be_resolved_is_refused_not_an_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unreadable source is an untrusted one; it may not escape as an exception the
    # engine would report as its own defect.
    root = data_root(tmp_path)
    original = Path.resolve

    def resolve(self: Path, strict: bool = False) -> Path:
        if self.name == "unreadable.gpkg":
            raise PermissionError("denied")
        return original(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    refused(str(root / "unreadable.gpkg"), [root], must_exist=False, allow_directory=True)


def test_a_relative_path_is_refused(tmp_path: Path) -> None:
    root = data_root(tmp_path)
    refused("roads.gpkg", [root])


@pytest.mark.parametrize("must_exist", [True, False])
def test_a_null_byte_in_a_path_is_refused(must_exist: bool, tmp_path: Path) -> None:
    root = data_root(tmp_path)
    refused(str(root / "a\0b.qgz"), [root], must_exist=must_exist)


def test_a_file_outside_every_allowed_root_is_refused(tmp_path: Path) -> None:
    root = data_root(tmp_path)
    outside = readable_file(tmp_path)
    refused(str(outside), [root])


def test_no_allowed_root_refuses_everything(tmp_path: Path) -> None:
    refused(str(readable_file(tmp_path)), [])


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com/roads.gpkg",
        "http://example.com/roads.gpkg",
        "postgis:host=db.example.com dbname=gis",
        "/vsicurl/https://example.com/roads.gpkg",
        "/vsis3/bucket/roads.gpkg",
        r"\\no-such-host-audit\share\roads.gpkg",
        "//no-such-host-audit/share/roads.gpkg",
        r"\\?\UNC\no-such-host-audit\share\roads.gpkg",
    ],
)
def test_network_and_virtual_paths_are_refused(raw: str, tmp_path: Path) -> None:
    refused(raw, [tmp_path])


@pytest.mark.parametrize(
    "raw",
    [
        r"\\no-such-host-audit\share\roads.gpkg",
        "//no-such-host-audit/share/roads.gpkg",
        r"\\?\C:\data\roads.gpkg",
        "//?/C:/data/roads.gpkg",
    ],
)
@pytest.mark.parametrize("must_exist", [True, False])
def test_a_network_path_is_refused_before_the_file_system_is_touched(
    raw: str, must_exist: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A UNC path reaches a host, so resolving it would open a connection with the
    # operator's credentials (and block the main thread until it timed out).  The
    # refusal has to come first, which is what failing ``resolve`` proves.
    def resolve(*args: object, **kwargs: object) -> Path:
        raise AssertionError("the path was resolved before it was refused")

    monkeypatch.setattr(Path, "resolve", resolve)
    refused(raw, [tmp_path], must_exist=must_exist)


def test_the_refusal_does_not_repeat_the_path(tmp_path: Path) -> None:
    root = data_root(tmp_path)
    outside = readable_file(tmp_path)
    error = refused(str(outside), [root])
    assert str(outside) not in str(error)
    assert outside.name not in str(error)


def test_a_new_file_inside_the_root_may_be_saved(tmp_path: Path) -> None:
    root = data_root(tmp_path)
    target = root / "output.qgz"
    assert resolve_path(str(target), [root], must_exist=False) == target.resolve()


def test_an_existing_save_target_resolves(tmp_path: Path) -> None:
    root = data_root(tmp_path)
    target = readable_file(root, "output.qgz")
    assert resolve_path(str(target), [root], must_exist=False) == target.resolve()


def test_a_save_target_that_is_a_directory_is_refused(tmp_path: Path) -> None:
    root = data_root(tmp_path)
    existing = root / "photos"
    existing.mkdir()
    refused(str(existing), [root], must_exist=False)


def test_a_save_with_a_missing_parent_directory_is_refused(tmp_path: Path) -> None:
    root = data_root(tmp_path)
    refused(str(root / "missing" / "output.qgz"), [root], must_exist=False)


def test_a_save_target_outside_the_root_is_refused(tmp_path: Path) -> None:
    root = data_root(tmp_path)
    refused(str(tmp_path / "output.qgz"), [root], must_exist=False)


def test_a_save_target_named_as_the_parent_is_refused(tmp_path: Path) -> None:
    root = data_root(tmp_path)
    refused(str(root / ".."), [root], must_exist=False)


def escape_link(root: Path, outside: Path) -> Path:
    """Point a directory link inside ``root`` at ``outside``; skip where none can be made."""

    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
        return link
    except OSError:
        pass
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True
    )
    if created.returncode != 0:
        pytest.skip("this machine cannot create a directory link")
    return link


def test_a_link_leaving_the_root_is_refused(tmp_path: Path) -> None:
    root = data_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = readable_file(outside, "secret.gpkg")
    link = escape_link(root, outside)
    refused(str(link / secret.name), [root])
    refused(str(link / "new.gpkg"), [root], must_exist=False)


def test_a_link_staying_inside_the_root_is_allowed(tmp_path: Path) -> None:
    root = data_root(tmp_path)
    inner = root / "inner"
    inner.mkdir()
    target = readable_file(inner, "real.gpkg")
    link = escape_link(root, inner)
    assert resolve_path(str(link / target.name), [root], must_exist=True) == target.resolve()


def test_a_relative_root_allows_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = data_root(tmp_path)
    layer = readable_file(root)
    monkeypatch.chdir(tmp_path)
    refused(str(layer), [Path("data")])


def test_the_default_settings_allow_no_directory(tmp_path: Path) -> None:
    refused(str(readable_file(tmp_path)), list(Settings().allowed_data_roots))


def test_a_single_root_string_is_read_as_one_root(tmp_path: Path) -> None:
    settings = Settings.from_mapping({"allowed_data_roots": str(tmp_path)})
    assert settings.allowed_data_roots == (tmp_path,)


def test_a_single_path_root_is_read_as_one_root(tmp_path: Path) -> None:
    settings = Settings.from_mapping({"allowed_project_roots": tmp_path})
    assert settings.allowed_project_roots == (tmp_path,)


def test_unset_values_fall_back_to_the_defaults() -> None:
    assert Settings.from_mapping({"allowed_data_roots": None, "max_pending": None}) == Settings()


def test_settings_take_counts_as_strings_and_keep_the_other_defaults(tmp_path: Path) -> None:
    settings = Settings.from_mapping({"allowed_project_roots": [str(tmp_path)], "max_pending": "8"})
    assert settings.allowed_project_roots == (tmp_path,)
    assert settings.max_pending == 8
    assert settings.max_active_background == 2


COUNT_KEYS = (
    "max_active_background",
    "max_pending",
    "max_terminal_records",
    "terminal_ttl_seconds",
    "max_snapshot_features",
)


@pytest.mark.parametrize("key", COUNT_KEYS)
@pytest.mark.parametrize("value", [0, -1, "0", "-3"])
def test_a_count_below_one_is_refused_and_names_the_key(key: str, value: object) -> None:
    # A count of 0 has a silent failure mode: max_active_background=0 would leave
    # every background call waiting for a slot that never frees.
    with pytest.raises(RuntimeError) as caught:
        Settings.from_mapping({key: value})
    assert key in str(caught.value)


@pytest.mark.parametrize("key", COUNT_KEYS)
def test_a_count_that_is_not_a_number_names_the_key_and_the_value(key: str) -> None:
    with pytest.raises(RuntimeError) as caught:
        Settings.from_mapping({key: "32 (default)"})
    assert key in str(caught.value)
    assert "32 (default)" in str(caught.value)


def test_a_count_of_one_is_accepted() -> None:
    # The lower bound is inclusive: one background slot is a usable configuration.
    settings = Settings.from_mapping({"max_active_background": "1"})
    assert settings.max_active_background == 1
