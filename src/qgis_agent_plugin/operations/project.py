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

import io
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, Field, StrictBool, StrictStr

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
)
from ..core.security import is_remote_uri, resolve_path

# The two formats this plugin reads and writes; anything else is a caller mistake.
PROJECT_SUFFIXES = (".qgs", ".qgz")

# A ``.qgs``, or every member of a ``.qgz`` together, larger than this is refused
# rather than read: the scan runs on the main thread, and a small archive can hold
# members that expand to gigabytes.  Real project files are far smaller.
MAX_PROJECT_BYTES = 16 * 1024 * 1024

# An XML nesting depth above this is refused before ``read()``.  ``QgsProject.read()``
# parses the project recursively; on this machine (QGIS 3.44.13, Windows x64, default
# stack) 5500 nested elements still opened and 6000 killed the process with 0xC00000FD
# (measured, one probe per depth).  100 nested layer-tree groups -- a real project
# QGIS writes -- are depth 105, so this is ten times a project QGIS writes and five
# times below the smallest depth that was measured to crash.
MAX_PROJECT_DEPTH = 1000

# The parser is asked about at most this many tags.  Every element has one start tag,
# so ``bytes.count(b"<")`` is an upper bound on the element count: one memchr pass
# bounds the parse before it starts (400000 tags parse in 0.12-0.19 s, measured).  An
# ordinary 16 MB project holds 260000-390000 tags -- 42.6 to 61 bytes per tag in the
# measured projects -- so this bound leaves a real project under it; tag spam is
# refused by the count and never walked.
MAX_PROJECT_TAGS = 400_000

# What ``QgsProject.read()`` does with a ``.qgz``: it unpacks the whole archive, so a
# member it cannot extract is not a member the scan may skip.  Encrypted members and
# compression methods QGIS does not read are refused before ``read()`` sees the file.
_ZIP_ENCRYPTED = 0x1
_UNPACKABLE = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})

# One read from an archive member.  The budget counts these bytes as they are
# decompressed, never the size the manifest claims.
_CHUNK = 1024 * 1024

# What ``read()`` reports for a file it cannot parse; the scan uses the same sentence
# rather than inventing an error of its own.
_READ_FAILED = "the project file could not be read; the current project may have changed"


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


class ProjectCreateResult(BaseModel):
    crs_authid: StrictStr
    file_path: StrictStr | None = None
    layer_count: int = 0


class ProjectOpenParams(Params):
    path: Annotated[StrictStr, AfterValidator(_project_path)]
    discard_current: StrictBool = False


class ProjectOpenResult(BaseModel):
    file_path: StrictStr
    crs_authid: StrictStr
    layer_count: int
    # AT-18B: a project whose layers cannot load is read, but it is not reported
    # as fully usable, and it is not silently called a failure either.
    invalid_layer_count: int


class ProjectSaveParams(Params):
    path: Annotated[StrictStr, AfterValidator(_project_path)] | None = None
    overwrite: StrictBool = False


class ProjectSaveResult(BaseModel):
    file_path: StrictStr
    is_dirty: StrictBool


class ProjectGetInfoParams(Params):
    pass


class ProjectGetInfoResult(BaseModel):
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

    ``QgsProject.read()`` builds every layer the file names, so a remote URI in the
    file is an HTTP request or an SMB authentication attempt on the main thread --
    before any per-layer check could run (PRD 10.2) -- and a file nested deeply
    enough overflows the parser's stack inside the process.  Only the file's bytes
    are read here, and nothing is built.
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

    if path.suffix.lower() != ".qgz":
        _refuse_if_too_large(path.stat().st_size)
        return [path.read_bytes()]

    documents: list[bytes] = []
    total = 0
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
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
    which elements the file holds, what their text is, and how deep they nest.
    Comments and CDATA sections are not elements and add no depth, and a self-closing
    ``<a x="/>" .../>`` is the one element it is.

    The depth is refused while the file is parsed; a file that does not parse is not
    a project, and it is refused with the sentence ``read()`` would have produced.  A
    NUL byte still means the file is not UTF-8 (UTF-16), which QDomDocument would read
    but this scan does not decode; the tag count is one ``bytes.count`` so tag spam is
    refused before it is walked.
    """

    if b"\x00" in text:
        raise FrameworkError(PATH_NOT_ALLOWED, "the project file is not UTF-8; it was not opened")
    if text.count(b"<") > MAX_PROJECT_TAGS:
        raise FrameworkError(
            PROJECT_OPEN_FAILED,
            f"the project file has more than {MAX_PROJECT_TAGS} XML tags; it was not opened",
        )

    depth = 0
    try:
        for event, element in ET.iterparse(io.BytesIO(text), events=("start", "end")):
            if event == "start":
                depth += 1
                if depth > MAX_PROJECT_DEPTH:
                    raise FrameworkError(
                        PROJECT_OPEN_FAILED,
                        "the project file nests deeper than "
                        f"{MAX_PROJECT_DEPTH} levels; it was not opened",
                    )
                continue
            depth -= 1
            # An element's own text is complete when it ends; its children were
            # already answered when they ended, so the subtree can be dropped here.
            _refuse_if_remote_text(element)
            element.clear()
    except ET.ParseError:
        raise FrameworkError(PROJECT_OPEN_FAILED, _READ_FAILED) from None


def _refuse_if_remote_text(element: ET.Element) -> None:
    """Refuse an element whose text would make ``read()`` reach the network.

    ``datasource`` is the element QGIS takes a layer's source from, whatever its
    namespace or case; the markers are also asked of every other element's text, so
    renaming the carrier does not put the URI past the scan.  The parser has decoded
    character references by now, so ``&#47;vsicurl&#47;`` reads as the path it names.
    """

    text = element.text
    if not text:
        return
    stripped = text.strip()
    if (
        "://" in text
        or "/vsi" in text.lower()
        or stripped.startswith(("\\\\", "//"))
        or (_local_name(element.tag) == "datasource" and is_remote_uri(stripped))
    ):
        raise FrameworkError(
            PATH_NOT_ALLOWED,
            "the project file names a remote data source; it was not opened",
        )


def _local_name(tag: str) -> str:
    """The element name without its namespace, in lower case."""

    return tag.rsplit("}", 1)[-1].lower()


def _refuse_if_too_large(size: int) -> None:
    if size > MAX_PROJECT_BYTES:
        raise FrameworkError(PATH_NOT_ALLOWED, "the project file is too large; it was not opened")


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
