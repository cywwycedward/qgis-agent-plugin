"""用假 ctx 与假 PyQGIS 驱动 ``project.*`` 操作。

真实 PyQGIS 的行为（``read()`` 填不填图层树、多少层会打掉进程、``write()`` 写什么）留给 QGIS 侧的
验收跑；这里测的是操作的判断与它对 QGIS 的调用顺序：换工程前问什么、读之前拒什么、失败时把 QGIS
自己的哪句文本交出去。

The ``project.*`` operations, driven by a fake ctx and a fake PyQGIS.  Real PyQGIS behaviour --
whether ``read()`` fills the layer tree, how much nesting ends the process, what ``write()``
writes -- belongs to the QGIS acceptance run; what is measured here is each operation's criteria
and the order of its QGIS calls: what is asked before a project is replaced, what is refused
before a read, and which of QGIS's own sentences comes back on failure.
"""

from __future__ import annotations

import stat
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
# 绝对路径且格式正确，从不真的碰它：只有输入模型看得到它。
# Absolute and well formed, never touched: only the input models see it.
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


class FakeVectorLayer:
    """``QgsVectorLayer`` 在判据里被问到的那几个方法，于是 ``isinstance`` 也对得上。

    The ``QgsVectorLayer`` methods the criteria ask about, so ``isinstance`` matches too.
    """

    def __init__(
        self,
        *,
        name: str = "layer",
        valid: bool = True,
        provider: str = "ogr",
        editable: bool = False,
        modified: bool = False,
        feature_count: int = 0,
        temporary: bool = False,
        skip_memory_check: bool = False,
    ) -> None:
        self._name = name
        self._valid = valid
        self._provider = provider
        self._editable = editable
        self._modified = modified
        self._feature_count = feature_count
        self._temporary = temporary
        self._skip_memory_check = skip_memory_check

    def name(self) -> str:
        return self._name

    def isValid(self) -> bool:  # noqa: N802 - a PyQGIS name
        return self._valid

    def dataProvider(self) -> Any:  # noqa: N802 - a PyQGIS name
        return None if self._provider == "no provider" else object()

    def providerType(self) -> str:  # noqa: N802 - a PyQGIS name
        return self._provider

    def isEditable(self) -> bool:  # noqa: N802 - a PyQGIS name
        return self._editable

    def isModified(self) -> bool:  # noqa: N802 - a PyQGIS name
        return self._modified

    def featureCount(self) -> int:  # noqa: N802 - a PyQGIS name
        return self._feature_count

    def isTemporary(self) -> bool:  # noqa: N802 - a PyQGIS name
        return self._temporary

    def customProperty(self, key: str) -> Any:  # noqa: N802 - a PyQGIS name
        # QGIS 用 ``customProperty(...).toInt()`` 当布尔：设了就是 1。
        # QGIS reads ``customProperty(...).toInt()`` as a flag: set means 1.
        if key == "skipMemoryLayersCheck":
            return int(self._skip_memory_check)
        return None


class FakeRasterAttributeTable:
    def __init__(self, dirty: bool) -> None:
        self._dirty = dirty

    def isDirty(self) -> bool:  # noqa: N802 - a PyQGIS name
        return self._dirty


class FakeRasterLayer:
    """``QgsRasterLayer`` 的波段与属性表：``dirty_bands`` 里的那条带是脏的。

    The bands and attribute tables of a ``QgsRasterLayer``: the ones in ``dirty_bands`` are dirty.
    """

    def __init__(
        self,
        *,
        name: str = "raster",
        band_count: int = 1,
        dirty_bands: tuple[int, ...] = (),
        provider: str = "gdal",
        temporary: bool = False,
    ) -> None:
        self._name = name
        self._band_count = band_count
        self._dirty_bands = frozenset(dirty_bands)
        self._provider = provider
        self._temporary = temporary

    def name(self) -> str:
        return self._name

    def isValid(self) -> bool:  # noqa: N802 - a PyQGIS name
        return True

    def providerType(self) -> str:  # noqa: N802 - a PyQGIS name
        return self._provider

    def isTemporary(self) -> bool:  # noqa: N802 - a PyQGIS name
        return self._temporary

    def bandCount(self) -> int:  # noqa: N802 - a PyQGIS name
        return self._band_count

    def attributeTable(self, band: int) -> FakeRasterAttributeTable | None:  # noqa: N802
        if band in self._dirty_bands:
            return FakeRasterAttributeTable(True)
        return None


FakeMapLayer = FakeVectorLayer | FakeRasterLayer


class FakeViewSettings:
    """工程默认视图：存进来的范围与旋转。

    The project's default view: the extent and rotation stored into it.
    """

    def __init__(self) -> None:
        self.extent: Any = None
        self.rotation: Any = None

    def setDefaultViewExtent(self, extent: Any) -> None:  # noqa: N802 - a PyQGIS name
        self.extent = extent

    def setDefaultRotation(self, rotation: float) -> None:  # noqa: N802 - a PyQGIS name
        self.rotation = rotation


