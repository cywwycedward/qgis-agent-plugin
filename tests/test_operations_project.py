"""用假 ctx 和假 PyQGIS 驱动 ``project.*`` 操作。

每个用例对着一条来自 QGIS 3.44 桌面版的判据：新建与打开只问"有未保存内容吗"（``is_dirty``），
``Save`` 直接写关联文件、``Save As`` 才问覆盖，``get_info`` 报 GUI 显示的那些事实。真实 PyQGIS
的行为（``read()`` 到底填不填图层树、深度多少会打掉进程）留给 QGIS 侧的验收跑。

The ``project.*`` operations, driven by a fake ctx and a fake PyQGIS.  Every case matches one
criterion taken from the QGIS 3.44 desktop source: creating and opening ask only whether there is
unsaved content (``is_dirty``), ``Save`` writes the associated file while ``Save As`` is the one
that asks about overwriting, and ``get_info`` reports the facts the GUI shows.  Real PyQGIS
behaviour (whether ``read()`` fills the layer tree, how much nesting ends the process) belongs to
the QGIS acceptance run, not here.
"""

from __future__ import annotations

import struct
import sys
import types
import zipfile
from pathlib import Path
from typing import Any

import pytest  # pyright: ignore[reportMissingImports]
from pydantic import ValidationError  # pyright: ignore[reportMissingImports]

