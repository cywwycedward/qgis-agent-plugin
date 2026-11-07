"""The F7 acceptance data, generated with PyQGIS into a throwaway directory.

PRD 12.1 forbids pointing the destructive cases at a user's own projects, so every
file here is generated from scratch: a GeoPackage holding two layers (one with a
numeric field that has nulls), a raster, a valid QGZ, a QGZ whose only data source
has been deleted, a corrupt GeoPackage, a corrupt QGZ, a file that already exists
for the "do not overwrite" case, a directory outside the allowed roots, a
junction inside them that points out, and two projects that name a remote
datasource.  ``build`` deletes and rebuilds its own
directory, so a rerun needs no cleanup.

Deleting and rebuilding is only allowed inside a root this script made itself, and
only inside the system temporary directory: ``claim_root`` writes the marker file
and refuses everything else, so a mistyped work directory can never be emptied
(PRD 12.1: a run may only destroy test data).  Nothing outside that root is ever
removed.

Nothing binary is committed: the acceptance runner points this at a temporary
directory whose own name contains a space, which is how the "path with spaces"
fixture exists at all.
"""

from __future__ import annotations

import gc
import re
import shutil
import subprocess
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import Path

from qgis.core import (
    QgsFeature,
    QgsProject,
    QgsRasterLayer,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

# What the cases compare against.  The cities layer is five points with
# ``pop`` = 100, null, 40, 60, null: a correct sum is 200 over three values.
POP_TOTAL = 200.0
POP_COUNT = 3
POP_NULLS = 2

CITIES_LAYER = "cities"
ROADS_LAYER = "roads"

# The marker a work root carries once this script owns it; nothing is ever removed
# from a directory that does not hold it.
MARKER = ".f7-acceptance"


class UnsafeRoot(Exception):
    """The work root is not this run's own: outside the temp directory, or not ours."""


# A project file that names a remote source: one connection attempt each if it is
# read before it is checked (AT-30).  The port is closed and the host does not exist.
REMOTE_VSICURL = "/vsicurl/http://127.0.0.1:9/never.tif"
REMOTE_UNC = r"\\no-such-host-audit\share\data.gpkg"

# One member of the oversized project expands to this many bytes: over the scan's
# limit by far, and a few hundred KB on disk because it is all spaces (AT-33).
BOMB_MEMBER_BYTES = 300 * 1024 * 1024


@dataclass(frozen=True)
class Fixtures:
    """Every generated file, by the role a case needs it for."""

    root: Path
    cities: Path
    spaced: Path
    corrupt_gpkg: Path
    raster: Path
    project: Path
    missing: Path
    corrupt_project: Path
    existing_project: Path
    outside: Path
    leak: Path
    remote_vsicurl: Path
    remote_unc: Path
    remote_utf16: Path
    remote_spaced: Path
    moved_directory: Path
    oversized: Path
    encrypted: Path
    escape: Path | None


def claim_root(root: Path, temporary: Path) -> Path:
    """Return the work root this run may destroy, or refuse to touch it (PRD 12.1).

    The fixtures are rebuilt from scratch on every run, so the root has to be this
    script's own: strictly inside the system temporary directory, and carrying
    :data:`MARKER`.  A directory that does not hold the marker is refused instead of
    emptied -- a mistyped work directory must never lose the operator's files.
    """

    root = root.resolve()
    temporary = temporary.resolve()
    if root == temporary or temporary not in root.parents:
        raise UnsafeRoot(f"the work directory must be strictly inside {temporary}: {root}")
    if root.exists():
        if not root.is_dir():
            raise UnsafeRoot(f"the work directory is not a directory: {root}")
        if any(root.iterdir()) and not (root / MARKER).is_file():
            raise UnsafeRoot(f"the work directory holds files this run did not create: {root}")
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text("f7 acceptance work directory\n", encoding="utf-8")
    return root


def build(root: Path) -> Fixtures:
    """Regenerate the fixture set inside an already claimed work root."""

    root = root.resolve()
    data = _fresh(root / "data")
    # A sibling of the allowed root, so a path here is genuinely outside the roots the
    # acceptance run allows (AT-19) while still inside a directory this run owns.
    outside = _fresh(root / "outside")

    cities = data / "cities.gpkg"
    _write_layer(
        cities,
        CITIES_LAYER,
        "Point",
        ["name:string", "pop:integer", "temp:double"],
        [
            ("alpha", 100, 1.5),
            ("beta", None, None),
            ("gamma", 40, 2.5),
            ("delta", 60, 3.5),
            ("epsilon", None, 4.5),
        ],
    )
    # A second table in the same GeoPackage: the ``sub_layer`` case of layer.add.
    _write_layer(cities, ROADS_LAYER, "LineString", ["road_id:integer"], [(7,)], append_layer=True)

    spaced = data / "city data.gpkg"
    spaced.write_bytes(cities.read_bytes())

    corrupt_gpkg = data / "corrupt.gpkg"
    corrupt_gpkg.write_bytes(b"this is not a geopackage, only bytes with that suffix")

    raster = data / "cities.tif"
    _write_raster(raster)

    project = data / "project.qgz"
    _write_project(project, [(cities, CITIES_LAYER, "cities")], [raster])

    # A project whose only data source is deleted after it was saved: QGIS still
    # reads the file, and the registered layer is no longer valid (AT-18B).
    vanishing = data / "vanishing.gpkg"
    vanishing.write_bytes(cities.read_bytes())
    missing = data / "missing.qgz"
    _write_project(missing, [(vanishing, CITIES_LAYER, "vanishing")], [])
    gc.collect()
    vanishing.unlink()

    corrupt_project = data / "corrupt.qgz"
    corrupt_project.write_bytes(b"this is not a zipped project, only bytes with that suffix")

    existing_project = data / "existing.qgs"
    existing_project.write_bytes(b"an existing project file whose bytes must not change")

    # A project inside the allowed roots whose only layer names a source outside
    # them: the project file passes the path policy, its data source does not.
    secret = outside / "secret.gpkg"
    secret.write_bytes(cities.read_bytes())
    leak = data / "leak.qgz"
    _write_project(leak, [(secret, CITIES_LAYER, "leaked")], [])

    # Two projects inside the allowed roots whose only layer names a remote source:
    # reading either one before checking it would cost a connection (AT-30).
    remote_vsicurl = data / "remote vsicurl.qgs"
    _write_remote_project(remote_vsicurl, raster, REMOTE_VSICURL)
    remote_unc = data / "remote unc.qgs"
    _write_remote_project(remote_unc, raster, REMOTE_UNC)

    # The same remote source written so that a UTF-8 text scan cannot see it: once in
    # UTF-16, once with the whitespace XML allows inside the tag (AT-31).
    remote_utf16 = data / "remote utf16.qgs"
    _write_remote_project(remote_utf16, raster, REMOTE_VSICURL)
    _encode_utf16(remote_utf16)
    remote_spaced = data / "remote spaced.qgs"
    _write_remote_project(remote_spaced, raster, REMOTE_VSICURL)
    _space_datasource_tags(remote_spaced)

    # A project whose only source lives in a directory that was moved away: the path
    # is inside the allowed roots, only nothing is there any more (AT-32).
    moved_directory = data / "moved directory.qgs"
    _write_project(moved_directory, [(cities, CITIES_LAYER, "moved")], [])
    _rewrite_datasource(moved_directory, str(data / "moved-away" / "cities.gpkg"))

    # A project that is tiny on disk but would take 300 MB if it were unpacked, and
    # one whose member is marked encrypted (AT-33, AT-34).
    oversized = data / "oversized member.qgz"
    _write_oversized_project(oversized, raster)
    encrypted = data / "encrypted member.qgz"
    _write_encrypted_project(encrypted, raster)

    return Fixtures(
        root=data,
        cities=cities,
        spaced=spaced,
        corrupt_gpkg=corrupt_gpkg,
        raster=raster,
        project=project,
        missing=missing,
        corrupt_project=corrupt_project,
        existing_project=existing_project,
        outside=outside,
        leak=leak,
        remote_vsicurl=remote_vsicurl,
        remote_unc=remote_unc,
        remote_utf16=remote_utf16,
        remote_spaced=remote_spaced,
        moved_directory=moved_directory,
        oversized=oversized,
        encrypted=encrypted,
        escape=_junction(data / "escape", outside),
    )


def _fresh(path: Path) -> Path:
    """Empty one directory this run owns, and recreate it.

    The only directory :func:`claim_root` ever accepted is marked, so the parent of
    ``path`` carries the marker; anything else is refused rather than deleted.
    """

    if not (path.parent / MARKER).is_file():
        raise UnsafeRoot(f"refusing to rebuild a directory this run did not create: {path}")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def _write_layer(
    path: Path,
    name: str,
    geometry: str,
    fields: list[str],
    rows: list[tuple[object, ...]],
    *,
    append_layer: bool = False,
) -> None:
    spec = "&".join(["crs=EPSG:4326", *(f"field={field}" for field in fields)])
    memory = QgsVectorLayer(f"{geometry}?{spec}", name, "memory")
    features = []
    for row in rows:
        feature = QgsFeature(memory.fields())
        feature.setAttributes(list(row))
        features.append(feature)
    memory.dataProvider().addFeatures(features)

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = name
    if append_layer:
        options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
    error, message, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
        memory, str(path), QgsProject.instance().transformContext(), options
    )
    if error != QgsVectorFileWriter.NoError:
        raise RuntimeError(f"could not write {path}: {message}")


