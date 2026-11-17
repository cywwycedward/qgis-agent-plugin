# 静态分析器自己的解释器里没有 qgis/pydantic，所以本文件只关掉“找不到模块”这一类报告；能不能
# 真的加载由 QGIS 解释器的验收跑证明。
# The analyzer's own interpreter has no qgis/pydantic, so this file turns off only the
# missing-module report; whether it really loads is proven by the QGIS interpreter's run.
# pyright: reportMissingImports=false

"""验收用例：四项 project 操作。

新建、打开、保存与读信息，包括换掉当前工程之前问的那几件事，以及读取之前就被拒的文件形状。

The acceptance cases for the four project operations: create, open, save and get_info,
including the questions asked before a project is replaced and the file shapes refused before
anything is read.
"""

from __future__ import annotations

import time
from pathlib import Path

import fixtures  # pyright: ignore[reportMissingImports]
from harness import Context, equal, require  # pyright: ignore[reportMissingImports]
from helpers import (
    cities_layer,
    error_code,
    project_layer,
    project_layer_ids,
    project_layers,
    reset_project,
    result_of,
)

from qgis_agent_plugin.core.models import (  # pyright: ignore[reportMissingImports]
    PATH_NOT_ALLOWED,
    PROJECT_OPEN_FAILED,
    PROJECT_REPLACE_REQUIRED,
    PROJECT_SAVE_FAILED,
    TARGET_EXISTS,
    ExecutionStatus,
)
from qgis_agent_plugin.operations.project import MAX_PROJECT_DEPTH  # pyright: ignore[reportMissingImports]


def at_15_stateful_replace_refused(ctx: Context) -> str:
    """未保存内容有四道问句；有图层但不属于这四件事时替换照常进行。

    ``QgisApp::fileNew`` 与 ``openProject`` 调的是同一串检查：图层编辑、memory/临时图层、工程脏、
    栅格 RAT。这里把最容易漏的那一条—“图层有未提交编辑而工程 ``is_dirty()`` 仍是 false”—
    在真 QGIS 上造出来，证明它是判据的一部分。

    Unsaved content is four questions, and ``QgisApp::fileNew`` and ``openProject`` call the same
    chain: layer edits, memory/temporary layers, a dirty project, a raster attribute table.  This
    builds the easiest one to miss -- a layer with uncommitted edits while ``is_dirty()`` is still
    false -- in real QGIS, to show it is part of the criterion.
    """

    from qgis.core import QgsProject, QgsVectorLayerUtils  # pyright: ignore[reportMissingImports]

    reset_project(ctx)
    cities_layer(ctx, "at15 cities")
    layers_before = [(item, layer.name()) for item, layer in project_layers().items()]

    QgsProject.instance().setDirty(True)
    record = ctx.run("project.create", {})
    equal(record.status, ExecutionStatus.FAILED, "project.create without discard_current")
    equal(error_code(record), PROJECT_REPLACE_REQUIRED, "the code for an unarmed replacement")
    equal(
        [(item, layer.name()) for item, layer in project_layers().items()],
        layers_before,
        "the project after the refused call",
    )

    # 同一个工程，没被标脏：替换照常进行。
    # The same project, not marked dirty: the replacement goes through.
    QgsProject.instance().setDirty(False)
    unarmed = ctx.run("project.open", {"path": str(ctx.fixtures.project)})
    equal(
        unarmed.status,
        ExecutionStatus.SUCCEEDED,
        "project.open over a clean project that still holds layers",
    )
    equal(len(project_layers()), 2, "the layers the opened project brought")

    # 图层有未提交的编辑，而工程自己没脏：``saveDirty()`` 的 markDirty 分支就是为这个缺口存在的。
    # A layer holds uncommitted edits while the project itself is clean: that gap is exactly what
    # ``saveDirty()``'s markDirty branch exists for.
    reset_project(ctx)
    edited_id = cities_layer(ctx, "at15 edited")
    layer = project_layer(edited_id)
    layer.startEditing()
    # ``createFeature`` 给 fid 一个自动值：cities 的主键是 fid，手动逐位赋属性会错位。
    # ``createFeature`` autogenerates the fid: cities is keyed on fid, and setting attributes
    # positionally would shift them.
    feature = QgsVectorLayerUtils.createFeature(layer)
    feature["name"] = "at15"
    require(layer.addFeature(feature), "the edit was not added to the layer buffer")
    QgsProject.instance().setDirty(False)
    equal(layer.isEditable(), True, "the layer after startEditing()")
    equal(layer.isModified(), True, "the layer after addFeature()")
    equal(QgsProject.instance().isDirty(), False, "the project after setDirty(False)")

    layers_with_edits = [(item, item_layer.name()) for item, item_layer in project_layers().items()]
    edited = ctx.run("project.create", {})
    equal(edited.status, ExecutionStatus.FAILED, "project.create over an edited layer")
    equal(error_code(edited), PROJECT_REPLACE_REQUIRED, "the code for an edited layer")
    edit_error = edited.error
    if edit_error is None:
        raise AssertionError("a failed record carries an error")
    require(
        "a layer with unsaved edits" in edit_error.message,
        f"the message does not name the layer edits: {edit_error.message!r}",
    )
    equal(
        [(item, item_layer.name()) for item, item_layer in project_layers().items()],
        layers_with_edits,
        "the project after the refused call on an edited layer",
    )

    armed = ctx.run("project.create", {"discard_current": True})
    equal(armed.status, ExecutionStatus.SUCCEEDED, "project.create with the discard answer")
    reset_project(ctx)
    return (
        f"dirty flag -> FAILED(PROJECT_REPLACE_REQUIRED) with {len(layers_before)} layer(s) still "
        "registered; same project once clean -> SUCCEEDED; a layer with uncommitted edits while "
        "is_dirty() is false -> FAILED(PROJECT_REPLACE_REQUIRED) naming 'a layer with unsaved "
        "edits'; discard_current=true -> SUCCEEDED"
    )


