"""验收用的测试数据，用 PyQGIS 现场生成在一个一次性目录里。

破坏性用例不能对着用户自己的工程做，所以这里每个文件都是现造的：一个装着三张表的 GeoPackage
（其中一层带一个有空值的数值字段）、一张栅格、一个合法 QGZ、一个唯一数据源已被删掉的 QGZ、一个坏
GeoPackage、一个坏 QGZ、“不要覆盖”用例要的那个已存在文件、一个工程目录之外的目录、一个指向外面的
目录联接、两个数据源写成远程的项目，以及那些让 ``QgsProject.read()`` 崩掉、或者绕过读前扫描的工程
与归档。``build`` 删掉并重建它自己的目录，所以重跑不需要清理。

删除和重建只允许发生在本脚本自己造出来的根目录里，而且只在系统临时目录之内：``claim_root`` 写下
标记文件，其他情况一律拒绝，所以手误写错的工作目录永远不会被清空。那个根目录之外的东西永远不会被
删除。

仓库里不提交二进制文件：验收运行器把它指向一个名字里带空格的临时目录，“路径带空格”那份数据就是这
样来的。

The acceptance data, generated with PyQGIS into a throwaway directory.  A destructive case must
not be pointed at a user's own project, so every file here is generated from scratch: a
GeoPackage holding three layers (one with a numeric field that has nulls), a raster, a valid
QGZ, a QGZ whose only data source has been deleted, a corrupt GeoPackage, a corrupt QGZ, a file
that already exists for the "do not overwrite" case, a directory outside the fixture tree, a
junction inside them that points out, a project that names a remote datasource, and the projects
and archives whose nesting or encryption makes ``QgsProject.read()`` end the process.
``build`` deletes and rebuilds its own directory, so a rerun needs no cleanup.

Deleting and rebuilding is only allowed inside a root this script made itself, and only inside
the system temporary directory: ``claim_root`` writes the marker file and refuses everything
else, so a mistyped work directory can never be emptied.  Nothing outside that root is ever
removed.

Nothing binary is committed: the acceptance runner points this at a temporary directory whose
own name contains a space, which is how the "path with spaces" fixture exists at all.
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

from qgis.core import (  # pyright: ignore[reportMissingImports]
    QgsFeature,
    QgsProject,
    QgsRasterLayer,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

# 用例拿来比对的基准值。cities 是五个点，``pop`` = 100、null、40、60、null，正确的和是三个
# 值的 200。
# What the cases compare against.  The cities layer is five points with ``pop`` = 100, null, 40,
# 60, null: a correct sum is 200 over three values.
POP_TOTAL = 200.0
POP_COUNT = 3
POP_NULLS = 2

CITIES_LAYER = "cities"
ROADS_LAYER = "roads"
# 一张面表，让填充符号与各类渲染器有第三种几何可试。
# A polygon table, so the fill symbol and the renderer families have a third geometry to try.
PARCELS_LAYER = "parcels"

# 工作根目录被本脚本接管后带的标记；没有这个标记的目录，什么都不删。
# The marker a work root carries once this script owns it; nothing is ever removed from a
# directory that does not hold it.
MARKER = ".f7-acceptance"


class UnsafeRoot(Exception):
    """这个工作根不属于本次运行：在临时目录之外，或者不是我们的。

    The work root is not this run's own: outside the temp directory, or not ours.
    """


# 数据源写成远程的工程文件。端口是关的，所以 ``read()`` 只在它上面试一次连接就被拒绝：
# operation 不管远程源，拿它来证明这一点不会变慢。
# A project file that names a remote source.  The port is closed, so ``read()`` fails one
# connection attempt and moves on: the operation does not judge remote sources, and this proves it
# without paying a slow network round trip.
REMOTE_VSICURL = "/vsicurl/http://127.0.0.1:9/never.tif"

# ``QgsProject.read()`` 撑不住的形状：8000 层嵌套让解析器的栈溢出，深度写成明文、CDATA、
# 注释里、或把标签的结束藏进属性都一样。上限是 4096 层，所以 8000 一定被拒。
# The shape ``QgsProject.read()`` cannot survive: 8000 nested elements overflow the parser's stack
# whether the depth is written plainly, inside CDATA, inside a comment, or with the end of each tag
# hidden in an attribute.  The bound is 4096 levels, so 8000 is refused every time.
DEEP_PROJECT_LEVELS = 8_000
# 工程可能带的图层树层数：200 个分组写出 XML 后深度约 205（实测），必须能打开。
# Layer tree nesting a project may carry: 200 groups are depth ~205 in the written XML (measured)
# and must open.
NESTED_GROUPS = 200
# 正常工程可能带的标注图层数：每个标注写两个标签，属性值里有一个裸 ">"，字节扫描把它算成了
# 嵌套，解析器不会。
# Labelled layers a normal project may carry: each label adds two tags whose attribute value holds
# a bare ">", which a byte scan counted as nesting and a parser does not.
LABELLED_LAYERS = 30
# 一个内部子集实体被重复引用：真 QGIS 3.44 根本不展开它（实测 1 MiB 实体引用 50 次 0.53 s 读完
# ok=True），所以这份文件现在证明的是：这种工程能打开。
# One internal-subset entity, repeated: real QGIS 3.44 does not expand it at all (measured: a
# 1 MiB entity referenced 50 times read in 0.53 s, ok=True), so this file now proves that such a
# project opens.
ENTITY_BYTES = 1024 * 1024
ENTITY_REFERENCES = 50


@dataclass(frozen=True)
class Fixtures:
    """生成出来的每个文件，按用例需要它的用途命名。

    Every generated file, by the role a case needs it for.
    """

    root: Path
    cities: Path
    spaced: Path
    corrupt_gpkg: Path
    raster: Path
    # 三个波段，让 ``multibandcolor`` 真的有 RGB 可选。
    # Three bands, so ``multibandcolor`` has a real RGB choice to make.
    raster3: Path
    project: Path
    missing: Path
    corrupt_project: Path
    existing_project: Path
    outside: Path
    leak: Path
    remote_vsicurl: Path
    moved_directory: Path
    encrypted: Path
    deep_project: Path
    hidden_depth_cdata: Path
    hidden_depth_comment: Path
    hidden_depth_attribute: Path
    encrypted_sibling: Path
    labelled: Path
    grouped: Path
    entity_project: Path
    escape: Path | None


def claim_root(root: Path, temporary: Path) -> Path:
    """返回本次运行可以毁掉的工作根目录；不合格就拒绝碰它。

    测试数据每次都从头重建，所以根目录必须是本脚本自己的：严格位于系统临时目录之内，并且带着
    :data:`MARKER`。没有标记的目录会被拒绝，而不是被清空——写错的工作目录绝不能弄丢操作者的文件。

    Return the work root this run may destroy, or refuse to touch it.  The fixtures are rebuilt
    from scratch on every run, so the root has to be this script's own: strictly inside the
    system temporary directory, and carrying :data:`MARKER`.  A directory that does not hold the
    marker is refused instead of emptied -- a mistyped work directory must never lose the
    operator's files.
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
    """在一个已经被接管的工作根目录里重建整套测试数据。

    Regenerate the fixture set inside an already claimed work root.
    """

    root = root.resolve()
    data = _fresh(root / "data")
    # 工程目录的兄弟目录：这里的路径真的在工程目录之外，但它仍在本运行拥有的目录里。
    # A sibling of the project data, so a path here is genuinely outside it while still inside
    # a directory this run owns.
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
    # 同一个 GeoPackage 里的第二张表，用来测 layer.add 的 ``sub_layer``。
    # A second table in the same GeoPackage: the ``sub_layer`` case of layer.add.
    _write_layer(cities, ROADS_LAYER, "LineString", ["road_id:integer"], [(7,)], append_layer=True)
    # 面表，带一个文本字段和一个数值字段：分类与分级渲染器两种形状都要，而点/线/面正是按几何
    # 选符号那条规则覆盖的。
    # A polygon table with a text field and a numeric field: the categorized and graduated renderers
    # need both shapes, and Point, Line and Polygon are what the geometry-dependent symbol rule
    # covers.
    _write_layer(
        cities,
        PARCELS_LAYER,
        "Polygon",
        ["landuse:string", "area:double"],
        [("forest", 120.5), ("water", 41.0), ("forest", 80.25)],
        append_layer=True,
    )

    spaced = data / "city data.gpkg"
    spaced.write_bytes(cities.read_bytes())

    corrupt_gpkg = data / "corrupt.gpkg"
    corrupt_gpkg.write_bytes(b"this is not a geopackage, only bytes with that suffix")

    raster = data / "cities.tif"
    _write_raster(raster)
    raster3 = data / "bands3.tif"
    _write_raster(raster3, bands=3)

    project = data / "project.qgz"
    _write_project(project, [(cities, CITIES_LAYER, "cities")], [raster])

    # 唯一数据源在保存后被删掉的工程：QGIS 仍会读这个文件，但注册进来的图层已经无效了。
    # A project whose only data source is deleted after it was saved: QGIS still reads the file, and
    # the registered layer is no longer valid.
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

    # 工程在测试目录里，唯一图层却指向外面：路径只要求绝对，所以这个工程照样打开。
    # A project inside the fixture tree whose only layer names a source outside it: paths only
    # have to be absolute, so the project opens and that layer loads.
    secret = outside / "secret.gpkg"
    secret.write_bytes(cities.read_bytes())
    leak = data / "leak.qgz"
    _write_project(leak, [(secret, CITIES_LAYER, "leaked")], [])

    # 唯一图层是远程源的工程：operation 不看远程源，``read()`` 自己决定。
    # A project whose only layer names a remote source: the operation does not judge remote
    # sources, ``read()`` decides.
    remote_vsicurl = data / "remote vsicurl.qgs"
    _write_remote_project(remote_vsicurl, raster, REMOTE_VSICURL)

    # 唯一数据源在已经被搬走的目录里：路径是绝对的，只是那儿已经没有文件了。
    # A project whose only source lives in a directory that was moved away: the path is absolute,
    # only nothing is there any more.
    moved_directory = data / "moved directory.qgs"
    _write_project(moved_directory, [(cities, CITIES_LAYER, "moved")], [])
    _rewrite_datasource(moved_directory, str(data / "moved-away" / "cities.gpkg"))

    # 一个磁盘上很小、解开要 300 MB 的工程已经不再需要：体积不再是一条判据；带加密标志的
    # 工程仍要，因为那个形状会让进程直接结束（本机实测 0xC0000005）。
    # The "tiny on disk, 300 MB unpacked" project is gone: size is no longer a criterion.  The
    # encrypted one stays, because that shape ends the process outright (measured 0xC0000005).
    encrypted = data / "encrypted member.qgz"
    _write_encrypted_project(encrypted, raster)

    # 一个嵌套远超上限的工程。
    # A project nested far past the bound.
    deep_project = data / "deep project.qgs"
    deep_project.write_bytes(
        b"<qgis>" + b"<a>" * DEEP_PROJECT_LEVELS + b"</a>" * DEEP_PROJECT_LEVELS + b"</qgis>"
    )

    # 同样 8000 层，写成字节扫描看不见的样子。
    # The same 8000 levels written so that a byte scan cannot see them.
    hidden_depth_cdata = data / "hidden depth cdata.qgs"
    hidden_depth_cdata.write_bytes(
        b"<qgis>"
        + b"<a><![CDATA[</q>]]>" * DEEP_PROJECT_LEVELS
        + b"</a>" * DEEP_PROJECT_LEVELS
        + b"</qgis>"
    )
    hidden_depth_comment = data / "hidden depth comment.qgs"
    hidden_depth_comment.write_bytes(
        b"<qgis>"
        + b"<a><!-- </q> -->" * DEEP_PROJECT_LEVELS
        + b"</a>" * DEEP_PROJECT_LEVELS
        + b"</qgis>"
    )
    hidden_depth_attribute = data / "hidden depth attribute.qgs"
    hidden_depth_attribute.write_bytes(
        b"<qgis>" + b'<a x="/>">' * DEEP_PROJECT_LEVELS + b"</a>" * DEEP_PROJECT_LEVELS + b"</qgis>"
    )

    # 真工程加一个带 zip 加密标记的兄弟成员：read() 会解开每个成员，并在它上面死掉。
    # A real project plus a sibling member carrying the zip encryption flag: read() unpacks every
    # member and dies on this one.
    encrypted_sibling = data / "encrypted sibling.qgz"
    _write_encrypted_sibling(encrypted_sibling, raster)

    # 扫描不能拒的两个普通工程：一个带 30 个标注图层（每个标注在属性值里写一个裸 ">"），一个
    # 带 200 层嵌套分组。
    # Two ordinary projects the scan may not refuse: one with 30 labelled layers, each of which
    # writes a bare ">" inside an attribute, and one with 200 nested layer-tree groups.
    labelled = data / "labelled.qgs"
    _write_labelled_project(labelled, cities, CITIES_LAYER, LABELLED_LAYERS)
    grouped = data / "grouped.qgs"
    _write_grouped_project(grouped, cities, CITIES_LAYER, NESTED_GROUPS)

    # 一个声明了 XML 实体的文件。旧扫描还拒过两个普通工程——一个 Delimited Text 图层被写成
    # ``file:./points.csv``，一个图层的元数据里带 URL——那两个由各自的用例去写：QGIS 只在 provider
    # 加载之后才把 CSV 写成相对路径，而 ``build`` 在应用启动之前跑。
    # A file that declares an XML entity.  The two ordinary projects the old scan also refused -- a
    # Delimited Text layer spelled ``file:./points.csv`` and a layer whose metadata carries a URL --
    # are written by their case instead: QGIS only spells the CSV source relatively once its
    # providers are loaded, and ``build`` runs before the application starts.
    entity_project = data / "entity expansion.qgs"
    _write_entity_project(entity_project)

    return Fixtures(
        root=data,
        cities=cities,
        spaced=spaced,
        corrupt_gpkg=corrupt_gpkg,
        raster=raster,
        raster3=raster3,
        project=project,
        missing=missing,
        corrupt_project=corrupt_project,
        existing_project=existing_project,
        outside=outside,
        leak=leak,
        remote_vsicurl=remote_vsicurl,
        moved_directory=moved_directory,
        encrypted=encrypted,
        deep_project=deep_project,
        hidden_depth_cdata=hidden_depth_cdata,
        hidden_depth_comment=hidden_depth_comment,
        hidden_depth_attribute=hidden_depth_attribute,
        encrypted_sibling=encrypted_sibling,
        labelled=labelled,
        grouped=grouped,
        entity_project=entity_project,
        escape=_junction(data / "escape", outside),
    )


