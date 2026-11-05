"""The gates that keep a command from replacing what the user cannot get back."""

from __future__ import annotations

from pathlib import Path

import pytest

from qgis_agent_plugin.qgis_actions.base import ActionContext, ActionError
from qgis_agent_plugin.qgis_actions.project.create_new_project import (
    CreateNewProject,
    CreateNewProjectParams,
)
from qgis_agent_plugin.qgis_actions.project.utils import unsaved_refusal


class FakeLayer:
    """The three facts the unsaved rule reads off a layer."""

    def __init__(self, name: str, provider: str = "ogr", modified: bool = False) -> None:
        self._name = name
        self._provider = provider
        self._modified = modified

    def name(self) -> str:
        return self._name

    def providerType(self) -> str:
        return self._provider

    def isModified(self) -> bool:
        return self._modified


class FakeCrs:
    """Just enough of a CRS for a command that reports the project's authority id."""

    def authid(self) -> str:
        return "EPSG:4326"


class FakeProject:
    """A project as the gates read it: no QGIS, only what the rules ask.

    ``cleared`` and ``writes`` say whether a command got as far as touching the
    project, which is what a refusal has to leave alone.
    """

    def __init__(self, dirty: bool = False, layers: tuple[FakeLayer, ...] = ()) -> None:
        self._dirty = dirty
        self._layers = {str(index): layer for index, layer in enumerate(layers)}
        self.cleared = False
        self.writes: list[str] = []

    def mapLayers(self) -> dict[str, FakeLayer]:
        return self._layers

    def isDirty(self) -> bool:
        return self._dirty

    def fileName(self) -> str:
        return r"D:\work\current.qgz"

    def clear(self) -> None:
        self.cleared = True

    def setFilePathStorage(self, storage: object) -> None:
        pass

    def setTitle(self, title: str) -> None:
        pass

    def setCrs(self, crs: object) -> None:
        pass

    def setFileName(self, name: str) -> None:
        pass

    def crs(self) -> FakeCrs:
        return FakeCrs()

    def error(self) -> str:
        return ""

    def write(self, path: str) -> bool:
        self.writes.append(path)
        return True


def test_unsaved_refusal_of_a_clean_project_allows_the_replacement() -> None:
    for policy in ("refuse", "save", "discard"):
        assert unsaved_refusal(FakeProject(), policy, "a new empty project") is None


def test_unsaved_refusal_of_a_dirty_project_names_the_file_and_the_way_out() -> None:
    refusal = unsaved_refusal(FakeProject(dirty=True), "refuse", "a new empty project")

    assert refusal is not None
    assert "current.qgz" in refusal
    assert "a new empty project" in refusal
    assert "on_unsaved_changes='discard'" in refusal


def test_unsaved_refusal_of_edits_and_memory_layers_lists_both() -> None:
    project = FakeProject(
        layers=(FakeLayer("scratch", "memory"), FakeLayer("roads", modified=True))
    )

    refusal = unsaved_refusal(project, "refuse", "this load")

    assert refusal is not None
    assert "layers with unsaved edits: roads" in refusal
    assert "memory layers: scratch" in refusal


def test_unsaved_refusal_with_discard_allows_what_refuse_stopped() -> None:
    project = FakeProject(dirty=True, layers=(FakeLayer("roads", modified=True),))

    assert unsaved_refusal(project, "discard", "this load") is None


def test_create_new_project_at_an_existing_path_refuses_and_keeps_the_project(
    tmp_path: Path,
) -> None:
    taken = tmp_path / "taken.qgz"
    taken.write_text("a project that was already here")
    project = FakeProject()

    with pytest.raises(ActionError) as raised:
        CreateNewProject().run(
            ActionContext(project=project), CreateNewProjectParams(path=str(taken))
        )

    assert raised.value.code == "path_exists"
    assert str(taken) in str(raised.value)
    assert project.cleared is False
    assert project.writes == []
    assert taken.read_text() == "a project that was already here"
