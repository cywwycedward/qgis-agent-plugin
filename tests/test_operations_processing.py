"""The processing operations against a fake QGIS.

The real registry, providers and algorithms are exercised by the QGIS acceptance
run; here the question is whether the contract holds -- which fields a definition
report carries, what reaches ``qgis.processing.run()`` and with which keywords, what
a run keeps from its feedback, what happens to a result layer, and which values can
leave as JSON.  The fakes are deliberately narrow: one process-wide ``run`` with a
canned answer, a feedback base class that notes what was asked of it, and a project
with QGIS's real duplicate-id rule.  Nothing here models a parameter check, an
instance factory or a ``canExecute()``, because the operations must not reach for
one (design §5.3).
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import sys
import types
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from pydantic import ValidationError

from qgis_agent_plugin.core.config import Settings
from qgis_agent_plugin.core.contracts import OperationContext
from qgis_agent_plugin.core.models import INTERNAL_ERROR, INVALID_INPUT, FrameworkError
from qgis_agent_plugin.core.registry import OperationRegistry
from qgis_agent_plugin.operations import processing as ops

# Mirrors ``QgsProcessingParameterDefinition.FlagHidden`` in the 3.44.13 build.
_FLAG_HIDDEN = 4

LOGGER_NAME = "qgis_agent_plugin.operations.processing"

# --- the fake QGIS -----------------------------------------------------------


class FakeLayer:
    """A layer the project can hold, and the object a run hands back; identity is all."""

    def __init__(self, layer_id: str) -> None:
        self.layer_id = layer_id

    def id(self) -> str:
        return self.layer_id


class FakeProject:
    """A project with QGIS's real addMapLayer rule: a duplicate id is refused."""

    def __init__(self) -> None:
        self.layers: dict[str, Any] = {}
        self.added: list[Any] = []
        self.removed: list[str] = []

    def mapLayer(self, layer_id: str) -> Any | None:
        return self.layers.get(layer_id)

    def mapLayers(self) -> dict[str, Any]:
        return dict(self.layers)

    def addMapLayer(self, layer: Any, add_to_legend: bool = True) -> Any:
        # Measured on 3.44.13: a second layer with an existing id is not added and
        # the project returns None, leaving the first layer in place.
        if layer.id() in self.layers:
            return None
        self.layers[layer.id()] = layer
        self.added.append(layer)
        return layer

    def removeMapLayer(self, layer_id: str) -> Any | None:
        self.removed.append(layer_id)
        return self.layers.pop(layer_id, None)


class FakeFeedback:
    """``QgsProcessingFeedback``: the base implementation the records sit on.

    Each call notes its name and how many records already existed, which is how a
    test can see that the base class ran *before* the subclass recorded its message.
    """

    def __init__(self) -> None:
        self.base: list[tuple[str, int]] = []

    def _called(self, name: str) -> None:
        self.base.append((name, len(getattr(self, "records", []))))

    def pushInfo(self, info: str) -> None:
        self._called("pushInfo")

    def pushDebugInfo(self, info: str) -> None:
        self._called("pushDebugInfo")

    def pushCommandInfo(self, info: str) -> None:
        self._called("pushCommandInfo")

    def pushConsoleInfo(self, info: str) -> None:
        self._called("pushConsoleInfo")

    def pushFormattedMessage(self, html: str, text: str) -> None:
        self._called("pushFormattedMessage")

    def pushWarning(self, warning: str) -> None:
        self._called("pushWarning")

    def reportError(self, message: str, fatalError: bool = False) -> None:
        self._called("reportError")


class FakeParameterDefinition:
    """The namespace holding the one flag the definition report reads."""

    FlagHidden = _FLAG_HIDDEN


class FakeDefinition:
    """Enough of one parameter definition for one summary."""

    def __init__(
        self,
        name: str,
        type_: str,
        *,
        description: str = "",
        help_: str = "",
        hidden: bool = False,
    ) -> None:
        self._name = name
        self._type = type_
        self._description = description
        self._help = help_
        self._hidden = hidden

    def name(self) -> str:
        return self._name

    def type(self) -> str:
        return self._type

    def description(self) -> str:
        return self._description

    def help(self) -> str:
        return self._help

    def flags(self) -> int:
        return _FLAG_HIDDEN if self._hidden else 0


class FakeEnumDefinition(FakeDefinition):
    """The one definition family with display options; the base class has none."""

    def __init__(self, name: str, options: list[str], **values: Any) -> None:
        super().__init__(name, "enum", **values)
        self._options = list(options)

    def options(self) -> list[str]:
        return list(self._options)