def _fresh(path: Path) -> Path:
    """清空本运行拥有的一个目录，再重建它。

    唯一被 :func:`claim_root` 接受的目录带着标记，所以 ``path`` 的父目录有这个标记；其他情况一律
    拒绝，而不是删除。

    Empty one directory this run owns, and recreate it.  The only directory
    :func:`claim_root` ever accepted is marked, so the parent of ``path`` carries the marker;
    anything else is refused rather than deleted.
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


def _write_raster(path: Path, bands: int = 1) -> None:
    """一张很小的 GeoTIFF，由 QGIS 自带的那份 GDAL 写出。

    第 *n* 个波段填的是 ``3 * n``，所以用例既能按序号、也能按取值区分波段。

    A tiny GeoTIFF, written by the GDAL the QGIS installation ships.  Band *n* is filled with
    ``3 * n``, so a case can tell the bands apart by value as well as by number.
    """

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        from osgeo import gdal, osr  # pyright: ignore[reportMissingImports]

    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(str(path), 8, 8, bands, gdal.GDT_Byte)
    for index in range(1, bands + 1):
        band = dataset.GetRasterBand(index)
        band.Fill(3 * index)
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
    """真工程，把唯一图层的 ``<datasource>`` 改写成 ``remote``。

    直接拿 ``remote`` 建图层本身就会去碰它，而这是测试数据绝不能做的事；先写本地工程、事后再改
    文本，得到的文件 QGIS 仍然乐于打开。

    A real project whose one layer's ``<datasource>`` is rewritten to ``remote``.  Building the
    layer from ``remote`` would itself reach it, which is the one thing a fixture may not do;
    writing a local project and replacing the text afterwards leaves a file QGIS would happily
    open.
    """

    _write_project(path, [], [local_raster])
    _rewrite_datasource(path, remote)


def _rewrite_datasource(path: Path, source: str) -> None:
    """改写工程第一个图层的数据源，其他内容不动。

    一个 ``.qgs`` 里不止一个 ``<datasource>``：注记图层也有一个，而且排在前面。只有
    ``<projectlayers>`` 里的那个元素才是 ``read()`` 会建出来、会去碰的图层。

    Replace the datasource of the project's first map layer, leaving the rest.  A ``.qgs`` holds
    more than one ``<datasource>``: the annotation layer has one too, and it comes first.  Only
    the element inside ``<projectlayers>`` is the layer ``read()`` builds and reaches.
    """

    _replace_first_datasource(path, f"<datasource>{source}</datasource>")


def write_vsi_directory_project(path: Path, source: Path, sub_layer: str) -> None:
    """真工程，数据源在一个名叫 ``vsi_data`` 的目录里。

    ``/vsi`` 只有在打开路径时才是虚拟文件系统：目录名叫 ``vsi_data`` 的图层就是普通本地文件，
    而按子串判远程源的旧扫描把它当成网络地址拒掉了。

    A real project whose datasource sits in a directory spelled ``vsi_data``.  ``/vsi`` is a
    virtual file system only when it opens the path: a layer in a directory called ``vsi_data``
    is an ordinary local file, and the old value scan refused it as a remote source.
    """

    from qgis.core import QgsVectorLayer  # pyright: ignore[reportMissingImports]

    directory = path.parent / "vsi_data"
    directory.mkdir(exist_ok=True)
    local = directory / source.name
    local.write_bytes(source.read_bytes())
    layer = QgsVectorLayer(f"{local}|layername={sub_layer}", "vsi data", "ogr")
    if not layer.isValid():
        raise RuntimeError(f"could not build a layer from {local}")
    project = QgsProject()
    project.addMapLayer(layer, True)
    if not project.write(str(path)):
        raise RuntimeError(f"could not write {path}")
    project.clear()
    if "vsi_data" not in path.read_text(encoding="utf-8").partition("<projectlayers>")[2]:
        raise RuntimeError(f"{path} does not name its source inside vsi_data")


def write_subdataset_project(path: Path, local_raster: Path) -> None:
    """真工程，唯一图层是本地 GDAL 子数据集。

    GDAL 的栅格 provider 不拆这种写法：写出来的 ``<datasource>`` 整个是 ``GTIFF_DIR:1:<file>``，
    路径只要求绝对，所以它原样进 provider、图层建得出来。工程先用普通文件写出来、再改文本，和
    ``_write_remote_project`` 一样：拿这种写法建图层，正是 ``read()`` 在用例里做的事。

    A real project whose one layer is a local GDAL subdataset.  GDAL's raster provider does not
    split the spelling: the written ``<datasource>`` is ``GTIFF_DIR:1:<file>`` whole, paths only
    have to be absolute, so it reaches the provider as written and the layer builds.  The project is
    written from the plain file and the text is replaced afterwards, as ``_write_remote_project``
    does: building the layer from the spelling is what ``read()`` does during the case.
    """

    _write_project(path, [], [local_raster])
    _rewrite_datasource(path, f"GTIFF_DIR:1:{local_raster}")


def _replace_first_datasource(path: Path, replacement: str) -> None:
    """改写第一个图层的数据源，``.qgs`` 与 ``.qgz`` 两种都支持。"""

    if path.suffix.lower() == ".qgz":
        with zipfile.ZipFile(path) as archive:
            members = [(item.filename, archive.read(item)) for item in archive.infolist()]
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in members:
                if name.lower().endswith(".qgs"):
                    data = _replace_in_datasource_text(data.decode("utf-8"), replacement).encode(
                        "utf-8"
                    )
                archive.writestr(name, data)
        return
    path.write_text(
        _replace_in_datasource_text(path.read_text(encoding="utf-8"), replacement),
        encoding="utf-8",
    )


def _replace_in_datasource_text(text: str, replacement: str) -> str:
    head, marker, layers = text.partition("<projectlayers>")
    rewritten, replaced = re.subn(
        r"<datasource>[^<]*</datasource>",
        # 这里给函数而不是模板：UNC 路径里全是反斜杠转义。
        # A function, not a template: a UNC path is full of backslash escapes.
        lambda _match: replacement,
        layers,
        count=1,
    )
    if not marker or replaced != 1:
        raise RuntimeError("the project XML holds no <datasource> to replace")
    return head + marker + rewritten


def _write_encrypted_project(path: Path, local_raster: Path) -> None:
    """一个合法工程，成员带加密标记，``zipfile`` 会拒绝它。

    加密标志由 :func:`_set_encryption_flag` 打上，解成员时同样抛 RuntimeError。

    A valid project whose member is marked encrypted, which ``zipfile`` refuses.  The flag is set
    by :func:`_set_encryption_flag`, and extracting the member raises RuntimeError.
    """

    _write_project(path, [], [local_raster])
    _set_encryption_flag(path)


def _write_encrypted_sibling(path: Path, local_raster: Path) -> None:
    """一个合法工程，外加一个带加密标记的非 ``.qgs`` 成员。

    ``QgsProject.read()`` 会解开归档里的每个成员，不只 ``.qgs`` 那个；这个形状曾让进程以访问违例
    结束（实测）。

    A valid project plus a non-``.qgs`` member marked encrypted.  ``QgsProject.read()`` unpacks
    every member of the archive, not only the ``.qgs`` one, so this shape ended the process with
    an access violation (measured).
    """

    _write_project(path, [], [local_raster])
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("extra.db", b"a sibling member that is not the project")
    _set_encryption_flag(path, "extra.db")


def _set_encryption_flag(path: Path, member: str | None = None) -> None:
    """给一个成员打上 zip 加密标志（不给名字时打第一个）。

    ``zipfile`` 写不出加密成员，所以标志位打在一份普通头部上；解那个成员时就会抛 RuntimeError，
    和一个真加过密的成员一样。

    Set the zip encryption flag on one member (the first one when unnamed).  ``zipfile`` cannot
    write an encrypted member, so the flag is set on an otherwise plain header; extracting that
    member then raises RuntimeError, exactly as a password-protected member does.
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
    if not patched:
        raise RuntimeError(f"could not find {member or 'a member'} in {path}")
    path.write_bytes(bytes(data))