def at_16_stateful_replace(ctx: Context) -> str:
    reset_project(ctx)
    cities_layer(ctx, "at16 cities")
    from qgis.core import QgsProject  # pyright: ignore[reportMissingImports]

    QgsProject.instance().setDirty(True)
    epoch_before = ctx.lifecycle.current_epoch
    equal(len(project_layers()), 1, "the stateful project before the replacement")

    record = ctx.run("project.create", {"discard_current": True})
    equal(record.status, ExecutionStatus.SUCCEEDED, "an armed project.create")
    equal(result_of(record)["layer_count"], 0, "the layer count in the result")
    equal(len(project_layers()), 0, "the registered layers after the replacement")
    equal(ctx.lifecycle.current_epoch, epoch_before + 1, "the project epoch")
    # 新建完的工程不是脏的：换工程时置脏被挡住，脏标记不会从旧的那个漏过来。
    # A project that was just created is not dirty: dirtying is blocked while the project is
    # replaced, so the old one's flag does not leak into it.
    info = result_of(ctx.run("project.get_info", {}))
    equal(info["is_dirty"], False, "the dirty flag right after project.create")
    equal(info["file_path"], None, "the file of a project that was just created")
    return (
        f"SUCCEEDED: layer_count 1 -> 0, is_dirty false, no associated file, epoch "
        f"{epoch_before} -> {epoch_before + 1}"
    )


def at_17_save_refuses_existing(ctx: Context) -> str:
    from qgis.core import QgsProject  # pyright: ignore[reportMissingImports]

    target = ctx.fixtures.existing_project
    bytes_before = target.read_bytes()

    record = ctx.run("project.save", {"path": str(target)})
    equal(record.status, ExecutionStatus.FAILED, "project.save onto an existing file")
    equal(error_code(record), TARGET_EXISTS, "the code for an existing target")
    equal(target.read_bytes(), bytes_before, "the bytes of the existing target")

    fresh = ctx.fixtures.root / "written by at17.qgs"
    if fresh.exists():
        fresh.unlink()
    saved = ctx.run("project.save", {"path": str(fresh)})
    equal(saved.status, ExecutionStatus.SUCCEEDED, "project.save onto a new file")
    require(fresh.exists(), "no file was written")

    # Save（不给路径）写自己的关联文件，不看覆盖标记。
    # Save (no path) writes the project's own associated file and reads no overwrite flag.
    again = ctx.run("project.save", {})
    equal(again.status, ExecutionStatus.SUCCEEDED, "project.save onto its own associated file")
    # QGIS 记的文件名用正斜杠，比的是同一个路径而不是同一串字符。
    # QGIS remembers the file name with forward slashes, so compare paths, not spellings.
    equal(Path(QgsProject.instance().fileName()), fresh, "the associated file after Save")
    # Save 把当前的画布视图存进工程：重开时回到这里。
    # Save stores the current canvas view in the project, so reopening returns to it.
    stored = QgsProject.instance().viewSettings().defaultViewExtent()
    equal(
        (stored.xMinimum(), stored.yMinimum(), stored.xMaximum(), stored.yMaximum()),
        (0.0, 0.0, 1.0, 1.0),
        "the default view Save stored (this run has no canvas, so the stand-in reports 0..1)",
    )

    # 关联文件不可写时，在写之前就拒，文件一个字节都不变。
    # With an unwritable associated file the refusal comes before the write, and not one byte of the
    # file changes.
    import stat

    bytes_before = fresh.read_bytes()
    fresh.chmod(stat.S_IREAD)
    try:
        unwritable = ctx.run("project.save", {})
    finally:
        fresh.chmod(stat.S_IWRITE)
    equal(unwritable.status, ExecutionStatus.FAILED, "project.save onto its own read-only file")
    equal(error_code(unwritable), PROJECT_SAVE_FAILED, "the code for a read-only target")
    error = unwritable.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    # 拒绝先于写入，用的是 QGIS 写之前那一句；``write()`` 自己的那句带整条路径，不是这一句。
    # The refusal comes before any write and is the sentence QGIS uses there; ``write()``'s own
    # sentence carries the whole path and is not this one.
    equal(error.message, "The project file is not writable.", "the message for a read-only target")
    equal(fresh.read_bytes(), bytes_before, "the bytes of the read-only file")
    reset_project(ctx)
    return (
        f"Save As onto an existing file: FAILED(TARGET_EXISTS) and the target's "
        f"{len(bytes_before)} bytes are unchanged; Save As onto a new path: SUCCEEDED and the file "
        "was written; Save onto that associated file: SUCCEEDED without an overwrite flag, and the "
        "default view it stored is in the project; Save onto the same file made read-only: "
        "FAILED(PROJECT_SAVE_FAILED) saying it is not writable, with the bytes unchanged"
    )