from qgis_agent_plugin.core.config import Settings  # pyright: ignore[reportMissingImports]
from qgis_agent_plugin.core.contracts import OperationContext  # pyright: ignore[reportMissingImports]
from qgis_agent_plugin.core.models import (  # pyright: ignore[reportMissingImports]
    INTERNAL_ERROR,
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
from qgis_agent_plugin.core.registry import OperationRegistry  # pyright: ignore[reportMissingImports]
from qgis_agent_plugin.operations import project  # pyright: ignore[reportMissingImports]

ROOT = Path(__file__).resolve().parents[1]
# 绝对路径且格式正确，从不真的碰它。
# 只有输入模型会看到它。
# Absolute and well formed, never touched.
# Only the input models see it.
A_PROJECT_FILE = str(ROOT / "maps" / "a.qgs")


class FakeCrs:
    """只有真 QGIS 解析得出的 authid 才有规范形式，别的都报空串，和 ``crs().authid()`` 一样。

    Only an authid a real QGIS resolves has a canonical form; anything else reports an empty
    string, as ``crs().authid()`` does.
    """

    _KNOWN = {"epsg:4326": "EPSG:4326", "epsg:3857": "EPSG:3857"}

    def __init__(self, authid: str) -> None:
        self._authid = authid

    def authid(self) -> str:
        return self._KNOWN.get(self._authid.lower(), "")


@pytest.fixture
def qgis(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """操作在方法体内 import ``qgis.core``；这个假模块就是那一步要的东西。

    The operations import ``qgis.core`` inside their method body; this is that module.
    """

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
    """``QgsProject`` 里这四个操作碰得到的那几个方法。

    The handful of ``QgsProject`` methods these four operations touch.
    """

    def __init__(
        self,
        *,
        layers: dict[str, FakeLayer] | None = None,
        file_name: str = "",
        dirty: bool = False,
        crs_authid: str = "EPSG:4326",
        title: str = "",
        read_result: bool = True,
        layers_after_read: dict[str, FakeLayer] | None = None,
        crs_after_read: str = "EPSG:4326",
        write_result: bool = True,
    ) -> None:
        self.layers = dict(layers or {})
        self.file_name = file_name
        self.dirty = dirty
        self.crs_authid = crs_authid
        self.title_text = title
        self.read_result = read_result
        self.layers_after_read = dict(layers_after_read or {})
        self.crs_after_read = crs_after_read
        self.write_result = write_result
        self.read_path: str | None = None
        self.reads = 0
        self.clears = 0
        self.writes: list[str] = []
        self.crs_set: FakeCrs | None = None

    def mapLayers(self) -> dict[str, FakeLayer]:  # noqa: N802 - a PyQGIS name
        return dict(self.layers)

    def fileName(self) -> str:  # noqa: N802 - a PyQGIS name
        return self.file_name

    def isDirty(self) -> bool:  # noqa: N802 - a PyQGIS name
        return self.dirty

    def setDirty(self, dirty: bool) -> None:  # noqa: N802 - a PyQGIS name
        self.dirty = dirty

    def title(self) -> str:
        return self.title_text

    def crs(self) -> FakeCrs:
        return FakeCrs(self.crs_authid)

    def setCrs(self, crs: FakeCrs) -> None:  # noqa: N802 - a PyQGIS name
        self.crs_set = crs
        self.crs_authid = crs.authid()
        # 真 ``setCrs`` 会把工程标脏：那是 ``project.create`` 之后必须清掉它的原因。
        # The real ``setCrs`` dirties the project: that is why ``project.create`` has to clear it.
        self.dirty = True

    def clear(self) -> None:
        self.clears += 1
        self.layers = {}
        self.file_name = ""
        self.dirty = False
        self.crs_authid = ""
        self.title_text = ""

    def read(self, path: str) -> bool:
        self.reads += 1
        self.read_path = path
        # 真 ``QgsProject::read()`` 先记住文件名，再决定读不读得出来。
        # The real ``QgsProject::read()`` remembers the file name first, then decides.
        self.file_name = path
        if not self.read_result:
            return False
        self.layers = dict(self.layers_after_read)
        self.crs_authid = self.crs_after_read
        self.dirty = False
        return True

    def write(self, path: str) -> bool:
        self.writes.append(path)
        if not self.write_result:
            return False
        self.file_name = path
        self.dirty = False
        return True


class FakeIface:
    def __init__(
        self,
        project_: FakeProject | None = None,
        *,
        result: bool = True,
        new_crs: str = "EPSG:3857",
        new_layers: dict[str, FakeLayer] | None = None,
    ) -> None:
        self.project = project_
        self.result = result
        # 真 ``QgisApp::fileNew`` 最后一行是 ``setDirty( false )``，CRS 来自设置而不是调用方；
        # 默认模板还会带图层进来。
        # The real ``QgisApp::fileNew`` ends with ``setDirty( false )``, its CRS comes from a
        # setting rather than from the caller, and the default template brings layers along.
        self.new_crs = new_crs
        self.new_layers = dict(new_layers or {})
        self.new_project_calls: list[bool] = []

    def newProject(self, promptToSaveFlag: bool = False) -> bool:  # noqa: N802 - a PyQGIS name
        self.new_project_calls.append(promptToSaveFlag)
        if self.result and self.project is not None:
            self.project.clear()
            self.project.crs_authid = self.new_crs
            self.project.layers = dict(self.new_layers)
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


def nested_project(directory: Path, name: str, depth: int) -> Path:
    """一个良构的工程，根元素下面嵌了 ``depth`` 层。

    A well-formed project nested ``depth`` levels below its root element.
    """

    target = directory / name
    target.write_bytes(b"<qgis>" + b"<a>" * depth + b"</a>" * depth + b"</qgis>")
    return target


def archive(
    directory: Path, name: str, member: bytes, *, method: int = zipfile.ZIP_DEFLATED
) -> Path:
    target = directory / name
    with zipfile.ZipFile(target, "w", method) as handle:
        handle.writestr("project.qgs", member)
    return target


def set_encryption_flag(path: Path) -> None:
    """把归档成员标成加密：``zipfile`` 写不了加密成员，直接置通用标志位。

    Mark the archive member as encrypted: ``zipfile`` cannot write an encrypted member, so the
    general purpose flag is set instead.
    """

    data = bytearray(path.read_bytes())
    for signature, flag_at, length_at, name_at in (
        (b"PK\x03\x04", 6, 26, 30),
        (b"PK\x01\x02", 8, 28, 46),
    ):
        cursor = 0
        while (start := data.find(signature, cursor)) >= 0:
            cursor = start + 1
            length = int.from_bytes(data[start + length_at : start + length_at + 2], "little")
            name = bytes(data[start + name_at : start + name_at + length]).decode("utf-8")
            if name.endswith(".qgs"):
                flags = int.from_bytes(data[start + flag_at : start + flag_at + 2], "little")
                data[start + flag_at : start + flag_at + 2] = (flags | 0x1).to_bytes(2, "little")
    path.write_bytes(bytes(data))


def break_member_crc(path: Path) -> None:
    """把成员的 CRC 改成错的：真 QGIS 3.44 照读，所以扫描也不许拒。

    Break the member's CRC: real QGIS 3.44 reads it anyway, so the scan may not refuse it.
    """

    data = bytearray(path.read_bytes())
    for signature, crc_at in ((b"PK\x03\x04", 14), (b"PK\x01\x02", 16)):
        start = data.find(signature)
        data[start + crc_at : start + crc_at + 4] = struct.pack("<I", 0xDEADBEEF)
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
        (project.ProjectCreateParams, {"crs_authid": None, "discard_current": False}),
        (project.ProjectOpenParams, {"path": A_PROJECT_FILE, "discard_current": False}),
        (project.ProjectSaveParams, {"path": None, "overwrite": False}),
        (project.ProjectGetInfoParams, {}),
    ],
    ids=["create", "open", "save", "get_info"],
)
def test_an_unknown_input_field_is_refused(model: Any, fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as caught:
        model(**fields, unexpected=True)
    assert "unexpected" in str(caught.value)


@pytest.mark.parametrize("value", ["true", 1, 0, None])
def test_a_boolean_field_refuses_a_non_boolean(value: object) -> None:
    with pytest.raises(ValidationError):
        project.ProjectCreateParams(discard_current=value)


def test_create_defaults_to_no_crs_and_no_discard() -> None:
    # CRS 不给就是不碰 QGIS 自己会给的那个；这不是"默认 EPSG:4326"。
    # No CRS means "leave the one QGIS would give"; it is not "EPSG:4326 by default".
    fields = project.ProjectCreateParams.model_fields
    assert fields["crs_authid"].default is None
    assert fields["discard_current"].default is False


@pytest.mark.parametrize("raw", ["maps/a.txt", "maps/a.qgs~", "maps/a"])
def test_a_save_target_must_carry_a_project_suffix(raw: str) -> None:
    # Save As：GUI 的保存对话框按过滤器把后缀补成 .qgs/.qgz，所以别的后缀不是一条保存目标。
    # Save As: the GUI's save dialog appends .qgs/.qgz by filter, so no other suffix is a target.
    with pytest.raises(ValidationError):
        project.ProjectSaveParams(path=raw)


def test_a_save_suffix_is_case_insensitive() -> None:
    assert project.ProjectSaveParams(path="C:/maps/a.QGZ").path == "C:/maps/a.QGZ"


@pytest.mark.parametrize("raw", ["a.qgs", "C:maps/a.qgs", "\\a.qgs"])
def test_an_open_path_has_no_shape_rule_beyond_being_a_path(
    qgis: types.ModuleType, raw: str
) -> None:
    # 打开只要求"是个路径"：后缀由 GUI 的对话框决定，相对路径由 ``absolute_path`` 拒。
    # Opening only asks for a path: the dialog decides the suffix and ``absolute_path`` refuses a
    # relative one.
    request = project.ProjectOpenParams(path=raw)
    refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(context(FakeProject()), request),
    )


