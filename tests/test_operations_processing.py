"""processing 操作的契约测试：用一副最小的假 QGIS 盯住调用形状，而不去重跑真实算法。

真实的注册表、提供者与算法由 QGIS 侧的验收跑覆盖；这里只问契约是否成立——一份算法报告
带哪些字段、交给 ``qgis.processing.run()`` 的到底是什么、关键字里有几项、一次运行从反馈
对象上留下什么、结果图层落到哪里、哪些值能作为 JSON 离开。假件刻意做窄：一个进程级的
``run`` 只回一个写死的答案、一个会自己记账的反馈基类、一个带 QGIS 真实「id 不可重复」
规则的工程。参数检查在这里是预设的答案：假定义只说“接受”或“不接受”，原因是 QGIS 的判据
不是本模块的，要由验收跑去量；测试要证明的是操作问了 QGIS 的问题，并且把答案报成了同一句话。

The processing operations against a fake QGIS: the question is whether the contract
holds -- which fields a definition report carries, what reaches ``qgis.processing.run()``
and with which keywords, what a run keeps from its feedback, what happens to a result
layer, and which values can leave as JSON.  The fakes are deliberately narrow: one
process-wide ``run`` with a canned answer, a feedback base class that notes what was
asked of it, and a project with QGIS's real duplicate-id rule.  The parameter checks
are canned answers here: a fake definition says "acceptable" or not, because the
criterion is QGIS's and the acceptance run measures it; what a test proves is that the
operation asks QGIS's questions and reports the answers in the same sentence.
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

import pytest  # pyright: ignore[reportMissingImports]
from pydantic import ValidationError  # pyright: ignore[reportMissingImports]

from qgis_agent_plugin.core.config import Settings  # pyright: ignore[reportMissingImports]
from qgis_agent_plugin.core.contracts import (  # pyright: ignore[reportMissingImports]
    OperationContext,
)
from qgis_agent_plugin.core.models import (  # pyright: ignore[reportMissingImports]
    INTERNAL_ERROR,
    INVALID_INPUT,
    FrameworkError,
)
from qgis_agent_plugin.core.registry import OperationRegistry  # pyright: ignore[reportMissingImports]
from qgis_agent_plugin.operations import processing as ops  # pyright: ignore[reportMissingImports]

# 与 3.44.13 里 ``FlagHidden`` / ``FlagOptional`` 的取值一致。
# Mirrors ``FlagHidden`` / ``FlagOptional`` in 3.44.13.
_FLAG_HIDDEN = 4
_FLAG_OPTIONAL = 8

# 同两个算法标志的真实取值；假件只需要它们互不相同。
# The real values of the two algorithm flags; the fake only needs them to differ.
_FLAG_HIDE_FROM_TOOLBOX = 64
_FLAG_KNOWN_ISSUES = 128

# 两个开关的 QgsSettings 键；fake 把持久化的值原样返回，字符串归一由被测代码自己做。
# The two switches' QgsSettings keys; the fake returns the persisted value as it is, and the
# code under test does the string normalisation.
_SETTINGS_WARN_UNMATCHING_CRS = "Processing/Configuration/WARN_UNMATCHING_CRS"
_SETTINGS_SHOW_KNOWN_ISSUES = "Processing/Configuration/SHOW_ALGORITHMS_KNOWN_ISSUES"

LOGGER_NAME = "qgis_agent_plugin.operations.processing"

# --- 假 QGIS 层 / the fake QGIS ---------------------------------------------


class FakeProject:
    """一次调用手里的工程句柄；processing 的操作不直接用它。

    The project handle one call holds; a processing operation does not read it.
    """


class FakeFeedback:
    """``QgsProcessingFeedback`` 的替身：记录都挂在它这个基类上。

    每次调用都记下自己的名字和当时已有多少条记录，测试因此能确认基类方法是在子类写入
    那条消息之前跑完的。

    A stand-in for ``QgsProcessingFeedback``, the base implementation the records sit on.
    Each call notes its name and how many records already existed, which is how a test
    can see that the base class ran *before* the subclass recorded its own message.
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
    """只装报告读到的两个标志位的命名空间。

    The namespace holding the two flags the reports read.
    """

    FlagHidden = _FLAG_HIDDEN
    FlagOptional = _FLAG_OPTIONAL


class FakeDefinition:
    """一条参数定义里被摘要与检查读到的全部内容，多的不建。

    检查的答案由用例预设：``checkValueIsAcceptable`` 与 ``isSupportedOutputValue`` 只把问题
    记下来并回一个写死的判决，因为真正的判据在 QGIS 里，不在本模块。

    Everything a summary and the checks read from one parameter definition.  The
    verdicts are canned: ``checkValueIsAcceptable`` and ``isSupportedOutputValue``
    record the question and answer from the fixture, because the criterion lives in
    QGIS and not in this module.
    """

    def __init__(
        self,
        name: str,
        type_: str,
        *,
        description: str = "",
        help_: str = "",
        hidden: bool = False,
        destination: bool = False,
        optional: bool = False,
        create_by_default: bool = True,
        gui_default: Any = None,
        acceptable: bool = True,
        supported: bool = True,
        output_error: str = "the output value is not supported",
    ) -> None:
        self._name = name
        self._type = type_
        self._description = description
        self._help = help_
        self._hidden = hidden
        self._destination = destination
        self._optional = optional
        self._create_by_default = create_by_default
        self._gui_default = gui_default
        self._acceptable = acceptable
        self._supported = supported
        self._output_error = output_error
        self.checked: list[Any] = []
        self.supported_checks: list[Any] = []

    def name(self) -> str:
        return self._name

    def type(self) -> str:
        return self._type

    def description(self) -> str:
        return self._description

    def help(self) -> str:
        return self._help

    def flags(self) -> int:
        flags = _FLAG_HIDDEN if self._hidden else 0
        return flags | (_FLAG_OPTIONAL if self._optional else 0)

    def isDestination(self) -> bool:  # noqa: N802 - the native spelling
        return self._destination

    def checkValueIsAcceptable(self, value: Any) -> bool:  # noqa: N802 - the native spelling
        self.checked.append(value)
        return self._acceptable

    def defaultValueForGui(self) -> Any:  # noqa: N802 - the native spelling
        return self._gui_default

    def createByDefault(self) -> bool:  # noqa: N802 - the native spelling
        return self._create_by_default

    def isSupportedOutputValue(  # noqa: N802 - the native spelling
        self, value: Any, context: Any
    ) -> tuple[bool, str]:
        self.supported_checks.append((value, context))
        return self._supported, self._output_error


