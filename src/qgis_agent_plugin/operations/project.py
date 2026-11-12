"""四个 ``project.*`` 操作。

四个都跑在主线程：``QgsProject`` 不能从后台线程碰。其中三个会替换或写入工程状态，所以把自己
标成破坏性，于是调用方不读代码也能看见风险。

``create`` 与 ``open`` 共用一道闸：一个还装着值得保留的东西（图层、关联文件、未保存改动）的
工程，只有在调用方说 ``discard_current=true`` 时才会被替换。``save`` 对文件有对应的闸：已存在
的目标只有在 ``overwrite=true`` 时才会被覆盖。

QGIS 在使用它的方法内部 import，所以本模块在裸解释器里仍能加载，路径与 CRS 规则不依赖 QGIS 就能测。

All four run on the main thread, because a ``QgsProject`` may not be touched from a
worker, and the three that replace or write state declare themselves destructive, so
a caller can see the risk without reading the code.

``create`` and ``open`` share one guard: a project that holds anything worth keeping
-- layers, an associated file, unsaved changes -- is only replaced when the caller
says ``discard_current=true``.  ``save`` has the matching guard for files: an existing
target is only replaced with ``overwrite=true``.

QGIS is imported inside the methods that need it, so this module still loads in a
bare interpreter and the path/CRS rules stay testable without QGIS.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Annotated, Any
from xml.parsers import expat

from pydantic import AfterValidator, Field, StrictBool, StrictStr

from ..core.contracts import (
    BaseCommand,
    BaseQuery,
    MainThreadOperationMixin,
    OperationContext,
)
from ..core.models import (
    INTERNAL_ERROR,
    INVALID_CRS,
    PATH_NOT_ALLOWED,
    PROJECT_OPEN_FAILED,
    PROJECT_PATH_REQUIRED,
    PROJECT_REPLACE_REQUIRED,
    PROJECT_SAVE_FAILED,
    TARGET_EXISTS,
    FrameworkError,
    Params,
    Result,
)
from ..core.security import resolve_path

# 本插件只读写这两种格式 / the two formats this plugin reads and writes;
# 别的都是调用方的错 / anything else is a caller mistake.
PROJECT_SUFFIXES = (".qgs", ".qgz")

# 超过这个大小的 ``.qgs``，或 ``.qgz`` 所有成员之和，直接拒掉不去读 / a ``.qgs``, or every
# member of a ``.qgz`` together, above this size is refused rather than read: 扫描在主线程跑 /
# the scan runs on the main thread, 而小归档里的成员能解压成几十亿字节 / and a small archive
# 能装下解压后几十亿字节的成员 / can hold members that expand to gigabytes.
MAX_PROJECT_BYTES = 16 * 1024 * 1024

# XML 嵌套深度超过这个值就在 ``read()`` 之前被拒 / an XML nesting depth above this is
# refused before ``read()``. 界限由时间定，不是由崩溃定 / the bound is set by time, not by
# the crash: ``QgsProject.read()`` 递归解析工程，代价比深度增长更快 / it parses the project
# recursively and its cost grows faster than the depth（QGIS 3.44.13、Windows x64 实测 /
# measured on QGIS 3.44.13, Windows x64: 200 层嵌套图层树分组 0.28 s / 200 nested layer-tree
# groups 0.28 s, 600 层 8.84 s / 600 groups 8.84 s, 900 层 30.11 s / 900 groups 30.11 s --
# 而一个 4.2 MB 的平铺工程只读 0.14 s / while a flat 4.2 MB project read in 0.14 s, 所以那
# 30 s 是深度造成的 / so those 30 s are the depth). 100 层嵌套分组是深度 105 / 100 nested
# groups, a real project QGIS writes, are depth 105; 256 留了 2.4 倍余量 / 256 keeps 2.4 times
# that, 按实测曲线约半秒 / and about half a second by the measured curve. 5500 层还能打开 /
# 5500 levels still opened, 6000 层让进程以 0xC00000FD 死掉 / and 6000 killed the process with
# 0xC00000FD, 而只耗时 30 s 的文件已经卡住主线程 / while a file that merely takes 30 s has
# 已经停住了它 / stopped it already.
MAX_PROJECT_DEPTH = 256

# 解析器最多被问这么多个标签 / the parser is asked about at most this many tags.
# 每个元素有一个起始标签，所以 ``bytes.count(b"<")`` 是元素数的上界 / every element has one
# start tag, so ``bytes.count(b"<")`` bounds the element count: 一次 memchr 就在解析前定界 /
# one memchr pass bounds the parse before it starts（实测 40 万标签解析 0.12-0.19 s /
# 400000 tags parse in 0.12-0.19 s, measured）. 一个普通的 16 MB 工程有 26-39 万个标签 /
# an ordinary 16 MB project holds 260000-390000 tags（实测每标签 42.6-61 字节 /
# 42.6 to 61 bytes per tag in the measured projects）, 所以这个界放得过真实工程 / so this
# bound leaves a real project under it / 于是真实工程仍在界内;
# 标签炸弹按计数拒掉，永不走一遍 / tag spam is refused by the count and never walked.
MAX_PROJECT_TAGS = 400_000

# 一个 ``.qgz`` 最多能有几个成员 / the most members a ``.qgz`` may hold. 字节预算数的是
# 解压后的字节，空成员在那里不花钱 / the byte budget counts decompressed bytes, so an empty
# member costs nothing there -- 但每个成员都要一次 ``ZipFile.open()`` / but every member costs
# one ``ZipFile.open()``: 10.4 MB 归档里 10 万个空成员要 1.75 s / 100000 empty members in a
# 10.4 MB archive took 1.75 s（实测 / measured）, 与数量线性 / linear in the count. 数量要等
# ``zipfile`` 解析完整个中央目录才知道 / the count is only known after ``zipfile`` has parsed
# the whole central directory, 这本身也要内存 / which costs memory as well（39 MB 归档里 40 万
# 个空成员：2.92 s、222 MiB / 400000 empty members in a 39 MB archive: 2.92 s and 222 MiB）,
# 所以归档自身的大小在打开前先限住 / so the archive's own size is bounded before it is
# opened, 这个界兜住还塞得下的部分 / and this bound is the backstop for what still fits.
# 工程归档就是一个 ``.qgs`` 加附件 / a project archive is a ``.qgs`` and its attachments,
# 不是几千个条目 / not thousands of entries.
MAX_ARCHIVE_MEMBERS = 1000

# ``QgsProject.read()`` 对 ``.qgz`` 的做法：整个归档都解开 / what ``QgsProject.read()``
# does with a ``.qgz``: it unpacks the whole archive, 所以它解不出的成员 / so a member it
# 不是扫描能跳过的 / cannot extract is not a member the scan may skip.
# 加密成员和 QGIS 不读的压缩方式在 ``read()`` 之前就拒 / so encrypted members and compression
# 方法也一并拒掉 / methods QGIS does not read are refused before it sees the file.
_ZIP_ENCRYPTED = 0x1
_UNPACKABLE = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})

# 从归档成员里读一次 / one read from an archive member: 预算按解压字节算 / the budget counts
# decompressed bytes, 不信清单声称的大小 / never the size the manifest claims.
_CHUNK = 1024 * 1024

# 只看一个值的开头 / only the start of a value is judged, 所以元素文本只留前导空白之后的
# 这么多字符 / so an element's text is kept up to this many characters past its leading
# whitespace. 最长的标记是 scheme 加 ``://``（``https://`` 八个字符）/ the longest marker is
# a scheme plus ``://`` (``https://`` is eight), 于是每种真实 scheme 都有余地 / which leaves
# every real scheme room; 这个界省掉每层重拼一次 16 MB 文本 / it saves re-joining a 16 MB
# 文本，嵌套 256 层也一样 / text once per nesting level, 256 levels deep included.
# shortcut: scheme 超过 59 字符的值看不见, 有 provider 真这么拼就该升级
# 英文：a value whose scheme is longer than 59 characters is not seen / upgrade once a
# provider starts spelling one: 这就是升级条件 / that is the trigger.
_VALUE_HEAD = 64

# 值最开头的一个 scheme / a scheme at the very start of a value. ``EPSG:4326`` 和
# ``C:/data`` 有冒号但没有 ``//`` / they carry a colon but no ``//``, 而两者都是 QGIS 会写进
# 工程的值 / and both are values QGIS writes into a project.
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")

# 一个 GDAL 子数据集源，``decodeUri`` 会整串返回 / a GDAL subdataset source, which
# ``decodeUri`` 返回整串（见 ``_subdataset_path``）/ returns it whole (see ``_subdataset_path``):
# 大写驱动名前缀 / an upper-case driver prefix, 然后是被引号包住或跟在 ``WORD:<index>:`` 后的
# 文件 / then the file, quoted or behind a ``WORD:<index>:`` prefix.
_SUBDATASET = re.compile(r"\A[A-Z][A-Z0-9_]+:")
_SUBDATASET_INDEX = re.compile(r"\A(?:\d+|off):", re.IGNORECASE)
_ABSOLUTE_PATH = re.compile(r"\A(?:[A-Za-z]:[\\/]|[\\/])")

# 数据源是唯一按包含关系判断的值 / a data source is the one value judged by containment:
# GDAL 在自己的本地语法里拼一个远程文件 / GDAL spells a remote file inside its own local
# syntax (``NETCDF:"/vsicurl/http://…":var``), 而标记可能落在这个扫描保留的开头之后 / and
# the marker can sit past the head this scan keeps. ``_MARKER_TAIL`` 是保留的末尾字符数 /
# the number of trailing characters kept, 而它让跨两个字符事件的标记照样看得见 / and it keeps
# 跨事件的标记不漏 / a marker split across two character-data events visible.
_MARKER = "://"
_MARKER_TAIL = len(_MARKER) - 1

# ``read()`` 对无法解析的文件的说法 / what ``read()`` reports for a file it cannot parse;
# 扫描用同一句话 / the scan uses the same sentence, 不自己发明另一个错误 / rather than
# 发明一个自己的错误 / inventing an error of its own.
_READ_FAILED = "the project file could not be read; the current project may have changed"

# 两条远程规则共用这句拒绝语 / the refusal both remote rules share: 哪个值点了主机名不进
# 消息 / which value named the host stays out of it.
_REMOTE_SOURCE = "the project file names a remote data source; it was not opened"


def _project_path(raw: str) -> str:
    """在任何文件系统调用之前，先拒掉形状不对的路径。

    这里只管形状：目录是否允许、文件是否存在属于路径策略的判断，所以形状合法的路径仍然要过
    ``resolve_path``。

    Only the shape belongs here: whether the directory is allowed, and whether the
    file exists, is the path policy's decision, so a well-formed path still has to
    survive ``resolve_path``.
    """

    path = Path(raw)
    if not path.is_absolute() or path.suffix.lower() not in PROJECT_SUFFIXES:
        raise ValueError("path must be an absolute .qgs or .qgz path")
    return raw


class ProjectCreateParams(Params):
    crs_authid: StrictStr = Field(default="EPSG:4326", min_length=1)
    discard_current: StrictBool = False


class ProjectCreateResult(Result):
    crs_authid: StrictStr
    file_path: StrictStr | None = None
    layer_count: int = 0


class ProjectOpenParams(Params):
    path: Annotated[StrictStr, AfterValidator(_project_path)]
    discard_current: StrictBool = False


class ProjectOpenResult(Result):
    file_path: StrictStr
    crs_authid: StrictStr
    layer_count: int
    # 一个有图层加载不了的工程照样读进来 / a project whose layers cannot load is read, 但不
    # 报成完全可用 / but it is not reported as fully usable, 也不悄悄算失败 / and it is not
    # 当成失败 / silently called a failure either.
    invalid_layer_count: int


class ProjectSaveParams(Params):
    path: Annotated[StrictStr, AfterValidator(_project_path)] | None = None
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
    project_epoch: int


class ProjectCreate(
    BaseCommand[ProjectCreateParams, ProjectCreateResult],
    MainThreadOperationMixin[ProjectCreateParams, ProjectCreateResult],
):
    name = "project.create"
    description = "Replace the current project with a new empty one on a given CRS"
    input_model = ProjectCreateParams
    output_model = ProjectCreateResult
    destructive = True

    def execute_main(
        self, ctx: OperationContext, request: ProjectCreateParams
    ) -> ProjectCreateResult:
        from qgis.core import QgsCoordinateReferenceSystem

        # 无法使用的 CRS 是调用方的错 / an unusable CRS is a caller mistake,
        # 在碰到当前工程之前就拒掉 / refused before the current project is touched.
        crs = QgsCoordinateReferenceSystem(request.crs_authid)
        if not crs.isValid():
            raise FrameworkError(INVALID_CRS, f"{request.crs_authid} is not a usable CRS")
        _refuse_replacement_unless_discarding(ctx, request.discard_current)

        if not ctx.iface.newProject(promptToSaveFlag=False):
            # QGIS 拒绝建工程既不是调用方的错 / QGIS declining to create a project is not a
            # 也不是图层或路径问题 / caller mistake, and not a layer or path problem,
            # 所以归内部错误 / so it is an internal error.
            raise FrameworkError(INTERNAL_ERROR, "QGIS did not create a new project")

        ctx.project.setCrs(crs)
        return ProjectCreateResult(
            # 调用方给的字符串可能是合法别名（``epsg:4326``）/ the caller's string may be a
            # valid alias (``epsg:4326``); 这里报工程现在持有的规范 authid / so report the
            # canonical authid the project now holds, 与 ``open``/``get_info`` 一致 / as they do.
            crs_authid=crs.authid(),
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
        path = resolve_path(request.path, ctx.policy.allowed_project_roots, must_exist=True)
        _refuse_replacement_unless_discarding(ctx, request.discard_current)
        _refuse_remote_datasources(path)

        # 用 ``read()`` 而不是可能弹窗的 ``iface.addProject()`` / ``read()`` rather than
        # ``iface.addProject()``, which may prompt: 拒绝必须是错误码 / a refusal has to be an
        # error code, 永远不是模态对话框 / never a modal dialog.
        if not ctx.project.read(str(path)):
            raise FrameworkError(
                PROJECT_OPEN_FAILED,
                "the project file could not be read; the current project may have changed",
            )

        layers = ctx.project.mapLayers()
        outside = _sources_outside_the_allowed_roots(ctx, layers)
        if outside:
            # 这些源是工程文件自己点的名 / the project file named these sources, 所以
            # ``read()`` 已经碰到它们 / so ``read()`` already reached them; 移除它们才能让新工程
            # 留在调用方被承诺的策略之内 / removing them is what keeps the new project inside
            # 那条策略 / the policy the caller was promised.
            for layer_id in outside:
                ctx.project.removeMapLayer(layer_id)
            raise FrameworkError(
                PATH_NOT_ALLOWED,
                f"{len(outside)} layer data source(s) are outside allowed_data_roots; "
                "they were removed and the project was replaced",
            )
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
        target = request.path or project.fileName()
        if not target:
            raise FrameworkError(
                PROJECT_PATH_REQUIRED, "no path was given and the project has no associated file"
            )

        path = resolve_path(target, ctx.policy.allowed_project_roots, must_exist=False)
        # 关联文件也算在内 / the associated file counts too: 覆盖它是一个丢文件的决定 /
        # overwriting it is a file loss decision, 得由调用方明确做出 / which the caller has
        # 明确给出 / to make explicitly.
        if path.exists() and not request.overwrite:
            raise FrameworkError(
                TARGET_EXISTS, "the target file exists; pass overwrite=true to replace it"
            )

        if not project.write(str(path)):
            raise FrameworkError(
                PROJECT_SAVE_FAILED,
                "the project could not be written; the target file may be partially written",
            )
        # ``write()`` 成功后会清掉脏标记 / ``write()`` clears the dirty flag on success,
        # 所以这里报的是 QGIS 留下的状态 / so this reports the project as QGIS left it,
        # 而不是照抄一份示例输出 / not an echoed sample.
        return ProjectSaveResult(file_path=str(path), is_dirty=project.isDirty())


class ProjectGetInfo(
    BaseQuery[ProjectGetInfoParams, ProjectGetInfoResult],
    MainThreadOperationMixin[ProjectGetInfoParams, ProjectGetInfoResult],
):
    name = "project.get_info"
    description = "Read the current project's file, CRS, dirty flag, layer count and epoch"
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
            project_epoch=ctx.lifecycle.current_epoch,
        )


def _refuse_remote_datasources(path: Path) -> None:
    """在一个工程被打开之前，拒掉不能让它到达 ``read()`` 的那些。

    ``QgsProject.read()`` 会建出文件点名的每个图层，并加载布局点名的图片，所以文件里任何位置
    的远程 URI——图层的 ``<datasource>``、布局条目的 ``file=`` 属性——都会在主线程上发起一次
    HTTP 请求或 SMB 认证尝试，而且发生在任何逐图层检查能跑之前；嵌套够深的文件还会在进程内把
    解析器的栈打爆。这里只读文件的字节，什么都不建。

    ``QgsProject.read()`` builds every layer the file names and loads the pictures its
    layouts name, so a remote URI anywhere in the file -- a layer's ``<datasource>``,
    a layout item's ``file=`` attribute -- is an HTTP request or an SMB authentication
    attempt on the main thread, before any per-layer check could run -- and a file
    nested deeply enough overflows the parser's stack inside the process.  Only the
    file's bytes are read here, and nothing is built.
    """

    try:
        for document in _project_documents(path):
            _scan_project_xml(document)
    except FrameworkError:
        raise
    except (OSError, zipfile.BadZipFile):
        # 这个扫描打不开的文件，正是 ``read()`` 会报成 PROJECT_OPEN_FAILED 的那种 / a file
        # this scan cannot open is what ``read()`` reports as PROJECT_OPEN_FAILED, 扫描不是新
        # 的失败来源 / and the scan is not a new failure source of its own.
        return
    except Exception:  # noqa: BLE001 - a zipfile failure the checks did not predict
        # 调用方拿到的仍是 ``read()`` 会给出的码 / the caller still gets the code ``read()``
        # would have produced, 而不是像插件缺陷的 INTERNAL_ERROR / not an INTERNAL_ERROR
        # 那种读起来像插件缺陷的东西 / that reads like a plugin defect.
        raise FrameworkError(PROJECT_OPEN_FAILED, _READ_FAILED) from None


def _project_documents(path: Path) -> list[bytes]:
    """要解析的 XML：一个 ``.qgs``，或 ``.qgz`` 里每个 ``.qgs`` 成员。

    归档的每个成员都会解压，不只是 ``.qgs`` 那些：``read()`` 解开整个归档，所以它解不出的成员
    会结束进程（实测：一个加密的兄弟成员造成 0xC0000005），而且预算必须数真正解压出来的字节，
    而不是清单声称的大小（实测：一个声明 100 字节的 204 KB 归档峰值 437 MiB）。预算算的是总量
    而不是单个成员：一个 112 KB 归档里 100 个 1 MB 成员过了逐成员检查，然后被同时留在内存里
    两次（实测 200 MB）。

    Every member of an archive is decompressed, not only the ``.qgs`` ones: ``read()``
    unpacks the whole archive, so a member it cannot extract ends the process
    (measured, 0xC0000005 on an encrypted sibling member), and the budget has to count
    the bytes actually decompressed rather than the sizes the manifest claims
    (measured, 437 MiB peak for a 204 KB archive that declared 100 bytes).  The budget
    is the total, not each member: 100 one-megabyte members of a 112 KB archive passed
    a per-member check and were held twice over (measured, 200 MB).
    """

    # 归档自身的大小在 ``zipfile`` 建出来之前就问 / the archive's own size is asked before
    # ``zipfile`` is built: 成员预算数的是解压后的字节 / the member budget counts decompressed
    # bytes, 所以 40 万个空成员不花它一分钱 / so 400000 empty members cost it nothing, 而它们
    # 需要的中央目录要 2.92 s、222 MiB 才解析完，然后同样被拒 / while the central directory
    # they need took 2.92 s and 222 MiB to parse and was then refused（实测 / measured）.
    # 一个 16 MB 归档约装 16 万成员 / a 16 MB archive holds about 160000 members, 所以这个界
    # 钉住的是打开一个归档的代价 / so this bound is what pins the cost of opening one, 不管它
    # 的清单怎么说 / whatever its manifest says.
    _refuse_if_too_large(path.stat().st_size)

    if path.suffix.lower() != ".qgz":
        return [path.read_bytes()]

    documents: list[bytes] = []
    total = 0
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise FrameworkError(
                PROJECT_OPEN_FAILED,
                f"the project archive has more than {MAX_ARCHIVE_MEMBERS} members; "
                "it was not opened",
            )
        for member in members:
            if member.flag_bits & _ZIP_ENCRYPTED or member.compress_type not in _UNPACKABLE:
                raise FrameworkError(PROJECT_OPEN_FAILED, _READ_FAILED)
            kept = bytearray() if member.filename.lower().endswith(".qgs") else None
            try:
                with archive.open(member) as handle:
                    while chunk := handle.read(_CHUNK):
                        total += len(chunk)
                        _refuse_if_too_large(total)
                        if kept is not None:
                            kept.extend(chunk)
            except (RuntimeError, NotImplementedError, zipfile.BadZipFile):
                # 清单没标成读不了的成员也不给 ``read()`` / nor is a member the manifest did
                # not announce as unreadable / 加密，或自己的流不认声明的大小 / encrypted, or a
                # 不认自己声明大小的流 / size its own stream does not honour.
                raise FrameworkError(PROJECT_OPEN_FAILED, _READ_FAILED) from None
            if kept is not None:
                documents.append(bytes(kept))
    return documents


def _scan_project_xml(text: bytes) -> None:
    """拒掉不能到达 ``read()`` 的工程 XML，判断交给解析器。

    四轮字节扫描都被合法 XML 绕过去了——属性值里的 ``>``、注释或 CDATA 里的假闭合标签、被
    QGIS 自己写出的标签撑大的深度计数器——所以现在由解析器回答：文件里有哪些元素、它们的文本是
    什么、每个属性值是什么、嵌套多深。注释和 CDATA 不是元素、不加深度，自闭合标签就是它那一个
    元素。文本取自解析器的 character-data 事件（``_ProjectScan``），不取自树：这里不建树，所以
    走文件时没东西需要释放。

    深度在解析过程中就被拒；解析不了的文件不是工程，用 ``read()`` 会给出的那句话拒掉。NUL 字节
    仍然意味着文件不是 UTF-8（而是 UTF-16），QDomDocument 会读它但本扫描不解码；标签数只是一次
    ``bytes.count``，于是标签炸弹在走一遍之前就被拒。

    Four rounds of byte scanners were walked past by legal XML -- a ``>`` inside an
    attribute value, a fake close tag inside a comment or CDATA, a depth counter that
    a label QGIS itself writes inflated -- so the parser answers the questions now:
    which elements the file holds, what their text is, every attribute value, and how
    deep they nest.
    Comments and CDATA sections are not elements and add no depth, and a self-closing
    ``<a x="/>">`` is the one element it is.  The text is taken from the parser's
    character-data events (``_ProjectScan``), not from a tree: no tree is built, so
    nothing has to be freed while the file is walked.

    The depth is refused while the file is parsed; a file that does not parse is not
    a project, and it is refused with the sentence ``read()`` would have produced.  A
    NUL byte still means the file is not UTF-8 (UTF-16), which QDomDocument would read
    but this scan does not decode; the tag count is one ``bytes.count`` so tag spam is
    refused before it is walked.
    """

    if b"\x00" in text:
        raise FrameworkError(PATH_NOT_ALLOWED, "the project file is not UTF-8; it was not opened")
    if _declares_entities(text):
        raise FrameworkError(
            PATH_NOT_ALLOWED, "the project file declares XML entities; it was not opened"
        )
    if text.count(b"<") > MAX_PROJECT_TAGS:
        raise FrameworkError(
            PROJECT_OPEN_FAILED,
            f"the project file has more than {MAX_PROJECT_TAGS} XML tags; it was not opened",
        )

    scan = _ProjectScan()
    # 开启命名空间处理 / namespace processing: ``<qgs:datasource>`` 就是 ``<datasource>`` 那个
    # 元素 / the element ``<datasource>`` is.
    parser = expat.ParserCreate(namespace_separator="}")
    parser.StartElementHandler = scan.start
    parser.EndElementHandler = scan.end
    parser.CharacterDataHandler = scan.data
    try:
        parser.Parse(text, True)
    except expat.ExpatError:
        raise FrameworkError(PROJECT_OPEN_FAILED, _READ_FAILED) from None


class _Head:
    """一个值的开头：跳过前导空白，至多 ``_VALUE_HEAD`` 个字符。

    扫描只判断值的开头，所以元素文本只留这么多；留下整段文本意味着每嵌套一层就要重新拼接一次。
    数据源还要按包含关系判断，所以开头之后那部分的末尾字符也留着，``saw_marker`` 记住
    ``_MARKER`` 是否出现在那里：一个标志位，不是第二份文本副本。

    The scan judges only the start of a value, so this is all it keeps of an element's
    text; keeping the whole text would mean re-joining it once per nesting level.  A
    data source is judged by containment as well, so the last characters of the part
    past the head are kept and ``saw_marker`` remembers whether ``_MARKER`` sits there:
    one flag, not a second copy of the text.
    """

    __slots__ = ("saw_marker", "started", "tail", "text")

    def __init__(self) -> None:
        self.text = ""
        # 值的第一个非空白字符是否已留下 / whether the value's first non-whitespace character
        # 已经留下 / has been kept already.
        self.started = False
        # 留下的末尾字符，于是跨两个事件的标记照样看得见 / the last characters kept, so a
        # 跨事件的标记也看得见 / marker split across two events is still seen.
        self.tail = ""
        # 开头之后那部分值里是否有远程标记 / whether the part of the value that is past the
        # 头之外那部分 / head holds a remote marker.
        self.saw_marker = False

    def add(self, data: str) -> None:
        if not self.started:
            data = data.lstrip()
            if not data:
                return
            self.started = True
        room = _VALUE_HEAD - len(self.text)
        if room > 0:
            self.text += data[:room]
            if len(self.text) == _VALUE_HEAD:
                self.tail = self.text[-_MARKER_TAIL:]
            data = data[room:]
        if not data or self.saw_marker:
            return
        joined = self.tail + data
        self.saw_marker = _MARKER in joined
        self.tail = joined[-_MARKER_TAIL:]

    def absorb(self, child: _Head) -> None:
        """接下一个刚结束的子元素的值，就像 ``QDomElement::text()`` 拼接的那样。

        Take a finished child's value, as ``QDomElement::text()`` concatenates it.
        """

        self.add(child.text)
        self.saw_marker = self.saw_marker or child.saw_marker
        if child.tail:
            self.tail = child.tail


class _ProjectScan:
    """文件持有的每个值，取自解析器的 character-data 事件。

    每个值都问同一条规则——元素的整段文本和每个属性值——规则里没有任何元素名
    （``_refuse_if_remote_value``）：QGIS 读的不只是 ``<datasource>``，布局的图片条目把图片存在
    一个 ``file=`` 属性里，而 ``read()`` 会加载它。

    元素文本不是 ``element.text``：``QDomElement::text()`` 会把这个元素起始标签到对应结束标签
    之间的每个 character-data 事件按文档顺序拼起来，孙元素也算在内。所以子元素的文本留在父元素
    里，而 ``/v<x/>sicurl/htt<y/>p:...``——五个节点，没一个是标记——就是 ``read()`` 到达的地址
    （实测）。只判断文本开头，所以只留 ``_VALUE_HEAD`` 个字符。

    扫描里最终还是有一个元素名：``<datasource>`` 还要按包含关系判断，因为它就是会变成 provider
    数据源的那个值（``_refuse_if_remote_source``）。

    One rule is asked of every value -- an element's whole text and every attribute
    value -- and no element name is in it (``_refuse_if_remote_value``): QGIS reads
    more than ``<datasource>``, and a layout's picture item keeps its picture in a
    ``file=`` attribute that ``read()`` loads.

    An element's text is not ``element.text``: ``QDomElement::text()`` concatenates
    every character-data event between the element's start tag and its matching end
    tag, in document order, grandchildren included.  So a child's text stays in its
    parent's, and ``/v<x/>sicurl/htt<y/>p:...`` -- five nodes, none of them a marker
    -- is the address ``read()`` reached (measured).  Only the start of the text is
    judged, so only ``_VALUE_HEAD`` characters of it are kept.

    One element name is in the scan after all: a ``<datasource>`` is judged by
    containment too, because it is the value that becomes a provider's data source
    (``_refuse_if_remote_source``).
    """

    def __init__(self) -> None:
        self.depth = 0
        # 每个还开着的元素文本的开头，最外层在前 / the start of the text of every element
        # 还开着的、最外层在前 / still open, outermost first.
        self.open: list[_Head] = []

    def start(self, name: str, attributes: Any) -> None:
        self.depth += 1
        if self.depth > MAX_PROJECT_DEPTH:
            raise FrameworkError(
                PROJECT_OPEN_FAILED,
                f"the project file nests deeper than {MAX_PROJECT_DEPTH} levels; it was not opened",
            )
        for value in attributes.values():
            _refuse_if_remote_value(value)
        self.open.append(_Head())

    def end(self, name: str) -> None:
        self.depth -= 1
        head = self.open.pop()
        if self.open:
            self.open[-1].absorb(head)
        _refuse_if_remote_value(head.text)
        if _local_name(name) == "datasource":
            _refuse_if_remote_source(head)

    def data(self, text: str) -> None:
        if self.open:
            self.open[-1].add(text)


def _declares_entities(text: bytes) -> bool:
    """文件是否声明了 XML 实体，那种东西在任何预算之前就被 expat 展开。

    内部子集在解析时就被展开：一个 1 MiB 实体被读 50 次，让 1 MiB 的工程峰值到 100 MiB
    （实测），而 8 MiB 被读 90 次峰值到 1440 MiB——同时标签数、深度和文件大小都很小。QGIS 写的
    是 ``<!DOCTYPE qgis PUBLIC '…' 'SYSTEM'>``，里面没有 ``[``，所以这个拒绝不会拒掉任何
    QGIS 写出的工程。

    An internal subset is expanded while the file is parsed: a 1 MiB entity read 50
    times made a 1 MiB project peak at 100 MiB (measured), and 8 MiB read 90 times
    peaked at 1440 MiB -- while the tag count, the depth and the file size all stay
    small.  QGIS writes ``<!DOCTYPE qgis PUBLIC '…' 'SYSTEM'>``, which has no ``[``,
    so this refuses no project QGIS writes.
    """

    if b"<!ENTITY" in text:
        return True
    start = text.find(b"<!DOCTYPE")
    if start < 0:
        return False
    bracket = text.find(b"[", start)
    end = text.find(b">", start)
    return bracket >= 0 and (end < 0 or bracket < end)


def _refuse_if_remote_value(value: str) -> None:
    """拒掉开头就点出网络 I/O 的值，在 ``read()`` 碰到它之前。

    一条规则，里面没有元素名：它对每个元素的文本和每个属性值都问一遍，因为 ``read()`` 取的可
    不只是图层来源。布局的图片条目用属性点名图片（``<LayoutItem type="65640"
    file="http://host/picture.png">``），而 ``read()`` 会加载它：一个 5 KB、数据源都在白名单里
    的工程在 0.25 s 内发起 9 个 TCP 连接，对一个接受连接但永不回答的主机则耗到 180 s，而且仍然
    报成功（实测）。

    只问「开头」，所以只是包含标记的本地文本仍然是本地文本：``D:/vsi_data/x.gpkg``、
    ``./vsi_data/...``，以及引用网址的说明文字。本地 scheme 先剥掉（``_local_path_of``：
    ``file:``，QGIS 为 CSV 图层写它，以及 ``attachment:``，它写进每个带工程样式的工程），然后
    值不得以 ``/vsi``（GDAL 的虚拟文件系统）、UNC 前缀或 ``scheme://`` 开头。解析器到这时已经解完
    字符引用，所以 ``&#47;vsicurl&#47;`` 读出来就是它点名的路径。

    这不是「工程文件里没有其它东西会被取」的证明。它是今天已知的形状清单，对每个值都问一遍，而
    工程点名的图层在 ``read()`` 之后还有第二道检查（``_sources_outside_the_allowed_roots``）。
    ``<datasource>`` 在这条之上还要回答包含关系那个问题，因为它就是会变成 provider 数据源的值
    （``_refuse_if_remote_source``）。

    One rule, no element name in it: it is asked of every element's text and every
    attribute value, because ``read()`` fetches more than a layer's source.  A layout's
    picture item names its picture in an attribute (``<LayoutItem type="65640"
    file="http://host/picture.png">``) and ``read()`` loads it: 9 TCP connections in
    0.25 s from a 5 KB project inside the roots, and 180 s against a host that accepts
    and never answers, still reported as ``SUCCEEDED`` (measured).

    Only the *start* is asked, so local text that merely contains a marker is still
    local: ``D:/vsi_data/x.gpkg``, ``./vsi_data/...`` and a description quoting a URL.
    A local scheme is peeled first (``_local_path_of``: ``file:``, which QGIS writes for
    a CSV layer, and ``attachment:``, which it writes into every project that carries a
    project style), then the value may not begin with ``/vsi`` (GDAL's virtual file
    systems), with a UNC prefix, or with ``scheme://``.  The parser has decoded
    character references by now, so ``&#47;vsicurl&#47;`` reads as the path it names.

    What this is not: a proof that nothing else in a project file is fetched.  It is the
    list of shapes known today, asked of every value, and the layers a project names get
    a second check after ``read()`` (``_sources_outside_the_allowed_roots``).  A
    ``<datasource>`` is asked the containment question on top of this one, because it is
    the value that becomes a provider's data source (``_refuse_if_remote_source``).
    """

    text = value.strip()
    local = _local_path_of(text)
    if local is not None:
        text = local.strip()
    remote = (
        text.lower().startswith("/vsi")
        or text.startswith(("\\\\", "//"))
        or _SCHEME.match(text) is not None
    )
    if remote:
        raise FrameworkError(PATH_NOT_ALLOWED, _REMOTE_SOURCE)


def _refuse_if_remote_source(head: _Head) -> None:
    """拒掉值里任何位置就点出网络 I/O 的数据源。

    ``<datasource>`` 是唯一会变成 provider 数据源的值，而 GDAL 在自己的本地语法里拼一个远程文件：
    ``NETCDF:"/vsicurl/http://…":var`` 和 ``GTIFF_DIR:1:/vsicurl/http://…`` 就是栅格的
    ``<datasource>`` 里装的东西。上面那条开头规则只有在标记落在值的前 ``_VALUE_HEAD`` 个字符里
    时才看得见它们；一个这形状的 5 KB 工程被放过去了，``read()`` 在任何逐图层检查跑之前就发了 5
    个 TCP 连接（实测）。

    所以标记在这里按包含关系问，而且只在这里：引用网址的说明文字仍然读得进去，路径里带一个
    叫 ``vsi_data`` 目录的本地路径也一样，这就是 ``/vsi`` 仍然只问前缀的原因。本地 scheme 按开头
    规则同样的方式先剥掉，所以 ``file:///C:/points.csv``——QGIS 存 CSV 图层用的写法——不算标记。

    A ``<datasource>`` is the one value that becomes a provider's data source, and GDAL
    spells a remote file inside its own local syntax:
    ``NETCDF:"/vsicurl/http://…":var`` and ``GTIFF_DIR:1:/vsicurl/http://…`` are what a
    raster's ``<datasource>`` holds.  The start rule above sees them only when the marker
    falls in the value's first ``_VALUE_HEAD`` characters; a 5 KB project of that shape
    was let through and ``read()`` sent 5 TCP connections before any per-layer check ran
    (measured).

    So the marker is asked by containment here, and only here: a description quoting a
    URL is still read, and a local path inside a directory named ``vsi_data`` still is
    too, which is why ``/vsi`` stays a prefix question.  A local scheme is peeled first,
    as the start rule peels it, so ``file:///C:/points.csv`` -- a CSV layer QGIS writes
    that way -- is not a marker.
    """

    text = head.text.strip()
    local = _local_path_of(text)
    if head.saw_marker or _MARKER in (local if local is not None else text):
        raise FrameworkError(PATH_NOT_ALLOWED, _REMOTE_SOURCE)


def _local_name(tag: str) -> str:
    """元素名去掉命名空间，小写。

    The element name without its namespace, in lower case.
    """

    return tag.rpartition("}")[2].lower()


def _local_path_of(text: str) -> str | None:
    """一个带本地 scheme 的值点名的本地路径；其它任何值返回 None。

    ``file:./points.csv`` 和 ``file:///C:/data/points.csv`` 是 QGIS 为 CSV 图层写出的本地路径；
    ``file://host/share/...`` 点的是一个主机，原样返回，交给调用方按 ``scheme://`` 开头拒掉。
    ``attachment:///styles.db`` 是 QGIS 自己的本地 scheme：它点名工程附件目录里的文件，而
    QGIS 3.44 把它写进每个带工程样式的工程，拒掉它就等于拒掉每个存过的工程。

    ``file:./points.csv`` and ``file:///C:/data/points.csv`` are local paths QGIS
    writes for a CSV layer; ``file://host/share/...`` names a host and comes back as
    it is, for the caller to refuse for its ``scheme://`` start.  ``attachment:///
    styles.db`` is QGIS's own local scheme: it names a file in the project's
    attachment directory, and QGIS 3.44 writes one into every project that carries a
    project style, so refusing it would refuse every saved project.
    """

    scheme, _, rest = text.partition(":")
    scheme = scheme.lower()
    if scheme == "file":
        if not rest.startswith("//"):
            return rest
        authority = rest[2:]
        return authority if authority.startswith("/") else None
    if scheme == "attachment":
        # ``attachment:///name`` 和 ``attachment:name`` 是同一个相对名 / both spell the same
        # 相对名 / relative name.
        return rest.lstrip("/")
    return None


def _refuse_if_too_large(size: int) -> None:
    if size > MAX_PROJECT_BYTES:
        raise FrameworkError(PATH_NOT_ALLOWED, "the project file is too large; it was not opened")


def _subdataset_path(raw: str) -> str | None:
    """一个 GDAL 子数据集源点名的本地文件；其它任何源返回 None。

    GDAL 的栅格 provider 不拆子数据集：``decodeUri`` 整串返回 ``NETCDF:"C:/x.nc":temperature``
    和 ``GTIFF_DIR:1:C:/x.tif``，于是白名单被问到的是一个拼法而不是一个文件，一个每个文件都在
    白名单里的工程拿回 ``PATH_NOT_ALLOWED``、图层被移除（实测）。

    这些形状都是 GDAL 自己的：一个 ``WORD:`` 前缀，然后文件要么被引号包住
    （``NETCDF:"…":var``、``HDF4_EOS:EOS_GRID:"…":grid:field``），要么跟在
    ``WORD:<index>:`` 后面（``GTIFF_DIR:1:C:/x.tif``），要么光着（``NETCDF:C:/x.nc``）。别的都
    不是子数据集，保持原义：读不出来的源留在外面，由 ``resolve_path`` 判断它返回的东西。

    GDAL's raster provider does not split a subdataset: ``decodeUri`` returns
    ``NETCDF:"C:/x.nc":temperature`` and ``GTIFF_DIR:1:C:/x.tif`` whole, so the
    allow-list was asked about a spelling rather than a file, and a project whose every
    file was inside the roots came back ``PATH_NOT_ALLOWED`` with its layer removed
    (measured).

    The shapes are GDAL's own: a ``WORD:`` prefix, then the file either quoted
    (``NETCDF:"…":var``, ``HDF4_EOS:EOS_GRID:"…":grid:field``) or after a
    ``WORD:<index>:`` prefix (``GTIFF_DIR:1:C:/x.tif``), or bare (``NETCDF:C:/x.nc``).
    Anything else is not a subdataset and keeps its old meaning: a source this cannot
    read stays outside, and ``resolve_path`` judges what it returns.
    """

    if _SUBDATASET.match(raw) is None:
        return None
    rest = raw.split(":", 1)[1]
    if '"' in rest:
        return rest.split('"')[1] or None
    if _SUBDATASET_INDEX.match(rest) is not None:
        rest = rest.split(":", 1)[1]
    return rest if _ABSOLUTE_PATH.match(rest) is not None else None


def _sources_outside_the_allowed_roots(ctx: OperationContext, layers: dict[str, Any]) -> list[str]:
    """数据源本身会被路径策略拒掉的那些图层。

    白名单必须对工程点名的每个源都问一遍，不能只问工程文件：``read()`` 会去取文件指向的任何东西。
    由 provider 解自己的 URI，因为一个源不是路径字符串——``ogr`` 把一个源拼成
    ``path|layername=name``。源是目录也算源（ESRI FileGDB），所以不要求它一定是文件。说不成
    本地路径、或解不了自己 URI 的 provider 按白名单外处理：读不出来的源不是可信的源，而一个
    provider 的异常不该抹掉其它图层已经给出的答案。

    The allow-list has to be asked about every source a project names, not only
    about the project file: ``read()`` reaches whatever the file points at.  The
    provider decodes its own URI, because a source is not a path string -- ``ogr``
    spells one as ``path|layername=name``.  A source that is a directory is a source
    (an ESRI FileGDB), so only a file is not required.  A provider that cannot name
    a local path, or cannot decode its own URI, is treated as outside: an unreadable
    source is not a trusted one, and one provider's exception may not erase the
    answer the other layers already gave.
    """

    from qgis.core import QgsProviderRegistry

    outside: list[str] = []
    for layer_id, layer in layers.items():
        metadata = QgsProviderRegistry.instance().providerMetadata(layer.providerType())
        try:
            decoded = metadata.decodeUri(layer.source()) if metadata is not None else None
        except Exception:  # noqa: BLE001 - any decoder failure is an untrusted source
            outside.append(layer_id)
            continue
        raw = decoded.get("path") if decoded else None
        if not raw:
            outside.append(layer_id)
            continue
        # 白名单要看子数据集点名的文件，不是它那个拼法 / the allow-list has to see the file a
        # 子数据集点名的文件 / subdataset names, not its spelling.
        raw = _subdataset_path(raw) or raw
        try:
            # 故意按保存式解析：工程点名一个已经不在的源不是踩白名单，而是读得进 / save-shaped
            # on purpose: a project naming a source that is gone is read, 只是 invalid_layer_count
            # 大于 0 / just with ``invalid_layer_count`` above zero.
            resolve_path(raw, ctx.policy.allowed_data_roots, must_exist=False, allow_directory=True)
        except FrameworkError:
            outside.append(layer_id)
    return outside


def _refuse_replacement_unless_discarding(ctx: OperationContext, discard_current: bool) -> None:
    """只有明确的 ``discard_current=true`` 才能把一个有内容的工程换掉。

    开着的编辑会话自然也归这里管：缓冲区只开在图层上，所以图层数已经盖住了它，没有第四道检查
    要找。

    An open edit session is covered as well: a buffer is only open on a layer, so the
    layer count already covers it and there is no fourth check to look for.
    """

    if discard_current:
        return
    project = ctx.project
    if project.mapLayers() or project.fileName() or project.isDirty():
        raise FrameworkError(
            PROJECT_REPLACE_REQUIRED,
            "the current project holds layers, a file or unsaved changes; "
            "pass discard_current=true to replace it",
        )
