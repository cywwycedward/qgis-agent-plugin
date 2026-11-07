"""The layer operations against a fake QGIS.

The real provider and the real layer tree are exercised by the QGIS acceptance
run; here the question is whether the contract holds -- which path is refused,
which error code comes back, which URI reaches the layer constructor, and whether
a layer's own check state stays apart from its effective visibility.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from qgis_agent_plugin.core.config import Settings
from qgis_agent_plugin.core.contracts import OperationContext
from qgis_agent_plugin.core.models import (
    INVALID_LAYER,
    LAYER_NOT_FOUND,
    LAYER_NOT_IN_TREE,
    PATH_NOT_ALLOWED,
    FrameworkError,
)
from qgis_agent_plugin.core.registry import OperationRegistry
from qgis_agent_plugin.operations import layer as ops

# --- the fake QGIS -----------------------------------------------------------


class FakeCrs:
    def __init__(self, authid: str) -> None:
        self._authid = authid

    def authid(self) -> str:
        return self._authid


class FakeLayer:
    """Enough of ``QgsMapLayer``: identity, provider, CRS, validity."""

    def __init__(
        self,
        layer_id: str,
        name: str,
        provider: str = "ogr",
        authid: str = "EPSG:4326",
        valid: bool = True,
    ) -> None:
        self.layer_id = layer_id
        self.layer_name = name
        self.provider = provider
        self.authid = authid
        self.valid = valid

    def id(self) -> str:
        return self.layer_id

    def name(self) -> str:
        return self.layer_name

    def providerType(self) -> str:
        return self.provider

    def crs(self) -> FakeCrs:
        return FakeCrs(self.authid)

    def isValid(self) -> bool:
        return self.valid


class FakeGroup:
    """A parent group; nothing a layer operation does may change it (AT-21B)."""

    def __init__(self, checked: bool = True) -> None:
        self.checked = checked
        self.sets: list[bool] = []

    def itemVisibilityChecked(self) -> bool:
        return self.checked

    def isVisible(self) -> bool:
        return self.checked

    def setItemVisibilityChecked(self, visible: bool) -> None:
        self.sets.append(visible)
        self.checked = visible


class FakeNode:
    """A layer tree node: its own check state, and the group above it."""

    def __init__(self, parent: FakeGroup | None = None, checked: bool = True) -> None:
        self.checked = checked
        self.parent = parent
        self.sets: list[bool] = []

    def itemVisibilityChecked(self) -> bool:
        return self.checked

    def isVisible(self) -> bool:
        return self.checked and (self.parent is None or self.parent.checked)

    def setItemVisibilityChecked(self, visible: bool) -> None:
        self.sets.append(visible)
        self.checked = visible


class FakeTreeRoot:
    def __init__(self) -> None:
        self.nodes: dict[str, FakeNode] = {}

    def findLayer(self, layer_id: str) -> FakeNode | None:
        return self.nodes.get(layer_id)


class FakeProject:
    def __init__(self) -> None:
        self.layers: dict[str, Any] = {}
        self.root = FakeTreeRoot()
        self.added: list[tuple[Any, bool]] = []

    def mapLayers(self) -> dict[str, Any]:
        return dict(self.layers)

    def mapLayer(self, layer_id: str) -> Any | None:
        return self.layers.get(layer_id)

    def layerTreeRoot(self) -> FakeTreeRoot:
        return self.root

    def addMapLayer(self, layer: Any, add_to_legend: bool = True) -> Any:
        # Matches QGIS: without a legend entry the layer gets no tree node.
        self.layers[layer.id()] = layer
        self.added.append((layer, add_to_legend))
        if add_to_legend:
            self.root.nodes[layer.id()] = FakeNode()
        return layer


class FakeQgis:
    """A ``qgis.core`` holding only what the layer operations import."""

    def __init__(self) -> None:
        self.constructed: list[Any] = []
        self.encoded: list[dict[str, str]] = []
        self.provider_names: list[str] = []
        self.valid = True
        holder = self

        class QgsVectorLayer(FakeLayer):
            def __init__(self, uri: str, name: str, provider: str, layer_id: str | None = None):
                super().__init__(
                    layer_id or f"vector-{len(holder.constructed)}",
                    name,
                    provider,
                    valid=holder.valid,
                )
                self.source_uri = uri
                holder.constructed.append(self)

        class QgsRasterLayer(FakeLayer):
            def __init__(self, uri: str, name: str, provider: str, layer_id: str | None = None):
                super().__init__(
                    layer_id or f"raster-{len(holder.constructed)}",
                    name,
                    provider,
                    authid="EPSG:3857",
                    valid=holder.valid,
                )
                self.source_uri = uri
                holder.constructed.append(self)

        class OgrMetadata:
            def encodeUri(self, parts: dict[str, str]) -> str:
                holder.encoded.append(dict(parts))
                return f"{parts['path']}|layername={parts['layerName']}"

        class QgsProviderRegistry:
            @staticmethod
            def instance() -> FakeQgis:
                return holder

        self.metadata = OgrMetadata()
        self.QgsVectorLayer = QgsVectorLayer
        self.QgsRasterLayer = QgsRasterLayer
        self.QgsProviderRegistry = QgsProviderRegistry

    def providerMetadata(self, name: str) -> Any:
        self.provider_names.append(name)
        return self.metadata


@pytest.fixture
def fake_qgis(monkeypatch: pytest.MonkeyPatch) -> FakeQgis:
    fake = FakeQgis()
    core = fake_module(
        "qgis.core",
        QgsVectorLayer=fake.QgsVectorLayer,
        QgsRasterLayer=fake.QgsRasterLayer,
        QgsProviderRegistry=fake.QgsProviderRegistry,
    )
    monkeypatch.setitem(sys.modules, "qgis", fake_module("qgis", core=core))
    monkeypatch.setitem(sys.modules, "qgis.core", core)
    return fake


# --- helpers -----------------------------------------------------------------


def fake_module(name: str, **attributes: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    vars(module).update(attributes)
    return module


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    # A space in the root, because the real ogr URI has to survive one.
    root = tmp_path / "data root"
    root.mkdir()
    return root


def make_source(root: Path, name: str = "roads.gpkg") -> Path:
    path = root / name
    path.write_text("x", encoding="utf-8")
    return path


def make_context(project: Any, root: Path) -> OperationContext:
    return OperationContext(
        iface=None,
        project=project,
        lifecycle=None,
        policy=Settings.from_mapping({"allowed_data_roots": [str(root)]}),
    )


def make_layer(fake: FakeQgis, layer_id: str, name: str = "layer", kind: str = "vector") -> Any:
    layer_class = fake.QgsVectorLayer if kind == "vector" else fake.QgsRasterLayer
    return layer_class(f"source-{layer_id}", name, "ogr", layer_id=layer_id)


def add_params(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "kind": "vector",
        "source_path": "C:/data/roads.gpkg",
        "name": "Roads",
    }
    values.update(overrides)
    return values


def refuse(operation: Any, ctx: OperationContext, request: Any) -> str:
    with pytest.raises(FrameworkError) as caught:
        operation.execute_main(ctx, request)
    return caught.value.code


# --- input models (PRD 7.6, 通用输入约束) ------------------------------------


def test_an_undeclared_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        ops.LayerAddParams.model_validate(add_params(provider="postgres"))


@pytest.mark.parametrize("value", ["true", 1, "yes"])
def test_a_legend_flag_must_be_a_real_boolean(value: Any) -> None:
    with pytest.raises(ValidationError):
        ops.LayerAddParams.model_validate(add_params(add_to_legend=value))


@pytest.mark.parametrize("kind", ["other", "VECTOR", "points"])
def test_the_kind_is_a_two_valued_enumeration(kind: str) -> None:
    with pytest.raises(ValidationError):
        ops.LayerAddParams.model_validate(add_params(kind=kind))


def test_an_empty_source_path_is_refused() -> None:
    with pytest.raises(ValidationError):
        ops.LayerAddParams.model_validate(add_params(source_path=""))


@pytest.mark.parametrize("name", ["", "x" * 129])
def test_the_name_is_bounded(name: str) -> None:
    with pytest.raises(ValidationError):
        ops.LayerAddParams.model_validate(add_params(name=name))


def test_a_name_of_128_characters_is_accepted() -> None:
    params = ops.LayerAddParams.model_validate(add_params(name="x" * 128))
    assert len(params.name) == 128


@pytest.mark.parametrize("sub_layer", ["", "roads/1", "roads 1", "roads|layername=x", "a" * 65])
def test_a_sub_layer_outside_the_character_set_is_refused(sub_layer: str) -> None:
    with pytest.raises(ValidationError):
        ops.LayerAddParams.model_validate(add_params(sub_layer=sub_layer))


@pytest.mark.parametrize("sub_layer", ["roads", "roads_1-A", "a" * 64])
def test_a_plain_sub_layer_is_accepted(sub_layer: str) -> None:
    assert ops.LayerAddParams.model_validate(add_params(sub_layer=sub_layer)).sub_layer == sub_layer


def test_a_raster_cannot_carry_a_sub_layer() -> None:
    with pytest.raises(ValidationError):
        ops.LayerAddParams.model_validate(add_params(kind="raster", sub_layer="roads"))


def test_a_layer_id_cannot_be_empty() -> None:
    with pytest.raises(ValidationError):
        ops.LayerGetInfoParams.model_validate({"layer_id": ""})
    with pytest.raises(ValidationError):
        ops.LayerSetVisibilityParams.model_validate({"layer_id": "", "visible": True})


def test_a_visibility_flag_must_be_a_real_boolean() -> None:
    with pytest.raises(ValidationError):
        ops.LayerSetVisibilityParams.model_validate({"layer_id": "x", "visible": "false"})


# --- layer.add (PRD 7.6, AT-19, AT-20) ---------------------------------------


def test_a_path_outside_the_allowed_roots_is_refused(
    fake_qgis: FakeQgis, tmp_path: Path, data_root: Path
) -> None:
    project = FakeProject()
    outside = make_source(tmp_path)
    code = refuse(
        ops.LayerAdd(),
        make_context(project, data_root),
        ops.LayerAddParams(**add_params(source_path=str(outside))),
    )
    assert code == PATH_NOT_ALLOWED
    assert fake_qgis.constructed == []
    assert project.mapLayers() == {}


def test_a_source_qgis_cannot_load_is_invalid_layer(fake_qgis: FakeQgis, data_root: Path) -> None:
    fake_qgis.valid = False
    project = FakeProject()
    code = refuse(
        ops.LayerAdd(),
        make_context(project, data_root),
        ops.LayerAddParams(**add_params(source_path=str(make_source(data_root)))),
    )
    assert code == INVALID_LAYER
    assert project.added == []
    assert project.mapLayers() == {}


def test_a_vector_source_is_added_through_ogr_without_a_sub_layer(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    source = make_source(data_root)
    result = ops.LayerAdd().execute_main(
        make_context(project, data_root),
        ops.LayerAddParams(**add_params(source_path=str(source))),
    )

    built = fake_qgis.constructed[0]
    assert result.layer_id == built.id()
    assert result.name == "Roads"
    assert result.kind == "vector"
    assert result.provider == "ogr"
    assert result.crs_authid == "EPSG:4326"
    assert built.source_uri == str(source.resolve())
    assert built.provider == "ogr"
    assert fake_qgis.encoded == []
    assert project.layers[result.layer_id] is built
    assert project.added == [(built, True)]


def test_a_sub_layer_uri_comes_from_the_ogr_encoder(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    source = make_source(data_root, "roads with space.gpkg")
    result = ops.LayerAdd().execute_main(
        make_context(project, data_root),
        ops.LayerAddParams(**add_params(source_path=str(source), sub_layer="roads")),
    )

    resolved = str(source.resolve())
    # The encoder gets the path untouched: quoting it here would double-escape it.
    assert fake_qgis.provider_names == ["ogr"]
    assert fake_qgis.encoded == [{"path": resolved, "layerName": "roads"}]
    assert fake_qgis.constructed[0].source_uri == f"{resolved}|layername=roads"
    assert result.provider == "ogr"


def test_a_raster_source_is_added_through_gdal(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    source = make_source(data_root, "dem.tif")
    result = ops.LayerAdd().execute_main(
        make_context(project, data_root),
        ops.LayerAddParams(**add_params(kind="raster", source_path=str(source))),
    )

    built = fake_qgis.constructed[0]
    assert result.kind == "raster"
    assert result.provider == "gdal"
    assert built.source_uri == str(source.resolve())
    assert built.provider == "gdal"
    assert fake_qgis.encoded == []


def test_a_layer_added_without_a_legend_entry_has_no_tree_node(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    ctx = make_context(project, data_root)
    added = ops.LayerAdd().execute_main(
        ctx,
        ops.LayerAddParams(
            **add_params(source_path=str(make_source(data_root)), add_to_legend=False)
        ),
    )
    info = ops.LayerGetInfo().execute_main(ctx, ops.LayerGetInfoParams(layer_id=added.layer_id))

    assert info.in_layer_tree is False
    assert info.visible is None
    assert info.effective_visible is None
    code = refuse(
        ops.LayerSetVisibility(),
        ctx,
        ops.LayerSetVisibilityParams(layer_id=added.layer_id, visible=False),
    )
    assert code == LAYER_NOT_IN_TREE


# --- layer.list (PRD 7.7, AT-21, AT-21B) -------------------------------------


def test_layers_are_listed_in_id_order(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    for layer_id in ("zzz", "aaa", "mmm"):
        project.layers[layer_id] = make_layer(fake_qgis, layer_id)

    result = ops.LayerList().execute_main(make_context(project, data_root), ops.LayerListParams())
    assert [summary.layer_id for summary in result.layers] == ["aaa", "mmm", "zzz"]


def test_a_layer_without_a_tree_node_has_no_visibility(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = make_layer(fake_qgis, "untreed")
    project.layers[layer.id()] = layer

    result = ops.LayerList().execute_main(make_context(project, data_root), ops.LayerListParams())
    summary = result.layers[0]
    assert summary.in_layer_tree is False
    assert summary.visible is None
    assert summary.effective_visible is None
    assert summary.model_dump(mode="json")["visible"] is None


def test_a_hidden_parent_group_keeps_own_and_effective_visibility_apart(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = make_layer(fake_qgis, "grouped")
    project.layers[layer.id()] = layer
    project.root.nodes[layer.id()] = FakeNode(parent=FakeGroup(checked=False))

    result = ops.LayerList().execute_main(make_context(project, data_root), ops.LayerListParams())
    summary = result.layers[0]
    assert summary.in_layer_tree is True
    assert summary.visible is True
    assert summary.effective_visible is False


def test_a_layer_that_is_neither_vector_nor_raster_is_other(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    project.layers["plugin-1"] = FakeLayer("plugin-1", "annotations", provider="custom", authid="")
    project.root.nodes["plugin-1"] = FakeNode()

    result = ops.LayerList().execute_main(make_context(project, data_root), ops.LayerListParams())
    summary = result.layers[0]
    assert summary.kind == "other"
    assert summary.crs_authid == ""
    assert summary.visible is True


# --- layer.get_info (PRD 7.8) ------------------------------------------------


def test_get_info_refuses_an_unknown_layer(fake_qgis: FakeQgis, data_root: Path) -> None:
    code = refuse(
        ops.LayerGetInfo(),
        make_context(FakeProject(), data_root),
        ops.LayerGetInfoParams(layer_id="missing"),
    )
    assert code == LAYER_NOT_FOUND


def test_get_info_reports_validity_and_the_same_visibility_as_list(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    ctx = make_context(project, data_root)
    layer = make_layer(fake_qgis, "roads")
    layer.valid = False
    project.layers[layer.id()] = layer
    project.root.nodes[layer.id()] = FakeNode(parent=FakeGroup(checked=False))

    info = ops.LayerGetInfo().execute_main(ctx, ops.LayerGetInfoParams(layer_id=layer.id()))
    listed = ops.LayerList().execute_main(ctx, ops.LayerListParams()).layers[0]

    assert info.provider == "ogr"
    assert info.valid is False
    assert (info.in_layer_tree, info.visible, info.effective_visible) == (
        listed.in_layer_tree,
        listed.visible,
        listed.effective_visible,
    )
    assert (info.visible, info.effective_visible) == (True, False)


# --- layer.set_visibility (PRD 7.9, AT-21B, AT-22) ---------------------------


def test_set_visibility_refuses_an_unknown_layer(fake_qgis: FakeQgis, data_root: Path) -> None:
    code = refuse(
        ops.LayerSetVisibility(),
        make_context(FakeProject(), data_root),
        ops.LayerSetVisibilityParams(layer_id="missing", visible=True),
    )
    assert code == LAYER_NOT_FOUND


def test_set_visibility_refuses_a_layer_without_a_tree_node(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    project = FakeProject()
    layer = make_layer(fake_qgis, "untreed")
    project.layers[layer.id()] = layer
    code = refuse(
        ops.LayerSetVisibility(),
        make_context(project, data_root),
        ops.LayerSetVisibilityParams(layer_id=layer.id(), visible=True),
    )
    assert code == LAYER_NOT_IN_TREE


def test_setting_the_same_state_changes_nothing(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    layer = make_layer(fake_qgis, "roads")
    project.layers[layer.id()] = layer
    node = FakeNode()
    project.root.nodes[layer.id()] = node

    result = ops.LayerSetVisibility().execute_main(
        make_context(project, data_root),
        ops.LayerSetVisibilityParams(layer_id=layer.id(), visible=True),
    )
    assert result.changed is False
    assert node.sets == []
    assert (result.visible, result.effective_visible) == (True, True)


def test_setting_a_new_state_toggles_only_that_node(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    layer = make_layer(fake_qgis, "roads")
    project.layers[layer.id()] = layer
    node = FakeNode()
    project.root.nodes[layer.id()] = node

    result = ops.LayerSetVisibility().execute_main(
        make_context(project, data_root),
        ops.LayerSetVisibilityParams(layer_id=layer.id(), visible=False),
    )
    assert result.changed is True
    assert node.sets == [False]
    assert (result.visible, result.effective_visible) == (False, False)


def test_a_hidden_parent_group_is_never_touched(fake_qgis: FakeQgis, data_root: Path) -> None:
    project = FakeProject()
    ctx = make_context(project, data_root)
    layer = make_layer(fake_qgis, "grouped")
    project.layers[layer.id()] = layer
    group = FakeGroup(checked=False)
    node = FakeNode(parent=group)
    project.root.nodes[layer.id()] = node

    same = ops.LayerSetVisibility().execute_main(
        ctx, ops.LayerSetVisibilityParams(layer_id=layer.id(), visible=True)
    )
    assert same.changed is False
    assert (same.visible, same.effective_visible) == (True, False)

    toggled = ops.LayerSetVisibility().execute_main(
        ctx, ops.LayerSetVisibilityParams(layer_id=layer.id(), visible=False)
    )
    assert toggled.changed is True
    assert node.sets == [False]
    assert group.sets == []
    assert group.checked is False
    assert toggled.effective_visible is False


# --- the declarations and the import rule ------------------------------------


def test_the_four_operations_register_as_main_thread_layer_capabilities() -> None:
    operations = (ops.LayerAdd(), ops.LayerList(), ops.LayerGetInfo(), ops.LayerSetVisibility())
    registry = OperationRegistry()
    for operation in operations:
        registry.register(operation)

    assert [
        (spec.name, spec.kind, spec.strategy, spec.destructive) for spec in registry.list_specs()
    ] == [
        ("layer.add", "command", "main_thread", False),
        ("layer.get_info", "query", "main_thread", False),
        ("layer.list", "query", "main_thread", False),
        ("layer.set_visibility", "command", "main_thread", False),
    ]


_GUARD = """
import sys

from qgis_agent_plugin.operations import layer

assert callable(layer.layer_source_uri)
leaked = sorted(
    name
    for name in sys.modules
    if name.split(".")[0] in {"qgis", "PyQt5", "PyQt6", "PySide2", "PySide6"}
)
assert not leaked, leaked
"""


def test_the_layer_module_imports_without_qgis() -> None:
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ}
    env["PYTHONPATH"] = os.pathsep.join([str(root / "src"), env.get("PYTHONPATH", "")])
    result = subprocess.run(
        [sys.executable, "-c", _GUARD], capture_output=True, text=True, env=env, cwd=root
    )
    assert result.returncode == 0, result.stderr
