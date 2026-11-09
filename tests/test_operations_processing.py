"""The processing operations against a fake QGIS.

The real registry, providers and algorithms are exercised by the QGIS acceptance
run; here the question is whether the contract holds -- which keys reach the
native parameter check, which files are refused and which destination values are
handed on, what happens to a result layer, and which values can leave as JSON.  The
fakes are deliberately narrow: one algorithm prototype, one fresh instance per run,
and a context whose temporary store is visible from the test.
"""

from __future__ import annotations

import logging
import math
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
    INTERNAL_ERROR,
    INVALID_INPUT,
    LAYER_NOT_FOUND,
    PATH_NOT_ALLOWED,
    FrameworkError,
)
from qgis_agent_plugin.core.registry import OperationRegistry
from qgis_agent_plugin.operations import processing as ops

# Mirrors ``QgsProcessingParameterDefinition.FlagOptional`` in the 3.44.13 build;
# the fake module hands this value out as the class attribute the operation reads.
_FLAG_OPTIONAL = 8

# --- the fake QGIS -----------------------------------------------------------


class FakeLayer:
    """A layer the project or the context can hold; only identity matters here."""

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


class FakeLayerStore:
    """``QgsMapLayerStore``, as far as result ownership needs it."""

    def __init__(self, layers: tuple[Any, ...] = ()) -> None:
        self.layers = {layer.id(): layer for layer in layers}
        self.taken: list[str] = []

    def mapLayer(self, layer_id: str) -> Any | None:
        return self.layers.get(layer_id)

    def add(self, layer: Any) -> None:
        self.layers[layer.id()] = layer

    def take(self, layer_id: str) -> Any | None:
        self.taken.append(layer_id)
        return self.layers.pop(layer_id, None)


class FakeDefinition:
    """Enough of ``QgsProcessingParameterDefinition`` for one parameter summary."""

    def __init__(
        self,
        name: str,
        type_: str,
        *,
        description: str = "",
        optional: bool = False,
        default: Any = None,
        help_: str = "",
        destination: bool = False,
        depends_on: tuple[str, ...] = (),
    ) -> None:
        self._name = name
        self._type = type_
        self._description = description
        self._optional = optional
        self._default = default
        self._help = help_
        self._destination = destination
        self._depends_on = depends_on

    def name(self) -> str:
        return self._name

    def type(self) -> str:
        return self._type

    def description(self) -> str:
        return self._description

    def flags(self) -> int:
        return _FLAG_OPTIONAL if self._optional else 0

    def defaultValue(self) -> Any:
        return self._default

    def help(self) -> str:
        return self._help

    def isDestination(self) -> bool:
        return self._destination

    def dependsOnOtherParameters(self) -> list[str]:
        return list(self._depends_on)


class FakeEnumDefinition(FakeDefinition):
    """The one definition family with display options; the base class has none."""

    def __init__(self, name: str, options: list[str], **values: Any) -> None:
        super().__init__(name, "enum", **values)
        self._options = list(options)

    def options(self) -> list[str]:
        return list(self._options)


class FakeOutput:
    def __init__(self, name: str, type_: str, description: str = "") -> None:
        self._name = name
        self._type = type_
        self._description = description

    def name(self) -> str:
        return self._name

    def type(self) -> str:
        return self._type

    def description(self) -> str:
        return self._description


class FakeProvider:
    """The one provider answer the plugin asks for: can it produce a non-file result?"""

    def __init__(self, non_file_based_output: bool) -> None:
        self._non_file_based_output = non_file_based_output

    def supportsNonFileBasedOutput(self) -> bool:
        return self._non_file_based_output


class FakeAlgorithm:
    """One ``QgsProcessingAlgorithm`` instance, with a log of what was asked of it."""

    def __init__(
        self,
        algorithm_id: str,
        *,
        name: str = "Algorithm",
        group: str = "Group",
        description: str = "Help",
        definitions: tuple[Any, ...] = (),
        outputs: tuple[Any, ...] = (),
        executable: bool = True,
        refusal: tuple[bool, str] | None = None,
        results: Any = None,
        completed: bool = True,
        layers: tuple[Any, ...] = (),
        errors: tuple[str, ...] = (),
        non_file_based_output: bool = True,
    ) -> None:
        self.algorithm_id = algorithm_id
        self._name = name
        self._group = group
        self._description = description
        self._definitions = definitions
        self._outputs = outputs
        self._executable = executable
        self._refusal = refusal
        self._results = results
        self._completed = completed
        self._layers = layers
        self._errors = errors
        self._provider = FakeProvider(non_file_based_output)
        self.calls: list[Any] = []

    def id(self) -> str:
        return self.algorithm_id

    def displayName(self) -> str:
        return self._name

    def group(self) -> str:
        return self._group

    def shortHelpString(self) -> str:
        return self._description

    def parameterDefinitions(self) -> list[Any]:
        self.calls.append("parameterDefinitions")
        return list(self._definitions)

    def outputDefinitions(self) -> list[Any]:
        return list(self._outputs)

    def canExecute(self) -> tuple[bool, str]:
        # QGIS 3.44.13 binds this as ``(bool, message)``; verified on the real build.
        self.calls.append("canExecute")
        return (self._executable, "" if self._executable else "not available here")

    def provider(self) -> FakeProvider:
        return self._provider

    def checkParameterValues(self, parameters: Any, context: Any) -> tuple[bool, str]:
        self.calls.append(("checkParameterValues", parameters, context))
        return self._refusal if self._refusal is not None else (True, "")

    def run(self, parameters: Any, context: Any, feedback: Any) -> tuple[Any, bool]:
        self.calls.append(("run", parameters, context, feedback))
        for text in self._errors:
            feedback.reportError(text)
        for layer in self._layers:
            context.temporaryLayerStore().add(layer)
        return (self._results, self._completed)


