# 静态分析器自己的解释器里没有 qgis/pydantic，所以本文件只关掉“找不到模块”这一类报告；能不能
# 真的加载由 QGIS 解释器的验收跑证明。
# The analyzer's own interpreter has no qgis/pydantic, so this file turns off only the
# missing-module report; whether it really loads is proven by the QGIS interpreter's run.
# pyright: reportMissingImports=false

"""验收用例：三个 processing 操作。

工具箱看到的算法清单、一个算法的原生定义，以及真的跑一次：参数怎么补默认值、输出归谁、
不是 JSON 的东西怎么被拒。验收自己注册一个提供者，好走到第三方那条路。

The acceptance cases for the three processing operations: the list the toolbox shows, one
algorithm's native definition, and a real run -- how parameters get their defaults, who owns
an output, and what happens to values that are not JSON.  The acceptance registers a provider
of its own, which is what makes the third-party path reachable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fixtures  # pyright: ignore[reportMissingImports]
from harness import Context, Skip, equal, pump, require, require_processing  # pyright: ignore[reportMissingImports]
from helpers import (
    algorithm_info,
    cities_layer,
    error_code,
    message_of,
    output_layer,
    outputs_of,
    parameters_by_name,
    processing_algorithms,
    project_layer,
    project_layer_ids,
    project_layers,
    result_of,
    run_algorithm,
)
from pydantic import ValidationError  # pyright: ignore[reportMissingImports]

from qgis_agent_plugin.core.models import (  # pyright: ignore[reportMissingImports]
    INTERNAL_ERROR,
    INVALID_INPUT,
    PATH_NOT_ALLOWED,
    ExecutionStatus,
)

# 验收自己注册一个 Processing 提供者：这是唯一能走到第三方提供者路径的办法，因为这台机器上
# QGIS 自带的算法都没有这个形状（实测）。经 ``addProvider`` 加进来的提供者在插件看来就是第三方，
# 它的算法走普通路径——先被发现，再用 ``run()`` 执行。
# The acceptance registers its own Processing provider: it is the only way to reach a third-party
# path, because no algorithm QGIS ships here has that shape (measured).  A provider added through
# ``addProvider`` is third-party as far as the plugin can see, and its algorithms take the
# ordinary path: discovery, then ``run()`` like every other algorithm.
PROBE_PROVIDER = "p5probe"


PROBE_ALGORITHMS = (
    f"{PROBE_PROVIDER}:non_json",
    f"{PROBE_PROVIDER}:runs",
)


# 记录探针算法被要求做了什么，这样用例才能证明算法真的跑了，而不是被插件提前拒掉。
# What the probe algorithms were asked to do, so a case can show that the algorithm really ran
# instead of being refused up front by the plugin.
PROBE_CALLS: list[str] = []


# 原生注册表接管提供者的所有权，所以这里必须让 Python 包装对象活着，与
# ``_REGISTERED_RENDERER_METADATA`` 是同一个理由。
# The native registry takes ownership of a provider, so the Python wrapper has to stay alive here
# for the same reason the renderer registry one does.
_registered_provider: list[Any] = []


def processing_probe_provider() -> None:
    """登记验收自己的提供者一次；拿不到它就拒跑。

    这些类写在这个函数里，是因为本模块不得在模块级 import QGIS（仓库的导入规则）；只有注册表自己
    拒了提供者（``addProvider()`` 返回假）才抛 ``Skip``，那是唯一会让第三方路径无法度量的情形。

    Register the acceptance's own provider once, and refuse to run without it.  The classes
    live inside this function because this module may not import QGIS at module level (the
    repository's import rule), and ``Skip`` is raised only when the registry itself refuses the
    provider (``addProvider()`` returning false), which is the one condition that would leave
    the third-party paths unmeasurable.
    """

    if _registered_provider:
        return
    from qgis.core import (  # pyright: ignore[reportMissingImports]
        QgsApplication,
        QgsProcessingAlgorithm,
        QgsProcessingOutputNumber,
        QgsProcessingOutputVectorLayer,
        QgsProcessingProvider,
        QgsVectorLayer,
    )

    class NonJsonResult(QgsProcessingAlgorithm):
        """先交出一个真的结果图层的 id，再交一个没有 JSON 映像的值。

        Hands over the id of a real result layer, then a value with no JSON image.
        """

        def name(self) -> str:
            return "non_json"

        def displayName(self) -> str:
            return "P5 probe: non-JSON result"

        def createInstance(self):  # noqa: N802 - the native spelling
            return NonJsonResult()

        def initAlgorithm(self, config=None):  # noqa: N802 - the native spelling
            self.addOutput(QgsProcessingOutputVectorLayer("LAYER", "the layer it hands over"))

        def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
            PROBE_CALLS.append(f"{PROBE_PROVIDER}:non_json.processAlgorithm")
            layer = QgsVectorLayer("Point?field=p5:string", "p5 probe result", "memory")
            # ``LAYER`` 是一个真实图层的 id（输出定义把它当图层输出），后面跟着一个没有无损 JSON
            # 映像的值；拒绝发生在结果转 JSON 那一步，所以这一对值必须真的交到插件手里。
            # ``LAYER`` is a real layer's id (the output definition types it as a layer output),
            # followed by a value with no lossless JSON image; the refusal happens in the JSON
            # step, so this pair really has to reach the plugin.
            return {"LAYER": layer.id(), "OBJECT": object()}

    class Runs(QgsProcessingAlgorithm):
        """一个真的跑起来并返回 JSON 的第三方算法。

        A third-party algorithm that really runs and returns JSON.
        """

        def name(self) -> str:
            return "runs"

        def displayName(self) -> str:
            return "P5 probe: runs"

        def createInstance(self):  # noqa: N802 - the native spelling
            return Runs()

        def initAlgorithm(self, config=None):  # noqa: N802 - the native spelling
            self.addOutput(QgsProcessingOutputNumber("VALUE", "a number"))

        def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
            PROBE_CALLS.append(f"{PROBE_PROVIDER}:runs.processAlgorithm")
            return {"VALUE": 42}

    class AcceptanceProvider(QgsProcessingProvider):
        def id(self) -> str:
            return PROBE_PROVIDER

        def name(self) -> str:
            return "P5 acceptance probes"

        def loadAlgorithms(self) -> None:  # noqa: N802 - the native spelling
            for algorithm in (NonJsonResult(), Runs()):
                self.addAlgorithm(algorithm)

    provider = AcceptanceProvider()
    if not QgsApplication.processingRegistry().addProvider(provider):
        raise Skip(
            "the processing registry refused the acceptance's own provider (addProvider() "
            "returned false), so the third-party paths of P01/P02/P15 cannot be measured here"
        )
    _registered_provider.append(provider)


def p01_list_algorithms(ctx: Context) -> str:
    require_processing()
    processing_probe_provider()
    from qgis.core import Qgis, QgsSettings

    algorithms = processing_algorithms()
    # 工具箱总会挡掉 HideFromToolbox；KnownIssues 只在对应开关关着时挡掉（默认关着）。
    # The toolbox always hides HideFromToolbox; it hides KnownIssues only while the matching
    # switch is off (off by default).
    excluded = int(Qgis.ProcessingAlgorithmFlag.HideFromToolbox)
    shown = (
        str(
            QgsSettings().value("Processing/Configuration/SHOW_ALGORITHMS_KNOWN_ISSUES", False)
        ).lower()
        == "true"
    )
    if not shown:
        excluded |= int(Qgis.ProcessingAlgorithmFlag.KnownIssues)
    visible = {
        algorithm.id(): algorithm
        for algorithm in algorithms
        if not int(algorithm.flags()) & excluded
    }
    hidden = [algorithm.id() for algorithm in algorithms if algorithm.id() not in visible]
    require(
        bool(hidden),
        "no registered algorithm carries HideFromToolbox or a hidden KnownIssues flag, so the "
        "filter is not measurable here",
    )

    record = ctx.run("processing.list_algorithms", {})
    equal(record.status, ExecutionStatus.SUCCEEDED, "processing.list_algorithms status")
    summaries = result_of(record)["algorithms"]
    ids = [summary["id"] for summary in summaries]

    expected = sorted(
        (
            algorithm.provider().name(),
            algorithm.group(),
            algorithm.displayName(),
            algorithm.id(),
        )
        for algorithm in visible.values()
    )
    # 列表是扁平投影，但顺序照工具箱那三层（provider → 分组 → 算法）；同一个键出现多次时用 id
    # 定序。
    # A flat projection, but ordered as the toolbox's three levels are (provider, group,
    # algorithm); equal keys are ordered by id.
    equal(
        [
            (summary["provider"], summary["group"], summary["name"], summary["id"])
            for summary in summaries
        ],
        expected,
        "the toolbox (provider, group, display name, id) order",
    )
    equal(ids, [algorithm_id for _, _, _, algorithm_id in expected], "the visible ids")
    equal(len(ids), len(set(ids)), "duplicate ids")
    for summary in summaries:
        equal(sorted(summary), ["group", "id", "name", "provider"], "the fields of one summary")
    sample = next(summary for summary in summaries if summary["id"] == "native:buffer")
    equal(
        sample["name"],
        visible["native:buffer"].displayName(),
        "a display name from the registry",
    )
    equal(sample["group"], visible["native:buffer"].group(), "a group from the registry")
    equal(
        sample["provider"],
        visible["native:buffer"].provider().name(),
        "a provider name from the registry",
    )
    for algorithm_id in PROBE_ALGORITHMS:
        require(
            algorithm_id in ids,
            f"the third-party algorithm {algorithm_id} is missing from the list",
        )
    prefixes = {algorithm_id.split(":")[0] for algorithm_id in ids}
    require(len(prefixes) > 1, f"only one provider prefix in the list: {sorted(prefixes)}")
    return (
        f"{len(ids)} algorithms in the toolbox order (provider, group, display name) -- not its "
        f"tree -- exactly the live registry minus the {len(hidden)} carrying HideFromToolbox or "
        f"KnownIssues ({', '.join(sorted(hidden)[:3])}…), over {len(prefixes)} provider prefixes "
        f"({', '.join(sorted(prefixes))}), four fields each"
    )


def p02_every_registered_algorithm_answers(ctx: Context) -> str:
    require_processing()
    processing_probe_provider()
    algorithms = processing_algorithms()

    refused = []
    for algorithm in algorithms:
        record = ctx.run("processing.get_algorithm_info", {"algorithm_id": algorithm.id()})
        if record.status is not ExecutionStatus.SUCCEEDED:
            code = record.error.code if record.error is not None else str(record.status)
            refused.append(f"{algorithm.id()}: {code}")
    require(not refused, f"the definition of {len(refused)} algorithms was refused: {refused[:3]}")

    # 本次运行自己注册的提供者就是这里的第三方例子。
    # A provider this run registered is the third-party example here.
    third_party = run_algorithm(ctx, f"{PROBE_PROVIDER}:runs", {})
    equal(
        third_party.status,
        ExecutionStatus.SUCCEEDED,
        f"{PROBE_PROVIDER}:runs failed: {third_party.error}",
    )
    equal(outputs_of(third_party), {"VALUE": 42}, "the third-party run's outputs")

    # 还要测一个 QGIS 自带、但不属于 'native' 的提供者，如果这台机器上装了一个。
    # A provider QGIS ships itself but that is not 'native', when this machine has one.
    extra = "no QGIS-shipped non-native provider with a runnable algorithm is installed here"
    shipped = next((item for item in algorithms if item.id() == "3d:tessellate"), None)
    if shipped is not None:
        layer_id = cities_layer(ctx, "p02 cities")
        record = run_algorithm(ctx, shipped.id(), {"INPUT": layer_id, "OUTPUT": "memory:"})
        equal(record.status, ExecutionStatus.SUCCEEDED, f"{shipped.id()} failed: {record.error}")
        equal(shipped.provider().id(), "3d", "the provider of the algorithm that ran")
        layer = output_layer(ctx, record, "OUTPUT")
        require(layer.isValid(), "the result of a non-native provider is not a valid layer")
        extra = (
            f"provider '3d' also ran {shipped.id()} from a project layer into memory "
            f"({layer.featureCount()} features in the project)"
        )
    return (
        f"all {len(algorithms)} registered algorithms answered get_algorithm_info, so discovery "
        f"filters nothing; the provider this run registered ran {PROBE_PROVIDER}:runs for real "
        f"(VALUE=42); {extra}"
    )


def p03_algorithm_info_fields(ctx: Context) -> str:
    require_processing()
    from qgis.core import (  # pyright: ignore[reportMissingImports]
        QgsApplication,
        QgsProcessingParameterDefinition,
        QgsProcessingParameterEnum,
    )

    registry = QgsApplication.processingRegistry()
    six_fields = ["accepted_types", "description", "help", "name", "options", "type"]
    parameters_seen = 0
    outputs_seen = 0
    for algorithm_id in (
        "native:buffer",
        "native:zonalstatisticsfb",
        "native:basicstatisticsforfields",
    ):
        algorithm = registry.algorithmById(algorithm_id)
        require(algorithm is not None, f"{algorithm_id} is not registered here")
        visible = [
            definition
            for definition in algorithm.parameterDefinitions()
            if not definition.flags() & QgsProcessingParameterDefinition.FlagHidden
        ]
        info = algorithm_info(ctx, algorithm_id)
        equal(
            sorted(info),
            ["algorithm", "outputs", "parameters"],
            f"{algorithm_id}: the result fields",
        )
        details = info["algorithm"]
        equal(
            sorted(details),
            ["help", "id", "name", "short_description"],
            f"{algorithm_id}: the algorithm fields",
        )
        equal(details["id"], algorithm.id(), f"{algorithm_id}: id")
        equal(details["name"], algorithm.displayName(), f"{algorithm_id}: name")
        equal(
            details["short_description"],
            algorithm.shortDescription() or "",
            f"{algorithm_id}: short_description",
        )
        equal(details["help"], algorithm.shortHelpString() or "", f"{algorithm_id}: help")
        equal(
            [parameter["name"] for parameter in info["parameters"]],
            [definition.name() for definition in visible],
            f"{algorithm_id}: the native parameter order with the hidden ones skipped",
        )
        for parameter, definition in zip(info["parameters"], visible, strict=True):
            name = f"{algorithm_id}.{definition.name()}"
            equal(sorted(parameter), six_fields, f"{name}: the six fields")
            equal(parameter["type"], definition.__class__.__name__, f"{name}: type")
            equal(parameter["description"], definition.description() or "", f"{name}: description")
            equal(parameter["help"], definition.help() or "", f"{name}: help")
            if isinstance(definition, QgsProcessingParameterEnum):
                equal(parameter["options"], list(definition.options()), f"{name}: option order")
            else:
                equal(parameter["options"], None, f"{name}: options on a non-enum parameter")
            parameter_type = registry.parameterType(definition.type())
            equal(
                parameter["accepted_types"],
                [] if parameter_type is None else list(parameter_type.acceptedPythonTypes()),
                f"{name}: accepted_types",
            )
            parameters_seen += 1
        equal(
            [(output["name"], output["type"], output["description"]) for output in info["outputs"]],
            [
                (output.name(), output.__class__.__name__, output.description() or "")
                for output in algorithm.outputDefinitions()
            ],
            f"{algorithm_id}: the native output order, the class name as the type",
        )
        outputs_seen += len(info["outputs"])

    # 隐藏参数确实被略过：这次运行会找一个真的藏了参数的算法来验证。
    # A hidden parameter really is left out: this run finds an algorithm that hides one.
    hidden_algorithm = next(
        (
            algorithm
            for algorithm in processing_algorithms()
            if any(
                definition.flags() & QgsProcessingParameterDefinition.FlagHidden
                for definition in algorithm.parameterDefinitions()
            )
        ),
        None,
    )
    if hidden_algorithm is None:
        raise Skip(
            "no registered algorithm hides a parameter here, so nothing would prove the skip"
        )
    hidden = [
        definition.name()
        for definition in hidden_algorithm.parameterDefinitions()
        if definition.flags() & QgsProcessingParameterDefinition.FlagHidden
    ]
    listed = [
        parameter["name"] for parameter in algorithm_info(ctx, hidden_algorithm.id())["parameters"]
    ]
    require(
        not set(hidden) & set(listed),
        f"{hidden_algorithm.id()}: a hidden parameter was reported anyway: {listed}",
    )

    zonal = parameters_by_name(algorithm_info(ctx, "native:zonalstatisticsfb"))
    enum_options = zonal["STATISTICS"]["options"]
    require(
        isinstance(enum_options, list) and len(enum_options) > 1,
        f"an enum's options did not survive: {enum_options!r}",
    )
    equal(zonal["COLUMN_PREFIX"]["options"], None, "options are null for a string parameter")
    return (
        f"{parameters_seen} parameters and {outputs_seen} outputs of three algorithms read back "
        f"field by field against the live definitions (six fields, native order, the class name "
        f"as the type, acceptedPythonTypes as accepted_types, an enum's option order, null "
        f"options elsewhere, four algorithm fields); {hidden_algorithm.id()} hides "
        f"{len(hidden)} parameter(s) ({', '.join(hidden)}) and none of them is reported; "
        f"STATISTICS keeps its {len(enum_options)} options and COLUMN_PREFIX answers null"
    )


def p04_unknown_algorithm(ctx: Context) -> str:
    require_processing()
    missing = "native:p5-acceptance-not-an-algorithm"
    help_sentence = f'Algorithm "{missing}" not found.'
    run_sentence = f"Error: Algorithm {missing} not found\n"
    before = project_layer_ids()

    info = ctx.run("processing.get_algorithm_info", {"algorithm_id": missing})
    equal(info.status, ExecutionStatus.FAILED, "get_algorithm_info on an unknown id")
    equal(error_code(info), INVALID_INPUT, "the code for an unknown algorithm")
    equal(message_of(info), help_sentence, "the get_algorithm_info sentence")

    # 运行路径自己解析 id：未知 id 在调用 ``runAndLoadResults()`` 之前就被拒，仍是调用方的输入
    # 错误。
    # The run path resolves the id itself: an unknown id is refused before
    # ``runAndLoadResults()`` and stays a caller input mistake.
    run = run_algorithm(ctx, missing, {})
    equal(run.status, ExecutionStatus.FAILED, "run_algorithm on an unknown id")
    equal(error_code(run), INVALID_INPUT, "the run code for an unknown algorithm")
    equal(message_of(run), run_sentence, "the run sentence")
    equal(project_layer_ids(), before, "the layers after the unknown id")
    for record in (info, run):
        require(record.result is None, "a refused call carries a result")
    return (
        f"an unknown id is FAILED(INVALID_INPUT) from both operations, each with its own action's "
        f"sentence: {help_sentence!r} from get_algorithm_info (algorithmHelp's "
        f"printout) and {run_sentence!r} from run_algorithm (the run path's exception, newline "
        "included); neither call carries a result or adds a layer"
    )


def p05_unknown_key(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p05 cities")
    before = project_layer_ids()
    parameters = {
        "INPUT": layer_id,
        "DISTANCE": 1.0,
        "SEGMENTS": 8,
        "DISSOLVE": False,
        "OUTPUT": "memory:",
        "P5_NOT_A_PARAMETER": 1,
    }

    record = run_algorithm(ctx, "native:buffer", parameters)
    equal(
        record.status,
        ExecutionStatus.SUCCEEDED,
        f"a run with an undeclared parameter: {record.error}",
    )
    layer = output_layer(ctx, record, "OUTPUT")
    require(layer.isValid(), "the buffer result is not a valid layer")
    equal(layer.featureCount(), 5, "one buffer per input point")
    equal(len(project_layer_ids()), len(before) + 1, "the layers after the run")
    return (
        "P5_NOT_A_PARAMETER reached QGIS untouched and native:buffer SUCCEEDED, adding a valid "
        f"{layer.featureCount()}-feature layer to the project: an undeclared key is QGIS's to "
        "ignore"
    )


def p06_buffer_memory_output(ctx: Context) -> str:
    require_processing()
    from qgis.core import Qgis  # pyright: ignore[reportMissingImports]

    layer_id = cities_layer(ctx, "p06 cities")
    before = project_layer_ids()
    record = run_algorithm(
        ctx,
        "native:buffer",
        {
            "INPUT": layer_id,
            "DISTANCE": 1.0,
            "SEGMENTS": 8,
            "DISSOLVE": False,
            "OUTPUT": "memory:",
        },
    )
    equal(record.status, ExecutionStatus.SUCCEEDED, f"native:buffer failed: {record.error}")
    equal(sorted(outputs_of(record)), ["OUTPUT"], "the native output keys")
    layer = output_layer(ctx, record, "OUTPUT")
    require(layer.isValid(), "the buffer result is not a valid layer")
    equal(layer.geometryType(), Qgis.GeometryType.Polygon, "the geometry of the result")
    equal(layer.featureCount(), 5, "one buffer per input point")
    equal(sorted(project_layer_ids()), sorted([*before, layer.id()]), "the project's layers")
    pump(ctx.app, 0.05)
    require(
        project_layers().get(layer.id()) is layer, "the result layer went away with its context"
    )
    return (
        'OUTPUT="memory:" SUCCEEDED, its result layer was taken out of the temporary store and '
        f"registered in the project as {layer.id()[:8]}…, a valid polygon layer with "
        f"{layer.featureCount()} features that is still there after the event loop turned"
    )


def p07_array_input(ctx: Context) -> str:
    require_processing()
    first = cities_layer(ctx, "p07 first")
    second = cities_layer(ctx, "p07 second")
    require(first != second, "the two layer.add calls returned the same id")

    record = run_algorithm(
        ctx, "native:mergevectorlayers", {"LAYERS": [first, second], "OUTPUT": "memory:"}
    )
    equal(record.status, ExecutionStatus.SUCCEEDED, f"mergevectorlayers failed: {record.error}")
    layer = output_layer(ctx, record, "OUTPUT")
    equal(layer.featureCount(), 10, "the features of two five-feature layers merged from a list")
    return (
        "native:mergevectorlayers accepted a two-element JSON array of project layer ids "
        f"(LAYERS) and returned {layer.featureCount()} features, so a list parameter is passed "
        "through as the native structure"
    )


def p08_scalar_output(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p08 cities")

    record = run_algorithm(
        ctx, "native:basicstatisticsforfields", {"INPUT_LAYER": layer_id, "FIELD_NAME": "pop"}
    )
    equal(record.status, ExecutionStatus.SUCCEEDED, f"basicstatistics failed: {record.error}")
    outputs = outputs_of(record)
    equal(outputs["COUNT"], 3, "the non-null values of pop")
    equal(outputs["SUM"], fixtures.POP_TOTAL, "the sum of pop")
    require(
        abs(outputs["MEAN"] - fixtures.POP_TOTAL / 3) < 1e-9,
        f"the mean of pop: {outputs['MEAN']!r}",
    )
    for key in ("COUNT", "SUM", "MEAN", "MIN", "MAX"):
        require(
            type(outputs[key]) in (int, float),
            f"{key} is not a JSON number: {outputs[key]!r}",
        )

    # 两个输出控件的默认值都是“临时输出”，所以两个键都出现：一个临时图层、一个临时 HTML。
    # Both output widgets default to a temporary output, so both keys appear: a temporary layer
    # and a temporary HTML file.
    statistics = output_layer(ctx, record, "OUTPUT")
    require(statistics.isValid(), "the statistics layer is not valid")
    equal(statistics.featureCount(), 1, "the statistics table's row count")
    html = outputs["OUTPUT_HTML_FILE"]
    require(isinstance(html, str), f"the HTML output is {html!r}, not a path")
    require(Path(html).is_file(), f"the HTML output {html!r} was not written")

    # ``None`` 是“跳过输出”的位置：两个键都不出现，标量照旧。
    # ``None`` is "skip output": neither key appears, and the scalars stay.
    skipped = run_algorithm(
        ctx,
        "native:basicstatisticsforfields",
        {
            "INPUT_LAYER": layer_id,
            "FIELD_NAME": "pop",
            "OUTPUT": None,
            "OUTPUT_HTML_FILE": None,
        },
    )
    equal(skipped.status, ExecutionStatus.SUCCEEDED, f"skipped outputs: {skipped.error}")
    skipped_outputs = outputs_of(skipped)
    require(
        "OUTPUT" not in skipped_outputs and "OUTPUT_HTML_FILE" not in skipped_outputs,
        f"a skipped output still appeared: {sorted(skipped_outputs)}",
    )
    equal(skipped_outputs["SUM"], fixtures.POP_TOTAL, "the sum with both outputs skipped")
    return (
        f"a scalar-only run returned {len(outputs) - 2} finite JSON numbers (COUNT=3, "
        f"SUM=200.0, MEAN={outputs['MEAN']:.3f}) plus the panel's two temporary outputs: the "
        f"statistics layer ({statistics.featureCount()} row) and the HTML file it wrote, while "
        'passing ``None`` for both destinations is "skip output" and leaves the '
        "scalars only"
    )


def p09_multiple_outputs(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p09 cities")

    record = run_algorithm(
        ctx,
        "native:adduniquevalueindexfield",
        {
            "INPUT": layer_id,
            "FIELD": "name",
            "FIELD_NAME": "idx",
            "OUTPUT": "memory:",
            "SUMMARY_OUTPUT": "memory:",
        },
    )
    equal(record.status, ExecutionStatus.SUCCEEDED, f"adduniquevalueindexfield: {record.error}")
    equal(sorted(outputs_of(record)), ["OUTPUT", "SUMMARY_OUTPUT"], "the native output keys")
    first = output_layer(ctx, record, "OUTPUT")
    second = output_layer(ctx, record, "SUMMARY_OUTPUT")
    require(first.id() != second.id(), "both output keys name the same layer")
    require(first.isValid() and second.isValid(), "an output layer is not valid")
    require(
        "idx" in [field.name() for field in first.fields()],
        f"the new field is missing: {[field.name() for field in first.fields()]}",
    )
    equal(second.featureCount(), 5, "the summary has one feature per input feature")
    return (
        "both native output keys survived (OUTPUT and SUMMARY_OUTPUT) as two distinct, valid "
        f"project layers ({first.featureCount()} and {second.featureCount()} features), so no "
        "fixed OUTPUT-only mapping is assumed"
    )


def p10_json_values(ctx: Context) -> str:
    require_processing()
    from qgis.core import Qgis, QgsProject  # pyright: ignore[reportMissingImports]

    from qgis_agent_plugin.operations.processing import ProcessingRunAlgorithmParams  # pyright: ignore[reportMissingImports]

    accepted = ProcessingRunAlgorithmParams.model_validate(
        {
            "algorithm_id": "native:mergevectorlayers",
            "parameters": {"LAYERS": ["a", "b"], "DEEP": {"values": [1, {"flag": True}]}},
        }
    )
    equal(
        accepted.parameters["DEEP"],
        {"values": [1, {"flag": True}]},
        "a nested JSON object survived the input boundary",
    )

    refused = {
        "NaN": float("nan"),
        "infinity": float("inf"),
        "a Qt enum": Qgis.GeometryType.Point,
        "a QObject": QgsProject.instance(),
        "bytes": b"\x00",
        "an integer key": {1: "x"},
        "a nested NaN": ["a", [float("-inf")]],
    }
    for label, value in refused.items():
        try:
            ProcessingRunAlgorithmParams.model_validate(
                {"algorithm_id": "native:buffer", "parameters": {"DISTANCE": value}}
            )
        except ValidationError:
            continue
        raise AssertionError(f"the input boundary accepted {label}")

    layer_id = cities_layer(ctx, "p10 cities")
    before = project_layer_ids()
    record = run_algorithm(
        ctx,
        "native:buffer",
        {"INPUT": layer_id, "DISTANCE": float("nan"), "OUTPUT": "memory:"},
    )
    equal(record.status, ExecutionStatus.FAILED, "a run whose distance is NaN")
    equal(error_code(record), INVALID_INPUT, "the code for a NaN parameter")
    equal(project_layer_ids(), before, "the layers after the NaN refusal")
    return (
        "a nested JSON object and array are accepted and unchanged, while NaN, infinity, a Qt "
        "enum, a QObject, bytes, an integer dict key and a nested -inf are all refused with "
        "INVALID_INPUT before anything runs"
    )


def p12_input_path_policy(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p12 cities")
    outside = ctx.fixtures.outside / "secret.gpkg"
    require(outside.is_file(), "the outside fixture is missing")

    # 插件侧没有路径策略：任何本地绝对路径都原样到达 QGIS，接下来由 QGIS 决定。远程那一半故意
    # 不测——要测它就得发真请求，而没有用例会发。
    # There is no plugin-side path policy: any local absolute path reaches QGIS as it arrived, and
    # what happens next is QGIS's decision.  The remote half is not measured here on purpose:
    # measuring it would mean a real request, and no case makes one.
    outside_record = run_algorithm(
        ctx, "native:mergevectorlayers", {"LAYERS": [layer_id, str(outside)], "OUTPUT": "memory:"}
    )
    if outside_record.status is ExecutionStatus.SUCCEEDED:
        merged = output_layer(ctx, outside_record, "OUTPUT")
        outside_evidence = (
            f"native:mergevectorlayers read {outside.name} from outside the fixtures directory and "
            f"merged it into a layer with {merged.featureCount()} features (the project layer "
            f"plus the five features of the outside file)"
        )
    else:
        outside_evidence = (
            f"native:mergevectorlayers failed on {outside.name} with "
            f"{error_code(outside_record)} from QGIS's own layer loading"
        )
        require(
            error_code(outside_record) != PATH_NOT_ALLOWED,
            "the plugin still refuses a local file the caller named",
        )

    # 同一个边界的正面一半：CRS 权威码长得像 URI scheme，但它是 QGIS 解析的标识符，不是数据源。
    # The positive half of the same boundary: a CRS authority code has the shape of a URI scheme,
    # yet it is an identifier QGIS parses, not a data source.
    reprojected = run_algorithm(
        ctx,
        "native:reprojectlayer",
        {"INPUT": layer_id, "TARGET_CRS": "EPSG:32633", "OUTPUT": "memory:"},
    )
    equal(reprojected.status, ExecutionStatus.SUCCEEDED, f"reprojectlayer: {reprojected.error}")
    equal(
        output_layer(ctx, reprojected, "OUTPUT").crs().authid(),
        "EPSG:32633",
        "the CRS of the reprojected result",
    )
    return (
        f"{outside_evidence}; "
        "TARGET_CRS='EPSG:32633' on the same entry point SUCCEEDED and the result really is in "
        "EPSG:32633, so a CRS code is still handed through as data"
    )


def p13_destination_policy(ctx: Context) -> str:
    require_processing()
    from qgis.core import QgsProcessing, QgsVectorLayer  # pyright: ignore[reportMissingImports]

    layer_id = cities_layer(ctx, "p13 cities")
    target = ctx.fixtures.root / "p13 output.gpkg"
    before = project_layer_ids()

    # 写真实文件是调用方的决定：QGIS 就写到被告知的位置。``gdal:convertformat`` 是拷贝图层，所以
    # 写出的文件要带同样的要素与几何——只看行数会被几何全 NULL 的表骗过，而 ``gdal:buffervectors``
    # 在几何列名不匹配时留下的正是这种表。
    # A real file destination is the caller's decision: QGIS writes it where it was told.
    # ``gdal:convertformat`` copies the layer, so the written file has to carry the same features
    # and their geometry: a count alone is fooled by a table whose every geometry is NULL, which is
    # what ``gdal:buffervectors`` leaves behind when its geometry column name does not match.
    written = run_algorithm(
        ctx,
        "gdal:convertformat",
        {"INPUT": layer_id, "OUTPUT": str(target)},
    )
    equal(written.status, ExecutionStatus.SUCCEEDED, f"convertformat to a file: {written.error}")
    equal(outputs_of(written), {"OUTPUT": str(target)}, "the value of the output key")
    require(target.is_file(), f"{target.name} was not written")
    # 磁盘输出也是运行的结果：QGIS 的后处理把它读进工程。
    # A disk output is a result too: QGIS's post-processing reads it into the project.
    loaded = {lid: layer for lid, layer in project_layers().items() if lid not in before}
    equal(len(loaded), 1, "the layers the disk run added")
    loaded_layer = next(iter(loaded.values()))
    require(
        str(target) in loaded_layer.source(),
        f"the loaded layer's source {loaded_layer.source()!r} is not {target}",
    )
    product = QgsVectorLayer(str(target), "p13 written", "ogr")
    require(product.isValid(), f"the written dataset {target.name} is not a readable layer")
    source = project_layer(layer_id)
    equal(product.featureCount(), source.featureCount(), "the features the written dataset holds")
    with_geometry = sum(
        1
        for feature in product.getFeatures()
        if feature.hasGeometry() and not feature.geometry().isNull()
    )
    equal(with_geometry, source.featureCount(), "the written features that really carry a geometry")
    equal(product.wkbType(), source.wkbType(), "the geometry type the written dataset keeps")

    # QGIS 自己的临时输出：运行结果里是图层 id，图层已在工程里。
    # QGIS's own temporary output: the result value is the layer id and the layer is already in
    # the project.
    temporary = run_algorithm(
        ctx,
        "native:buffer",
        {"INPUT": layer_id, "DISTANCE": 1.0, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
    )
    equal(temporary.status, ExecutionStatus.SUCCEEDED, f"TEMPORARY_OUTPUT: {temporary.error}")
    require(output_layer(ctx, temporary, "OUTPUT").isValid(), "the temporary output layer")

    # 非文件目的地由输出控件先判：GDAL 提供者不支持非文件输出，所以 "memory:" 在参数面板里
    # 就会被拒；插件用的是同一条判据、同一句话。
    # A non-file destination is judged by the output widget first: the GDAL provider does not
    # support non-file outputs, so "memory:" is refused in the panel; the plugin uses the same
    # criterion and the same sentence.
    made_up = run_algorithm(
        ctx, "gdal:buffervectors", {"INPUT": layer_id, "DISTANCE": 1.0, "OUTPUT": "memory:"}
    )
    equal(made_up.status, ExecutionStatus.FAILED, "a memory destination on the gdal provider")
    equal(
        error_code(made_up),
        INVALID_INPUT,
        "the code for a memory destination on the gdal provider",
    )
    equal(
        message_of(made_up),
        "This algorithm only supports disk-based outputs",
        "the memory destination's sentence",
    )
    require(project_layers().get("memory:") is None, "the project holds a layer named 'memory:'")

    # 远程字形不在可写扩展名的判据里：它先过同一条输出控件判据，然后由 QGIS 去开；这个值指向
    # 本机关闭的端口，所以验收的请求出不了这台机器。
    # A remote spelling is not one the writable-extension criterion can judge: it passes the same
    # output-widget criterion and then QGIS opens it; this value names a closed local port, so the
    # acceptance request cannot leave the machine.
    remote = run_algorithm(
        ctx,
        "native:buffer",
        {"INPUT": layer_id, "DISTANCE": 1.0, "OUTPUT": fixtures.REMOTE_VSICURL},
    )
    equal(remote.status, ExecutionStatus.FAILED, "a remote destination")
    remote_code = error_code(remote)
    require(
        remote_code != PATH_NOT_ALLOWED,
        "the plugin still refuses a remote destination before QGIS sees it",
    )

    return (
        "a destination is judged by QGIS's output criterion and then handed to QGIS: "
        f"gdal:convertformat wrote a GeoPackage at the path the caller named "
        f"({target.stat().st_size} bytes) that opens with the source's {product.featureCount()} "
        "features, the source's wkb type and a non-null geometry on every one of them, and the "
        f'outputs value is that same path while the project gains one loaded layer named "'
        f'{loaded_layer.name()}"; native:buffer with OUTPUT="TEMPORARY_OUTPUT" '
        "SUCCEEDED with its result layer in the project; gdal:buffervectors with "
        'OUTPUT="memory:" is FAILED(INVALID_INPUT) with the output criterion\'s own sentence '
        "(the GDAL provider supports disk-based outputs only) instead of QGIS's misleading "
        f'SUCCEEDED with the literal "memory:"; a /vsicurl destination is FAILED({remote_code}) '
        "from QGIS, not PATH_NOT_ALLOWED from the plugin"
    )


def p14_output_ownership(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p14 cities")
    record = run_algorithm(
        ctx,
        "native:buffer",
        {"INPUT": layer_id, "DISTANCE": 0.5, "SEGMENTS": 8, "DISSOLVE": False, "OUTPUT": "memory:"},
    )
    equal(record.status, ExecutionStatus.SUCCEEDED, f"native:buffer failed: {record.error}")
    transferred = outputs_of(record)["OUTPUT"]
    layer = project_layer(transferred)
    equal(layer.id(), transferred, "the id the record reported is the project's own id")

    # 这次运行的 QgsProcessingContext 已经没了，图层必须活得比它长。
    # The run's QgsProcessingContext is gone by now; the layer has to outlive it.
    pump(ctx.app, 0.1)
    equal(project_layer(transferred).featureCount(), 5, "the features after the context was gone")
    info = ctx.run("layer.get_info", {"layer_id": transferred})
    equal(info.status, ExecutionStatus.SUCCEEDED, f"layer.get_info on the result: {info.error}")
    equal(result_of(info)["layer_id"], transferred, "the layer the project resolves the id to")
    return (
        f"the result layer {transferred[:8]}… belongs to the project, still resolves by the id the "
        "record reported after the processing context was destroyed, and layer.get_info reads it"
    )


def p15_non_json_output(ctx: Context) -> str:
    require_processing()
    processing_probe_provider()

    # 探针先交出一个结果图层的 id（不是图层对象），再交一个没有无损 JSON 映像的值；拒绝发生在
    # 结果转 JSON 那一步，工程里不该多出任何东西。
    # The probe hands over a result layer id (not a layer object) and then a value with no
    # lossless JSON image; the refusal happens in the JSON step, and the project must gain
    # nothing.
    before = project_layer_ids()
    record = run_algorithm(ctx, f"{PROBE_PROVIDER}:non_json", {})

    equal(record.status, ExecutionStatus.FAILED, "a result with no lossless JSON image")
    equal(error_code(record), INTERNAL_ERROR, "the code for a result with no JSON image")
    require(record.result is None, "a failed run carries a result")
    require(
        f"{PROBE_PROVIDER}:non_json.processAlgorithm" in PROBE_CALLS,
        f"the plugin refused the run without calling the algorithm: {PROBE_CALLS}",
    )

    message = message_of(record)
    leaks = ("0x", "object at", "<object", "QgsVectorLayer", "\\")
    leaked = [leak for leak in leaks if leak in message]
    require(
        not leaked,
        f"the failure message describes the object or a path ({leaked}): {message!r}",
    )

    equal(project_layer_ids(), before, "the project's layers after the failed run")
    return (
        f"{PROBE_PROVIDER}:non_json handed over a result layer id and then an object; the run "
        f"is FAILED(INTERNAL_ERROR) with no result, the message names neither the object nor a "
        f"path ({message!r}), and the project is still its {len(before)} layers"
    )


def p16_submit_cancel_queued(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p16 cities")
    before = project_layer_ids()

    submitted = ctx.submit(
        "processing.run_algorithm",
        {
            "algorithm_id": "native:buffer",
            "parameters": {"INPUT": layer_id, "DISTANCE": 1.0, "OUTPUT": "memory:"},
        },
    )
    equal(submitted.status, ExecutionStatus.QUEUED, "the status right after submit")
    receipt = ctx.engine.cancel(submitted.execution_id)
    require(receipt.accepted, "the cancel of a queued run was refused")
    equal(receipt.status, ExecutionStatus.CANCELLED, "the status the cancel reported")

    pump(ctx.app, 0.2)
    record = ctx.record(submitted.execution_id)
    equal(record.status, ExecutionStatus.CANCELLED, "the status after the event loop turned")
    require(record.result is None, "a cancelled run carries a result")
    equal(project_layer_ids(), before, "the layers after the cancelled run")
    return (
        "a submitted processing run was QUEUED, cancelled before the event loop reached it, and "
        "stayed CANCELLED with no result and no layer; nothing was executed"
    )


def p17_temporary_output_default(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p17 cities")
    before = project_layer_ids()
    parameters = {"INPUT": layer_id, "DISTANCE": 1.0, "SEGMENTS": 8, "DISSOLVE": False}

    # 参数面板总会给输出参数一个值，默认就是临时输出；插件补的是同一个值。
    # The parameter panel always carries a destination value, a temporary output by default; the
    # plugin fills in the same value.
    record = run_algorithm(ctx, "native:buffer", parameters)
    equal(record.status, ExecutionStatus.SUCCEEDED, f"a run without OUTPUT: {record.error}")
    outputs = outputs_of(record)
    equal(sorted(outputs), ["OUTPUT"], "the output keys")
    layer = output_layer(ctx, record, "OUTPUT")
    equal(layer.featureCount(), 5, "the buffered features")
    require(layer.isTemporary(), "the filled destination is not a temporary output")
    equal(sorted(project_layer_ids()), sorted([*before, layer.id()]), "the project's layers")
    return (
        "native:buffer without OUTPUT SUCCEEDED: the missing destination got a temporary output, "
        f"so the {layer.featureCount()}-feature result layer ({layer.id()[:8]}…) is in the "
        "project; the plugin reports the key it filled"
    )


def p18_parameter_value_refused(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p18 cities")
    before = project_layer_ids()

    # 面板逐参数问 ``checkValueIsAcceptable``，第一句不过的就带它的描述拒掉整次调用。
    # The panel asks ``checkValueIsAcceptable`` per parameter and refuses the whole call with the
    # first description that fails it.
    bad_type = run_algorithm(
        ctx, "native:buffer", {"INPUT": layer_id, "DISTANCE": "abc", "OUTPUT": "memory:"}
    )
    equal(bad_type.status, ExecutionStatus.FAILED, "a numeric parameter given text")
    equal(error_code(bad_type), INVALID_INPUT, "the code for a numeric parameter given text")
    equal(
        message_of(bad_type),
        "Wrong or missing parameter value: Distance",
        "the sentence for a bad number",
    )

    missing = run_algorithm(ctx, "native:buffer", {"DISTANCE": 1.0, "OUTPUT": "memory:"})
    equal(missing.status, ExecutionStatus.FAILED, "a required source left out")
    equal(error_code(missing), INVALID_INPUT, "the code for a required source left out")
    equal(
        message_of(missing),
        "Wrong or missing parameter value: Input layer",
        "the sentence for a missing source",
    )

    equal(project_layer_ids(), before, "the layers after both refusals")

    # PREDICATE 的控件默认值是 [0]；面板会把它一起放进 map，而 QGIS 自己对缺键的 enum 不回落
    # 到默认值，所以这条只有补了默认值才会成功。
    # PREDICATE's widget default is [0]; the panel carries it, and QGIS itself does not fall back
    # to the default for a missing enum key, so this succeeds only once the default is filled.
    first = cities_layer(ctx, "p18 first")
    second = cities_layer(ctx, "p18 second")
    defaulted = run_algorithm(
        ctx,
        "native:extractbylocation",
        {"INPUT": first, "INTERSECT": second, "OUTPUT": "memory:"},
    )
    equal(defaulted.status, ExecutionStatus.SUCCEEDED, f"the filled default: {defaulted.error}")
    require("OUTPUT" in outputs_of(defaulted), "the defaulted run produced no OUTPUT")
    return (
        "a text where native:buffer wants a number and a missing required source are both "
        "FAILED(INVALID_INPUT) with the panel's own sentences "
        f"({message_of(bad_type)!r} and {message_of(missing)!r}), and neither adds a layer; "
        "native:extractbylocation without PREDICATE SUCCEEDED because its widget default [0] was "
        "filled in (QGIS itself refuses the missing key)"
    )


def p19_output_extension_refused(ctx: Context) -> str:
    require_processing()
    layer_id = cities_layer(ctx, "p19 cities")
    target = ctx.fixtures.root / "p19 output.zzz"

    # 输出控件会按提供者的可写格式判目的地；QGIS 自己写的时候会把 .zzz 补成 .gpkg，面板不会。
    # The output widget judges a destination by the provider's writable formats; QGIS itself would
    # append .gpkg to .zzz when writing, the panel does not.
    record = run_algorithm(
        ctx, "native:buffer", {"INPUT": layer_id, "DISTANCE": 1.0, "OUTPUT": str(target)}
    )
    equal(record.status, ExecutionStatus.FAILED, "an unsupported output extension")
    equal(error_code(record), INVALID_INPUT, "the code for an unsupported output extension")
    equal(
        message_of(record),
        "“.zzz” files are not supported as outputs for this algorithm",
        "the sentence for an unsupported output extension",
    )
    written = list(ctx.fixtures.root.glob("p19 output*"))
    require(not written, f"the refused destination still wrote {[path.name for path in written]}")
    return (
        "OUTPUT='p19 output.zzz' is FAILED(INVALID_INPUT) with the output widget's sentence and "
        "writes no file, not even the .zzz.gpkg QGIS would have appended"
    )


def p20_crs_mismatch_question(ctx: Context) -> str:
    require_processing()
    if "gdal:clipvectorbypolygon" not in {algorithm.id() for algorithm in processing_algorithms()}:
        raise Skip(
            "gdal:clipvectorbypolygon is not registered here, so the CRS question cannot be "
            "measured"
        )

    # 夹具里的要素没有几何，而 GDAL 的裁剪需要真的几何：这里直接在工程里搭一个点图层（4326）
    # 和一个面掩膜（32633），两者的 CRS 不一致就是本用例要测的那件事。
    # The fixtures carry no geometry, and GDAL's clip needs real geometry: this case builds a
    # point layer (4326) and a polygon mask (32633) in the project, and their mismatched CRSes are
    # what the case measures.
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer

    points = QgsVectorLayer("Point?crs=EPSG:4326&field=id:integer", "p20 points", "memory")
    provider = points.dataProvider()
    for index in range(3):
        feature = QgsFeature(points.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(index), float(index))))
        feature["id"] = index
        provider.addFeature(feature)
    points.updateExtents()
    QgsProject.instance().addMapLayer(points)

    # 掩膜在 32633 里盖住这些点变换后的位置（约 166000E），所以裁剪会真的留下一层要素。
    # The mask covers where those points land in 32633 (about 166000E), so the clip really keeps
    # features.
    mask = QgsVectorLayer("Polygon?crs=EPSG:32633&field=id:integer", "p20 mask", "memory")
    provider = mask.dataProvider()
    feature = QgsFeature(mask.fields())
    feature.setGeometry(
        QgsGeometry.fromPolygonXY(
            [
                [
                    QgsPointXY(-1_000_000, -1_000_000),
                    QgsPointXY(-1_000_000, 1_000_000),
                    QgsPointXY(1_000_000, 1_000_000),
                    QgsPointXY(1_000_000, -1_000_000),
                ]
            ]
        )
    )
    feature["id"] = 1
    provider.addFeature(feature)
    mask.updateExtents()
    QgsProject.instance().addMapLayer(mask)
    before = project_layer_ids()

    # 参数 CRS 不一致时会问“要继续吗”，默认按钮是“否”；operation 用参数回答同一句。
    # QGIS asks "do you want to continue?" when the parameters' CRSes differ, with "No" as the
    # default button; the operation answers that question with a parameter.
    parameters = {"INPUT": points.id(), "MASK": mask.id()}
    refused = run_algorithm(ctx, "gdal:clipvectorbypolygon", parameters)
    equal(refused.status, ExecutionStatus.FAILED, "a CRS mismatch with no answer")
    equal(error_code(refused), INVALID_INPUT, "the code for a CRS mismatch with no answer")
    equal(
        message_of(refused),
        "Parameters do not all use the same CRS. This can cause unexpected results.\n"
        "Do you want to continue?",
        "the CRS question",
    )
    equal(project_layer_ids(), before, "the layers after the refusal")

    continued = ctx.run(
        "processing.run_algorithm",
        {
            "algorithm_id": "gdal:clipvectorbypolygon",
            "parameters": parameters,
            "continue_on_crs_mismatch": True,
        },
    )
    equal(continued.status, ExecutionStatus.SUCCEEDED, f"the answered run: {continued.error}")
    output = outputs_of(continued)["OUTPUT"]
    require(isinstance(output, str), f"the continued run's OUTPUT is {output!r}, not a path")
    require(Path(output).is_file(), f"the continued run wrote no file at {output!r}")
    written = QgsVectorLayer(output, "p20 written", "ogr")
    require(written.isValid(), f"the continued run's output {output!r} is not a readable layer")

    # 开关关着 = 不问。持久化的是字符串 'false'，只有按 ProcessingConfig 的读法才算关。
    # The switch off means no question.  The persisted value is the string 'false', and it is off
    # only when read the way ProcessingConfig reads it.
    from qgis.core import QgsSettings

    settings = QgsSettings()
    switch = "Processing/Configuration/WARN_UNMATCHING_CRS"
    settings.setValue(switch, "false")
    settings.sync()
    try:
        unchecked = run_algorithm(ctx, "gdal:clipvectorbypolygon", parameters)
        equal(unchecked.status, ExecutionStatus.SUCCEEDED, f"the switch-off run: {unchecked.error}")
        unchecked_output = outputs_of(unchecked)["OUTPUT"]
        require(
            Path(unchecked_output).is_file(),
            f"the switch-off run wrote no file at {unchecked_output!r}",
        )
    finally:
        settings.remove(switch)
        settings.sync()

    return (
        "gdal:clipvectorbypolygon on a 4326 input and a 32633 mask is FAILED(INVALID_INPUT) "
        f"with the question ({message_of(refused)!r}) and adds nothing; the same call "
        f"with continue_on_crs_mismatch=true writes a readable GeoPackage "
        f"({written.featureCount()} feature(s)); with the warning switch persisted as "
        "'false' the same parameters also run, so the question follows that switch and "
        "its answer is a parameter"
    )


PROCESSING_CASES = (
    ("P01", p01_list_algorithms),
    ("P02", p02_every_registered_algorithm_answers),
    ("P03", p03_algorithm_info_fields),
    ("P04", p04_unknown_algorithm),
    ("P05", p05_unknown_key),
    ("P06", p06_buffer_memory_output),
    ("P07", p07_array_input),
    ("P08", p08_scalar_output),
    ("P09", p09_multiple_outputs),
    ("P10", p10_json_values),
    ("P12", p12_input_path_policy),
    ("P13", p13_destination_policy),
    ("P14", p14_output_ownership),
    ("P15", p15_non_json_output),
    ("P16", p16_submit_cancel_queued),
    ("P17", p17_temporary_output_default),
    ("P18", p18_parameter_value_refused),
    ("P19", p19_output_extension_refused),
    ("P20", p20_crs_mismatch_question),
)
