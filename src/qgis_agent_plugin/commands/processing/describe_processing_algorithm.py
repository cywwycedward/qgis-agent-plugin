"""Describe one processing algorithm: what to pass it, and what it answers.

The description is the algorithm's own declaration -- the same one QGIS reads
for its dialog, its help page and its ``processing.algorithmHelp()`` -- so it is
there for every installed algorithm, without the per-algorithm documentation
files those help pages otherwise come from.

What a parameter *takes* is not read off the parameter: QGIS registers one
description per parameter type, and that is where the accepted value shapes and
the type's own name come from.  Everything else is the definition itself, in
QGIS's own spelling.
"""

from __future__ import annotations

import sys
from functools import cache
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ...command import Command, CommandContext, Params, Result
from ...protocol import CommandError
from .documentation import parameter_documentation, render_algorithm_help
from .shared import require_algorithm

# What QGIS reports when a numeric limit is not a limit at all: both ends of the
# double range stand in for "unbounded", and a caller reading them as bounds
# would be reading a number that means nothing.
_UNBOUNDED = sys.float_info.max

# ``dataType()`` means something different on each class -- QGIS documents
# ``Qgis.ProcessingSourceType`` on sinks and destinations, a number kind on the
# number-ish parameters, and a field kind on ``field``.  Only these six hand out
# layer kinds, which is the one of the three a caller passes a layer for.
_LAYER_KIND_ACCESSOR = {
    "source": "dataTypes",
    "vector": "dataTypes",
    "layer": "dataTypes",
    "multilayer": "layerType",
    "sink": "dataType",
    "vectorDestination": "dataType",
}


class DescribeProcessingAlgorithmParams(Params):
    """Which algorithm to describe."""

    algorithm: str = Field(min_length=1, max_length=256)


class ParameterDocumentation(BaseModel):
    """QGIS documentation attached to a parameter type and definition."""

    model_config = ConfigDict(extra="forbid")

    help: str = Field(description="Parameter-level help written by QGIS.")
    parameter_class: str = Field(
        description="QGIS parameter class, e.g. 'QgsProcessingParameterAggregate'."
    )
    type_description: str = Field(description="QGIS's description of the parameter type.")
    python_import: str = Field(description="QGIS's Python import statement for the parameter type.")
    accepted_string_values: list[str] = Field(
        description="String forms explicitly accepted by QGIS's parameter type."
    )
    accepted_parameter_types: list[str] = Field(
        description="Parameter type ids that can be used as model inputs for this type."
    )
    accepted_output_types: list[str] = Field(
        description="Output type ids that can be used as inputs for this type."
    )
    class_documentation: str = Field(
        description="The QGIS parameter class documentation, when the class is exposed."
    )
    default_python: str | None = Field(
        description="The default serialized as a Python value, or null when there is no default."
    )


class AlgorithmParameter(BaseModel):
    """One parameter: the key to pass, and what QGIS does with it.

    The names are this answer's own rather than QGIS's, because the shape is a
    decision: ``type`` is QGIS's parameter type id (``source``, ``enum``,
    ``vectorDestination``), ``type_name`` is what QGIS calls that type in a
    dialog, and ``accepted`` is what QGIS says the parameter takes, in its own
    words.  A parameter type a provider never registered -- four of them in
    3.44, the IDW interpolation data among them -- has neither, which is why
    both are empty rather than left out.

    The fields only some kinds of parameter have say so by being ``None``.
    ``minimum`` and ``maximum`` are ``None`` on a side QGIS leaves unbounded,
    and ``default`` is ``None`` when the parameter has no default at all, which
    is every destination parameter: a run has to be told where its output goes.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Key to pass in execute_processing's parameters.")
    description: str = Field(description="What QGIS calls the parameter in a dialog.")
    type: str = Field(description="QGIS parameter type id, e.g. 'source', 'enum'.")
    type_name: str = Field(
        description="QGIS's name for that type; empty when it is not registered."
    )
    accepted: list[str] = Field(
        description="Value shapes QGIS accepts, in its own words; empty when the type "
        "is not registered."
    )
    default: Any = Field(
        description="The algorithm's own default; null when it has none, as every "
        "destination parameter has."
    )
    optional: bool = Field(description="Whether the algorithm may be run without it.")
    advanced: bool = Field(description="Whether a dialog keeps it behind an advanced toggle.")
    hidden: bool = Field(
        description="Whether a dialog leaves it out of its list; the algorithm still takes it."
    )
    destination: bool = Field(description="Whether an output is written here.")
    depends_on: list[str] = Field(
        description="Parameters this one is a choice within, e.g. a field of INPUT."
    )
    options: list[str] | None = Field(
        description="An enum's labels in value order, so the index is the value to pass; "
        "null when the kind has no such notion."
    )
    allow_multiple: bool | None = Field(
        description="Whether more than one may be given; null when the kind has no such notion."
    )
    data_types: list[str] | None = Field(
        description="Layer kinds it takes, e.g. ['VectorPolygon']; empty when it takes any "
        "of them; null when the kind has no such notion."
    )
    has_geometry: bool | None = Field(
        description="Whether a destination writes geometry or a table; null otherwise."
    )
    file_filter: str | None = Field(
        description="The file dialog's filter for it; null when the kind has none."
    )
    minimum: float | None = Field(
        description="Lowest value accepted; null when QGIS leaves that side unbounded."
    )
    maximum: float | None = Field(
        description="Highest value accepted; null when QGIS leaves that side unbounded."
    )
    documentation: ParameterDocumentation = Field(
        description="Additional documentation QGIS attaches to this parameter."
    )


class AlgorithmOutput(BaseModel):
    """One thing the algorithm answers with, under the name a run reports it as.

    ``type`` is QGIS's own word for it: a layer output (``outputVector``, ...)
    comes back from ``execute_processing`` as a layer report, a number or a
    boolean as itself, and everything else as text.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Key this output appears under in a run's answer.")
    description: str = Field(description="What QGIS calls the output in a dialog.")
    type: str = Field(description="QGIS output type, e.g. 'outputVector', 'outputNumber'.")


