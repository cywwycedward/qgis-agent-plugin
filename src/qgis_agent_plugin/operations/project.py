"""The four ``project.*`` operations (PRD 7.2-7.5).

All four run on the main thread, because a ``QgsProject`` may not be touched from
a worker (PRD 8.3), and the three that replace or write state declare themselves
destructive from the PRD 7.1 table, so a caller can see the risk without reading
the code.

``create`` and ``open`` share one guard: a project that holds anything worth
keeping -- layers, an associated file, unsaved changes -- is only replaced when
the caller says ``discard_current=true`` (PRD 7.2, 7.3, 10.3).  ``save`` has the
matching guard for files: an existing target is only replaced with
``overwrite=true`` (PRD 7.4, 10.3, AT-17).

QGIS is imported inside the one method that needs it, so this module still loads
in a bare interpreter and the path/CRS rules stay testable without QGIS.
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

# The two formats this plugin reads and writes; anything else is a caller mistake.
PROJECT_SUFFIXES = (".qgs", ".qgz")

# A ``.qgs``, or every member of a ``.qgz`` together, larger than this is refused
# rather than read: the scan runs on the main thread, and a small archive can hold
# members that expand to gigabytes.  Real project files are far smaller.
MAX_PROJECT_BYTES = 16 * 1024 * 1024

# An XML nesting depth above this is refused before ``read()``.  The bound is set by
# time, not by the crash: ``QgsProject.read()`` parses the project recursively and its
# cost grows faster than the depth (measured on QGIS 3.44.13, Windows x64: 200 nested
# layer-tree groups 0.28 s, 600 groups 8.84 s, 900 groups 30.11 s -- while a flat 4.2 MB
# project read in 0.14 s, so those 30 s are the depth).  100 nested groups, a real
# project QGIS writes, are depth 105; 256 keeps 2.4 times that and about half a second
# by the measured curve.  5500 levels still opened and 6000 killed the process with
# 0xC00000FD, but a file that merely takes 30 s has stopped the main thread already.
MAX_PROJECT_DEPTH = 256

# The parser is asked about at most this many tags.  Every element has one start tag,
# so ``bytes.count(b"<")`` is an upper bound on the element count: one memchr pass
# bounds the parse before it starts (400000 tags parse in 0.12-0.19 s, measured).  An
# ordinary 16 MB project holds 260000-390000 tags -- 42.6 to 61 bytes per tag in the
# measured projects -- so this bound leaves a real project under it; tag spam is
# refused by the count and never walked.
MAX_PROJECT_TAGS = 400_000

# The most members a ``.qgz`` may hold.  The byte budget counts decompressed bytes, so
# an empty member costs nothing there -- but every member costs one ``ZipFile.open()``:
# 100000 empty members in a 10.4 MB archive took 1.75 s (measured), linear in the count.
# The count is only known after ``zipfile`` has parsed the whole central directory, which
# costs memory as well (400000 empty members in a 39 MB archive: 2.92 s and 222 MiB), so
# the archive's own size is bounded before it is opened and this bound is the backstop
# for what still fits.  A project archive is a ``.qgs`` and its attachments, not
# thousands of entries.
MAX_ARCHIVE_MEMBERS = 1000

# What ``QgsProject.read()`` does with a ``.qgz``: it unpacks the whole archive, so a
# member it cannot extract is not a member the scan may skip.  Encrypted members and
# compression methods QGIS does not read are refused before ``read()`` sees the file.
_ZIP_ENCRYPTED = 0x1
_UNPACKABLE = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})

# One read from an archive member.  The budget counts these bytes as they are
# decompressed, never the size the manifest claims.
_CHUNK = 1024 * 1024

# Only the start of a value is judged, so an element's text is kept up to this many
# characters after its leading whitespace.  The longest marker is a scheme plus
# ``://`` (``https://`` is eight), which leaves every real scheme room; the bound is
# what keeps a 16 MB text nested 256 levels deep from being re-joined once per level.
# shortcut: a value whose scheme is longer than 59 characters is not seen; upgrade if
# a provider starts spelling one.
_VALUE_HEAD = 64

# A scheme at the very start of a value.  ``EPSG:4326`` and ``C:/data`` carry a colon
# but no ``//``, and both are values QGIS writes into a project.
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")

# A GDAL subdataset source, which ``decodeUri`` returns whole (see ``_subdataset_path``):
# an upper-case driver prefix, then the file quoted or behind a ``WORD:<index>:`` prefix.
_SUBDATASET = re.compile(r"\A[A-Z][A-Z0-9_]+:")
_SUBDATASET_INDEX = re.compile(r"\A(?:\d+|off):", re.IGNORECASE)
_ABSOLUTE_PATH = re.compile(r"\A(?:[A-Za-z]:[\\/]|[\\/])")

# A data source is the one value judged by containment: GDAL spells a remote file
# inside its own local syntax (``NETCDF:"/vsicurl/http://…":var``) and the marker can
# sit past the head this scan keeps.  ``_MARKER_TAIL`` is how many characters of the
# value are kept, so a marker split across two character-data events is still seen.
_MARKER = "://"
_MARKER_TAIL = len(_MARKER) - 1

# What ``read()`` reports for a file it cannot parse; the scan uses the same sentence
# rather than inventing an error of its own.
_READ_FAILED = "the project file could not be read; the current project may have changed"

# The refusal both remote rules share: which value named the host stays out of it.
_REMOTE_SOURCE = "the project file names a remote data source; it was not opened"


def _project_path(raw: str) -> str:
    """Refuse a path whose shape is wrong before any file system call.

    Only the shape belongs here: whether the directory is allowed, and whether
    the file exists, is the path policy's decision (PRD 10.2), so a well-formed
    path still has to survive ``resolve_path``.
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
    # AT-18B: a project whose layers cannot load is read, but it is not reported
    # as fully usable, and it is not silently called a failure either.
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

        # An unusable CRS is a caller mistake, and it is refused before the
        # current project is touched (PRD 7.2).
        crs = QgsCoordinateReferenceSystem(request.crs_authid)
        if not crs.isValid():
            raise FrameworkError(INVALID_CRS, f"{request.crs_authid} is not a usable CRS")
        _refuse_replacement_unless_discarding(ctx, request.discard_current)

        if not ctx.iface.newProject(promptToSaveFlag=False):
            # PRD 7.2 names no code for this: QGIS declining to create a project
            # is not a caller mistake and not a layer or path problem.
            raise FrameworkError(INTERNAL_ERROR, "QGIS did not create a new project")

        ctx.project.setCrs(crs)
        return ProjectCreateResult(
            # The caller's string may be a valid alias (``epsg:4326``); report the
            # canonical authid the project now holds, as ``open``/``get_info`` do.
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

        # `read()` rather than `iface.addProject()`, which may prompt: a refusal
        # has to be an error code, never a modal dialog (PRD 7.3).
        if not ctx.project.read(str(path)):
            raise FrameworkError(
                PROJECT_OPEN_FAILED,
                "the project file could not be read; the current project may have changed",
            )

        layers = ctx.project.mapLayers()
        outside = _sources_outside_the_allowed_roots(ctx, layers)
        if outside:
            # The project file named these sources, so ``read()`` already reached
            # them; removing them is what keeps the new project inside the policy
            # the caller was promised (PRD 10.2, see docs/OPERATIONS.md §3).
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
        # The associated file counts too: overwriting it is a file loss decision
        # the caller has to make explicitly (PRD 7.4, AT-17).
        if path.exists() and not request.overwrite:
            raise FrameworkError(
                TARGET_EXISTS, "the target file exists; pass overwrite=true to replace it"
            )

        if not project.write(str(path)):
            raise FrameworkError(
                PROJECT_SAVE_FAILED,
                "the project could not be written; the target file may be partially written",
            )
        # `write()` clears the dirty flag on success, so this reports the project
        # as QGIS left it rather than echoing the PRD's sample output.
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
    """Refuse a project that must not reach ``read()``, before it is opened.

    ``QgsProject.read()`` builds every layer the file names and loads the pictures its
    layouts name, so a remote URI anywhere in the file -- a layer's ``<datasource>``,
    a layout item's ``file=`` attribute -- is an HTTP request or an SMB authentication
    attempt on the main thread, before any per-layer check could run (PRD 10.2) -- and
    a file nested deeply enough overflows the parser's stack inside the process.  Only
    the file's bytes are read here, and nothing is built.
    """

    try:
        for document in _project_documents(path):
            _scan_project_xml(document)
    except FrameworkError:
        raise
    except (OSError, zipfile.BadZipFile):
        # A file this scan cannot open is what ``read()`` reports as
        # PROJECT_OPEN_FAILED; the scan is not a new failure source of its own.
        return
    except Exception:  # noqa: BLE001 - a zipfile failure the checks did not predict
        # The caller still gets the code ``read()`` would have produced, not an
        # INTERNAL_ERROR that reads like a plugin defect.
        raise FrameworkError(PROJECT_OPEN_FAILED, _READ_FAILED) from None


def _project_documents(path: Path) -> list[bytes]:
    """The XML to parse: a ``.qgs``, or each ``.qgs`` member of a ``.qgz``.

    Every member of an archive is decompressed, not only the ``.qgs`` ones: ``read()``
    unpacks the whole archive, so a member it cannot extract ends the process
    (measured, 0xC0000005 on an encrypted sibling member), and the budget has to count
    the bytes actually decompressed rather than the sizes the manifest claims
    (measured, 437 MiB peak for a 204 KB archive that declared 100 bytes).  The budget
    is the total, not each member: 100 one-megabyte members of a 112 KB archive passed
    a per-member check and were held twice over (measured, 200 MB).
    """

    # The archive's own size is asked before ``zipfile`` is built: the member budget
    # counts decompressed bytes, so 400000 empty members cost it nothing while the
    # central directory they need took 2.92 s and 222 MiB to parse and was then
    # refused (measured).  A 16 MB archive holds about 160000 members, so this bound is
    # what pins the cost of opening one, whatever its manifest says.
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
                # A member the manifest did not announce as unreadable (encrypted, or
                # a size its own stream does not honour) is not handed to ``read()``.
                raise FrameworkError(PROJECT_OPEN_FAILED, _READ_FAILED) from None
            if kept is not None:
                documents.append(bytes(kept))
    return documents


def _scan_project_xml(text: bytes) -> None:
    """Refuse the project XML that must not reach ``read()``, answered by a parser.

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
    # Namespace processing, so ``<qgs:datasource>`` is the element ``<datasource>`` is.
    parser = expat.ParserCreate(namespace_separator="}")
    parser.StartElementHandler = scan.start
    parser.EndElementHandler = scan.end
    parser.CharacterDataHandler = scan.data
    try:
        parser.Parse(text, True)
    except expat.ExpatError:
        raise FrameworkError(PROJECT_OPEN_FAILED, _READ_FAILED) from None


class _Head:
    """The start of one value: leading whitespace skipped, at most ``_VALUE_HEAD`` chars.

    The scan judges only the start of a value, so this is all it keeps of an element's
    text; keeping the whole text would mean re-joining it once per nesting level.  A
    data source is judged by containment as well, so the last characters of the part
    past the head are kept and ``saw_marker`` remembers whether ``_MARKER`` sits there:
    one flag, not a second copy of the text.
    """

    __slots__ = ("saw_marker", "started", "tail", "text")

    def __init__(self) -> None:
        self.text = ""
        # Whether the value's first non-whitespace character has been kept already.
        self.started = False
        # The last characters kept, so a marker split across two events is still seen.
        self.tail = ""
        # Whether the part of the value that is past the head holds a remote marker.
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
        """Take a finished child's value, as ``QDomElement::text()`` concatenates it."""

        self.add(child.text)
        self.saw_marker = self.saw_marker or child.saw_marker
        if child.tail:
            self.tail = child.tail


class _ProjectScan:
    """Every value the file holds, taken from the parser's character-data events.

    One rule is asked of every value -- an element's whole text and every attribute
    value -- and no element name is in it (``_refuse_if_remote_value``): QGIS reads
    more than ``<datasource>``, and a layout's picture item keeps its picture in a
    ``file=`` attribute that ``read()`` loads (A11 #1).

    An element's text is not ``element.text``: ``QDomElement::text()`` concatenates
    every character-data event between the element's start tag and its matching end
    tag, in document order, grandchildren included.  So a child's text stays in its
    parent's, and ``/v<x/>sicurl/htt<y/>p:...`` -- five nodes, none of them a marker
    -- is the address ``read()`` reached (measured, A7 #1).  Only the start of the
    text is judged, so only ``_VALUE_HEAD`` characters of it are kept.

    One element name is in the scan after all: a ``<datasource>`` is judged by
    containment too, because it is the value that becomes a provider's data source
    (``_refuse_if_remote_source``, A12 #1).
    """

    def __init__(self) -> None:
        self.depth = 0
        # The start of the text of every element still open, outermost first.
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
    """Whether the file declares XML entities, which expat expands before any budget.

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
    """Refuse a value that names network I/O at its start, before ``read()`` reaches it.

    One rule, no element name in it: it is asked of every element's text and every
    attribute value, because ``read()`` fetches more than a layer's source.  A layout's
    picture item names its picture in an attribute (``<LayoutItem type="65640"
    file="http://host/picture.png">``) and ``read()`` loads it: 9 TCP connections in
    0.25 s from a 5 KB project inside the roots, and 180 s against a host that accepts
    and never answers, still reported as ``SUCCEEDED`` (measured, A11 #1).

    Only the *start* is asked, so local text that merely contains a marker is still
    local: ``D:/vsi_data/x.gpkg``, ``./vsi_data/...`` and a description quoting a URL
    (A6 #3, A7 #3).  A local scheme is peeled first (``_local_path_of``: ``file:``,
    which QGIS writes for a CSV layer, and ``attachment:``, which it writes into every
    project that carries a project style), then the value may not begin with ``/vsi``
    (GDAL's virtual file systems), with a UNC prefix, or with ``scheme://``.  The
    parser has decoded character references by now, so ``&#47;vsicurl&#47;`` reads as
    the path it names.

    What this is not: a proof that nothing else in a project file is fetched.  It is
    the list of shapes known today, asked of every value, and the layers a project
    names get a second check after ``read()`` (``_sources_outside_the_allowed_roots``).
    A ``<datasource>`` is asked the containment question on top of this one, because
    it is the value that becomes a provider's data source (``_refuse_if_remote_source``).
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
    """Refuse a data source that names network I/O anywhere in its value.

    A ``<datasource>`` is the one value that becomes a provider's data source, and GDAL
    spells a remote file inside its own local syntax: ``NETCDF:"/vsicurl/http://…":var``
    and ``GTIFF_DIR:1:/vsicurl/http://…`` are what a raster's ``<datasource>`` holds.  The
    start rule above sees them only when the marker falls in the value's first
    ``_VALUE_HEAD`` characters; a 5 KB project of that shape was let through and
    ``read()`` sent 5 TCP connections before any per-layer check ran (measured, A12 #1).

    So the marker is asked by containment here, and only here: a description quoting a
    URL is still read (A6 #3), and a local path inside a directory named ``vsi_data``
    still is too (A7 #3), which is why ``/vsi`` stays a prefix question.  A local scheme
    is peeled first, as the start rule peels it, so ``file:///C:/points.csv`` -- a CSV
    layer QGIS writes that way -- is not a marker.
    """

    text = head.text.strip()
    local = _local_path_of(text)
    if head.saw_marker or _MARKER in (local if local is not None else text):
        raise FrameworkError(PATH_NOT_ALLOWED, _REMOTE_SOURCE)


def _local_name(tag: str) -> str:
    """The element name without its namespace, in lower case."""

    return tag.rpartition("}")[2].lower()


def _local_path_of(text: str) -> str | None:
    """The local path a value with a local scheme names, or None for any other value.

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
        # ``attachment:///name`` and ``attachment:name`` are the same relative name.
        return rest.lstrip("/")
    return None


def _refuse_if_too_large(size: int) -> None:
    if size > MAX_PROJECT_BYTES:
        raise FrameworkError(PATH_NOT_ALLOWED, "the project file is too large; it was not opened")


def _subdataset_path(raw: str) -> str | None:
    """The local file a GDAL subdataset source names, or None for any other source.

    GDAL's raster provider does not split a subdataset: ``decodeUri`` returns
    ``NETCDF:"C:/x.nc":temperature`` and ``GTIFF_DIR:1:C:/x.tif`` whole, so the
    allow-list was asked about a spelling rather than a file, and a project whose every
    file was inside the roots came back ``PATH_NOT_ALLOWED`` with its layer removed
    (A12 #2).

    The shapes are GDAL's own: a ``WORD:`` prefix, then the file either quoted
    (``NETCDF:"…":var``, ``HDF4_EOS:EOS_GRID:"…":grid:field``) or after a
    ``WORD:<index>:`` prefix (``GTIFF_DIR:1:C:/x.tif``), or bare
    (``NETCDF:C:/x.nc``).  Anything else is not a subdataset and keeps its old meaning:
    a source this cannot read stays outside, and ``resolve_path`` judges what it returns.
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
    """The layers whose own data source the path policy would refuse.

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
        # The allow-list has to see the file a subdataset names, not its spelling.
        raw = _subdataset_path(raw) or raw
        try:
            # Save-shaped on purpose: a project naming a source that is gone is
            # AT-18B (read, invalid_layer_count > 0), not a breach of the roots.
            resolve_path(raw, ctx.policy.allowed_data_roots, must_exist=False, allow_directory=True)
        except FrameworkError:
            outside.append(layer_id)
    return outside


def _refuse_replacement_unless_discarding(ctx: OperationContext, discard_current: bool) -> None:
    """Only an explicit ``discard_current=true`` may replace a project with content.

    PRD 7.2 also names an open edit session; a buffer is only open on a layer, so
    the layer count already covers it and there is no fourth check to look for.
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