class FakeEnumDefinition(FakeDefinition):
    """唯一带可选项的定义族；基类没有 ``options()``，所以单列一个子类。

    The one definition family with display options; the base class has none.
    """

    def __init__(self, name: str, options: list[str], **values: Any) -> None:
        super().__init__(name, "enum", **values)
        self._options = list(options)

    def options(self) -> list[str]:
        return list(self._options)


class FakeParameterType:
    """``QgsProcessingParameterType`` 的替身：只暴露被读取的 accepted python types。

    A ``QgsProcessingParameterType``: only its accepted python types are read.
    """

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


class FakeProvider:
    """``QgsProcessingProvider`` 的替身：它显示给自己的名字，加运行前要报的那句告警。

    A ``QgsProcessingProvider``: the name it shows for itself and the warning a run has to
    report.
    """

    def __init__(self, name: str = "", warning: str = "") -> None:
        self._name = name
        self._warning = warning

    def name(self) -> str:
        return self._name

    def warningMessage(self) -> str:  # noqa: N802 - the native spelling
        return self._warning


class FakeAlgorithm:
    """一条已注册的算法定义；运行前要问的每个问题都在这里回一个预设的答案。

    One registered algorithm definition; every question asked before a run answers from a canned
    verdict here.
    """

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
        flags: int = 0,
        parameters_ok: bool = True,
        parameters_message: str = "",
        crs_ok: bool = True,
        provider_name: str = "",
        provider_warning: str = "",
    ) -> None:
        self.algorithm_id = algorithm_id
        self._name = name
        self._group = group
        self._short_description = short_description
        self._help = help_
        self._definitions = definitions
        self._outputs = outputs
        self._flags = flags
        self._parameters_ok = parameters_ok
        self._parameters_message = parameters_message
        self._crs_ok = crs_ok
        self.parameter_checks: list[tuple[dict[str, Any], Any]] = []
        self.crs_checks: list[tuple[dict[str, Any], Any]] = []
        self.preprocessed: list[dict[str, Any]] = []
        # ``preprocessParameters`` 交回的 map，在调用方的 map 之后合并。
        # What preprocessParameters hands back, merged after the caller's map.
        self.adds_on_preprocess: dict[str, Any] = {}
        self._provider = FakeProvider(provider_name, provider_warning)

    def id(self) -> str:
        return self.algorithm_id

    def displayName(self) -> str:  # noqa: N802 - the native spelling
        return self._name

    def group(self) -> str:
        return self._group

    def flags(self) -> int:
        return self._flags

    def shortDescription(self) -> str:  # noqa: N802 - the native spelling
        return self._short_description

    def shortHelpString(self) -> str:  # noqa: N802 - the native spelling
        return self._help

    def parameterDefinitions(self) -> list[Any]:  # noqa: N802 - the native spelling
        return list(self._definitions)

    def outputDefinitions(self) -> list[Any]:  # noqa: N802 - the native spelling
        return list(self._outputs)

    def provider(self) -> FakeProvider:
        return self._provider

    def preprocessParameters(  # noqa: N802 - the native spelling
        self, parameters: dict[str, Any]
    ) -> dict[str, Any]:
        self.preprocessed.append(dict(parameters))
        return {**parameters, **self.adds_on_preprocess}

    def checkParameterValues(  # noqa: N802 - the native spelling
        self, parameters: dict[str, Any], context: Any
    ) -> tuple[bool, str]:
        self.parameter_checks.append((parameters, context))
        return self._parameters_ok, self._parameters_message

    def validateInputCrs(  # noqa: N802 - the native spelling
        self, parameters: dict[str, Any], context: Any
    ) -> bool:
        self.crs_checks.append((parameters, context))
        return self._crs_ok


class FakeRegistry:
    """``QgsProcessingRegistry`` 的替身，只建被查找的算法。

    ``QgsProcessingRegistry``, creating only the algorithms it is asked for.
    """

    def __init__(self) -> None:
        self.algorithms_by_id: dict[str, FakeAlgorithm] = {}
        self.parameter_types: dict[str, FakeParameterType] = {}
        self.lookups: list[str] = []
        self.created: list[str] = []

    def algorithms(self) -> list[FakeAlgorithm]:
        return list(self.algorithms_by_id.values())

    def algorithmById(self, algorithm_id: str) -> FakeAlgorithm | None:  # noqa: N802
        self.lookups.append(algorithm_id)
        return self.algorithms_by_id.get(algorithm_id)

    def createAlgorithmById(  # noqa: N802 - the native spelling
        self, algorithm_id: str
    ) -> FakeAlgorithm | None:
        self.created.append(algorithm_id)
        return self.algorithms_by_id.get(algorithm_id)

    def parameterType(self, name: str) -> FakeParameterType | None:  # noqa: N802
        return self.parameter_types.get(name)