def _write_raster(path: Path) -> None:
    """A tiny GeoTIFF, written by the GDAL the QGIS installation ships."""

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        from osgeo import gdal, osr

    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(str(path), 8, 8, 1, gdal.GDT_Byte)
    band = dataset.GetRasterBand(1)
    band.Fill(3)
    band.SetNoDataValue(0)
    reference = osr.SpatialReference()
    reference.ImportFromEPSG(4326)
    dataset.SetProjection(reference.ExportToWkt())
    dataset.SetGeoTransform([0, 0.01, 0, 1, 0, -0.01])
    dataset = None
    if not QgsRasterLayer(str(path), "probe", "gdal").isValid():
        raise RuntimeError(f"could not write {path}")


def _write_project(path: Path, vectors: list[tuple[Path, str, str]], rasters: list[Path]) -> None:
    project = QgsProject()
    for source, sub_layer, name in vectors:
        uri = str(source) if sub_layer is None else f"{source}|layername={sub_layer}"
        project.addMapLayer(QgsVectorLayer(uri, name, "ogr"), True)
    for source in rasters:
        project.addMapLayer(QgsRasterLayer(str(source), "cities_tif", "gdal"), True)
    if not project.write(str(path)):
        raise RuntimeError(f"could not write {path}")
    project.clear()