def _write_labelled_project(path: Path, source: Path, sub_layer: str, count: int) -> None:
    """一个真工程，带 ``count`` 个各带一个默认单一标注的图层。

    QGIS 3.44 会转义 ``<``、``&`` 与 ``"``，但不转义 ``>``：每个标注写出两个标签，属性值里有裸
    ``>``。

    A real project with ``count`` layers that each carry a default single label.  QGIS 3.44
    escapes ``<``, ``&`` and ``"`` but not ``>``: every label writes two tags whose attribute
    value holds a bare ``>``.
    """

    from qgis.core import (  # pyright: ignore[reportMissingImports]
        QgsPalLayerSettings,
        QgsTextFormat,
        QgsVectorLayer,
        QgsVectorLayerSimpleLabeling,
    )

    project = QgsProject()
    for index in range(count):
        layer = QgsVectorLayer(f"{source}|layername={sub_layer}", f"labelled {index}", "ogr")
        settings = QgsPalLayerSettings()
        settings.fieldName = "name"
        settings.setFormat(QgsTextFormat())
        layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        layer.setLabelsEnabled(True)
        project.addMapLayer(layer, True)
    if not project.write(str(path)):
        raise RuntimeError(f"could not write {path}")
    project.clear()


def _write_grouped_project(path: Path, source: Path, sub_layer: str, groups: int) -> None:
    """一个真工程，唯一图层坐在 ``groups`` 层嵌套分组的最底下。

    A real project whose one layer sits at the bottom of ``groups`` nested groups.
    """

    from qgis.core import QgsVectorLayer  # pyright: ignore[reportMissingImports]

    project = QgsProject()
    layer = QgsVectorLayer(f"{source}|layername={sub_layer}", "grouped", "ogr")
    project.addMapLayer(layer, True)
    node = project.layerTreeRoot()
    for index in range(groups):
        node = node.addGroup(f"group {index}")
    node.addLayer(layer)
    if not project.write(str(path)):
        raise RuntimeError(f"could not write {path}")
    project.clear()