def at_18_corrupt_project(ctx: Context) -> str:
    """同一条规则的两面：坏的 QGZ 被拒且当前工程被清空，好的被读进来。

    Both sides of the same rule: a corrupt QGZ is refused and the current project is cleared, a
    valid one is read.
    """

    reset_project(ctx)
    cities_layer(ctx, "at18 kept")
    equal(len(project_layers()), 1, "the project before the failed open")

    record = ctx.run(
        "project.open",
        {"path": str(ctx.fixtures.corrupt_project), "discard_current": True},
    )
    equal(record.status, ExecutionStatus.FAILED, "project.open on a corrupt file")
    equal(error_code(record), PROJECT_OPEN_FAILED, "the code for a corrupt project")
    error = record.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    # 拒语是 QGIS 自己那句（"Unable to unzip file '<path>'"），操作不另写一句。
    # The refusal is QGIS's own sentence ("Unable to unzip file '<path>'"), not one written here.
    require("Unable to unzip file" in error.message, f"the message: {error.message!r}")
    # QGIS 的 addProject 先关掉当前工程再 read()：读失败时眼前这个工程是空的。
    # QGIS's addProject closes the current project before read(): a failed read leaves it empty.
    equal(len(project_layers()), 0, "the layers left after the failed open")

    valid = ctx.run("project.open", {"path": str(ctx.fixtures.project), "discard_current": True})
    equal(valid.status, ExecutionStatus.SUCCEEDED, "project.open on a valid QGZ")
    values = result_of(valid)
    equal(values["layer_count"], 2, "the layers the valid QGZ holds")
    equal(values["invalid_layer_count"], 0, "the invalid layers of the valid QGZ")
    listed = result_of(ctx.run("layer.list"))["layers"]
    equal(
        {row["name"]: row["kind"] for row in listed},
        {"cities": "vector", "cities_tif": "raster"},
        "the layers the QGZ really loaded: the GeoPackage and the GeoTIFF",
    )
    return (
        "corrupt QGZ -> FAILED(PROJECT_OPEN_FAILED) with QGIS's own 'Unable to unzip file' "
        "sentence, and the project holds 0 layers afterwards; the valid QGZ -> SUCCEEDED with 2 "
        "layers, 0 invalid; the session kept answering"
    )


def at_18b_project_with_unavailable_layer(ctx: Context) -> str:
    from qgis.core import QgsProject  # pyright: ignore[reportMissingImports]

    record = ctx.run("project.open", {"path": str(ctx.fixtures.missing), "discard_current": True})
    equal(
        record.status, ExecutionStatus.SUCCEEDED, "project.open on a project whose source is gone"
    )
    values = result_of(record)
    equal(values["layer_count"], 1, "the layers the project registers")
    equal(values["invalid_layer_count"], 1, "the layers that do not load")
    require(values["invalid_layer_count"] > 0, "the result claims every data source is valid")
    # 读取期间置脏被挡住，所以刚打开的工程是干净的；坏图层只做汇报，不替那个“不可用图层”对话框
    # 做选择。
    # Dirtying is blocked while the file is read, so a project that was just opened is clean; bad
    # layers are reported and nothing is decided on that dialog's behalf.
    equal(QgsProject.instance().isDirty(), False, "the dirty flag after opening the project")
    return (
        "read() succeeded with 1 registered layer and invalid_layer_count=1 (its data source was "
        "deleted), so the result does not claim every source is usable; the project is not dirty "
        "afterwards"
    )