class Call(NamedTuple):
    """记录下的一次 ``qgis.processing.run()`` 调用。

    One recorded ``qgis.processing.run()`` call.
    """

    algorithm_id: str
    parameters: dict[str, Any]
    kwargs: dict[str, Any]

    @property
    def feedback(self) -> Any:
        """操作自建的反馈对象；它长什么样是运行自己的事。

        The feedback the operation built; its surface is the run's own business.
        """
        return self.kwargs["feedback"]

    @property
    def context(self) -> Any:
        """同一个调用里建出来的那个 processing 上下文。

        The processing context built in the same call.
        """
        return self.kwargs["context"]


class FakeContext:
    """``QgsProcessingContext`` 的替身：只记得是哪个反馈对象建了它。

    ``QgsProcessingContext``: only which feedback built it.
    """

    def __init__(self, feedback: Any) -> None:
        self.feedback = feedback


class FakeProcessing:
    """``qgis.processing``：每次运行记一条调用，回一个备好的答案。

    ``qgis.processing``: one recorded call per run, with a canned answer.
    """

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.results: Any = {}
        self.error: BaseException | None = None
        # ``(方法名, 参数)`` 重放到本次的反馈对象上。
        # Replayed on this call's feedback.
        self.messages: list[tuple[str, tuple[Any, ...]]] = []
        self.contexts: list[FakeContext] = []

    def createContext(self, feedback: Any) -> FakeContext:  # noqa: N802 - the native spelling
        context = FakeContext(feedback)
        self.contexts.append(context)
        return context

    def runAndLoadResults(  # noqa: N802 - the native spelling
        self, algorithm_id: str, parameters: dict[str, Any], **kwargs: Any
    ) -> Any:
        self.calls.append(Call(algorithm_id, parameters, kwargs))
        feedback = self.kwargs_feedback(kwargs)
        if feedback is not None:
            for name, arguments in self.messages:
                getattr(feedback, name)(*arguments)
        if self.error is not None:
            raise self.error
        return self.results

    def run(self, *arguments: Any, **kwargs: Any) -> Any:
        # 不走只运行、不加载结果的那条路：结果图层就是靠加载进工程的。
        # Never the run-without-loading path: loading the result layers is the point.
        raise AssertionError(f"processing.run must not be used: {arguments} {kwargs}")

    @staticmethod
    def kwargs_feedback(kwargs: dict[str, Any]) -> FakeFeedback | None:
        return kwargs.get("feedback")


class FakeQgis:
    """只装 processing 操作会 import 的那几样的 ``qgis`` 包。

    The ``qgis`` package holding only what the processing operations import.
    """

    def __init__(self) -> None:
        self.registry = FakeRegistry()
        self.processing = FakeProcessing()
        # 参数 CRS 不一致就先警告的开关，默认开着。
        # The "warn before executing if parameter CRSes do not match" switch, on by default.
        self.settings: dict[str, Any] = {
            _SETTINGS_WARN_UNMATCHING_CRS: True,
            _SETTINGS_SHOW_KNOWN_ISSUES: False,
        }

    def modules(self) -> dict[str, types.ModuleType]:
        holder = self

        class QgsApplication:
            @staticmethod
            def processingRegistry() -> FakeRegistry:
                return holder.registry

        class AlgorithmFlag:
            HideFromToolbox = _FLAG_HIDE_FROM_TOOLBOX
            KnownIssues = _FLAG_KNOWN_ISSUES

        class Qgis:
            ProcessingAlgorithmFlag = AlgorithmFlag

        class QgsProcessing:
            TEMPORARY_OUTPUT = "TEMPORARY_OUTPUT"

        class QgsSettings:
            def value(self, key: str, default: Any = None) -> Any:
                return holder.settings.get(key, default)

        core = fake_module(
            "qgis.core",
            Qgis=Qgis,
            QgsApplication=QgsApplication,
            QgsProcessing=QgsProcessing,
            QgsProcessingFeedback=FakeFeedback,
            QgsProcessingParameterDefinition=FakeParameterDefinition,
            QgsProcessingParameterEnum=FakeEnumDefinition,
            QgsSettings=QgsSettings,
        )
        processing = fake_module(
            "qgis.processing",
            run=self.processing.run,
            runAndLoadResults=self.processing.runAndLoadResults,
            createContext=self.processing.createContext,
        )
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


# --- 辅助函数 / helpers ------------------------------------------------------


