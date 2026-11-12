"""三个 ``processing.*`` 操作。

三者都跑在主线程：QGIS 的 processing 框架直接操作工程和它持有的图层，一次运行会把结果图层
交给那个工程，所以它挪不到后台线程。

这里没有算法白名单，也没有 ``runnable`` 字段：``list_algorithms`` 报告注册表里的每个算法，
``get_algorithm_info`` 读算法自己的原生定义（``algorithmHelp()`` 打印的那些字段，转成 JSON），
``run_algorithm`` 是所有算法共同的唯一入口。

``run_algorithm`` 不重新实现 QGIS。它只把调用交给 ``qgis.processing.run()``，外加一个会留住
运行消息的 feedback，于是算法是否存在、参数值是否可接受、processing 上下文怎么建、算法怎么
执行，全部由 QGIS 决定。没声明的参数键由 QGIS 忽略，路径或远程 URI 由 QGIS 解析，本模块没
归类的参数原样到达算法。失败就是 ``qgis.processing.run()`` 抛出的异常：feedback 记录和异常
进本地日志，异常原样抛出，于是引擎按它报告意外失败的唯一方式报告它；这里不把异常文本翻成
插件的错误码。

本模块仍然守着的边界是 JSON。``parameters`` 是一棵有限的 JSON 树，键为原生参数名：非字符串键、
NaN、无穷、bytes 或 QGIS/Qt 对象在 QGIS 看到之前就被拒；没有无损 JSON 形式的运行结果同样被拒，
而不是 ``str()`` 一下。唯一被翻译的值是结果图层——``qgis.processing.run()`` 返回真实的
``QgsMapLayer`` 对象，不属任何工程，所以每个都加进引擎的工程并按 id 报告；结果无法返回时，
这次调用加进去的图层会被移除，但不声称算法的其它副作用被撤销。

QGIS 在使用它的方法内部 import，所以本模块在裸解释器里也能加载，契约不依赖运行中的 QGIS。

All three operations run on the main thread: QGIS's processing framework works on
the project and the layers it holds, and a run hands result layers to that project,
so it cannot move to a worker.

There is no algorithm allow-list and no ``runnable`` field: ``list_algorithms``
reports every registered algorithm, ``get_algorithm_info`` reads the native
definitions -- the fields ``algorithmHelp()`` prints, as JSON -- and
``run_algorithm`` is the one entry point for all of them.

``run_algorithm`` does not re-implement QGIS.  It hands the call to
``qgis.processing.run()`` with nothing but a feedback that keeps the run's messages,
so whether the algorithm exists, whether its parameter values are acceptable, how
the processing context is built and how the algorithm executes are all decided by
QGIS.  An undeclared parameter key is QGIS's to ignore, a path or a remote URI is
QGIS's to resolve, and a parameter this module never classifies reaches the
algorithm exactly as it arrived.  A failure is an exception from
``qgis.processing.run()``: the feedback records and the exception go to the local
log and the exception is raised unchanged, so the engine reports it the one way it
reports an unexpected failure.  Nothing here classifies an exception text into a
plugin error code.

The boundary this module still owns is the JSON one.  ``parameters`` is a finite
JSON tree keyed by native parameter names; a non-string key, a NaN, an infinity,
bytes or a QGIS/Qt object is refused before QGIS sees it, and a result that has no
lossless JSON form is refused rather than ``str()``-ed.  A result layer is the one
value translated: ``qgis.processing.run()`` hands back real ``QgsMapLayer`` objects
that belong to no project, so each is added to the engine's project and reported by
its id; the layers this call added are removed again when its result cannot be
returned, and nothing claims the algorithm's own side effects were undone.

QGIS is imported inside the methods that use it, so this module loads in a bare
interpreter and its contracts stay testable without a running QGIS.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator

from ..core.contracts import (
    BaseCommand,
    BaseQuery,
    MainThreadOperationMixin,
    OperationContext,
)
from ..core.models import INTERNAL_ERROR, INVALID_INPUT, FrameworkError, Params, Result

_LOGGER = logging.getLogger(__name__)

# 没能产出的 JSON 映像 / A JSON image that could not be produced,
# 用来和合法的 ``None`` 区分开 / kept apart from a legitimate ``None``.
_NO_JSON = object()


def _json_image(value: Any) -> Any:
    """``value`` 的 JSON 形式；没有无损形式时返回 ``_NO_JSON``。

    只有精确的 JSON 类型能通过。``bool``、``int``、``float`` 故意按精确类型比较：某些构建里
    Qt 枚举是 ``int`` 的子类，而任意数字子类本身也不是 JSON。容器被复制成普通的 list 与 dict，
    于是 tuple 或 mapping 子类照样能出来，其它类型的对象则根本没有映像。

    Only the exact JSON types pass.  ``bool``, ``int`` and ``float`` are compared by
    exact type on purpose: a Qt enum is an ``int`` subclass in some builds and an
    arbitrary number subclass is not JSON either.  Containers are copied into plain
    lists and dicts, so a tuple or a mapping subclass still comes out as JSON while
    an object of any other kind has no image at all.
    """

    kind = type(value)
    if value is None or kind is bool or kind is int or kind is str:
        return value
    if kind is float:
        return value if math.isfinite(value) else _NO_JSON
    if isinstance(value, (list, tuple)):
        items = [_json_image(item) for item in value]
        return _NO_JSON if any(item is _NO_JSON for item in items) else items
    if isinstance(value, dict):
        image: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                return _NO_JSON
            child = _json_image(item)
            if child is _NO_JSON:
                return _NO_JSON
            image[key] = child
        return image
    return _NO_JSON


def _require_json(value: Any) -> Any:
    """``value`` 作为 JSON 树；不是的话抛 ``ValueError`` 交给模型去报。

    ``value`` as a JSON tree, or a ``ValueError`` for the model to report.
    """

    try:
        image = _json_image(value)
    except RecursionError:
        # 环状或过深的树是调用方的错 / a cyclic or absurdly deep tree is the caller's mistake,
        # 不是插件的错 / not a plugin one.
        # 不接住它就会以 INTERNAL_ERROR 逃出去 / left alone it escapes as an INTERNAL_ERROR,
        # 而不是 INVALID_INPUT / instead of INVALID_INPUT.
        image = _NO_JSON
    if image is _NO_JSON:
        raise ValueError("only finite JSON values are allowed")
    return image


class AlgorithmSummary(BaseModel):
    """一个已注册算法，说成调用方能够据此行动的样子。

    One registered algorithm, in the terms the caller can act on.
    """

    model_config = ConfigDict(extra="forbid")

    id: StrictStr
    name: StrictStr
    group: StrictStr


class AlgorithmDetails(BaseModel):
    """算法本身，照它自己的定义陈述的样子。

    The algorithm itself, as its own definition states it.
    """

    model_config = ConfigDict(extra="forbid")

    id: StrictStr
    name: StrictStr
    short_description: StrictStr
    help: StrictStr


class AlgorithmParameter(BaseModel):
    """一个可见的原生参数定义。

    ``options`` 只是枚举的展示列表；基类定义根本没有 ``options()``，所以用 ``None`` 陈述这个
    事实，而不是编一个空列表。``accepted_types`` 是参数类型对象说可以传进去的东西，注册表里没有
    它的类型对象时为空列表。

    ``options`` is only an enum's display list; the base definition has no
    ``options()`` at all, so ``None`` states that instead of a fabricated empty list.
    ``accepted_types`` is what the parameter type object says can be passed in, and
    an empty list when the registry has no type object for it.
    """

    model_config = ConfigDict(extra="forbid")

    name: StrictStr
    description: StrictStr
    help: StrictStr
    type: StrictStr
    options: list[StrictStr] | None
    accepted_types: list[StrictStr]


class AlgorithmOutput(BaseModel):
    """一个声明的输出；不是某次运行产出的值。

    One declared output; not a value a run produced.
    """

    model_config = ConfigDict(extra="forbid")

    name: StrictStr
    type: StrictStr
    description: StrictStr


class ProcessingMessage(BaseModel):
    """一次运行留下的一条消息，级别就是接收它的那个 feedback 方法。

    One message a run kept, at the level of the feedback method that took it.
    """

    model_config = ConfigDict(extra="forbid")

    # 级别取 feedback 的方法名 / the level is a feedback method's name:
    # info/debug/command/console/formatted/warning/error / 这七个方法，不多不少。
    level: StrictStr
    text: StrictStr
    fatal: StrictBool = False


class ProcessingListAlgorithmsParams(Params):
    """故意为空：当前注册表就是唯一的输入。

    Empty on purpose: the current registry is the only input.
    """


class ProcessingListAlgorithmsResult(Result):
    algorithms: list[AlgorithmSummary]


class ProcessingGetAlgorithmInfoParams(Params):
    algorithm_id: StrictStr = Field(min_length=1)


class ProcessingGetAlgorithmInfoResult(Result):
    algorithm: AlgorithmDetails
    parameters: list[AlgorithmParameter]
    outputs: list[AlgorithmOutput]


class ProcessingRunAlgorithmParams(Params):
    """一个原生算法 id 和它的原生参数表。

    A native algorithm id and its native parameter map.
    """

    algorithm_id: StrictStr = Field(min_length=1)
    # 键类型必须严格：顶层是 pydantic 的 dict / strict keys: the top level is pydantic's
    # dict, 所以这是「不许非字符串键」唯一能到的位置 / so this is the only place the
    # no-non-string-key rule can reach; 其余交给那趟遍历 / the walk checks the rest.
    parameters: dict[StrictStr, Any]

    @field_validator("parameters")
    @classmethod
    def _parameters_are_json(cls, value: dict[StrictStr, Any]) -> dict[StrictStr, Any]:
        return _require_json(value)


class ProcessingRunAlgorithmResult(Result):
    """跑的是哪个 id、每个输出在它的原生键下、以及运行留下的消息。

    The id that ran, every output under its native key, and the run's messages.
    """

    algorithm_id: StrictStr
    outputs: dict[str, Any]
    messages: list[ProcessingMessage]

    @field_validator("outputs")
    @classmethod
    def _outputs_are_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _require_json(value)


def algorithm_summary(algorithm: Any) -> AlgorithmSummary:
    """列表用的唯一摘要形状：注册表对这个算法的说法。

    The list's one summary shape: what the registry says about an algorithm.
    """

    return AlgorithmSummary(
        id=algorithm.id(),
        name=algorithm.displayName(),
        group=algorithm.group(),
    )


def _parameter_info(definition: Any, registry: Any) -> AlgorithmParameter:
    """一个可见定义折成六个字段，措辞保持原生。

    One visible definition as the six fields, in its native wording.
    """

    from qgis.core import QgsProcessingParameterEnum

    parameter_type = registry.parameterType(definition.type())
    return AlgorithmParameter(
        name=definition.name(),
        description=definition.description() or "",
        help=definition.help() or "",
        type=definition.__class__.__name__,
        options=list(definition.options())
        if isinstance(definition, QgsProcessingParameterEnum)
        else None,
        accepted_types=(
            [] if parameter_type is None else list(parameter_type.acceptedPythonTypes())
        ),
    )


def _json_result(project: Any, value: Any, added: set[str]) -> Any:
    """一个结果值转成 JSON，顺便把每个结果图层加进 ``project``。

    ``qgis.processing.run()`` 交出的图层带着所有权但不属任何工程，所以每个只加一次并按 id
    报告；这次调用已经加过的图层解析成同一个 id。其余的值要么是 JSON，要么是失败：没有无损映像
    的值被拒，绝不用 ``str()`` 描述一下了事。

    ``qgis.processing.run()`` hands the layers it produced over with their ownership
    and no project, so each one is added once and reported by its id; a layer this
    call already added resolves to the same id.  Anything else is JSON or a failure:
    a value with no lossless image is refused, never described by ``str()``.
    """

    from qgis.core import QgsMapLayer

    if isinstance(value, QgsMapLayer):
        layer_id = value.id()
        if layer_id not in added:
            if project.addMapLayer(value) is None:
                raise FrameworkError(INTERNAL_ERROR, "a result layer was not added to the project")
            added.add(layer_id)
        return layer_id
    if isinstance(value, (list, tuple)):
        return [_json_result(project, item, added) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise FrameworkError(INTERNAL_ERROR, "an algorithm result key is not a string")
            result[key] = _json_result(project, item, added)
        return result
    image = _json_image(value)
    if image is _NO_JSON:
        raise FrameworkError(INTERNAL_ERROR, "an algorithm result cannot be expressed as JSON")
    return image


class ProcessingListAlgorithms(
    BaseQuery[ProcessingListAlgorithmsParams, ProcessingListAlgorithmsResult],
    MainThreadOperationMixin[ProcessingListAlgorithmsParams, ProcessingListAlgorithmsResult],
):
    """每个已注册算法，不筛选，按 id 排序。

    Every registered algorithm, unfiltered and in id order.
    """

    name = "processing.list_algorithms"
    description = "List every algorithm currently registered with QGIS Processing"
    input_model = ProcessingListAlgorithmsParams
    output_model = ProcessingListAlgorithmsResult

    def execute_main(
        self, ctx: OperationContext, request: ProcessingListAlgorithmsParams
    ) -> ProcessingListAlgorithmsResult:
        from qgis.core import QgsApplication

        algorithms = [
            algorithm_summary(algorithm)
            for algorithm in QgsApplication.processingRegistry().algorithms()
        ]
        algorithms.sort(key=lambda summary: summary.id)
        return ProcessingListAlgorithmsResult(algorithms=algorithms)


class ProcessingGetAlgorithmInfo(
    BaseQuery[ProcessingGetAlgorithmInfoParams, ProcessingGetAlgorithmInfoResult],
    MainThreadOperationMixin[ProcessingGetAlgorithmInfoParams, ProcessingGetAlgorithmInfoResult],
):
    """一个算法的原生定义，按 QGIS 声明它的顺序。

    The native definition of one algorithm, in the order QGIS declares it.
    """

    name = "processing.get_algorithm_info"
    description = "Read one registered algorithm's native parameters and outputs"
    input_model = ProcessingGetAlgorithmInfoParams
    output_model = ProcessingGetAlgorithmInfoResult

    def execute_main(
        self, ctx: OperationContext, request: ProcessingGetAlgorithmInfoParams
    ) -> ProcessingGetAlgorithmInfoResult:
        from qgis.core import QgsApplication, QgsProcessingParameterDefinition

        registry = QgsApplication.processingRegistry()
        algorithm = registry.algorithmById(request.algorithm_id)
        if algorithm is None:
            # 合法调用说出未知算法是调用方的输入错误 / a valid call naming an unknown algorithm
            # is a caller input mistake / 不是未知的插件操作 / not an unknown plugin operation.
            raise FrameworkError(INVALID_INPUT, "the algorithm is not registered")

        # ``algorithmHelp()`` 跳过隐藏参数 / skips a hidden parameter,
        # 这里也一样 / and so does this.
        hidden = QgsProcessingParameterDefinition.FlagHidden
        return ProcessingGetAlgorithmInfoResult(
            algorithm=AlgorithmDetails(
                id=algorithm.id(),
                name=algorithm.displayName(),
                short_description=algorithm.shortDescription() or "",
                help=algorithm.shortHelpString() or "",
            ),
            parameters=[
                _parameter_info(definition, registry)
                for definition in algorithm.parameterDefinitions()
                if not definition.flags() & hidden
            ],
            outputs=[
                AlgorithmOutput(
                    name=output.name(),
                    type=output.__class__.__name__,
                    description=output.description() or "",
                )
                for output in algorithm.outputDefinitions()
            ],
        )


class ProcessingRunAlgorithm(
    BaseCommand[ProcessingRunAlgorithmParams, ProcessingRunAlgorithmResult],
    MainThreadOperationMixin[ProcessingRunAlgorithmParams, ProcessingRunAlgorithmResult],
):
    """通过 QGIS 跑一个已注册算法，并留下它报告的东西。

    Run one registered algorithm through QGIS, and keep what it reported.
    """

    name = "processing.run_algorithm"
    description = "Run any algorithm currently registered with QGIS Processing"
    input_model = ProcessingRunAlgorithmParams
    output_model = ProcessingRunAlgorithmResult
    # 名字代表的是一组算法 / the name stands for a group of algorithms
    # 它们可能改动数据或工程 / that may change data or the project,
    # 所以风险标签要整个名字一起定 / so it cannot depend on the particular call.
    destructive = True

    def execute_main(
        self, ctx: OperationContext, request: ProcessingRunAlgorithmParams
    ) -> ProcessingRunAlgorithmResult:
        from qgis import processing
        from qgis.core import QgsProcessingFeedback

        class ProcessingRunFeedback(QgsProcessingFeedback):
            """基类自己的日志，外加每条消息一条记录。

            The base class's own logging, plus one record per message.
            """

            def __init__(self) -> None:
                super().__init__()
                self.records: list[ProcessingMessage] = []

            def _record(self, level: str, text: str, fatal: bool = False) -> None:
                self.records.append(ProcessingMessage(level=level, text=text, fatal=fatal))

            def pushInfo(self, info: str) -> None:
                super().pushInfo(info)
                self._record("info", info)

            def pushDebugInfo(self, info: str) -> None:
                super().pushDebugInfo(info)
                self._record("debug", info)

            def pushCommandInfo(self, info: str) -> None:
                super().pushCommandInfo(info)
                self._record("command", info)

            def pushConsoleInfo(self, info: str) -> None:
                super().pushConsoleInfo(info)
                self._record("console", info)

            def pushFormattedMessage(self, html: str, text: str) -> None:
                super().pushFormattedMessage(html, text)
                self._record("formatted", text)

            def pushWarning(self, warning: str) -> None:
                super().pushWarning(warning)
                self._record("warning", warning)

            def reportError(self, message: str, fatalError: bool = False) -> None:
                super().reportError(message, fatalError)
                self._record("error", message, bool(fatalError))

            @property
            def errors(self) -> list[str]:
                return [record.text for record in self.records if record.level == "error"]

            @property
            def has_errors(self) -> bool:
                return any(record.level == "error" for record in self.records)

        feedback = ProcessingRunFeedback()
        try:
            # 只给 feedback / only the feedback:
            # 上下文和参数校验归 QGIS / QGIS creates the context and checks the parameters,
            # 执行也归 QGIS / and runs the algorithm itself.
            results = processing.run(request.algorithm_id, request.parameters, feedback=feedback)
        except Exception:
            # 原生消息和异常可能带上路径或主机名 / they may quote a path or a host,
            # 所以只进本地日志不进记录 / so they reach the local log and not the record;
            # 调用方拿到引擎对意外失败的判定 / the caller gets the engine's failure verdict.
            for record in feedback.records:
                _LOGGER.warning(
                    "processing %s reported %s: %s", request.algorithm_id, record.level, record.text
                )
            _LOGGER.exception("processing.run(%s) raised", request.algorithm_id)
            raise

        added: set[str] = set()
        try:
            return ProcessingRunAlgorithmResult(
                algorithm_id=request.algorithm_id,
                # ``None`` 是 QGIS 的「没有结果」 / ``None`` is QGIS's "no result",
                # 在这里当成一个空结果 / which is an empty one here.
                outputs=_json_result(ctx.project, {} if results is None else results, added),
                messages=feedback.records,
            )
        except Exception:  # noqa: BLE001
            # 只撤掉这次调用加进去的图层 / only what this call added is undone,
            # 算法的外部副作用不声称回滚 / not the algorithm's own external side effects.
            for layer_id in added:
                ctx.project.removeMapLayer(layer_id)
            raise


# 本模块唯一的元组 / the module's one tuple:
# ``operations/__init__.py`` import 它 / ``operations/__init__.py`` imports it,
# 不许再写第二份 / and must not define a second copy.
PROCESSING_OPERATIONS = (
    ProcessingListAlgorithms,
    ProcessingGetAlgorithmInfo,
    ProcessingRunAlgorithm,
)
