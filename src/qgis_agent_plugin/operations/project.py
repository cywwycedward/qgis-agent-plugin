"""四个 ``project.*`` 操作。

四个都跑在主线程：``QgsProject`` 不能从后台线程碰。其中三个会替换或写入工程状态，所以把自己
标成破坏性，于是调用方不读代码也能看见风险。

每一条判据都取自 QGIS 3.44 桌面版里"同一个用户动作"的源码：``New Project`` 与 ``Open Project``
先问"要保存当前工程吗"，然后才动当前工程；``Save`` 直接写关联文件、不问覆盖，``Save As`` 的
覆盖确认是文件对话框问的。换工程之前的“未保存内容”是 GUI 的四道问句——图层里没提交的编辑、带
要素的 memory 图层或临时图层、工程自己脏了、栅格波段的属性表脏了——所以这里查这四条，两个参数
（``discard_current`` 与 ``overwrite``）代表它们共同的答案；没有“图层数”“关联文件名”这类 GUI
不看的条件。

唯一比 GUI 严的是两处会让进程直接消失的输入（P3 防死）：``read()`` 在 6000 层嵌套上以栈溢出
结束进程，加密的 ``.qgz`` 成员让它以访问越界结束进程。两条都有本机实测，见 ``_refuse_if_too_deep``。

QGIS 在使用它的方法内部 import，所以本模块在裸解释器里仍能加载，路径规则不依赖 QGIS 就能测。

All four run on the main thread, because a ``QgsProject`` may not be touched from a worker, and
the three that replace or write state declare themselves destructive, so a caller can see the risk
without reading the code.

Every criterion comes from the QGIS 3.44 desktop source for the same user action: ``New Project``
and ``Open Project`` ask whether to save the current project before touching it; ``Save`` writes
the associated file without asking about overwriting, and ``Save As`` lets the file dialog ask.
Unsaved content, before a project is replaced, is the GUI's four questions -- a layer with
uncommitted edits, a memory layer holding features or a temporary layer, the project's own
dirty flag, and a raster band's attribute table -- so all four are asked here, and two
parameters (``discard_current`` and ``overwrite``) carry their answers.  Nothing else: the
layer count and the associated file name are not conditions the GUI looks at.

The only place stricter than the GUI is the two inputs that end the process outright (P3, an
anti-death guard): ``read()`` overflows the stack at 6000 levels of nesting, and an encrypted
``.qgz`` member ends it with an access violation.  Both are measured on this machine; see
``_refuse_if_too_deep``.

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

# 保存目标的后缀只有这两种：GUI 的保存对话框总会把后缀补成其中之一。
# The only two suffixes a save target may have: the GUI's save dialog always appends one of them.
PROJECT_SUFFIXES = (".qgs", ".qgz")

# 嵌套超过这么多层的工程在 ``read()`` 之前被拒，因为再深就会把进程打掉。界限只由崩溃定，
# 不由耗时定：本机实测（QGIS 3.44.13，Windows x64）5500 层在 0.10 s 内读完，6000 层让进程立刻以
# 0xC00000FD（栈溢出）结束，而元素名叫什么都一样（``a``/``b``/``maplayer``/``properties`` 四种
# 形状的上界相同），所以这是 DOM 深度的性质，不是某个标签的性质。4096 是 5500 的 1.33 倍余量；
# QGIS 自己写出的 100 层嵌套分组只有 105 层深。900 层要 30 s 才读完，但"慢"不是这里的理由：
# GUI 同样慢，只有崩溃才允许比 GUI 更严。
# A project nested deeper than this is refused before ``read()``, because going deeper ends the
# process. The bound is set by the crash, not by time: measured here (QGIS 3.44.13, Windows x64),
# 5500 levels read in 0.10 s while 6000 levels ended the process at once with 0xC00000FD (stack
# overflow), and the element name made no difference (``a``/``b``/``maplayer``/``properties`` all
# had the same bound), so this is a property of the DOM depth rather than of one tag. 4096 leaves
# 1.33 times the depth measured to succeed; the 100 nested groups QGIS itself writes are 105 deep.
# 900 levels take 30 s to read, but slowness is not the reason anything here is refused: the GUI is
# just as slow, and only a crash lets this operation be stricter.
MAX_PROJECT_DEPTH = 4096

# ``QgsProject.read()`` 对 ``.qgz`` 的做法是把整个归档解开，而加密成员会让它直接结束进程。
# What ``QgsProject.read()`` does with a ``.qgz``: it unpacks the whole archive, and an encrypted
# member ends the process.
_ZIP_ENCRYPTED = 0x1

# ``read()`` 读不出来时的那句话。读失败之前工程已经被清空（GUI 的 ``addProject`` 也在 ``read()``
# 之前 ``closeProject()``），所以这里说的是"已被清空"，不是"可能变了"。
# What ``read()`` leaves behind when it fails.  The project has already been cleared at this point
# (the GUI's ``addProject`` calls ``closeProject()`` before ``read()`` too), so the message says it
# was cleared rather than that it may have changed.
_READ_FAILED = "the project file could not be read; the current project was cleared"

# ``read()`` 之前就拒掉的两种形状（P3 防死）共用这句尾巴：它们还没碰当前工程，所以不能沿用
# ``_READ_FAILED`` 的"已清空"。
# The two shapes refused before ``read()`` (P3, anti-death) share this tail: they have not touched
# the current project, so they may not reuse ``_READ_FAILED``'s "was cleared".
_NOT_CHANGED = "; it was not opened and the current project was not changed"

# 归档里带加密成员的拒语：``QgsZipUtils::unzip`` 解开每个成员，加密的那个会让进程消失。
# The refusal for an archive holding an encrypted member: ``QgsZipUtils::unzip`` unpacks every
# member, and the encrypted one ends the process.
_ENCRYPTED = "the project archive holds an encrypted member"

# 流式喂给解析器的一块。整个文件不进内存，于是实体展开之类的放大不会变成本扫描的峰值。
# One chunk fed to the parser.  The file never has to fit in memory, so amplification such as
# entity expansion does not become this scan's peak.
_CHUNK = 1024 * 1024


def _save_target(raw: str) -> str:
    """一个保存目标的形状：后缀是 ``.qgs`` 或 ``.qgz``。

    只管后缀：绝对路径由 ``absolute_path`` 判，文件系统的问题由 ``write()`` 回答。
    ``Save`` 用工程自己的关联文件时没有这条要求，因为 GUI 那时不经过保存对话框。

    The shape of a save target: a ``.qgs`` or ``.qgz`` suffix.  Only the suffix belongs here:
    ``absolute_path`` judges the absolute form and ``write()`` answers file system questions.
    ``Save`` on the project's own associated file does not have this requirement, because the GUI
    does not go through the save dialog then.
    """

    if Path(raw).suffix.lower() not in PROJECT_SUFFIXES:
        raise ValueError("path must be a .qgs or .qgz path")
    return raw


class ProjectCreateParams(Params):
    # 给了就用它，不给就留 QGIS 自己给新工程的 CRS（设置里的 ``projections/defaultProjectCrs``，
    # 以及默认模板里的那个）。GUI 的新建工程从来不问 CRS，所以这里没有"必须有效"这一条。
    # Given, it is used; absent, the CRS QGIS itself gives a new project stays (the
    # ``projections/defaultProjectCrs`` setting, and whatever the default template carries).  The
    # GUI's new-project action never asks for a CRS, so there is no "must be valid" rule here.
    crs_authid: StrictStr | None = Field(default=None, min_length=1)
    discard_current: StrictBool = False


class ProjectCreateResult(Result):
    crs_authid: StrictStr
    file_path: StrictStr | None = None
    layer_count: int = 0


class ProjectOpenParams(Params):
    path: StrictStr = Field(min_length=1)
    discard_current: StrictBool = False


class ProjectOpenResult(Result):
    file_path: StrictStr
    crs_authid: StrictStr
    layer_count: int
    # 一个有图层加载不了的工程照样读进来，但不报成完全可用，也不悄悄算失败。
    # A project whose layers cannot load is read, but it is not reported as fully usable, and it is
    # not silently called a failure either.
    invalid_layer_count: int


class ProjectSaveParams(Params):
    path: Annotated[StrictStr, AfterValidator(_save_target)] | None = None
    overwrite: StrictBool = False


class ProjectSaveResult(Result):
    file_path: StrictStr
    is_dirty: StrictBool


class ProjectGetInfoParams(Params):
    pass


class ProjectGetInfoResult(Result):
    file_path: StrictStr | None
    crs_authid: StrictStr
    is_dirty: StrictBool
    layer_count: int
    # 标题栏显示的就是它：工程标题，没设置时是空串。
    # What the window title shows: the project title, empty when none is set.
    title: StrictStr
    project_epoch: int


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
        from qgis.core import QgsCoordinateReferenceSystem  # pyright: ignore[reportMissingImports]

        # 问句在前、动作在后，与 GUI 的顺序一致：先把"要丢掉当前工程吗"回答掉，再动工程。
        # The question comes before the action, as it does in the GUI: answer "discard it?" first,
        # then touch the project.
        _refuse_replacement_unless_discarding(ctx, request.discard_current)

        if not ctx.iface.newProject(promptToSaveFlag=False):
            # QGIS 拒绝建工程既不是调用方的错，也不是图层或路径问题，所以归内部错误。
            # QGIS declining to create a project is not a caller mistake and not a layer or path
            # problem, so it is an internal error.
            raise FrameworkError(INTERNAL_ERROR, "QGIS did not create a new project")

        if request.crs_authid is not None:
            ctx.project.setCrs(QgsCoordinateReferenceSystem(request.crs_authid))
        # GUI 建完工程最后一步就是 ``setDirty( false )``，而 ``setCrs`` 会把它标脏；照抄这个顺序，
        # 新建的工程才是"没有未保存内容"的那一个。
        # The GUI's last step after creating a project is ``setDirty( false )``, and ``setCrs``
        # dirties it; copying that order is what makes a new project the one without unsaved
        # content.
        ctx.project.setDirty(False)

        return ProjectCreateResult(
            # 报工程现在持有的规范 authid：调用方给的可能是合法别名（``epsg:4326``），也可能
            # 根本构造不出 CRS，那时 QGIS 留下的是空 authid，这里照实报。
            # Report the canonical authid the project now holds: the caller's string may be a valid
            # alias (``epsg:4326``) or name no CRS at all, in which case QGIS keeps an empty authid
            # and that is what is reported.
            crs_authid=ctx.project.crs().authid(),
            file_path=ctx.project.fileName() or None,
            layer_count=len(ctx.project.mapLayers()),
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
        # 后缀不判：GUI 的打开对话框收下你打的任何文件名，``QgsProject.read()`` 也只按后缀决定
        # "是不是压缩包"、剩下的交给解析器；拒掉某种后缀就是拒掉 GUI 打得开的工程。
        # The suffix is not judged: the GUI's open dialog accepts whatever name you type, and
        # ``QgsProject.read()`` only uses the suffix to decide "archive or not" and leaves the rest
        # to the parser; refusing a suffix would refuse projects the GUI opens.
        path = absolute_path(request.path)
        _refuse_replacement_unless_discarding(ctx, request.discard_current)
        _refuse_if_too_deep(path)

        # GUI 的 ``addProject`` 在 ``read()`` 之前先 ``closeProject()``：于是读失败时眼前这个工程
        # 是空的，而不是"旧内容配上新文件名"。这里照抄，失败状态才和 GUI 一样。
        # The GUI's ``addProject`` calls ``closeProject()`` before ``read()``, so a failed read
        # leaves an empty project rather than the old contents under the new file name.  Copying
        # that is what makes the failure state the same as the GUI's.
        ctx.project.clear()

        # 用 ``read()`` 而不是可能弹窗的 ``iface.addProject()``：拒绝必须是错误码，不是对话框。
        # ``read()`` rather than ``iface.addProject()``, which may prompt: a refusal has to be an
        # error code, never a modal dialog.
        if not ctx.project.read(str(path)):
            raise FrameworkError(PROJECT_OPEN_FAILED, _READ_FAILED)

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
    description = "Write the current project to a local .qgs or .qgz file"
    input_model = ProjectSaveParams
    output_model = ProjectSaveResult
    destructive = True

    def execute_main(self, ctx: OperationContext, request: ProjectSaveParams) -> ProjectSaveResult:
        project = ctx.project
        # 给了路径就是 Save As，没给就是 Save：后者写工程自己的关联文件，GUI 那时不弹对话框。
        # A path means Save As, no path means Save: the latter writes the project's own associated
        # file and the GUI shows no dialog for it.
        target = request.path if request.path is not None else project.fileName()
        if not target:
            raise FrameworkError(
                PROJECT_PATH_REQUIRED, "no path was given and the project has no associated file"
            )

        path = absolute_path(target)
        # 只有 Save As 有覆盖确认：GUI 的对话框问"要覆盖吗"，而 Save 直接写关联文件、不问。
        # Only Save As asks about overwriting: the GUI's dialog asks, while Save writes the
        # associated file directly without asking.
        if request.path is not None and path.exists() and not request.overwrite:
            raise FrameworkError(
                TARGET_EXISTS, "the target file exists; pass overwrite=true to replace it"
            )

        if not project.write(str(path)):
            raise FrameworkError(
                PROJECT_SAVE_FAILED,
                "the project could not be written; the target file may be partially written",
            )
        # ``write()`` 成功后会清掉脏标记，所以这里报的是 QGIS 留下的状态，而不是照抄一份示例输出。
        # ``write()`` clears the dirty flag on success, so this reports the project as QGIS left it
        # rather than echoing a sample output.
        return ProjectSaveResult(file_path=str(path), is_dirty=project.isDirty())


class ProjectGetInfo(
    BaseQuery[ProjectGetInfoParams, ProjectGetInfoResult],
    MainThreadOperationMixin[ProjectGetInfoParams, ProjectGetInfoResult],
):
    name = "project.get_info"
    description = "Read the current project's file, CRS, dirty flag, layer count, title and epoch"
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
            project_epoch=ctx.lifecycle.current_epoch,
        )


def _refuse_if_too_deep(path: Path) -> None:
    """在 ``read()`` 之前拒掉会打掉进程的嵌套深度，以及 ``read()`` 解不开的加密成员。

    这是 P3 那一类：只有“进程直接死掉”才允许比 GUI 严，所以只留两条**实测**能杀掉进程的形状，
    其余一律交给 ``read()``：

    - 嵌套超过 ``MAX_PROJECT_DEPTH`` 层 → 栈溢出。实测 6000 层立刻 0xC00000FD，5500 层 0.10 s
      读完。
    - ``.qgz`` 里加密的成员 → 访问越界。实测整包一个成员带通用标志位 0x1，``read()`` 里直接
      0xC0000005，连异常都没有。

    两条都拒在 ``ctx.project.clear()`` 之前，所以当前工程没被动过；用的消息（``_NOT_CHANGED``）
    说的是这一点，与 ``read()`` 读失败后那句“已被清空”（``_READ_FAILED``）分开。

    被量过、但**不**杀进程的形状一个都不留：CRC 对不上的成员 ``read()`` 照读（实测 ok=True）、
    用的是 bzip2 压缩的成员也照读（实测 ok=True）、声明的解压尺寸撒谎只是让 ``read()`` 返回假
    （实测 ok=False，和这里拒掉的结果同一个码）、XML 实体 QGIS 根本不展开（实测 1 MiB 实体引用 50
    次 0.53 s 读完 ok=True）、64 MiB 的工程 1.80 s 读完（所以没有体积上限）。远程数据源也不看：
    GUI 会去取它，那只是慢。

    扫描读不懂的文件不是这里的结论：解析报错就返回，让 ``read()`` 用自己的码回答。

    The P3 category: being stricter than the GUI is allowed only for "the process dies", so only
    the two shapes **measured** to kill it stay here, and everything else is left to ``read()``:

    - nesting deeper than ``MAX_PROJECT_DEPTH`` -> stack overflow.  Measured: 6000 levels ended the
      process at once with 0xC00000FD, 5500 levels read in 0.10 s.
    - an encrypted member inside a ``.qgz`` -> access violation.  Measured: one member with general
      purpose flag 0x1 took the process out with 0xC0000005 inside ``read()``, not even an
      exception.

    Both refuse before ``ctx.project.clear()``, so the current project has not been touched, and the
    message they use (``_NOT_CHANGED``) says so; that is why it is separate from the "was cleared"
    sentence a failed ``read()`` reports (``_READ_FAILED``).

    Nothing that was measured and does **not** kill it survives: a member whose CRC does not match
    is read anyway (measured ok=True), so is one compressed with bzip2 (measured ok=True), a
    member lying about its decompressed size only makes ``read()`` return false (measured ok=False,
    the same code refusing it here would give), XML entities are not expanded by QGIS at all
    (measured: a 1 MiB entity referenced 50 times read in 0.53 s, ok=True), and a 64 MiB project
    read in 1.80 s (which is why there is no size bound left).  Remote data sources are not looked
    at either: the GUI goes and fetches them, and that is merely slow.

    A file this scan cannot read is not its verdict: a parse error returns and ``read()`` answers
    with its own code.
    """

    try:
        _walk_documents(path)
    except FrameworkError:
        raise
    except Exception:  # noqa: BLE001 - 任何读不懂的形状都交给 read()
        return


def _walk_documents(path: Path) -> None:
    """把 ``read()`` 会解析的那个工程文档交给深度计数器，并检查归档里有没有加密成员。

    ``.qgz`` 的成员表要整份看：``QgsZipUtils::unzip`` 解开每个成员
    （``src/core/qgsziputils.cpp:78``），所以一个加密的兄弟成员也足以杀掉进程，哪怕它不是
    ``.qgs``。成员表只看头部字段，不解压任何成员。

    深度却只数**一个**文档：``QgsProjectArchive::projectFile()`` 只返回归档里第一个后缀为 ``.qgs``
    的成员（``src/core/qgsarchive.cpp:147``–``157``），``QgsProject::unzip`` 也只把它交给
    ``readProjectFile()``（``src/core/project/qgsproject.cpp:4677``）。第二个 ``.qgs`` 再深也进不了
    DOM，拒它就是拒一个 GUI 打得开的工程。

    The whole member list of a ``.qgz`` is looked at: ``QgsZipUtils::unzip`` unpacks every member
    (``src/core/qgsziputils.cpp:78``), so a single encrypted sibling kills the process even when it
    is not the ``.qgs``; only header fields are read for that check, and no member is decompressed
    for it.

    The depth counts **one** document only: ``QgsProjectArchive::projectFile()`` returns the first
    member whose suffix is ``.qgs`` (``src/core/qgsarchive.cpp:147``-``157``), and
    ``QgsProject::unzip`` hands only that one to ``readProjectFile()``
    (``src/core/project/qgsproject.cpp:4677``).  A second ``.qgs`` never reaches the DOM, however
    deep it is, and refusing on it would refuse a project the GUI opens.
    """

    if path.suffix.lower() != ".qgz":
        with path.open("rb") as handle:
            _count_depth(handle)
        return

    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        for member in members:
            if member.flag_bits & _ZIP_ENCRYPTED:
                raise FrameworkError(PROJECT_OPEN_FAILED, _ENCRYPTED + _NOT_CHANGED)
        project_member = next(
            (item for item in members if item.filename.lower().endswith(".qgs")), None
        )
        if project_member is not None:
            with archive.open(project_member) as handle:
                _count_depth(handle)


class _Depth:
    """解析过程中数最高层数，超过上限就地拒掉。

    注释和 CDATA 不是元素、不加层数，自闭合标签就是它那一个元素——这些都交给解析器，不由字节扫描
    猜。

    Counts the deepest nesting while the file is parsed, refusing on the spot when it passes the
    bound.  Comments and CDATA sections are not elements and add no depth, and a self-closing tag is
    the one element it is: the parser answers those questions, not a guess over bytes.
    """

    __slots__ = ("depth", "peak")

    def __init__(self) -> None:
        self.depth = 0
        self.peak = 0

    def start(self, name: str, attributes: Any) -> None:
        self.depth += 1
        if self.depth > self.peak:
            self.peak = self.depth
        if self.depth > MAX_PROJECT_DEPTH:
            raise FrameworkError(
                PROJECT_OPEN_FAILED,
                f"the project file nests deeper than {MAX_PROJECT_DEPTH} levels" + _NOT_CHANGED,
            )

    def end(self, name: str) -> None:
        self.depth -= 1


def _count_depth(handle: Any) -> None:
    """分块喂解析器，只记层数：整个文件不必进内存，实体展开也不累积。

    Feed the parser in chunks, counting nesting only: the file need not fit in memory, and entity
    expansion accumulates nothing.
    """

    scan = _Depth()
    # 开启命名空间处理，于是 ``<qgs:a>`` 和 ``<a>`` 算同一层，和 ``QDomElement`` 看到的一样。
    # Namespace processing, so ``<qgs:a>`` and ``<a>`` take the same level, as ``QDomElement``
    # sees them.
    parser = expat.ParserCreate(namespace_separator="}")
    parser.StartElementHandler = scan.start
    parser.EndElementHandler = scan.end
    while chunk := handle.read(_CHUNK):
        parser.Parse(chunk, False)
    parser.Parse(b"", True)


def _refuse_replacement_unless_discarding(ctx: OperationContext, discard_current: bool) -> None:
    """只有明确的 ``discard_current=true`` 才能把有未保存内容的工程换掉。

    GUI 在换掉工程之前会按同一个顺序问四句话（``QgisApp::fileNew`` 与 ``openProject(QString)``
    调的是同一串，``qgisapp.cpp:5820`` / ``:6943``）：

    1. ``checkUnsavedLayerEdits()``（``:13560``）：非 memory 的矢量图层里有
       ``isEditable() && isModified()`` 的（``:13573``、``:13578``）就逐层问“要保存编辑吗”；
    2. ``checkMemoryLayers()``（``:13669``）：有带要素的 memory 图层（``:13684``，设了
       ``skipMemoryLayersCheck`` 的除外）或有临时图层（``:13690``）；
    3. ``saveDirty()``（``:13481``）：先为同一批图层编辑 ``markDirty()``（``:13509``），再问
       ``isDirty()``（``:13522``）；
    4. ``checkUnsavedRasterAttributeTableEdits()``（``:13589``）：栅格波段自己的属性表脏了
       （``:13615``）。

    四句话合起来只有一个意思——“丢掉这些继续吗”。所以这里只要有一条成立就拒，而
    ``discard_current=true`` 就是那句“丢掉”；这四句里没有一句能由 operation 答成“保存”，因为只有
    GUI 能弹保存对话框。图层数与关联文件名都不在条件里：一个没改过的工程（哪怕有图层）GUI 不问
    就换掉。

    两个设置不读（``qgis/askToSaveProjectChanges``、``askToSaveMemoryLayers``）：它们只决定 GUI
    问不问，而 operation 那侧的问句就是参数；读它们会让同一组入参在两种设置下给出不同结果。想走
    “不问了、直接丢”那条路，调用方给 ``discard_current=true`` 即可。

    The GUI asks four questions before it replaces a project, in one fixed order
    (``QgisApp::fileNew`` and ``openProject(QString)`` call the same chain, ``qgisapp.cpp:5820``
    / ``:6943``): vector layers other than memory ones with ``isEditable() && isModified()``
    (``:13573``, ``:13578``);
    memory layers holding features (``:13684``, unless ``skipMemoryLayersCheck``) or temporary
    layers (``:13690``); the project being dirty (``:13522``, after ``saveDirty()`` marks it at
    ``:13509`` for those same layer edits); and a raster band's own attribute table being dirty
    (``:13615``).

    All four mean one thing -- "discard this and go on?" -- so one true condition is enough to
    refuse, and ``discard_current=true`` is that answer.  None of the four can be answered "save"
    from an operation, because only the GUI can show a save dialog.  Neither the layer count nor the
    associated file name is part of the condition: the GUI replaces a project nobody edited, layers
    and all, without asking.

    Two settings are not read (``qgis/askToSaveProjectChanges``, ``askToSaveMemoryLayers``): they
    only decide whether the GUI asks, and the operation's side of each question is the parameter;
    reading them would make one set of arguments mean two things.
    """

    if discard_current:
        return
    asked = _unanswered_questions(ctx.project)
    if asked:
        raise FrameworkError(
            PROJECT_REPLACE_REQUIRED,
            f"the current project holds {', '.join(asked)}; "
            "pass discard_current=true to replace it",
        )


def _unanswered_questions(project: Any) -> list[str]:
    """GUI 会问、而 operation 只能由 ``discard_current`` 回答的那几句话，命中的那些。

    每一条只照抄 GUI 的条件，不多不少；顺序就是 GUI 问它们的顺序，所以报出来的第一句话就是用户
    在桌面版上会先看到的那句。

    Every entry is the GUI's condition copied, no more and no less; the order is the order the GUI
    asks them in, so the first phrase reported is the question a desktop user would see first.
    """

    from qgis.core import QgsRasterLayer, QgsVectorLayer  # pyright: ignore[reportMissingImports]

    asked: list[str] = []
    layers = list(project.mapLayers().values())

    # 1 非 memory 的矢量图层带着没提交的编辑（``qgisapp.cpp:13573``、``:13578``）。
    # 1 A vector layer other than a memory one holds uncommitted edits (``:13573``, ``:13578``).
    for layer in layers:
        if not isinstance(layer, QgsVectorLayer) or layer.providerType() == "memory":
            continue
        if layer.isEditable() and layer.isModified():
            asked.append("a layer with unsaved edits")
            break

    # 2 带要素的 memory 图层或有临时图层（``qgisapp.cpp:13684``、``:13690``）。两个都有时 GUI
    # 先问临时图层那个警告（``:13698`` 的 ``if ( hasTemporaryLayers ) … else if``）。
    # 2 A memory layer with features, or a temporary layer (``:13684``, ``:13690``).  With both, the
    # GUI warns about the temporary ones first (``:13698``), so the message does too.
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
        asked.append("temporary layers")
    elif has_memory:
        asked.append("scratch layers")

    # 3 工程自己是脏的（``qgisapp.cpp:13522``）。
    # 3 The project itself is dirty (``:13522``).
    if project.isDirty():
        asked.append("unsaved changes")

    # 4 栅格的属性表脏了。上界就是 GUI 那个 ``bandNo < bandCount()``（``qgisapp.cpp:13615``）：
    # 最高那一带 GUI 不问，这里也不问，否则就是比 GUI 严。
    # 4 A raster's attribute table is dirty.  The bound is the GUI's own ``bandNo < bandCount()``
    # (``:13615``): the top band is not asked about there either, and asking here would be stricter
    # than the GUI.
    for layer in layers:
        if not isinstance(layer, QgsRasterLayer):
            continue
        for band in range(1, layer.bandCount()):
            table = layer.attributeTable(band)
            if table is not None and table.isDirty():
                asked.append("a dirty raster attribute table")
                break
    return asked