class FakeReferencedRectangle:
    """``QgsReferencedRectangle``：范围带上工程的 CRS。

    ``QgsReferencedRectangle``: an extent together with the project's CRS.
    """

    def __init__(self, rectangle: Any, crs: FakeCrs) -> None:
        self.rectangle = rectangle
        self.crs = crs


class FakeProject:
    """``QgsProject`` 里这四个操作碰得到的那几个方法。

    The handful of ``QgsProject`` methods these four operations touch.
    """

    def __init__(
        self,
        *,
        layers: dict[str, FakeMapLayer] | None = None,
        file_name: str = "",
        dirty: bool = False,
        crs_authid: str = "EPSG:4326",
        title: str = "",
        read_result: bool = True,
        read_error: str = "Unable to open the project file",
        layers_after_read: dict[str, FakeMapLayer] | None = None,
        crs_after_read: str = "EPSG:4326",
        write_result: bool = True,
        write_error: str = "Unable to save to file",
    ) -> None:
        self.layers = dict(layers or {})
        self.file_name = file_name
        self.dirty = dirty
        self.crs_authid = crs_authid
        self.title_text = title
        self.read_result = read_result
        self.read_error = read_error
        self.layers_after_read = dict(layers_after_read or {})
        self.crs_after_read = crs_after_read
        self.write_result = write_result
        self.write_error = write_error
        self.error_text = ""
        self.read_path: str | None = None
        self.reads = 0
        self.clears = 0
        self.writes: list[str] = []
        self.view = FakeViewSettings()
        # ``blockDirtying`` 生效期间置脏被挡住，和真 ``QgsProject`` 一样。
        # While ``blockDirtying`` is in effect a dirtying call is dropped, as in ``QgsProject``.
        self.blocking = 0
        self.dirtied_while_blocked = 0
        # 清空与读取各自记下当时有没有挡：真工程在清空时由图层树发信号叫 ``markDirty``。
        # 没走到时是 -1。
        # The close and the read each record whether blocking was in effect: at close time the real
        # project is dirtied by signals from the layer tree.  -1 means the step never ran.
        self.blocked_at_clear = -1
        self.blocked_at_read = -1

    def mapLayers(self) -> dict[str, FakeMapLayer]:  # noqa: N802 - a PyQGIS name
        return dict(self.layers)

    def fileName(self) -> str:  # noqa: N802 - a PyQGIS name
        return self.file_name

    def isDirty(self) -> bool:  # noqa: N802 - a PyQGIS name
        return self.dirty

    def setDirty(self, dirty: bool) -> None:  # noqa: N802 - a PyQGIS name
        if dirty and self.blocking:
            self.dirtied_while_blocked += 1
            return
        self.dirty = dirty

    def title(self) -> str:
        return self.title_text

    def crs(self) -> FakeCrs:
        return FakeCrs(self.crs_authid)

    def viewSettings(self) -> FakeViewSettings:  # noqa: N802 - a PyQGIS name
        return self.view

    def error(self) -> str:
        return self.error_text

    def clear(self) -> None:
        self.clears += 1
        self.layers = {}
        self.file_name = ""
        self.crs_authid = ""
        self.title_text = ""
        self.blocked_at_clear = self.blocking
        # 清空会经由图层树与画布把工程标脏；挡板在时就吞掉。
        # Closing the project dirties it through the layer tree and the canvas; the blocker swallows
        # that when it is in effect.
        self.setDirty(True)

    def read(self, path: str) -> bool:
        self.reads += 1
        self.read_path = path
        self.blocked_at_read = self.blocking
        # 真 ``read()`` 先记住文件名，再决定读不读得出来。
        # The real ``read()`` remembers the file name first, then decides.
        self.file_name = path
        if not self.read_result:
            self.error_text = self.read_error
            return False
        self.layers = dict(self.layers_after_read)
        self.crs_authid = self.crs_after_read
        # 读的过程中会置脏的东西：真 ``read()`` 里有一堆，这里一个代表它们。
        # Things that dirty during a read: the real ``read()`` has plenty, one stands for them.
        self.setDirty(True)
        return True

    def write(self, path: str) -> bool:
        self.writes.append(path)
        if not self.write_result:
            self.error_text = self.write_error
            return False
        self.file_name = path
        self.dirty = False
        return True


class FakeCanvas:
    def __init__(self) -> None:
        self.extent_value = "the canvas extent"
        self.rotation_value = 12.5

    def extent(self) -> Any:
        return self.extent_value

    def rotation(self) -> float:
        return self.rotation_value