def at_19_path_policy(ctx: Context) -> str:
    """路径只剩一条规则：必须是绝对路径；其余由 QGIS 与 provider 回答。

    One rule is left: a path has to be absolute; QGIS and the provider answer the rest.
    """

    reset_project(ctx)
    outside = ctx.fixtures.outside / "at19"
    outside.mkdir(exist_ok=True)

    relative = ctx.run(
        "layer.add", {"kind": "vector", "source_path": "cities.gpkg", "name": "at19"}
    )
    equal(relative.status, ExecutionStatus.FAILED, "layer.add from a relative path")
    equal(error_code(relative), PATH_NOT_ALLOWED, "the code for a relative path")

    saved = ctx.run("project.save", {"path": str(outside / "at19.qgs")})
    equal(saved.status, ExecutionStatus.SUCCEEDED, "project.save outside any configured root")
    require((outside / "at19.qgs").exists(), "the file the save reported was not written")

    # 一个 URL 不是 Windows 的绝对路径，所以它和相对路径落在同一条规则上。
    # A URL is not a Windows absolute path, so it meets the same rule as a relative path.
    remote = ctx.run(
        "layer.add",
        {"kind": "vector", "source_path": "https://127.0.0.1:9/x.gpkg", "name": "at19 remote"},
    )
    equal(remote.status, ExecutionStatus.FAILED, "layer.add from an https URI")
    equal(error_code(remote), PATH_NOT_ALLOWED, "the code for an https URI")
    reset_project(ctx)
    return (
        "a relative path -> FAILED(PATH_NOT_ALLOWED); project.save into a directory outside any "
        "configured root -> SUCCEEDED and the file exists; an https URI -> PATH_NOT_ALLOWED, the "
        "same absolute-path rule rather than a scheme rule"
    )


def at_29_open_accepts_a_source_outside_any_root(ctx: Context) -> str:
    """工程自己点名的数据源不由插件审：打开成功，图层留着。

    The plugin does not judge the sources a project names: the open succeeds and the layer stays.
    """

    reset_project(ctx)
    record = ctx.run("project.open", {"path": str(ctx.fixtures.leak), "discard_current": True})

    equal(
        record.status,
        ExecutionStatus.SUCCEEDED,
        "project.open on a project whose layer source is outside any configured root",
    )
    names = [layer.name() for layer in project_layers().values()]
    require("leaked" in names, f"the outside layer is missing: {names}")
    equal(len(project_layers()), 1, "the layers the project kept")
    reset_project(ctx)
    return (
        "a QGZ whose only layer names a GeoPackage in a sibling directory: SUCCEEDED with that "
        "layer still registered, because the plugin does not judge a project's data sources"
    )


def at_30_open_does_not_judge_a_remote_source(ctx: Context) -> str:
    """远程数据源不是操作的判据：``read()`` 会去取它。

    那一层于是加载不出来，结果里就多一个 ``invalid_layer_count``：慢不是拒绝的理由，死才是。

    A remote datasource is not an operation's criterion: ``read()`` goes and fetches it, so that
    layer fails to load and shows up in ``invalid_layer_count``.  Slowness is not a reason to
    refuse an input; death is.
    """

    reset_project(ctx)
    record = ctx.run(
        "project.open",
        {"path": str(ctx.fixtures.remote_vsicurl), "discard_current": True},
    )

    equal(record.status, ExecutionStatus.SUCCEEDED, "project.open on a /vsicurl source")
    values = result_of(record)
    equal(values["layer_count"], 1, "the layers the project registers")
    equal(values["invalid_layer_count"], 1, "the layers whose remote source never answered")
    reset_project(ctx)
    return (
        "a project whose only layer names /vsicurl/http://127.0.0.1:9/never.tif: SUCCEEDED "
        "with layer_count 1 and invalid_layer_count 1, because the plugin does not refuse a "
        "remote source (the GUI fetches it too; the closed port answers at once)"
    )


def at_32_open_keeps_a_source_whose_directory_is_gone(ctx: Context) -> str:
    """数据源目录被搬走：工程照样打开，只是那一层加载不出来。

    A moved-away source directory: the project still opens, and only that layer fails to load.
    """

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at32 old", sub_layer=fixtures.CITIES_LAYER)

    record = ctx.run(
        "project.open",
        {"path": str(ctx.fixtures.moved_directory), "discard_current": True},
    )

    equal(
        record.status,
        ExecutionStatus.SUCCEEDED,
        "project.open on a project whose source directory is gone",
    )
    values = result_of(record)
    equal(values["layer_count"], 1, "the layers the project registers")
    equal(values["invalid_layer_count"], 1, "the layers that do not load")
    listed = result_of(ctx.run("layer.list"))["layers"]
    equal([row["name"] for row in listed], ["moved"], "the layers still registered")
    reset_project(ctx)
    return (
        "SUCCEEDED with layer_count 1 and invalid_layer_count 1, the source directory "
        "does not exist: the layer stays registered and the read is not turned into "
        "PATH_NOT_ALLOWED"
    )