def test_an_empty_open_path_is_refused_by_the_model() -> None:
    # 空串在模型边界就拒，不到路径规则那一步。
    # An empty string is refused at the model edge, before the path rule runs.
    with pytest.raises(ValidationError):
        project.ProjectOpenParams(path="")


def test_an_open_path_may_have_any_suffix(qgis: types.ModuleType, tmp_path: Path) -> None:
    # 备份文件 ``x.qgs~`` 与手改名的工程都是 GUI 打得开的文件。
    # A ``x.qgs~`` backup and a project renamed by hand are both files the GUI opens.
    target = tmp_path / "renamed.txt"
    target.write_text("<qgis/>", encoding="utf-8")
    loaded = FakeProject()
    result = project.ProjectOpen().execute_main(
        context(loaded), project.ProjectOpenParams(path=str(target))
    )
    assert result.file_path == str(target)
    assert loaded.read_path == str(target)


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
            ["file_path", "crs_authid", "is_dirty", "layer_count", "title", "project_epoch"],
        ),
    ],
    ids=["create", "open", "save", "get_info"],
)
def test_a_result_carries_exactly_the_declared_output_fields(model: Any, fields: list[str]) -> None:
    # 记录的键就是契约：多一个字段会流到每个调用方。
    # Every record's keys are the contract: an extra field reaches every caller.
    assert list(model.model_fields) == fields


