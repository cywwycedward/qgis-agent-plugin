"""三个 ``processing.*`` 操作。

三者都在主线程跑：Processing 直接操作工程与它持有的图层，一次运行还会把结果图层交给那个工程。

没有一条判据是本模块自己的：要列什么、挡什么、问什么、按什么顺序问、拒绝时说什么，全部取自
Processing 的源码（注册表与工具箱的过滤、``algorithmHelp()``、参数面板的
``createProcessingParameters`` 与 ``AlgorithmDialog.runAlgorithm``）。本模块只拥有两件事：GUI 交
给用户的那些话原样交给调用方（``messages`` 与失败记录），以及 ``parameters``/``outputs`` 必须是
有限 JSON 树这一条边界。

QGIS 在使用它的方法内部 import，所以本模块在裸解释器里也能加载，契约不依赖运行中的 QGIS。

All three run on the main thread: Processing works on the project and the layers it holds, and a
run hands its result layers to that project.

Not one criterion here is this module's own: what to list, what to hide, what to ask, in which
order, and what to say when refusing all come from Processing's source (the registry and the
toolbox's filters, ``algorithmHelp()``, the parameter panel's ``createProcessingParameters`` and
``AlgorithmDialog.runAlgorithm``).  This module owns two things only: the sentences the GUI hands
to the user are handed to the caller unchanged (``messages`` and the failed record), and the
boundary that ``parameters`` and ``outputs`` are finite JSON trees.

QGIS is imported inside the methods that use it, so this module loads in a bare interpreter and
its contracts stay testable without a running QGIS.
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

# 参数面板对不合法值说的那句话，``{0}`` 是参数的描述。
# The parameter panel's sentence for a bad value, ``{0}`` being the parameter's description.
_BAD_PARAMETER = "Wrong or missing parameter value: {0}"

# 参数 CRS 不一致时的问句；QGIS 的消息框默认按钮是“否”。
# The question asked when the parameters' CRSes differ; QGIS's message box defaults to "No".
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

    image = _json_image(value)
    if image is _NO_JSON:
        raise ValueError("only finite JSON values are allowed")
    return image


class AlgorithmSummary(BaseModel):
    """工具箱里的一行：provider、分组、算法各取它显示给自己的那个名字。

    One row of the toolbox: the provider, the group and the algorithm, each under the name it
    shows for itself.
    """

    model_config = ConfigDict(extra="forbid")

    id: StrictStr
    name: StrictStr
    group: StrictStr
    provider: StrictStr


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
    """一个原生算法 id、它的原生参数表，以及参数 CRS 不一致时的那个回答。

    A native algorithm id, its native parameter map, and the answer to the CRS question.
    """

    algorithm_id: StrictStr = Field(min_length=1)
    # 键类型必须严格：顶层是 pydantic 的 dict，所以这是「不许非字符串键」唯一能到的位置，
    # 其余交给那趟遍历。
    # Strict keys: the top level is pydantic's dict, so this is the only place the
    # no-non-string-key rule can reach; the walk checks the rest.
    parameters: dict[StrictStr, Any]
    # QGIS 在参数 CRS 不一致时会问“要继续吗”，默认按钮是“否”；这个字段就是那个回答。
    # QGIS asks "do you want to continue?" when the parameters' CRSes differ, with "No" as the
    # default button; this field is that answer.
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

    provider = algorithm.provider()
    return AlgorithmSummary(
        id=algorithm.id(),
        name=algorithm.displayName(),
        group=algorithm.group(),
        provider="" if provider is None else provider.name(),
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


def _json_result(value: Any) -> Any:
    """一个结果值作为 JSON，或者 ``INTERNAL_ERROR``。

    QGIS 交回的是原生 Python 对象：目的地串、路径、数字、列表，偶尔也有算法自己造的对象。前几种
    原样出去，最后一种被拒——没有无损的 JSON 形式时不再 ``str()`` 一下。

    One result value as JSON, or ``INTERNAL_ERROR``.  What QGIS hands back is native Python
    objects: destination strings, paths, numbers, lists, and occasionally an object an algorithm
    made up itself.  The first kinds leave as they are and the last is refused -- a value with no
    lossless JSON form is not ``str()``-ed.
    """

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


def _config_flag(key: str, default: bool) -> bool:
    """一个 Processing 布尔配置的当前值，按 ``ProcessingConfig`` 读它的那条规则。

    ``ProcessingConfig`` 把持久化的值按 ``str(value).lower() == "true"`` 归一（缺键时保留默认），
    所以 ``'false'`` 或 ``'0'`` 不是真；直接 ``bool()`` 会把它们当成真。

    One Processing boolean setting, as ``ProcessingConfig`` reads it.  It normalises the
    persisted value with ``str(value).lower() == "true"`` (keeping the default when the key is
    absent), so ``'false'`` or ``'0'`` is not true; a plain ``bool()`` would treat them as true.
    """

    from qgis.core import QgsSettings  # pyright: ignore[reportMissingImports]

    raw = QgsSettings().value(key, default)
    if raw is None:
        # 缺键或显式存成 null 时保留默认值。
        # A missing key or an explicitly null value keeps the default.
        return default
    return str(raw).lower() == "true"


def _values_with_defaults(algorithm: Any, parameters: dict[str, Any]) -> dict[str, Any]:
    """参数面板会交出的那张表：缺席的参数补上控件自己的默认值。

    面板给每个可见参数建控件：非输出控件一出生就是 ``defaultValueForGui()``（没有 GUI 设置时就是
    参数自己的默认值），输出控件在没有默认值时是临时输出（可选且不默认创建的那类留空，即跳过）。
    提交时它把每个控件的值都放进 map，包括那些没被碰过的，所以这里对缺席的非输出参数也放同一个
    值——QGIS 自己的 ``checkParameterValues`` 对缺键的 enum 并不回落到默认值，只靠“QGIS 会解析
    缺席”是不够的。没有默认值的保持缺席（面板那边是空控件，两边都会拒）。输出参数照旧：没有默认
    值就是临时输出，可选且不默认创建的保持缺席（面板的“跳过输出”）；调用方给 ``None`` 也是跳过
    输出，那个键在交给 QGIS 之前就被去掉。
    隐藏参数面板从不发送，这里也不补。补进来的默认值可能是 QGIS 对象（控件本来就是），它们只往
    QGIS 走，不会出现在结果里。

    The map the parameter panel hands over: missing parameters get the widget's own default.  The
    panel creates a widget per visible parameter: a non-destination widget starts at
    ``defaultValueForGui()`` (the parameter's own default when no GUI setting overrides it), and a
    destination widget without a default starts at the temporary output (the optional ones not
    created by default start empty, which is a skip).  The panel submits every widget's value,
    touched or not, so a missing non-destination parameter is filled with that same value here --
    QGIS's own ``checkParameterValues`` does not fall back to the default for an enum whose key is
    missing, so relying on "QGIS resolves a missing value" is only half the story.  A parameter
    with no default stays absent (the panel's widget is empty and both sides refuse it).
    Destinations keep their rule: no default means a temporary output, and an optional destination
    that is not created by default stays absent (the panel's "skip output"); an explicit ``None``
    is that same skip, and the key is dropped before QGIS sees it.  Hidden parameters are never
    sent by the panel and are not filled here either.  A filled default may be a QGIS object (the
    widget's value is), and it only travels to QGIS; it never appears in the result.
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
            # 显式 ``None`` 是调用方对“跳过输出”的回答：面板对跳过的输出不放键，QGIS 也只有
            # 缺键时才把它当“没有这个输出”。
            # An explicit ``None`` is the caller's "skip output" answer: the panel drops the key
            # of a skipped output, and only a missing key reads as "no such output" to QGIS.
            if definition.isDestination() and values[name] is None:
                del values[name]
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


def _require_acceptable_values(algorithm: Any, parameters: dict[str, Any], context: Any) -> None:
    """逐个参数问 QGIS 的判据，顺序就是参数定义的顺序。

    非输出参数问 ``checkValueIsAcceptable``，描述填进面板的那句话；有值的输出问
    ``isSupportedOutputValue``，用它自己的错误文本。隐藏参数面板不提交也不检查。

    Every parameter's own criterion, in definition order.  A non-destination parameter asks
    ``checkValueIsAcceptable``, whose description fills the panel's sentence; an output that has
    a value asks ``isSupportedOutputValue`` and uses its own error text.  A hidden parameter is
    neither submitted nor checked.
    """

    from qgis.core import QgsProcessingParameterDefinition  # pyright: ignore[reportMissingImports]

    hidden = QgsProcessingParameterDefinition.FlagHidden
    for definition in algorithm.parameterDefinitions():
        if definition.flags() & hidden:
            continue
        value = parameters.get(definition.name())
        if not definition.isDestination():
            if not definition.checkValueIsAcceptable(value):
                raise FrameworkError(INVALID_INPUT, _BAD_PARAMETER.format(definition.description()))
        elif value:
            supported, error = definition.isSupportedOutputValue(value, context)
            if not supported:
                raise FrameworkError(INVALID_INPUT, error)


def _require_acceptable_run(
    algorithm: Any, parameters: dict[str, Any], context: Any, continue_on_crs_mismatch: bool
) -> None:
    """运行前的最后两道问句：参数 CRS 不一致的确认，以及 ``checkParameterValues``。

    只有“参数 CRS 不一致就先警告”那个配置开着时才问第一句；关着时直接继续。

    The last two questions before a run: the CRS-mismatch confirmation and
    ``checkParameterValues``.  The first is asked only while the "warn before executing if
    parameter CRSes do not match" setting is on; with the setting off the run continues straight
    away.
    """

    warned = _config_flag("Processing/Configuration/WARN_UNMATCHING_CRS", True)
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
    """工具箱能看到的每个算法，按（分组名、算法名）摊平排序。

    工具箱是一棵树（provider → 分组 → 算法），列表是扁平投影：树序不复刻。

    Every algorithm the toolbox shows, flattened by group name then algorithm name.  The
    toolbox is a tree (provider, group, algorithm) and the list is a flat projection: the tree
    order is not reproduced.
    """

    name = "processing.list_algorithms"
    description = "List the algorithms the QGIS Processing toolbox shows"
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
        shown = _config_flag("Processing/Configuration/SHOW_ALGORITHMS_KNOWN_ISSUES", False)
        if not shown:
            excluded |= int(Qgis.ProcessingAlgorithmFlag.KnownIssues)
        algorithms = [
            algorithm_summary(algorithm)
            for algorithm in QgsApplication.processingRegistry().algorithms()
            if not int(algorithm.flags()) & excluded
        ]
        # 工具箱是三层树（provider → 分组 → 算法），每层按名字排；扁平列表照它的遍历顺序摊开，
        # 同名的用 id 定序。
        # shortcut: 这里按码点比较，工具箱用 locale 排序，需要与 Qt 逐字一致时换成 QCollator。
        # English: code-point comparison here, locale collation in the toolbox, switch to QCollator
        # when byte-identical order with Qt matters.
        # The toolbox is a three-level tree (provider, group, algorithm) with each level ordered by
        # name; the flat list follows that traversal and orders equal names by id.
        algorithms.sort(
            key=lambda summary: (summary.provider, summary.group, summary.name, summary.id)
        )
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

        algorithm = QgsApplication.processingRegistry().createAlgorithmById(request.algorithm_id)
        if algorithm is None:
            raise _run_unknown_algorithm(request.algorithm_id)
        feedback = ProcessingRunFeedback()
        context = processing.createContext(feedback)
        parameters = _values_with_defaults(algorithm, request.parameters)
        _require_acceptable_values(algorithm, parameters, context)
        parameters = algorithm.preprocessParameters(parameters)
        _require_acceptable_run(algorithm, parameters, context, request.continue_on_crs_mismatch)
        # 提供者的告警是运行前该让用户看到的那一句。
        # A provider's warning is the sentence the user is owed before the run.
        provider = algorithm.provider()
        if provider is not None and provider.warningMessage():
            feedback.reportError(provider.warningMessage())
        try:
            # 参数、反馈与上下文都是参数面板会交出的那几样；运行与结果后处理都归 QGIS。
            # The parameters, feedback and context are the ones the parameter panel hands over;
            # the run and its result post-processing are QGIS's.
            results = processing.runAndLoadResults(
                request.algorithm_id, parameters, feedback=feedback, context=context
            )
        except Exception as error:  # noqa: BLE001 - what QGIS raised is the caller's sentence
            # 消息与异常原文另有一份进本地日志：结果只有成功时才有字段装它们。
            # The messages and the exception text keep a second copy in the local log: only a
            # successful run has a field to carry them.
            for record in feedback.records:
                _LOGGER.warning(
                    "processing %s reported %s: %s", request.algorithm_id, record.level, record.text
                )
            _LOGGER.exception("processing.runAndLoadResults(%s) raised", request.algorithm_id)
            raise FrameworkError(INTERNAL_ERROR, str(error) or type(error).__name__) from error

        return ProcessingRunAlgorithmResult(
            algorithm_id=request.algorithm_id,
            outputs=_json_result(results),
            messages=feedback.records,
        )


# 本模块唯一的元组：``operations/__init__.py`` import 它，不许再写一份。
# The module's one tuple: ``operations/__init__.py`` imports it and must not define a
# second copy.
PROCESSING_OPERATIONS = (
    ProcessingListAlgorithms,
    ProcessingGetAlgorithmInfo,
    ProcessingRunAlgorithm,
)