def at_34_unreadable_member_is_a_project_read_failure(ctx: Context) -> str:
    """加密成员会让 ``read()`` 直接杀掉进程，所以它在 read() 之前被拒，用 read() 会报的码。

    An encrypted member kills the process inside ``read()``, so it is refused before ``read()``,
    with the code ``read()`` would have reported.
    """

    reset_project(ctx)

    record = ctx.run(
        "project.open",
        {"path": str(ctx.fixtures.encrypted), "discard_current": True},
    )

    equal(
        record.status,
        ExecutionStatus.FAILED,
        "project.open on a QGZ whose member is encrypted",
    )
    equal(
        error_code(record),
        PROJECT_OPEN_FAILED,
        "the code for a QGZ with an encrypted member (INTERNAL_ERROR would blame the plugin)",
    )
    error = record.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    # 拒在 clear() 之前，所以消息不能说“已清空”，也不能说 read() 的那句话。
    # Refused before clear(), so the message may say neither "cleared" nor read()'s sentence.
    require("encrypted" in error.message, f"the message: {error.message!r}")
    require("not changed" in error.message, f"the message: {error.message!r}")
    require("cleared" not in error.message, f"the message: {error.message!r}")
    reset_project(ctx)
    return (
        "the member carries the zip encryption flag: FAILED(PROJECT_OPEN_FAILED) saying the "
        "archive 'was not opened and the current project was not changed', and read() never sees "
        "it (QGIS 3.44 ended the process with 0xC0000005 on it, measured here)"
    )


def at_35_open_refuses_a_deeply_nested_project_before_reading_it(ctx: Context) -> str:
    """嵌套过深的工程会让 ``read()`` 的解析器崩溃，所以它绝不能走到 read()。

    但“深”只算 ``read()`` 会解析的那个文档：一个归档里的第二个 ``.qgs`` 永远进不了 DOM
    （``QgsProjectArchive::projectFile()`` 只取第一个）。

    A deeply nested project crashes ``read()``'s parser, so it never reaches it -- but "deep" only
    counts the one document ``read()`` parses: a second ``.qgs`` inside an archive never reaches the
    DOM (``QgsProjectArchive::projectFile()`` takes the first one only).
    """

    import zipfile

    from qgis.core import QgsProject  # pyright: ignore[reportMissingImports]

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at35 kept", sub_layer=fixtures.CITIES_LAYER)
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()

    started = time.monotonic()
    record = ctx.run(
        "project.open",
        {"path": str(ctx.fixtures.deep_project), "discard_current": True},
    )
    elapsed = time.monotonic() - started

    equal(record.status, ExecutionStatus.FAILED, "project.open on a deeply nested project")
    equal(error_code(record), PROJECT_OPEN_FAILED, "the code for a deeply nested project")
    error = record.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    require("nests deeper" in error.message, f"the message: {error.message!r}")
    require("not changed" in error.message, f"the message: {error.message!r}")
    require(elapsed < 1.0, f"the 8000-level project took {elapsed:.2f}s")
    equal(project_layer_ids(), before, "the project's layers after the refusal")
    equal(QgsProject.instance().fileName(), file_before, "the project's file after the refusal")

    # 同一个归档里第二个 ``.qgs`` 很深：真 QGIS 只读第一个，所以这里也必须放行。
    # A second, deep ``.qgs`` in the same archive: real QGIS reads only the first, so this must
    # pass.
    two_members = ctx.fixtures.root / "two project members.qgz"
    with zipfile.ZipFile(two_members, "w") as archive:
        archive.writestr("first.qgs", ctx.fixtures.grouped.read_bytes())
        archive.writestr("second.qgs", ctx.fixtures.deep_project.read_bytes())
    allowed = ctx.run("project.open", {"path": str(two_members), "discard_current": True})
    equal(
        allowed.status,
        ExecutionStatus.SUCCEEDED,
        "project.open on an archive whose second .qgs is deep",
    )
    reset_project(ctx)
    return (
        "a 56 KB project nested 8000 levels deep: FAILED(PROJECT_OPEN_FAILED) in "
        f"{elapsed * 1000:.0f} ms, saying the project was not changed, and the old project kept "
        f"its layer (bound is {MAX_PROJECT_DEPTH}; QGIS 3.44 ended the process with 0xC00000FD at "
        "6000 levels, measured here); the same 8000 levels inside the second .qgs of an archive "
        "-> SUCCEEDED, because read() only parses the first one"
    )