class FakeParameterType:
    """A ``QgsProcessingParameterType``: only its accepted python types are read."""

    def __init__(self, *python_types: str) -> None:
        self._python_types = list(python_types)

    def acceptedPythonTypes(self) -> list[str]:
        return list(self._python_types)


class FakeOutput:
    def __init__(self, name: str, description: str = "") -> None:
        self._name = name
        self._description = description

    def name(self) -> str:
        return self._name

    def description(self) -> str:
        return self._description


class FakeAlgorithm:
    """One registered algorithm definition; a run never touches this object."""

    def __init__(
        self,
        algorithm_id: str,
        *,
        name: str = "Algorithm",
        group: str = "Group",
        short_description: str = "",
        help_: str = "",
        definitions: tuple[Any, ...] = (),
        outputs: tuple[Any, ...] = (),
    ) -> None:
        self.algorithm_id = algorithm_id
        self._name = name
        self._group = group
        self._short_description = short_description
        self._help = help_
        self._definitions = definitions
        self._outputs = outputs

    def id(self) -> str:
        return self.algorithm_id

    def displayName(self) -> str:
        return self._name

    def group(self) -> str:
        return self._group

    def shortDescription(self) -> str:
        return self._short_description

    def shortHelpString(self) -> str:
        return self._help

    def parameterDefinitions(self) -> list[Any]:
        return list(self._definitions)

    def outputDefinitions(self) -> list[Any]:
        return list(self._outputs)


class FakeRegistry:
    """``QgsProcessingRegistry``, deliberately without an instance factory."""

    def __init__(self) -> None:
        self.algorithms_by_id: dict[str, FakeAlgorithm] = {}
        self.parameter_types: dict[str, FakeParameterType] = {}
        self.lookups: list[str] = []

    def algorithms(self) -> list[FakeAlgorithm]:
        return list(self.algorithms_by_id.values())

    def algorithmById(self, algorithm_id: str) -> FakeAlgorithm | None:
        self.lookups.append(algorithm_id)
        return self.algorithms_by_id.get(algorithm_id)

    def parameterType(self, name: str) -> FakeParameterType | None:
        return self.parameter_types.get(name)


class Call(NamedTuple):
    """One recorded ``qgis.processing.run()`` call."""

    algorithm_id: str
    parameters: dict[str, Any]
    kwargs: dict[str, Any]

    @property
    def feedback(self) -> Any:
        """The feedback the operation built; its surface is the run's own business."""
        return self.kwargs["feedback"]


class FakeProcessing:
    """``qgis.processing``: one recorded call per run, with a canned answer."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.results: Any = {}
        self.error: BaseException | None = None
        # ``(method name, arguments)`` replayed on the feedback the run was given.
        self.messages: list[tuple[str, tuple[Any, ...]]] = []

    def run(self, algorithm_id: str, parameters: dict[str, Any], **kwargs: Any) -> Any:
        self.calls.append(Call(algorithm_id, parameters, kwargs))
        feedback = self.kwargs_feedback(kwargs)
        if feedback is not None:
            for name, arguments in self.messages:
                getattr(feedback, name)(*arguments)
        if self.error is not None:
            raise self.error
        return self.results

    @staticmethod
    def kwargs_feedback(kwargs: dict[str, Any]) -> FakeFeedback | None:
        return kwargs.get("feedback")


class FakeQgis:
    """The ``qgis`` package holding only what the processing operations import."""

    def __init__(self) -> None:
        self.registry = FakeRegistry()
        self.processing = FakeProcessing()

    def modules(self) -> dict[str, types.ModuleType]:
        holder = self

        class QgsApplication:
            @staticmethod
            def processingRegistry() -> FakeRegistry:
                return holder.registry

        core = fake_module(
            "qgis.core",
            QgsApplication=QgsApplication,
            QgsProcessingFeedback=FakeFeedback,
            QgsProcessingParameterDefinition=FakeParameterDefinition,
            QgsProcessingParameterEnum=FakeEnumDefinition,
            QgsMapLayer=FakeLayer,
        )
        processing = fake_module("qgis.processing", run=self.processing.run)
        return {
            "qgis": fake_module("qgis", core=core, processing=processing),
            "qgis.core": core,
            "qgis.processing": processing,
        }


@pytest.fixture
def fake_qgis(monkeypatch: pytest.MonkeyPatch) -> FakeQgis:
    fake = FakeQgis()
    for name, module in fake.modules().items():
        monkeypatch.setitem(sys.modules, name, module)
    return fake


# --- helpers -----------------------------------------------------------------


def fake_module(name: str, **attributes: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    vars(module).update(attributes)
    return module


def make_context(project: Any) -> OperationContext:
    return OperationContext(iface=None, project=project, lifecycle=None, policy=Settings())


def project_with_layers(*layer_ids: str) -> FakeProject:
    project = FakeProject()
    for layer_id in layer_ids:
        project.layers[layer_id] = FakeLayer(layer_id)
    return project


def install(
    fake: FakeQgis,
    *definitions: Any,
    algorithm_id: str = "native:test",
    name: str = "Algorithm",
    group: str = "Group",
    short_description: str = "",
    help_: str = "",
    outputs: tuple[Any, ...] = (),
) -> FakeAlgorithm:
    algorithm = FakeAlgorithm(
        algorithm_id,
        name=name,
        group=group,
        short_description=short_description,
        help_=help_,
        definitions=definitions,
        outputs=outputs,
    )
    fake.registry.algorithms_by_id[algorithm_id] = algorithm
    return algorithm


def parameter_values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "STYLE",
        "description": "Line style",
        "help": "",
        "type": "enum",
        "options": ["Solid", "Dashed"],
        "accepted_types": ["str"],
    }
    values.update(overrides)
    return values


def run_params(parameters: dict[str, Any], algorithm_id: str = "native:test") -> Any:
    return ops.ProcessingRunAlgorithmParams(algorithm_id=algorithm_id, parameters=parameters)


def run(fake: FakeQgis, parameters: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    """One successful call, with the run's answer already in place."""

    fake.processing.results = kwargs.pop("results", {})
    return ops.ProcessingRunAlgorithm().execute_main(
        make_context(kwargs.pop("project", FakeProject())),
        run_params({} if parameters is None else parameters),
    )


