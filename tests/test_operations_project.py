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
import time
import types
import zipfile
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
    core.QgsProviderRegistry = FakeProviderRegistry
    package: Any = types.ModuleType("qgis")
    package.core = core
    monkeypatch.setitem(sys.modules, "qgis", package)
    monkeypatch.setitem(sys.modules, "qgis.core", core)
    return core


class FakeProviderMetadata:
    """Only what ``project.open`` asks: decode a provider URI into its path."""

    def decodeUri(self, uri: str) -> dict[str, str]:  # noqa: N802 - a PyQGIS name
        path = uri.split("|", 1)[0]
        return {"path": path} if path else {}


class FakeProviderRegistry:
    """``QgsProviderRegistry.instance().providerMetadata(id)``, reduced to two providers."""

    _instance: FakeProviderRegistry | None = None

    def __init__(self) -> None:
        self.metadata = {"ogr": FakeProviderMetadata(), "gdal": FakeProviderMetadata()}

    @classmethod
    def instance(cls) -> FakeProviderRegistry:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def providerMetadata(  # noqa: N802 - a PyQGIS name
        self, provider: str
    ) -> FakeProviderMetadata | None:
        return self.metadata.get(provider)


class FakeLayer:
    def __init__(self, *, valid: bool = True, source: str = "", provider: str = "ogr") -> None:
        self._valid = valid
        self._source = source
        self._provider = provider

    def isValid(self) -> bool:
        return self._valid

    def source(self) -> str:
        return self._source

    def providerType(self) -> str:
        return self._provider


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
        self.removed: list[str] = []
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

    def removeMapLayer(self, layer_id: str) -> None:  # noqa: N802 - a PyQGIS name
        self.removed.append(layer_id)
        self.layers.pop(layer_id, None)

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
    data_roots: tuple[Path, ...] | None = None,
    epoch: int = 0,
) -> OperationContext:
    return OperationContext(
        iface=iface or FakeIface(project_),
        project=project_,
        lifecycle=FakeLifecycle(epoch),
        policy=Settings(
            allowed_project_roots=roots,
            # Most cases allow one root for both keys; the project.open cases need
            # the data roots to be told apart from the project roots.
            allowed_data_roots=roots if data_roots is None else data_roots,
        ),
    )