class FakePrototype(FakeAlgorithm):
    """What ``algorithmById`` returns: running it is the bug the fake must expose."""

    def fresh(self) -> FakeAlgorithm:
        return FakeAlgorithm(
            self.algorithm_id,
            name=self._name,
            group=self._group,
            description=self._description,
            definitions=self._definitions,
            outputs=self._outputs,
            executable=self._executable,
            refusal=self._refusal,
            results=self._results,
            completed=self._completed,
            layers=self._layers,
            errors=self._errors,
            non_file_based_output=self._provider.supportsNonFileBasedOutput(),
        )

    def run(self, parameters: Any, context: Any, feedback: Any) -> tuple[Any, bool]:
        raise AssertionError("the registry prototype was run")


class FakeRegistry:
    """``QgsProcessingRegistry``: one prototype per id, one fresh instance per run."""

    def __init__(self) -> None:
        self.prototypes: dict[str, FakePrototype] = {}
        self.lookups: list[tuple[str, str]] = []
        self.instances: list[FakeAlgorithm] = []

    def algorithms(self) -> list[FakePrototype]:
        return list(self.prototypes.values())

    def algorithmById(self, algorithm_id: str) -> FakePrototype | None:
        self.lookups.append(("algorithmById", algorithm_id))
        return self.prototypes.get(algorithm_id)

    def createAlgorithmById(self, algorithm_id: str) -> FakeAlgorithm | None:
        self.lookups.append(("createAlgorithmById", algorithm_id))
        prototype = self.prototypes.get(algorithm_id)
        if prototype is None:
            return None
        instance = prototype.fresh()
        self.instances.append(instance)
        return instance


class FakeQgis:
    """A ``qgis.core`` holding only what the processing operations import."""

    def __init__(self) -> None:
        self.registry = FakeRegistry()
        self.seed_layers: tuple[Any, ...] = ()
        self.contexts: list[Any] = []
        self.feedback: list[Any] = []
        self.take_result_none = False
        holder = self

        class QgsProcessingContext:
            def __init__(self) -> None:
                self.project: Any = None
                self.store = FakeLayerStore(holder.seed_layers)
                holder.contexts.append(self)

            def setProject(self, project: Any) -> None:
                self.project = project

            def temporaryLayerStore(self) -> FakeLayerStore:
                return self.store

            def takeResultLayer(self, layer_id: str) -> Any | None:
                if holder.take_result_none:
                    return None
                return self.store.take(layer_id)

        class QgsProcessingFeedback:
            def __init__(self) -> None:
                holder.feedback.append(self)

            def reportError(self, message: str, fatalError: bool = False) -> None:
                pass

        class QgsApplication:
            @staticmethod
            def processingRegistry() -> FakeRegistry:
                return holder.registry

        class QgsProcessingParameterDefinition:
            FlagOptional = _FLAG_OPTIONAL

        self.QgsApplication = QgsApplication
        self.QgsProcessingContext = QgsProcessingContext
        self.QgsProcessingFeedback = QgsProcessingFeedback
        self.QgsProcessingParameterDefinition = QgsProcessingParameterDefinition


@pytest.fixture
def fake_qgis(monkeypatch: pytest.MonkeyPatch) -> FakeQgis:
    fake = FakeQgis()
    core = fake_module(
        "qgis.core",
        QgsApplication=fake.QgsApplication,
        QgsProcessingContext=fake.QgsProcessingContext,
        QgsProcessingFeedback=fake.QgsProcessingFeedback,
        QgsProcessingParameterDefinition=fake.QgsProcessingParameterDefinition,
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
    description: Any = "Help",
    outputs: tuple[Any, ...] = (),
    results: Any = None,
    completed: bool = True,
    executable: bool = True,
    refusal: tuple[bool, str] | None = None,
    layers: tuple[Any, ...] = (),
    errors: tuple[str, ...] = (),
    non_file_based_output: bool = True,
) -> FakePrototype:
    prototype = FakePrototype(
        algorithm_id,
        name=name,
        group=group,
        description=description,
        definitions=definitions,
        outputs=outputs,
        executable=executable,
        refusal=refusal,
        results={} if results is None else results,
        completed=completed,
        layers=layers,
        errors=errors,
        non_file_based_output=non_file_based_output,
    )
    fake.registry.prototypes[algorithm_id] = prototype
    return prototype


def standard_definitions() -> tuple[Any, ...]:
    return (
        FakeDefinition("INPUT", "source"),
        FakeDefinition("DISTANCE", "number", default=10),
        FakeDefinition("OUTPUT", "sink", destination=True),
    )


def run_params(parameters: dict[str, Any], algorithm_id: str = "native:test") -> Any:
    return ops.ProcessingRunAlgorithmParams(algorithm_id=algorithm_id, parameters=parameters)


def parameter_values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "STYLE",
        "type": "enum",
        "description": "Line style",
        "optional": False,
        "default": 0,
        "help": "",
        "is_destination": False,
        "depends_on": [],
        "options": ["Solid", "Dashed"],
    }
    values.update(overrides)
    return values


def refuse(operation: Any, ctx: OperationContext, request: Any) -> str:
    with pytest.raises(FrameworkError) as caught:
        operation.execute_main(ctx, request)
    return caught.value.code


def called(instance: FakeAlgorithm) -> list[Any]:
    """The stage names a fake instance was asked to perform, in order."""

    return [entry if isinstance(entry, str) else entry[0] for entry in instance.calls]


# --- input and output models (PRD §3.1.1, §3.1.2, §3.2.1, §8.1) --------------


def test_the_list_parameters_accept_only_an_empty_object() -> None:
    assert ops.ProcessingListAlgorithmsParams.model_validate({}).model_dump() == {}
    with pytest.raises(ValidationError):
        ops.ProcessingListAlgorithmsParams.model_validate({"provider": "native"})


def test_an_undeclared_field_is_refused_on_every_model() -> None:
    with pytest.raises(ValidationError):
        ops.AlgorithmSummary(id="a", name="A", group="G", runnable=True)
    with pytest.raises(ValidationError):
        ops.AlgorithmParameter(**parameter_values(), min=0)
    with pytest.raises(ValidationError):
        ops.AlgorithmOutput(name="OUT", type="outputVector", description="", path="x")
    with pytest.raises(ValidationError):
        ops.ProcessingGetAlgorithmInfoParams(algorithm_id="a", provider="native")
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithmParams(algorithm_id="a", parameters={}, provider="native")
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithmResult(algorithm_id="a", outputs={}, count=1)
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


