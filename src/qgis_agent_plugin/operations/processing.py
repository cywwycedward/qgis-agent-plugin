"""三个 ``processing.*`` 操作。

三者都跑在主线程：QGIS 的 processing 框架直接操作工程和它持有的图层，一次运行会把结果图层
交给那个工程，所以它挪不到后台线程。

这里没有算法白名单，也没有 ``runnable`` 字段：``list_algorithms`` 报告桌面版工具箱里能看到的
每个算法（``HideFromToolbox`` 与 ``KnownIssues`` 两个标志把算法挡在工具箱外，也就挡在列表外），
按分组名、算法名摊平排序——工具箱的 provider/分组树序不复刻，顺序不是判据；
``get_algorithm_info`` 读算法自己的原生定义（``algorithmHelp()`` 打印的那些字段，转成 JSON），
``run_algorithm`` 是所有算法共同的唯一入口。

``run_algorithm`` 不重新实现 QGIS，但是它点“运行”之前问的问题与桌面版窗口完全一样，而且每个
问题都问 QGIS 自己的函数：按 id 取出算法实例，补上桌面版参数面板会交出的输出默认值，逐个参数问
``checkValueIsAcceptable``，对每个有值的输出问 ``isSupportedOutputValue``，问那个 CRS 不一致的
确认，最后问 ``checkParameterValues``。任一条不通过就是 ``INVALID_INPUT``，消息就是桌面版对同一个
问题说的那一句。通过之后才把参数、一个会留住消息的 feedback 与同一个 processing 上下文交给
``qgis.processing.run()``，算法怎么执行仍由 QGIS 决定。没声明的参数键由 QGIS 忽略，路径或远程
URI 也由 QGIS 解析。运行失败仍是 ``qgis.processing.run()`` 抛出的异常：feedback 记录和异常进
本地日志，异常原样抛出，于是引擎按它报告意外失败的唯一方式报告它；这里不把异常文本翻成插件的
错误码。

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
reports every algorithm the desktop toolbox shows -- the ``HideFromToolbox`` and
``KnownIssues`` flags keep an algorithm out of the toolbox, and out of the list --
flattened by group name and algorithm name; the toolbox's provider/group tree order is
not reproduced, and order is not a criterion.  ``get_algorithm_info`` reads the native
definitions -- the fields ``algorithmHelp()`` prints, as JSON -- and ``run_algorithm`` is
the one entry point for all of them.

``run_algorithm`` does not re-implement QGIS, but it asks exactly the questions the
desktop window asks before Run, and every question goes through QGIS's own
function: resolve the algorithm by id, fill in the output defaults the desktop
panel would carry, ask every parameter's ``checkValueIsAcceptable``, ask
``isSupportedOutputValue`` for every output that has a value, ask the CRS-mismatch
confirmation, and finally ask ``checkParameterValues``.  Any refusal is
``INVALID_INPUT`` carrying the sentence the desktop uses for that same question.
Only then do the parameters, a feedback that keeps the run's messages and the same
processing context reach ``qgis.processing.run()``, and how the algorithm executes
stays QGIS's decision.  An undeclared parameter key is QGIS's to ignore, a path or
a remote URI is QGIS's to resolve.  A failed run is still an exception from
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

from pydantic import (  # pyright: ignore[reportMissingImports]
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    field_validator,
)

from ..core.contracts import (
    BaseCommand,
    BaseQuery,
    MainThreadOperationMixin,
    OperationContext,
)
from ..core.models import INTERNAL_ERROR, INVALID_INPUT, FrameworkError, Params, Result

_LOGGER = logging.getLogger(__name__)

# 没能产出的 JSON 映像，用来和合法的 ``None`` 区分开。
# A JSON image that could not be produced, kept apart from a legitimate ``None``.
_NO_JSON = object()

# 桌面版参数面板对不合法值说的那句话，参数名换成它的描述。
# The sentence the desktop parameter panel uses for a bad value, with its description filled in.
_BAD_PARAMETER = "Wrong or missing parameter value: {0}"

# 桌面版在参数 CRS 不一致、用户还没回答时问的那一句；默认按钮是“否”。
# What the desktop asks when the parameters' CRSes differ and nobody has answered yet; the
# default button is "No".
_CRS_MISMATCH_QUESTION = (
    "Parameters do not all use the same CRS. This can cause unexpected results.\n"
    "Do you want to continue?"
)


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
        # 环状或过深的树是调用方的错，不是插件的错：不接住它就会以 INTERNAL_ERROR 逃出去，
        # 而不是 INVALID_INPUT。
        # A cyclic or absurdly deep tree is the caller's mistake, not a plugin one:
        # left alone it escapes as an INTERNAL_ERROR instead of INVALID_INPUT.
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

    # 级别取 feedback 的方法名：info/debug/command/console/formatted/warning/error，七个方法，
    # 不多不少。
    # The level is one of the feedback's seven methods:
    # info/debug/command/console/formatted/warning/error.
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
    """一个原生算法 id、它的原生参数表，以及桌面版会问的那个 CRS 问题。

    A native algorithm id, its native parameter map, and the CRS question the desktop asks.
    """

    algorithm_id: StrictStr = Field(min_length=1)
    # 键类型必须严格：顶层是 pydantic 的 dict，所以这是「不许非字符串键」唯一能到的位置，
    # 其余交给那趟遍历。
    # Strict keys: the top level is pydantic's dict, so this is the only place the
    # no-non-string-key rule can reach; the walk checks the rest.
    parameters: dict[StrictStr, Any]
    # 桌面版发现参数 CRS 不一致时会问“要继续吗”，默认按钮是“否”；这个字段就是那个回答。
    # The desktop asks "do you want to continue?" when the parameters' CRSes differ, with "No"
    # as the default button; this field is that answer.
    continue_on_crs_mismatch: StrictBool = False

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

    from qgis.core import QgsProcessingParameterEnum  # pyright: ignore[reportMissingImports]

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

    from qgis.core import QgsMapLayer  # pyright: ignore[reportMissingImports]

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


def _help_unknown_algorithm(algorithm_id: str) -> FrameworkError:
    """帮助路径对未知 id 的说法，与 ``algorithmHelp()`` 打印的那一句相同。

    What the help path says for an unknown id, the sentence ``algorithmHelp()`` prints.
    """

    return FrameworkError(INVALID_INPUT, f'Algorithm "{algorithm_id}" not found.')


def _run_unknown_algorithm(algorithm_id: str) -> FrameworkError:
    """运行路径对未知 id 的说法，与 ``processing.run`` 报出并抛出的那一句相同（含结尾换行）。

    What the run path says for an unknown id, the sentence ``processing.run`` reports and
    raises (including its trailing newline).
    """

    return FrameworkError(INVALID_INPUT, f"Error: Algorithm {algorithm_id} not found\n")


def _desktop_switch(key: str, default: bool) -> bool:
    """桌面版读布尔配置的方式：只有 ``true`` 才算开。

    ``ProcessingConfig`` 把持久化的值按 ``str(value).lower() == "true"`` 归一（缺键时保留默认），
    所以 ``'false'`` 或 ``'0'`` 不是真；直接 ``bool()`` 会把它们当成真。

    How the desktop reads a boolean setting: only ``true`` is on.  ``ProcessingConfig``
    normalises the persisted value with ``str(value).lower() == "true"`` (keeping the
    default when the key is absent), so ``'false'`` or ``'0'`` is not true; a plain
    ``bool()`` would treat them as true.
    """

    from qgis.core import QgsSettings  # pyright: ignore[reportMissingImports]

    raw = QgsSettings().value(key, default)
    if raw is None:
        # 显式存成 null 时桌面版保留默认值。
        # An explicitly null value keeps the desktop's default.
        return default
    return str(raw).lower() == "true"


def _gui_parameter_values(algorithm: Any, parameters: dict[str, Any]) -> dict[str, Any]:
    """桌面版参数面板会交出的那张表：缺席的参数补上控件自己的默认值。

    面板给每个可见参数建控件，控件一出生就带着 ``defaultValueForGui()``（没有 GUI 设置时就是
    参数自己的默认值）；提交时它把每个控件的值都放进 map，包括那些没被碰过的。所以这里对缺席的
    非输出参数也放同一个值——QGIS 自己的 ``checkParameterValues`` 对缺键的 enum 并不回落到默认
    值，只靠“QGIS 会解析缺席”是不够的。没有默认值的保持缺席（面板那边是空控件，两边都会拒）。
    输出参数照旧：没有默认值就是临时输出，可选且不默认创建的保持缺席（面板的“跳过输出”）。
    隐藏参数面板从不发送，这里也不补。补进来的默认值可能是 QGIS 对象（控件本来就是），它们只往
    QGIS 走，不会出现在结果里。

    The map the desktop panel would hand over: missing parameters get the widget's own
    default.  The panel creates a widget per visible parameter, the widget starts at
    ``defaultValueForGui()`` (the parameter's own default when no GUI setting overrides
    it), and the panel submits every widget's value, touched or not.  So a missing
    non-destination parameter is filled with that same value here -- QGIS's own
    ``checkParameterValues`` does not fall back to the default for an enum whose key is
    missing, so relying on "QGIS resolves a missing value" is only half the story.  A
    parameter with no default stays absent (the panel's widget is empty and both sides
    refuse it).  Destinations keep their rule: no default means a temporary output, and
    an optional destination that is not created by default stays absent (the panel's
    "skip output").  Hidden parameters are never sent by the panel and are not filled here
    either.  A filled default may be a QGIS object (the widget's value is), and it only
    travels to QGIS; it never appears in the result.
    """

    from qgis.core import (  # pyright: ignore[reportMissingImports]
        QgsProcessing,
        QgsProcessingParameterDefinition,
    )

    hidden = QgsProcessingParameterDefinition.FlagHidden
    optional = QgsProcessingParameterDefinition.FlagOptional
    values = dict(parameters)
    for definition in algorithm.parameterDefinitions():
        if definition.flags() & hidden:
            continue
        name = definition.name()
        if name in values:
            continue
        value = definition.defaultValueForGui()
        if definition.isDestination():
            if value is None or (isinstance(value, str) and not value):
                if definition.flags() & optional and not definition.createByDefault():
                    continue
                value = QgsProcessing.TEMPORARY_OUTPUT
        elif value is None:
            continue
        values[name] = value
    return values


def _require_acceptable(
    algorithm: Any, parameters: dict[str, Any], context: Any, continue_on_crs_mismatch: bool
) -> None:
    """桌面版点“运行”前问的每个问题，按同一顺序用 QGIS 自己的函数问一遍。

    Every question the desktop asks before Run, in the same order and through QGIS's own
    functions.
    """

    from qgis.core import QgsProcessingParameterDefinition  # pyright: ignore[reportMissingImports]

    hidden = QgsProcessingParameterDefinition.FlagHidden
    for definition in algorithm.parameterDefinitions():
        if definition.flags() & hidden or definition.isDestination():
            continue
        if not definition.checkValueIsAcceptable(parameters.get(definition.name())):
            raise FrameworkError(INVALID_INPUT, _BAD_PARAMETER.format(definition.description()))
    for definition in algorithm.parameterDefinitions():
        if definition.flags() & hidden or not definition.isDestination():
            continue
        value = parameters.get(definition.name())
        if not value:
            continue
        supported, error = definition.isSupportedOutputValue(value, context)
        if not supported:
            raise FrameworkError(INVALID_INPUT, error)
    # 桌面版只在“参数 CRS 不一致就先警告”这个开关打开时问；关掉时它直接继续。
    # The desktop asks only while its "warn before executing if parameter CRSes do not match"
    # switch is on; with the switch off it continues straight away.
    warned = _desktop_switch("Processing/Configuration/WARN_UNMATCHING_CRS", True)
    if (
        warned
        and not continue_on_crs_mismatch
        and not algorithm.validateInputCrs(parameters, context)
    ):
        raise FrameworkError(INVALID_INPUT, _CRS_MISMATCH_QUESTION)
    ok, message = algorithm.checkParameterValues(parameters, context)
    if not ok:
        raise FrameworkError(INVALID_INPUT, message)


class ProcessingListAlgorithms(
    BaseQuery[ProcessingListAlgorithmsParams, ProcessingListAlgorithmsResult],
    MainThreadOperationMixin[ProcessingListAlgorithmsParams, ProcessingListAlgorithmsResult],
):
    """桌面版工具箱能看到的每个算法，按（分组名、算法名）摊平排序。

    工具箱是一棵树（provider → 分组 → 算法），列表是扁平的：树序不复刻，顺序不是判据。

    Every algorithm the desktop toolbox shows, flattened by group name then algorithm name.
    The toolbox is a tree (provider, group, algorithm) and the list is flat: the tree order is
    not reproduced, and order is not a criterion.
    """

    name = "processing.list_algorithms"
    description = "List every algorithm the QGIS desktop toolbox shows"
    input_model = ProcessingListAlgorithmsParams
    output_model = ProcessingListAlgorithmsResult

    def execute_main(
        self, ctx: OperationContext, request: ProcessingListAlgorithmsParams
    ) -> ProcessingListAlgorithmsResult:
        from qgis.core import (  # pyright: ignore[reportMissingImports]
            Qgis,
            QgsApplication,
        )

        # 工具箱总会挡掉 HideFromToolbox；KnownIssues 只在“显示有已知问题的算法”关着时挡掉。
        # The toolbox always hides HideFromToolbox; it hides KnownIssues only while "show
        # algorithms with known issues" is off.
        excluded = int(Qgis.ProcessingAlgorithmFlag.HideFromToolbox)
        shown = _desktop_switch("Processing/Configuration/SHOW_ALGORITHMS_KNOWN_ISSUES", False)
        if not shown:
            excluded |= int(Qgis.ProcessingAlgorithmFlag.KnownIssues)
        algorithms = [
            algorithm_summary(algorithm)
            for algorithm in QgsApplication.processingRegistry().algorithms()
            if not int(algorithm.flags()) & excluded
        ]
        # 列表是扁平投影：按分组名、算法名、id 排；工具箱的 provider/分组树序不复刻。
        # The list is a flat projection ordered by group name, algorithm name and id; the
        # toolbox's provider/group tree order is not reproduced.
        algorithms.sort(key=lambda summary: (summary.group, summary.name, summary.id))
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
        from qgis.core import (  # pyright: ignore[reportMissingImports]
            QgsApplication,
            QgsProcessingParameterDefinition,
        )

        registry = QgsApplication.processingRegistry()
        algorithm = registry.algorithmById(request.algorithm_id)
        if algorithm is None:
            # 合法调用说出未知算法是调用方的输入错误，不是未知的插件操作。
            # A valid call naming an unknown algorithm is a caller input mistake; it is not
            # an unknown plugin operation.
            raise _help_unknown_algorithm(request.algorithm_id)

        # ``algorithmHelp()`` 会跳过隐藏参数，这里也一样。
        # ``algorithmHelp()`` skips a hidden parameter, and so does this.
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
    # 名字代表的是一组算法，它们可能改动数据或工程，所以风险标签要整个名字一起定。
    # The name stands for a group of algorithms that may change data or the project, so the
    # risk label cannot depend on the particular call.
    destructive = True

    def execute_main(
        self, ctx: OperationContext, request: ProcessingRunAlgorithmParams
    ) -> ProcessingRunAlgorithmResult:
        from qgis import processing  # pyright: ignore[reportMissingImports]
        from qgis.core import (  # pyright: ignore[reportMissingImports]
            QgsApplication,
            QgsProcessingFeedback,
        )

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

        algorithm = QgsApplication.processingRegistry().createAlgorithmById(request.algorithm_id)
        if algorithm is None:
            raise _run_unknown_algorithm(request.algorithm_id)
        feedback = ProcessingRunFeedback()
        context = processing.createContext(feedback)
        parameters = _gui_parameter_values(algorithm, request.parameters)
        _require_acceptable(algorithm, parameters, context, request.continue_on_crs_mismatch)
        try:
            # 参数、反馈与上下文都是桌面版会交出的那几样，算法由 QGIS 自己跑。
            # The parameters, feedback and context are the ones the desktop would hand over;
            # QGIS runs the algorithm itself.
            results = processing.run(
                request.algorithm_id, parameters, feedback=feedback, context=context
            )
        except Exception:
            # 原生消息和异常可能带上路径或主机名，所以只进本地日志、不进记录；调用方拿到的是
            # 引擎对意外失败的判定。
            # The native messages and the exception may quote a path or a host, so they reach
            # the local log and not the record; the caller gets the engine's own
            # unexpected-failure verdict.
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
                # ``None`` 是 QGIS 的「没有结果」，在这里当成一个空结果。
                # ``None`` is QGIS's "no result", which is an empty one here.
                outputs=_json_result(ctx.project, {} if results is None else results, added),
                messages=feedback.records,
            )
        except Exception:  # noqa: BLE001
            # 只撤掉这次调用加进去的图层 / only what this call added is undone,
            # 算法的外部副作用不声称回滚 / not the algorithm's own external side effects.
            for layer_id in added:
                ctx.project.removeMapLayer(layer_id)
            raise


# 本模块唯一的元组：``operations/__init__.py`` import 它，不许再写一份。
# The module's one tuple: ``operations/__init__.py`` imports it and must not define a
# second copy.
PROCESSING_OPERATIONS = (
    ProcessingListAlgorithms,
    ProcessingGetAlgorithmInfo,
    ProcessingRunAlgorithm,
)