# --- input and output models (design §3.5, §4.4, PRD §8.1) --------------------


def test_the_list_parameters_accept_only_an_empty_object() -> None:
    assert ops.ProcessingListAlgorithmsParams.model_validate({}).model_dump() == {}
    with pytest.raises(ValidationError):
        ops.ProcessingListAlgorithmsParams.model_validate({"provider": "native"})


def test_an_undeclared_field_is_refused_on_every_model() -> None:
    with pytest.raises(ValidationError):
        ops.AlgorithmSummary(id="a", name="A", group="G", runnable=True)
    with pytest.raises(ValidationError):
        ops.AlgorithmDetails(id="a", name="A", short_description="", help="", group="G")
    with pytest.raises(ValidationError):
        ops.AlgorithmParameter(**parameter_values(), optional=True)
    with pytest.raises(ValidationError):
        ops.AlgorithmOutput(name="OUT", type="outputVector", description="", path="x")
    with pytest.raises(ValidationError):
        ops.ProcessingMessage(level="info", text="", fatal=False, html="<b></b>")
    with pytest.raises(ValidationError):
        ops.ProcessingGetAlgorithmInfoParams(algorithm_id="a", provider="native")
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithmParams(algorithm_id="a", parameters={}, provider="native")
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithmResult(algorithm_id="a", outputs={}, messages=[], count=1)
    with pytest.raises(ValidationError):
        ops.ProcessingListAlgorithmsResult(algorithms=[], count=0)


def test_a_summary_only_takes_real_strings() -> None:
    with pytest.raises(ValidationError):
        ops.AlgorithmSummary(id=1, name="A", group="G")
    with pytest.raises(ValidationError):
        ops.AlgorithmSummary(id="a", name=None, group="G")
    assert ops.AlgorithmSummary(id="a", name="A", group="G").model_dump() == {
        "id": "a",
        "name": "A",
        "group": "G",
    }


def test_the_details_block_carries_exactly_the_four_design_fields() -> None:
    details = ops.AlgorithmDetails(
        id="native:buffer", name="Buffer", short_description="Buffers", help="How to buffer"
    )
    assert list(details.model_dump()) == ["id", "name", "short_description", "help"]
    with pytest.raises(ValidationError):
        ops.AlgorithmDetails(id="a", name="A", short_description=None, help="")
    with pytest.raises(ValidationError):
        ops.AlgorithmDetails(id="a", name="A", short_description="", help=1)


