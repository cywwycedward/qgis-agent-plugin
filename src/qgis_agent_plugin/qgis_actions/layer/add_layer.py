"""Add the layers a data source holds to the project.

QGIS answers "what layers does this source hold, and which provider reads it"
itself: ``QgsProviderRegistry.querySublayers()`` resolves the provider for a URI
and describes every sublayer it finds, and each description builds its own
layer.  So this command never guesses a provider, and it never picks one
sublayer out of several unless the caller named it -- QGIS's own answer to a
container path is the first sublayer, which is a choice the caller cannot see.

Sources QGIS's registry cannot announce are out of reach here on purpose: a
scratch layer, a CSV to be read as points, and a tile URL to be read as vector
tiles are the three the desktop also asks the user to name a provider for, and
this command does not take that answer yet.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, Field

from ..base import Action, ActionContext, ActionError, BaseParams, BaseResult
from ..utils import layer_source
from .base import LayerReport, layer_report


class AddLayerParams(BaseParams):
    """A source, which of its layers to take, and what to call them.

    ``source`` is a file or a provider URI, ``sublayers`` names the layers to
    take out of a container, and ``name`` carries one name per layer that is
    added.  Everything but the source is optional: left out, it means "every
    layer QGIS finds, each under the name QGIS gives it".
    """

    source: Annotated[str, Field(min_length=1, max_length=4096), AfterValidator(layer_source)]
    sublayers: list[Annotated[str, Field(min_length=1, max_length=256)]] | None = Field(
        default=None,
        min_length=1,
        description="Names of the sublayers to add; every sublayer QGIS finds when omitted.",
    )
    name: list[Annotated[str, Field(min_length=1, max_length=256)]] | None = Field(
        default=None,
        min_length=1,
        description="One name per layer added, in the order the answer lists them: the order of "
        "sublayers when they were given, QGIS's own order otherwise. Every layer keeps the "
        "name QGIS gives it when this is omitted.",
    )


class AddLayerResult(BaseResult):
    """One report per layer added, each in the shape its kind has.

    A kind without its own report class is still described, with the facts every
    layer has, so adding a mesh or a tile layer answers like every other add.
    """

    layers: list[LayerReport]


class AddLayer(Action):
    """Add every layer of ``source`` to the project, or the sublayers named."""

    name = "add_layer"
    read_only = False
    params_model = AddLayerParams

    def run(self, ctx: ActionContext, params: AddLayerParams) -> dict[str, Any]:
        from qgis.core import Qgis, QgsProviderRegistry

        found = QgsProviderRegistry.instance().querySublayers(
            params.source, Qgis.SublayerQueryFlags(Qgis.SublayerQueryFlag.ResolveGeometryType)
        )
        chosen = self._chosen(found, params)
        if params.name is not None and len(params.name) != len(chosen):
            raise ActionError(
                "invalid_field",
                f"{params.source!r} yields {len(chosen)} layers but {len(params.name)} names "
                "were given: one name per layer, in the order the answer lists it. "
                f"Layers: {self._candidates(chosen)}",
            )

        transform = ctx.project.transformContext()
        wanted = params.name or [None] * len(chosen)
        layers = [self._build(detail, name, transform) for detail, name in zip(chosen, wanted)]

        # Nothing reaches the project until every layer is known to be good: a
        # source that turns out to be unreadable must not leave half of itself
        # behind, and a layer QGIS cannot read is one no caller asked for.
        for layer in layers:
            if not layer.isValid():
                raise ActionError(
                    "layer_not_valid",
                    f"Layer is not valid: {layer.publicSource() or params.source}",
                )
        for layer in layers:
            ctx.project.addMapLayer(layer)

        return AddLayerResult(layers=[layer_report(layer) for layer in layers]).model_dump(
            mode="json"
        )

    def _chosen(self, found: list[Any], params: AddLayerParams) -> list[Any]:
        """Return the sublayers to add, in the order the answer will list them.

        Without names that is the order QGIS found them in; with names it is the
        order the caller asked in, which is what lets one name sit next to the
        layer it is meant for.  A source holding nothing, a name nothing matches,
        and a name two sublayers share all fail here: each of them leaves the
        caller unable to say which layer was meant.
        """

        if not found:
            raise ActionError(
                "no_sublayers",
                f"QGIS can read no layer out of {params.source!r}. It may be missing, or it "
                "may need a provider this command does not take yet (a scratch layer, a CSV "
                "read as points, a tile URL read as vector tiles).",
            )
        if params.sublayers is None:
            return found
        by_name: dict[str, list[Any]] = {}
        for detail in found:
            by_name.setdefault(detail.name(), []).append(detail)
        chosen = []
        for wanted in params.sublayers:
            matches = by_name.get(wanted, [])
            if not matches:
                raise ActionError(
                    "no_sublayers",
                    f"No sublayer named {wanted!r} in {params.source}. "
                    f"Sublayers: {self._candidates(found)}",
                )
            if len(matches) > 1:
                raise ActionError(
                    "ambiguous_sublayer",
                    f"{len(matches)} sublayers in {params.source} are named {wanted!r}, so a "
                    "name cannot say which one is meant; add them all, or point at the one "
                    f"you want. Sublayers: {self._candidates(found)}",
                )
            chosen.append(matches[0])
        return chosen

    def _build(self, detail: Any, name: str | None, transform: Any) -> Any:
        """Build the layer one of QGIS's descriptions stands for, under one name.

        The caller's name for it wins; without one it keeps the name QGIS gave its
        sublayer -- the file or table it came from -- and a sublayer QGIS does not
        name (a service layer) falls back to its kind.
        """

        from qgis.core import QgsMapLayerFactory, QgsProviderSublayerDetails

        layer = detail.toLayer(QgsProviderSublayerDetails.LayerOptions(transform))
        if layer is None:
            raise ActionError(
                "layer_not_valid", f"Could not build a layer from {detail.uri() or detail.name()}"
            )
        layer.setName(
            name or detail.name() or QgsMapLayerFactory.typeToString(detail.type())
        )
        return layer

    def _candidates(self, found: list[Any]) -> str:
        """Name what the source did hold, so a miss can be corrected in one retry."""

        from qgis.core import QgsMapLayerFactory

        return ", ".join(
            f"{detail.name() or QgsMapLayerFactory.typeToString(detail.type())} "
            f"({QgsMapLayerFactory.typeToString(detail.type())})"
            for detail in found
        )