def at_40_open_reads_a_project_with_labelled_layers(ctx: Context) -> str:
    """属性里的 ``>`` 正是 QGIS 给每个标注写出来的东西，所以正常工程不能被拒。

    A ``>`` inside an attribute is what QGIS itself writes for every label.
    """

    reset_project(ctx)
    on_disk = ctx.fixtures.labelled.stat().st_size

    started = time.monotonic()
    record = ctx.run("project.open", {"path": str(ctx.fixtures.labelled), "discard_current": True})
    elapsed = time.monotonic() - started

    equal(
        record.status,
        ExecutionStatus.SUCCEEDED,
        "project.open on a project with labelled layers",
    )
    values = result_of(record)
    equal(
        values["layer_count"],
        fixtures.LABELLED_LAYERS,
        "the layers the labelled project holds",
    )
    equal(values["invalid_layer_count"], 0, "the invalid layers of the labelled project")
    # 墙钟时间只能区分“答出来了”与“主线程被停住”：同一个 30 层工程的读取两次整套运行分别实测
    # 1.1 s 与 2.1 s，而文件扫描两次都是 31 ms，所以再收紧上限测的就不是代码本身了。
    # Wall clock only separates "answered" from "stopped the main thread": the same 30-layer read
    # took 1.1 s and then 2.1 s in two full runs while the scan of the file took 31 ms both times,
    # so a tighter bound would not measure the code.
    require(elapsed < 20.0, f"the {on_disk} byte project took {elapsed:.2f}s")
    reset_project(ctx)
    return (
        f"a {on_disk} byte project with {fixtures.LABELLED_LAYERS} labelled layers: SUCCEEDED "
        f"with {values['layer_count']} layers in {elapsed * 1000:.0f} ms. The old counter "
        "drifted +2 per labelled layer and refused 28 of them (measured); the parser counts "
        "elements, and every label tag is one"
    )


def at_41_open_refuses_depth_written_inside_cdata_comments_or_attributes(
    ctx: Context,
) -> str:
    """四种把深度计数压住的写法：CDATA、注释、属性值、以及没声明前缀的标签。

    The four shapes that held a depth counter down.
    """

    from qgis.core import QgsProject  # pyright: ignore[reportMissingImports]

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at41 kept", sub_layer=fixtures.CITIES_LAYER)
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()
    # 前缀没声明时 ``QDomDocument`` 把它当普通元素名，所以它也是一层；这个文件由用例自己写，
    # 因为它只在这个用例里有意义。
    # With an undeclared prefix ``QDomDocument`` takes it as an ordinary element name, so it is one
    # level too; the case writes this file itself because it means nothing outside it.
    prefix_project = ctx.fixtures.root / "hidden depth prefix.qgs"
    prefix_project.write_bytes(
        b"<qgis>"
        + b"<q:a>" * fixtures.DEEP_PROJECT_LEVELS
        + b"</q:a>" * fixtures.DEEP_PROJECT_LEVELS
        + b"</qgis>"
    )
    timings: list[str] = []
    for label, target in (
        ("CDATA", ctx.fixtures.hidden_depth_cdata),
        ("a comment", ctx.fixtures.hidden_depth_comment),
        ("an attribute", ctx.fixtures.hidden_depth_attribute),
        ("an undeclared prefix", prefix_project),
    ):
        on_disk = target.stat().st_size
        started = time.monotonic()
        record = ctx.run("project.open", {"path": str(target), "discard_current": True})
        elapsed = time.monotonic() - started
        equal(record.status, ExecutionStatus.FAILED, f"project.open on depth hidden by {label}")
        equal(error_code(record), PROJECT_OPEN_FAILED, f"the code for {label}")
        error = record.error
        if error is None:
            raise AssertionError("a failed record carries an error")
        require("nests deeper" in error.message, f"the message for {label}: {error.message!r}")
        require(elapsed < 0.5, f"the {on_disk} byte project took {elapsed:.2f}s")
        equal(project_layer_ids(), before, f"the project's layers after {label}")
        equal(QgsProject.instance().fileName(), file_before, f"the project's file after {label}")
        timings.append(f"{label} {on_disk} bytes {elapsed * 1000:.0f} ms")
    reset_project(ctx)
    return (
        f"{'; '.join(timings)}: FAILED(PROJECT_OPEN_FAILED) with 'nests deeper' before read(). "
        "The audit measured read() exit code -1073741571 (0xC00000FD) on each of these shapes; "
        "this process is still answering and the old project kept its layer"
    )


def at_42_open_refuses_an_archive_whose_sibling_member_is_encrypted(ctx: Context) -> str:
    """``read()`` 会把归档里每个成员都解出来，不只 ``.qgs`` 那个。

    ``read()`` unpacks every member, not only the ``.qgs`` one.
    """

    from qgis.core import QgsProject  # pyright: ignore[reportMissingImports]

    reset_project(ctx)
    ctx.add_layer(ctx.fixtures.cities, "at42 kept", sub_layer=fixtures.CITIES_LAYER)
    before = project_layer_ids()
    file_before = QgsProject.instance().fileName()
    on_disk = ctx.fixtures.encrypted_sibling.stat().st_size

    started = time.monotonic()
    record = ctx.run(
        "project.open",
        {"path": str(ctx.fixtures.encrypted_sibling), "discard_current": True},
    )
    elapsed = time.monotonic() - started

    equal(record.status, ExecutionStatus.FAILED, "project.open on an encrypted sibling member")
    equal(error_code(record), PROJECT_OPEN_FAILED, "the code for an encrypted sibling member")
    error = record.error
    if error is None:
        raise AssertionError("a failed record carries an error")
    require("not changed" in error.message, f"the message: {error.message!r}")
    require("cleared" not in error.message, f"the message: {error.message!r}")
    require(elapsed < 0.5, f"the {on_disk} byte archive took {elapsed:.2f}s")
    equal(project_layer_ids(), before, "the project's layers after the refusal")
    equal(QgsProject.instance().fileName(), file_before, "the project's file after the refusal")
    reset_project(ctx)
    return (
        f"a {on_disk} byte QGZ whose extra.db carries the encryption flag: "
        f"FAILED(PROJECT_OPEN_FAILED) in {elapsed * 1000:.0f} ms, saying the project was not "
        "changed, and the layers and file name are the ones from before the call. The audit "
        "measured read() exit code -1073741819 (0xC0000005) on this shape"
    )