def test_a_parameter_carries_exactly_the_six_design_fields_in_order() -> None:
    parameter = ops.AlgorithmParameter(**parameter_values())
    assert list(parameter.model_dump()) == [
        "name",
        "description",
        "help",
        "type",
        "options",
        "accepted_types",
    ]
    assert parameter.options == ["Solid", "Dashed"]
    with pytest.raises(ValidationError):
        ops.AlgorithmParameter(**parameter_values(options="Solid,Dashed"))
    with pytest.raises(ValidationError):
        ops.AlgorithmParameter(**parameter_values(accepted_types=[1]))
    with pytest.raises(ValidationError):
        ops.AlgorithmParameter(**parameter_values(type=1))


def test_a_message_is_three_fields_and_not_fatal_by_default() -> None:
    message = ops.ProcessingMessage(level="info", text="started")
    assert message.model_dump() == {"level": "info", "text": "started", "fatal": False}
    with pytest.raises(ValidationError):
        ops.ProcessingMessage(level=1, text="started")
    with pytest.raises(ValidationError):
        ops.ProcessingMessage(level="error", text="broken", fatal="true")


def test_an_algorithm_id_cannot_be_empty() -> None:
    with pytest.raises(ValidationError):
        ops.ProcessingGetAlgorithmInfoParams(algorithm_id="")
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithmParams(algorithm_id="", parameters={})


def test_parameters_must_be_a_json_object() -> None:
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithmParams(algorithm_id="a", parameters=["DISTANCE", 10])


def test_parameters_are_kept_as_a_finite_json_tree() -> None:
    values = {
        "DISTANCE": 10,
        "FACTOR": 2.5,
        "LABEL": "x",
        "DISSOLVE": True,
        "OFFSET": None,
        "LAYERS": [1, None, {"k": [2.5]}],
    }
    params = ops.ProcessingRunAlgorithmParams(algorithm_id="a", parameters=values)
    assert params.parameters == values


def test_a_tuple_parameter_stays_a_json_array() -> None:
    params = ops.ProcessingRunAlgorithmParams(algorithm_id="a", parameters={"LAYERS": (1, 2)})
    assert params.parameters == {"LAYERS": [1, 2]}


@pytest.mark.parametrize(
    "value",
    [
        math.nan,
        math.inf,
        -math.inf,
        b"raw bytes",
        object(),
        {1, 2},
        Path("roads.gpkg"),
        {1: "a non-string key"},
    ],
    ids=["nan", "inf", "neg-inf", "bytes", "object", "set", "path", "int-key"],
)
def test_a_parameter_without_a_json_image_is_refused(value: Any) -> None:
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithmParams(algorithm_id="a", parameters={"VALUE": value})


def test_a_nested_non_string_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithmParams(
            algorithm_id="a", parameters={"FIELD_MAPPING": {1: "target"}}
        )


def test_a_parameter_key_must_be_a_string() -> None:
    # The top level is pydantic's dict rather than the walk's, so the key rule has
    # to be part of the field type too.
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithmParams(algorithm_id="a", parameters={b"OFFSET": 1})

    class Colour(StrEnum):
        RED = "red"

    # A str subclass is still a str, so an enum key keeps working.
    enum_keyed = ops.ProcessingRunAlgorithmParams(algorithm_id="a", parameters={Colour.RED: 1})
    assert enum_keyed.parameters == {"red": 1}


def _too_deep_list() -> Any:
    """A tree deeper than the interpreter recurses (1000 by default)."""

    value: Any = None
    for _ in range(sys.getrecursionlimit() + 1):
        value = [value]
    return value


def _cyclic_list() -> Any:
    """A list that contains itself: no finite JSON image exists for it."""

    value: list[Any] = []
    value.append(value)
    return value


@pytest.mark.parametrize(
    "value", [_too_deep_list(), _cyclic_list()], ids=["deeper-than-the-stack", "cyclic"]
)
def test_a_parameter_tree_the_json_walk_cannot_finish_is_invalid_input(value: Any) -> None:
    # RecursionError is not a ValidationError, and letting it escape reports a
    # caller's parameter tree as an internal failure.
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithmParams(algorithm_id="a", parameters={"VALUE": value})


def test_outputs_are_checked_as_json_too() -> None:
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithmResult(algorithm_id="a", outputs={"MEAN": math.nan}, messages=[])
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithmResult(algorithm_id="a", outputs={"LAYER": object()}, messages=[])
    result = ops.ProcessingRunAlgorithmResult(
        algorithm_id="a",
        outputs={"LIST": [1, None], "OBJECT": {"mean": 2.5}},
        messages=[ops.ProcessingMessage(level="info", text="done")],
    )
    assert result.model_dump(mode="json") == {
        "algorithm_id": "a",
        "outputs": {"LIST": [1, None], "OBJECT": {"mean": 2.5}},
        "messages": [{"level": "info", "text": "done", "fatal": False}],
    }