def _write_remote_project(path: Path, local_raster: Path, remote: str) -> None:
    """A real project whose one layer's ``<datasource>`` is rewritten to ``remote``.

    Building the layer from ``remote`` would itself reach it, which is the one thing
    a fixture may not do; writing a local project and replacing the text afterwards
    leaves a file QGIS would happily open.
    """

    _write_project(path, [], [local_raster])
    _rewrite_datasource(path, remote)


def _rewrite_datasource(path: Path, source: str) -> None:
    """Replace the datasource of the project's first map layer, leaving the rest.

    A ``.qgs`` holds more than one ``<datasource>``: the annotation layer has one too,
    and it comes first.  Only the element inside ``<projectlayers>`` is the layer
    ``read()`` builds and reaches.
    """

    head, marker, layers = path.read_text(encoding="utf-8").partition("<projectlayers>")
    rewritten, replaced = re.subn(
        r"<datasource>[^<]*</datasource>",
        # A function, not a template: a UNC path is full of backslash escapes.
        lambda _match: f"<datasource>{source}</datasource>",
        layers,
        count=1,
    )
    if not marker or replaced != 1:
        raise RuntimeError(f"could not place the datasource in {path}")
    path.write_text(head + marker + rewritten, encoding="utf-8")


def _encode_utf16(path: Path) -> None:
    """Rewrite a project in UTF-16 with a BOM, which QDomDocument still reads."""

    text = path.read_text(encoding="utf-8")
    path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))


def _space_datasource_tags(path: Path) -> None:
    """Give the ``datasource`` tags the whitespace XML allows before ``>``."""

    text = path.read_text(encoding="utf-8")
    spaced = text.replace("<datasource>", "<datasource >").replace(
        "</datasource>", "</datasource >"
    )
    if spaced == text:
        raise RuntimeError(f"could not space the datasource tags in {path}")
    path.write_text(spaced, encoding="utf-8")


def _write_oversized_project(path: Path, local_raster: Path) -> None:
    """A valid project padded past the scan limit: tiny on disk, 300 MB unpacked."""

    seed = path.with_name(path.name + ".seed.qgs")
    _write_project(seed, [], [local_raster])
    document = seed.read_bytes()
    seed.unlink()
    path.with_name(seed.name + "_attachments.zip").unlink(missing_ok=True)
    chunk = b" " * (1024 * 1024)
    with (
        zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive,
        archive.open("project.qgs", "w") as member,
    ):
        member.write(document)
        for _ in range(BOMB_MEMBER_BYTES // len(chunk)):
            member.write(chunk)


def _write_encrypted_project(path: Path, local_raster: Path) -> None:
    """A valid project whose member is marked encrypted, which ``zipfile`` refuses.

    ``zipfile`` cannot write an encrypted member, so the general purpose flag is set
    on an otherwise plain archive; extracting the member then raises RuntimeError,
    exactly as a password-protected project does (AT-34).
    """

    _write_project(path, [], [local_raster])
    data = bytearray(path.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = data.find(signature)
        if start < 0:
            raise RuntimeError(f"could not find a zip header in {path}")
        flags = int.from_bytes(data[start + offset : start + offset + 2], "little") | 1
        data[start + offset : start + offset + 2] = flags.to_bytes(2, "little")
    path.write_bytes(bytes(data))


def _junction(link: Path, target: Path) -> Path | None:
    """A directory link inside the allowed roots that resolves outside them.

    A junction is used because a symbolic link needs a privilege this machine may
    not grant; it resolves exactly like one for ``Path.resolve``.
    """

    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
    )
    return link if created.returncode == 0 and link.exists() else None
