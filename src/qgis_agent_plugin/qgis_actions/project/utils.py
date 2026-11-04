"""Helpers the project commands share."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# What a command may do with the project it is about to replace.
UnsavedPolicy = Literal["discard", "save", "refuse"]


@dataclass(frozen=True)
class UnsavedState:
    """What replacing the current project would drop.

    ``dirty`` is the project itself, which a save can still keep; ``layer_edits``
    and ``memory_layers`` are the parts no save can, because an edit buffer and a
    memory layer's data are not in a project file.
    """

    dirty: bool
    layer_edits: list[str]
    memory_layers: list[str]


def unsaved_state(project: Any) -> UnsavedState:
    """Report what the current project holds that replacing it would drop.

    A memory layer is reported once, through ``memory_layers``: its edits leave
    with the layer itself, so ``layer_edits`` leaves it out.
    """

    layer_edits = []
    memory_layers = []
    for layer in project.mapLayers().values():
        if layer.providerType() == "memory":
            memory_layers.append(layer.name())
        elif layer.isModified():
            # Not gated on isEditable(): that also requires a live data provider,
            # and a layer whose provider is gone still holds edits a save drops.
            layer_edits.append(layer.name())
    # Only the layer tree changes the running application marks arrive here: a
    # bare QgsApplication marks none of them, the desktop marks them all.
    return UnsavedState(
        dirty=project.isDirty(),
        layer_edits=layer_edits,
        memory_layers=memory_layers,
    )


def refusal_before_replacing(project: Any, state: UnsavedState, replacement: str) -> str:
    """Say what replacing the project would drop, and that the choice is the caller's.

    ``replacement`` names what the caller asked for instead -- a load, a new empty
    project -- so a command whose answer is a refusal says which one it refused.
    """

    dropped = []
    if state.dirty:
        dropped.append("the project's unsaved changes")
    if state.layer_edits:
        dropped.append("layers with unsaved edits: " + ", ".join(state.layer_edits))
    if state.memory_layers:
        dropped.append("memory layers: " + ", ".join(state.memory_layers))
    return (
        f"The current project ({project.fileName() or 'never saved'}) would be "
        f"replaced by {replacement}; that would drop {'; '.join(dropped)}. Save the "
        "current project first to keep what a save can, or repeat with "
        "on_unsaved_changes='discard'."
    )


def save_before_replacing() -> str:
    """Say what a caller that only wants to be told to save has to hear."""

    return (
        "The current project has unsaved changes and would be replaced; "
        "save the current project first, then repeat."
    )


def unsaved_refusal(project: Any, policy: UnsavedPolicy, replacement: str) -> str | None:
    """Return why this policy stops the replacement, or None when it may go ahead.

    A project with nothing to lose never stops, and ``discard`` chooses to lose
    what no save can keep, so neither answers anything.  ``refuse`` and ``save``
    both stop and differ only in what the failure asks the caller to do.  The
    message is returned rather than raised: only a command's own ``run`` fails.
    """

    state = unsaved_state(project)
    if not (state.dirty or state.layer_edits or state.memory_layers):
        return None
    if policy == "refuse":
        return refusal_before_replacing(project, state, replacement)
    if policy == "save":
        return save_before_replacing()
    return None