# --- processing.list_algorithms (PRD §3.1.1, P01) ----------------------------


def test_every_algorithm_is_listed_in_id_order(fake_qgis: FakeQgis) -> None:
    install(fake_qgis, algorithm_id="zzz:second", name="Second", group="Group B")
    install(fake_qgis, algorithm_id="aaa:first", name="First", group="Group A")
    install(fake_qgis, algorithm_id="native:buffer", name="Buffer", group="Vector geometry")

    result = ops.ProcessingListAlgorithms().execute_main(
        make_context(FakeProject()), ops.ProcessingListAlgorithmsParams()
    )
    assert [(summary.id, summary.name, summary.group) for summary in result.algorithms] == [
        ("aaa:first", "First", "Group A"),
        ("native:buffer", "Buffer", "Vector geometry"),
        ("zzz:second", "Second", "Group B"),
    ]


def test_an_empty_registry_lists_nothing(fake_qgis: FakeQgis) -> None:
    result = ops.ProcessingListAlgorithms().execute_main(
        make_context(FakeProject()), ops.ProcessingListAlgorithmsParams()
    )
    assert result.algorithms == []
    assert result.model_dump() == {"algorithms": []}


def test_the_list_result_carries_only_algorithms(fake_qgis: FakeQgis) -> None:
    install(fake_qgis, algorithm_id="native:buffer", name="Buffer", group="Vector geometry")
    result = ops.ProcessingListAlgorithms().execute_main(
        make_context(FakeProject()), ops.ProcessingListAlgorithmsParams()
    )
    assert result.model_dump() == {
        "algorithms": [{"id": "native:buffer", "name": "Buffer", "group": "Vector geometry"}]
    }


# --- processing.get_algorithm_info (design §3, P03, P04) ---------------------


def info(fake: FakeQgis, algorithm_id: str = "native:test") -> Any:
    return ops.ProcessingGetAlgorithmInfo().execute_main(
        make_context(FakeProject()),
        ops.ProcessingGetAlgorithmInfoParams(algorithm_id=algorithm_id),
    )


def test_info_carries_exactly_the_three_design_sections(fake_qgis: FakeQgis) -> None:
    install(fake_qgis, FakeDefinition("INPUT", "source"), outputs=(FakeOutput("OUTPUT"),))
    result = info(fake_qgis)
    assert list(result.model_dump()) == ["algorithm", "parameters", "outputs"]
    assert result.algorithm.model_dump() == {
        "id": "native:test",
        "name": "Algorithm",
        "short_description": "",
        "help": "",
    }


def test_info_reads_the_algorithm_block_from_the_definition(fake_qgis: FakeQgis) -> None:
    install(
        fake_qgis,
        algorithm_id="native:buffer",
        name="Buffer",
        short_description="Buffers features",
        help_="How to buffer",
    )
    details = info(fake_qgis, "native:buffer").algorithm
    assert (details.id, details.name) == ("native:buffer", "Buffer")
    assert (details.short_description, details.help) == ("Buffers features", "How to buffer")


def test_info_reports_a_missing_description_and_help_as_empty_strings(
    fake_qgis: FakeQgis,
) -> None:
    install(fake_qgis, FakeDefinition("INPUT", "source"))
    result = info(fake_qgis)
    assert result.algorithm.short_description == ""
    assert result.algorithm.help == ""
    assert (result.parameters[0].description, result.parameters[0].help) == ("", "")


def test_info_keeps_the_native_order_of_parameters_and_outputs(fake_qgis: FakeQgis) -> None:
    install(
        fake_qgis,
        FakeDefinition("OUTPUT", "sink"),
        FakeDefinition("INPUT", "source"),
        FakeDefinition("DISTANCE", "number"),
        outputs=(
            FakeOutput("OUTPUT", "Buffered"),
            FakeOutput("REPORT"),
        ),
    )
    result = info(fake_qgis)
    assert [parameter.name for parameter in result.parameters] == ["OUTPUT", "INPUT", "DISTANCE"]
    assert [(output.name, output.type, output.description) for output in result.outputs] == [
        ("OUTPUT", FakeOutput.__name__, "Buffered"),
        ("REPORT", FakeOutput.__name__, ""),
    ]