def write_csv_project(path: Path, data: Path) -> None:
    """一个真工程，QGIS 把它的 Delimited Text 图层写成 ``file:./points.csv``。

    写在 CSV 自己的目录里，而且在 ``initQgis()`` 之后，这样 QGIS 存下才是相对的 ``file:`` URI。

    A real project whose Delimited Text layer QGIS spells ``file:./points.csv``.  Written into the
    CSV's own directory, after ``initQgis()``, so QGIS stores the relative ``file:`` URI.
    """

    from qgis.core import QgsProviderRegistry, QgsVectorLayer  # pyright: ignore[reportMissingImports]

    csv = data / "points.csv"
    csv.write_text("name,lon,lat\none,1.0,2.0\ntwo,3.0,4.0\n", encoding="utf-8")
    metadata = QgsProviderRegistry.instance().providerMetadata("delimitedtext")
    if metadata is None:
        raise RuntimeError("the delimitedtext provider is not installed")
    uri = metadata.encodeUri(
        {"path": str(csv), "type": "csv", "xField": "lon", "yField": "lat", "crs": "EPSG:4326"}
    )
    layer = QgsVectorLayer(uri, "points", "delimitedtext")
    if not layer.isValid():
        raise RuntimeError(f"could not build a delimited text layer from {csv}")
    project = QgsProject()
    project.addMapLayer(layer, True)
    if not project.write(str(path)):
        raise RuntimeError(f"could not write {path}")
    project.clear()
    # 用例要的就是 QGIS 选的这种写法：写成别的样子必须在这里失败，而不是让一个从没碰到这个形状
    # 的用例算通过。
    # The case is about the spelling QGIS chose: a build that stores it differently must fail here
    # rather than pass as a case that never met the shape.
    if f"<datasource>file:./{csv.name}</datasource>" not in path.read_text(encoding="utf-8"):
        raise RuntimeError(f"{path} does not name its CSV as a relative file: URI")


