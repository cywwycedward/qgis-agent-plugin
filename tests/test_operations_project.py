"""用假 ctx 和假 PyQGIS 驱动 ``project.*`` 操作。

这些是单元层的验收点：读不出来的输入、工程替换时的守卫、路径白名单、覆盖规则，以及
部分加载时的结果计数。真实 PyQGIS 的行为（``read()`` 到底有没有往图层树里填东西）留给
QGIS 侧的验收跑，不在这里。

The ``project.*`` operations, driven by a fake ctx and a fake PyQGIS.  These are the
unit-level acceptance points: unreadable input, the replacement guard, the path policy,
the overwrite rule and the partial-load count.  Real PyQGIS behaviour (whether ``read()``
actually populates the layer tree) belongs to the QGIS acceptance run, not here.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import tracemalloc
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
# 绝对路径且格式正确，从不真的碰它。
# 只有输入模型会看到它。
# Absolute and well formed, never touched.
# Only the input models see it.
A_PROJECT_FILE = str(ROOT / "maps" / "a.qgs")


class FakeCrs:
    """只对真实 QGIS 能解析的 authid 有效，别的都不算。

    Valid exactly for the authids a real QGIS resolves; anything else is not.
    """

    _KNOWN = frozenset({"EPSG:4326", "EPSG:3857"})

    def __init__(self, authid: str) -> None:
        self._authid = authid

    def isValid(self) -> bool:
        return self._authid.upper() in self._KNOWN

    def authid(self) -> str:
        """QGIS 报的是规范形式，不管调用方用的是哪个别名。

        QGIS reports the canonical form whatever alias the caller used.
        """

        return self._authid.upper()


@pytest.fixture
def qgis(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """操作在方法体内 import ``qgis.core``；这个假模块就是那一步要的东西。

    The operations import ``qgis.core`` inside their method body; this is that module.
    """

    core: Any = types.ModuleType("qgis.core")
    core.QgsCoordinateReferenceSystem = FakeCrs
    core.QgsProviderRegistry = FakeProviderRegistry
    package: Any = types.ModuleType("qgis")
    package.core = core
    monkeypatch.setitem(sys.modules, "qgis", package)
    monkeypatch.setitem(sys.modules, "qgis.core", core)
    return core


class FakeProviderMetadata:
    """只实现 ``project.open`` 用得上的那一步：把 provider URI 拆出路径。

    Only what ``project.open`` asks: decode a provider URI into its path.
    """

    def decodeUri(self, uri: str) -> dict[str, str]:  # noqa: N802 - a PyQGIS name
        path = uri.split("|", 1)[0]
        return {"path": path} if path else {}


class FakeProviderRegistry:
    """``QgsProviderRegistry.instance().providerMetadata(id)``，只留两个 provider。

    ``QgsProviderRegistry.instance().providerMetadata(id)``, reduced to two providers.
    """

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
            # 真实的新工程是空的。
            # 所以计数从它读。
            # A real new project starts empty.
            # So the counts are read from it.
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
    epoch: int = 0,
) -> OperationContext:
    return OperationContext(
        iface=iface or FakeIface(project_),
        project=project_,
        lifecycle=FakeLifecycle(epoch),
        policy=Settings(),
    )


def project_dir(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


REMOTE_SOURCE = "/vsicurl/http://127.0.0.1:9/never.tif"


def project_xml(datasource: str, *, tag: str = "datasource") -> str:
    """QGIS 为一个图层的 datasource 写出的最小工程 XML。

    The smallest project XML QGIS writes around one layer's datasource.
    """

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
    """``.qgz`` 就是一个 zip，里面的 ``.qgs`` 成员是同一份 XML。

    A ``.qgz`` is a zip whose ``.qgs`` member is the same XML.
    """

    return qgz_bytes(directory, name, project_xml(datasource).encode("utf-8"))


def qgz_bytes(directory: Path, name: str, member: bytes) -> Path:
    target = directory / name
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("project.qgs", member)
    return target


def write_project_xml(directory: Path, name: str, xml: str) -> Path:
    """把 ``xml`` 写成 ``.qgs``，或写进 ``.qgz`` 的 ``.qgs`` 成员。

    Write ``xml`` as a ``.qgs``, or as the ``.qgs`` member of a ``.qgz``.
    """

    if name.lower().endswith(".qgz"):
        return qgz_bytes(directory, name, xml.encode("utf-8"))
    target = directory / name
    target.write_text(xml, encoding="utf-8")
    return target


def layout_project(source: Path, item: str) -> str:
    """一个带本地图层和一个打印布局的工程，布局里放着 ``item``。

    A project with one local layer and one print layout holding ``item``.
    """

    return (
        "<!DOCTYPE qgis>\n<qgis><projectlayers><maplayer>"
        f"<datasource>{source}</datasource>"
        "</maplayer></projectlayers>"
        f'<Layouts><Layout name="sheet">{item}</Layout></Layouts>'
        "</qgis>"
    )


# 审计量到的形状：旧扫描在第二个上花了 661 秒。
# The old scan needed 661 s for the second.
UNCLOSED_DATASOURCES = 82_000
DEEP_NESTING = 8_000


def unclosed_project(directory: Path, name: str) -> Path:
    """一个除了重复的未闭合 ``<datasource>`` 之外什么都没有的工程。

    A project that is nothing but an unclosed ``<datasource>`` repeated.
    """

    target = directory / name
    target.write_bytes(b"<qgis>" + b"<datasource>" * UNCLOSED_DATASOURCES)
    return target


def nested_project(directory: Path, name: str, depth: int) -> Path:
    """一个良构的工程，根元素下面嵌了 ``depth`` 层。

    A well-formed project nested ``depth`` levels below its root element.
    """

    target = directory / name
    target.write_bytes(b"<qgis>" + b"<a>" * depth + b"</a>" * depth + b"</qgis>")
    return target


def encoded_project(directory: Path, name: str, datasource: str) -> Path:
    """同样的 XML 用 UTF-16 写：QDomDocument 读得懂，UTF-8 文本扫描读不懂。

    The same XML in UTF-16, which QDomDocument reads and a UTF-8 text scan cannot.
    """

    member = b"\xff\xfe" + project_xml(datasource).encode("utf-16-le")
    if name.lower().endswith(".qgz"):
        return qgz_bytes(directory, name, member)
    target = directory / name
    target.write_bytes(member)
    return target


def set_encryption_flag(path: Path, member: str | None = None) -> None:
    """把一个归档成员（默认第一个）标成加密。

    ``zipfile`` 写不了加密成员，所以直接给普通归档打上通用标志位；解压时就会抛
    ``RuntimeError``，与真的密码保护成员一样。

    Mark one archive member (the first by default) as encrypted.  ``zipfile`` cannot
    write an encrypted member, so the general purpose flag is set on an otherwise plain
    archive; extracting that member then raises RuntimeError, exactly as a
    password-protected member does.
    """

    data = bytearray(path.read_bytes())
    patched = 0
    for signature, flag_at, length_at, name_at in (
        (b"PK\x03\x04", 6, 26, 30),
        (b"PK\x01\x02", 8, 28, 46),
    ):
        cursor = 0
        while (start := data.find(signature, cursor)) >= 0:
            cursor = start + 1
            length = int.from_bytes(data[start + length_at : start + length_at + 2], "little")
            name = bytes(data[start + name_at : start + name_at + length]).decode("utf-8")
            if member is not None and name != member:
                continue
            flags = int.from_bytes(data[start + flag_at : start + flag_at + 2], "little") | 1
            data[start + flag_at : start + flag_at + 2] = flags.to_bytes(2, "little")
            patched += 1
            if member is None:
                break
    assert patched >= 2, f"could not patch the headers of {member!r}"
    path.write_bytes(bytes(data))


def lie_about_member_size(path: Path, size: int) -> None:
    """把中央目录里成员的尺寸改小。

    预算读的是目录；成员本身没动，所以 ``archive.read()`` 照样把它整个展开（实测峰值
    437 MiB）。

    Shrink a member's size in the central directory.  The directory is what a budget
    reads; the member itself is untouched, so ``archive.read()`` still inflates all of
    it (437 MiB peak measured).
    """

    data = bytearray(path.read_bytes())
    start = data.find(b"PK\x01\x02")
    assert start >= 0, "the archive has no central header"
    data[start + 24 : start + 28] = size.to_bytes(4, "little")
    path.write_bytes(bytes(data))


def refused(code: str, call: Any) -> FrameworkError:
    with pytest.raises(FrameworkError) as caught:
        call()
    assert caught.value.code == code
    return caught.value


# --- 输入模型 / input models -----------------------------------------------


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
    # 合法载荷先单独跑一遍。
    # 这样拒绝只能来自未知字段。
    # The valid payload is checked first.
    # So the refusal can only come from the extra key.
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


# --- 输出模型 / output models ----------------------------------------------


@pytest.mark.parametrize(
    "model, fields",
    [
        (project.ProjectCreateResult, ["crs_authid", "file_path", "layer_count"]),
        (
            project.ProjectOpenResult,
            ["file_path", "crs_authid", "layer_count", "invalid_layer_count"],
        ),
        (project.ProjectSaveResult, ["file_path", "is_dirty"]),
        (
            project.ProjectGetInfoResult,
            ["file_path", "crs_authid", "is_dirty", "layer_count", "project_epoch"],
        ),
    ],
    ids=["create", "open", "save", "get_info"],
)
def test_a_result_carries_exactly_the_prd_output_fields(model: Any, fields: list[str]) -> None:
    # 记录的键就是契约。
    # 多一个字段会流到每个调用方。
    # Every record's keys are the contract.
    # An extra field reaches every caller.
    assert list(model.model_fields) == fields


def test_a_result_rejects_an_undeclared_field() -> None:
    # 没有 ``extra="forbid"`` 会丢掉拼错的键。
    # 换成字段自己的默认值。
    # 于是调用方读到一个谁也没产出的值。
    # With no such flag the key is dropped.
    # It substitutes the field's own default.
    # The caller then reads a value nobody produced.
    with pytest.raises(ValidationError):
        project.ProjectCreateResult(crs_authid="EPSG:4326", layer_counts=5)


# --- project.create：新建工程 / creating a project -------------------------


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
    # QGIS 的工程模板会往空工程里填图层。
    # 计数从工程里读，不写死成零。
    # A template can populate a brand new project.
    # The counts come from the project, not from zero.
    template = FakeProject(layers={"a": FakeLayer(), "b": FakeLayer()})
    ctx = context(template, iface=FakeIface())

    result = project.ProjectCreate().execute_main(
        ctx, project.ProjectCreateParams(discard_current=True)
    )

    assert result.layer_count == 2
    assert result.file_path is None


def test_create_refuses_an_unusable_crs_before_replacing(qgis: types.ModuleType) -> None:
    # 工程里有内容且没被授权。
    # CRS 先于替换守卫被拒。
    # A stateful project without authorization.
    # The CRS is refused before the guard.
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


# --- project.open：打开工程 / opening a project ----------------------------


def test_open_refuses_to_replace_a_project_with_content(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    root = project_dir(tmp_path)
    target = root / "a.qgs"
    target.write_text("", encoding="utf-8")
    loaded = FakeProject(dirty=True)
    ctx = context(loaded)
    request = project.ProjectOpenParams(path=str(target))

    refused(PROJECT_REPLACE_REQUIRED, lambda: project.ProjectOpen().execute_main(ctx, request))
    assert loaded.read_path is None


def test_open_refuses_to_reopen_the_associated_file_without_discarding(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 重载工程自己的文件也会丢掉内存里的工程。
    # 所以关联文件不能豁免替换守卫。
    # Reloading its own file still discards it.
    # So the associated file cannot exempt the guard.
    root = project_dir(tmp_path)
    target = root / "current.qgs"
    target.write_text("", encoding="utf-8")
    loaded = FakeProject(file_name=str(target), dirty=True)
    ctx = context(loaded)
    request = project.ProjectOpenParams(path=str(target))

    refused(PROJECT_REPLACE_REQUIRED, lambda: project.ProjectOpen().execute_main(ctx, request))
    assert loaded.read_path is None


def test_open_reports_a_failed_read(qgis: types.ModuleType, tmp_path: Path) -> None:
    root = project_dir(tmp_path)
    target = root / "a.qgs"
    target.write_text("not a project", encoding="utf-8")
    loaded = FakeProject(read_result=False)
    ctx = context(loaded)
    request = project.ProjectOpenParams(path=str(target))

    failed = refused(PROJECT_OPEN_FAILED, lambda: project.ProjectOpen().execute_main(ctx, request))
    assert "may have changed" in failed.message


def test_open_replaces_a_stateful_project_when_explicitly_allowed(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # open 与 create 共用同一道守卫。
    # 所以被授权的分支也在这里跑。
    # `open` and `create` share one guard.
    # So the authorized branch runs here too.
    root = project_dir(tmp_path)
    target = qgs_file(root, "a.qgs", str(root / "a.gpkg"))
    loaded = FakeProject(layers={"old": FakeLayer()}, file_name="C:/maps/old.qgs", dirty=True)
    ctx = context(loaded)

    result = project.ProjectOpen().execute_main(
        ctx, project.ProjectOpenParams(path=str(target), discard_current=True)
    )

    assert loaded.read_path == str(target.resolve())
    assert loaded.file_name == str(target.resolve())
    assert result.layer_count == 0


def test_open_reports_the_path_it_was_given(qgis: types.ModuleType, tmp_path: Path) -> None:
    # 路径只过绝对路径检查，不解析：``sub/..`` 原样传到 QGIS 与输出。
    # The path is checked for being absolute and not resolved: ``sub/..`` reaches QGIS and the
    # output as it arrived.
    root = project_dir(tmp_path)
    (root / "sub").mkdir()
    target = root / "a.qgz"
    target.write_text("", encoding="utf-8")
    loaded = FakeProject()
    ctx = context(loaded)
    raw = str(root / "sub" / ".." / "a.qgz")

    result = project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=raw))

    assert loaded.read_path == str(Path(raw))
    assert result.file_path == str(Path(raw))


def test_open_succeeds_and_counts_the_layers_that_cannot_load(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    root = project_dir(tmp_path)
    target = root / "a.qgz"
    target.write_text("", encoding="utf-8")
    loaded = FakeProject(
        read_result=True,
        # 两个能用的图层加一个坏的。
        # 数错一侧当然过不了。
        # Two usable layers and one broken one.
        # Counting the wrong side cannot pass.
        layers_after_read={
            "good": FakeLayer(source=str(root / "a.gpkg")),
            "also_good": FakeLayer(source=str(root / "a.gpkg")),
            "broken": FakeLayer(valid=False, source=str(root / "a.gpkg")),
        },
        crs_authid="EPSG:3857",
    )
    ctx = context(loaded)

    result = project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert Path(result.file_path) == target.resolve()
    assert loaded.read_path == str(target.resolve())
    assert result.layer_count == 3
    assert result.invalid_layer_count == 1
    assert result.crs_authid == "EPSG:3857"


def test_open_keeps_a_layer_whose_source_is_gone(qgis: types.ModuleType, tmp_path: Path) -> None:
    # 根目录内被删掉的数据源只是部分加载。
    # 不算越界。
    # 所以这里刻意不问数据源是否存在。
    # A deleted source inside the roots is a partial load.
    # Not a breach of the policy.
    # Nothing here asks whether it exists.
    root = project_dir(tmp_path)
    target = root / "a.qgz"
    target.write_text("", encoding="utf-8")
    loaded = FakeProject(
        layers_after_read={"gone": FakeLayer(valid=False, source=str(root / "gone.gpkg"))}
    )
    ctx = context(loaded)

    result = project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert result.layer_count == 1
    assert result.invalid_layer_count == 1
    assert loaded.removed == []


@pytest.mark.parametrize(
    "spelling",
    ["GTIFF_DIR:1:{path}", 'NETCDF:"{path}":temperature'],
    ids=["gtiif-dir", "netcdf"],
)
def test_open_reads_a_project_whose_layer_is_a_local_subdataset(
    qgis: types.ModuleType, tmp_path: Path, spelling: str
) -> None:
    # GDAL 的栅格 provider 不拆子数据集。
    # decodeUri 把整个写法当成 "path"。
    # 所以白名单被问的是一个写法。
    # 而这个工程点到的文件全在根内。
    # GDAL's raster provider does not split a subdataset.
    # decodeUri answers the whole spelling as the path.
    # So the allow-list was asked about a spelling.
    # Though every file it names is inside.
    root = project_dir(tmp_path)
    source = spelling.format(path=root / "pages.tif")
    target = qgs_file(root, "subdataset.qgs", source)
    loaded = FakeProject(layers_after_read={"pages": FakeLayer(source=source, provider="gdal")})
    ctx = context(loaded)

    result = project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert result.layer_count == 1
    assert loaded.removed == []


@pytest.mark.parametrize("suffix", [".qgs", ".qgz"])
@pytest.mark.parametrize(
    "remote",
    [
        REMOTE_SOURCE,
        r"\\no-such-host-audit\share\data.gpkg",
        # QGIS 造图层前就会解出它。
        # QGIS decodes it before it builds the layer.
        "&#47;vsicurl&#47;http&#58;&#47;&#47;127.0.0.1&#58;9/never.tif",
    ],
    ids=["vsi", "unc", "entity-escaped"],
)
def test_open_refuses_a_remote_source_before_the_project_is_read(
    qgis: types.ModuleType, tmp_path: Path, suffix: str, remote: str
) -> None:
    # ``read()`` 会造出文件里点到的每个图层。
    # 所以那里的远程 URI 会提前发请求。
    # 在任何逐层检查之前。
    # ``read()`` builds every layer named.
    # So a remote URI there is reached first.
    # Before any per-layer check runs.
    root = project_dir(tmp_path)
    write = qgs_file if suffix == ".qgs" else qgz_file
    target = write(root, f"remote{suffix}", remote)
    loaded = FakeProject(layers_after_read={"never": FakeLayer(source=remote)})
    ctx = context(loaded)

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    # 拒绝必须发生在 ``read()`` 之前。
    # ``read_path`` 没被写就是凭据。
    # 没有图层被造出来。
    # 也没有数据源被连上。
    # The refusal comes before ``read()``.
    # An unset read_path is the proof.
    # No layer was built.
    # No datasource was contacted.
    assert loaded.read_path is None
    assert "remote data source" in failed.message
    assert remote not in failed.message


@pytest.mark.parametrize(
    "source",
    [
        "type=xyz&amp;url=https://tiles.invalid/{z}/{x}/{y}.png",
        'NETCDF:"/vsicurl/http://127.0.0.1:9/never.nc":temperature',
        "GTIFF_DIR:1:/vsicurl/http://127.0.0.1:9/never.tif",
        # 标记落在扫描保留的 64 个字符之后。
        # 只看开头的规则看不见它。
        # 只有“包含”规则能看见。
        # The marker sits past the 64 kept characters.
        # The start rule misses it.
        # Only the containment rule can see it.
        'NETCDF:"C:/' + "dataset/" * 10 + 'http://127.0.0.1:9/never.nc":temperature',
        # 标记还被拆进了子元素。
        # 标志位跟随 text() 的顺序，含子元素。
        # The marker is split across a child element.
        # The flag follows the text() order.
        'NETCDF:"C:/' + "dataset/" * 10 + 'htt<x/>p://127.0.0.1:9/never.nc":temperature',
    ],
    ids=["xyz-url", "netcdf-url", "gtiif-dir-url", "marker-past-the-head", "marker-split"],
)
def test_open_refuses_a_datasource_that_carries_a_url(
    qgis: types.ModuleType, tmp_path: Path, source: str
) -> None:
    # 远程文件也能藏在值的中间。
    # GDAL 自己就会打开 ``NETCDF:"/vsicurl/http://…"``。
    # 5 KB 的工程实测发了 5 个连接。
    # 包含规则只问 ``<datasource>`` 的值。
    # 所以下面的散文用例仍能打开。
    # A remote file can hide inside the value.
    # GDAL opens that spelling itself.
    # A 5 KB project sent 5 connections, measured.
    # The containment rule asks the datasource value only.
    # So the prose case below still opens.
    root = project_dir(tmp_path)
    target = qgs_file(root, "tiles.qgs", source)
    loaded = FakeProject(layers_after_read={"tiles": FakeLayer(source=source)})
    ctx = context(loaded)

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert loaded.removed == []
    assert "remote data source" in failed.message
    assert "127.0.0.1" not in failed.message


@pytest.mark.parametrize("suffix", [".qgs", ".qgz"])
def test_open_refuses_a_utf16_project_before_the_project_is_read(
    qgis: types.ModuleType, tmp_path: Path, suffix: str
) -> None:
    # QDomDocument 能读 UTF-16 工程。
    # 按 UTF-8 解码的扫描只看到 NUL。
    # 于是什么都匹配不到。
    # NUL 字节就是线索。
    # QDomDocument reads a UTF-16 project.
    # A UTF-8 scan sees only NUL bytes.
    # So it matches nothing.
    # NUL bytes are the tell.
    root = project_dir(tmp_path)
    target = encoded_project(root, f"encoded{suffix}", REMOTE_SOURCE)
    loaded = FakeProject(layers_after_read={"never": FakeLayer(source=REMOTE_SOURCE)})
    ctx = context(loaded)

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
    # ``<datasource >`` 是合法 XML。
    # 对 QGIS 是同一个元素。
    # 只有死认拼写的扫描器才看不见它。
    # ``<datasource >`` is legal XML.
    # The same element to QGIS.
    # Only a scanner insisting on the spelling misses it.
    root = project_dir(tmp_path)
    xml = project_xml(REMOTE_SOURCE, tag=tag)
    if suffix == ".qgz":
        target = qgz_bytes(root, f"spaced{suffix}", xml.encode("utf-8"))
    else:
        target = root / f"spaced{suffix}"
        target.write_text(xml, encoding="utf-8")
    loaded = FakeProject(layers_after_read={"never": FakeLayer(source=REMOTE_SOURCE)})
    ctx = context(loaded)

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
    # 扫描要读整个文件。
    # 所以它的尺寸就是主线程的内存。
    # 超限的文件在读到第一个字节之前就被拒。
    # The scan reads the whole file.
    # Its size is the main thread's memory.
    # An over-limit file is refused before a byte is read.
    root = project_dir(tmp_path)
    target = root / "huge.qgs"
    with target.open("wb") as handle:
        handle.truncate(project.MAX_PROJECT_BYTES + 1)

    def read_bytes(self: Path) -> bytes:
        raise AssertionError("the project file was read before it was measured")

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    ctx = context(FakeProject())

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert "too large" in failed.message


def test_a_qgz_member_over_the_scan_limit_is_refused_while_it_is_unpacked(
    qgis: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 小归档里能放一个解压后上 GB 的成员。
    # 预算按解压出的字节数计。
    # 到限额就停。
    # 所以不必整块持有。
    # A small archive can hold a gigabyte member.
    # The budget counts decompressed bytes.
    # It stops at the limit.
    # So the member is never held whole.
    root = project_dir(tmp_path)
    target = root / "bomb.qgz"
    with (
        zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive,
        archive.open("project.qgs", "w") as member,
    ):
        member.write(b" " * (project.MAX_PROJECT_BYTES + 1))

    unpacked: list[int] = []
    original = zipfile.ZipExtFile.read

    def counting(self: zipfile.ZipExtFile, size: int = -1) -> bytes:
        data = original(self, size)
        unpacked.append(len(data))
        return data

    monkeypatch.setattr(zipfile.ZipExtFile, "read", counting)
    ctx = context(FakeProject())

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert "too large" in failed.message
    assert sum(unpacked) <= project.MAX_PROJECT_BYTES + project._CHUNK


def test_a_qgz_over_the_total_scan_budget_is_refused_before_members_are_unpacked(
    qgis: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 限额曾经按成员算。
    # 100 个 1 MB 成员全通过。
    # 峰值 200 MB（实测）。
    # 现在预算算总数。
    # 并在解开第一个成员之前检查。
    # The limit used to be per member.
    # 100 one-megabyte members all passed.
    # Peak was 200 MB, measured.
    # The budget is the total now.
    # Checked before the first member is unpacked.
    root = project_dir(tmp_path)
    target = root / "many members.qgz"
    member = b" " * (1024 * 1024)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(project.MAX_PROJECT_BYTES // len(member) + 1):
            archive.writestr(f"part{index}.qgs", member)
    # 每个成员单看都在限额内。
    # 归档在磁盘上也很小。
    # Every member is under the limit on its own.
    # The archive is tiny on disk too.
    assert target.stat().st_size < project.MAX_PROJECT_BYTES // 4

    def read(self: zipfile.ZipFile, name: str) -> bytes:
        raise AssertionError(f"the archive member {name} was unpacked before it was measured")

    monkeypatch.setattr(zipfile.ZipFile, "read", read)
    ctx = context(FakeProject())

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
    # 被取代的正则从每个 ``<datasource`` 搜到文件末尾。
    # 984 KB 这样的输入花了 661 秒（实测）。
    # 而且一个都没匹配上。
    # 深度上限在第 65 个开标签处结束扫描。
    # The old regex searched to the end.
    # 984 KB of them took 661 s, measured.
    # And nothing matched.
    # The depth bound ends the scan on tag 65.
    root = project_dir(tmp_path)
    target = unclosed_project(root, "unclosed.qgs")
    assert target.stat().st_size > 900_000
    loaded = FakeProject()
    ctx = context(loaded)

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
    # ``QgsProject.read()`` 是递归解析。
    # 8000 层（56 KB）就把 C++ 栈压爆。
    # 进程直接死掉，没有记录。
    # 所以扫描在交给 ``read()`` 之前限住深度。
    # ``QgsProject.read()`` parses recursively.
    # 8000 levels (56 KB) overflowed the C++ stack.
    # The process died outright, with no record.
    # So the depth is bounded before read().
    root = project_dir(tmp_path)
    target = nested_project(root, "deep.qgs", DEEP_NESTING)
    loaded = FakeProject()
    ctx = context(loaded)

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
    # 上限不能把普通工程变成失败。
    # QGIS 自己写出的最深工程是 105 层的 100 组树。
    # 上限是 256。
    # The bound may not fail an ordinary project.
    # The deepest real tree is 105.
    # The bound is 256.
    root = project_dir(tmp_path)
    target = nested_project(root, "nested.qgs", project.MAX_PROJECT_DEPTH - 1)
    loaded = FakeProject()
    ctx = context(loaded)

    project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert loaded.read_path == str(target)


def test_a_one_megabyte_flat_project_is_scanned_linearly(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 过了标签上限的文件必须被解析到底。
    # 线性解析意味着 1 MB 普通元素只要几毫秒。
    # 而不是每层再扫一遍。
    # A file past the tag bound is parsed to its end.
    # One linear parse costs milliseconds.
    # Not a rescan per level.
    root = project_dir(tmp_path)
    target = root / "flat.qgs"
    target.write_bytes(b"<qgis>" + b"<layer></layer>" * 69_915 + b"</qgis>")
    assert target.stat().st_size > 1024 * 1024
    assert target.read_bytes().count(b"<") < project.MAX_PROJECT_TAGS
    loaded = FakeProject()
    ctx = context(loaded)

    started = time.monotonic()
    project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))
    elapsed = time.monotonic() - started

    assert loaded.read_path == str(target)
    assert elapsed < 1.0, f"the {target.stat().st_size} byte scan took {elapsed:.2f}s"


def test_a_project_with_more_tags_than_the_scan_will_parse_is_refused(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 解析每个元素一步。
    # 标签垃圾在一次 ``bytes.count`` 后就被拒。
    # The parse costs one step per element.
    # Tag spam is refused after one ``bytes.count``.
    root = project_dir(tmp_path)
    target = root / "spam.qgs"
    target.write_bytes(b"<qgis>" + b"<a></a>" * (project.MAX_PROJECT_TAGS // 2 + 1))
    loaded = FakeProject()
    ctx = context(loaded)

    started = time.monotonic()
    failed = refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )
    elapsed = time.monotonic() - started

    assert loaded.read_path is None
    assert "XML tags" in failed.message
    assert elapsed < 0.5, f"the {target.stat().st_size} byte scan took {elapsed:.2f}s"


# 这些 XML 形状骗过四套扫描器。
# 每种都真的嵌了 ``HIDDEN_DEPTH`` 层。
# 且都是合法 XML。
# CDATA 或注释里的假结束标签不算标签。
# 引号属性里的裸 ">" 不结束标签。
# 实测：184 KB 被放行，read() 随后死于栈溢出。
# These XML shapes fooled four byte scanners.
# Each one really nests HIDDEN_DEPTH elements.
# All of them are legal XML.
# A fake close tag in CDATA is no tag.
# A bare ">" inside a quoted attribute ends nothing.
# Measured: 184 KB passed, read() then crashed.
HIDDEN_DEPTH = 2000
DEPTH_SHAPES = {
    "cdata": (b"<a><![CDATA[</q>]]>", b"</a>"),
    "comment": (b"<a><!-- </q> -->", b"</a>"),
    "attribute": (b'<a x="/>">', b"</a>"),
}

# 两个 QGIS 3.44 标签，属性值里有裸 ">"。
# 一个是正方向符号。
# 另一个是标注的线符号。
# 它那串转义过的 XML 就是一个属性值。
# 这是在一个带标注的图层上实测到的。
# Two QGIS 3.44 tags hold a bare ">".
# One is the right-direction symbol.
# The other is a label's line symbol.
# Its escaped XML is one attribute value.
# Measured on a project with one labelled layer.
QGIS_LABEL_TAGS = (
    b'<text-format wrapChar="" rightDirectionSymbol=">"/>',
    b'<Option type="QString" name="lineSymbol" value="&lt;symbol type=&quot;line&quot; '
    b'alpha=&quot;1&quot;>&lt;/symbol>"/>',
)


@pytest.mark.parametrize("shape", sorted(DEPTH_SHAPES))
@pytest.mark.parametrize("suffix", [".qgs", ".qgz"])
def test_open_refuses_depth_hidden_by_cdata_a_comment_or_an_attribute(
    qgis: types.ModuleType, tmp_path: Path, suffix: str, shape: str
) -> None:
    # 旧计数器按原始字节 +1/-1。
    # CDATA 或注释里的假 </q> 把它压下去。
    # 所以 184 KB、8000 层的真嵌套被放行。
    # read() 压爆栈（实测）。
    # The old counter was +1/-1 over the raw bytes.
    # A fake close tag in CDATA presses it down.
    # So 184 KB of real 8000-level nesting passed.
    # read() then overflowed the stack, measured.
    opening, closing = DEPTH_SHAPES[shape]
    body = b"<qgis>" + opening * HIDDEN_DEPTH + closing * HIDDEN_DEPTH + b"</qgis>"
    root = project_dir(tmp_path)
    if suffix == ".qgz":
        target = qgz_bytes(root, f"hidden{suffix}", body)
    else:
        target = root / f"hidden{suffix}"
        target.write_bytes(body)
    loaded = FakeProject()
    ctx = context(loaded)

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


@pytest.mark.parametrize("suffix", [".qgs", ".qgz"])
@pytest.mark.parametrize(
    "remote",
    [REMOTE_SOURCE, r"\\no-such-host-audit\share\data.gpkg"],
    ids=["vsi", "unc"],
)
def test_open_sees_a_datasource_whose_attribute_hides_its_tag_end(
    qgis: types.ModuleType, tmp_path: Path, suffix: str, remote: str
) -> None:
    # ``<datasource x="/>" ...>`` 是合法 XML。
    # 第一个 ">" 在属性里。
    # 旧扫描把它当自闭合标签。
    # read() 就够到了 /vsicurl 源。
    # 实测 0.08 秒 9 个连接。
    # That spelling is legal XML.
    # The first ">" is inside the attribute.
    # The old scan took it for a self-closing tag.
    # So read() reached the /vsicurl source.
    # 9 connections in 0.08 s, measured.
    xml = (
        "<!DOCTYPE qgis>\n<qgis><projectlayers><maplayer>"
        f'<datasource x="/>">{remote}</datasource>'
        "</maplayer></projectlayers></qgis>"
    )
    root = project_dir(tmp_path)
    if suffix == ".qgz":
        target = qgz_bytes(root, f"hidden{suffix}", xml.encode("utf-8"))
    else:
        target = root / f"hidden{suffix}"
        target.write_text(xml, encoding="utf-8")
    loaded = FakeProject(layers_after_read={"never": FakeLayer(source=remote)})
    ctx = context(loaded)

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert "remote data source" in failed.message
    assert remote not in failed.message


def test_open_refuses_a_remote_uri_wherever_the_project_keeps_it(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 换个元素名不能把 URI 藏过扫描。
    # 每个元素的正文都被问同一个问题。
    # Renaming the element cannot hide the URI.
    # Every element's text gets the same question.
    root = project_dir(tmp_path)
    xml = (
        "<qgis><projectlayers><maplayer>"
        f"<sourceLocation>{REMOTE_SOURCE}</sourceLocation>"
        "</maplayer></projectlayers></qgis>"
    )
    target = root / "renamed.qgs"
    target.write_text(xml, encoding="utf-8")
    loaded = FakeProject(layers_after_read={"never": FakeLayer(source=REMOTE_SOURCE)})
    ctx = context(loaded)

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert "remote data source" in failed.message


def test_open_refuses_a_datasource_in_a_namespace(qgis: types.ModuleType, tmp_path: Path) -> None:
    # 不管什么前缀，对 QGIS 都是 datasource。
    # 真正要紧的是值的开头。
    # 扫描从不问元素名。
    # Any prefix is still a datasource.
    # What matters is where the value starts.
    # The scan never asks the element name.
    root = project_dir(tmp_path)
    target = qgs_file(root, "namespaced.qgs", REMOTE_SOURCE)
    target.write_text(
        '<qgis xmlns:qgs="http://qgis.org/x"><qgs:datasource>'
        f"{REMOTE_SOURCE}</qgs:datasource></qgis>",
        encoding="utf-8",
    )
    loaded = FakeProject(layers_after_read={"never": FakeLayer(source=REMOTE_SOURCE)})
    ctx = context(loaded)

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert "remote data source" in failed.message


def test_a_project_that_is_not_well_formed_xml_is_refused_before_read(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 解析器读不了的文件不是工程。
    # 它被拒时给的是 read() 会给出的那句话。
    # 而不是交给一个在这类输入上崩过的解析器。
    # A file the parser cannot read is not a project.
    # It is refused the way read() would refuse.
    # Not handed to a parser that crashed before.
    root = project_dir(tmp_path)
    target = root / "broken.qgs"
    target.write_bytes(b"<qgis><projectlayers></qgis>")
    loaded = FakeProject(read_result=False)
    ctx = context(loaded)

    failed = refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert "may have changed" in failed.message


@pytest.mark.parametrize("suffix", [".qgs", ".qgz"])
def test_open_refuses_a_remote_source_that_a_child_element_carries_in_its_tail(
    qgis: types.ModuleType, tmp_path: Path, suffix: str
) -> None:
    # 元素正文不只是 ``element.text``。
    # 那个形状里 URI 是子元素的 tail。
    # QDomElement::text() 会拼整个子树。
    # 于是 read() 看到了它。
    # 而扫描只问了 ``element.text``（实测）。
    # An element's text is more than ``element.text``.
    # There the URI is the child's tail.
    # text() concatenates the whole subtree.
    # So read() saw and reached it.
    # The scan only asked ``element.text``, measured.
    xml = (
        "<!DOCTYPE qgis>\n<qgis><projectlayers><maplayer>"
        f"<datasource><x/>{REMOTE_SOURCE}</datasource>"
        "</maplayer></projectlayers></qgis>"
    )
    root = project_dir(tmp_path)
    if suffix == ".qgz":
        target = qgz_bytes(root, f"tail{suffix}", xml.encode("utf-8"))
    else:
        target = root / f"tail{suffix}"
        target.write_text(xml, encoding="utf-8")
    loaded = FakeProject(layers_after_read={"never": FakeLayer(source=REMOTE_SOURCE)})
    ctx = context(loaded)

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert "remote data source" in failed.message


@pytest.mark.parametrize("suffix", [".qgs", ".qgz"])
def test_open_refuses_a_source_split_across_several_child_elements(
    qgis: types.ModuleType, tmp_path: Path, suffix: str
) -> None:
    # text() 是子树里每个字符数据事件。
    # 按文档顺序拼起来。
    # 这五段碎片谁都不含 /vsi、:// 或 UNC。
    # 拼起来却是 read() 够到的地址。
    # 这里发了 8 个连接（实测）。
    # text() is every character-data event.
    # Concatenated in document order.
    # None of them holds /vsi, :// or a UNC prefix.
    # Their concatenation is the address read() reached.
    # 8 connections, measured.
    xml = (
        "<!DOCTYPE qgis>\n<qgis><projectlayers><maplayer>"
        "<datasource>/v<x/>sicurl/htt<y/>p:<y2/>/<z/>/127.0.0.1:9/never.tif</datasource>"
        "</maplayer></projectlayers></qgis>"
    )
    root = project_dir(tmp_path)
    if suffix == ".qgz":
        target = qgz_bytes(root, f"split{suffix}", xml.encode("utf-8"))
    else:
        target = root / f"split{suffix}"
        target.write_text(xml, encoding="utf-8")
    loaded = FakeProject(layers_after_read={"never": FakeLayer(source=REMOTE_SOURCE)})
    ctx = context(loaded)

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert "remote data source" in failed.message
    assert REMOTE_SOURCE not in failed.message


@pytest.mark.parametrize("suffix", [".qgs", ".qgz"])
def test_open_refuses_a_project_that_declares_xml_entities(
    qgis: types.ModuleType, tmp_path: Path, suffix: str
) -> None:
    # expat 会把内部子集展开进 element.text。
    # 所以磁盘字节数说不清扫描要多少内存。
    # 1 MiB 读 50 次，峰值 100 MiB。
    # 而文件只有 1 MiB、51 个标签、深度 3。
    # 其它上限全都沉默。
    # expat expands the subset into element.text.
    # So the bytes on disk say nothing.
    # 1 MiB read 50 times peaked at 100 MiB.
    # With 1 MiB, 51 tags and depth 3.
    # Every other bound stays silent.
    entity = "A" * (1024 * 1024)
    xml = f'<!DOCTYPE qgis [<!ENTITY a "{entity}">]><qgis>{"<a>&a;</a>" * 50}</qgis>'
    root = project_dir(tmp_path)
    if suffix == ".qgz":
        target = qgz_bytes(root, f"entities{suffix}", xml.encode("utf-8"))
    else:
        target = root / f"entities{suffix}"
        target.write_text(xml, encoding="utf-8")
    loaded = FakeProject()
    ctx = context(loaded)

    tracemalloc.start()
    try:
        failed = refused(
            PATH_NOT_ALLOWED,
            lambda: project.ProjectOpen().execute_main(
                ctx, project.ProjectOpenParams(path=str(target))
            ),
        )
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert loaded.read_path is None
    assert "entities" in failed.message
    assert peak < 64 * 1024 * 1024, f"the scan peaked at {peak / 1024 / 1024:.1f} MiB"


def test_a_project_with_the_doctype_qgis_writes_is_still_read(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 上面那条拒绝不能让普通工程打不开。
    # QGIS 写的是没有内部子集的公共 doctype。
    # 磁盘上每个工程都长这样。
    # That refusal may not lose a normal project.
    # QGIS writes a public doctype with no subset.
    # Every project on disk has one.
    root = project_dir(tmp_path)
    target = root / "doctype.qgs"
    target.write_text(
        "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>"
        f"<qgis><projectlayers><maplayer><datasource>{root / 'a.gpkg'}</datasource>"
        "</maplayer></projectlayers></qgis>",
        encoding="utf-8",
    )
    loaded = FakeProject()
    ctx = context(loaded)

    project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert loaded.read_path == str(target)


@pytest.mark.parametrize(
    "datasource,layername",
    [
        ("./vsi_data/x.gpkg", None),
        ("C:/data/vsidata/x.gpkg", None),
        ("a.gpkg", "data/vsicache"),
    ],
    ids=["relative-datasource", "absolute-datasource", "layer-name"],
)
def test_open_reads_a_project_whose_text_only_contains_vsi(
    qgis: types.ModuleType, tmp_path: Path, datasource: str, layername: str | None
) -> None:
    # 只有开头的 /vsi 才是 GDAL 文件系统。
    # ./vsi_data/ 和 C:/data/vsidata/ 都是普通目录。
    # 图层名根本不是数据源。
    # 子串检查把这三个全当成远程拒了。
    # Only a leading /vsi names a GDAL file system.
    # Both are ordinary local directories.
    # A layer name is not a data source at all.
    # The substring check refused all three as remote.
    root = project_dir(tmp_path)
    where = f"<layername>{layername}</layername>" if layername else ""
    target = root / "vsi text.qgs"
    target.write_text(
        "<qgis><projectlayers><maplayer>"
        f"<datasource>{datasource}</datasource>{where}"
        "</maplayer></projectlayers></qgis>",
        encoding="utf-8",
    )
    loaded = FakeProject(layers_after_read={"kept": FakeLayer(source=str(root / "a.gpkg"))})
    ctx = context(loaded)

    result = project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert loaded.read_path == str(target)
    assert result.layer_count == 1


@pytest.mark.parametrize(
    "source", ["file:./points.csv", "file:/data/points.csv", "file:///C:/data/points.csv"]
)
def test_open_reads_a_project_whose_datasource_is_a_file_uri(
    qgis: types.ModuleType, tmp_path: Path, source: str
) -> None:
    # CSV 在工程旁边时，QGIS 写成 ``file:./points.csv``。
    # ``file:`` 不是能碰到主机的 scheme。
    # 所以旧扫描把每个 CSV 工程都拒了。
    # QGIS writes it that way.
    # ``file:`` reaches no host.
    # So the old scan refused every CSV project.
    root = project_dir(tmp_path)
    target = qgs_file(root, "csv.qgs", source)
    # provider 先解 ``file:`` 再问白名单。
    # 假图层写的是 URI 指向的那个文件。
    # The provider resolves it before the check.
    # The fake layer names the file the URI points to.
    loaded = FakeProject(layers_after_read={"points": FakeLayer(source=str(root / "points.csv"))})
    ctx = context(loaded)

    result = project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert result.layer_count == 1
    assert loaded.removed == []


@pytest.mark.parametrize(
    "source", ["file://no-such-host-audit/share/points.csv", "file://localhost/C:/points.csv"]
)
def test_open_refuses_a_file_uri_that_names_a_host(
    qgis: types.ModuleType, tmp_path: Path, source: str
) -> None:
    # ``file://<authority>/…`` 点了名一个主机。
    # 去掉前缀不能把它变成本地路径。
    # That spelling names a host.
    # Stripping the prefix may not make it a local path.
    root = project_dir(tmp_path)
    target = qgs_file(root, "hosted.qgs", source)
    loaded = FakeProject(layers_after_read={"never": FakeLayer(source=source)})
    ctx = context(loaded)

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert "remote data source" in failed.message


def test_open_reads_a_project_whose_layer_metadata_quotes_a_url(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 散文里的 URL 不是以 URL 开头的值。
    # 统一规则只问每个值的开头。
    # 所以引用 URL 的描述照样能读。
    # A URL inside prose does not start with one.
    # The rule asks only where a value starts.
    # So prose quoting a URL still opens.
    root = project_dir(tmp_path)
    target = root / "metadata.qgs"
    target.write_text(
        "<qgis><projectlayers><maplayer>"
        f"<datasource>{root / 'a.gpkg'}</datasource>"
        "<metadata><abstract>Downloaded from https://example.org/data</abstract></metadata>"
        "</maplayer></projectlayers></qgis>",
        encoding="utf-8",
    )
    loaded = FakeProject()
    ctx = context(loaded)

    project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert loaded.read_path == str(target)


def test_open_refuses_a_metadata_value_that_is_a_url(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 这个误拒是刻意接受的。
    # 规则里没有元素名。
    # 所以标识符是 URL 的图层在 read() 前被拒。
    # 散文那一半已经修好。
    # 这一半是每个值问同一规则的代价。
    # 它被记下来，而不是又开一个特例。
    # The false refusal is accepted on purpose.
    # The rule has no element name in it.
    # So a URL identifier is refused first.
    # The prose half is already fixed.
    # This half is the price of one uniform rule.
    # It is recorded rather than special-cased back.
    root = project_dir(tmp_path)
    target = root / "metadata.qgs"
    target.write_text(
        "<qgis><projectlayers><maplayer>"
        f"<datasource>{root / 'a.gpkg'}</datasource>"
        "<metadata><identifier>https://example.org/datasets/42</identifier></metadata>"
        "</maplayer></projectlayers></qgis>",
        encoding="utf-8",
    )
    loaded = FakeProject()
    ctx = context(loaded)

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert "remote data source" in failed.message


@pytest.mark.parametrize("suffix", [".qgs", ".qgz"])
def test_open_refuses_a_layout_picture_that_names_a_remote_file(
    qgis: types.ModuleType, tmp_path: Path, suffix: str
) -> None:
    # ``read()`` 会加载布局项点名的图片。
    # 而那个名字是个属性。
    # 只问 datasource 正文的扫描放过了它。
    # project.open 发了 9 个连接（0.25 秒）。
    # 才返回 SUCCEEDED。
    # 而一个只接受连接、不答复的主机卡了 180 秒。
    # ``read()`` loads the picture a layout names.
    # And that name is an attribute.
    # A text-only scan let it through.
    # It made 9 connections (0.25 s).
    # Before returning SUCCEEDED.
    # A silent host held it for 180 s.
    root = project_dir(tmp_path)
    xml = layout_project(
        root / "a.gpkg", '<LayoutItem type="65640" file="http://127.0.0.1:9/picture.png"/>'
    )
    target = write_project_xml(root, f"picture{suffix}", xml)
    loaded = FakeProject(layers_after_read={"kept": FakeLayer(source=str(root / "a.gpkg"))})
    ctx = context(loaded)

    started = time.monotonic()
    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )
    elapsed = time.monotonic() - started

    assert loaded.read_path is None
    assert "remote data source" in failed.message
    assert "127.0.0.1" not in failed.message
    # 毫秒级，不是抓取的秒级。
    # 什么都没读，什么都没连。
    # Milliseconds, not the seconds a fetch takes.
    # Nothing was read or contacted.
    assert elapsed < 0.5, f"the {target.stat().st_size} byte project took {elapsed:.2f}s"


@pytest.mark.parametrize("suffix", [".qgs", ".qgz"])
def test_open_refuses_a_layout_html_source_that_is_a_remote_url(
    qgis: types.ModuleType, tmp_path: Path, suffix: str
) -> None:
    # HTML 项的来源也是一个值。
    # 这里是元素的整段正文。
    # An HTML item's source is a value like any other.
    # Here it is an element's whole text.
    root = project_dir(tmp_path)
    xml = layout_project(
        root / "a.gpkg",
        '<LayoutMultiFrame type="65648"><url>http://127.0.0.1:9/page.html</url></LayoutMultiFrame>',
    )
    target = write_project_xml(root, f"html{suffix}", xml)
    loaded = FakeProject()
    ctx = context(loaded)

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert "remote data source" in failed.message


@pytest.mark.parametrize(
    "value",
    [r"\\no-such-host-audit\share\picture.png", "/vsicurl/http://127.0.0.1:9/picture.tif"],
    ids=["unc", "vsi"],
)
def test_open_refuses_an_attribute_value_that_starts_with_a_remote_marker(
    qgis: types.ModuleType, tmp_path: Path, value: str
) -> None:
    # 同一个问题，这次问属性值。
    # 规则里没有元素名。
    # The same question, asked of an attribute value.
    # No element name is in the rule.
    root = project_dir(tmp_path)
    xml = layout_project(root / "a.gpkg", f'<LayoutItem type="65640" file="{value}"/>')
    target = write_project_xml(root, "attribute.qgs", xml)
    loaded = FakeProject()
    ctx = context(loaded)

    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert "remote data source" in failed.message


def test_open_reads_a_project_of_local_paths_and_a_quoted_url(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 统一规则不能拒掉的东西。
    # QGIS 自己的 CSV 写法、工程旁边的图片。
    # 属性值里的 C:/ 和 file:///C:/。
    # 以及描述里的一个 URL。
    # What the uniform rule may not refuse.
    # QGIS's own CSV spelling, a nearby picture.
    # C:/ and file:///C:/ in an attribute value.
    # And a URL inside a description.
    root = project_dir(tmp_path)
    target = root / "ordinary.qgs"
    target.write_text(
        "<!DOCTYPE qgis>\n<qgis><projectlayers><maplayer>"
        "<datasource>file:./points.csv</datasource>"
        '<provider encoding="UTF-8">delimitedtext</provider>'
        "<srs><authid>EPSG:4326</authid></srs>"
        "<metadata><abstract>Downloaded from https://example.org/data</abstract></metadata>"
        "</maplayer></projectlayers>"
        # QGIS 3.44 自己写出的工程样式。
        # 是本地 scheme，不是主机。
        # What QGIS 3.44 writes for its own project style.
        # A local scheme, not a host.
        '<ProjectStyleSettings projectStyleId="attachment:///styles.db" '
        'iccProfileId="attachment:///"/>'
        '<Layouts><Layout name="sheet">'
        '<LayoutItem type="65640" file="./data/pixel.tif"/>'
        '<LayoutItem type="65640" file="C:/data/pixel.tif"/>'
        '<LayoutItem type="65640" value="file:///C:/data/pixel.tif"/>'
        "</Layout></Layouts></qgis>",
        encoding="utf-8",
    )
    loaded = FakeProject(layers_after_read={"points": FakeLayer(source=str(root / "points.csv"))})
    ctx = context(loaded)

    result = project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert result.layer_count == 1
    assert loaded.removed == []


def test_a_project_of_repeated_and_oversized_attributes_is_refused_in_milliseconds(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 每个值只问一次开头。
    # 所以恶意文件的代价是解析。
    # 不是随它增长的循环。
    # 远程值放在最后一个属性。
    # 所以 14 MB 的值和 20000 个属性先被走过。
    # One start-of-value question per value.
    # So a hostile file costs a parse.
    # Not a loop that grows with it.
    # The remote value is the last attribute.
    # So the big value and repeats go first.
    root = project_dir(tmp_path)
    target = root / "attributes.qgs"
    repeated = " ".join(f'k{index}="v"' for index in range(20_000))
    padding = "x" * (14 * 1024 * 1024)
    target.write_text(
        "<qgis><projectlayers><maplayer>"
        f"<datasource>{root / 'a.gpkg'}</datasource>"
        f'<metadata big="{padding}" {repeated} '
        'remote="/vsicurl/http://127.0.0.1:9/never.tif"/>'
        "</maplayer></projectlayers></qgis>",
        encoding="utf-8",
    )
    assert target.stat().st_size < project.MAX_PROJECT_BYTES
    loaded = FakeProject()
    ctx = context(loaded)

    started = time.monotonic()
    failed = refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )
    elapsed = time.monotonic() - started

    assert loaded.read_path is None
    assert "remote data source" in failed.message
    assert elapsed < 2.0, f"the {target.stat().st_size} byte project took {elapsed:.2f}s"


def test_a_large_text_nested_deep_is_not_rejoined_at_every_level(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 只判断元素正文的开头。
    # 所以每个打开的元素只留 64 个字符。
    # 每层拼一次正文就复制一次。
    # 这个形状那样要 0.5-1.0 秒。
    # 有上限时只要毫秒。
    # 这些都是 ``read()`` 之前的主线程时间。
    # Only the start of an element's text is judged.
    # So 64 characters are kept per open element.
    # Joining it per level copies it once per level.
    # This shape took 0.5-1.0 s that way.
    # Milliseconds with the bound.
    # All of it is main-thread time before ``read()``.
    root = project_dir(tmp_path)
    target = root / "deep text.qgs"
    depth = 200
    target.write_text(
        "<qgis>"
        + "<g>pre" * depth
        + f"<datasource>{root / 'a.gpkg'}</datasource>"
        + "y" * (8 * 1024 * 1024)
        + "post</g>" * depth
        + "</qgis>",
        encoding="utf-8",
    )
    loaded = FakeProject(layers_after_read={"kept": FakeLayer(source=str(root / "a.gpkg"))})
    ctx = context(loaded)

    started = time.monotonic()
    result = project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))
    elapsed = time.monotonic() - started

    assert result.layer_count == 1
    assert elapsed < 0.3, f"the {target.stat().st_size} byte project took {elapsed:.2f}s"


def test_a_qgz_with_too_many_members_is_refused_before_any_is_unpacked(
    qgis: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 预算按解压出的字节数计。
    # 空成员在那里不花成本。
    # 但每个成员都要一次 ZipFile.open()。
    # 100000 个空成员让主线程卡了 1.75 秒。
    # The budget counts decompressed bytes.
    # An empty member costs nothing there.
    # But each member costs one ZipFile.open().
    # 100000 empty members took 1.75 s.
    root = project_dir(tmp_path)
    target = root / "many empty members.qgz"
    with zipfile.ZipFile(target, "w") as archive:
        for index in range(100_000):
            archive.writestr(f"empty{index}.txt", b"")
    assert target.stat().st_size < project.MAX_PROJECT_BYTES

    def open_member(self: zipfile.ZipFile, member: Any, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"the archive member {member} was unpacked before it was counted")

    monkeypatch.setattr(zipfile.ZipFile, "open", open_member)
    loaded = FakeProject()
    ctx = context(loaded)

    started = time.monotonic()
    failed = refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )
    elapsed = time.monotonic() - started

    assert loaded.read_path is None
    assert "members" in failed.message
    # 加限之前是 1.75 秒。
    # 那时每个成员都被逐个打开。
    # 现在剩下的只是 zipfile 读一次中央目录。
    # 这台机器上约 0.6-1.0 秒。
    # 1.75 s before the bound.
    # Every member was opened in turn then.
    # The rest is zipfile reading the directory once.
    # About 0.6-1.0 s on this box.
    assert elapsed < 1.5, f"the {target.stat().st_size} byte archive took {elapsed:.2f}s"


def test_a_qgz_over_the_size_budget_is_refused_before_its_directory_is_read(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 成员预算按解压后的字节计。
    # 空成员在那里不花成本。
    # 400000 个空成员要 2.48 秒。
    # 峰值 222 MiB。
    # 归档尺寸先问，再让 zipfile 读目录。
    # The member budget counts decompressed bytes.
    # An empty member costs nothing there.
    # 400000 empty members cost 2.48 s.
    # Peak 222 MiB.
    # The archive's size is asked before the directory.
    root = project_dir(tmp_path)
    target = root / "many empty members.qgz"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as archive:
        for index in range(150_000):
            archive.writestr(f"{index:x}", b"")
        archive.writestr("padding.bin", b" " * (6 * 1024 * 1024))
    assert target.stat().st_size > project.MAX_PROJECT_BYTES
    loaded = FakeProject()
    ctx = context(loaded)

    tracemalloc.start()
    try:
        started = time.monotonic()
        failed = refused(
            PATH_NOT_ALLOWED,
            lambda: project.ProjectOpen().execute_main(
                ctx, project.ProjectOpenParams(path=str(target))
            ),
        )
        elapsed = time.monotonic() - started
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert loaded.read_path is None
    # ``too large`` 说的是归档自己的尺寸。
    # 成员数是打开过的归档才会拿到的理由。
    # 那要先把 150001 条目录建起来。
    # ``too large`` is the archive's own size.
    # The member count refuses an opened archive.
    # That needs the 150001-entry directory first.
    assert "too large" in failed.message
    # 光是这个目录就花了 6.7 秒。
    # 探针里峰值 66 MiB，tracemalloc 开着。
    # 这次扫描剩下的只是一次 stat()。
    # The directory alone took 6.7 s.
    # It peaked at 66 MiB with tracemalloc on.
    # What is left of this scan is one stat().
    assert elapsed < 0.2, f"the {target.stat().st_size} byte archive took {elapsed:.2f}s"
    assert peak < 64 * 1024 * 1024, f"the scan peaked at {peak / 1024 / 1024:.1f} MiB"


def test_the_depth_bound_is_the_one_read_time_measured(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 上限按 read() 的耗时定。
    # 不是按它崩掉定。
    # 900 层让主线程花 30.11 秒（实测）。
    # 600 层 8.84 秒，200 层 0.28 秒。
    # 256 落在它们之间。
    # 而真实的分组树只有 105 层。
    # The bound is set by read()'s time.
    # Not by its crash.
    # 900 levels took 30.11 s, measured.
    # 600 levels 8.84 s, 200 levels 0.28 s.
    # 256 sits between them.
    # A real group tree is depth 105.
    assert project.MAX_PROJECT_DEPTH == 256
    root = project_dir(tmp_path)
    target = nested_project(root, "at the bound.qgs", project.MAX_PROJECT_DEPTH)
    loaded = FakeProject()
    ctx = context(loaded)

    failed = refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert "nests deeper" in failed.message


def test_open_reads_a_project_with_the_attributes_qgis_itself_writes(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # QGIS 只转义 <、& 和 "，不转义 >。
    # 带标注的图层里就有两个这样的标签。
    # 属性值里有裸 ">"。
    # 旧扫描在那里结束了标签，多出一层再也回不来。
    # 28 个带标注图层因此被永久拒掉。
    # QGIS escapes <, & and ", not >.
    # A labelled layer carries two such tags.
    # A bare ">" inside an attribute value.
    # The old scan ended a tag there.
    # So 28 labelled layers were refused.
    root = project_dir(tmp_path)
    sample = b"".join(tag * 70 for tag in QGIS_LABEL_TAGS)
    target = root / "labelled.qgs"
    target.write_bytes(
        b"<qgis><projectlayers><maplayer><srs><authid>EPSG:4326</authid></srs>"
        b"<labeling><settings>" + sample + b"</settings></labeling>"
        b"<datasource>" + str(root / "a.gpkg").encode() + b"</datasource>"
        b"</maplayer></projectlayers></qgis>"
    )
    loaded = FakeProject()
    ctx = context(loaded)

    project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert loaded.read_path == str(target)


def test_open_refuses_an_archive_whose_sibling_member_is_encrypted(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # read() 会解开每个成员，不只是 .qgs。
    # 一个加密的 extra.db 把进程带崩了。
    # 现在每个成员都检查。
    # read() unpacks every member, not only .qgs.
    # An encrypted extra.db crashed the process.
    # Every member is checked now.
    root = project_dir(tmp_path)
    target = root / "sibling.qgz"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("project.qgs", project_xml(str(root / "a.gpkg")))
        archive.writestr("extra.db", b"a sibling member read() also unpacks")
    set_encryption_flag(target, "extra.db")
    loaded = FakeProject()
    ctx = context(loaded)

    failed = refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert "may have changed" in failed.message


def test_open_refuses_a_member_compressed_in_a_way_qgis_does_not_read(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # ``zipfile`` 读得了 bzip2。
    # 但 QGIS 的解压只确认支持两种成员。
    # 一是 stored，二是 deflated。
    # 所以它可能撑不住的归档先被拒。
    # ``zipfile`` reads bzip2.
    # But QGIS's unzip is only known to read two kinds.
    # Stored, and deflated.
    # So an archive it may not survive is refused first.
    root = project_dir(tmp_path)
    target = root / "bzip2.qgz"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_BZIP2) as archive:
        archive.writestr("project.qgs", project_xml(str(root / "a.gpkg")))
    loaded = FakeProject()
    ctx = context(loaded)

    failed = refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path is None
    assert "may have changed" in failed.message


def test_a_member_that_lies_about_its_size_is_not_read_in_one_piece(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 目录写着 100 字节，成员其实是 192 MB。
    # 旧预算信了目录。
    # archive.read() 随后把整个成员展开。
    # 204 KB 的归档峰值 437 MiB。
    # 现在按块读并计数。
    # 所以成员从不整块留下。
    # The directory said 100 bytes, the member 192 MB.
    # The old budget believed it.
    # archive.read() then inflated the whole member.
    # A 204 KB archive peaked at 437 MiB.
    # It reads in chunks and counts the bytes.
    # So the member is never held whole.
    root = project_dir(tmp_path)
    target = root / "lying.qgz"
    chunk = b" " * (1024 * 1024)
    with (
        zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive,
        archive.open("project.qgs", "w") as member,
    ):
        for _ in range(192):
            member.write(chunk)
    assert target.stat().st_size < project.MAX_PROJECT_BYTES // 8
    lie_about_member_size(target, 100)
    loaded = FakeProject()
    ctx = context(loaded)

    tracemalloc.start()
    try:
        failed = refused(
            PROJECT_OPEN_FAILED,
            lambda: project.ProjectOpen().execute_main(
                ctx, project.ProjectOpenParams(path=str(target))
            ),
        )
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert loaded.read_path is None
    assert "may have changed" in failed.message
    assert peak < 64 * 1024 * 1024, f"the scan peaked at {peak / 1024 / 1024:.1f} MiB"


def test_open_still_reads_a_project_whose_datasource_is_a_relative_path(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # QGIS 默认存相对路径。
    # 里面的 ``..`` 由路径策略解析。
    # 两者都不是远程源，也不是缺陷。
    # QGIS stores relative paths by default.
    # A ``..`` inside one is resolved by the path policy.
    # Neither is a remote source or a structural defect.
    root = project_dir(tmp_path)
    (root / "nested").mkdir()
    source = root / "nested" / ".." / "data.gpkg"
    target = qgs_file(root, "relative.qgs", "../data.gpkg")
    loaded = FakeProject(layers_after_read={"relative": FakeLayer(source=str(source))})
    ctx = context(loaded)

    result = project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert result.layer_count == 1
    assert loaded.removed == []


def test_open_reports_an_encrypted_member_as_a_read_failure(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 加密成员让 ``zipfile`` 抛 RuntimeError。
    # 扫描不是新的失败来源。
    # 所以文件仍用 ``read()`` 的码被拒。
    # 即 PROJECT_OPEN_FAILED，而不是 INTERNAL_ERROR。
    # 后者读起来像插件缺陷。
    # ``read()`` 根本没被调用。
    # QGIS 3.44 在这类归档上会崩。
    # An encrypted member makes ``zipfile`` raise.
    # A scan is not a new failure source.
    # So the file keeps read()'s own code.
    # PROJECT_OPEN_FAILED, not INTERNAL_ERROR.
    # The latter reads like a plugin defect.
    # ``read()`` is not called at all.
    # QGIS 3.44 crashes on such an archive.
    root = project_dir(tmp_path)
    target = qgz_file(root, "locked.qgz", str(root / "a.gpkg"))
    set_encryption_flag(target)
    loaded = FakeProject(read_result=False)
    ctx = context(loaded)

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
    # 目录本身搬走时也一样。
    # 数据源仍在根内。
    # 所以只是部分加载，不删层也不换工程。
    # Also when the directory itself moved away.
    # The source is still inside the roots.
    # So it is a partial load, not a breach.
    root = project_dir(tmp_path)
    source = root / "moved-away" / "cities.gpkg"
    target = (
        qgz_file(root, f"moved{suffix}", str(source))
        if suffix == ".qgz"
        else qgs_file(root, f"moved{suffix}", str(source))
    )
    loaded = FakeProject(layers_after_read={"moved": FakeLayer(valid=False, source=str(source))})
    ctx = context(loaded)

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
    ctx = context(loaded)

    project.ProjectOpen().execute_main(ctx, project.ProjectOpenParams(path=str(target)))

    assert loaded.read_path == str(target.resolve())


def test_open_reports_a_corrupt_archive_as_a_read_failure(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 预扫只读字节。
    # 解不开的文件仍是工程文件自己的问题。
    # 由 ``read()`` 报成 PROJECT_OPEN_FAILED。
    # The pre-read scan only reads bytes.
    # A file it cannot unzip is the file's own problem.
    # Reported by read() as PROJECT_OPEN_FAILED.
    root = project_dir(tmp_path)
    target = root / "corrupt.qgz"
    target.write_bytes(b"this is not zipped project data")
    loaded = FakeProject(read_result=False)
    ctx = context(loaded)

    refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            ctx, project.ProjectOpenParams(path=str(target))
        ),
    )

    assert loaded.read_path == str(target.resolve())


# --- project.save：保存工程 / saving a project ---------------------------


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
    ctx = context(loaded)

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
    # 连当前关联文件也要 overwrite=true。
    # 否则普通 save() 会悄悄覆盖它。
    # Even the associated file needs overwrite=true.
    # Otherwise a plain save() would replace it.
    root = project_dir(tmp_path)
    target = root / "current.qgs"
    target.write_text("original", encoding="utf-8")
    loaded = FakeProject(file_name=str(target))
    ctx = context(loaded)

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
    ctx = context(loaded)

    result = project.ProjectSave().execute_main(ctx, project.ProjectSaveParams(path=str(target)))

    assert Path(result.file_path) == target.resolve()
    assert loaded.writes == [str(target.resolve())]
    assert result.is_dirty is False


def test_save_overwrites_when_explicitly_allowed(qgis: types.ModuleType, tmp_path: Path) -> None:
    root = project_dir(tmp_path)
    target = root / "out.qgs"
    target.write_text("original", encoding="utf-8")
    # 工程故意已经有文件。
    # 显式 `path` 要赢。
    # 关联文件只是 `path` 为 null 时的兜底。
    # The project already has a file on purpose.
    # An explicit `path` has to win.
    # The associated file is only the fallback.
    loaded = FakeProject(file_name=str(root / "current.qgs"))
    ctx = context(loaded)

    project.ProjectSave().execute_main(
        ctx, project.ProjectSaveParams(path=str(target), overwrite=True)
    )

    assert loaded.writes == [str(target.resolve())]


def test_save_falls_back_to_the_associated_file(qgis: types.ModuleType, tmp_path: Path) -> None:
    root = project_dir(tmp_path)
    target = root / "current.qgs"
    target.write_text("original", encoding="utf-8")
    loaded = FakeProject(file_name=str(target))
    ctx = context(loaded)

    result = project.ProjectSave().execute_main(ctx, project.ProjectSaveParams(overwrite=True))

    assert Path(result.file_path) == target.resolve()


def test_save_reports_a_failed_write(qgis: types.ModuleType, tmp_path: Path) -> None:
    root = project_dir(tmp_path)
    loaded = FakeProject(write_result=False)
    ctx = context(loaded)

    failed = refused(
        PROJECT_SAVE_FAILED,
        lambda: project.ProjectSave().execute_main(
            ctx, project.ProjectSaveParams(path=str(root / "out.qgs"))
        ),
    )
    assert "partially written" in failed.message


# --- project.get_info：看工程 / reading the project -----------------------


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


# --- 声明与裸解释器 / declarations and the bare interpreter -----------------


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