def fake_module(name: str, **attributes: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    vars(module).update(attributes)
    return module


def make_context(project: Any) -> OperationContext:
    return OperationContext(iface=None, project=project, lifecycle=None, policy=Settings())


def install(
    fake: FakeQgis,
    *definitions: Any,
    algorithm_id: str = "native:test",
    name: str = "Algorithm",
    group: str = "Group",
    short_description: str = "",
    help_: str = "",
    outputs: tuple[Any, ...] = (),
    flags: int = 0,
    parameters_ok: bool = True,
    parameters_message: str = "",
    crs_ok: bool = True,
    provider_name: str = "",
    provider_warning: str = "",
) -> FakeAlgorithm:
    algorithm = FakeAlgorithm(
        algorithm_id,
        name=name,
        group=group,
        short_description=short_description,
        help_=help_,
        definitions=definitions,
        outputs=outputs,
        flags=flags,
        parameters_ok=parameters_ok,
        parameters_message=parameters_message,
        crs_ok=crs_ok,
        provider_name=provider_name,
        provider_warning=provider_warning,
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


def run_params(
    parameters: dict[str, Any],
    algorithm_id: str = "native:test",
    **overrides: Any,
) -> Any:
    return ops.ProcessingRunAlgorithmParams(
        algorithm_id=algorithm_id, parameters=parameters, **overrides
    )


def destination(name: str = "OUTPUT", **values: Any) -> FakeDefinition:
    """一个输出参数定义：默认必填、默认创建、没有预设的 GUI 默认值。

    One destination definition: required, created by default, with no GUI default value.
    """

    values.setdefault("description", "the output")
    return FakeDefinition(name, "sink", destination=True, **values)


def run(fake: FakeQgis, parameters: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    """一次成功的调用，运行要回的答案已经先放好。

    One successful call, with the run's answer already in place.
    """

    if "native:test" not in fake.registry.algorithms_by_id:
        install(fake)
    fake.processing.results = kwargs.pop("results", {})
    return ops.ProcessingRunAlgorithm().execute_main(
        make_context(FakeProject()),
        run_params({} if parameters is None else parameters),
    )


# --- 输入与输出模型 / input and output models ------------------------------


def test_the_list_parameters_accept_only_an_empty_object() -> None:
    assert ops.ProcessingListAlgorithmsParams.model_validate({}).model_dump() == {}
    with pytest.raises(ValidationError):
        ops.ProcessingListAlgorithmsParams.model_validate({"provider": "native"})


def test_an_undeclared_field_is_refused_on_every_model() -> None:
    with pytest.raises(ValidationError):
        ops.AlgorithmSummary(id="a", name="A", group="G", provider_id="native")
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
        ops.AlgorithmSummary(id=1, name="A", group="G", provider="P")
    with pytest.raises(ValidationError):
        ops.AlgorithmSummary(id="a", name=None, group="G", provider="P")
    assert ops.AlgorithmSummary(id="a", name="A", group="G", provider="P").model_dump() == {
        "id": "a",
        "name": "A",
        "group": "G",
        "provider": "P",
    }


def test_the_details_block_carries_the_id_name_short_description_and_help() -> None:
    details = ops.AlgorithmDetails(
        id="native:buffer", name="Buffer", short_description="Buffers", help="How to buffer"
    )
    assert list(details.model_dump()) == ["id", "name", "short_description", "help"]
    with pytest.raises(ValidationError):
        ops.AlgorithmDetails(id="a", name="A", short_description=None, help="")
    with pytest.raises(ValidationError):
        ops.AlgorithmDetails(id="a", name="A", short_description="", help=1)


def test_a_parameter_carries_its_six_fields_in_order() -> None:
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


def test_the_crs_answer_is_a_strict_boolean() -> None:
    params = ops.ProcessingRunAlgorithmParams(algorithm_id="a", parameters={})
    assert params.continue_on_crs_mismatch is False
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithmParams(
            algorithm_id="a", parameters={}, continue_on_crs_mismatch="true"
        )


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
    # 顶层是 pydantic 的 dict，不是递归的那个。
    # 所以键的规则也写进字段类型。
    # The top level is pydantic's dict, not the walk's.
    # So the key rule is part of the field type too.
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithmParams(algorithm_id="a", parameters={b"OFFSET": 1})

    class Colour(StrEnum):
        RED = "red"

    # ``str`` 子类仍是 ``str``，枚举键照常可用。
    # A str subclass works as an enum key.
    enum_keyed = ops.ProcessingRunAlgorithmParams(algorithm_id="a", parameters={Colour.RED: 1})
    assert enum_keyed.parameters == {"red": 1}


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


# --- processing.list_algorithms：列出算法 / listing the algorithms ----------


def test_the_list_follows_the_toolbox_tree_order(fake_qgis: FakeQgis) -> None:
    # 工具箱是 provider → 分组 → 算法三层，每层按名字排；三个键都故意与 id 的顺序相反。
    # The toolbox is provider, group, algorithm, each level ordered by name; every key is
    # deliberately the reverse of the id order.
    install(fake_qgis, algorithm_id="zzz:alpha", name="Zulu", group="Alpha", provider_name="Zed")
    install(
        fake_qgis,
        algorithm_id="native:beta",
        name="Bravo",
        group="Alpha",
        provider_name="QGIS (native c++)",
    )
    install(fake_qgis, algorithm_id="aaa:gamma", name="Alpha", group="Beta", provider_name="Aaa")

    result = ops.ProcessingListAlgorithms().execute_main(
        make_context(FakeProject()), ops.ProcessingListAlgorithmsParams()
    )
    assert [(s.provider, s.group, s.name, s.id) for s in result.algorithms] == [
        ("Aaa", "Beta", "Alpha", "aaa:gamma"),
        ("QGIS (native c++)", "Alpha", "Bravo", "native:beta"),
        ("Zed", "Alpha", "Zulu", "zzz:alpha"),
    ]


def test_two_providers_offering_the_same_names_stay_in_provider_order(fake_qgis: FakeQgis) -> None:
    # 真注册表里就有这种同名：同一个分组、同一个算法名同时出现在两个 provider 里。
    # The real registry has this: one group and one algorithm name in two providers.
    install(
        fake_qgis,
        algorithm_id="native:raster-calculator",
        name="Raster calculator",
        group="Raster analysis",
        provider_name="QGIS (native c++)",
    )
    install(
        fake_qgis,
        algorithm_id="qgis:raster-calculator",
        name="Raster calculator",
        group="Raster analysis",
        provider_name="QGIS",
    )

    result = ops.ProcessingListAlgorithms().execute_main(
        make_context(FakeProject()), ops.ProcessingListAlgorithmsParams()
    )
    assert [summary.provider for summary in result.algorithms] == ["QGIS", "QGIS (native c++)"]


def test_the_list_drops_hidden_and_known_issue_algorithms(fake_qgis: FakeQgis) -> None:
    install(fake_qgis, algorithm_id="native:hidden", flags=_FLAG_HIDE_FROM_TOOLBOX)
    install(fake_qgis, algorithm_id="native:known-issues", flags=_FLAG_KNOWN_ISSUES)
    install(fake_qgis, algorithm_id="native:plain")

    result = ops.ProcessingListAlgorithms().execute_main(
        make_context(FakeProject()), ops.ProcessingListAlgorithmsParams()
    )
    assert [summary.id for summary in result.algorithms] == ["native:plain"]


@pytest.mark.parametrize("stored", ["false", "0", 0], ids=["text", "zero", "int"])
def test_a_persisted_off_value_keeps_a_known_issue_hidden(fake_qgis: FakeQgis, stored: Any) -> None:
    fake_qgis.settings[_SETTINGS_SHOW_KNOWN_ISSUES] = stored
    install(fake_qgis, algorithm_id="native:known-issues", flags=_FLAG_KNOWN_ISSUES)

    result = ops.ProcessingListAlgorithms().execute_main(
        make_context(FakeProject()), ops.ProcessingListAlgorithmsParams()
    )
    assert result.algorithms == []


def test_a_known_issue_is_listed_once_the_setting_shows_it(fake_qgis: FakeQgis) -> None:
    fake_qgis.settings[_SETTINGS_SHOW_KNOWN_ISSUES] = True
    install(fake_qgis, algorithm_id="native:hidden", flags=_FLAG_HIDE_FROM_TOOLBOX)
    install(fake_qgis, algorithm_id="native:known-issues", flags=_FLAG_KNOWN_ISSUES)

    result = ops.ProcessingListAlgorithms().execute_main(
        make_context(FakeProject()), ops.ProcessingListAlgorithmsParams()
    )
    # HideFromToolbox 不受这个开关影响；KnownIssues 跟着它。
    # HideFromToolbox does not follow the switch; KnownIssues does.
    assert [summary.id for summary in result.algorithms] == ["native:known-issues"]


def test_an_empty_registry_lists_nothing(fake_qgis: FakeQgis) -> None:
    result = ops.ProcessingListAlgorithms().execute_main(
        make_context(FakeProject()), ops.ProcessingListAlgorithmsParams()
    )
    assert result.algorithms == []
    assert result.model_dump() == {"algorithms": []}


def test_the_list_result_carries_only_algorithms(fake_qgis: FakeQgis) -> None:
    install(
        fake_qgis,
        algorithm_id="native:buffer",
        name="Buffer",
        group="Vector geometry",
        provider_name="QGIS (native c++)",
    )
    result = ops.ProcessingListAlgorithms().execute_main(
        make_context(FakeProject()), ops.ProcessingListAlgorithmsParams()
    )
    assert result.model_dump() == {
        "algorithms": [
            {
                "id": "native:buffer",
                "name": "Buffer",
                "group": "Vector geometry",
                "provider": "QGIS (native c++)",
            }
        ]
    }


# --- processing.get_algorithm_info：一条算法的详情 / one algorithm's details -


def info(fake: FakeQgis, algorithm_id: str = "native:test") -> Any:
    return ops.ProcessingGetAlgorithmInfo().execute_main(
        make_context(FakeProject()),
        ops.ProcessingGetAlgorithmInfoParams(algorithm_id=algorithm_id),
    )


def test_info_carries_the_algorithm_its_parameters_and_its_outputs(fake_qgis: FakeQgis) -> None:
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
    # 非枚举的定义没有可选项。
    # 字段就用 ``None`` 说明这一点。
    # A non-enum definition has no options.
    # The field states that with None.
    assert second.model_dump() == {
        "name": "DISTANCE",
        "description": "",
        "help": "",
        "type": FakeDefinition.__name__,
        "options": None,
        "accepted_types": [],
    }


def test_info_reports_an_unknown_parameter_type_as_an_empty_list(fake_qgis: FakeQgis) -> None:
    """``registry.parameterType()`` 返回 ``None`` 时给空表，而不是 ``null``。

    ``registry.parameterType()`` returning None is an empty list, not null.
    """
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
    # 帮助路径自己的句子，与 ``algorithmHelp()`` 打印的那一句相同。
    # The help path's own sentence, the one ``algorithmHelp()`` prints.
    assert caught.value.message == 'Algorithm "native:missing" not found.'


# --- processing.run_algorithm：这次调用 / the call --------------------------


def test_the_run_hands_over_the_id_the_parameters_and_a_feedback(fake_qgis: FakeQgis) -> None:
    install(fake_qgis)
    fake_qgis.processing.results = {"OUTPUT": "done"}
    parameters = {"INPUT": "roads", "DISTANCE": 5, "OUTPUT": "memory:"}

    result = ops.ProcessingRunAlgorithm().execute_main(
        make_context(FakeProject()), run_params(parameters)
    )

    assert len(fake_qgis.processing.calls) == 1
    call = fake_qgis.processing.calls[0]
    assert (call.algorithm_id, call.parameters) == ("native:test", parameters)
    # 反馈与上下文一起交出去，上下文就是用它建的那个；没有 onFinish 或 is_child_algorithm。
    # The feedback and the context travel together, the context built from that feedback; no
    # onFinish or is_child_algorithm.
    assert sorted(call.kwargs) == ["context", "feedback"]
    assert isinstance(call.feedback, FakeFeedback)
    assert call.context.feedback is call.feedback
    assert fake_qgis.processing.contexts == [call.context]
    assert result.algorithm_id == "native:test"
    assert result.outputs == {"OUTPUT": "done"}
    assert result.messages == []


def test_the_run_resolves_the_id_through_the_registry_factory(fake_qgis: FakeQgis) -> None:
    """运行路径用 ``createAlgorithmById`` 拿实例；这里是同一个厂。

    The run path takes its instance from ``createAlgorithmById``; so does this.
    """
    install(fake_qgis)

    run(fake_qgis)

    assert fake_qgis.registry.created == ["native:test"]
    assert fake_qgis.registry.lookups == []


def test_an_unknown_algorithm_is_invalid_input_before_anything_is_built(
    fake_qgis: FakeQgis,
) -> None:
    with pytest.raises(FrameworkError) as caught:
        ops.ProcessingRunAlgorithm().execute_main(
            make_context(FakeProject()), run_params({}, algorithm_id="native:nope")
        )
    assert caught.value.code == INVALID_INPUT
    # 运行路径的句子，连结尾换行也一起带回来。
    # The run path's sentence, its trailing newline included.
    assert caught.value.message == "Error: Algorithm native:nope not found\n"
    assert fake_qgis.processing.contexts == []
    assert fake_qgis.processing.calls == []


# --- processing.run_algorithm：运行前问的问题 / the questions before a run -----


def test_a_missing_required_destination_gets_the_temporary_output(fake_qgis: FakeQgis) -> None:
    output = destination()
    install(fake_qgis, FakeDefinition("DISTANCE", "number"), output)

    run(fake_qgis, {"DISTANCE": 5})

    call = fake_qgis.processing.calls[0]
    assert call.parameters == {"DISTANCE": 5, "OUTPUT": "TEMPORARY_OUTPUT"}
    # 控件也会检查它自己的默认值，所以补上的值不是“跳过检查”。
    # The widget checks its own default too, so a filled value is not a skipped check.
    assert [value for value, _ in output.supported_checks] == ["TEMPORARY_OUTPUT"]


def test_a_destination_the_caller_named_is_kept(fake_qgis: FakeQgis) -> None:
    install(fake_qgis, destination())

    run(fake_qgis, {"OUTPUT": "memory:"})
    # ``None`` 是“跳过输出”：面板对跳过的输出不放键。
    # ``None`` is "skip output": the panel keeps no key for a skipped output.
    run(fake_qgis, {"OUTPUT": None})

    assert fake_qgis.processing.calls[0].parameters == {"OUTPUT": "memory:"}
    assert fake_qgis.processing.calls[1].parameters == {}


def test_an_optional_destination_that_is_off_by_default_stays_absent(fake_qgis: FakeQgis) -> None:
    install(fake_qgis, destination("REPORT", optional=True, create_by_default=False))

    run(fake_qgis, {})

    assert fake_qgis.processing.calls[0].parameters == {}


def test_an_optional_destination_that_is_on_by_default_gets_the_temporary_output(
    fake_qgis: FakeQgis,
) -> None:
    install(fake_qgis, destination("REPORT", optional=True, create_by_default=True))

    run(fake_qgis, {})

    assert fake_qgis.processing.calls[0].parameters == {"REPORT": "TEMPORARY_OUTPUT"}


def test_a_hidden_destination_is_never_filled(fake_qgis: FakeQgis) -> None:
    install(fake_qgis, destination(hidden=True))

    run(fake_qgis, {})

    assert fake_qgis.processing.calls[0].parameters == {}


def test_the_first_refusal_follows_the_definition_order(fake_qgis: FakeQgis) -> None:
    # 先声明的先说话：面板是一个循环，不是“先查完输入再查输出”。
    # The first declared parameter speaks first: the panel is one loop, not "all inputs, then all
    # outputs".
    output = destination(supported=False, output_error="the output is refused")
    source = FakeDefinition("INPUT", "source", description="Input layer", acceptable=False)
    install(fake_qgis, output, source)

    with pytest.raises(FrameworkError) as caught:
        run(fake_qgis, {"OUTPUT": "C:/out.gpkg"})

    assert caught.value.message == "the output is refused"


def test_a_later_refusal_waits_for_the_earlier_one(fake_qgis: FakeQgis) -> None:
    output = destination(supported=False, output_error="the output is refused")
    source = FakeDefinition("INPUT", "source", description="Input layer", acceptable=False)
    install(fake_qgis, source, output)

    with pytest.raises(FrameworkError) as caught:
        run(fake_qgis, {"OUTPUT": "C:/out.gpkg"})

    assert caught.value.message == "Wrong or missing parameter value: Input layer"
    # 第一句之后就停：后面的输出没有被问。
    # The loop stops at the first refusal: the output is never asked.
    assert output.supported_checks == []


def test_a_parameter_default_wins_over_the_temporary_output(fake_qgis: FakeQgis) -> None:
    install(fake_qgis, destination(gui_default="fixed.gpkg"))

    run(fake_qgis, {})

    assert fake_qgis.processing.calls[0].parameters == {"OUTPUT": "fixed.gpkg"}


def test_the_algorithm_preprocesses_the_parameters_after_the_value_checks(
    fake_qgis: FakeQgis,
) -> None:
    algorithm = install(fake_qgis, FakeDefinition("INPUT", "source"))
    algorithm.adds_on_preprocess = {"RESOLVED": 1}

    run(fake_qgis, {"INPUT": "roads"})

    assert algorithm.preprocessed == [{"INPUT": "roads"}]
    # 预处理之后的 map 才是 QGIS 剩下的问题与运行拿到的那张。
    # The preprocessed map is the one QGIS's remaining questions and the run see.
    assert algorithm.parameter_checks[0][0] == {"INPUT": "roads", "RESOLVED": 1}
    assert fake_qgis.processing.calls[0].parameters == {"INPUT": "roads", "RESOLVED": 1}


def test_a_refused_value_stops_the_call_before_preprocessing(fake_qgis: FakeQgis) -> None:
    algorithm = install(
        fake_qgis, FakeDefinition("DISTANCE", "number", description="Distance", acceptable=False)
    )

    with pytest.raises(FrameworkError):
        run(fake_qgis, {"DISTANCE": "abc"})

    assert algorithm.preprocessed == []
    assert fake_qgis.processing.calls == []


def test_the_panel_sentence_is_the_one_for_a_bad_parameter(fake_qgis: FakeQgis) -> None:
    bad = FakeDefinition("DISTANCE", "number", description="Distance", acceptable=False)
    install(fake_qgis, bad, destination())

    with pytest.raises(FrameworkError) as caught:
        run(fake_qgis, {"DISTANCE": "abc"})

    assert caught.value.code == INVALID_INPUT
    assert caught.value.message == "Wrong or missing parameter value: Distance"
    assert bad.checked == ["abc"]
    assert fake_qgis.processing.calls == []


def test_a_missing_parameter_is_checked_with_the_widgets_default(fake_qgis: FakeQgis) -> None:
    # 面板的控件一出生就带着默认值，提交时也把它放进 map；缺键的 enum 在 QGIS 自己那里不会
    # 回落到默认值，所以这里必须补。
    # The panel's widget starts with the default and submits it; QGIS itself does not fall back
    # to the default for an enum whose key is missing, so it has to be filled here.
    predicate = FakeDefinition("PREDICATE", "enum", description="Predicate", gui_default=[0])
    install(fake_qgis, FakeDefinition("INPUT", "source"), predicate)

    run(fake_qgis, {"INPUT": "roads"})

    assert predicate.checked == [[0]]
    assert fake_qgis.processing.calls[0].parameters == {"INPUT": "roads", "PREDICATE": [0]}


def test_a_parameter_the_caller_named_wins_over_the_default(fake_qgis: FakeQgis) -> None:
    predicate = FakeDefinition("PREDICATE", "enum", description="Predicate", gui_default=[0])
    install(fake_qgis, predicate)

    run(fake_qgis, {"PREDICATE": [2]})

    assert predicate.checked == [[2]]
    assert fake_qgis.processing.calls[0].parameters == {"PREDICATE": [2]}


def test_a_missing_parameter_without_a_default_is_checked_as_empty(fake_qgis: FakeQgis) -> None:
    bad = FakeDefinition("INPUT", "source", description="Input layer", acceptable=False)
    install(fake_qgis, bad)

    with pytest.raises(FrameworkError) as caught:
        run(fake_qgis, {})

    assert caught.value.message == "Wrong or missing parameter value: Input layer"
    assert bad.checked == [None]


def test_a_hidden_parameter_is_not_checked_by_the_panel(fake_qgis: FakeQgis) -> None:
    hidden = FakeDefinition("OPTIONS", "string", hidden=True, acceptable=False)
    install(fake_qgis, hidden)

    run(fake_qgis, {"OPTIONS": "x"})

    assert hidden.checked == []


def test_an_unsupported_output_value_is_invalid_input_with_the_qgis_sentence(
    fake_qgis: FakeQgis,
) -> None:
    sentence = "“.zzz” files are not supported as outputs for this algorithm"
    output = destination(supported=False, output_error=sentence)
    install(fake_qgis, output)

    with pytest.raises(FrameworkError) as caught:
        run(fake_qgis, {"OUTPUT": r"C:\out\x.zzz"})

    assert caught.value.code == INVALID_INPUT
    assert caught.value.message == sentence
    assert [value for value, _ in output.supported_checks] == [r"C:\out\x.zzz"]
    assert fake_qgis.processing.calls == []


def test_a_skipped_destination_is_not_extension_checked(fake_qgis: FakeQgis) -> None:
    output = destination()
    install(fake_qgis, output)

    run(fake_qgis, {"OUTPUT": None})

    assert output.supported_checks == []
    assert fake_qgis.processing.calls[0].parameters == {}


def test_a_crs_mismatch_is_refused_with_the_question(fake_qgis: FakeQgis) -> None:
    algorithm = install(fake_qgis, crs_ok=False)

    with pytest.raises(FrameworkError) as caught:
        run(fake_qgis, {})

    assert caught.value.code == INVALID_INPUT
    assert caught.value.message == (
        "Parameters do not all use the same CRS. This can cause unexpected results.\n"
        "Do you want to continue?"
    )
    assert len(algorithm.crs_checks) == 1
    assert algorithm.crs_checks[0][1] is fake_qgis.processing.contexts[0]
    assert fake_qgis.processing.calls == []


def test_the_crs_answer_lets_the_run_continue(fake_qgis: FakeQgis) -> None:
    install(fake_qgis, crs_ok=False)

    ops.ProcessingRunAlgorithm().execute_main(
        make_context(FakeProject()),
        run_params({}, continue_on_crs_mismatch=True),
    )

    assert len(fake_qgis.processing.calls) == 1


@pytest.mark.parametrize("stored", [False, "false", "0", 0], ids=["bool", "text", "zero", "int"])
def test_the_crs_question_is_skipped_for_every_persisted_off_value(
    fake_qgis: FakeQgis, stored: Any
) -> None:
    # QGIS 把持久化的值按 ``str(value).lower() == "true"`` 归一；``'false'``/``'0'`` 不是真。
    # QGIS normalises a persisted value with ``str(value).lower() == "true"``; ``'false'``
    # and ``'0'`` are not true.
    fake_qgis.settings[_SETTINGS_WARN_UNMATCHING_CRS] = stored
    algorithm = install(fake_qgis, crs_ok=False)

    run(fake_qgis)

    assert algorithm.crs_checks == []
    assert len(fake_qgis.processing.calls) == 1


def test_the_provider_warning_is_reported_before_the_run(fake_qgis: FakeQgis) -> None:
    install(fake_qgis, provider_warning="this provider is deprecated")

    result = run(fake_qgis)

    assert [(message.level, message.text) for message in result.messages] == [
        ("error", "this provider is deprecated")
    ]


def test_a_parameter_check_failure_is_invalid_input_with_the_qgis_sentence(
    fake_qgis: FakeQgis,
) -> None:
    sentence = "Could not load source layer for INPUT: no value specified for parameter"
    algorithm = install(fake_qgis, parameters_ok=False, parameters_message=sentence)

    with pytest.raises(FrameworkError) as caught:
        run(fake_qgis, {})

    assert caught.value.code == INVALID_INPUT
    assert caught.value.message == sentence
    assert algorithm.parameter_checks[0][0] == {}
    assert algorithm.parameter_checks[0][1] is fake_qgis.processing.contexts[0]
    assert fake_qgis.processing.calls == []


def test_the_parameters_reach_the_run_as_they_arrived(fake_qgis: FakeQgis) -> None:
    """路径、远程值、没声明的键都原样交出去，判断这些是 QGIS 的事。

    A path, a remote value and an unknown key are all QGIS's to judge now.
    """
    parameters = {
        "INPUT": "https://example.com/data.gpkg",
        "MASK": r"C:\outside\the\roots\mask.shp",
        "P5_NOT_A_PARAMETER": 1,
    }
    run(fake_qgis, parameters)
    assert fake_qgis.processing.calls[0].parameters == parameters


def test_an_empty_result_is_a_success(fake_qgis: FakeQgis) -> None:
    assert run(fake_qgis, results={}).outputs == {}


def test_a_result_that_is_not_a_mapping_is_refused(fake_qgis: FakeQgis) -> None:
    install(fake_qgis)
    fake_qgis.processing.results = ["not", "a", "mapping"]
    with pytest.raises(ValidationError):
        ops.ProcessingRunAlgorithm().execute_main(make_context(FakeProject()), run_params({}))


# --- processing.run_algorithm：反馈对象留下的记录 / the feedback records -----


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


def test_each_feedback_method_runs_the_base_class_before_recording(fake_qgis: FakeQgis) -> None:
    fake_qgis.processing.messages = [
        ("pushInfo", ("started",)),
        ("reportError", ("first failure", False)),
        ("pushWarning", ("careful",)),
        ("reportError", ("second failure", True)),
    ]
    run(fake_qgis)
    feedback = fake_qgis.processing.calls[0].feedback

    # 基类方法在对应记录写入前跑完。
    # The base method ran before its record was appended.
    assert feedback.base == [
        ("pushInfo", 0),
        ("reportError", 1),
        ("pushWarning", 2),
        ("reportError", 3),
    ]


# --- processing.run_algorithm：结果值 / the result values --------------------


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
    # 被拒的结果用契约描述，不用对象的 repr。
    # A refused result is described by its contract.
    assert caught.value.message == "an algorithm result cannot be expressed as JSON"


# --- processing.run_algorithm：失败的那一句 / the failure sentence ----------


class RunFailed(RuntimeError):
    """假 ``qgis.processing.runAndLoadResults()`` 抛出的异常。

    What a fake ``qgis.processing.runAndLoadResults()`` raises.
    """


def test_a_failed_run_is_reported_as_internal_error_with_qgis_sentence(
    fake_qgis: FakeQgis, caplog: pytest.LogCaptureFixture
) -> None:
    install(fake_qgis)
    native = "ERROR 1: PQconnectdb failed, connection to 127.0.0.1, port 1 refused"
    fake_qgis.processing.messages = [("pushInfo", (native,)), ("reportError", ("giving up", True))]
    fake_qgis.processing.error = RunFailed("the algorithm did not finish")

    with (
        caplog.at_level(logging.WARNING, logger=LOGGER_NAME),
        pytest.raises(FrameworkError) as caught,
    ):
        ops.ProcessingRunAlgorithm().execute_main(make_context(FakeProject()), run_params({}))

    # 码是插件的兑底码，消息是 QGIS 给用户的那一句。
    # The code is the plugin's fallback; the message is the sentence QGIS shows the user.
    assert caught.value.code == INTERNAL_ERROR
    assert caught.value.message == "the algorithm did not finish"
    # 原生消息和异常文本只进本地日志。
    # Native messages and text go to the local log only.
    assert native in caplog.text
    assert "RunFailed" in caplog.text
    assert "the algorithm did not finish" in caplog.text


def test_an_empty_exception_text_falls_back_to_the_type_name(fake_qgis: FakeQgis) -> None:
    install(fake_qgis)
    fake_qgis.processing.error = RunFailed()

    with pytest.raises(FrameworkError) as caught:
        run(fake_qgis)

    # 记录的消息不能是空串：QGIS 没给文本时用异常的类型名。
    # A record's message is never empty: with no text from QGIS the exception's type name is used.
    assert caught.value.code == INTERNAL_ERROR
    assert caught.value.message == "RunFailed"


def test_a_failed_run_reports_the_call_shape_and_no_result(fake_qgis: FakeQgis) -> None:
    install(fake_qgis)
    fake_qgis.processing.messages = [("reportError", ("broken", False))]
    fake_qgis.processing.error = RunFailed("no result")
    with pytest.raises(FrameworkError):
        ops.ProcessingRunAlgorithm().execute_main(make_context(FakeProject()), run_params({}))
    # 记录由引擎写，失败时不返回；交给 QGIS 的仍是那几样。
    # The engine builds the record and a failure returns nothing; what QGIS got is unchanged.
    assert fake_qgis.processing.calls[0].kwargs.keys() == {"feedback", "context"}


# --- 声明、类的摆放与导入规则 / the declarations, the class placement, the import rule ---


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


def test_the_feedback_class_is_built_lazily_and_the_message_is_module_level() -> None:
    """哪些名字出现在模块里就是导入规则：QGIS 的子类只能由函数造出来。

    Which names the module exports is the import rule: a QGIS subclass may only be built by a
    function, so the module names the builder rather than the subclass.
    """
    assert "ProcessingMessage" in vars(ops)
    assert callable(ops._new_feedback)
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