def test_a_result_rejects_an_undeclared_field() -> None:
    with pytest.raises(ValidationError):
        project.ProjectCreateResult(crs_authid="EPSG:4326", layer_counts=5)


# --- project.create：新建工程 / creating a project -------------------------


def test_create_refuses_a_project_with_unsaved_changes(qgis: types.ModuleType) -> None:
    loaded = FakeProject(dirty=True)
    iface = FakeIface(loaded)
    refused(
        PROJECT_REPLACE_REQUIRED,
        lambda: project.ProjectCreate().execute_main(
            context(loaded, iface=iface), project.ProjectCreateParams()
        ),
    )
    assert iface.new_project_calls == []


@pytest.mark.parametrize(
    "state",
    [
        {"layers": {"a": FakeLayer()}},
        {"file_name": "C:/maps/current.qgs"},
        {"layers": {"a": FakeLayer()}, "file_name": "C:/maps/current.qgs"},
    ],
    ids=["layers", "associated file", "both"],
)
def test_create_replaces_a_clean_project_whatever_it_holds(
    qgis: types.ModuleType, state: dict[str, Any]
) -> None:
    # GUI 的条件只有一个：``is_dirty()``。图层数与关联文件名都不是条件。
    # The GUI has one condition only: ``is_dirty()``.  Layer count and file name are not it.
    loaded = FakeProject(**state)
    iface = FakeIface(loaded)
    result = project.ProjectCreate().execute_main(
        context(loaded, iface=iface), project.ProjectCreateParams()
    )
    assert iface.new_project_calls == [False]
    assert result.layer_count == 0


def test_create_keeps_the_crs_qgis_gave_when_the_caller_asks_for_none(
    qgis: types.ModuleType,
) -> None:
    loaded = FakeProject()
    iface = FakeIface(loaded, new_crs="EPSG:3857")
    result = project.ProjectCreate().execute_main(
        context(loaded, iface=iface), project.ProjectCreateParams()
    )
    # 不传 CRS 就不碰它：新工程的 CRS 是设置里的那一个。
    # No CRS means it is not touched: a new project's CRS is the one from the setting.
    assert loaded.crs_set is None
    assert result.crs_authid == "EPSG:3857"


def test_create_sets_the_crs_the_caller_gave(qgis: types.ModuleType) -> None:
    loaded = FakeProject()
    result = project.ProjectCreate().execute_main(
        context(loaded),
        project.ProjectCreateParams(crs_authid="epsg:3857"),
    )
    # 报的是工程现在持有的规范 authid，不是调用方打的那串。
    # The canonical authid the project holds is reported, not the caller's spelling.
    assert result.crs_authid == "EPSG:3857"


def test_create_does_not_refuse_an_unusable_crs(qgis: types.ModuleType) -> None:
    # GUI 的新建工程不问 CRS，也不会因为设置里的默认值失效而拒绝新建：``setCrs`` 只是把一个
    # 无效 CRS 装进工程，报出来是空 authid。
    # The GUI's new-project action asks for no CRS and does not refuse when the default setting has
    # gone bad: ``setCrs`` installs the invalid CRS and it reports an empty authid.
    loaded = FakeProject()
    result = project.ProjectCreate().execute_main(
        context(loaded),
        project.ProjectCreateParams(crs_authid="NOT:A_CRS"),
    )
    assert result.crs_authid == ""
    assert loaded.crs_set is not None


def test_create_leaves_a_clean_project(qgis: types.ModuleType) -> None:
    # ``setCrs`` 会把工程标脏，而 GUI 的 ``fileNew`` 最后一步是 ``setDirty( false )``。
    # ``setCrs`` dirties the project, and the GUI's ``fileNew`` ends with ``setDirty( false )``.
    loaded = FakeProject()
    project.ProjectCreate().execute_main(
        context(loaded), project.ProjectCreateParams(crs_authid="EPSG:4326")
    )
    assert loaded.crs_set is not None
    assert loaded.dirty is False