def test_info_reads_the_six_parameter_fields(fake_qgis: FakeQgis) -> None:
    enum = FakeEnumDefinition(
        "STYLE", ["Solid", "Dashed"], description="Line style", help_="How to draw the line"
    )
    plain = FakeDefinition("DISTANCE", "number")
    fake_qgis.registry.parameter_types["enum"] = FakeParameterType("str", "int")
    install(fake_qgis, enum, plain)

    first, second = info(fake_qgis).parameters
    assert first.model_dump() == {
        "name": "STYLE",
        "description": "Line style",
        "help": "How to draw the line",
        "type": FakeEnumDefinition.__name__,
        "options": ["Solid", "Dashed"],
        "accepted_types": ["str", "int"],
    }
    # A non-enum definition has no options at all, so the field states that.
    assert second.model_dump() == {
        "name": "DISTANCE",
        "description": "",
        "help": "",
        "type": FakeDefinition.__name__,
        "options": None,
        "accepted_types": [],
    }


def test_info_reports_an_unknown_parameter_type_as_an_empty_list(fake_qgis: FakeQgis) -> None:
    """``registry.parameterType()`` returning None is an empty list, not null."""
    install(fake_qgis, FakeDefinition("SRC", "customprobe"))
    assert info(fake_qgis).parameters[0].accepted_types == []


def test_info_skips_a_hidden_parameter(fake_qgis: FakeQgis) -> None:
    install(
        fake_qgis,
        FakeDefinition("INPUT", "source"),
        FakeDefinition("OPTIONS", "string", hidden=True),
        FakeDefinition("OUTPUT", "sink"),
    )
    assert [parameter.name for parameter in info(fake_qgis).parameters] == ["INPUT", "OUTPUT"]


def test_info_refuses_an_unknown_algorithm_as_invalid_input(fake_qgis: FakeQgis) -> None:
    with pytest.raises(FrameworkError) as caught:
        info(fake_qgis, "native:missing")
    assert caught.value.code == INVALID_INPUT
    assert caught.value.message == "the algorithm is not registered"


# --- processing.run_algorithm: the call (design §5.2, §5.5) ------------------


def test_the_run_hands_over_the_id_the_parameters_and_a_feedback(fake_qgis: FakeQgis) -> None:
    fake_qgis.processing.results = {"OUTPUT": "done"}
    parameters = {"INPUT": "roads", "DISTANCE": 5, "OUTPUT": "memory:"}

    result = ops.ProcessingRunAlgorithm().execute_main(
        make_context(project_with_layers("roads")), run_params(parameters)
    )

    assert len(fake_qgis.processing.calls) == 1
    call = fake_qgis.processing.calls[0]
    assert (call.algorithm_id, call.parameters) == ("native:test", parameters)
    # Nothing else is passed: no context, no onFinish, no is_child_algorithm.
    assert list(call.kwargs) == ["feedback"]
    assert isinstance(call.feedback, FakeFeedback)
    assert result.algorithm_id == "native:test"
    assert result.outputs == {"OUTPUT": "done"}
    assert result.messages == []


def test_the_run_never_asks_the_registry_for_an_instance(fake_qgis: FakeQgis) -> None:
    """The fake registry has no ``createAlgorithmById``; a call to it would AttributeError."""
    install(fake_qgis)
    assert not hasattr(fake_qgis.registry, "createAlgorithmById")

    run(fake_qgis)

    assert fake_qgis.registry.lookups == []


def test_the_parameters_reach_the_run_as_they_arrived(fake_qgis: FakeQgis) -> None:
    """A path, a remote value and an unknown key are all QGIS's to judge now."""
    parameters = {
        "INPUT": "https://example.com/data.gpkg",
        "MASK": r"C:\outside\the\roots\mask.shp",
        "P5_NOT_A_PARAMETER": 1,
    }
    run(fake_qgis, parameters)
    assert fake_qgis.processing.calls[0].parameters == parameters


def test_an_empty_result_is_a_success(fake_qgis: FakeQgis) -> None:
    assert run(fake_qgis, results={}).outputs == {}


def test_a_none_result_is_an_empty_success(fake_qgis: FakeQgis) -> None:
    assert run(fake_qgis, results=None).outputs == {}


def test_a_result_that_is_not_a_mapping_is_refused(fake_qgis: FakeQgis) -> None:
    fake_qgis.processing.results = ["not", "a", "mapping"]
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithm().execute_main(make_context(FakeProject()), run_params({}))


# --- processing.run_algorithm: the feedback records (design §4.4) ------------