class DescribeProcessingAlgorithmResult(Result):
    """One algorithm as QGIS declares it.

    ``description`` and ``help`` are empty when the algorithm has none to give
    (93 and 55 of the 440 installed here), ``flags`` holds QGIS's own flag names
    of which ``FlagDeprecated`` is the one that changes what a caller should do,
    and ``id`` is the id QGIS resolves rather than the spelling it was asked
    with, so an alias answers with the algorithm it stands for.
    """

    id: str = Field(description="Algorithm id, as QGIS resolves it.")
    name: str = Field(description="Name QGIS shows for the algorithm.")
    provider: str = Field(description="Provider id, e.g. 'native', 'gdal'.")
    group: str = Field(description="Group QGIS files it under, e.g. 'Vector geometry'.")
    description: str = Field(description="One line about it; empty when it has none.")
    help: str = Field(description="Its help text as written, newlines included; empty when none.")
    tags: list[str] = Field(description="Words it can be found by.")
    flags: list[str] = Field(description="QGIS's flag names for it, e.g. ['FlagDeprecated'].")
    parameters: list[AlgorithmParameter] = Field(
        description="In the algorithm's own order, which is the order a dialog shows."
    )
    outputs: list[AlgorithmOutput] = Field(description="What a run answers with.")
    algorithm_help: str = Field(
        description="A readable help page assembled from QGIS's algorithm declarations."
    )


class DescribeProcessingAlgorithm(Command):
    """Describe the algorithm ``algorithm`` names, or refuse the name."""

    name = "describe_processing_algorithm"
    params_model = DescribeProcessingAlgorithmParams

    def run(
        self, ctx: CommandContext, params: DescribeProcessingAlgorithmParams
    ) -> DescribeProcessingAlgorithmResult:
        try:
            return _answer(require_algorithm(params.algorithm), ctx.project)
        except CommandError:
            raise
        except Exception as error:
            raise CommandError(
                "documentation_failed",
                f"Failed to describe {params.algorithm}: {error}",
            ) from error


def _answer(algorithm: Any, project: Any = None) -> DescribeProcessingAlgorithmResult:
    """Read one algorithm into the answer's shape."""

    from qgis.core import QgsProcessingContext

    context = QgsProcessingContext()
    if project is not None:
        context.setProject(project)
    definitions = list(algorithm.parameterDefinitions())
    parameter_types = {
        definition.type(): _parameter_type(definition.type()) for definition in definitions
    }

    return DescribeProcessingAlgorithmResult(
        id=algorithm.id(),
        name=algorithm.displayName(),
        provider=algorithm.provider().id(),
        group=algorithm.group(),
        description=algorithm.shortDescription() or "",
        help=algorithm.shortHelpString() or "",
        tags=list(algorithm.tags()),
        flags=_flags(algorithm),
        parameters=[
            _parameter(definition, parameter_types[definition.type()], context)
            for definition in definitions
        ],
        outputs=[
            AlgorithmOutput(
                name=definition.name(),
                description=definition.description(),
                type=definition.type(),
            )
            for definition in algorithm.outputDefinitions()
        ],
        algorithm_help=render_algorithm_help(algorithm, parameter_types),
    )