def test_create_reports_the_layers_the_new_project_brought(qgis: types.ModuleType) -> None:
    # 默认模板可以带图层进来。
    # The default template may bring layers along.
    loaded = FakeProject()
    iface = FakeIface(loaded, new_layers={"a": FakeLayer(), "b": FakeLayer()})
    result = project.ProjectCreate().execute_main(
        context(loaded, iface=iface), project.ProjectCreateParams()
    )
    assert result.layer_count == 2
    assert result.file_path is None


def test_create_reports_a_refused_new_project(qgis: types.ModuleType) -> None:
    loaded = FakeProject()
    iface = FakeIface(loaded, result=False)
    refused(
        INTERNAL_ERROR,
        lambda: project.ProjectCreate().execute_main(
            context(loaded, iface=iface), project.ProjectCreateParams()
        ),
    )


# --- project.open：打开工程 / opening a project ----------------------------


def test_open_refuses_a_project_with_unsaved_changes(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    target = tmp_path / "a.qgs"
    target.write_text("<qgis/>", encoding="utf-8")
    loaded = FakeProject(dirty=True)
    refused(
        PROJECT_REPLACE_REQUIRED,
        lambda: project.ProjectOpen().execute_main(
            context(loaded), project.ProjectOpenParams(path=str(target))
        ),
    )
    assert loaded.reads == 0


def test_open_replaces_a_clean_project_whatever_it_holds(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 一个没改过、但有图层也有文件的工程，GUI 不问就换掉。
    # A project nobody edited, layers and file included, is replaced without asking.
    target = tmp_path / "a.qgs"
    target.write_text("<qgis/>", encoding="utf-8")
    loaded = FakeProject(layers={"a": FakeLayer()}, file_name=str(target))
    result = project.ProjectOpen().execute_main(
        context(loaded), project.ProjectOpenParams(path=str(target))
    )
    assert loaded.read_path == str(target)
    assert result.file_path == str(target)


def test_open_clears_the_project_before_reading(qgis: types.ModuleType, tmp_path: Path) -> None:
    # GUI 的 ``addProject`` 先 ``closeProject()`` 再 ``read()``。
    # The GUI's ``addProject`` calls ``closeProject()`` before ``read()``.
    target = tmp_path / "a.qgs"
    target.write_text("<qgis/>", encoding="utf-8")
    loaded = FakeProject(layers={"a": FakeLayer()}, file_name=str(target))
    project.ProjectOpen().execute_main(context(loaded), project.ProjectOpenParams(path=str(target)))
    assert loaded.clears == 1


def test_open_reports_a_failed_read_with_the_project_cleared(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    target = tmp_path / "a.qgs"
    target.write_text("<qgis/>", encoding="utf-8")
    loaded = FakeProject(layers={"a": FakeLayer()}, read_result=False)
    error = refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            context(loaded), project.ProjectOpenParams(path=str(target))
        ),
    )
    assert "cleared" in error.message
    assert loaded.clears == 1
    assert loaded.mapLayers() == {}


def test_open_counts_the_layers_that_cannot_load(qgis: types.ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "a.qgs"
    target.write_text("<qgis/>", encoding="utf-8")
    loaded = FakeProject(
        layers_after_read={"a": FakeLayer(), "b": FakeLayer(valid=False)},
        crs_after_read="EPSG:3857",
    )
    result = project.ProjectOpen().execute_main(
        context(loaded), project.ProjectOpenParams(path=str(target))
    )
    assert (result.layer_count, result.invalid_layer_count) == (2, 1)
    assert result.crs_authid == "EPSG:3857"


def test_a_project_nested_up_to_the_bound_is_still_read(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    target = nested_project(project_dir(tmp_path), "deep.qgs", project.MAX_PROJECT_DEPTH - 1)
    loaded = FakeProject()
    project.ProjectOpen().execute_main(context(loaded), project.ProjectOpenParams(path=str(target)))
    assert loaded.read_path == str(target)


def test_a_deeper_project_is_refused_before_it_is_read(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # P3 防死：深度超过上限的工程会让 ``read()`` 栈溢出杀掉进程（本机实测 6000 层
    # 0xC00000FD，5500 层 0.10 s 读完），所以这一条比 GUI 严，而且拒在 ``read()`` 之前。
    # P3 anti-death: a project past the bound makes ``read()`` overflow the stack and end the
    # process (measured here: 6000 levels 0xC00000FD, 5500 levels read in 0.10 s), so this one
    # criterion is stricter than the GUI and it refuses before ``read()``.
    target = nested_project(project_dir(tmp_path), "deep.qgs", project.MAX_PROJECT_DEPTH + 1)
    loaded = FakeProject()
    error = refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            context(loaded), project.ProjectOpenParams(path=str(target))
        ),
    )
    assert "nests deeper" in error.message
    assert loaded.reads == 0
    # 拒在清空之前：一个会被拒的文件不该把当前工程拿走。
    # Refused before the clear: a file that is going to be refused must not take the project.
    assert loaded.clears == 0


def test_depth_counts_the_qgs_member_of_an_archive(qgis: types.ModuleType, tmp_path: Path) -> None:
    inner = (
        b"<qgis>"
        + b"<a>" * (project.MAX_PROJECT_DEPTH + 1)
        + b"</a>" * (project.MAX_PROJECT_DEPTH + 1)
        + b"</qgis>"
    )
    target = archive(project_dir(tmp_path), "deep.qgz", inner)
    loaded = FakeProject()
    refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            context(loaded), project.ProjectOpenParams(path=str(target))
        ),
    )
    assert loaded.reads == 0