def test_a_parameter_needs_real_booleans_and_string_lists() -> None:
    with pytest.raises(ValidationError):
        ops.AlgorithmParameter(**parameter_values(optional="true"))
    with pytest.raises(ValidationError):
        ops.AlgorithmParameter(**parameter_values(is_destination=1))
    with pytest.raises(ValidationError):
        ops.AlgorithmParameter(**parameter_values(depends_on=[1]))
    with pytest.raises(ValidationError):
        ops.AlgorithmParameter(**parameter_values(options="Solid,Dashed"))


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


def test_outputs_are_checked_as_json_too() -> None:
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithmResult(algorithm_id="a", outputs={"MEAN": math.nan})
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithmResult(algorithm_id="a", outputs={"LAYER": object()})
    result = ops.ProcessingRunAlgorithmResult(
        algorithm_id="a", outputs={"LIST": [1, None], "OBJECT": {"mean": 2.5}}
    )
    assert result.model_dump(mode="json") == {
        "algorithm_id": "a",
        "outputs": {"LIST": [1, None], "OBJECT": {"mean": 2.5}},
    }


def test_a_parameter_default_has_to_be_json_as_well() -> None:
    with pytest.raises(ValidationError):
        ops.AlgorithmParameter(**parameter_values(default=math.inf))
    with pytest.raises(ValidationError):
        ops.AlgorithmParameter(**parameter_values(default={"a": object()}))
    assert ops.AlgorithmParameter(**parameter_values(default=None)).default is None


# --- processing.list_algorithms (PRD §3.1.1, P01) ----------------------------


def test_every_algorithm_is_listed_in_id_order(fake_qgis: FakeQgis, data_root: Path) -> None:
    install(fake_qgis, algorithm_id="zzz:second", name="Second", group="Group B")
    install(fake_qgis, algorithm_id="aaa:first", name="First", group="Group A")
    install(fake_qgis, algorithm_id="native:buffer", name="Buffer", group="Vector geometry")

    result = ops.ProcessingListAlgorithms().execute_main(
        make_context(FakeProject(), data_root), ops.ProcessingListAlgorithmsParams()
    )
    assert [(summary.id, summary.name, summary.group) for summary in result.algorithms] == [
        ("aaa:first", "First", "Group A"),
        ("native:buffer", "Buffer", "Vector geometry"),
        ("zzz:second", "Second", "Group B"),
    ]


def test_an_empty_registry_lists_nothing(fake_qgis: FakeQgis, data_root: Path) -> None:
    result = ops.ProcessingListAlgorithms().execute_main(
        make_context(FakeProject(), data_root), ops.ProcessingListAlgorithmsParams()
    )
    assert result.algorithms == []
    assert result.model_dump() == {"algorithms": []}