def at_43_open_reads_a_project_with_a_deep_group_tree(ctx: Context) -> str:
    """真实的分组树远远到不了的深度上限，所以正常工程不能被它拒掉。

    A depth limit that a real group tree never reaches.
    """

    reset_project(ctx)
    on_disk = ctx.fixtures.grouped.stat().st_size

    started = time.monotonic()
    record = ctx.run("project.open", {"path": str(ctx.fixtures.grouped), "discard_current": True})
    elapsed = time.monotonic() - started

    equal(
        record.status,
        ExecutionStatus.SUCCEEDED,
        "project.open on a project with a deep layer-tree group",
    )
    values = result_of(record)
    equal(values["layer_count"], 1, "the layers of the grouped project")
    require(elapsed < 20.0, f"the {on_disk} byte project took {elapsed:.2f}s")
    reset_project(ctx)
    return (
        f"a {on_disk} byte project with {fixtures.NESTED_GROUPS} nested layer-tree groups "
        f"(depth ~205 in the written XML, measured): SUCCEEDED with {values['layer_count']} "
        f"layer in {elapsed * 1000:.0f} ms; the scan's limit is {MAX_PROJECT_DEPTH} levels"
    )


def at_46_open_reads_a_project_that_declares_xml_entities(ctx: Context) -> str:
    """XML 实体不是判据：真 QGIS 不展开它，所以操作也不能拿它拒。

    XML entities are not a criterion: real QGIS does not expand them, so the operation may not
    refuse on them either.
    """

    reset_project(ctx)
    on_disk = ctx.fixtures.entity_project.stat().st_size

    started = time.monotonic()
    record = ctx.run(
        "project.open", {"path": str(ctx.fixtures.entity_project), "discard_current": True}
    )
    elapsed = time.monotonic() - started

    equal(
        record.status,
        ExecutionStatus.SUCCEEDED,
        "project.open on a project declaring XML entities",
    )
    values = result_of(record)
    require(elapsed < 20.0, f"the {on_disk} byte project took {elapsed:.2f}s")
    reset_project(ctx)
    return (
        f"a {on_disk} byte project whose 1 MiB entity is referenced 50 times: SUCCEEDED with "
        f"{values['layer_count']} layer(s) in {elapsed * 1000:.0f} ms. QDomDocument does not "
        "expand a custom entity (measured ok=True in 0.53 s), so nothing refuses it: the scan "
        "streams the file and accumulates no expansion"
    )


def at_47_open_reads_projects_with_values_that_look_remote(ctx: Context) -> str:
    """两个普通工程：CSV 图层的 ``file:`` 源，与元数据里带 URL 的图层。

    两个都必须能打开：扫描只看结构，不看值的内容，远程与否是 ``read()`` 的事。

    Two ordinary projects: a CSV layer's ``file:`` source, and a layer whose metadata carries a
    URL.  Both have to open: the scan reads structure only, never value contents, and whether a
    source is remote is ``read()``'s business.
    """

    reset_project(ctx)
    csv_project = ctx.fixtures.root / "csv project.qgs"
    fixtures.write_csv_project(csv_project, ctx.fixtures.root)
    metadata_project = ctx.fixtures.root / "metadata project.qgs"
    fixtures.write_metadata_project(metadata_project, ctx.fixtures.cities, fixtures.CITIES_LAYER)

    started = time.monotonic()
    record = ctx.run("project.open", {"path": str(csv_project), "discard_current": True})
    csv_elapsed = time.monotonic() - started
    equal(record.status, ExecutionStatus.SUCCEEDED, "project.open on a CSV layer spelled file:./")
    csv_layers = result_of(record)["layer_count"]
    require(csv_layers >= 1, f"the CSV project's layers: {csv_layers}")
    require(csv_elapsed < 20.0, f"the CSV project took {csv_elapsed:.2f}s")

    started = time.monotonic()
    metadata_record = ctx.run(
        "project.open", {"path": str(metadata_project), "discard_current": True}
    )
    metadata_elapsed = time.monotonic() - started

    equal(
        metadata_record.status,
        ExecutionStatus.SUCCEEDED,
        "project.open on a layer whose metadata identifier is a URL",
    )
    metadata_layers = result_of(metadata_record)["layer_count"]
    require(metadata_layers >= 1, f"the metadata project's layers: {metadata_layers}")
    require(metadata_elapsed < 20.0, f"the metadata project took {metadata_elapsed:.2f}s")
    reset_project(ctx)
    return (
        f"a Delimited Text layer spelled file:./points.csv: SUCCEEDED with {csv_layers} layer "
        f"in {csv_elapsed * 1000:.0f} ms; a layer whose metadata identifier is "
        f"https://example.org/datasets/42: SUCCEEDED with {metadata_layers} layer in "
        f"{metadata_elapsed * 1000:.0f} ms. Both were refused by the old value scan; read() "
        "reads them, and a value that is only metadata names no data source"
    )