def test_an_encrypted_archive_member_is_refused_before_it_is_read(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # P3 防死：加密成员让真 QGIS 3.44 在 ``read()`` 里以 0xC0000005 结束进程（本机实测）。
    # P3 anti-death: an encrypted member ends real QGIS 3.44 with 0xC0000005 inside ``read()``
    # (measured here).
    target = archive(project_dir(tmp_path), "a.qgz", b"<qgis/>")
    set_encryption_flag(target)
    loaded = FakeProject()
    refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            context(loaded), project.ProjectOpenParams(path=str(target))
        ),
    )
    assert loaded.reads == 0


@pytest.mark.parametrize("shape", ["crc", "bzip2", "entities", "not xml at all"])
def test_a_shape_qgis_reads_is_not_refused_by_the_scan(
    qgis: types.ModuleType, tmp_path: Path, shape: str
) -> None:
    # 这些形状都实测过：真 QGIS 3.44 照读（CRC 错 ok=True、bzip2 成员 ok=True、1 MiB 实体引用
    # 50 次 0.53 s ok=True），所以扫描不许比 GUI 严。
    # Every one of these was measured: real QGIS 3.44 reads it (broken CRC ok=True, bzip2 member
    # ok=True, a 1 MiB entity referenced 50 times in 0.53 s ok=True), so the scan may not be
    # stricter than the GUI.
    directory = project_dir(tmp_path)
    if shape == "crc":
        target = archive(directory, "a.qgz", b"<qgis/>")
        break_member_crc(target)
    elif shape == "bzip2":
        target = archive(directory, "a.qgz", b"<qgis/>", method=zipfile.ZIP_BZIP2)
    elif shape == "entities":
        target = directory / "a.qgs"
        target.write_text(
            '<!DOCTYPE qgis [<!ENTITY big "'
            + "x" * (1024 * 1024)
            + '">]><qgis><properties>'
            + "<note>&big;</note>" * 50
            + "</properties></qgis>",
            encoding="utf-8",
        )
    else:
        target = directory / "a.qgs"
        target.write_bytes(b"\x00\x01not xml at all")
    loaded = FakeProject()
    project.ProjectOpen().execute_main(context(loaded), project.ProjectOpenParams(path=str(target)))
    # 扫描读不懂就交给 ``read()``，它不是判据本身。
    # A file the scan cannot read goes to ``read()``; the scan is not a criterion by itself.
    assert loaded.read_path == str(target)


def test_a_corrupt_archive_is_left_to_read(qgis: types.ModuleType, tmp_path: Path) -> None:
    target = project_dir(tmp_path) / "a.qgz"
    target.write_bytes(b"not a zip")
    loaded = FakeProject(read_result=False)
    refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            context(loaded), project.ProjectOpenParams(path=str(target))
        ),
    )
    assert loaded.read_path == str(target)


# --- project.save：保存工程 / saving a project -----------------------------