def project_dir(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


REMOTE_SOURCE = "/vsicurl/http://127.0.0.1:9/never.tif"


def project_xml(datasource: str, *, tag: str = "datasource") -> str:
    """The smallest project XML QGIS writes around one layer's datasource."""

    return (
        "<!DOCTYPE qgis>\n<qgis><projectlayers><maplayer>"
        f"<{tag}>{datasource}</{tag}>"
        "</maplayer></projectlayers></qgis>"
    )


def qgs_file(directory: Path, name: str, datasource: str) -> Path:
    target = directory / name
    target.write_text(project_xml(datasource), encoding="utf-8")
    return target


def qgz_file(directory: Path, name: str, datasource: str) -> Path:
    """A ``.qgz`` is a zip whose ``.qgs`` member is the same XML."""

    return qgz_bytes(directory, name, project_xml(datasource).encode("utf-8"))


def qgz_bytes(directory: Path, name: str, member: bytes) -> Path:
    target = directory / name
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("project.qgs", member)
    return target


# The audit's measured shapes: the old scan spent 661 s on the second one.
UNCLOSED_DATASOURCES = 82_000
DEEP_NESTING = 8_000


def unclosed_project(directory: Path, name: str) -> Path:
    """A project that is nothing but an unclosed ``<datasource>`` repeated."""

    target = directory / name
    target.write_bytes(b"<qgis>" + b"<datasource>" * UNCLOSED_DATASOURCES)
    return target


def nested_project(directory: Path, name: str, depth: int) -> Path:
    """A well-formed project nested ``depth`` levels below its root element."""

    target = directory / name
    target.write_bytes(b"<qgis>" + b"<a>" * depth + b"</a>" * depth + b"</qgis>")
    return target


def encoded_project(directory: Path, name: str, datasource: str) -> Path:
    """The same XML in UTF-16, which QDomDocument reads and a UTF-8 text scan cannot."""

    member = b"\xff\xfe" + project_xml(datasource).encode("utf-16-le")
    if name.lower().endswith(".qgz"):
        return qgz_bytes(directory, name, member)
    target = directory / name
    target.write_bytes(member)
    return target


def set_encryption_flag(path: Path) -> None:
    """Mark the archive's one member as encrypted, which ``zipfile`` cannot write."""

    data = bytearray(path.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = data.find(signature)
        assert start >= 0, "the archive has no local or central header"
        flags = int.from_bytes(data[start + offset : start + offset + 2], "little") | 1
        data[start + offset : start + offset + 2] = flags.to_bytes(2, "little")
    path.write_bytes(bytes(data))


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
    save = project.ProjectSaveParams.model_validate({})
    assert save.path is None
    assert save.overwrite is False


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
    # The project is stateful and not authorized: the CRS is refused before the
    # replacement guard gets a say, which is the documented order (report §6.5).
    loaded = FakeProject(file_name="C:/maps/current.qgs")
    iface = FakeIface(loaded)
    ctx = context(loaded, iface=iface)

    refused(
        INVALID_CRS,
        lambda: project.ProjectCreate().execute_main(
            ctx, project.ProjectCreateParams(crs_authid="EPSG:9999")
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
    # The project holds a layer on purpose: the allow-list has to refuse the path
    # before the replacement guard gets a say, or a caller reads
    # PROJECT_REPLACE_REQUIRED for a path they may not use at all (AT-19).
    loaded = FakeProject(layers={"kept": FakeLayer()})
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


def test_open_refuses_to_reopen_the_associated_file_without_discarding(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # Reloading the project's own file still throws away the in-memory project, so
    # the associated file must not exempt the replacement guard (PRD 7.3, 10.3).
    root = project_dir(tmp_path)
    target = root / "current.qgs"
    target.write_text("", encoding="utf-8")
    loaded = FakeProject(file_name=str(target), dirty=True)
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
            "good": FakeLayer(source=str(root / "a.gpkg")),
            "also_good": FakeLayer(source=str(root / "a.gpkg")),
            "broken": FakeLayer(valid=False, source=str(root / "a.gpkg")),
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


def test_open_refuses_a_project_that_points_outside_the_allowed_roots(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # A project file names its layers' sources, so ``read()`` reaches them even
    # though every path the caller typed was allowed (PRD 10.2).
    root = project_dir(tmp_path)
    target = root / "leak.qgz"
    target.write_text("", encoding="utf-8")
    secret = tmp_path / "elsewhere" / "secret.gpkg"
    secret.parent.mkdir()
    secret.write_text("x", encoding="utf-8")
    loaded = FakeProject(
        layers_after_read={"leaked": FakeLayer(source=f"{secret}|layername=secret")}
    )
    ctx = context(loaded, roots=(root,))
    request = project.ProjectOpenParams(path=str(target))

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(ctx, request),
    )

    # The layer is not merely reported: it may not stay loaded either.
    assert loaded.removed == ["leaked"]
    assert loaded.layers == {}
    assert "1 layer data source(s)" in failed.message
    # ``read()`` already replaced the project, so the message may not claim it was
    # not opened: a caller reading that would keep working on a project that is gone.
    assert "was replaced" in failed.message
    assert "not opened" not in failed.message
    # The message never repeats the path it refused.
    assert "secret.gpkg" not in failed.message


def test_open_keeps_a_layer_whose_source_is_gone_but_inside_the_roots(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # AT-18B: a deleted source inside the roots is a partial load, not a breach of
    # the policy, so the existence of the source is deliberately not what is asked.
    root = project_dir(tmp_path)
    target = root / "a.qgz"
    target.write_text("", encoding="utf-8")
    loaded = FakeProject(
        layers_after_read={"gone": FakeLayer(valid=False, source=str(root / "gone.gpkg"))}
    )
    ctx = context(loaded, roots=(root,))

    result = project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert result.layer_count == 1
    assert result.invalid_layer_count == 1
    assert loaded.removed == []


def test_open_refuses_a_layer_whose_provider_names_no_local_path(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # A source nobody can decode is not a source this policy can vouch for.
    root = project_dir(tmp_path)
    target = root / "a.qgz"
    target.write_text("", encoding="utf-8")
    loaded = FakeProject(
        layers_after_read={"remote": FakeLayer(source="memory?geometry=Point", provider="memory")}
    )
    ctx = context(loaded, roots=(root,))
    request = project.ProjectOpenParams(path=str(target))

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(ctx, request),
    )

    assert loaded.removed == ["remote"]
    assert "1 layer data source(s)" in failed.message


def test_open_refuses_a_layer_whose_provider_cannot_decode_its_uri(
    qgis: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One provider's exception may not erase what the other layers already answered:
    # the failing layer counts as outside instead of turning the whole check into an
    # internal error that leaves every layer loaded.
    class ExplodingMetadata:
        def decodeUri(self, uri: str) -> dict[str, str]:  # noqa: N802 - a PyQGIS name
            raise RuntimeError("this provider cannot decode its own URI")

    root = project_dir(tmp_path)
    target = root / "a.qgz"
    target.write_text("", encoding="utf-8")
    monkeypatch.setitem(FakeProviderRegistry.instance().metadata, "boom", ExplodingMetadata())
    loaded = FakeProject(
        layers_after_read={
            "boom": FakeLayer(source="whatever", provider="boom"),
            "inside": FakeLayer(source=str(root / "a.gpkg")),
        }
    )
    ctx = context(loaded, roots=(root,))
    request = project.ProjectOpenParams(path=str(target))

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(ctx, request),
    )

    assert loaded.removed == ["boom"]
    assert loaded.layers.keys() == {"inside"}
    assert "1 layer data source(s)" in failed.message


def test_open_accepts_a_directory_shaped_source_inside_the_roots(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # An ESRI FileGDB is a directory, and a project holding one is an ordinary
    # project: the policy asks where it lives, not whether it is a regular file.
    root = project_dir(tmp_path)
    gdb = root / "roads.gdb"
    gdb.mkdir()
    target = qgs_file(root, "gdb.qgs", str(gdb))
    loaded = FakeProject(layers_after_read={"gdb": FakeLayer(source=str(gdb))})
    ctx = context(loaded, roots=(root,))

    result = project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert result.layer_count == 1
    assert loaded.removed == []


def test_open_refuses_a_directory_shaped_source_outside_the_roots(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    root = project_dir(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    gdb = elsewhere / "roads.gdb"
    gdb.mkdir()
    target = qgs_file(root, "gdb.qgs", str(gdb))
    loaded = FakeProject(layers_after_read={"gdb": FakeLayer(source=str(gdb))})
    ctx = context(loaded, roots=(root,))

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.removed == ["gdb"]
    assert "1 layer data source(s)" in failed.message


@pytest.mark.parametrize("suffix", [".qgs", ".qgz"])
@pytest.mark.parametrize(
    "remote",
    [
        REMOTE_SOURCE,
        r"\\no-such-host-audit\share\data.gpkg",
        "type=xyz&amp;url=https://tiles.invalid/{z}/{x}/{y}.png",
        # What QGIS's own XML parser decodes before it builds the layer.
        "&#47;vsicurl&#47;http&#58;&#47;&#47;127.0.0.1&#58;9/never.tif",
    ],
    ids=["vsi", "unc", "embedded-url", "entity-escaped"],
)
def test_open_refuses_a_remote_source_before_the_project_is_read(
    qgis: types.ModuleType, tmp_path: Path, suffix: str, remote: str
) -> None:
    # ``read()`` builds every layer the file names, so a remote URI there is an HTTP
    # request or an SMB authentication attempt before any per-layer check runs.
    root = project_dir(tmp_path)
    write = qgs_file if suffix == ".qgs" else qgz_file
    target = write(root, f"remote{suffix}", remote)
    loaded = FakeProject(layers_after_read={"never": FakeLayer(source=remote)})
    ctx = context(loaded, roots=(root,))

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    # The refusal has to come before ``read()``, which is what an unset read_path
    # proves: no layer was built and no datasource was contacted.
    assert loaded.read_path is None
    assert "remote data source" in failed.message
    assert remote not in failed.message


@pytest.mark.parametrize("suffix", [".qgs", ".qgz"])
def test_open_refuses_a_utf16_project_before_the_project_is_read(
    qgis: types.ModuleType, tmp_path: Path, suffix: str
) -> None:
    # QDomDocument reads a UTF-16 project; a scan that decodes the file as UTF-8 sees
    # a NUL between every character and matches nothing.  NUL bytes are the tell.
    root = project_dir(tmp_path)
    target = encoded_project(root, f"encoded{suffix}", REMOTE_SOURCE)
    loaded = FakeProject(layers_after_read={"never": FakeLayer(source=REMOTE_SOURCE)})
    ctx = context(loaded, roots=(root,))

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert "not UTF-8" in failed.message


@pytest.mark.parametrize("suffix", [".qgs", ".qgz"])
@pytest.mark.parametrize("tag", ["datasource ", "datasource\n"])
def test_open_sees_a_datasource_tag_with_whitespace(
    qgis: types.ModuleType, tmp_path: Path, suffix: str, tag: str
) -> None:
    # ``<datasource >`` is legal XML and the same element to QGIS; only a scanner
    # that insists on the exact spelling misses it.
    root = project_dir(tmp_path)
    xml = project_xml(REMOTE_SOURCE, tag=tag)
    if suffix == ".qgz":
        target = qgz_bytes(root, f"spaced{suffix}", xml.encode("utf-8"))
    else:
        target = root / f"spaced{suffix}"
        target.write_text(xml, encoding="utf-8")
    loaded = FakeProject(layers_after_read={"never": FakeLayer(source=REMOTE_SOURCE)})
    ctx = context(loaded, roots=(root,))

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert "remote data source" in failed.message


def test_a_qgs_over_the_scan_limit_is_refused_without_reading_it(
    qgis: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scan reads the whole file, so its size is the main thread's memory: a file
    # over the limit is refused before a byte of it is read.
    root = project_dir(tmp_path)
    target = root / "huge.qgs"
    with target.open("wb") as handle:
        handle.truncate(project.MAX_PROJECT_BYTES + 1)

    def read_bytes(self: Path) -> bytes:
        raise AssertionError("the project file was read before it was measured")

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    ctx = context(FakeProject(), roots=(root,))

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert "too large" in failed.message


def test_a_qgz_member_over_the_scan_limit_is_refused_before_it_is_unpacked(
    qgis: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A small archive can hold a member that expands to gigabytes; the declared
    # size is checked before the member is decompressed into memory.
    root = project_dir(tmp_path)
    target = root / "bomb.qgz"
    with (
        zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive,
        archive.open("project.qgs", "w") as member,
    ):
        member.write(b" " * (project.MAX_PROJECT_BYTES + 1))

    def read(self: zipfile.ZipFile, name: str) -> bytes:
        raise AssertionError(f"the archive member {name} was unpacked before it was measured")

    monkeypatch.setattr(zipfile.ZipFile, "read", read)
    ctx = context(FakeProject(), roots=(root,))

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert "too large" in failed.message


def test_a_qgz_over_the_total_scan_budget_is_refused_before_members_are_unpacked(
    qgis: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The limit used to be per member: 100 one-megabyte members passed it, were read
    # into a list, joined into a second copy and peaked at 200 MB (measured).  The
    # budget is the total, and it is checked before the first member is unpacked.
    root = project_dir(tmp_path)
    target = root / "many members.qgz"
    member = b" " * (1024 * 1024)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(project.MAX_PROJECT_BYTES // len(member) + 1):
            archive.writestr(f"part{index}.qgs", member)
    # Every member is under the limit on its own, and the archive is tiny on disk.
    assert target.stat().st_size < project.MAX_PROJECT_BYTES // 4

    def read(self: zipfile.ZipFile, name: str) -> bytes:
        raise AssertionError(f"the archive member {name} was unpacked before it was measured")

    monkeypatch.setattr(zipfile.ZipFile, "read", read)
    ctx = context(FakeProject(), roots=(root,))

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert "too large" in failed.message


def test_an_unclosed_datasource_project_is_refused_in_milliseconds(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # The regex this scan replaced searched from every ``<datasource`` to the end of
    # the file: 984 KB of them took 661 s on the main thread (measured) and matched
    # nothing.  The depth bound ends the scan on the sixty-fifth opening tag.
    root = project_dir(tmp_path)
    target = unclosed_project(root, "unclosed.qgs")
    assert target.stat().st_size > 900_000
    loaded = FakeProject()
    ctx = context(loaded, roots=(root,))

    started = time.monotonic()
    failed = refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )
    elapsed = time.monotonic() - started

    assert loaded.read_path is None
    assert "nests deeper" in failed.message
    assert elapsed < 0.5, f"the {target.stat().st_size} byte scan took {elapsed:.2f}s"


def test_a_deeply_nested_project_is_refused_before_it_is_read(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # ``QgsProject.read()`` parses recursively: 8000 levels (56 KB) overflowed the C++
    # stack and killed the process with 0xC00000FD, with no error code and no record
    # (measured).  The scan bounds the depth before ``read()`` is given the file.
    root = project_dir(tmp_path)
    target = nested_project(root, "deep.qgs", DEEP_NESTING)
    loaded = FakeProject()
    ctx = context(loaded, roots=(root,))

    failed = refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert "nests deeper" in failed.message
    assert "remote data source" not in failed.message


def test_a_project_nested_up_to_the_depth_bound_is_still_read(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # The bound may not turn an ordinary project into a failure: 64 levels is far
    # deeper than any project QGIS writes, and it is still accepted.
    root = project_dir(tmp_path)
    target = nested_project(root, "nested.qgs", project.MAX_PROJECT_DEPTH - 1)
    loaded = FakeProject()
    ctx = context(loaded, roots=(root,))

    project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert loaded.read_path == str(target)


def test_a_one_megabyte_flat_project_is_scanned_linearly(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # A file that passes the element bound has to be walked to its end; a single
    # linear pass means a megabyte of ordinary elements costs milliseconds, not a
    # rescan per element.
    root = project_dir(tmp_path)
    target = root / "flat.qgs"
    target.write_bytes(b"<qgis>" + b"<layer></layer>" * 69_915 + b"</qgis>")
    assert target.stat().st_size > 1024 * 1024
    assert target.read_bytes().count(b"<") < project.MAX_PROJECT_ELEMENTS
    loaded = FakeProject()
    ctx = context(loaded, roots=(root,))

    started = time.monotonic()
    project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))
    elapsed = time.monotonic() - started

    assert loaded.read_path == str(target)
    assert elapsed < 1.0, f"the {target.stat().st_size} byte scan took {elapsed:.2f}s"


def test_a_project_with_more_elements_than_the_scan_will_walk_is_refused(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # One Python step per element is the scan's cost, so tag spam that no QGIS project
    # looks like is refused after a single ``bytes.count``, not a second-long walk.
    root = project_dir(tmp_path)
    target = root / "spam.qgs"
    target.write_bytes(b"<qgis>" + b"<a></a>" * (project.MAX_PROJECT_ELEMENTS // 2 + 1))
    loaded = FakeProject()
    ctx = context(loaded, roots=(root,))

    started = time.monotonic()
    failed = refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )
    elapsed = time.monotonic() - started

    assert loaded.read_path is None
    assert "XML elements" in failed.message
    assert elapsed < 0.5, f"the {target.stat().st_size} byte scan took {elapsed:.2f}s"


def test_open_still_reads_a_project_whose_datasource_is_a_relative_path(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # QGIS stores relative paths by default, and a ``..`` inside one is resolved by
    # the path policy: neither is a remote source or a structural defect.
    root = project_dir(tmp_path)
    (root / "nested").mkdir()
    source = root / "nested" / ".." / "data.gpkg"
    target = qgs_file(root, "relative.qgs", "../data.gpkg")
    loaded = FakeProject(layers_after_read={"relative": FakeLayer(source=str(source))})
    ctx = context(loaded, roots=(root,))

    result = project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert result.layer_count == 1
    assert loaded.removed == []


def test_open_reports_an_encrypted_member_as_a_read_failure(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # ``zipfile`` raises RuntimeError for an encrypted member; a scan is not a new
    # failure source, so the file is still refused with ``read()``'s code,
    # PROJECT_OPEN_FAILED, not an INTERNAL_ERROR that reads like a plugin defect.
    # ``read()`` is not called: QGIS 3.44 crashes on such an archive (measured).
    root = project_dir(tmp_path)
    target = qgz_file(root, "locked.qgz", str(root / "a.gpkg"))
    set_encryption_flag(target)
    loaded = FakeProject(read_result=False)
    ctx = context(loaded, roots=(root,))

    failed = refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert "may have changed" in failed.message


@pytest.mark.parametrize("suffix", [".qgs", ".qgz"])
def test_open_keeps_a_layer_whose_source_directory_is_gone(
    qgis: types.ModuleType, tmp_path: Path, suffix: str
) -> None:
    # A2 #4's promise, for the case where the directory itself moved away: the source
    # is still inside the roots, so it is AT-18B (read, invalid_layer_count > 0), not
    # a breach that removes the layer and replaces the project.
    root = project_dir(tmp_path)
    source = root / "moved-away" / "cities.gpkg"
    target = (
        qgz_file(root, f"moved{suffix}", str(source))
        if suffix == ".qgz"
        else qgs_file(root, f"moved{suffix}", str(source))
    )
    loaded = FakeProject(layers_after_read={"moved": FakeLayer(valid=False, source=str(source))})
    ctx = context(loaded, roots=(root,))

    result = project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert result.layer_count == 1
    assert result.invalid_layer_count == 1
    assert loaded.removed == []


def test_open_still_reads_a_project_whose_sources_are_local(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    root = project_dir(tmp_path)
    target = qgs_file(root, "local.qgs", str(root / "a.gpkg"))
    loaded = FakeProject()
    ctx = context(loaded, roots=(root,))

    project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert loaded.read_path == str(target.resolve())


def test_open_reports_a_corrupt_archive_as_a_read_failure(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # The pre-read scan only reads bytes; a file it cannot unzip is still the
    # project file's own problem, reported by ``read()`` as PROJECT_OPEN_FAILED.
    root = project_dir(tmp_path)
    target = root / "corrupt.qgz"
    target.write_bytes(b"this is not zipped project data")
    loaded = FakeProject(read_result=False)
    ctx = context(loaded, roots=(root,))

    refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path == str(target.resolve())


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
    # The project already has a file on purpose: an explicit `path` has to win,
    # the associated file is only the fallback when `path` is null (PRD 7.4).
    loaded = FakeProject(file_name=str(root / "current.qgs"))
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
    # The target exists on purpose: the allow-list has to refuse the path before
    # the overwrite guard runs, otherwise existence outside the roots leaks
    # through TARGET_EXISTS (AT-19).
    (outside / "out.qgs").write_text("keep", encoding="utf-8")
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