def at_49_open_reads_a_project_whose_source_is_inside_a_vsi_directory(ctx: Context) -> str:
    """``/vsi`` 只有在数据源开头才是虚拟文件系统；目录名叫 vsi_data 就是本地文件。

    ``/vsi`` names a virtual file system only at the start of a source.
    """

    reset_project(ctx)
    target = ctx.fixtures.root / "vsi directory project.qgs"
    fixtures.write_vsi_directory_project(target, ctx.fixtures.cities, fixtures.CITIES_LAYER)

    started = time.monotonic()
    record = ctx.run("project.open", {"path": str(target), "discard_current": True})
    elapsed = time.monotonic() - started

    equal(record.status, ExecutionStatus.SUCCEEDED, "project.open on a source inside vsi_data")
    values = result_of(record)
    require(values["layer_count"] == 1, f"the layers of the project: {values['layer_count']}")
    require(elapsed < 20.0, f"the project took {elapsed:.2f}s")
    layers = project_layer_ids()
    reset_project(ctx)
    return (
        "a project whose datasource sits in a directory named vsi_data: SUCCEEDED with "
        f"{values['layer_count']} layer in {elapsed * 1000:.0f} ms (it registered {len(layers)}). "
        "/vsi is a GDAL file system only when it opens the path: the substring check refused the "
        "project as a remote data source, while read() reads it (measured)"
    )


def at_51_open_reads_a_project_whose_layer_is_a_local_subdataset(ctx: Context) -> str:
    """本地 GDAL 子数据集按它自己点名的文件读，与拼法无关。

    A local GDAL subdataset is read through the file it names, whatever its spelling.
    """

    reset_project(ctx)
    target = ctx.fixtures.root / "local subdataset.qgs"
    fixtures.write_subdataset_project(target, ctx.fixtures.raster)

    started = time.monotonic()
    record = ctx.run("project.open", {"path": str(target), "discard_current": True})
    elapsed = time.monotonic() - started
    equal(record.status, ExecutionStatus.SUCCEEDED, "project.open on a local subdataset")
    values = result_of(record)
    require(values["layer_count"] == 1, f"the layers of the project: {values['layer_count']}")
    equal(values["invalid_layer_count"], 0, "the invalid layers of the project")
    kept = project_layer_ids()
    require(len(kept) == 1, f"the registered layers: {kept}")
    reset_project(ctx)
    return (
        "a project whose layer is GTIFF_DIR:1:<the fixture>/cities.tif: SUCCEEDED with "
        f"{values['layer_count']} layer in {elapsed * 1000:.0f} ms and the layer kept; the "
        "subdataset spelling reaches the provider as it was written"
    )


PROJECT_CASES = (
    ("AT-15", at_15_stateful_replace_refused),
    ("AT-16", at_16_stateful_replace),
    ("AT-17", at_17_save_refuses_existing),
    ("AT-18", at_18_corrupt_project),
    ("AT-18B", at_18b_project_with_unavailable_layer),
    ("AT-19", at_19_path_policy),
    ("AT-29", at_29_open_accepts_a_source_outside_any_root),
    ("AT-30", at_30_open_does_not_judge_a_remote_source),
    ("AT-32", at_32_open_keeps_a_source_whose_directory_is_gone),
    ("AT-34", at_34_unreadable_member_is_a_project_read_failure),
    ("AT-35", at_35_open_refuses_a_deeply_nested_project_before_reading_it),
    ("AT-40", at_40_open_reads_a_project_with_labelled_layers),
    ("AT-41", at_41_open_refuses_depth_written_inside_cdata_comments_or_attributes),
    ("AT-42", at_42_open_refuses_an_archive_whose_sibling_member_is_encrypted),
    ("AT-43", at_43_open_reads_a_project_with_a_deep_group_tree),
    ("AT-46", at_46_open_reads_a_project_that_declares_xml_entities),
    ("AT-47", at_47_open_reads_projects_with_values_that_look_remote),
    ("AT-49", at_49_open_reads_a_project_whose_source_is_inside_a_vsi_directory),
    ("AT-51", at_51_open_reads_a_project_whose_layer_is_a_local_subdataset),
)