def test_the_list_hides_nothing_and_asks_nothing_about_executability(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    prototype = install(fake_qgis, algorithm_id="third:party", executable=False)
    result = ops.ProcessingListAlgorithms().execute_main(
        make_context(FakeProject(), data_root), ops.ProcessingListAlgorithmsParams()
    )
    assert [summary.id for summary in result.algorithms] == ["third:party"]
    assert prototype.calls == []


def test_the_list_result_carries_only_algorithms(fake_qgis: FakeQgis, data_root: Path) -> None:
    install(fake_qgis, algorithm_id="native:buffer", name="Buffer", group="Vector geometry")
    result = ops.ProcessingListAlgorithms().execute_main(
        make_context(FakeProject(), data_root), ops.ProcessingListAlgorithmsParams()
    )
    assert result.model_dump() == {
        "algorithms": [{"id": "native:buffer", "name": "Buffer", "group": "Vector geometry"}]
    }


# --- processing.get_algorithm_info (PRD §3.1.2, P03, P04) --------------------


def test_info_keeps_the_native_order_of_parameters_and_outputs(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    install(
        fake_qgis,
        FakeDefinition("OUTPUT", "sink", destination=True),
        FakeDefinition("INPUT", "source"),
        FakeDefinition("DISTANCE", "number"),
        outputs=(
            FakeOutput("OUTPUT", "outputVector", "Buffered"),
            FakeOutput("REPORT", "outputString", ""),
        ),
    )

    result = ops.ProcessingGetAlgorithmInfo().execute_main(
        make_context(FakeProject(), data_root),
        ops.ProcessingGetAlgorithmInfoParams(algorithm_id="native:test"),
    )
    assert [parameter.name for parameter in result.parameters] == ["OUTPUT", "INPUT", "DISTANCE"]
    assert [(output.name, output.type, output.description) for output in result.outputs] == [
        ("OUTPUT", "outputVector", "Buffered"),
        ("REPORT", "outputString", ""),
    ]
    assert result.model_dump().keys() == {"algorithm", "description", "parameters", "outputs"}


def test_info_reads_the_nine_parameter_fields(fake_qgis: FakeQgis, data_root: Path) -> None:
    install(
        fake_qgis,
        FakeEnumDefinition(
            "STYLE",
            ["Solid", "Dashed"],
            description="Line style",
            optional=True,
            default=1,
            help_="How to draw the line",
            depends_on=("INPUT",),
        ),
        FakeDefinition("OUTPUT", "sink", destination=True),
        outputs=(FakeOutput("OUTPUT", "outputVector"),),
    )

    result = ops.ProcessingGetAlgorithmInfo().execute_main(
        make_context(FakeProject(), data_root),
        ops.ProcessingGetAlgorithmInfoParams(algorithm_id="native:test"),
    )
    style = result.parameters[0]
    assert style.model_dump().keys() == {
        "name",
        "type",
        "description",
        "optional",
        "default",
        "help",
        "is_destination",
        "depends_on",
        "options",
    }
    assert (style.name, style.type) == ("STYLE", "enum")
    assert (style.description, style.help) == ("Line style", "How to draw the line")
    assert style.optional is True
    assert style.default == 1
    assert style.is_destination is False
    assert style.depends_on == ["INPUT"]
    assert style.options == ["Solid", "Dashed"]

    destination = result.parameters[1]
    assert destination.is_destination is True
    assert destination.options is None
    # An absent native description or help is reported as empty, not invented.
    assert (destination.description, destination.help) == ("", "")


def test_info_reports_missing_help_and_description_as_empty_strings(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    install(fake_qgis, FakeDefinition("INPUT", "source"), description=None)
    result = ops.ProcessingGetAlgorithmInfo().execute_main(
        make_context(FakeProject(), data_root),
        ops.ProcessingGetAlgorithmInfoParams(algorithm_id="native:test"),
    )
    assert result.description == ""
    assert result.parameters[0].help == ""


def test_info_falls_back_to_null_for_a_default_without_a_json_image(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    install(
        fake_qgis,
        FakeDefinition("A", "number", default=object()),
        FakeDefinition("B", "number", default=math.nan),
        FakeDefinition("C", "number", default=[1, object()]),
        FakeDefinition("D", "number", default={"k": 1}),
        FakeDefinition("E", "number"),
        FakeDefinition("F", "number", default=[1, 2]),
        FakeDefinition("G", "number", default="10"),
    )

    result = ops.ProcessingGetAlgorithmInfo().execute_main(
        make_context(FakeProject(), data_root),
        ops.ProcessingGetAlgorithmInfoParams(algorithm_id="native:test"),
    )
    assert {parameter.name: parameter.default for parameter in result.parameters} == {
        "A": None,
        "B": None,
        "C": None,
        "D": {"k": 1},
        "E": None,
        "F": [1, 2],
        "G": "10",
    }


def test_info_refuses_an_unknown_algorithm_as_invalid_input(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    install(fake_qgis)
    code = refuse(
        ops.ProcessingGetAlgorithmInfo(),
        make_context(FakeProject(), data_root),
        ops.ProcessingGetAlgorithmInfoParams(algorithm_id="native:missing"),
    )
    assert code == INVALID_INPUT


# --- processing.run_algorithm: the call order (PRD §3.2.3, P05) --------------


def test_an_unknown_algorithm_is_invalid_input(fake_qgis: FakeQgis, data_root: Path) -> None:
    code = refuse(
        ops.ProcessingRunAlgorithm(),
        make_context(FakeProject(), data_root),
        run_params({}, algorithm_id="native:missing"),
    )
    assert code == INVALID_INPUT
    assert fake_qgis.registry.instances == []


def test_the_run_uses_a_fresh_instance_not_the_registry_prototype(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    prototype = install(fake_qgis, *standard_definitions(), results={"OUTPUT": "done"})
    ops.ProcessingRunAlgorithm().execute_main(
        make_context(project_with_layers("roads"), data_root),
        run_params({"INPUT": "roads", "OUTPUT": "memory:"}),
    )
    assert fake_qgis.registry.lookups == [
        ("algorithmById", "native:test"),
        ("createAlgorithmById", "native:test"),
    ]
    assert prototype.calls == []
    assert len(fake_qgis.registry.instances) == 1


def test_the_run_follows_the_prd_order(fake_qgis: FakeQgis, data_root: Path) -> None:
    install(fake_qgis, *standard_definitions(), results={"OUTPUT": "done"})
    project = project_with_layers("roads")

    ops.ProcessingRunAlgorithm().execute_main(
        make_context(project, data_root),
        run_params({"INPUT": "roads", "DISTANCE": 5, "OUTPUT": "memory:"}),
    )

    instance = fake_qgis.registry.instances[0]
    assert called(instance) == [
        "canExecute",
        "parameterDefinitions",
        "checkParameterValues",
        "run",
    ]
    checked = instance.calls[2][1]
    parameters, context, feedback = instance.calls[3][1:]
    assert checked == {"INPUT": "roads", "DISTANCE": 5, "OUTPUT": "memory:"}
    assert parameters == checked
    # The project the engine handed over, never the process-wide singleton.
    assert context.project is project
    assert fake_qgis.contexts == [context]
    assert isinstance(feedback, fake_qgis.QgsProcessingFeedback)


def test_an_algorithm_that_cannot_execute_is_refused_before_its_parameters(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    install(fake_qgis, *standard_definitions(), executable=False)
    code = refuse(
        ops.ProcessingRunAlgorithm(),
        make_context(FakeProject(), data_root),
        run_params({"NOT_A_PARAMETER": 1}),
    )
    assert code == INVALID_INPUT
    instance = fake_qgis.registry.instances[0]
    assert called(instance) == ["canExecute"]


def test_a_can_execute_answer_is_read_in_both_native_shapes() -> None:
    class Answer:
        def __init__(self, value: Any) -> None:
            self._value = value

        def canExecute(self) -> Any:
            return self._value

    assert ops._can_execute(Answer((True, ""))) == (True, "")
    assert ops._can_execute(Answer((False, "no provider"))) == (False, "no provider")
    assert ops._can_execute(Answer(False)) == (False, "")


def test_an_undeclared_parameter_name_is_refused(fake_qgis: FakeQgis, data_root: Path) -> None:
    install(fake_qgis, *standard_definitions(), results={})
    code = refuse(
        ops.ProcessingRunAlgorithm(),
        make_context(FakeProject(), data_root),
        run_params({"INPUT": "roads", "NOPE": 1}),
    )
    assert code == INVALID_INPUT
    # The plugin refuses the key itself: the native check never gets the chance to
    # accept it, because ``checkParameterValues`` does not reject unknown keys.
    assert called(fake_qgis.registry.instances[0]) == ["canExecute", "parameterDefinitions"]


# --- processing.run_algorithm: the path and layer boundaries (P12, P13) ------


def test_an_allowed_file_input_is_resolved_before_the_run(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    source = make_source(data_root, "clip mask.shp")
    install(fake_qgis, FakeDefinition("MASK", "file"), results={})

    ops.ProcessingRunAlgorithm().execute_main(
        make_context(FakeProject(), data_root), run_params({"MASK": str(source)})
    )
    instance = fake_qgis.registry.instances[0]
    assert instance.calls[2][1] == {"MASK": str(source.resolve())}
    assert instance.calls[3][1] == {"MASK": str(source.resolve())}


def test_a_file_input_outside_the_roots_is_refused(
    fake_qgis: FakeQgis, data_root: Path, tmp_path: Path
) -> None:
    outside = make_source(tmp_path)
    install(fake_qgis, FakeDefinition("MASK", "file"), results={})
    code = refuse(
        ops.ProcessingRunAlgorithm(),
        make_context(FakeProject(), data_root),
        run_params({"MASK": str(outside)}),
    )
    assert code == PATH_NOT_ALLOWED
    assert called(fake_qgis.registry.instances[0]) == ["canExecute", "parameterDefinitions"]


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/mask.gpkg",
        "\\\\server\\share\\mask.gpkg",
        "/vsicurl/https://example.com/mask.gpkg",
    ],
    ids=["url", "unc", "vsi"],
)
def test_a_remote_file_input_is_refused(fake_qgis: FakeQgis, data_root: Path, value: str) -> None:
    install(fake_qgis, FakeDefinition("MASK", "file"), results={})
    code = refuse(
        ops.ProcessingRunAlgorithm(),
        make_context(FakeProject(), data_root),
        run_params({"MASK": value}),
    )
    assert code == PATH_NOT_ALLOWED


def test_a_remote_value_is_refused_on_a_type_this_build_does_not_know(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """A type the plugin cannot classify keeps the strict refusal (PRD §3.2.2)."""
    install(fake_qgis, FakeDefinition("SRC", "customprobe"), results={})
    code = refuse(
        ops.ProcessingRunAlgorithm(),
        make_context(FakeProject(), data_root),
        run_params({"SRC": "/vsicurl/http://example.invalid/secret.csv"}),
    )
    assert code == PATH_NOT_ALLOWED
    assert called(fake_qgis.registry.instances[0]) == ["canExecute", "parameterDefinitions"]


@pytest.mark.parametrize(
    "value",
    ["EPSG:32633", "urn:ogc:def:crs:EPSG::32633", "32633", "+proj=utm +zone=33 +datum=WGS84"],
    ids=["authid", "urn", "code", "proj"],
)
def test_a_crs_value_reaches_the_algorithm_as_it_arrived(
    fake_qgis: FakeQgis, data_root: Path, value: str
) -> None:
    """A crs is an identifier QGIS reads itself, not a URI (review r1 B1).

    ``EPSG:32633`` has the shape of a URI scheme, which is why the blanket remote
    refusal used to reject ``native:reprojectlayer``'s standard spelling.
    """
    install(fake_qgis, FakeDefinition("TARGET_CRS", "crs"), results={})

    ops.ProcessingRunAlgorithm().execute_main(
        make_context(FakeProject(), data_root), run_params({"TARGET_CRS": value})
    )
    assert fake_qgis.registry.instances[0].calls[2][1] == {"TARGET_CRS": value}


@pytest.mark.parametrize(
    ("type_", "value"),
    [
        ("string", "EPSG:32633"),
        ("string", "table:alpha"),
        ("string", "https://example.invalid/data.csv"),
        ("enum", "\\\\server\\share\\data.csv"),
        ("field", "roads:name"),
        ("expression", '"pop" / 2'),
        ("extent", "1,2,3,4 [EPSG:32633]"),
        ("number", 3),
        ("boolean", True),
        ("layout", "A3:plan"),
        ("maptheme", "A3:plan"),
        ("attribute", "dim:name"),
        ("color", "#ff0000"),
        ("coordinateoperation", "EPSG:1188"),
    ],
    ids=[
        "string-authid",
        "string-table",
        "string-url",
        "enum-unc",
        "field",
        "expression",
        "extent",
        "number",
        "boolean",
        "layout",
        "maptheme",
        "attribute",
        "color",
        "coordinateoperation",
    ],
)
def test_a_value_parameter_keeps_its_value(
    fake_qgis: FakeQgis, data_root: Path, type_: str, value: Any
) -> None:
    """The plugin judges a value type not at all; parsing the value is QGIS's job."""
    install(fake_qgis, FakeDefinition("VALUE", type_), results={})

    ops.ProcessingRunAlgorithm().execute_main(
        make_context(FakeProject(), data_root), run_params({"VALUE": value})
    )
    assert fake_qgis.registry.instances[0].calls[2][1] == {"VALUE": value}


# The 53 parameter type strings the 440 registered algorithms report on 3.44.13,
# copied by hand: this is the independent statement the module's own lists must add up
# to, so dropping a name from either side fails here instead of silently widening the
# boundary.
MEASURED_PARAMETER_TYPES = frozenset(
    {
        "aggregates",
        "alignrasterlayers",
        "area",
        "attribute",
        "authcfg",
        "band",
        "boolean",
        "color",
        "coordinateoperation",
        "crs",
        "databaseschema",
        "databasetable",
        "distance",
        "duration",
        "dxflayers",
        "enum",
        "execute_sql",
        "expression",
        "extent",
        "field",
        "fields_mapping",
        "file",
        "fileDestination",
        "folderDestination",
        "geometry",
        "idw_interpolation_data",
        "layer",
        "layout",
        "layoutitem",
        "maptheme",
        "matrix",
        "mesh",
        "meshdatasetgroups",
        "meshdatasettime",
        "multilayer",
        "number",
        "point",
        "pointCloudDestination",
        "pointcloud",
        "providerconnection",
        "raster",
        "rasterDestination",
        "raster_calc_expression",
        "relief_colors",
        "scale",
        "sink",
        "source",
        "string",
        "tininputlayers",
        "vector",
        "vectorDestination",
        "vectorTileDestination",
        "vectortilewriterlayers",
    }
)

# The types that name I/O: a destination, one file, a layer reference, a provider
# connection, or a value with layer references inside it.
IO_PARAMETER_TYPES = frozenset(
    {
        "alignrasterlayers",
        "databaseschema",
        "databasetable",
        "dxflayers",
        "file",
        "fileDestination",
        "folderDestination",
        "idw_interpolation_data",
        "layer",
        "mesh",
        "multilayer",
        "pointCloudDestination",
        "pointcloud",
        "providerconnection",
        "raster",
        "rasterDestination",
        "sink",
        "source",
        "tininputlayers",
        "vector",
        "vectorDestination",
        "vectorTileDestination",
        "vectortilewriterlayers",
    }
)


def test_the_value_and_io_lists_account_for_every_measured_parameter_type() -> None:
    """Every one of the 53 measured types is either a value or an I/O type."""
    assert ops._MEASURED_PARAMETER_TYPES == MEASURED_PARAMETER_TYPES
    assert ops._IO_PARAMETER_TYPES == IO_PARAMETER_TYPES
    assert ops._VALUE_PARAMETERS | ops._IO_PARAMETER_TYPES == MEASURED_PARAMETER_TYPES
    assert ops._VALUE_PARAMETERS.isdisjoint(ops._IO_PARAMETER_TYPES)
    assert len(MEASURED_PARAMETER_TYPES) == 53


def test_a_layer_parameter_must_name_a_project_layer(fake_qgis: FakeQgis, data_root: Path) -> None:
    install(fake_qgis, FakeDefinition("INPUT", "source"), results={})
    code = refuse(
        ops.ProcessingRunAlgorithm(),
        make_context(project_with_layers("roads"), data_root),
        run_params({"INPUT": "missing-layer"}),
    )
    assert code == LAYER_NOT_FOUND
    assert called(fake_qgis.registry.instances[0]) == ["canExecute", "parameterDefinitions"]


def test_an_allowed_local_file_may_stand_in_for_a_layer_parameter(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    source = make_source(data_root)
    install(fake_qgis, FakeDefinition("INPUT", "source"), results={})

    ops.ProcessingRunAlgorithm().execute_main(
        make_context(FakeProject(), data_root), run_params({"INPUT": str(source)})
    )
    assert fake_qgis.registry.instances[0].calls[2][1] == {"INPUT": str(source.resolve())}


def test_a_layer_source_outside_the_roots_is_refused(
    fake_qgis: FakeQgis, data_root: Path, tmp_path: Path
) -> None:
    outside = make_source(tmp_path)
    install(fake_qgis, FakeDefinition("INPUT", "source"), results={})
    code = refuse(
        ops.ProcessingRunAlgorithm(),
        make_context(FakeProject(), data_root),
        run_params({"INPUT": str(outside)}),
    )
    assert code == PATH_NOT_ALLOWED


@pytest.mark.parametrize(
    "type_",
    [
        "idw_interpolation_data",
        "tininputlayers",
        "alignrasterlayers",
        "dxflayers",
        "vectortilewriterlayers",
    ],
)
def test_a_parameter_type_carrying_layers_inside_its_value_is_refused(
    fake_qgis: FakeQgis, data_root: Path, type_: str
) -> None:
    """QGIS loads the sources inside these values, so the plugin cannot pass them on."""
    install(fake_qgis, FakeDefinition("LAYERS", type_), results={})
    code = refuse(
        ops.ProcessingRunAlgorithm(),
        make_context(FakeProject(), data_root),
        run_params({"LAYERS": [{"layer": "https://example.com/layers.gpkg", "type": 0}]}),
    )
    assert code == INVALID_INPUT
    assert called(fake_qgis.registry.instances[0]) == ["canExecute", "parameterDefinitions"]


def test_a_nested_layer_reference_is_refused_even_when_it_names_a_project_layer(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """The refusal is about the parameter type, not about the particular value."""
    install(fake_qgis, FakeDefinition("LAYERS", "dxflayers"), results={})
    code = refuse(
        ops.ProcessingRunAlgorithm(),
        make_context(project_with_layers("roads"), data_root),
        run_params({"LAYERS": [{"layer": "roads"}]}),
    )
    assert code == INVALID_INPUT


@pytest.mark.parametrize(
    "value",
    ["TEMPORARY_OUTPUT", "C:/data/out.gpkg", ""],
    ids=["temporary", "path", "empty"],
)
def test_a_string_destination_reaches_the_algorithm_unchanged(
    fake_qgis: FakeQgis, data_root: Path, value: str
) -> None:
    """Where a result is written is the caller's decision and QGIS's to judge."""
    install(fake_qgis, FakeDefinition("OUTPUT", "sink", destination=True), results={})
    ops.ProcessingRunAlgorithm().execute_main(
        make_context(FakeProject(), data_root), run_params({"OUTPUT": value})
    )
    assert fake_qgis.registry.instances[0].calls[2][1] == {"OUTPUT": value}


@pytest.mark.parametrize("value", [{"type": "memory"}, 5], ids=["object", "number"])
def test_a_destination_that_is_not_a_string_is_refused(
    fake_qgis: FakeQgis, data_root: Path, value: Any
) -> None:
    install(fake_qgis, FakeDefinition("OUTPUT", "sink", destination=True), results={})
    code = refuse(
        ops.ProcessingRunAlgorithm(),
        make_context(FakeProject(), data_root),
        run_params({"OUTPUT": value}),
    )
    assert code == INVALID_INPUT
    assert called(fake_qgis.registry.instances[0]) == ["canExecute", "parameterDefinitions"]


def test_a_memory_destination_needs_a_provider_that_can_keep_a_result_in_memory(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """``gdal:buffervectors`` answers SUCCEEDED with the literal string and no layer."""
    install(
        fake_qgis,
        FakeDefinition("OUTPUT", "sink", destination=True),
        results={},
        non_file_based_output=False,
    )
    code = refuse(
        ops.ProcessingRunAlgorithm(),
        make_context(FakeProject(), data_root),
        run_params({"OUTPUT": "memory:"}),
    )
    assert code == INVALID_INPUT
    assert called(fake_qgis.registry.instances[0]) == ["canExecute", "parameterDefinitions"]


def test_a_memory_destination_reaches_a_provider_that_supports_it(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    install(fake_qgis, FakeDefinition("OUTPUT", "sink", destination=True), results={})
    ops.ProcessingRunAlgorithm().execute_main(
        make_context(FakeProject(), data_root), run_params({"OUTPUT": "memory:"})
    )
    assert fake_qgis.registry.instances[0].calls[2][1] == {"OUTPUT": "memory:"}


def test_a_remote_value_as_a_destination_is_still_refused(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """The strict-type rule is untouched: only ``memory:`` escapes the remote refusal."""
    install(fake_qgis, FakeDefinition("OUTPUT", "vectorDestination", destination=True), results={})
    code = refuse(
        ops.ProcessingRunAlgorithm(),
        make_context(FakeProject(), data_root),
        run_params({"OUTPUT": "/vsicurl/http://127.0.0.1:9/never.gpkg"}),
    )
    assert code == PATH_NOT_ALLOWED
    assert called(fake_qgis.registry.instances[0]) == ["canExecute", "parameterDefinitions"]


def test_an_unset_destination_is_left_unset(fake_qgis: FakeQgis, data_root: Path) -> None:
    install(fake_qgis, FakeDefinition("OUTPUT", "sink", destination=True), results={})
    ops.ProcessingRunAlgorithm().execute_main(
        make_context(FakeProject(), data_root), run_params({"OUTPUT": None})
    )
    assert fake_qgis.registry.instances[0].calls[2][1] == {"OUTPUT": None}


def test_an_unset_file_input_is_left_unset(fake_qgis: FakeQgis, data_root: Path) -> None:
    install(fake_qgis, FakeDefinition("MASK", "file"), results={})
    ops.ProcessingRunAlgorithm().execute_main(
        make_context(FakeProject(), data_root), run_params({"MASK": None})
    )
    assert fake_qgis.registry.instances[0].calls[2][1] == {"MASK": None}


def test_an_unset_provider_connection_is_left_unset(fake_qgis: FakeQgis, data_root: Path) -> None:
    install(fake_qgis, FakeDefinition("CONNECTION", "providerconnection"), results={})
    ops.ProcessingRunAlgorithm().execute_main(
        make_context(FakeProject(), data_root), run_params({"CONNECTION": None})
    )
    assert fake_qgis.registry.instances[0].calls[2][1] == {"CONNECTION": None}


@pytest.mark.parametrize(
    "type_", ["providerconnection", "databaseschema", "databasetable"], ids=str
)
def test_a_provider_connection_parameter_is_refused(
    fake_qgis: FakeQgis, data_root: Path, type_: str
) -> None:
    install(fake_qgis, FakeDefinition("CONNECTION", type_), results={})
    code = refuse(
        ops.ProcessingRunAlgorithm(),
        make_context(FakeProject(), data_root),
        run_params({"CONNECTION": "some_connection"}),
    )
    assert code == PATH_NOT_ALLOWED


def test_the_run_never_invents_a_destination(fake_qgis: FakeQgis, data_root: Path) -> None:
    install(fake_qgis, *standard_definitions(), results={})
    result = ops.ProcessingRunAlgorithm().execute_main(
        make_context(project_with_layers("roads"), data_root), run_params({"INPUT": "roads"})
    )
    instance = fake_qgis.registry.instances[0]
    assert instance.calls[2][1] == {"INPUT": "roads"}
    assert instance.calls[3][1] == {"INPUT": "roads"}
    assert result.outputs == {}


# --- processing.run_algorithm: the native answer (P05, P08, P15) -------------


def test_a_native_parameter_refusal_is_invalid_input_without_its_message(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    install(
        fake_qgis,
        *standard_definitions(),
        refusal=(False, "Could not load source layer for INPUT: C:/secret/roads.gpkg"),
        results={},
    )
    with pytest.raises(FrameworkError) as caught:
        ops.ProcessingRunAlgorithm().execute_main(
            make_context(FakeProject(), data_root), run_params({"DISTANCE": 5})
        )
    assert caught.value.code == INVALID_INPUT
    assert "secret" not in caught.value.message
    assert called(fake_qgis.registry.instances[0]) == [
        "canExecute",
        "parameterDefinitions",
        "checkParameterValues",
    ]


def test_an_incomplete_run_is_an_internal_error(fake_qgis: FakeQgis, data_root: Path) -> None:
    install(fake_qgis, results={"OUTPUT": "done"}, completed=False)
    code = refuse(
        ops.ProcessingRunAlgorithm(),
        make_context(FakeProject(), data_root),
        run_params({}),
    )
    assert code == INTERNAL_ERROR


def test_a_run_without_a_result_mapping_is_an_internal_error(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    install(fake_qgis, results="not a mapping")
    code = refuse(
        ops.ProcessingRunAlgorithm(),
        make_context(FakeProject(), data_root),
        run_params({}),
    )
    assert code == INTERNAL_ERROR


def test_a_run_that_reports_an_error_and_returns_nothing_is_an_internal_error(
    fake_qgis: FakeQgis, data_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``ok=True`` is not success when the algorithm itself reported a failure."""
    native = 'ERROR 1: PQconnectdb failed, connection to "127.0.0.1", port 1 refused'
    install(fake_qgis, results={}, errors=(native,))
    with (
        caplog.at_level(logging.WARNING, logger="qgis_agent_plugin.operations.processing"),
        pytest.raises(FrameworkError) as caught,
    ):
        ops.ProcessingRunAlgorithm().execute_main(
            make_context(FakeProject(), data_root), run_params({})
        )
    assert caught.value.code == INTERNAL_ERROR
    # The native text can quote a host or a path: it reaches the log, never the DTO.
    assert "127.0.0.1" not in caught.value.message
    assert native in caplog.text


def test_a_run_that_reports_an_error_but_returns_a_result_stays_successful(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    """A run with real output is not overruled by text on the feedback."""
    install(fake_qgis, results={"OUTPUT": "done"}, errors=("something it logged",))
    result = ops.ProcessingRunAlgorithm().execute_main(
        make_context(FakeProject(), data_root), run_params({})
    )
    assert result.outputs == {"OUTPUT": "done"}


def test_a_run_returns_every_scalar_and_structured_output(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    results = {
        "MEAN": 12.5,
        "COUNT": 7,
        "OK": True,
        "NOTHING": None,
        "LIST": [1, 2.5],
        "OBJECT": {"nested": [None]},
        "LABEL": "mean of three bands",
    }
    install(fake_qgis, results=results)

    result = ops.ProcessingRunAlgorithm().execute_main(
        make_context(FakeProject(), data_root), run_params({})
    )
    assert result.algorithm_id == "native:test"
    assert result.outputs == results
    assert result.model_dump(mode="json") == {"algorithm_id": "native:test", "outputs": results}


@pytest.mark.parametrize(
    "value",
    [math.nan, math.inf, object(), b"raw bytes", {1, 2}],
    ids=["nan", "inf", "object", "bytes", "set"],
)
def test_a_result_without_a_json_image_is_an_internal_error(
    fake_qgis: FakeQgis, data_root: Path, value: Any
) -> None:
    install(fake_qgis, results={"OUTPUT": value})
    with pytest.raises(FrameworkError) as caught:
        ops.ProcessingRunAlgorithm().execute_main(
            make_context(FakeProject(), data_root), run_params({})
        )
    assert caught.value.code == INTERNAL_ERROR
    # A refused result is described by its contract, never by an object's repr.
    assert caught.value.message == "an algorithm result cannot be expressed as JSON"


def test_a_non_string_result_key_is_an_internal_error(fake_qgis: FakeQgis, data_root: Path) -> None:
    install(fake_qgis, results={1: "value"})
    with pytest.raises(FrameworkError) as caught:
        ops.ProcessingRunAlgorithm().execute_main(
            make_context(FakeProject(), data_root), run_params({})
        )
    assert caught.value.code == INTERNAL_ERROR
    assert caught.value.message == "an algorithm result key is not a string"


# --- processing.run_algorithm: result layer ownership (P06, P14) -------------


def test_a_temporary_result_layer_is_moved_into_the_project(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    temporary = FakeLayer("memory-out")
    install(fake_qgis, results={"OUTPUT": "memory-out"}, layers=(temporary,))
    project = project_with_layers("roads")

    result = ops.ProcessingRunAlgorithm().execute_main(
        make_context(project, data_root), run_params({})
    )
    assert result.outputs == {"OUTPUT": "memory-out"}
    assert project.layers["memory-out"] is temporary
    assert project.added == [temporary]
    context = fake_qgis.contexts[0]
    assert context.store.taken == ["memory-out"]
    assert context.store.layers == {}


def test_a_result_that_names_a_project_layer_keeps_its_id(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    install(fake_qgis, results={"OUTPUT": "roads"})
    project = project_with_layers("roads")

    result = ops.ProcessingRunAlgorithm().execute_main(
        make_context(project, data_root), run_params({})
    )
    assert result.outputs == {"OUTPUT": "roads"}
    assert project.added == []
    assert fake_qgis.contexts[0].store.taken == []


def test_a_repeated_result_id_resolves_to_the_same_project_layer(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    temporary = FakeLayer("memory-out")
    install(
        fake_qgis,
        results={"FIRST": "memory-out", "SECOND": ["memory-out"]},
        layers=(temporary,),
    )
    project = FakeProject()

    result = ops.ProcessingRunAlgorithm().execute_main(
        make_context(project, data_root), run_params({})
    )
    # The same result id is taken from the context once and added once.
    assert result.outputs == {"FIRST": "memory-out", "SECOND": ["memory-out"]}
    assert fake_qgis.contexts[0].store.taken == ["memory-out"]
    assert project.added == [temporary]


def test_a_project_id_collision_is_an_internal_error(fake_qgis: FakeQgis, data_root: Path) -> None:
    temporary = FakeLayer("memory-out")
    install(fake_qgis, results={"OUTPUT": "memory-out"}, layers=(temporary,))
    existing = FakeLayer("memory-out")
    project = FakeProject()
    project.layers["memory-out"] = existing

    with pytest.raises(FrameworkError) as caught:
        ops.ProcessingRunAlgorithm().execute_main(make_context(project, data_root), run_params({}))
    assert caught.value.code == INTERNAL_ERROR
    # The project refused the second layer with that id; the first one is untouched.
    assert project.mapLayer("memory-out") is existing
    assert project.added == []


def test_a_layer_the_context_cannot_hand_over_is_an_internal_error(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    temporary = FakeLayer("memory-out")
    install(fake_qgis, results={"OUTPUT": "memory-out"}, layers=(temporary,))
    fake_qgis.take_result_none = True
    project = FakeProject()

    with pytest.raises(FrameworkError) as caught:
        ops.ProcessingRunAlgorithm().execute_main(make_context(project, data_root), run_params({}))
    assert caught.value.code == INTERNAL_ERROR
    assert project.added == []


def test_a_failed_result_removes_only_the_layers_this_run_added(
    fake_qgis: FakeQgis, data_root: Path
) -> None:
    temporary = FakeLayer("memory-out")
    install(fake_qgis, results={"OUTPUT": "memory-out", "BROKEN": math.nan}, layers=(temporary,))
    project = project_with_layers("roads")

    with pytest.raises(FrameworkError) as caught:
        ops.ProcessingRunAlgorithm().execute_main(make_context(project, data_root), run_params({}))
    assert caught.value.code == INTERNAL_ERROR
    assert project.removed == ["memory-out"]
    assert project.mapLayer("memory-out") is None
    # The layer that was in the project before the call is untouched.
    assert project.mapLayer("roads") is not None


# --- the declarations and the import rule ------------------------------------


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


_GUARD = """
import sys

from qgis_agent_plugin.operations import processing

assert [operation.name for operation in processing.PROCESSING_OPERATIONS] == [
    "processing.list_algorithms",
    "processing.get_algorithm_info",
    "processing.run_algorithm",
]
assert callable(processing.algorithm_summary)
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