def write_metadata_project(path: Path, source: Path, sub_layer: str) -> None:
    """一个真工程，图层元数据里带着数据集自己的 URL。

    A real project whose layer metadata carries the dataset's own URL.
    """

    from qgis.core import QgsLayerMetadata, QgsVectorLayer  # pyright: ignore[reportMissingImports]

    layer = QgsVectorLayer(f"{source}|layername={sub_layer}", "documented", "ogr")
    metadata = QgsLayerMetadata()
    metadata.setIdentifier("https://example.org/datasets/42")
    metadata.setAbstract("Downloaded from https://example.org/data")
    layer.setMetadata(metadata)
    project = QgsProject()
    project.addMapLayer(layer, True)
    if not project.write(str(path)):
        raise RuntimeError(f"could not write {path}")
    project.clear()
    text = path.read_text(encoding="utf-8")
    if "<identifier>https://example.org/datasets/42</identifier>" not in text:
        raise RuntimeError(f"{path} does not carry the metadata identifier")


def _write_entity_project(path: Path) -> None:
    """一个声明了一个实体、并把它读了 50 次的工程。

    A project that declares one entity and reads it 50 times.
    """

    entity = "A" * ENTITY_BYTES
    path.write_text(
        f'<!DOCTYPE qgis [<!ENTITY a "{entity}">]><qgis>{"<a>&a;</a>" * ENTITY_REFERENCES}</qgis>',
        encoding="utf-8",
    )


def _junction(link: Path, target: Path) -> Path | None:
    """工程目录里的一个目录联接，解析后指向外面。

    用联接而不是符号链接：符号链接需要本机不一定给的特权；对 ``Path.resolve`` 来说两者行为一样。

    A directory link inside the fixture tree that resolves outside it.  A junction is used
    because a symbolic link needs a privilege this machine may not grant; it resolves exactly
    like one for ``Path.resolve``.
    """

    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
    )
    return link if created.returncode == 0 and link.exists() else None