class FakeIface:
    def __init__(
        self,
        project_: FakeProject | None = None,
        *,
        result: bool = True,
        new_crs: str = "EPSG:3857",
        new_layers: dict[str, FakeMapLayer] | None = None,
    ) -> None:
        self.project = project_
        self.result = result
        # 真 ``fileNew`` 的 CRS 来自设置，默认模板还会带图层进来。
        # The real ``fileNew`` takes its CRS from a setting, and the default template may bring
        # layers along.
        self.new_crs = new_crs
        self.new_layers = dict(new_layers or {})
        self.new_project_calls: list[bool] = []
        self.canvas = FakeCanvas()

    def newProject(self, promptToSaveFlag: bool = False) -> bool:  # noqa: N802 - a PyQGIS name
        self.new_project_calls.append(promptToSaveFlag)
        if self.result and self.project is not None:
            self.project.clear()
            self.project.crs_authid = self.new_crs
            self.project.layers = dict(self.new_layers)
            # 真 ``fileNew`` 最后一步是 ``setDirty(false)``。
            # The real ``fileNew`` ends with ``setDirty(false)``.
            self.project.setDirty(False)
        return self.result

    def mapCanvas(self) -> FakeCanvas:  # noqa: N802 - a PyQGIS name
        return self.canvas


class FakeTask:
    def __init__(self, description: str) -> None:
        self._description = description

    def description(self) -> str:
        return self._description


class FakeTaskManager:
    """``QgsApplication.taskManager()``：按图层回答依赖它的任务。

    ``QgsApplication.taskManager()``: answers which tasks depend on a layer.
    """

    def __init__(self) -> None:
        self._dependent: dict[int, list[FakeTask]] = {}

    def depend(self, layer: FakeMapLayer, *descriptions: str) -> None:
        self._dependent[id(layer)] = [FakeTask(text) for text in descriptions]

    def tasksDependentOnLayer(self, layer: FakeMapLayer) -> list[FakeTask]:  # noqa: N802
        return list(self._dependent.get(id(layer), []))


class FakeDirtyBlocker:
    def __init__(self, project_: FakeProject) -> None:
        self._project = project_

    def __enter__(self) -> None:
        self._project.blocking += 1

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self._project.blocking -= 1


class FakeQgsProject:
    """只是 ``QgsProject.blockDirtying`` 的宿主。

    Nothing but the host of ``QgsProject.blockDirtying``.
    """

    @staticmethod
    def blockDirtying(project_: FakeProject) -> FakeDirtyBlocker:  # noqa: N802 - a PyQGIS name
        return FakeDirtyBlocker(project_)


