"""四个 ``project.*`` 操作。

四个都跑在主线程：``QgsProject`` 不能从后台线程碰。新建、打开、保存会替换或写入工程状态，所以把
自己标成破坏性，调用方不读代码也能看见风险。

替换当前工程之前，先看有没有任务还在用它的图层：那种任务必须由调用方取消掉再重试，``discard_current``
不回答这一问。然后看工程里有没有没保存下来的东西：图层里没提交的编辑、带要素的 memory 图层或临时
图层、工程自己的脏标记、栅格波段的属性表；``discard_current=true`` 就是这四问共同的“丢掉”答案。

``project.open`` 另有两处比 QGIS 早拒的文件形状，都是实测能让进程直接消失的：嵌套过深的文档与加密
的 ``.qgz`` 成员。

QGIS 只在用到它的方法内部 import，所以本模块在裸解释器里也能加载，路径规则不依赖 QGIS 就能测。

All four run on the main thread, because a ``QgsProject`` may not be touched from a worker; the
three that replace or write state declare themselves destructive so a caller sees the risk
without reading the code.

Before the current project is replaced, tasks still using one of its layers are looked at: those
have to be cancelled by the caller and the call retried, which ``discard_current`` does not
answer.  Then what the project holds unsaved: uncommitted layer edits, a memory layer with
features or a temporary layer, the project's own dirty flag, and a raster band's attribute
table -- all four answered ``discard`` by ``discard_current=true``.

``project.open`` refuses two file shapes earlier than QGIS does, both measured to end the process
outright: a document nested too deeply, and an encrypted ``.qgz`` member.

QGIS is imported inside the methods that need it, so this module still loads in a bare
interpreter and the path rules stay testable without QGIS.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Annotated, Any
from xml.parsers import expat

from pydantic import (  # pyright: ignore[reportMissingImports]
    AfterValidator,
    Field,
    StrictBool,
    StrictStr,
)

from ..core.contracts import (
    BaseCommand,
    BaseQuery,
    MainThreadOperationMixin,
    OperationContext,
)
from ..core.models import (
    INTERNAL_ERROR,
    PROJECT_OPEN_FAILED,
    PROJECT_PATH_REQUIRED,
    PROJECT_REPLACE_REQUIRED,
    PROJECT_SAVE_FAILED,
    TARGET_EXISTS,
    FrameworkError,
    Params,
    Result,
)
from ..core.security import absolute_path

# 比这更深的工程在 ``read()`` 之前被拒：它的 DOM 解析是递归下降的，深度到六千层时实测以
# 0xC00000FD（栈溢出）结束进程，而 5500 层 0.10 s 就读完了。4096 对实测读得完的深度留了三分之一
# 余量；QGIS 自己写出的 100 层分组只有 105 层深。
# A project nested deeper than this is refused before ``read()``: its DOM parser descends
# recursively, and at six thousand levels it ended the process with 0xC00000FD (a stack
# overflow) while 5500 levels read in 0.10 s.  4096 leaves a third of the measured working depth
# as margin; the 100 nested groups QGIS itself writes are 105 levels deep.
MAX_PROJECT_DEPTH = 4096

# ``.qgz`` 成员头里的加密标志位：``QgsZipUtils::unzip`` 解包每个成员，加密的那个实测以
# 0xC0000005（访问越界）结束进程。
# The encryption bit in a ``.qgz`` member header: ``QgsZipUtils::unzip`` unpacks every member, and
# an encrypted one ended the process with 0xC0000005 (an access violation).
_ZIP_ENCRYPTED = 0x1

# 这两种形状拒在碰当前工程之前，所以拒语要说清它没被动过。``read()`` 失败是另一回事：那时工程已经
# 被清空，由 QGIS 自己的错误文本回答。
# These two shapes are refused before the current project is touched, so the refusal says so.  A
# failed ``read()`` is the other case: the project is already cleared then, and QGIS's own error
# text answers it.
_NOT_CHANGED = "; it was not opened and the current project was not changed"

# 一次喂给解析器的字节数：整个文件不必进内存。
# Bytes handed to the parser at a time, so the whole file need not fit in memory.
_CHUNK = 1024 * 1024

# 保存目标的后缀只有这两种：保存对话框按过滤器把名字补成其中之一，所以别的后缀不是它会给的答案。
# The only two suffixes a save target may have: the save dialog appends one of them by filter, so
# any other suffix is not an answer it can give.
PROJECT_SUFFIXES = (".qgs", ".qgz")


def _save_target(raw: str) -> str:
    """``path`` 的形状：后缀是 ``.qgs`` 或 ``.qgz``，大小写不敏感。

    只判后缀：绝对路径由 ``absolute_path`` 判，目标存不存在由 ``execute_main`` 的覆盖检查回答，
    可写与怎么写由 ``write()`` 回答。

    The shape of ``path``: a ``.qgs`` or ``.qgz`` suffix, case-insensitively.

    Only the suffix is judged here: ``absolute_path`` checks the absolute form, the overwrite check
    in ``execute_main`` answers whether the target exists, and ``write()`` answers writability and
    how to write.
    """

    if Path(raw).suffix.lower() not in PROJECT_SUFFIXES:
        raise ValueError("path must be a .qgs or .qgz path")
    return raw


class ProjectCreateParams(Params):
    discard_current: StrictBool = False


class ProjectCreateResult(Result):
    # 报工程现在持有的规范 authid：新工程的 CRS 来自 QGIS 的设置与模板。
    # Reports the canonical authid the project now holds: a new project's CRS comes from QGIS's
    # settings and template.
    crs_authid: StrictStr
    # 默认模板可以带图层进来。
    # The default template may bring layers along.
    layer_count: int


class ProjectOpenParams(Params):
    path: StrictStr = Field(min_length=1)
    discard_current: StrictBool = False


class ProjectOpenResult(Result):
    file_path: StrictStr
    crs_authid: StrictStr
    layer_count: int
    # 读得进来但源没接上的图层：QGIS 会把它们列在那个"不可用图层"对话框里，工程照样是打开的。
    # Layers that loaded but whose source did not answer: QGIS lists them in its unavailable-layer
    # dialog, and the project is open all the same.
    invalid_layer_count: int


class ProjectSaveParams(Params):
    path: Annotated[StrictStr, Field(min_length=1), AfterValidator(_save_target)] | None = None
    overwrite: StrictBool = False


class ProjectSaveResult(Result):
    file_path: StrictStr


class ProjectGetInfoParams(Params):
    pass


class ProjectGetInfoResult(Result):
    # 没保存过的工程没有文件，属性面板那时显示的是空。
    # A project that was never saved has no file, and the properties panel shows nothing for it.
    file_path: StrictStr | None
    crs_authid: StrictStr
    is_dirty: StrictBool
    layer_count: int
    # 工程属性里的标题；没设置时是空串，标题栏那时改用"Untitled Project"或文件名，那两样由
    # ``file_path`` 回答。
    # The title from the project properties, empty when unset; the title bar falls back to
    # "Untitled Project" or the file name then, and ``file_path`` answers those.
    title: StrictStr


class ProjectCreate(
    BaseCommand[ProjectCreateParams, ProjectCreateResult],
    MainThreadOperationMixin[ProjectCreateParams, ProjectCreateResult],
):
    name = "project.create"
    description = "Replace the current project with a new one, as the New Project action does"
    input_model = ProjectCreateParams
    output_model = ProjectCreateResult
    destructive = True

    def execute_main(
        self, ctx: OperationContext, request: ProjectCreateParams
    ) -> ProjectCreateResult:
        _refuse_if_tasks_depend_on_layers(ctx.project)
        _refuse_replacement(ctx.project, request.discard_current)

        # 这一步就是 QGIS 的 New Project：它自己清空当前工程、给新工程一个设置里的 CRS、读默认
        # 模板，最后把脏标记清掉。剩下的状态都从工程里读，不在这里重算。
        # This step is QGIS's own New Project: it clears the current project, gives the new one the
        # CRS from its settings, reads the default template and finally clears the dirty flag.
        # Everything else is read back from the project rather than recomputed here.
        if not ctx.iface.newProject(promptToSaveFlag=False):
            # 这条路下 QGIS 只在有任务依赖当前工程的图层时返回假，那句拒绝由上面的检查先说过；能到
            # 这里说明检查之后又冒出了一个任务。工程没换，所以这不能算成功，而调用方能做的还是取消
            # 那个任务再试。
            # On this path QGIS returns false only when a task depends on a layer of the current
            # project, which the check above refuses first; reaching this means a task appeared
            # after it.  The project was not replaced, so this is not a success, and the caller's
            # move is still to cancel that task and retry.
            raise FrameworkError(
                INTERNAL_ERROR,
                "QGIS did not create a new project; "
                "cancel any task that depends on a layer and retry",
            )

        project = ctx.project
        return ProjectCreateResult(
            crs_authid=project.crs().authid(),
            layer_count=len(project.mapLayers()),
        )


class ProjectOpen(
    BaseCommand[ProjectOpenParams, ProjectOpenResult],
    MainThreadOperationMixin[ProjectOpenParams, ProjectOpenResult],
):
    name = "project.open"
    description = "Replace the current project with an existing .qgs or .qgz file"
    input_model = ProjectOpenParams
    output_model = ProjectOpenResult
    destructive = True

    def execute_main(self, ctx: OperationContext, request: ProjectOpenParams) -> ProjectOpenResult:
        from qgis.core import QgsProject  # pyright: ignore[reportMissingImports]

        # 后缀不判：打开对话框收下你打的任何文件名，``read()`` 只按后缀决定"是不是压缩包"，剩下的
        # 交给解析器。拒掉某种后缀就是拒掉一个打得开的工程。
        # The suffix is not judged: the open dialog accepts whatever name you type, ``read()`` uses
        # the suffix only to decide "archive or not" and leaves the rest to the parser, so
        # refusing a suffix would refuse a project that opens.
        path = absolute_path(request.path)
        _refuse_if_tasks_depend_on_layers(ctx.project)
        _refuse_replacement(ctx.project, request.discard_current)
        _refuse_if_ends_the_process(path)

        # QGIS 在 ``read()`` 之前先关掉当前工程，于是读失败时眼前这个是空的，而不是旧内容配上新
        # 文件名；关掉与读取都在“不许置脏”的范围内，所以失败之后工程也是干净的。
        # QGIS closes the current project before ``read()``, so a failed read leaves an empty one
        # rather than the old contents under a new file name; the close and the read both happen
        # with dirtying blocked, so the project is clean after a failure too.
        with QgsProject.blockDirtying(ctx.project):
            ctx.project.clear()
            opened = ctx.project.read(str(path))
        if not opened:
            # 失败原因由 QGIS 自己拼，它点出打不开的那个文件或解析器的抱怨。
            # QGIS composes the reason itself, naming the file it could not open or the parser's
            # complaint.
            raise FrameworkError(PROJECT_OPEN_FAILED, ctx.project.error())

        layers = ctx.project.mapLayers()
        return ProjectOpenResult(
            file_path=str(path),
            crs_authid=ctx.project.crs().authid(),
            layer_count=len(layers),
            invalid_layer_count=sum(1 for layer in layers.values() if not layer.isValid()),
        )


class ProjectSave(
    BaseCommand[ProjectSaveParams, ProjectSaveResult],
    MainThreadOperationMixin[ProjectSaveParams, ProjectSaveResult],
):
    name = "project.save"
    description = "Write the current project to a .qgs or .qgz file"
    input_model = ProjectSaveParams
    output_model = ProjectSaveResult
    destructive = True

    def execute_main(self, ctx: OperationContext, request: ProjectSaveParams) -> ProjectSaveResult:
        project = ctx.project
        # 给了路径就是 Save As，没给就是 Save：后者写工程自己的关联文件，那张保存对话框不出现。
        # A path means Save As; no path means Save, which writes the project's own associated file
        # and never reaches the save dialog.
        target = request.path if request.path is not None else project.fileName()
        if not target:
            raise FrameworkError(
                PROJECT_PATH_REQUIRED, "no path was given and the project has no associated file"
            )
        path = absolute_path(target)

        if request.path is None:
            _refuse_if_unwritable(path)
            # Save 把当前的画布视图写进工程，重开时回到这里；Save As 不做这一步。
            # Save stores the current canvas view in the project so reopening returns to it; Save
            # As does not do this.
            _store_current_view(ctx, project)
        elif path.exists() and not request.overwrite:
            # 覆盖确认是保存对话框问的，而这里没有对话框，所以由 ``overwrite`` 回答。
            # The save dialog asks about overwriting and there is no dialog here, so ``overwrite``
            # carries that answer.
            raise FrameworkError(
                TARGET_EXISTS, "the target file exists; pass overwrite=true to replace it"
            )

        if not project.write(str(path)):
            # 同 ``project.open``：写失败的原因由 QGIS 拼，包括目标不可写与磁盘写满。
            # As in ``project.open``: QGIS composes why the write failed, an unwritable target and
            # a full disk included.
            raise FrameworkError(PROJECT_SAVE_FAILED, project.error())
        return ProjectSaveResult(file_path=str(path))


class ProjectGetInfo(
    BaseQuery[ProjectGetInfoParams, ProjectGetInfoResult],
    MainThreadOperationMixin[ProjectGetInfoParams, ProjectGetInfoResult],
):
    name = "project.get_info"
    description = "Read the current project's file, CRS, dirty flag, layer count and title"
    input_model = ProjectGetInfoParams
    output_model = ProjectGetInfoResult

    def execute_main(
        self, ctx: OperationContext, request: ProjectGetInfoParams
    ) -> ProjectGetInfoResult:
        project = ctx.project
        return ProjectGetInfoResult(
            file_path=project.fileName() or None,
            crs_authid=project.crs().authid(),
            is_dirty=project.isDirty(),
            layer_count=len(project.mapLayers()),
            title=project.title(),
        )


def _refuse_if_tasks_depend_on_layers(project: Any) -> None:
    """有任务还在用这个工程的图层时拒，并把它们的描述一并给出去。

    QGIS 在换工程之前也先看这个，只是它弹一个模态框让人把任务取消掉再重试。这里等不到人点，所以
    同一句话作为拒绝返回给调用方；任务在别的 ``QgsTaskManager`` 里（本插件的后台任务就在插件自己
    那个里），这个检查看不见它们，替换工程时由引擎加代数并协作式取消来处理。

    Refuses while a task still depends on a layer of this project, listing the tasks.

    QGIS looks at the same thing before replacing a project, except it shows a modal box so a
    person cancels the tasks and retries.  Nobody here can click it, so the same sentence comes
    back as a refusal; tasks in another ``QgsTaskManager`` (this plugin's own background work is
    in the plugin's) are invisible to this check, and replacing the project handles those by
    bumping the epoch and asking them to stop.
    """

    from qgis.core import QgsApplication  # pyright: ignore[reportMissingImports]

    descriptions: list[str] = []
    for layer in project.mapLayers().values():
        for task in QgsApplication.taskManager().tasksDependentOnLayer(layer):
            if task.description() not in descriptions:
                descriptions.append(task.description())
    if descriptions:
        raise FrameworkError(
            PROJECT_REPLACE_REQUIRED,
            "The following tasks are currently running which depend on layers in this project:"
            "\n\n"
            + "\n".join(f" • {description}" for description in descriptions)
            + "\n\nPlease cancel these tasks and retry.",
        )


def _refuse_replacement(project: Any, discard_current: bool) -> None:
    """只有 ``discard_current=true`` 才能换掉一个还有没保存内容的工程。

    四条判据都是 QGIS 换工程前会问的，顺序也是它问的顺序，所以报出来的第一件事就是用户会先看到
    的那一问：

    1. 非 memory 的矢量图层带着没提交的编辑；
    2. 带要素的 memory 图层（设了 ``skipMemoryLayersCheck`` 的除外）或临时图层；
    3. 工程自己的脏标记；
    4. 栅格波段的属性表脏了，循环上界是 ``bandCount()``——最高那一带 QGIS 也不问。

    四条都只有一个意思："丢掉这些继续吗"。所以这里命中一条就拒，而 ``discard_current=true`` 就是
    那句"丢掉"；四问里没有一条能由操作答成"保存"，因为保存对话框只在 QGIS 那边。

    Only ``discard_current=true`` replaces a project that still holds unsaved content.

    All four criteria are what QGIS asks before replacing a project, in the order it asks them, so
    the first thing reported is the question a person would meet first: uncommitted edits in a
    vector layer other than a memory one; a memory layer with features (unless it carries
    ``skipMemoryLayersCheck``) or a temporary layer; the project's own dirty flag; and a dirty
    raster band attribute table, whose loop stops at ``bandCount()`` because QGIS does not ask
    about the top band either.

    All four mean one thing -- "discard this and go on?" -- so one hit refuses, and
    ``discard_current=true`` is that answer.  None of them can be answered "save" from here: only
    QGIS can show the save dialog.
    """

    if discard_current:
        return
    unsaved = _unsaved_content(project)
    if unsaved:
        raise FrameworkError(
            PROJECT_REPLACE_REQUIRED,
            f"the current project holds {', '.join(unsaved)}; "
            "pass discard_current=true to replace it",
        )


def _unsaved_content(project: Any) -> list[str]:
    """工程里没保存下来的东西，按 QGIS 问它们的顺序，每一条只说事实。

    What the project holds unsaved, in the order QGIS asks about it; each entry states a fact.
    """

    from qgis.core import QgsRasterLayer, QgsVectorLayer  # pyright: ignore[reportMissingImports]

    unsaved: list[str] = []
    layers = list(project.mapLayers().values())

    # 1 非 memory 的矢量图层带着没提交的编辑。QGIS 那一问会点名图层，所以这里也带上名字：调用方
    # 靠它才找得到是哪一层。
    # 1 A vector layer other than a memory one holds uncommitted edits.  QGIS names the layer in
    # that question, so its name comes along: it is how the caller finds out which layer it is.
    for layer in layers:
        if not isinstance(layer, QgsVectorLayer):
            continue
        if layer.dataProvider() is None or layer.providerType() == "memory":
            continue
        if layer.isEditable() and layer.isModified():
            unsaved.append(f"a layer with unsaved edits ('{layer.name()}')")
            break

    # 2 带要素的 memory 图层或有临时图层；两者都有时 QGIS 先问临时图层那个。
    # 2 A memory layer with features, or a temporary layer; with both, QGIS warns about the
    # temporary ones first.
    has_memory = False
    has_temporary = False
    for layer in layers:
        if layer.providerType() == "memory":
            if (
                isinstance(layer, QgsVectorLayer)
                and layer.featureCount() != 0
                and not layer.customProperty("skipMemoryLayersCheck")
            ):
                has_memory = True
                break
        elif layer.isTemporary():
            has_temporary = True
    if has_temporary:
        unsaved.append("temporary layers")
    elif has_memory:
        unsaved.append("scratch layers")

    # 3 工程自己脏了。
    # 3 The project itself is dirty.
    if project.isDirty():
        unsaved.append("unsaved changes")

    # 4 栅格的属性表脏了；上界就是 ``bandCount()``，最高那一带不问。QGIS 那一问带图层名与波段号。
    # 4 A raster's attribute table is dirty; the bound is ``bandCount()``, so the top band is not
    # asked about.  QGIS names the layer and the bands in that question.
    for layer in layers:
        if not isinstance(layer, QgsRasterLayer):
            continue
        dirty = [
            band
            for band in range(1, layer.bandCount())
            if (table := layer.attributeTable(band)) is not None and table.isDirty()
        ]
        if dirty:
            bands = ", ".join(str(band) for band in dirty)
            unsaved.append(f"a dirty attribute table on layer '{layer.name()}' (bands {bands})")
            break

    return unsaved


def _refuse_if_unwritable(path: Path) -> None:
    """关联文件存在却写不进去时，用 QGIS 写之前拒的那句话拒。

    When the associated file exists but cannot be written, refuse with the sentence QGIS uses
    before writing.

    ``write()`` 自己也会拒，但它的拒语带整条路径；这一条是 QGIS 在写之前说的那句，短，而且先于任何
    写入。

    ``write()`` refuses as well, but its sentence repeats the whole path; this is the one QGIS says
    before writing, it is short, and it comes before anything is written.
    """

    from qgis.PyQt.QtCore import QFileInfo  # pyright: ignore[reportMissingImports]

    info = QFileInfo(str(path))
    if info.exists() and not info.isWritable():
        raise FrameworkError(PROJECT_SAVE_FAILED, "The project file is not writable.")


def _store_current_view(ctx: OperationContext, project: Any) -> None:
    """把当前的画布范围与旋转存进工程，重开时回到这里。

    The current canvas extent and rotation, stored in the project so reopening returns to them.
    """

    from qgis.core import QgsReferencedRectangle  # pyright: ignore[reportMissingImports]

    canvas = ctx.iface.mapCanvas()
    settings = project.viewSettings()
    settings.setDefaultViewExtent(QgsReferencedRectangle(canvas.extent(), project.crs()))
    settings.setDefaultRotation(canvas.rotation())


def _refuse_if_ends_the_process(path: Path) -> None:
    """在 ``read()`` 之前拒掉实测能让进程直接消失的两种文件形状。

    别的都不在这里判：读不懂、解不开、CRC 对不上、压缩方法少见、声明尺寸撒谎、远程数据源，``read()``
    都能自己回答，而 QGIS 打得开的工程这里也不能拒。这两条都拒在 ``ctx.project.clear()`` 之前，所以
    当前工程没被动过，拒语说的就是这一点。

    Refuses the two file shapes measured to end the process outright, before ``read()`` runs.

    Nothing else is judged here: a file this scan cannot read, an archive with a broken CRC or an
    unusual compression method, one whose declared sizes lie, a remote data source -- ``read()``
    answers all of those, and a project QGIS opens must not be refused.  Both refusals happen
    before ``ctx.project.clear()``, so the current project is untouched, which is what the message
    says.
    """

    try:
        if path.suffix.lower() != ".qgz":
            with path.open("rb") as handle:
                _count_depth(handle)
            return

        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            # 成员表要整份看：解包做的是每个成员，所以一个加密的兄弟成员也足以杀掉进程，哪怕它
            # 不是 ``.qgs``。这一段只读头里的字段，不解压任何成员。
            # The whole member list is looked at: unpacking works through every member, so one
            # encrypted sibling is enough even when it is not the ``.qgs``.  Only header fields are
            # read for this, and no member is decompressed.
            if any(member.flag_bits & _ZIP_ENCRYPTED for member in members):
                raise FrameworkError(
                    PROJECT_OPEN_FAILED,
                    "the project archive holds an encrypted member" + _NOT_CHANGED,
                )
            # 深度只数 ``read()`` 会解析的那一个文档：归档里第一个后缀为 ``.qgs`` 的成员。第二个再
            # 深也进不了 DOM，拒它就是拒一个打得开的工程。
            # Only the document ``read()`` will parse is counted: the first member whose suffix is
            # ``.qgs``.  A second one never reaches the DOM however deep it is, and refusing on it
            # would refuse a project that opens.
            project_member = next(
                (item for item in members if item.filename.lower().endswith(".qgs")), None
            )
            if project_member is not None:
                with archive.open(project_member) as handle:
                    _count_depth(handle)
    except FrameworkError:
        raise
    except Exception:  # noqa: BLE001 - 读不懂的形状交给 read()
        return


class _Depth:
    """解析过程中数当前层数，超过上限就地拒掉。

    注释和 CDATA 不是元素、不加层数，自闭合标签就是它那一个元素——这些都交给解析器，不由字节扫描
    猜。

    Counts the current nesting while the file is parsed, refusing on the spot past the bound.
    Comments and CDATA sections are not elements and add no depth, and a self-closing tag is the
    one element it is: the parser answers those questions.
    """

    __slots__ = ("depth",)

    def __init__(self) -> None:
        self.depth = 0

    def start(self, name: str, attributes: Any) -> None:
        self.depth += 1
        if self.depth > MAX_PROJECT_DEPTH:
            raise FrameworkError(
                PROJECT_OPEN_FAILED,
                f"the project nests deeper than {MAX_PROJECT_DEPTH} levels" + _NOT_CHANGED,
            )

    def end(self, name: str) -> None:
        self.depth -= 1


def _count_depth(handle: Any) -> None:
    """分块喂解析器，只记层数。

    Feed the parser in chunks, counting nesting only.
    """

    scan = _Depth()
    # 不开命名空间处理：``QDomDocument`` 也不开，所以 ``<q:a>``（前缀没声明）在两边都是普通元素
    # 名、都算一层。开了它，这种标签会让 expat 报 unbound prefix，深度没数，那个工程就绕过上限。
    # Namespace processing stays off: ``QDomDocument`` does not turn it on either, so ``<q:a>`` with
    # an undeclared prefix is an ordinary element name on both sides and counts one level.  Turning
    # it on makes expat report an unbound prefix, no depth is counted, and that project gets past
    # the bound.
    parser = expat.ParserCreate()
    parser.StartElementHandler = scan.start
    parser.EndElementHandler = scan.end
    while chunk := handle.read(_CHUNK):
        parser.Parse(chunk, False)
    parser.Parse(b"", True)