def _parameter(definition: Any, parameter_type: Any, context: Any) -> AlgorithmParameter:
    """Read one parameter definition, asking QGIS for its type's own description."""

    from qgis.core import Qgis

    flags = int(definition.flags())
    return AlgorithmParameter(
        name=definition.name(),
        description=definition.description(),
        type=definition.type(),
        type_name="" if parameter_type is None else parameter_type.name(),
        accepted=[] if parameter_type is None else list(parameter_type.acceptedPythonTypes()),
        default=_default(definition.defaultValue()),
        optional=bool(flags & int(Qgis.ProcessingParameterFlag.Optional)),
        advanced=bool(flags & int(Qgis.ProcessingParameterFlag.Advanced)),
        hidden=bool(flags & int(Qgis.ProcessingParameterFlag.Hidden)),
        destination=definition.isDestination(),
        depends_on=list(definition.dependsOnOtherParameters()),
        options=list(definition.options()) if hasattr(definition, "options") else None,
        allow_multiple=(
            definition.allowMultiple() if hasattr(definition, "allowMultiple") else None
        ),
        data_types=_data_types(definition),
        has_geometry=(definition.hasGeometry() if hasattr(definition, "hasGeometry") else None),
        file_filter=(
            definition.fileFilter() or None if hasattr(definition, "fileFilter") else None
        ),
        minimum=_bound(definition, "minimum"),
        maximum=_bound(definition, "maximum"),
        documentation=parameter_documentation(definition, parameter_type, context),
    )


def _parameter_type(kind: str) -> Any:
    """QGIS's own description of a parameter type, or ``None`` for an unregistered one."""

    from qgis.core import QgsApplication

    return QgsApplication.processingRegistry().parameterType(kind)


def _flags(algorithm: Any) -> list[str]:
    """The names QGIS gives the algorithm's flags, e.g. ``['FlagDeprecated']``.

    The bits are read off QGIS's own enumeration, so a flag a later QGIS adds is
    reported as it is named there instead of being dropped here.
    """

    from qgis.core import QgsProcessingAlgorithm

    value = int(algorithm.flags())
    names = []
    for name in dir(QgsProcessingAlgorithm):
        if not name.startswith("Flag"):
            continue
        try:
            member = int(getattr(QgsProcessingAlgorithm, name))
        except (TypeError, ValueError):  # the enum type itself, and its Flags class
            continue
        if value & member:
            names.append(name)
    return names


def _data_types(definition: Any) -> list[str] | None:
    """The layer kinds the parameter takes, or ``None`` when it takes no layers."""

    accessor = _LAYER_KIND_ACCESSOR.get(definition.type())
    if accessor is None:
        return None
    values = getattr(definition, accessor)()
    if not isinstance(values, (list, tuple)):
        values = [values]
    return [_layer_kind(int(value)) for value in values]


@cache
def _layer_kinds() -> dict[int, str]:
    """QGIS's layer kinds by their number, as QGIS names them; read once."""

    from qgis.core import Qgis

    return {int(member): name for name, member in Qgis.ProcessingSourceType.__members__.items()}


def _layer_kind(value: int) -> str:
    """One layer kind as QGIS names it, e.g. ``2`` is ``VectorPolygon``."""

    return _layer_kinds().get(value, str(value))


def _bound(definition: Any, name: str) -> float | None:
    """One numeric limit, or ``None`` when QGIS reports the unbounded sentinel."""

    if not hasattr(definition, name):
        return None
    value = getattr(definition, name)()
    return None if value is None or abs(value) >= _UNBOUNDED else value


def _default(value: Any) -> Any:
    """A default value as JSON, or ``None`` when the parameter has no default.

    A null ``QVariant`` is not ``None`` in Python and stringifies to the text
    ``NULL``, which would report a default that does not exist.  A CRS and a
    colour are the two objects QGIS holds defaults as, and both are reported in
    a form a caller can pass back: an authority id, and the ``#AARRGGBB``
    spelling QGIS accepts (``name()`` would drop a transparent colour's alpha).
    """

    from qgis.core import QgsCoordinateReferenceSystem
    from qgis.PyQt.QtCore import QVariant
    from qgis.PyQt.QtGui import QColor

    if value is None:
        return None
    if isinstance(value, QVariant):
        return None if value.isNull() else _default(value.value())
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_default(item) for item in value]
    if isinstance(value, dict):
        return {key: _default(item) for key, item in value.items()}
    if isinstance(value, QgsCoordinateReferenceSystem):
        return value.authid()
    if isinstance(value, QColor):
        return value.name(value.NameFormat.HexArgb)
    return str(value)