def test_save_without_a_path_or_associated_file_is_refused(qgis: types.ModuleType) -> None:
    loaded = FakeProject()
    refused(
        PROJECT_PATH_REQUIRED,
        lambda: project.ProjectSave().execute_main(context(loaded), project.ProjectSaveParams()),
    )


def test_save_as_refuses_to_overwrite_an_existing_target(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    target = tmp_path / "a.qgs"
    target.write_text("older", encoding="utf-8")
    loaded = FakeProject()
    refused(
        TARGET_EXISTS,
        lambda: project.ProjectSave().execute_main(
            context(loaded), project.ProjectSaveParams(path=str(target))
        ),
    )
    assert loaded.writes == []
    assert target.read_text(encoding="utf-8") == "older"


def test_save_as_overwrites_when_the_caller_says_so(qgis: types.ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "a.qgs"
    target.write_text("older", encoding="utf-8")
    loaded = FakeProject()
    project.ProjectSave().execute_main(
        context(loaded), project.ProjectSaveParams(path=str(target), overwrite=True)
    )
    assert loaded.writes == [str(target)]


def test_save_writes_the_associated_file_without_an_overwrite_flag(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # GUI 的 Save 不弹对话框、不问覆盖：它直接写关联文件。
    # The GUI's Save shows no dialog and asks nothing about overwriting: it writes the associated
    # file directly.
    target = tmp_path / "a.qgz"
    target.write_text("older", encoding="utf-8")
    loaded = FakeProject(file_name=str(target))
    result = project.ProjectSave().execute_main(context(loaded), project.ProjectSaveParams())
    assert loaded.writes == [str(target)]
    assert result.file_path == str(target)


def test_save_reports_the_dirty_state_qgis_leaves(qgis: types.ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "a.qgs"
    loaded = FakeProject(dirty=True)
    result = project.ProjectSave().execute_main(
        context(loaded), project.ProjectSaveParams(path=str(target))
    )
    assert result.is_dirty is False


def test_save_reports_a_failed_write(qgis: types.ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "a.qgs"
    loaded = FakeProject(write_result=False)
    refused(
        PROJECT_SAVE_FAILED,
        lambda: project.ProjectSave().execute_main(
            context(loaded), project.ProjectSaveParams(path=str(target))
        ),
    )


def test_save_refuses_a_relative_associated_file(qgis: types.ModuleType) -> None:
    loaded = FakeProject(file_name="maps/a.qgs")
    refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectSave().execute_main(context(loaded), project.ProjectSaveParams()),
    )


# --- project.get_info：看工程 / reading the project -----------------------


def test_get_info_reports_the_project_and_the_lifecycle_epoch() -> None:
    loaded = FakeProject(
        layers={"a": FakeLayer(), "b": FakeLayer(valid=False)},
        file_name="C:/maps/a.qgz",
        dirty=True,
        crs_authid="EPSG:3857",
        title="Roads of Q1",
    )
    result = project.ProjectGetInfo().execute_main(
        context(loaded, epoch=7), project.ProjectGetInfoParams()
    )
    assert result.file_path == "C:/maps/a.qgz"
    assert result.crs_authid == "EPSG:3857"
    assert result.is_dirty is True
    assert result.layer_count == 2
    # 标题栏显示的就是它：没设置时是空串，不是一个缺字段。
    # This is what the window title shows: an empty string when unset, not a missing field.
    assert result.title == "Roads of Q1"
    assert result.project_epoch == 7


def test_get_info_reports_no_file_for_an_unsaved_project() -> None:
    loaded = FakeProject()
    result = project.ProjectGetInfo().execute_main(context(loaded), project.ProjectGetInfoParams())
    assert result.file_path is None
    assert result.title == ""


def test_get_info_does_not_write_or_replace_anything() -> None:
    loaded = FakeProject(layers={"a": FakeLayer()}, file_name="C:/maps/a.qgs", dirty=True)
    project.ProjectGetInfo().execute_main(context(loaded), project.ProjectGetInfoParams())
    assert (loaded.writes, loaded.reads, loaded.clears) == ([], 0, 0)
    assert loaded.dirty is True


# --- 声明 / declarations ---------------------------------------------------


def test_the_four_operations_declare_the_agreed_surface() -> None:
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