class FakeFileInfo:
    """``QFileInfo`` 被问到的两件事：在不在、写不写得进去。

    The two things asked of ``QFileInfo``: whether the file is there and whether it may be written.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def exists(self) -> bool:
        return self._path.exists()

    def isWritable(self) -> bool:  # noqa: N802 - a PyQGIS name
        return bool(self._path.stat().st_mode & stat.S_IWRITE)


@pytest.fixture
def qgis(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """操作在方法体内 import ``qgis``；这就是那几步要的东西。

    The operations import ``qgis`` inside their method body; this is what those steps get.
    """

    core: Any = types.ModuleType("qgis.core")
    core.QgsCoordinateReferenceSystem = FakeCrs
    # 判据按 QGIS 的类名区分矢量与栅格，所以假模块里要有这两个类。
    # The criteria tell vector from raster by QGIS's own class names, so the fake module needs both.
    core.QgsVectorLayer = FakeVectorLayer
    core.QgsRasterLayer = FakeRasterLayer
    core.QgsReferencedRectangle = FakeReferencedRectangle
    core.QgsProject = FakeQgsProject
    core.task_manager = FakeTaskManager()
    core.QgsApplication = types.SimpleNamespace(taskManager=lambda: core.task_manager)
    qtcore: Any = types.ModuleType("qgis.PyQt.QtCore")
    qtcore.QFileInfo = FakeFileInfo
    pyqt: Any = types.ModuleType("qgis.PyQt")
    pyqt.QtCore = qtcore
    package: Any = types.ModuleType("qgis")
    package.core = core
    package.PyQt = pyqt
    monkeypatch.setitem(sys.modules, "qgis", package)
    monkeypatch.setitem(sys.modules, "qgis.core", core)
    monkeypatch.setitem(sys.modules, "qgis.PyQt", pyqt)
    monkeypatch.setitem(sys.modules, "qgis.PyQt.QtCore", qtcore)
    return core


def tasks(qgis: types.ModuleType) -> FakeTaskManager:
    return qgis.task_manager


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


def a_qgs(directory: Path) -> Path:
    target = directory / "a.qgs"
    target.write_text("<qgis/>", encoding="utf-8")
    return target


def nested_project(directory: Path, name: str, depth: int, prefix: str = "a") -> Path:
    """一个良构的工程，根元素下面嵌了 ``depth`` 层。

    A well-formed project nested ``depth`` levels below its root element.
    """

    tag = prefix.encode()
    element = b"<" + tag + b">"
    closing = b"</" + tag + b">"
    target = directory / name
    target.write_bytes(b"<qgis>" + element * depth + closing * depth + b"</qgis>")
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


# --- 输入与输出模型 / input and output models ------------------------------


@pytest.mark.parametrize(
    "model, fields",
    [
        (project.ProjectCreateParams, {"discard_current": False}),
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


def test_create_asks_only_for_the_discard_answer() -> None:
    # 新工程的 CRS 由 QGIS 自己取，所以这里没有 CRS 参数。
    # A new project's CRS is QGIS's own choice, so there is no CRS parameter here.
    assert list(project.ProjectCreateParams.model_fields) == ["discard_current"]
    assert project.ProjectCreateParams.model_fields["discard_current"].default is False


@pytest.mark.parametrize("model", [project.ProjectOpenParams, project.ProjectSaveParams])
def test_an_empty_path_is_refused_by_the_model(model: Any) -> None:
    # 空串不是一条路径，所以在模型边界就拒，不到 ``absolute_path`` 那一步。
    # An empty string is not a path, so it is refused at the model edge, before ``absolute_path``.
    with pytest.raises(ValidationError):
        model(path="")


@pytest.mark.parametrize("raw", ["C:/maps/a.txt", "C:/maps/a.qgs~", "C:/maps/a"])
def test_a_save_target_must_carry_a_project_suffix(raw: str) -> None:
    # 保存对话框按过滤器把名字补成 .qgs/.qgz，所以别的后缀不是它能给出的答案。
    # The save dialog appends .qgs or .qgz by filter, so no other suffix is an answer it can give.
    with pytest.raises(ValidationError):
        project.ProjectSaveParams(path=raw)


def test_a_save_suffix_is_case_insensitive() -> None:
    assert project.ProjectSaveParams(path="C:/maps/a.QGZ").path == "C:/maps/a.QGZ"


@pytest.mark.parametrize("raw", ["a.qgs", "C:maps/a.qgs", "\\a.qgs"])
def test_an_open_path_has_no_shape_rule_beyond_being_a_path(
    qgis: types.ModuleType, raw: str
) -> None:
    # 打开只要求"是个路径"：后缀不判，相对路径由 ``absolute_path`` 拒。
    # Opening only asks for a path: the suffix is not judged and ``absolute_path`` refuses a
    # relative one.
    refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectOpen().execute_main(
            context(FakeProject()), project.ProjectOpenParams(path=raw)
        ),
    )


@pytest.mark.parametrize(
    "model, fields",
    [
        (project.ProjectCreateResult, ["crs_authid", "layer_count"]),
        (
            project.ProjectOpenResult,
            ["file_path", "crs_authid", "layer_count", "invalid_layer_count"],
        ),
        (project.ProjectSaveResult, ["file_path"]),
        (
            project.ProjectGetInfoResult,
            ["file_path", "crs_authid", "is_dirty", "layer_count", "title"],
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
    error = refused(
        PROJECT_REPLACE_REQUIRED,
        lambda: project.ProjectCreate().execute_main(
            context(loaded, iface=iface), project.ProjectCreateParams()
        ),
    )
    assert error.message.endswith("pass discard_current=true to replace it")
    assert iface.new_project_calls == []


@pytest.mark.parametrize(
    "state",
    [
        {"layers": {"a": FakeVectorLayer()}},
        {"file_name": "C:/maps/current.qgs"},
        {"layers": {"a": FakeVectorLayer()}, "file_name": "C:/maps/current.qgs"},
    ],
    ids=["layers", "associated file", "both"],
)
def test_create_replaces_a_clean_project_whatever_it_holds(
    qgis: types.ModuleType, state: dict[str, Any]
) -> None:
    # 这个工程没有一条没保存的东西，所以换它不问；图层数与关联文件名本来就不是判据。
    # Nothing about this project is unsaved, so replacing it asks nothing; the layer count and the
    # associated file name were never criteria.
    loaded = FakeProject(**state)
    iface = FakeIface(loaded)
    result = project.ProjectCreate().execute_main(
        context(loaded, iface=iface), project.ProjectCreateParams()
    )
    assert iface.new_project_calls == [False]
    assert result.layer_count == 0


def test_create_reports_what_qgis_left(qgis: types.ModuleType) -> None:
    # 新工程没有关联文件，CRS 与图层都是 QGIS 给的那些。
    # A new project has no associated file, and its CRS and layers are the ones QGIS gave it.
    loaded = FakeProject()
    iface = FakeIface(loaded, new_crs="EPSG:3857", new_layers={"a": FakeVectorLayer()})
    result = project.ProjectCreate().execute_main(
        context(loaded, iface=iface), project.ProjectCreateParams()
    )
    assert result.crs_authid == "EPSG:3857"
    assert result.layer_count == 1


def test_create_leaves_the_new_project_clean(qgis: types.ModuleType) -> None:
    loaded = FakeProject(dirty=True)
    project.ProjectCreate().execute_main(
        context(loaded), project.ProjectCreateParams(discard_current=True)
    )
    assert loaded.dirty is False


def test_create_is_refused_while_a_task_depends_on_a_layer(
    qgis: types.ModuleType,
) -> None:
    layer = FakeVectorLayer(name="roads")
    loaded = FakeProject(layers={"a": layer})
    tasks(qgis).depend(layer, "Exporting roads")
    iface = FakeIface(loaded)
    error = refused(
        PROJECT_REPLACE_REQUIRED,
        lambda: project.ProjectCreate().execute_main(
            context(loaded, iface=iface), project.ProjectCreateParams(discard_current=True)
        ),
    )
    # 说的是 QGIS 自己那句话，连任务描述一起给出：调用方能做的只有取消它们再试。
    # It is QGIS's own sentence with the task descriptions: the only move left is to cancel them.
    assert error.message.startswith(
        "The following tasks are currently running which depend on layers in this project:"
    )
    assert " • Exporting roads" in error.message
    assert error.message.endswith("Please cancel these tasks and retry.")
    assert iface.new_project_calls == []


def test_create_reports_a_new_project_qgis_refused(qgis: types.ModuleType) -> None:
    loaded = FakeProject()
    iface = FakeIface(loaded, result=False)
    error = refused(
        INTERNAL_ERROR,
        lambda: project.ProjectCreate().execute_main(
            context(loaded, iface=iface), project.ProjectCreateParams()
        ),
    )
    # 这条路下 QGIS 只在有任务依赖图层时拒绝，所以消息必须让调用方知道该做什么。
    # On this path QGIS refuses only over a task depending on a layer, so the message has to say
    # what to do about it.
    assert "cancel any task that depends on a layer and retry" in error.message


# --- project.open：打开工程 / opening a project ----------------------------


def test_open_refuses_a_project_with_unsaved_changes(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    target = a_qgs(project_dir(tmp_path))
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
    target = a_qgs(project_dir(tmp_path))
    loaded = FakeProject(layers={"a": FakeVectorLayer()}, file_name=str(target))
    result = project.ProjectOpen().execute_main(
        context(loaded), project.ProjectOpenParams(path=str(target))
    )
    assert loaded.read_path == str(target)
    assert result.file_path == str(target)


def test_open_clears_the_project_before_reading(qgis: types.ModuleType, tmp_path: Path) -> None:
    # QGIS 的 ``addProject`` 先关掉当前工程再读，于是读失败时眼前这个是空的。
    # QGIS's ``addProject`` closes the current project before reading, so a failed read leaves an
    # empty one.
    target = a_qgs(project_dir(tmp_path))
    loaded = FakeProject(layers={"a": FakeVectorLayer()})
    project.ProjectOpen().execute_main(context(loaded), project.ProjectOpenParams(path=str(target)))
    assert loaded.clears == 1


def test_open_closes_and_reads_with_dirtying_blocked(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 关掉旧工程与读取都在挡板里：清空会经由图层树把工程标脏，而读失败时 GUI 留下的工程是干净的。
    # Both the close and the read run with dirtying blocked: closing dirties the project through the
    # layer tree, and a failed read leaves a clean project.
    target = a_qgs(project_dir(tmp_path))
    loaded = FakeProject()
    project.ProjectOpen().execute_main(context(loaded), project.ProjectOpenParams(path=str(target)))
    assert loaded.blocked_at_clear >= 1
    assert loaded.blocked_at_read >= 1
    assert loaded.dirtied_while_blocked >= 2
    assert loaded.dirty is False


def test_a_failed_open_leaves_a_clean_project(qgis: types.ModuleType, tmp_path: Path) -> None:
    target = a_qgs(project_dir(tmp_path))
    loaded = FakeProject(read_result=False)
    refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            context(loaded), project.ProjectOpenParams(path=str(target))
        ),
    )
    # 读失败时眼前这个工程是空的，也是干净的；否则下一次替换会多拒一次。
    # A failed read leaves an empty project, and a clean one; otherwise the next replacement would
    # be refused for nothing.
    assert loaded.mapLayers() == {}
    assert loaded.dirty is False


@pytest.mark.parametrize(
    "state",
    [
        # 1 非 memory 的矢量图层带着没提交的编辑，而工程自己没有脏标记：``saveDirty`` 就是为这个
        # 缺口存在的。
        # 1 A vector layer holds uncommitted edits while the project carries no dirty flag: that
        # gap is what ``saveDirty`` exists for.
        {"layers": {"a": FakeVectorLayer(editable=True, modified=True)}},
        # 2 带要素的 memory 图层。
        # 2 A memory layer holding features.
        {"layers": {"a": FakeVectorLayer(provider="memory", feature_count=3)}},
        # 3 临时图层。
        # 3 A temporary layer.
        {"layers": {"a": FakeVectorLayer(temporary=True)}},
        # 4 栅格属性表脏了：两带，第一带脏。
        # 4 A dirty raster attribute table: two bands, the first one dirty.
        {"layers": {"a": FakeRasterLayer(band_count=2, dirty_bands=(1,))}},
    ],
    ids=["layer edits", "memory layer", "temporary layer", "raster attribute table"],
)
def test_open_asks_the_four_questions(
    qgis: types.ModuleType, tmp_path: Path, state: dict[str, Any]
) -> None:
    target = a_qgs(project_dir(tmp_path))
    loaded = FakeProject(**state)
    assert loaded.isDirty() is False, "each state must be clean at the project level"
    error = refused(
        PROJECT_REPLACE_REQUIRED,
        lambda: project.ProjectOpen().execute_main(
            context(loaded), project.ProjectOpenParams(path=str(target))
        ),
    )
    assert error.message.endswith("pass discard_current=true to replace it")
    # 没有回答时什么都不能动：既没读，也没清。
    # Nothing may be touched while the question is unanswered: no read, no clear.
    assert loaded.reads == 0
    assert loaded.clears == 0


@pytest.mark.parametrize(
    "state",
    [
        {"layers": {"a": FakeVectorLayer(editable=True, modified=True)}},
        {"layers": {"a": FakeVectorLayer(provider="memory", feature_count=3)}},
        {"layers": {"a": FakeRasterLayer(band_count=2, dirty_bands=(1,))}},
    ],
    ids=["layer edits", "memory layer", "raster attribute table"],
)
def test_create_asks_the_same_questions(qgis: types.ModuleType, state: dict[str, Any]) -> None:
    loaded = FakeProject(**state)
    iface = FakeIface(loaded)
    refused(
        PROJECT_REPLACE_REQUIRED,
        lambda: project.ProjectCreate().execute_main(
            context(loaded, iface=iface), project.ProjectCreateParams()
        ),
    )
    assert iface.new_project_calls == []


def test_a_discard_answer_replaces_whatever_the_questions_found(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    target = a_qgs(project_dir(tmp_path))
    loaded = FakeProject(
        layers={
            "a": FakeVectorLayer(editable=True, modified=True),
            "b": FakeVectorLayer(provider="memory", feature_count=3),
            "c": FakeRasterLayer(band_count=2, dirty_bands=(1,)),
        }
    )
    result = project.ProjectOpen().execute_main(
        context(loaded), project.ProjectOpenParams(path=str(target), discard_current=True)
    )
    assert result.file_path == str(target)
    assert loaded.read_path == str(target)


def test_a_dirty_table_on_the_top_band_is_not_asked_about(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # QGIS 的循环上界是 ``bandCount()``，最高那一带它不问；这里问就是比它严。
    # QGIS loops up to ``bandCount()``, so the top band is never asked about; asking here would be
    # stricter than it is.
    target = a_qgs(project_dir(tmp_path))
    loaded = FakeProject(layers={"a": FakeRasterLayer(band_count=2, dirty_bands=(2,))})
    project.ProjectOpen().execute_main(context(loaded), project.ProjectOpenParams(path=str(target)))
    assert loaded.read_path == str(target)


def test_a_scratch_layer_without_features_is_not_asked_about(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # ``featureCount() != 0`` 是判据：空 scratch 图层不问。
    # ``featureCount() != 0`` is the criterion: an empty scratch layer is not asked about.
    target = a_qgs(project_dir(tmp_path))
    loaded = FakeProject(layers={"a": FakeVectorLayer(provider="memory", feature_count=0)})
    project.ProjectOpen().execute_main(context(loaded), project.ProjectOpenParams(path=str(target)))
    assert loaded.read_path == str(target)


def test_a_memory_layer_that_skips_the_check_is_not_asked_about(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # ``skipMemoryLayersCheck`` 那个自定义属性就是给"别问我"留的开关。
    # The ``skipMemoryLayersCheck`` custom property is the switch left for "do not ask me".
    target = a_qgs(project_dir(tmp_path))
    loaded = FakeProject(
        layers={"a": FakeVectorLayer(provider="memory", feature_count=3, skip_memory_check=True)}
    )
    project.ProjectOpen().execute_main(context(loaded), project.ProjectOpenParams(path=str(target)))
    assert loaded.read_path == str(target)


def test_a_layer_that_is_editable_but_unmodified_is_not_asked_about(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 判据是 ``isEditable() && isModified()``：开了编辑但什么都没改，不问。
    # The criterion is ``isEditable() && isModified()``: an edit session with no change is not
    # asked about.
    target = a_qgs(project_dir(tmp_path))
    loaded = FakeProject(layers={"a": FakeVectorLayer(editable=True, modified=False)})
    project.ProjectOpen().execute_main(context(loaded), project.ProjectOpenParams(path=str(target)))
    assert loaded.read_path == str(target)


def test_a_vector_layer_without_a_provider_is_not_asked_about(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    target = a_qgs(project_dir(tmp_path))
    loaded = FakeProject(
        layers={"a": FakeVectorLayer(provider="no provider", editable=True, modified=True)}
    )
    project.ProjectOpen().execute_main(context(loaded), project.ProjectOpenParams(path=str(target)))
    assert loaded.read_path == str(target)


def test_open_reports_the_error_qgis_gave(qgis: types.ModuleType, tmp_path: Path) -> None:
    target = a_qgs(project_dir(tmp_path))
    loaded = FakeProject(
        layers={"a": FakeVectorLayer()},
        read_result=False,
        read_error="Unable to unzip file 'C:/maps/a.qgz'",
    )
    error = refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            context(loaded), project.ProjectOpenParams(path=str(target))
        ),
    )
    # 读失败的原因由 QGIS 自己拼，操作不另写一句。
    # QGIS composes why the read failed; the operation writes no sentence of its own.
    assert error.message == "Unable to unzip file 'C:/maps/a.qgz'"
    assert loaded.clears == 1
    assert loaded.mapLayers() == {}


def test_open_reports_the_layers_that_could_not_load(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    target = a_qgs(project_dir(tmp_path))
    loaded = FakeProject(
        layers_after_read={"a": FakeVectorLayer(), "b": FakeVectorLayer(valid=False)},
        crs_after_read="EPSG:3857",
    )
    result = project.ProjectOpen().execute_main(
        context(loaded), project.ProjectOpenParams(path=str(target))
    )
    assert (result.layer_count, result.invalid_layer_count) == (2, 1)
    assert result.crs_authid == "EPSG:3857"


def test_open_is_refused_while_a_task_depends_on_a_layer(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    target = a_qgs(project_dir(tmp_path))
    layer = FakeVectorLayer()
    loaded = FakeProject(layers={"a": layer})
    tasks(qgis).depend(layer, "Exporting")
    error = refused(
        PROJECT_REPLACE_REQUIRED,
        lambda: project.ProjectOpen().execute_main(
            context(loaded), project.ProjectOpenParams(path=str(target), discard_current=True)
        ),
    )
    assert " • Exporting" in error.message
    # 拒在碰当前工程之前。
    # Refused before the current project is touched.
    assert (loaded.reads, loaded.clears) == (0, 0)


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
    # 过了上限就会让 ``read()`` 以栈溢出结束进程，所以这一条比 QGIS 严，而且拒在读之前。
    # Past the bound ``read()`` overflows the stack and ends the process, so this one criterion is
    # stricter than QGIS and it refuses before the read.
    target = nested_project(project_dir(tmp_path), "deep.qgs", project.MAX_PROJECT_DEPTH + 1)
    loaded = FakeProject()
    error = refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            context(loaded), project.ProjectOpenParams(path=str(target))
        ),
    )
    assert "nests deeper" in error.message
    # 拒在清空之前，所以消息说的是工程没被动过。
    # Refused before the clear, so the message says the project was not touched.
    assert "not changed" in error.message
    assert (loaded.reads, loaded.clears) == (0, 0)


@pytest.mark.parametrize("prefix", ["a", "q:a", "qgs:a"])
def test_depth_counts_every_element_whatever_its_prefix(
    qgis: types.ModuleType, tmp_path: Path, prefix: str
) -> None:
    # 前缀不声明的标签也是 DOM 里的一层（``QDomDocument`` 不开命名空间），所以它也得被数到；
    # 不数它，一个 ``<q:a>`` 的工程就绕过上限。
    # A tag with an undeclared prefix is one level in the DOM too (``QDomDocument`` does not enable
    # namespaces), so it has to be counted; not counting it lets a ``<q:a>`` project past the bound.
    depth = project.MAX_PROJECT_DEPTH + 1
    target = nested_project(project_dir(tmp_path), "deep.qgs", depth, prefix=prefix)
    refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            context(FakeProject()), project.ProjectOpenParams(path=str(target))
        ),
    )


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


def test_only_the_qgs_member_read_parses_counts_for_depth(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # 归档里第一个 ``.qgs`` 才是 ``read()`` 解析的那个文档；第二个再深也进不了 DOM。
    # The first ``.qgs`` in an archive is the document ``read()`` parses; a second one never reaches
    # the DOM however deep it is.
    directory = project_dir(tmp_path)
    target = directory / "two projects.qgz"
    deep = (
        b"<qgis>"
        + b"<a>" * (project.MAX_PROJECT_DEPTH + 1)
        + b"</a>" * (project.MAX_PROJECT_DEPTH + 1)
        + b"</qgis>"
    )
    with zipfile.ZipFile(target, "w") as handle:
        handle.writestr("first.qgs", b"<qgis/>")
        handle.writestr("second.qgs", deep)
    loaded = FakeProject()
    project.ProjectOpen().execute_main(context(loaded), project.ProjectOpenParams(path=str(target)))
    assert loaded.read_path == str(target)


def test_an_encrypted_archive_member_is_refused_before_it_is_read(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    target = archive(project_dir(tmp_path), "a.qgz", b"<qgis/>")
    set_encryption_flag(target)
    loaded = FakeProject(layers={"a": FakeVectorLayer()})
    error = refused(
        PROJECT_OPEN_FAILED,
        lambda: project.ProjectOpen().execute_main(
            context(loaded), project.ProjectOpenParams(path=str(target))
        ),
    )
    assert "encrypted" in error.message
    assert "not changed" in error.message
    assert (loaded.reads, loaded.clears) == (0, 0)


@pytest.mark.parametrize("shape", ["crc", "bzip2", "entities", "not xml at all"])
def test_a_shape_qgis_reads_is_not_refused_by_the_scan(
    qgis: types.ModuleType, tmp_path: Path, shape: str
) -> None:
    # 这些形状 ``read()`` 都读得动（错 CRC、bzip2 成员、实体声明），所以扫描不许比它严。
    # ``read()`` gets through all of these (a broken CRC, a bzip2 member, an entity declaration), so
    # the scan may not be stricter than it is.
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
    # 扫描读不懂就交给 ``read()``：它不是判据本身。
    # A file the scan cannot read goes to ``read()``: the scan is not a criterion by itself.
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
    target = a_qgs(project_dir(tmp_path))
    loaded = FakeProject()
    refused(
        TARGET_EXISTS,
        lambda: project.ProjectSave().execute_main(
            context(loaded), project.ProjectSaveParams(path=str(target))
        ),
    )
    assert loaded.writes == []


def test_save_as_overwrites_when_the_caller_says_so(qgis: types.ModuleType, tmp_path: Path) -> None:
    target = a_qgs(project_dir(tmp_path))
    loaded = FakeProject()
    project.ProjectSave().execute_main(
        context(loaded), project.ProjectSaveParams(path=str(target), overwrite=True)
    )
    assert loaded.writes == [str(target)]


def test_save_writes_the_associated_file_without_an_overwrite_flag(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # Save 不问覆盖：它写关联文件，覆盖确认只在保存对话框里出现。
    # Save asks nothing about overwriting: it writes the associated file, and the confirmation only
    # happens in the save dialog.
    target = a_qgs(project_dir(tmp_path))
    loaded = FakeProject(file_name=str(target))
    result = project.ProjectSave().execute_main(context(loaded), project.ProjectSaveParams())
    assert loaded.writes == [str(target)]
    assert result.file_path == str(target)


def test_save_stores_the_current_view_but_save_as_does_not(
    qgis: types.ModuleType, tmp_path: Path
) -> None:
    # Save 把当前的画布范围与旋转存进工程，重开时回到这里；Save As 不写这两项。
    # Save stores the current canvas extent and rotation in the project so reopening returns to
    # them; Save As writes neither.
    directory = project_dir(tmp_path)
    associated = a_qgs(directory)
    loaded = FakeProject(file_name=str(associated))
    iface = FakeIface(loaded)
    project.ProjectSave().execute_main(context(loaded, iface=iface), project.ProjectSaveParams())
    assert loaded.view.extent.rectangle == iface.canvas.extent_value
    assert loaded.view.rotation == iface.canvas.rotation_value

    fresh = FakeProject()
    another = directory / "b.qgs"
    project.ProjectSave().execute_main(context(fresh), project.ProjectSaveParams(path=str(another)))
    assert (fresh.view.extent, fresh.view.rotation) == (None, None)


def test_save_refuses_an_unwritable_associated_file(qgis: types.ModuleType, tmp_path: Path) -> None:
    # 关联文件不可写时 QGIS 在写之前就说这句，短，而且不带整条路径。
    # When the associated file cannot be written QGIS says this before writing: short, and without
    # the whole path.
    target = a_qgs(project_dir(tmp_path))
    target.chmod(stat.S_IREAD)
    loaded = FakeProject(file_name=str(target))
    error = refused(
        PROJECT_SAVE_FAILED,
        lambda: project.ProjectSave().execute_main(context(loaded), project.ProjectSaveParams()),
    )
    assert error.message == "The project file is not writable."
    assert loaded.writes == []


def test_save_reports_the_error_qgis_gave(qgis: types.ModuleType, tmp_path: Path) -> None:
    target = project_dir(tmp_path) / "b.qgs"
    loaded = FakeProject(write_result=False, write_error="Unable to save to file C:/maps/b.qgs")
    error = refused(
        PROJECT_SAVE_FAILED,
        lambda: project.ProjectSave().execute_main(
            context(loaded), project.ProjectSaveParams(path=str(target))
        ),
    )
    # 写失败的原因也是 QGIS 拼的，包括目标不可写与磁盘写满。
    # QGIS composes why the write failed as well, an unwritable target and a full disk included.
    assert error.message == "Unable to save to file C:/maps/b.qgs"


def test_save_refuses_a_relative_associated_file(qgis: types.ModuleType) -> None:
    loaded = FakeProject(file_name="maps/a.qgs")
    refused(
        PATH_NOT_ALLOWED,
        lambda: project.ProjectSave().execute_main(context(loaded), project.ProjectSaveParams()),
    )


# --- project.get_info：看工程 / reading the project -----------------------


def test_get_info_reports_the_project() -> None:
    loaded = FakeProject(
        layers={"a": FakeVectorLayer(), "b": FakeVectorLayer(valid=False)},
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
    # 工程属性里的标题：没设置时是空串，不是一个缺字段。
    # The title from the project properties: an empty string when unset, not a missing field.
    assert result.title == "Roads of Q1"


def test_get_info_reports_no_file_for_an_unsaved_project() -> None:
    loaded = FakeProject()
    result = project.ProjectGetInfo().execute_main(context(loaded), project.ProjectGetInfoParams())
    assert result.file_path is None
    assert result.title == ""


def test_get_info_does_not_write_or_replace_anything() -> None:
    loaded = FakeProject(layers={"a": FakeVectorLayer()}, file_name="C:/maps/a.qgs", dirty=True)
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