def test_every_feedback_method_is_recorded_with_its_level(fake_qgis: FakeQgis) -> None:
    fake_qgis.processing.messages = [
        ("pushInfo", ("started",)),
        ("pushDebugInfo", ("a debug line",)),
        ("pushCommandInfo", ("gdal_translate -of GTiff",)),
        ("pushConsoleInfo", ("console output",)),
        ("pushFormattedMessage", ("<b>bold</b>", "bold")),
        ("pushWarning", ("careful",)),
        ("reportError", ("broken", False)),
    ]
    result = run(fake_qgis)
    assert [(message.level, message.text, message.fatal) for message in result.messages] == [
        ("info", "started", False),
        ("debug", "a debug line", False),
        ("command", "gdal_translate -of GTiff", False),
        ("console", "console output", False),
        ("formatted", "bold", False),
        ("warning", "careful", False),
        ("error", "broken", False),
    ]


def test_a_fatal_error_is_recorded_as_fatal(fake_qgis: FakeQgis) -> None:
    fake_qgis.processing.messages = [("reportError", ("the provider gave up", True))]
    message = run(fake_qgis).messages[0]
    assert (message.level, message.text, message.fatal) == ("error", "the provider gave up", True)


def test_the_feedback_keeps_the_base_class_calls_and_the_errors_view(fake_qgis: FakeQgis) -> None:
    fake_qgis.processing.messages = [
        ("pushInfo", ("started",)),
        ("reportError", ("first failure", False)),
        ("pushWarning", ("careful",)),
        ("reportError", ("second failure", True)),
    ]
    call = fake_qgis.processing.calls
    run(fake_qgis)
    feedback = call[0].feedback

    # Each base method ran before the record it belongs to was appended.
    assert feedback.base == [
        ("pushInfo", 0),
        ("reportError", 1),
        ("pushWarning", 2),
        ("reportError", 3),
    ]
    assert feedback.errors == ["first failure", "second failure"]
    assert feedback.has_errors is True


def test_a_feedback_without_an_error_reports_none(fake_qgis: FakeQgis) -> None:
    fake_qgis.processing.messages = [("pushInfo", ("started",))]
    run(fake_qgis)
    feedback = fake_qgis.processing.calls[0].feedback
    assert feedback.errors == []
    assert feedback.has_errors is False


# --- processing.run_algorithm: the result layers (design §5.7, P06, P14) -----


def test_a_result_layer_is_added_to_the_project_and_reported_by_its_id(
    fake_qgis: FakeQgis,
) -> None:
    layer = FakeLayer("memory-out")
    project = project_with_layers("roads")
    result = run(fake_qgis, results={"OUTPUT": layer}, project=project)

    assert result.outputs == {"OUTPUT": "memory-out"}
    assert project.mapLayer("memory-out") is layer
    assert project.added == [layer]


def test_a_layer_inside_a_list_and_a_mapping_is_added_as_well(fake_qgis: FakeQgis) -> None:
    first, second = FakeLayer("first-out"), FakeLayer("second-out")
    project = FakeProject()
    result = run(
        fake_qgis,
        results={"LAYERS": [first], "NESTED": {"LAYER": second}},
        project=project,
    )
    assert result.outputs == {"LAYERS": ["first-out"], "NESTED": {"LAYER": "second-out"}}
    assert project.added == [first, second]


def test_the_same_layer_is_added_once(fake_qgis: FakeQgis) -> None:
    layer = FakeLayer("memory-out")
    project = FakeProject()
    result = run(fake_qgis, results={"FIRST": layer, "SECOND": [layer]}, project=project)

    assert result.outputs == {"FIRST": "memory-out", "SECOND": ["memory-out"]}
    assert project.added == [layer]


def test_a_project_id_collision_is_an_internal_error(fake_qgis: FakeQgis) -> None:
    existing = FakeLayer("memory-out")
    project = FakeProject()
    project.layers["memory-out"] = existing

    with pytest.raises(FrameworkError) as caught:
        run(fake_qgis, results={"OUTPUT": FakeLayer("memory-out")}, project=project)
    assert caught.value.code == INTERNAL_ERROR
    # The project refused the second layer with that id; the first one is untouched.
    assert project.mapLayer("memory-out") is existing
    assert project.added == []


