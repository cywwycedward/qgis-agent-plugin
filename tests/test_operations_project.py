"""The ``project.*`` operations, driven by a fake ctx and a fake PyQGIS.

These are the unit-level ATs: unreadable input, the replacement guard, the path
policy, the overwrite rule and the AT-18B partial-load count.  Real PyQGIS
behaviour (does ``read()`` actually populate the layer tree) belongs to the QGIS
acceptance run, not here.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from qgis_agent_plugin.core.config import Settings
from qgis_agent_plugin.core.contracts import OperationContext
from qgis_agent_plugin.core.models import (
    INTERNAL_ERROR,
    INVALID_CRS,
    PATH_NOT_ALLOWED,
    PROJECT_OPEN_FAILED,
    PROJECT_PATH_REQUIRED,
    PROJECT_REPLACE_REQUIRED,
    PROJECT_SAVE_FAILED,
    TARGET_EXISTS,
    ExecutionStrategy,
    FrameworkError,
    OperationKind,
)
from qgis_agent_plugin.core.registry import OperationRegistry
from qgis_agent_plugin.operations import project

ROOT = Path(__file__).resolve().parents[1]
# Absolute and well formed, never touched: only the input models see it.
A_PROJECT_FILE = str(ROOT / "maps" / "a.qgs")


class FakeCrs:
    """Valid exactly for the authids a real QGIS resolves; anything else is not."""

    _KNOWN = frozenset({"EPSG:4326", "EPSG:3857"})

    def __init__(self, authid: str) -> None:
        self._authid = authid

    def isValid(self) -> bool:
        return self._authid.upper() in self._KNOWN

    def authid(self) -> str:
        """QGIS reports the canonical form whatever alias the caller used."""

        return self._authid.upper()


@pytest.fixture
def qgis(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """The operations import ``qgis.core`` inside their method body; this is that module."""

    core: Any = types.ModuleType("qgis.core")
    core.QgsCoordinateReferenceSystem = FakeCrs
    package: Any = types.ModuleType("qgis")
    package.core = core
    monkeypatch.setitem(sys.modules, "qgis", package)
    monkeypatch.setitem(sys.modules, "qgis.core", core)
    return core


class FakeLayer:
    def __init__(self, *, valid: bool = True) -> None:
        self._valid = valid

    def isValid(self) -> bool:
        return self._valid


class FakeProject:
    def __init__(
        self,
        *,
        layers: dict[str, FakeLayer] | None = None,
        file_name: str = "",
        dirty: bool = False,
        crs_authid: str = "EPSG:4326",
        read_result: bool = True,
        layers_after_read: dict[str, FakeLayer] | None = None,
        write_result: bool = True,
    ) -> None:
        self.layers = dict(layers or {})
        self.file_name = file_name
        self.dirty = dirty
        self.crs_authid = crs_authid
        self.read_result = read_result
        self.layers_after_read = dict(layers_after_read or {})
        self.write_result = write_result
        self.read_path: str | None = None
        self.writes: list[str] = []
        self.crs_set: FakeCrs | None = None

    def mapLayers(self) -> dict[str, FakeLayer]:  # noqa: N802 - a PyQGIS name
        return dict(self.layers)

    def fileName(self) -> str:  # noqa: N802 - a PyQGIS name
        return self.file_name

    def isDirty(self) -> bool:  # noqa: N802 - a PyQGIS name
        return self.dirty

    def crs(self) -> FakeCrs:
        return FakeCrs(self.crs_authid)

    def setCrs(self, crs: FakeCrs) -> None:  # noqa: N802 - a PyQGIS name
        self.crs_set = crs
        self.crs_authid = crs.authid()

    def read(self, path: str) -> bool:
        self.read_path = path
        if not self.read_result:
            return False
        self.file_name = path
        self.layers = dict(self.layers_after_read)
        return True

    def write(self, path: str) -> bool:
        self.writes.append(path)
        if not self.write_result:
            return False
        self.file_name = path
        self.dirty = False
        return True


class FakeIface:
    def __init__(self, project_: FakeProject | None = None, *, result: bool = True) -> None:
        self.project = project_
        self.result = result
        self.new_project_calls: list[bool] = []

    def newProject(self, promptToSaveFlag: bool = False) -> bool:  # noqa: N802 - a PyQGIS name
        self.new_project_calls.append(promptToSaveFlag)
        if self.result and self.project is not None:
            # A real new project starts empty, so the result counts are read from it.
            self.project.layers.clear()
            self.project.file_name = ""
            self.project.dirty = False
        return self.result


class FakeLifecycle:
    def __init__(self, epoch: int = 0) -> None:
        self.current_epoch = epoch


def context(
    project_: FakeProject,
    *,
    iface: FakeIface | None = None,
    roots: tuple[Path, ...] = (),
    epoch: int = 0,
) -> OperationContext:
    return OperationContext(
        iface=iface or FakeIface(project_),
        project=project_,
        lifecycle=FakeLifecycle(epoch),
        policy=Settings(allowed_project_roots=roots),
    )


def project_dir(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


def refused(code: str, call: Any) -> FrameworkError:
    with pytest.raises(FrameworkError) as caught:
        call()
    assert caught.value.code == code
    return caught.value


# --- input models -----------------------------------------------------------


@pytest.mark.parametrize(
    "model, fields",
    [
        (project.ProjectCreateParams, {}),
        (project.ProjectOpenParams, {"path": A_PROJECT_FILE}),
        (project.ProjectSaveParams, {}),
        (project.ProjectGetInfoParams, {}),
    ],
    ids=["create", "open", "save", "get_info"],
)
def test_an_unknown_input_field_is_refused(model: Any, fields: dict[str, Any]) -> None:
    # The valid payload has to pass on its own, otherwise the refusal below could
    # come from a missing field rather than from the unknown one.
    model.model_validate(fields)
    with pytest.raises(ValidationError):
        model.model_validate({**fields, "unexpected": True})


@pytest.mark.parametrize("value", ["true", "false", 0, 1])
def test_a_boolean_field_refuses_a_non_boolean(value: object) -> None:
    for model, field, extra in (
        (project.ProjectCreateParams, "discard_current", {}),
        (project.ProjectOpenParams, "discard_current", {"path": A_PROJECT_FILE}),
        (project.ProjectSaveParams, "overwrite", {}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate({field: value, **extra})


def test_an_empty_crs_is_refused() -> None:
    with pytest.raises(ValidationError):
        project.ProjectCreateParams.model_validate({"crs_authid": ""})


def test_the_default_crs_and_flags_are_the_prd_defaults() -> None:
    request = project.ProjectCreateParams.model_validate({})
    assert request.crs_authid == "EPSG:4326"
    assert request.discard_current is False
    assert project.ProjectSaveParams.model_validate({}).overwrite is False


@pytest.mark.parametrize("raw", ["maps/a.qgs", "", "a.gz", "C:/maps/a.txt", "C:/maps/a"])
def test_a_project_path_must_be_absolute_with_a_project_suffix(raw: str) -> None:
    for model in (project.ProjectOpenParams, project.ProjectSaveParams):
        with pytest.raises(ValidationError):
            model.model_validate({"path": raw})


def test_a_project_suffix_is_case_insensitive(tmp_path: Path) -> None:
    opened = project.ProjectOpenParams.model_validate({"path": str(tmp_path / "a.QGZ")})
    assert opened.path.endswith("a.QGZ")
    saved = project.ProjectSaveParams.model_validate({"path": str(tmp_path / "b.QGS")})
    assert saved.path is not None and saved.path.endswith("b.QGS")


# --- project.create ---------------------------------------------------------


@pytest.mark.parametrize(
    "project_state",
    [
        {"layers": {"a": FakeLayer()}},
        {"file_name": "C:/maps/current.qgs"},
        {"dirty": True},
    ],
    ids=["layers", "associated file", "unsaved changes"],
)
def test_create_refuses_to_replace_a_project_with_content(
    qgis: types.ModuleType, project_state: dict[str, Any]
) -> None:
    loaded = FakeProject(**project_state)
    iface = FakeIface(loaded)
    ctx = context(loaded, iface=iface)

    request = project.ProjectCreateParams()
    refused(
        PROJECT_REPLACE_REQUIRED,
        lambda: project.ProjectCreate().execute_main(ctx, request),
    )
    assert iface.new_project_calls == []


def test_create_replaces_the_project_when_explicitly_allowed(qgis: types.ModuleType) -> None:
    loaded = FakeProject(layers={"a": FakeLayer()}, file_name="C:/maps/old.qgs", dirty=True)
    iface = FakeIface(loaded)
    ctx = context(loaded, iface=iface)

    result = project.ProjectCreate().execute_main(
        ctx, project.ProjectCreateParams(discard_current=True)
    )

    assert iface.new_project_calls == [False]
    assert result.crs_authid == "EPSG:4326"
    assert result.file_path is None
    assert result.layer_count == 0
    assert loaded.crs_set is not None


def test_create_reports_the_canonical_crs_authid(qgis: types.ModuleType) -> None:
    loaded = FakeProject()
    iface = FakeIface(loaded)
    ctx = context(loaded, iface=iface)

    result = project.ProjectCreate().execute_main(
        ctx, project.ProjectCreateParams(crs_authid="epsg:3857")
    )

    assert result.crs_authid == "EPSG:3857"
    assert loaded.crs_authid == "EPSG:3857"


def test_create_reports_the_layers_the_new_project_brought(qgis: types.ModuleType) -> None:
    # A QGIS project template can populate a brand new project, so the counts are
    # read from the project rather than hardcoded to the PRD's sample zeros.
    template = FakeProject(layers={"a": FakeLayer(), "b": FakeLayer()})
    ctx = context(template, iface=FakeIface())

    result = project.ProjectCreate().execute_main(
        ctx, project.ProjectCreateParams(discard_current=True)
    )

    assert result.layer_count == 2
    assert result.file_path is None


def test_create_refuses_an_unusable_crs_before_replacing(qgis: types.ModuleType) -> None:
    loaded = FakeProject(file_name="C:/maps/current.qgs")
    iface = FakeIface(loaded)
    ctx = context(loaded, iface=iface)

    refused(
        INVALID_CRS,
        lambda: project.ProjectCreate().execute_main(
            ctx, project.ProjectCreateParams(crs_authid="EPSG:9999", discard_current=True)
        ),
    )
    assert iface.new_project_calls == []


def test_create_reports_a_refused_new_project(qgis: types.ModuleType) -> None:
    loaded = FakeProject()
    iface = FakeIface(loaded, result=False)
    ctx = context(loaded, iface=iface)

    refused(
        INTERNAL_ERROR,
        lambda: project.ProjectCreate().execute_main(ctx, project.ProjectCreateParams()),
    )


# --- project.open -----------------------------------------------------------


def test_open_refuses_a_path_outside_the_allowed_roots(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    root = project_dir(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    target = outside / "a.qgs"
    target.write_text("", encoding="utf-8")
    loaded = FakeProject()
    ctx = context(loaded, roots=(root,))
    request = project.ProjectOpenParams(path=str(target))

    refused(PATH_NOT_ALLOWED, lambda: project.ProjectOpen().execute_main(ctx, request))
    assert loaded.read_path is None


def test_open_refuses_a_missing_file(qgis: types.ModuleType, tmp_path: Path) -> None:
    root = project_dir(tmp_path)
    ctx = context(FakeProject(), roots=(root,))

    refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(root / "missing.qgs"))
        ),
    )


def test_open_refuses_to_replace_a_project_with_content(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    root = project_dir(tmp_path)
    target = root / "a.qgs"
    target.write_text("", encoding="utf-8")
    loaded = FakeProject(dirty=True)
    ctx = context(loaded, roots=(root,))
    request = project.ProjectOpenParams(path=str(target))

    refused(PROJECT_REPLACE_REQUIRED, lambda: project.ProjectOpen().execute_main(ctx, request))
    assert loaded.read_path is None


def test_open_reports_a_failed_read(qgis: types.ModuleType, tmp_path: Path) -> None:
    root = project_dir(tmp_path)
    target = root / "a.qgs"
    target.write_text("not a project", encoding="utf-8")
    loaded = FakeProject(read_result=False)
    ctx = context(loaded, roots=(root,))
    request = project.ProjectOpenParams(path=str(target))

    failed = refused(PROJECT_OPEN_FAILED, lambda: project.ProjectOpen().execute_main(ctx, request))
    assert "may have changed" in failed.message


def test_open_replaces_a_stateful_project_when_explicitly_allowed(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # PRD 7.3: `open` has the same guard as `create`, so the authorized branch has
    # to be exercised here too, not only through `create`.
    root = project_dir(tmp_path)
    target = root / "a.qgs"
    target.write_text("", encoding="utf-8")
    loaded = FakeProject(layers={"old": FakeLayer()}, file_name="C:/maps/old.qgs", dirty=True)
    ctx = context(loaded, roots=(root,))

    result = project.ProjectOpen().execute_main(
        ctx, project.ProjectOpenParams(path=str(target), discard_current=True)
    )

    assert loaded.read_path == str(target.resolve())
    assert loaded.file_name == str(target.resolve())
    assert result.layer_count == 0


def test_open_uses_the_normalised_path(qgis: types.ModuleType, tmp_path: Path) -> None:
    # `resolve_path` is what vets the path, so its result is what QGIS and the
    # caller's output must see, not the raw request string.
    root = project_dir(tmp_path)
    (root / "sub").mkdir()
    target = root / "a.qgz"
    target.write_text("", encoding="utf-8")
    loaded = FakeProject()
    ctx = context(loaded, roots=(root,))

    result = project.ProjectOpen().execute_main(
        ctx, project.ProjectOpenParams(path=str(root / "sub" / ".." / "a.qgz"))
    )

    assert loaded.read_path == str(target.resolve())
    assert Path(result.file_path) == target.resolve()


def test_open_succeeds_and_counts_the_layers_that_cannot_load(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    root = project_dir(tmp_path)
    target = root / "a.qgz"
    target.write_text("", encoding="utf-8")
    loaded = FakeProject(
        read_result=True,
        # Two usable layers and one broken one: the counts differ, so counting the
        # wrong side (the usable ones) cannot pass this test.
        layers_after_read={
            "good": FakeLayer(),
            "also_good": FakeLayer(),
            "broken": FakeLayer(valid=False),
        },
        crs_authid="EPSG:3857",
    )
    ctx = context(loaded, roots=(root,))

    result = project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert Path(result.file_path) == target.resolve()
    assert loaded.read_path == str(target.resolve())
    assert result.layer_count == 3
    assert result.invalid_layer_count == 1
    assert result.crs_authid == "EPSG:3857"


# --- project.save -----------------------------------------------------------


def test_save_without_a_path_or_associated_file_is_refused(qgis: types.ModuleType) -> None:
    ctx = context(FakeProject())

    refused(
        PROJECT_PATH_REQUIRED,
        lambda: project.ProjectSave().execute_main(ctx, project.ProjectSaveParams()),
    )


def test_save_refuses_to_overwrite_an_existing_target(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    root = project_dir(tmp_path)
    target = root / "out.qgs"
    target.write_text("original", encoding="utf-8")
    loaded = FakeProject()
    ctx = context(loaded, roots=(root,))

    refused(
        TARGET_EXISTS,
        lambda: project.ProjectSave().execute_main(
            ctx, project.ProjectSaveParams(path=str(target))
        ),
    )
    assert loaded.writes == []
    assert target.read_text(encoding="utf-8") == "original"


def test_save_refuses_to_overwrite_the_associated_file(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # PRD 7.4: "even the current associated file" needs overwrite=true; without it
    # a plain save() of an already-saved project would silently replace that file.
    root = project_dir(tmp_path)
    target = root / "current.qgs"
    target.write_text("original", encoding="utf-8")
    loaded = FakeProject(file_name=str(target))
    ctx = context(loaded, roots=(root,))

    refused(
        TARGET_EXISTS,
        lambda: project.ProjectSave().execute_main(ctx, project.ProjectSaveParams()),
    )
    assert loaded.writes == []
    assert target.read_text(encoding="utf-8") == "original"


def test_save_writes_a_new_file(qgis: types.ModuleType, tmp_path: Path) -> None:
    root = project_dir(tmp_path)
    target = root / "out.qgz"
    loaded = FakeProject(dirty=True)
    ctx = context(loaded, roots=(root,))

    result = project.ProjectSave().execute_main(ctx, project.ProjectSaveParams(path=str(target)))

    assert Path(result.file_path) == target.resolve()
    assert loaded.writes == [str(target.resolve())]
    assert result.is_dirty is False


def test_save_overwrites_when_explicitly_allowed(qgis: types.ModuleType, tmp_path: Path) -> None:
    root = project_dir(tmp_path)
    target = root / "out.qgs"
    target.write_text("original", encoding="utf-8")
    loaded = FakeProject()
    ctx = context(loaded, roots=(root,))

    project.ProjectSave().execute_main(
        ctx, project.ProjectSaveParams(path=str(target), overwrite=True)
    )

    assert loaded.writes == [str(target.resolve())]


def test_save_falls_back_to_the_associated_file(qgis: types.ModuleType, tmp_path: Path) -> None:
    root = project_dir(tmp_path)
    target = root / "current.qgs"
    target.write_text("original", encoding="utf-8")
    loaded = FakeProject(file_name=str(target))
    ctx = context(loaded, roots=(root,))

    result = project.ProjectSave().execute_main(ctx, project.ProjectSaveParams(overwrite=True))

    assert Path(result.file_path) == target.resolve()


def test_save_refuses_a_target_outside_the_allowed_roots(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    root = project_dir(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    loaded = FakeProject()
    ctx = context(loaded, roots=(root,))

    refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectSave().execute_main(
            ctx, project.ProjectSaveParams(path=str(outside / "out.qgs"))
        ),
    )
    assert loaded.writes == []


def test_save_reports_a_failed_write(qgis: types.ModuleType, tmp_path: Path) -> None:
    root = project_dir(tmp_path)
    loaded = FakeProject(write_result=False)
    ctx = context(loaded, roots=(root,))

    failed = refused(
        PROJECT_SAVE_FAILED,
        lambda: project.ProjectSave().execute_main(
            ctx, project.ProjectSaveParams(path=str(root / "out.qgs"))
        ),
    )
    assert "partially written" in failed.message


# --- project.get_info -------------------------------------------------------


def test_get_info_reports_the_project_and_the_lifecycle_epoch() -> None:
    loaded = FakeProject(
        layers={"a": FakeLayer(), "b": FakeLayer()},
        file_name="C:/maps/current.qgs",
        dirty=True,
        crs_authid="EPSG:3857",
    )

    result = project.ProjectGetInfo().execute_main(
        context(loaded, epoch=7), project.ProjectGetInfoParams()
    )

    assert result.file_path == "C:/maps/current.qgs"
    assert result.crs_authid == "EPSG:3857"
    assert result.is_dirty is True
    assert result.layer_count == 2
    assert result.project_epoch == 7


def test_get_info_reports_no_file_for_an_unsaved_project() -> None:
    result = project.ProjectGetInfo().execute_main(
        context(FakeProject()), project.ProjectGetInfoParams()
    )

    assert result.file_path is None
    assert result.project_epoch == 0


def test_get_info_does_not_write_or_replace_anything() -> None:
    loaded = FakeProject(layers={"a": FakeLayer()}, dirty=True)
    iface = FakeIface(loaded)

    project.ProjectGetInfo().execute_main(
        context(loaded, iface=iface), project.ProjectGetInfoParams()
    )

    assert iface.new_project_calls == []
    assert loaded.writes == []


# --- declarations and the bare interpreter ----------------------------------


def test_the_four_operations_declare_the_prd_surface() -> None:
    registry = OperationRegistry()
    for operation in (
        project.ProjectCreate(),
        project.ProjectOpen(),
        project.ProjectSave(),
        project.ProjectGetInfo(),
    ):
        registry.register(operation)

    specs = {spec.name: spec for spec in registry.list_specs()}
    assert set(specs) == {"project.create", "project.open", "project.save", "project.get_info"}
    for name in ("project.create", "project.open", "project.save"):
        assert specs[name].kind is OperationKind.COMMAND
        assert specs[name].destructive is True
    info = specs["project.get_info"]
    assert info.kind is OperationKind.QUERY
    assert info.destructive is False
    assert {spec.strategy for spec in specs.values()} == {ExecutionStrategy.MAIN_THREAD}


def test_the_module_imports_without_qgis() -> None:
    guard = (
        "import sys\n"
        "from qgis_agent_plugin.operations import project\n"
        "leaked = sorted(n for n in sys.modules if n.split('.')[0] in "
        "{'qgis', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6'})\n"
        "assert not leaked, leaked\n"
    )
    env = {**os.environ}
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), env.get("PYTHONPATH", "")])
    result = subprocess.run(
        [sys.executable, "-c", guard], capture_output=True, text=True, env=env, cwd=ROOT
    )
    assert result.returncode == 0, result.stderr