@pytest.mark.parametrize(
    "value",
    [object(), math.nan, b"raw bytes", {1, 2}, {1: "a non-string key"}],
    ids=["object", "nan", "bytes", "set", "int-key"],
)
def test_a_result_without_a_json_image_is_an_internal_error(
    fake_qgis: FakeQgis, value: Any
) -> None:
    with pytest.raises(FrameworkError) as caught:
        run(fake_qgis, results={"OUTPUT": value})
    assert caught.value.code == INTERNAL_ERROR
    # A refused result is described by its contract, never by an object's repr.
    assert caught.value.message in (
        "an algorithm result cannot be expressed as JSON",
        "an algorithm result key is not a string",
    )


def test_a_refused_result_removes_only_the_layers_this_run_added(fake_qgis: FakeQgis) -> None:
    layer = FakeLayer("memory-out")
    project = project_with_layers("roads")

    with pytest.raises(FrameworkError) as caught:
        run(fake_qgis, results={"OUTPUT": layer, "BROKEN": object()}, project=project)
    assert caught.value.code == INTERNAL_ERROR
    assert project.removed == ["memory-out"]
    assert project.mapLayer("memory-out") is None
    # The layer that was in the project before the call is untouched.
    assert project.mapLayer("roads") is not None


# --- processing.run_algorithm: a failure is QGIS's exception (design §5.6) --


class RunFailed(RuntimeError):
    """What a fake ``qgis.processing.run()`` raises."""


def test_an_exception_from_the_run_is_raised_unchanged(
    fake_qgis: FakeQgis, caplog: pytest.LogCaptureFixture
) -> None:
    native = "ERROR 1: PQconnectdb failed, connection to 127.0.0.1, port 1 refused"
    fake_qgis.processing.messages = [("pushInfo", (native,)), ("reportError", ("giving up", True))]
    fake_qgis.processing.error = RunFailed("the algorithm did not finish")

    with (
        caplog.at_level(logging.WARNING, logger=LOGGER_NAME),
        pytest.raises(RunFailed) as caught,
    ):
        ops.ProcessingRunAlgorithm().execute_main(make_context(FakeProject()), run_params({}))

    # The same exception object, not a plugin error code invented from its text.
    assert caught.value is fake_qgis.processing.error
    assert not isinstance(caught.value, FrameworkError)
    # The native messages and the exception text reach the local log only.
    assert native in caplog.text
    assert "RunFailed" in caplog.text
    assert "the algorithm did not finish" in caplog.text


def test_a_failed_run_reports_no_result_and_no_messages(fake_qgis: FakeQgis) -> None:
    fake_qgis.processing.messages = [("reportError", ("broken", False))]
    fake_qgis.processing.error = RunFailed("no result")
    with pytest.raises(RunFailed):
        ops.ProcessingRunAlgorithm().execute_main(make_context(FakeProject()), run_params({}))
    # The record is the engine's to build: this operation returns nothing on failure.
    assert fake_qgis.processing.calls[0].kwargs.keys() == {"feedback"}


# --- the declarations, the class placement and the import rule ---------------


def test_the_three_operations_register_as_main_thread_processing_capabilities() -> None:
    registry = OperationRegistry()
    for operation in ops.PROCESSING_OPERATIONS:
        registry.register(operation())

    assert [
        (spec.name, spec.kind, spec.strategy, spec.destructive) for spec in registry.list_specs()
    ] == [
        ("processing.get_algorithm_info", "query", "main_thread", False),
        ("processing.list_algorithms", "query", "main_thread", False),
        ("processing.run_algorithm", "command", "main_thread", True),
    ]


def test_the_feedback_is_built_inside_the_run_and_the_message_is_module_level() -> None:
    """Which names the module exports is the import rule: only the message is shared."""
    assert "ProcessingMessage" in vars(ops)
    assert not hasattr(ops, "ProcessingRunFeedback")


_GUARD = """
import sys

from qgis_agent_plugin.operations import processing

assert [operation.name for operation in processing.PROCESSING_OPERATIONS] == [
    "processing.list_algorithms",
    "processing.get_algorithm_info",
    "processing.run_algorithm",
]
assert callable(processing.ProcessingMessage)
assert not hasattr(processing, "ProcessingRunFeedback")
leaked = sorted(
    name
    for name in sys.modules
    if name.split(".")[0] in {"qgis", "PyQt5", "PyQt6", "PySide2", "PySide6"}
)
assert not leaked, leaked
"""


def test_the_processing_module_imports_without_qgis() -> None:
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ}
    env["PYTHONPATH"] = os.pathsep.join([str(root / "src"), env.get("PYTHONPATH", "")])
    result = subprocess.run(
        [sys.executable, "-c", _GUARD], capture_output=True, text=True, env=env, cwd=root
    )
    assert result.returncode == 0, result.stderr
