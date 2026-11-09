# QGIS Agent：Processing 与 Layer Symbology Operation 扩展 PRD

> **文档版本：2.0（修订实施基线；2026-11-08）**  
> **运行基准：QGIS Desktop 3.44.13 LTR / Python 3.12.13 / Pydantic 2.x**  
> **文档关系：**本文件基于用户提供的《QGIS Agent 插件开发指南》及上一版 PRD 修订；如与开发指南的冻结契约冲突，以开发指南为准。  
> **验证状态：**已对照 QGIS `final-3_44_13` 官方源码与 PyQGIS 3.44 API；**未在用户 Windows/QGIS 环境完成实际运行验收**。  
> **实现范围：**本文件是开发交付 PRD，不是已经执行的代码变更。

---

### 相较 v1.0 的变更

- **Processing**：去除算法可执行性字段与三算法允许集合；全部当前已注册算法使用同一个运行入口。运行输入由仅限标量改为 JSON 树，结果从单个 `layer_id/layer_name` 改为 `outputs` 映射。
- **Renderer**：新增 `layer.list_available_renderers`；对矢量/栅格 Registry 的不同过滤能力分别规定，不把候选类型误描述为 Setter 已实现类型。
- **安全与验收**：保持原开发指南的路径权限定义与主线程执行契约；明确“全量可请求执行”不等于“全部算法均可在现有文件写权限下成功”，测试总数改为 17 个 Operation。
- **其余 Style**：保留原已确认的五种矢量（含 `RuleRenderer`）及四种栅格结构化 Setter、原生 QML 只读导出、原有错误码和状态机。

---

## 1. 产品目标与实施边界

### 1.1 目标

在不修改既有 Operation / Engine / ExecutionStore / Runner / Plugin 生命周期的前提下，提供两组能力：

1. **Processing**：从当前 QGIS Processing Registry 列出**全部已注册算法**、读取原生参数和输出定义；任意已注册算法 ID 均可通过统一 `processing.run_algorithm` 发起执行，不维护算法 ID 允许名单，也不发布 `runnable` 或其他插件级可执行性筛选字段。
2. **Layer Style**：按图层导出只读 QML Style Categories 与样式 XML；查看当前 Renderer；从 QGIS Registry 列出该图层的 Renderer 候选类型；用结构化参数设置本期实施的基础 Renderer。

`processing.run_algorithm` 的“全量”含义是**统一发现、统一参数传入、统一尝试调度**，不是“QGIS 的每个算法都能接受任意 JSON 参数，且一定执行成功”。原生算法运行条件、特定参数类型、文件访问限制、非 JSON 结果及第三方 Provider 行为仍构成真实约束；遇到限制应返回明确的 `FAILED` 记录，绝不悄悄从列表中移除算法。

首期结构化可设置 Renderer：矢量 `nullSymbol`、`singleSymbol`、`categorizedSymbol`、`graduatedSymbol`、`RuleRenderer`；栅格 `singlebandgray`、`multibandcolor`、`singlebandpseudocolor`、`singlecolor`。其他已注册 Renderer 可被发现，并可通过 `get_renderer` 和 `get_qml` 查看，但不接收未定义的任意结构化 Setter 配置。

### 1.2 非目标与必须说明的限制

- 不创建 HTTP 服务、远程接口、独立进程，也不提供任意脚本执行服务。
- 不提供 QML 文件的创建/保存/加载/编辑，也不允许用一段 QML XML 替换 Renderer。
- 不自行实现 QGIS 全部 Processing 参数定义子类、算法逻辑、Provider 管理器或输入 GUI。
- 不保证所有算法在相同输入下成功、不保证所有 Provider 均已安装/初始化；也不保证第三方 Provider 是无副作用的安全沙箱。
- 不允许为了算法全量覆盖而突破既有 `allowed_data_roots`、`allowed_project_roots` 的既定语义：数据根为读取授权，工程根只用于工程文件读写；**本期没有通用的数据文件写入授权**，故不能承诺文件输出、栅格文件目的地或原地覆盖算法一律可运行。
- 不提供已开始主线程算法的取消和实时进度保证，不增加第三个 ExecutionStrategy。
- 不实现完整 QGIS Symbol Layer 编辑器与全部 Renderer 结构化设置。
- 不承诺失败的 Processing 算法有事务回滚；尤其第三方算法可能已经改变外部环境。

### 1.3 既有开发指南的硬约束

- 名称符合 `^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$`；一个业务领域一个模块，唯一注册入口 `register_all`。
- 全部新增 Operation 使用 `BaseQuery`/`BaseCommand` + `MainThreadOperationMixin`，只实现 `execute_main`；不得给后台纯数据阶段偷偷传 QGIS 对象。
- 输入继承 `Params(extra="forbid")`；输出新 `BaseModel` 均 `extra="forbid"`；Pydantic DTO 只使用 JSON 兼容数据，拒绝 NaN/Infinity。
- `core/*`、`operations/*`、`plugin.py` 能在裸 Python 导入；QGIS/Qt 在业务模块中仅函数体 import；不在 `ui/dock.py` import QGIS。
- 用 `ctx.project`，不在 Operation 内调 `QgsProject.instance()`；不弹任何对话框；Operation 实例无请求相关状态。
- 所有业务预期失败使用 `FrameworkError` 和现有 25 个错误码；脱敏错误正文不得包含输入值、原路径、Provider URI 或原始 traceback。
- 文件 I/O 使用 `core/security.resolve_path`；测试数据、临时目录及部署遵守原指南 §7.6、§9、§12。
- 不改 `core/contracts.py`/`core/engine.py` 公开签名、状态机、Runner、Settings 的现有七项；不装新依赖。

### 1.4 关键设计决策

| ID | 决策 | 理由 |
|---|---|---|
| D1 | 新增 **8 个** Operation，完成后总数 **17** | 不为每个 Processing 算法或 Renderer 注册独立 Operation |
| D2 | Processing 没有 `runnable`、算法允许名单或 Provider 允许名单 | 当前已注册算法均可查询并发起执行；具体失败在调用时报告 |
| D3 | 通用参数使用 QGIS 原生参数名与严格 JSON 树，**不强制标量化** | 支持数组、对象及多输入，保留 QGIS 参数系统表达能力 |
| D4 | 通用结果用 `outputs` JSON 映射 | 支持无结果、标量、多个输出及图层引用，不假定只有一张输出图层 |
| D5 | Processing 仍为 `main_thread`，`destructive=True` | QGIS 算法可具有副作用；`run()` 在主线程使用，不违反 Worker 纯数据约束 |
| D6 | 文件参数沿用读取根白名单；没有通用数据文件写入授权 | 保持开发指南语义，不把读目录暗改为可写目录 |
| D7 | `list_available_renderers` 从 QGIS Registry 动态获取 | 矢量按当前图层过滤；栅格仅能列已注册候选类型 |
| D8 | Style 继续使用只读原生 QML + 强类型 Setter | 不做 QML XML 逆向编辑；复杂读取直接依赖 QML |
| D9 | `RuleRenderer` 本期实现平面规则、表达式与 ELSE | 满足真实可用的规则渲染功能，避免重建全部递归编辑器 |

---

## 2. Operation 清单与外部行为

原有九个 Operation 保持不变；新增 **8 个**：Project 4、Layer 10、Processing 3，总计 **17**。

| Operation | 类名 | kind | strategy | destructive | 作用 |
|---|---|---|---|---|---|
| `processing.list_algorithms` | `ProcessingListAlgorithms` | query | main_thread | false | 列出当前已注册的全部算法 |
| `processing.get_algorithm_info` | `ProcessingGetAlgorithmInfo` | query | main_thread | false | 查询原生参数、输出定义和帮助 |
| `processing.run_algorithm` | `ProcessingRunAlgorithm` | command | main_thread | **true** | 发起任意当前已注册算法的执行 |
| `layer.list_style_categories` | `LayerListStyleCategories` | query | main_thread | false | 当前图层适用的 QML 样式类别 |
| `layer.get_qml` | `LayerGetQml` | query | main_thread | false | 内存导出按 Category 过滤的 QML |
| `layer.list_available_renderers` | `LayerListAvailableRenderers` | query | main_thread | false | 当前图层在 QGIS Registry 中的 Renderer 候选类型 |
| `layer.get_renderer` | `LayerGetRenderer` | query | main_thread | false | 当前活动 Renderer 类型 |
| `layer.set_renderer` | `LayerSetRenderer` | command | main_thread | **true** | 结构化配置并整体替换 Renderer |

`destructive` 只是既有元数据风险标签，Engine 不会自动确认。`processing.run_algorithm` 即使当前某次只读查询也统一声明 `true`，因为其名称代表的一组算法可能修改工程、数据或文件，不能逐请求动态更改 OperationSpec。

`run_sync(name, args)` 同栈运行；`submit(name, args)` 排队后在下一个主线程事件循环轮次运行。**算法一旦开始，当前 Engine 不支持中途取消**；`get()` 仍返回标准 `ExecutionRecord`。不把算法 Registry 误认为 OperationRegistry。

---

## 3. Processing Operation 协议

### 3.1 QGIS 原生事实与通用约束

QGIS 3.44 中：

1. `QgsApplication.processingRegistry().algorithms()` 返回当前已注册的算法；`algorithmById()` 查找算法定义；`createAlgorithmById()` 创建新的可执行实例。实际执行不得直接在 Registry 持有的算法原型上运行。
2. `QgsProcessingAlgorithm` 声明 `parameterDefinitions()`、`outputDefinitions()`、`checkParameterValues()`、`canExecute()` 和 `run()`。但是帮助文档可能为空；`canExecute()` 默认实现返回真；`checkParameterValues()` 的默认实现按已声明参数调用 `checkValueIsAcceptable()`，**不保证拒绝未知键或检查全部隐含副作用**。
3. Processing 参数可能是布尔、数值、字符串、枚举、列表、字典、图层、CRS、几何、文件目标等；算法结果可能包含标量、多个输出、图层引用，或不能直接表示为 JSON 的值。
4. 本插件不是 QGIS Provider 的信任隔离层：对于已安装并注册的本地/第三方算法，调用执行本身可能运行任意受其实现控制的逻辑。只能依赖使用者信任本地 Provider，不承诺安全沙箱。

#### 3.1.1 `processing.list_algorithms`

- 类：`ProcessingListAlgorithms`；`input_model=ProcessingListAlgorithmsParams(Params)`，**空模型**，只接受 `{}`。
- 结果：`ProcessingListAlgorithmsResult`，仅 `algorithms: list[AlgorithmSummary]`。
- `AlgorithmSummary` **三个字段**：

| 字段 | 类型 | 用途 |
|---|---|---|
| `id` | `StrictStr` | 原生算法 ID，供后续查询或执行；来自 `algorithm.id()` |
| `name` | `StrictStr` | 当前环境的显示名，来自 `displayName()` |
| `group` | `StrictStr` | 当前环境的分组显示名，来自 `group()` |

- 不返回 `runnable`、Provider 可用性判断、`count`（可计算）、`provider_id`（算法 ID 中已带 Provider 前缀）或其他实现层能力标记。
- 不过滤“不支持运行”、隐藏或第三方算法；按 `id` 升序排序，空 Registry 返回空数组；不自动启用/安装 Provider。

```json
{
  "algorithms": [
    {"id": "native:buffer", "name": "Buffer", "group": "Vector geometry"},
    {"id": "native:centroids", "name": "Centroids", "group": "Vector geometry"}
  ]
}
```

上面是结构示例，不是对真实注册算法总数及本地化名称的断言。

#### 3.1.2 `processing.get_algorithm_info`

- 类：`ProcessingGetAlgorithmInfo`。
- 输入：`ProcessingGetAlgorithmInfoParams(algorithm_id: StrictStr)`；`algorithm_id` 非空，按原生 ID 查找，未知 ID → `FAILED(INVALID_INPUT)`。
- 输出：`ProcessingGetAlgorithmInfoResult`：

| 字段 | 类型 | 意义 |
|---|---|---|
| `algorithm` | `AlgorithmSummary` | 与列表共享同一摘要，避免重复 id/name/group |
| `description` | `StrictStr` | 算法 `shortHelpString()`，没有就为 `""` |
| `parameters` | `list[AlgorithmParameter]` | `parameterDefinitions()` 原顺序，含 Destination |
| `outputs` | `list[AlgorithmOutput]` | `outputDefinitions()` 原顺序；不是实际运行结果 |

`AlgorithmParameter`：

| 字段 | 类型 | 数据来源及必须保留的理由 |
|---|---|---|
| `name` | `StrictStr` | 原生参数键；运行时原样使用 |
| `type` | `StrictStr` | `definition.type()`；解析输入的基本类型线索 |
| `description` | `StrictStr` | `definition.description()`；操作说明 |
| `optional` | `StrictBool` | `FlagOptional`；表示定义层是否可选，不承诺所有参数组合均可省略 |
| `default` | `JsonValue` | `defaultValue()` 中能无损转换为 JSON 的值，无法转换或未设定时 `null` |
| `help` | `StrictStr` | `definition.help()`，空帮助返回 `""`；不能凭空补说明 |
| `is_destination` | `StrictBool` | `definition.isDestination()`；区分普通输入和潜在文件/图层输出目标 |
| `depends_on` | `list[StrictStr]` | `dependsOnOtherParameters()`；供构造字段等依赖参数 |
| `options` | `list[StrictStr] \| None` | 仅枚举参数为显示选项，其他类型 null；顺序即原生枚举顺序 |

`AlgorithmOutput`：`name: StrictStr`、`type: StrictStr`、`description: StrictStr`。这三项来自每个 `QgsProcessingOutputDefinition`；无需增加与运行结果重复的输出路径字段。

处理细则：

- 默认值中的 JSON 对象和数组可以保留，但每个子值必须是有效 JSON；QGIS/Qt 对象、枚举实例等不可直接表达为 JSON 时返回 `null`，**不能**做 `str(obj)` 伪序列化。
- `default=null` 可能代表无默认值，也可能代表原生默认值为 null；不为此再造“是否有默认值”标志。复杂参数的实际接受形态以原生 QGIS 帮助和真实验证为准。
- `description`/`help` 原生为空时如实返回空字符串，不生成误导性的说明。
- 不返回全部 `metadata()`、环境变量、外部依赖诊断或大段非结构化参数表。

### 3.2 `processing.run_algorithm`

#### 3.2.1 类型与输入输出协议

- 类：`ProcessingRunAlgorithm`，`BaseCommand` + `MainThreadOperationMixin`，`destructive=True`。
- 输入模型 `ProcessingRunAlgorithmParams`：

| 字段 | 类型 | 必需 | 说明 |
|---|---|---|---|
| `algorithm_id` | `StrictStr`，非空 | 是 | 当前 Registry 中算法的原生 ID，不限制 Provider / ID 名单 |
| `parameters` | `dict[str, Any]` | 是 | 使用原生参数名的 JSON 对象；支持由有限 JSON 标量、数组和对象组成的递归值树，**但拒绝 Python/QGIS/Qt 对象** |

没有 `output_name`、`destination`、`provider`、`runnable`、`load_results` 等额外字段；各算法具体目的地直接放在它自己的原生 `parameters` 内。

输出模型 `ProcessingRunAlgorithmResult`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `algorithm_id` | `StrictStr` | 实际执行的原生算法 ID；`ExecutionRecord.operation` 只有 Operation 名称，故不可省略 |
| `outputs` | `dict[str, JsonValue]` | 原生输出键映射到可序列化 JSON 值；无输出算法允许 `{}`；多个结果按原生键保留 |

`JsonValue`：`null / bool / int / 有限 float / str / list[JsonValue] / dict[str, JsonValue]` 的递归联合；字典键必须为字符串。由 Pydantic 模型的递归校验拒绝 `NaN`、`Infinity`、`QgsMapLayer`、`QVariant` 对象、QObject、Qt Enum、bytes 或任意实例；禁止 `str(value)`、`repr(value)` 作为万能回退。

JSON 输入校验只检查载荷形状和有限性。具体字段允许值应由当前算法的原生参数定义以及对应的 `checkParameterValues()` 判断；插件只做必要的对象引用、文件路径和 Destination 边界处理。对未知参数名先与 `parameterDefinitions()` 集合核对，未知键返回 `INVALID_INPUT`，不能假装 QGIS 会拒绝它。

#### 3.2.2 参数传入约定

| 参数形态 | 调用方值 | 插件行为 |
|---|---|---|
| 数值 / 布尔 / 文本 / 枚举 | QGIS 接受的 JSON 标量 | 保持原生值，交给对应参数定义和算法进一步校验；不自行推断宽松类型 |
| 多选枚举 / 字符串列表 / 数值范围 / 矩阵 | JSON 数组 | 保持数组结构，使用原生参数验证；不强制转换成逗号分隔字符串 |
| 原生可接受的复合参数 | JSON 对象或数组 | 不猜测未定义 schema；只在 QGIS 原生支持该形态且通过校验时执行 |
| 当前工程图层引用 | 图层 ID 字符串，或列表中的图层 ID 字符串 | 按当前 `ctx.project` 的图层引用解析与验证；不得将未知字符串悄悄当作外部 URI 加载 |
| CRS / 表达式 / extent 等 | QGIS 原生支持的 JSON 字符串等 | 尊重 QGIS 参数原生表示；不在插件中另造第二套语法 |
| 输入文件 | 当前 `allowed_data_roots` 内的绝对本地文件路径 | 仅对原生定义明确表示文件输入时用 `resolve_path(..., must_exist=True)`；URL、UNC、VSI、未知连接 URI 拒绝 |
| 输出 Destination | 原生目标参数（如 `OUTPUT`） | **原值交给 QGIS**：`memory:`、普通路径、`TEMPORARY_OUTPUT`、`""` 都照传，写不写、写哪里由调用方与原生参数定义决定；插件只拒非字符串值。唯一的例外：值为 `memory:` 而 `algorithm.provider().supportsNonFileBasedOutput()` 为假时拒（`INVALID_INPUT`），因为实测这种组合会返回“成功”但只有字面量 `memory:`、没有图层 |
| 磁盘写入或文件型 Destination | 文件路径、数据库目标、`TEMPORARY_OUTPUT` 等 | **放行**（2026-11 用户裁决：不要「只允许 `memory:`」这条规则）；值原样交给 QGIS，由原生校验与算法决定结果。写授权模型另行设计（§12.1） |

**关于通用图层字符串的歧义：**某些算法接受 `layer_id`，也可能接受数据源字符串。当前工程中能精确找到的图层 ID 优先解析为现有对象；不能命中的字符串不得无检查地交给 QGIS 作为待加载的 Provider URI。对于算法定义能够识别的普通本地输入文件，先按数据根策略验证再交给 QGIS；其他源一律拒绝。不能完全判断的参数类型应在执行前拒绝，而不是绕过安全边界。

**关于默认/遗漏的 Destination：**不自动填充 `OUTPUT` 或创建临时文件。缺省值与 `None` 一律当作「未提供」留给原生 `checkParameterValues()` 判断（必填时它会拒绝）；调用方显式给出的任何字符串目标（含 `TEMPORARY_OUTPUT`）原样传下。插件不替算法选择临时文件，也不把缺省值悄悄换成 `memory:`。

示例 1：内存矢量结果（QGIS 原生参数名，示例不表示写死了 Buffer 白名单）：

```json
{
  "algorithm_id": "native:buffer",
  "parameters": {
    "INPUT": "roads_layer_id",
    "DISTANCE": 100.0,
    "SEGMENTS": 8,
    "DISSOLVE": false,
    "OUTPUT": "memory:"
  }
}
```

示例 2：多层输入数组：

```json
{
  "algorithm_id": "native:mergevectorlayers",
  "parameters": {
    "LAYERS": ["layer_id_a", "layer_id_b"],
    "OUTPUT": "memory:"
  }
}
```

示例 3：无文件结果的算法可只有非 Destination 参数，具体是否合规由其定义与校验结果决定；插件不得强行附加一个不存在的 `OUTPUT` 键。

#### 3.2.3 调用顺序（禁止算法白名单）

1. Engine 按现有入口创建记录并完成 Pydantic 输入校验；Operation 再校验 JSON 树、有限性、字典键是否为字符串。
2. `registry = QgsApplication.processingRegistry()`；用 `registry.algorithmById(id)` 确认算法存在；然后用 `registry.createAlgorithmById(id)` 得到**独立的新算法实例**；创建失败返回可预期失败，不对注册表原型调用 `run()`。
3. 调用新实例 `canExecute()`，若返回 `False` 则以 `FAILED(INVALID_INPUT)` 表示当前执行条件不满足；不将这一判断写入算法列表或持久状态。
4. 按 `parameterDefinitions()` 检查请求键：不存在的键 `INVALID_INPUT`；根据定义区分图层引用、纯值、输入文件和目标输出；保留定义的参数依赖信息并交由 QGIS 校验；禁止传入任意 QObject、远程 URI、未知本地路径或未获授权的写入目标。
5. 创建 `QgsProcessingContext()` 并 `setProject(ctx.project)`，创建 `QgsProcessingFeedback()`；明确使用当前工程作为图层 ID 的解析上下文，不调用 `QgsProject.instance()`。
6. 使用 `algorithm.checkParameterValues(resolved_parameters, context)`；正确处理其 `(bool, message)` 返回值，拒绝时 `FAILED(INVALID_INPUT)`；`message` 只留诊断日志，不能原样进入执行记录。
7. 在 QGIS 主线程使用 `algorithm.run(resolved_parameters, context, feedback)`；正确解包 `(results: dict, ok: bool)`。`ok=False` 时按失败语义处理，绝不伪造成功。
8. 对 `results` 做严格 JSON 归一化。若结果值指向 `context.temporaryLayerStore()` 中的图层，则按 `takeResultLayer(id)` 取走并加入 `ctx.project`，返回稳定的图层 ID；已有工程图层返回已有 ID。不需要图层的标量、数组、对象保持其 JSON 结构，不把对象字符串化。
9. 对成功结果构造 `ProcessingRunAlgorithmResult(algorithm_id=id, outputs=...)` 并返回。结果归一化过程中不能产生失效图层 ID、非有限数或连接 URI。

**输出所有权和失败边界：**只有已确认的图层 ID 才可从 Processing Context 转交工程。并且 QGIS Provider 可能不支持非文件输出（`supportsNonFileBasedOutput()` 为假时 Destination 会禁止相应形式），这属于调用时的原生约束，不能通过硬编码 `memory:` 绕过。不要对任意字符串调用 `takeResultLayer` 后又默默把它当普通字符串；操作可先检查 Context 的临时图层存储。已转移的多个结果图层在后续阶段发生失败时，原则上仅移除本次新加入的图层；对算法已写入的外部文件、数据库变动或工程修改不承诺自动回滚。`outputs` 中未知的不可序列化对象要明确失败，不能返回 Python 对象地址描述。

**失败与主线程阻塞：**`run_sync` 同栈运行；`submit` 排队后在主线程运行。已开始的长算法不能通过现有 Engine 中途取消，GUI 可能暂时无响应；不向后台 `execute_worker` 传 `QgsProcessingContext` 或任何 QGIS 对象。输入验证前置检查不改变工程，但实际 `run()` 可能发生副作用；`FAILED` 不等于事务回滚。

#### 3.2.4 通用执行的可用性定义（必须写入产品说明）

**保证：**当前注册算法 ID 都可列出、查询其原生参数定义并通过同一个 `processing.run_algorithm` 入口请求运行；不会因为不存在静态允许清单而在入口拒绝某个 Provider。

**不保证：**每个算法都能靠纯 JSON 完整表达所有参数、每个算法都有帮助文字、所有算法都能生成内存输出、所有算法都是无副作用的、所有返回值都天然 JSON 可序列化、第三方 Provider 没有外部访问、文件型目标在本期无需额外授权就可写入。

**执行拒绝的含义：**如某个算法因必要文件写入、参数对象不可表达或 Provider 环境缺失而失败，该算法仍保留在列表，`get_algorithm_info` 仍能读取其定义；这是明确的能力边界，不得静默将其视为未注册。当前文件访问规则限制了实际可完成的算法集合；若未来要求“无条件运行全部文件处理算法”，必须**先评审现有文件写授权和运行隔离设计**，不能在这一份 PRD 中绕开冻结的 Settings/安全契约。

---

## 4. Style Category 与 QML 读取

### 4.1 QGIS 的 18 个 Style Categories

QGIS `QgsMapLayer.StyleCategory` 的 18 个基本枚举名称为：

`LayerConfiguration`、`Symbology`、`Symbology3D`、`Labeling`、`Fields`、`Forms`、`Actions`、`MapTips`、`Diagrams`、`AttributeTable`、`Rendering`、`CustomProperties`、`GeometryOptions`、`Relations`、`Temporal`、`Legend`、`Elevation`、`Notes`。

`AllStyleCategories`、`AllVisualStyleCategories` 和 `AllAttributeCategories` 是**组合标志**，不作为单独可选择类别。

**类别适用性指“此类型图层具有对应的 QML 导出分支或语义”，不指当前 XML 一定有非空配置。** 例如无标注配置的图层仍然可能支持 `Labeling`；不应为了判断类别而重复导出 18 份 XML，再按节点非空来反推类别。

### 4.2 `layer.list_style_categories`

- 输入：`LayerListStyleCategoriesParams(layer_id: StrictStr)`。
- 输出：`LayerListStyleCategoriesResult(layer_id: str, categories: list[StyleCategoryName])`。
- 本期适用于当前工程中的有效、空间矢量图层（Point/Line/Polygon）和有效栅格图层；不适用的图层类型返回 `INVALID_LAYER`。
- 类别列表按 §4.1 所列固定枚举顺序输出，不按当前图层有没有标签、标注或备注而增删。

**空间矢量图层返回 18 类。**

**栅格图层返回 10 类：**

`LayerConfiguration`、`Symbology`、`Labeling`、`MapTips`、`AttributeTable`、`Rendering`、`CustomProperties`、`Temporal`、`Elevation`、`Notes`。

这里将 `AttributeTable` 纳入栅格，是因为 3.44.13 的 `QgsRasterLayer::writeSymbology()` 确实在设置 `AttributeTable` 标志时导出外部栅格属性表相关路径；`Labeling` 也有实际写入分支。`Symbology` 与 `Rendering` 则共享同一段 Raster Pipe 导出逻辑。此名单只承诺本插件的两种图层范围，不代表所有 QGIS `QgsMapLayer` 子类的完整支持矩阵。

示例：

```json
{"layer_id": "raster_layer_id"}
```

输出的 `categories` 只包含 Category 名字；不返回 `editable`、`supported`、`description`、`layer_type` 等不必要字段。是否提供某类别的 setter 是另一个独立问题。

### 4.3 `layer.get_qml`

输入 `LayerGetQmlParams`：

| 字段 | 类型 | 必需 | 默认 | 规则 |
|---|---|---|---|---|
| `layer_id` | `StrictStr` | 是 | — | 当前工程中存在的有效矢量或栅格图层 ID |
| `categories` | `list[StyleCategoryName] \| None` | 否 | null | null = 该图层 `list_style_categories` 的全部适用类别；指定数组必须非空、无重复，所有元素在其适用列表内 |

输出 `LayerGetQmlResult`：`layer_id: str`、`qml: str`。不返回重复的请求 Category 列表；QML 根节点本身包含 `styleCategories` 属性，且 Raster Pipe 可能跨类别输出，不能把重复的入参列表误描述为“XML 内部确实只包含这些类别”。

QGIS 3.44 PyQGIS 原生 API：

```python
from qgis.PyQt.QtXml import QDomDocument
from qgis.core import QgsMapLayer, QgsReadWriteContext

# 以下仅为函数体内的核心调用示例：
doc = QDomDocument()
message, ok = layer.exportNamedStyle(doc, QgsReadWriteContext(), categories_flag)
if not ok:
    raise FrameworkError(INTERNAL_ERROR, "layer style export failed")
qml = doc.toString()
```

**重要返回签名：** 3.44 Python 绑定的 `exportNamedStyle(...)` 返回 **`(str, bool)`**，不是单个字符串，也不是输出 XML 本身。输出 XML 由传入的 `QDomDocument` 提供。`message` 不能原样回传，避免泄漏路径等信息。

- 多个类别使用 `QgsMapLayer.StyleCategories` 的位标志按位组合。
- 不使用 `saveNamedStyle()`、`loadNamedStyle()`、`QgsMapLayerStyle.readFromLayer()` 或自行拼装 `<renderer-v2>` XML。
- 返回 `doc.toString()` 的原生完整 XML，不执行 XML 节点裁剪，也不假设特定属性顺序。
- 栅格 QGIS 源码明确：请求 `Symbology` **或** `Rendering` 都会导出 Raster Pipe；该耦合属于 QGIS 3.44 行为，不能视为实现缺陷。
- 只读取已加载的图层样式，不读任意 QML 文件，也不接受直接 XML 修改指令。
- 返回 QML 可能包含符号资源引用路径，属于对当前工程已有样式的只读披露，不可把该接口描述为通用脱敏导出器。

错误：图层不存在 `LAYER_NOT_FOUND`，图层类型/有效性错误 `INVALID_LAYER`，类别非法 `INVALID_INPUT`，原生导出异常或失败 `INTERNAL_ERROR`。

---

## 5. Renderer 基础协议

### 5.1 支持类型识别范围

`layer.get_renderer` 必须对 QGIS 3.44 当前已有的类型返回其原生 `renderer.type()` 字符串；不需构造对应 Renderer。

**矢量 12 类**：

1. `nullSymbol`
2. `singleSymbol`
3. `categorizedSymbol`
4. `graduatedSymbol`
5. `RuleRenderer`（注意大小写）
6. `pointDisplacement`
7. `pointCluster`
8. `heatmapRenderer`
9. `mergedFeatureRenderer`
10. `invertedPolygonRenderer`
11. `25dRenderer`
12. `embeddedSymbol`

**栅格 8 类**：

1. `singlebandgray`
2. `multibandcolor`（纠正原始需求中重复的第二个 `singlebandgray`）
3. `paletted`
4. `singlebandpseudocolor`
5. `singlebandcolordata`
6. `hillshade`
7. `contour`
8. `singlecolor`

如果第三方 Renderer 实际返回其他 `type()` 字符串，查询仍原样返回，不构造或尝试写入该类型；上述 20 类是 QGIS 3.44 内置识别和验收覆盖的基线，而不是封闭整个插件生态的假定。

### 5.2 `layer.list_available_renderers`

- `name`: `layer.list_available_renderers`（`available` 拼写固定，不能用 `avaiable`）。
- `input_model`: `LayerListAvailableRenderersParams(layer_id: StrictStr)`，`layer_id` 非空。
- `output_model`: `LayerListAvailableRenderersResult(renderer_types: list[StrictStr])`；**仅一个输出字段**，不重复返回 `layer_id`、`count`、`layer_type`、`editable`、`supported`。
- `kind=query`、`strategy=main_thread`、`destructive=False`。
- 适用范围与 `layer.get_renderer` 一致：当前工程中有效的 Point/Line/Polygon 矢量图层和有效栅格图层；其他图层返回 `INVALID_LAYER`。

**取值与语义（重要）**：

| 图层类型 | 原生 API | `renderer_types` 的准确含义 |
|---|---|---|
| 矢量 | `QgsApplication.rendererRegistry().renderersList(layer)` | QGIS Registry 根据当前矢量图层筛选的几何兼容 Renderer 类型；也可包括第三方注册项 |
| 栅格 | `QgsApplication.rasterRendererRegistry().renderersList()` | 当前环境注册的栅格 Renderer 类型；QGIS 3.44 **没有按具体栅格图层筛选**的对应重载 |

- 不维护一份静态的 12/8 类型集合来充当返回值。获取原生字符串、去重、按字符串升序输出，保持结果可比较；QGIS 原生返回已经唯一时不额外处理。
- 列表只表达当前 QGIS 注册表中的**候选类型**。它不承诺相应 Renderer 已被 `layer.set_renderer` 实现，也不承诺栅格当前波段/数据类型满足某种渲染方式的具体参数约束。实际设置仍由 Setter 严格校验。
- 不在结果中增加表示插件实现进度的布尔字段。

示例：

```json
{
  "renderer_types": ["RuleRenderer", "categorizedSymbol", "graduatedSymbol", "nullSymbol", "singleSymbol"]
}
```

示例仅展示部分可能值；真实结果必须来自实时 Registry，不能写死上述五项。

### 5.3 `layer.get_renderer`

- 输入：`LayerGetRendererParams(layer_id: StrictStr)`。
- 输出：`LayerGetRendererResult(layer_id: str, renderer_type: str)`。
- 只允许空间矢量和栅格图层；分别读取 `QgsVectorLayer.renderer().type()` 或 `QgsRasterLayer.renderer().type()`。
- 若图层存在但当前无 Renderer，`renderer_type` 返回 `""`，不触发自动创建；这与后续 Setter 可接受的具体类型名有意区分。
- 不返回 `config`、`editable`、`layer_type`、`symbol` 或能力名单。所有现有完整样式信息统一通过 `layer.get_qml(categories=["Symbology"])` 获取，避免只读配置与 QML 信息重复或在复杂符号上失真。

### 5.4 `layer.set_renderer`

- 输入：`LayerSetRendererParams(layer_id: StrictStr, renderer: RendererConfig)`。
- `RendererConfig` 为以 `type` 字符串为判别字段的 Pydantic 联合模型；每一个成员 `extra="forbid"`。
- 输出：`LayerSetRendererResult(layer_id: str, renderer_type: str)`；使用安装后对象的实际 `type()` 值，不额外返回整个输入配置。
- 替换 Renderer 会使原先未保存的符号设置丢失；`destructive=True`，**不向用户弹确认对话框**。

**一次性提交规范：**

1. 输入模型纯校验先通过；再确认 `ctx.project.mapLayer()` 中的图层存在、有效且类型匹配。
2. 校验所有字段、波段、颜色、表达式及业务组合关系。
3. 在主线程完整构造新 Renderer，构造期间不调用现有图层的 `setRenderer()`。
4. 只在新 Renderer 构造成功后一次性调用 `layer.setRenderer(new_renderer)`，由图层接管所有权；之后不得当作独立、长期持有的 Python 对象使用。
5. 必要时 `layer.triggerRepaint()`，并确保 `ctx.project` 的 dirty 状态与修改相符；操作不直接保存工程。
6. 失败不能将现有 Renderer 部分修改或替换为 null。成功结果与 `layer.get_renderer` 一致。

### 5.5 通用基础 Symbol（矢量）

本期三种 Symbol 配置均采用 `type` 判别字段，使用 `QgsMarkerSymbol.createSimple()`、`QgsLineSymbol.createSimple()`、`QgsFillSymbol.createSimple()` 构造，**不接收 Symbol Layer XML、SVG 文件路径或任意 QGIS 属性字典**。

| 模型（`type`） | 必填参数 | 可选参数及默认 | 用途 |
|---|---|---|---|
| `MarkerSymbolConfig` (`marker`) | `color` | `size_mm=2.0` | Point，固定圆形 marker |
| `LineSymbolConfig` (`line`) | `color` | `width_mm=0.3` | Line，固定实线 |
| `FillSymbolConfig` (`fill`) | `color` | `outline_color="#333333"`、`outline_width_mm=0.3` | Polygon，固定纯色填充 + 简单边框 |

- `color`、`outline_color` 必须符合 `^#[0-9A-Fa-f]{6}$`；无 alpha 通道支持。
- `size_mm`、`width_mm`、`outline_width_mm` 必须是有限数；marker size、line width >0；fill outline width >0（首期不提供隐藏边框的单独选项）。
- 仅支持几何类型 Point/Line/Polygon（包含 Multi*），调用时自动根据 `layer.geometryType()` 校验 Symbol type。
- 不暴露 Symbol 的内部 Qt 单位枚举；本期一律使用毫米。
- **不支持**自定义 Symbol Layer、多层符号、透明度参数、数据定义符号字段或表达式。现有复杂样式可以用 QML 原样读取，但 Setter 是显式整体替换，不保证还原这些配置。

---

## 6. 矢量 Renderer 具体配置（首期 5 类）

### 6.1 `nullSymbol`

配置模型 `NullSymbolRendererConfig`：仅一个字段 `type: Literal["nullSymbol"]`。

```json
{"layer_id":"polygon_layer_id", "renderer":{"type":"nullSymbol"}}
```

构造 `QgsNullSymbolRenderer`。该 Renderer 不绘制常规要素符号，但不等同于隐藏图层，也不主动修改 Labeling、Diagrams 等类别。

### 6.2 `singleSymbol`

配置模型 `SingleSymbolRendererConfig`：`type: Literal["singleSymbol"]`、`symbol: SimpleSymbolConfig`。

```json
{
  "layer_id":"parcels_id",
  "renderer":{
    "type":"singleSymbol",
    "symbol":{"type":"fill","color":"#6CAC83","outline_color":"#275E40","outline_width_mm":0.3}
  }
}
```

构造 `QgsSingleSymbolRenderer(symbol)`；根据图层几何类型拒绝不匹配 Symbol。默认不修改 LayerConfiguration、Labeling、3D Symbology 和其他类别配置。

### 6.3 `categorizedSymbol`

配置模型：

```text
CategorizedRendererConfig
  type: Literal["categorizedSymbol"]
  field: StrictStr
  categories: list[CategoryConfig]  # 至少 1 项

CategoryConfig
  value: str | int | finite-float | bool | None
  label: StrictStr
  symbol: SimpleSymbolConfig
```

```json
{
  "layer_id":"landuse_id",
  "renderer":{
    "type":"categorizedSymbol",
    "field":"landuse",
    "categories":[
      {"value":"forest","label":"Forest","symbol":{"type":"fill","color":"#398F59"}},
      {"value":"water","label":"Water","symbol":{"type":"fill","color":"#419ACB"}}
    ]
  }
}
```

- `field` 必须存在且非空，本期仅字段名，不支持表达式作为分类键。
- 各 `value` 必须能安全用于该字段的 QVariant 类型；同字段类型归一化后不得重复。数值必须有限；允许 null 表示空值类别，但最多一个。
- `label` 必须非空；每一项都含同几何类型的简单 Symbol。
- 按数组顺序创建 `QgsRendererCategory`，交给 `QgsCategorizedSymbolRenderer`。不自动枚举唯一值、不自动插入“其他”兜底类别；未匹配类别使用 QGIS 原生分类渲染行为，不在插件中生成默认 Symbol。

### 6.4 `graduatedSymbol`

配置模型：

```text
GraduatedRendererConfig
  type: Literal["graduatedSymbol"]
  field: StrictStr
  ranges: list[RangeConfig]  # 至少 1 项

RangeConfig
  lower: finite-float
  upper: finite-float
  label: StrictStr
  symbol: SimpleSymbolConfig
```

```json
{
  "layer_id":"population_id",
  "renderer":{
    "type":"graduatedSymbol",
    "field":"population",
    "ranges":[
      {"lower":0,"upper":1000,"label":"Low","symbol":{"type":"fill","color":"#CFE5B5"}},
      {"lower":1000,"upper":5000,"label":"High","symbol":{"type":"fill","color":"#468B46"}}
    ]
  }
}
```

- 字段必须存在并是 QGIS 可判定的数值型（整数、浮点等）；无效字段使用既有 `INVALID_FIELD_TYPE`。
- 所有 `lower < upper`，范围按 `lower` 升序且不能内部重叠；允许相邻边界共用相同端点。
- 不自动计算等距、分位数或自然断点；精确分级值由调用方提供。
- 使用 `QgsRendererRange` 与 `QgsGraduatedSymbolRenderer` 创建原生按颜色分级 Renderer。共有边界的包含语义遵从 QGIS 3.44 原生判定，不自行重新实现比较逻辑。

### 6.5 `RuleRenderer`（首期必须交付）

**完整支持平面规则组，不支持递归嵌套。** 这是明确的业务范围，不得以“类型已识别”代替实际 Setter 实现。

模型：

```text
RuleRendererConfig
  type: Literal["RuleRenderer"]
  rules: list[RuleConfig]   # 至少 1 项

RuleConfig
  label: StrictStr       # 非空
  filter: StrictStr | None = None
  else_rule: StrictBool = False
  symbol: SimpleSymbolConfig
```

规则约束：

1. 普通规则：`else_rule=false`，`filter` 为非空的合法 QGIS Expression；每一条都配置一个简单 Symbol。
2. ELSE 规则：`else_rule=true`，`filter` 必须为 null；最多一条，且必须是数组末项。
3. 规则列表保持用户给定顺序；不同普通规则**可以同时匹配**一个要素，QGIS 将按规则渲染语义处理，不能把它错误当成 `if/elif` 排他链。
4. 每条 Expression 用 `QgsExpression` 检查解析有效性；仅验证语法，不承诺 Expression 的运行期结果非空或始终成功。表达式不得作为 Python 代码执行。
5. 本期所有规则默认为启用；统一适用于所有比例尺。**不支持**子规则树、比例尺上下限、描述字段、停用规则、符号层级设置；如果需要这些能力，应在既有模型上增加明确字段，而不是接收 QML XML。

示例：

```json
{
  "layer_id":"roads_id",
  "renderer":{
    "type":"RuleRenderer",
    "rules":[
      {"label":"Main roads", "filter":"\"class\" = 'primary'", "symbol":{"type":"line","color":"#D95843","width_mm":0.8}},
      {"label":"Other roads", "else_rule":true, "symbol":{"type":"line","color":"#888888","width_mm":0.25}}
    ]
  }
}
```

原生映射：使用 `QgsRuleBasedRenderer.Rule(None)` 创建根节点；每个元素映射到 `QgsRuleBasedRenderer.Rule(symbol, filterExp=..., label=..., elseRule=...)` 并 `appendChild`；最后构造 `QgsRuleBasedRenderer(root)`。**不要**使用便利构造器创建带默认子规则的 Renderer 后再继续附加规则，否则可能出现意外的全匹配默认规则。需通过真实 QGIS 验收确认规则数量、ELSE 标志、表达式与 QML 一致。

### 6.6 矢量剩余 7 类的非实施范围

`pointDisplacement`、`pointCluster`、`heatmapRenderer`、`mergedFeatureRenderer`、`invertedPolygonRenderer`、`25dRenderer`、`embeddedSymbol`：本期仅可由 `get_renderer` 识别、通过 `get_qml` 导出；在 `set_renderer` 作为输入类型时拒绝 `INVALID_INPUT`。如后续新增，对应业务模型直接加入 `RendererConfig` 联合类型，不增加新 Operation。

---

## 7. 栅格 Renderer 具体配置（首期 4 类）

栅格 Renderer 不用 `QgsSymbol`：它将栅格波段数据转换为显示颜色。所有 `band` / `*_band` 均采用 **1-based** 编号，必须在 `[1, layer.bandCount()]` 范围内。Renderer 构造时从当前图层取得 `dataProvider()` / Raster Interface，交给 `QgsRasterLayer.setRenderer()` 管理，不存活到请求之外。

### 7.1 `singlebandgray`

```text
SingleBandGrayConfig
  type: Literal["singlebandgray"]
  band: StrictInt          # >=1
  min: finite-float
  max: finite-float
```

示例：

```json
{"layer_id":"dem_id","renderer":{"type":"singlebandgray","band":1,"min":0,"max":2500}}
```

`min < max`；通过 `QgsSingleBandGrayRenderer` + `QgsContrastEnhancement` 使用 StretchToMinimumMaximum，将最小值映射为黑、最大值映射为白，不扫描整幅栅格求统计，也不暴露更多对比度算法选项。

### 7.2 `multibandcolor`

```text
MultiBandColorConfig
  type: Literal["multibandcolor"]
  red_band: StrictInt
  green_band: StrictInt
  blue_band: StrictInt
```

示例：

```json
{"layer_id":"satellite_id","renderer":{"type":"multibandcolor","red_band":4,"green_band":3,"blue_band":2}}
```

- 三个 band 均须有效；**允许重复**同一波段。
- 使用 `QgsMultiBandColorRenderer(provider, red, green, blue)`；不自动扫描数据范围、计算百分比拉伸，也不承诺 16-bit 影像在无对比度增强时达到最佳视觉效果。
- 后续有真实需求再为各通道增加显式拉伸配置，不在第一版增加三组仅用于猜测的默认参数。

### 7.3 `singlebandpseudocolor`

```text
PseudoColorConfig
  type: Literal["singlebandpseudocolor"]
  band: StrictInt
  interpolation: Literal["linear", "discrete", "exact"] = "linear"
  stops: list[ColorStop]  # 至少 2 个

ColorStop
  value: finite-float
  color: ColorHex
```

```json
{
  "layer_id":"dem_id",
  "renderer":{
    "type":"singlebandpseudocolor",
    "band":1,
    "interpolation":"linear",
    "stops":[
      {"value":0,"color":"#2665B8"},
      {"value":1000,"color":"#F2E9A4"},
      {"value":2500,"color":"#B44A39"}
    ]
  }
}
```

要求值严格递增、不重复，颜色格式同 §5.5。原生使用 `QgsColorRampShader.ColorRampItem`、`QgsColorRampShader`、`QgsRasterShader` 与 `QgsSingleBandPseudoColorRenderer`。`linear/discrete/exact` 映射到 QGIS 3.44 `Qgis.ShaderInterpolationMethod` 的对应枚举值；不自动生成分级点、直方图或统计值。超出停靠点范围时沿用 QGIS Shader 的原生行为，不引入私有的自定义剪裁算法。

### 7.4 `singlecolor`

```text
SingleColorRasterConfig
  type: Literal["singlecolor"]
  band: StrictInt
  color: ColorHex
```

```json
{"layer_id":"mask_id","renderer":{"type":"singlecolor","band":1,"color":"#CC6633"}}
```

使用 `QgsRasterSingleColorRenderer(provider, band, QColor(color))`；仅绘制有效数据像元，NoData 行为交给 QGIS。`singlecolor` 是 QGIS 3.38 起存在的独立 Renderer 类型，不应与 `singlebandcolordata` 混淆。

### 7.5 栅格剩余 4 类的非实施范围

`paletted`、`singlebandcolordata`、`hillshade`、`contour` 本期仅可识别与 QML 只读导出，不能通过 `set_renderer` 创建。未来扩展：分别添加类别色表、单波段已有颜色数据、山体阴影参数、实时等值线参数的配置模型；不得与 Processing 生成新图层的算法混为一谈。

---


## 8. 跨操作规则、错误码与安全

### 8.1 字段审查（所有公开字段均应有独立意义）

| 模型 / 操作 | 保留字段 | 不增加的字段及理由 |
|---|---|---|
| `AlgorithmSummary` | `id/name/group` | 无 `runnable`、Provider 白名单、`count`（可计算） |
| `AlgorithmParameter` | `name/type/description/optional/default/help/is_destination/depends_on/options` | 无 `min/max` 等仅适用于部分参数的通用假字段，具体语义交原生定义 |
| `AlgorithmOutput` | `name/type/description` | 不虚构统一文件路径 |
| `ProcessingRunAlgorithmParams` | `algorithm_id/parameters` | 无单图层 `output_name`、重复 `provider_id`、运行能力布尔 |
| `ProcessingRunAlgorithmResult` | `algorithm_id/outputs` | 不假设单结果图层、不复制输出 `count`、不返回 Qt/QGIS 对象 |
| `LayerListStyleCategoriesResult` | `layer_id/categories` | 不添加 `editable`、`layer_type` 等与类别定义无关的字段 |
| `LayerGetQmlResult` | `layer_id/qml` | 不重复返回入参 Category，不承诺 QML 节点严格类别隔离 |
| `LayerListAvailableRenderersResult` | `renderer_types` | 不重复返回输入 `layer_id`，不增加 `editable`、`supported`、`count` |
| `LayerGetRendererResult` | `layer_id/renderer_type` | 详细内容由原生 QML 提供，不做有损结构化回读 |
| `LayerSetRendererResult` | `layer_id/renderer_type` | 只确认实际成功安装的原生 Renderer 类型 |

### 8.2 错误码（只复用原有 25 个）

| 情形 | 推荐码 | 精确语义 |
|---|---|---|
| 不存在的插件 Operation | `OPERATION_NOT_FOUND` | 只限 Engine/Registry 的 Operation 名称错误 |
| 不存在的 Processing 算法 ID | `INVALID_INPUT` | 是有效 Operation 调用中的业务输入错误 |
| 算法 `canExecute=False`、未知参数键、参数原生校验失败 | `INVALID_INPUT` | 不声称 QGIS 所有运行时错误都可预先检测 |
| 输入图层 ID 不存在 | `LAYER_NOT_FOUND` | 与错误的算法 ID 区分 |
| 图层无效、图层类型不适用 | `INVALID_LAYER` | 使用现有层级错误语义 |
| 分类字段不存在、字段类型不符合分级 | `INVALID_FIELD_TYPE` | 延用既有数值字段判定语义 |
| 不允许的输入路径、远程 URI、隐式加载的 Provider 连接串 | `PATH_NOT_ALLOWED` | 记录的 `error.message` 不含原始路径或 URI。**输出 Destination 不再是这个码**（2026-11 裁决后原样透传） |
| 非法 Renderer 配置、未知 Style Category、Selector 不能接受的 Renderer 类型 | `INVALID_INPUT` | 设置失败不替换旧 Renderer |
| QGIS 算法执行失败、非 JSON 结果、未知内部异常 | `INTERNAL_ERROR` | 当前词表无 Processing 专用运行失败码；诊断日志允许原始异常，仅内部可见 |
| 返回值不满足声明结果模型 | `OUTPUT_VALIDATION_FAILED` | Engine 输出校验负责；不要用它掩盖已预见的非 JSON 结果 |

禁止新增错误码、修改控制面/记录面区别、让 `TASK_SUBMISSION_FAILED` 表示主线程算法失败、或让失败信息泄漏用户数据。对已经开始运行的算法，`INTERNAL_ERROR` **不表示**副作用已回滚。

### 8.3 文件与执行信任边界

1. **当前工程已有层**：通过 ID 解析，输入层若有本地文件来源且当前访问策略要求审查，使用 `QgsProviderRegistry.decodeUri()` 或现有项目数据源校验约定识别底层本地路径；不能简单按 `|layername` 或 `:` 拆字符串作为最终访问判据。
2. **新增文件输入**：只有明确可识别的文件输入参数可以读取文件，先由 `resolve_path` + `allowed_data_roots` 验证；空根列表时拒绝。远端、UNC、VSI、未知 Provider 连接串不得隐式加载。
3. **新增文件输出**：**放行**（2026-11 用户裁决：先保证可执行，写授权模型另行设计）。Destination 的值（磁盘路径、`TEMPORARY_OUTPUT`、`memory:`）原样交给 QGIS，插件不做写路径审批；`memory:` 在 Provider 不支持非文件输出时拒（避免假成功）。需要收紧时先改设计（§12.1 第二行），再改实现与测试。
4. **算法内部行为**：对任意第三方/自定义算法，注册表不能证明其内部只访问声明的文件参数。插件只提供**进程内受信任代码调用**，不声称恶意 Provider 不可读写任何文件；对不可信 Provider 应在 QGIS 级别禁用，而不是假装本 Operation 是隔离沙箱。
5. **QGIS 临时目录**：处理算法可能使用内部缓存或文件作为实现细节，此类内部临时 I/O 不能仅凭参数检查完全排除；“不允许文件输出”指用户请求的结果文件 Destination，不等于保证所有内部算法完全无文件 I/O。
6. **QML**：始终从当前工程的已加载图层导出内存 XML，未经批准不将其当脱敏资源。路径、资源引用可能出现在 XML 中。
7. **Rule Expression**：使用 QGIS 表达式解析和计算，不视作 Python/SQL 任意代码解释器；原生表达式函数与第三方扩展不构成安全沙箱。

---

## 9. 代码落点与模块边界

```text
src/qgis_agent_plugin/
├── core/                        # 不改 frozen 契约、状态机、Runner、Settings
├── operations/
│   ├── __init__.py             # 注册：4 project + 10 layer + 3 processing
│   ├── project.py              # 保持不变
│   ├── layer.py                # 新增 5 个 layer.*，仅添加实际使用的私有构造函数
│   └── processing.py           # 新增 3 个 processing.*，通用原生参数/结果转换
├── plugin.py                   # 不增加基于 Operation 名称的特判
└── ui/
    └── dock.py                # 继续基于 input_schema 自动生成表单

tests/
├── test_operations_layer.py   # 扩展 Style、候选 Renderer 与 Setter
├── test_operations_processing.py # 新增：元数据、通用参数/结果、拒绝路径
├── test_package.py            # 9 → 17；名称集合及 processing 模块裸导入
└── qgis_integration/
    ├── cases.py               # 真 QGIS Registry、run、Renderer、QML 验收
    └── fixtures.py            # 现场生成临时 GPKG、GeoTIFF、矢量等
```

- `operations.processing` 只处理参数 JSON 边界、QGIS 原生执行、结果归一化及当前工程生命周期；不再维护三个算法专用允许集合或每算法 if/elif 大分派。
- 允许少量有实际复用需求的私有辅助函数，如 `_json_value`、`_resolve_parameter`、`_normalise_results`；不创建新的 Provider Registry 或独立 Processing 引擎。
- `operations.layer` 的 Renderer 模型仍采用已有 `type` 判别联合；只扩充真正落地的分支。新增 `list_available_renderers` 只读 Registry，不用自己硬编码图层适用清单。
- 新模块同样列入 `tests/test_package.py` 导入列表；新增 Operation 数量和名字断言同步更新；`metadata.txt`、docs 和开发指南中所有旧计数同步改为最新值。
- `ui/dock.py` 的 object/array → JSON 文本行为继续使用；不按 Operation 名字加逻辑分支。

---

## 10. 详细验收矩阵

### 10.1 Processing 发现、定义与运行

| ID | 场景 | 必须验证的行为 |
|---|---|---|
| P01 | 空参数列出算法 | 返回 `registry.algorithms()` 的全部 ID，升序、不按执行能力过滤；第三方已注册项也在其中 |
| P02 | 原生和第三方 Provider | 无算法白名单；所有存在的算法均可请求运行，运行失败作为执行记录反馈 |
| P03 | 算法信息 | 参数含 `is_destination`/`depends_on`，有空 help 时如实返回，枚举选项有顺序 |
| P04 | 不存在的算法 | `FAILED(INVALID_INPUT)`，不会触发 `OPERATION_NOT_FOUND` |
| P05 | 未知键 | `FAILED(INVALID_INPUT)`，`checkParameterValues` 未拒绝时插件仍要拒绝 |
| P06 | Buffer：矢量内存输出 | `OUTPUT="memory:"`；原生结果图层加入项目，结果 ID 在 Operation 返回后仍有效 |
| P07 | 多层输入/数组 | 真实多图层算法接受 JSON list，按原生输出定义返回结果 |
| P08 | 无文件的标量输出算法 | 返回数值、字符串、布尔等有限 JSON 结果；不强制单图层字段 |
| P09 | 多输出算法 | 输出映射的多个键被完整保留，不假设固定 `OUTPUT` 键 |
| P10 | 嵌套 JSON 值 | 允许 QGIS 参数定义接受的对象/数组；拒绝 QObject、Qt 枚举、NaN/Inf |
| P11 | 无法执行 / Provider 缺失 | `canExecute=False` 返回 FAILED；不自动安装新 Provider |
| P12 | 输入路径限制 | 数据根外、远程 URI 或不允许的连接串在调用算法前拒绝 |
| P13 | 文件输出/临时磁盘输出 | 磁盘路径与 `TEMPORARY_OUTPUT` 都交给 QGIS 并真的写出数据（文件可开、要素数与几何与源一致）；`memory:` 在无内存输出的 Provider 上 `FAILED(INVALID_INPUT)`，不允许假成功 |
| P14 | 输出所有权 | Context 临时图层转项目、Context 析构后图层仍可按 ID 查询 |
| P15 | 不可序列化结果 | 显式失败，不返回对象 `str`/`repr`；不声称已有外部副作用已回滚 |
| P16 | `submit` 主线程队列取消 | 在事件循环真正执行前取消为 `CANCELLED`；运行开始后不可中断 |
| P17 | 无显式 Destination 的算法 | 不强行补 `OUTPUT`；只有满足原生定义且不隐式文件输出时才执行 |

验收要求同时包含：成功的 Native 算法、非 Native Provider 在当前环境真实可用的例子、至少一种数组输入、标量输出以及一次真实的文件目标写入。若目标机器未安装某第三方 Provider，相关用例明确 `SKIP` 并记录原因，不能把 SKIP 计为 PASS；Native 核心用例必须实际通过。

### 10.2 QML Category 与只读 XML

| ID | 场景 | 必须验证的行为 |
|---|---|---|
| S01 | 有效空间矢量图层 | 18 个基础 Category 名称，固定顺序 |
| S02 | 有效栅格图层 | 返回 §4.2 的 10 类，包括 `Labeling` 与 `AttributeTable` |
| S03 | 空、未知、重复 Category | `FAILED(INVALID_INPUT)`，没有半完成结果 |
| S04 | 仅 `Symbology` 导出 | QGIS `exportNamedStyle` 在内存生成有效 XML，不创建 `.qml` 文件 |
| S05 | 多类别与栅格管线 | 遵从原生位标志，接受 Symbology/Rendering 重叠 |
| S06 | QML 导出失败 | 正确解包 `(message, ok)`，失败只回传脱敏 `INTERNAL_ERROR` |

### 10.3 `layer.list_available_renderers` 与 Renderer 读取

| ID | 场景 | 必须验证的行为 |
|---|---|---|
| A01 | Point/Line/Polygon 矢量 | 每次调用 `rendererRegistry().renderersList(layer)`，以原生候选结果为基准 |
| A02 | 栅格 | 使用 `rasterRendererRegistry().renderersList()`；不伪称按 band 动态筛选 |
| A03 | 已注册第三方 Renderer | 返回当前注册项；不以 Setter 已实现类型进行过滤 |
| A04 | 错误图层 ID 或类型 | 分别 `LAYER_NOT_FOUND` / `INVALID_LAYER` |
| A05 | 结果稳定 | 输出去重、按名字排序；只有 `renderer_types` 字段 |
| A06 | `get_renderer` | 仍只返回真实活动 type；复杂类型用 `get_qml` 读取完整配置 |

### 10.4 矢量 Renderer 设置（五类）

| ID | 场景 | 必须验证的行为 |
|---|---|---|
| V01 | `nullSymbol` | 原生 Renderer 安装成功，不改变图层树可见性 |
| V02 | `singleSymbol` Point/Line/Polygon | 几何与简单 Symbol 匹配，颜色、尺寸正确 |
| V03 | `categorizedSymbol` | 字段、分类顺序、标签及 Symbol 与 QML 相符 |
| V04 | `graduatedSymbol` | 分级范围及符号正确，非法重叠范围拒绝 |
| V05 | `RuleRenderer` 平面规则 | 两条以上合法规则、表达式、顺序均正确 |
| V06 | `RuleRenderer` ELSE | 只有一条 ELSE、位于最后，QML 不出现意外默认规则 |
| V07 | 非法表达式 / 多个 ELSE | `FAILED(INVALID_INPUT)`；旧 Renderer 保持不变 |
| V08 | 不支持的 Setter 类型或 Symbol 不匹配 | 明确拒绝；`list_available_renderers` 仍可列出该类型候选 |

### 10.5 栅格 Renderer 设置（四类）

| ID | 场景 | 必须验证的行为 |
|---|---|---|
| R01 | `singlebandgray` | band 和 contrast stretch 参数正确 |
| R02 | `multibandcolor` | RGB band 对应正确，可重复引用同一 band |
| R03 | `singlebandpseudocolor` | 插值、停靠点、色彩正确；非法排序拒绝 |
| R04 | `singlecolor` | 原生类型、band、color 正确 |
| R05 | band 越界、非法颜色、非有限数 | 全部拒绝，旧 Renderer 未变 |

### 10.6 框架、测试环境和部署回归

- 单测覆盖新增输入/输出模型的严格类型、JSON 递归校验、`extra="forbid"`、错误码、注册表、新模块裸解释器导入守卫；Engine 保持控制面异常与 `FAILED` 执行记录的差异。
- `tests/test_package.py` 从 9 个改为恰好 17 个（`project` 4、`layer` 10、`processing` 3），同步名字集合；不修改已有任何 Engine 状态转移。
- QGIS 验收以 `final-3_44_13` 实际安装为准，使用一次性 Profile、现场生成的小型 GPKG/GeoTIFF/内存数据；不碰操作者工程，运行时禁止安装或升级依赖。
- 测试失败输出 FAIL 和有效退出码；不可运行的测试 SKIP 不能算作 PASS；`npm run lint`、`npm run test:unit`、QGIS 集成验收均须真实通过。
- `npm run deploy` 只对目标部署目录镜像；先执行 `deploy:list` 可检查拷贝集；部署后重启 QGIS、检查 Dock 的 JSON 参数编辑与结果显示。

---

## 11. 交付物、完成标准与扩展路径

### 11.1 本轮交付

- 三个 Processing Operation：不维护算法可执行性字段或静态白名单；`parameters` 与 `outputs` 采用通用 JSON 契约。
- 五个 Layer Style Operation：`list_style_categories`、`get_qml`、`list_available_renderers`、`get_renderer`、`set_renderer`。
- 五种矢量与四种栅格 Renderer 的结构化 Setter，尤其 `RuleRenderer` 的 flat rules + ELSE。
- 注册、元数据、测试、真实 QGIS 验收和文档同步更新；新增模块遵守裸 Python 导入约束。
- 不新增第三方依赖、不增加新的 Engine 策略与 Settings 键、不修改现有 Operation 名称。

### 11.2 完成标准

1. 全部当前已注册算法均能被列出并查询声明，且不存在任何按算法 ID/Provider 维护的允许名单；执行入口对任何已注册 ID 均可进入统一检查和尝试执行流程。
2. 至少具备可验证的数组输入、多输出/标量输出及内存图层输出路径；文件写入请求在无授权情况下明确失败。**不把“每一个已注册算法都实际运行成功”作为本轮虚假承诺**。
3. `layer.list_available_renderers` 正确对接矢量/栅格的不同 Registry API；列表不被 Setter 支持范围过滤。
4. 五矢量 + 四栅格可编辑 Renderer 全部通过真实 QGIS 验收；RuleRenderer 的表达式、ELSE 和规则顺序可证明。
5. QML 只读导出真实有效，不提供 QML 文件写入/加载接口；工程与已有模块的回归测试无退化。
6. `test_package.py` 全集恰好 17 个，文档、`metadata.txt` 和代码注册同步，Lint/单测/真实验收无 FAIL。

### 11.3 后续扩展方向（不预建抽象）

- 文件类 Processing 算法大规模可用：先批准新增的数据写入授权/覆盖策略及临时输出边界；再扩展 `run_algorithm` 的文件目标处理，不需要改 Operation 名称。
- 需要主线程无阻塞、执行中取消：另行评审 `QgsProcessingAlgRunnerTask` / 原生 prepare、runPrepared、postProcess 的调度方案，不得直接把 QGIS 对象放进原 `execute_worker`。
- Renderer：在既有 `RendererConfig` 中按实际需求增加 PointCluster、Heatmap、RuleRenderer 嵌套与比例尺、Raster Hillshade/Paletted 等，不新增重复 Operation。

---

## 12. 设计复核与未覆盖风险

| 检查项目 | 结论 |
|---|---|
| 算法列表是否全量 | 是，以当前 `processingRegistry().algorithms()` 为准，不过滤 |
| 是否存在插件算法允许名单、`runnable` 或类似阶段标志 | **否**，三处协议均不包含此类字段 |
| 是否保证所有算法无条件执行成功 | **否**，QGIS 算法注册并不证明参数 JSON 完备、外部依赖与文件写入已被授权 |
| 是否允许任意 JSON 值进入 QGIS | **否**，递归检查 JSON 类型、拒绝未知键、处理路径/来源，再执行 QGIS 原生参数验证 |
| 是否修正固定单图层输出 | 是，`outputs` 通用 JSON 映射、支持 Context 临时图层所有权转移 |
| Renderer 列表是否使用官方 Registry | 是，矢量按图层，栅格全注册表候选，不虚构栅格逐层过滤 |
| 是否加入 `RuleRenderer` Setter | 是，flat rules、表达式、ELSE 具体约束不回退 |
| 是否保持冻结的 Engine / Runner / Settings | 是，无第三个策略、无新错误码、无新 Settings |
| 是否防止主线程操作冒充后台取消 | 是，明确执行开始后不可中断 |
| 是否把 Processing 描述为安全沙箱 | 否，明确第三方 Provider 和算法内部 I/O 的信任边界 |
| 是否完成目标 QGIS 3.44.13 的运行验收 | **是**（2026-11）：本机 3.44.13 上 99 条验收用例 PASS 96 / FAIL 0 / SKIP 3，见 `tests/qgis_integration/` 与 `docs/delegate/99-v2-final.md` |

**不得隐藏的技术风险**：

- 原生 `checkParameterValues()` 不会替插件检验未知输入键，也不会沙箱化 `processAlgorithm()`；`canExecute()` 默认真不能视为安全放行证明。
- QGIS 支持的参数对象范围超出严格 JSON；对无法无损表示的请求可以明确失败，但不创建隐式任意对象恢复器。
- 现有路径配置没有针对数据文件的写入授权，因此不可能在**不改变开发指南配置语义**的情况下保证所有文件输出型算法运行成功。
- Output 可能引用 Context 临时图层，丢失所有权会导致返回无效 Layer ID；成功前必须验证对象转移与存活。
- 原生 Processing 算法/第三方 Provider 可能修改当前工程或外部环境，失败无法保证回滚；`destructive=True` 是风险信号而非防护机制。
- 栅格 Registry 不具有按 `QgsRasterLayer` 检测可用类型的重载；调用方必须在设置时处理实际波段/数据类型限制。

### 12.1 已批准的权限边界（v2 首期，用户裁决）

本期优先级：**先让已注册算法都能被请求执行；权限模型（数据写入、数据库连接、远程 URI、出网）单独立项，届时按行收窄**。因此下列拒绝面是**有意保留的边界，不是缺陷清单**；在权限设计落地前不再新增拒绝规则。

| 边界 | 实测范围（本机 3.44.13，440 个算法） | 当前行为 | 权限设计时的方向 |
| --- | --- | --- | --- |
| 数据库/远程写目标写在普通参数里 | 11 个算法、20 个定义：`gdal:importvectorintopostgisdatabase{available,new}connection`、`native:importintopostgis`、`native:postgisexecutesql`、`native:spatialiteexecutesql`、`native:spatialiteexecutesqlregistered`、`qgis:importintospatialite`、`qgis:postgisexecuteandloadsql`（`DATABASE/DBNAME/HOST/PORT/USER/PASSWORD/SCHEMA/TABLE`） | 连接型参数（`providerconnection/databaseschema/databasetable`）拒绝；`vector` 型 `DATABASE` 与 `string` 型 `HOST/DBNAME/USER/PASSWORD/SCHEMA` **放行**（`native:spatialiteexecutesql`、`qgis:importintospatialite`、`gdal:…newconnection` 可执行，实测能写入） | 建「连接/写目标」授权模型；需先能区分读写再决定是否收窄 |
| 结果文件写盘 | 现在放行（2026-11 裁决）：`gdal` 57 / `pdal` 17 无内存输出，358 个算法带必填 Destination | 磁盘路径与 `TEMPORARY_OUTPUT` 原值交 QGIS；仅 `memory:` 在无内存输出的 Provider 上拒 | 已无写授权限制；需要时再设计写入审批（新 Settings 键或按次确认） |
| 输入数据根 | 全部文件输入 | 只允许 `allowed_data_roots` 内的绝对本地文件；远程 URI / UNC / `/vsi*` 拒绝（严格类型族与未知类型） | 是否需要扩展读授权 |
| 复合参数里的图层引用 | 5 个类型、7 个算法：`qgis:idwinterpolation`、`qgis:tininterpolation`、`native:alignrasters`、`native:dxfexport`、`native:writevectortiles_mbtiles`、`native:writevectortiles_xyz`、`native:tinmeshcreation` | 只要提供值即 `INVALID_INPUT`（插件不逐类型解析嵌套图层） | 逐类型解析后可放开 |
| 文件夹型 `file` 参数 | `native:importphotos`、`native:atlaslayouttoimage`、`native:atlaslayouttomultiplepdf` | `must_exist=True` 只接受普通文件，目录被拒 | 读授权是否包含目录 |
| 值型参数里的 URL | `native:httprequest`、`native:openurl` 等 `string`/`enum` 参数 | 放行（按类型豁免），算法会真发网络请求 | 出网授权 |

「全量可请求执行」仍按 §3.2.4 的口径理解：入口不按算法 ID/Provider 过滤，不等于每个算法都能在当前授权下跑完。

### 12.2 实施勘误（以目标安装的绑定为最终依据）

本机 QGIS 3.44.13（PyQt5 / Python 3.12.13）实测与本文件正文不符的四处，实现与验收均按实测写：

| # | 正文写法 | 实测 |
| --- | --- | --- |
| E1 | §3.2.3 把 `canExecute()` 当 `bool` | 绑定返回 `(bool, message)` 元组（440/440） |
| E2 | §4.3 / §10.2 S06 写 `message, ok = exportNamedStyle(...)` | 返回 `str`，空串 = 成功，XML 写入传入的 `QDomDocument` |
| E3 | §5.2 / §10.3 A02 写 `QgsApplication.rasterRendererRegistry()` | Python 绑定 `SIP_SKIP`；只能 `QgsRasterRendererRegistry()` 实例（8 个内置），第三方栅格 Renderer 不可观测 → A03 只对矢量成立 |
| E4 | 未描述同 id 图层加入语义 | `QgsProject.addMapLayer()` 重复 id 返回 `None`、不加入、不改名，按失败处理 |
| E5 | §7.3 用 `Qgis.ShaderInterpolationMethod` 设插值 | `QgsColorRampShader` 只有 `setColorRampType()`；两枚举数值相同（0/1/2） |

---

## 13. 研究依据与版本锁定

### 13.1 项目要求

- 用户提供的《QGIS Agent 插件开发指南》，重点 §4（分层与裸导入）、§6（协议/状态机/线程与生命周期）、§7（新增 Operation 与路径安全）、§8（完整新增流程）、§9（单测/真实验收）、§11（分支）和 §12（已知边界）。本 PRD 中的接口和取舍属于**本项目设计决定**，不是 QGIS 原生 Operation 接口。

### 13.2 QGIS 3.44.13 官方源码

- [QGIS `final-3_44_13` 源码树](https://github.com/qgis/QGIS/tree/final-3_44_13)
- [`qgsprocessingalgorithm.cpp`：`canExecute`、`checkParameterValues`、`run`](https://github.com/qgis/QGIS/blob/final-3_44_13/src/core/processing/qgsprocessingalgorithm.cpp)
- [`qgsprocessingregistry.cpp`：算法查找、创建、Provider 注册](https://github.com/qgis/QGIS/blob/final-3_44_13/src/core/processing/qgsprocessingregistry.cpp)
- [`qgsrendererregistry.cpp`：矢量 Renderer 兼容类型过滤](https://github.com/qgis/QGIS/blob/final-3_44_13/src/core/symbology/qgsrendererregistry.cpp)
- [`qgsrasterrendererregistry.cpp`：栅格 Renderer 候选注册表](https://github.com/qgis/QGIS/blob/final-3_44_13/src/core/raster/qgsrasterrendererregistry.cpp)
- [`qgsmaplayer.cpp`：`exportNamedStyle` 与 QML Style Categories](https://github.com/qgis/QGIS/blob/final-3_44_13/src/core/qgsmaplayer.cpp)
- [`qgsvectorlayer.cpp`：矢量符号样式导出](https://github.com/qgis/QGIS/blob/final-3_44_13/src/core/vector/qgsvectorlayer.cpp)
- [`qgsrasterlayer.cpp`：栅格样式导出及 Symbology/Rendering 耦合](https://github.com/qgis/QGIS/blob/final-3_44_13/src/core/raster/qgsrasterlayer.cpp)

### 13.3 PyQGIS 3.44 API 和官方用户文档

- [QgsProcessingRegistry](https://qgis.org/pyqgis/3.44/core/QgsProcessingRegistry.html)
- [QgsProcessingAlgorithm](https://qgis.org/pyqgis/3.44/core/QgsProcessingAlgorithm.html)
- [QgsProcessingParameterDefinition](https://qgis.org/pyqgis/3.44/core/QgsProcessingParameterDefinition.html)
- [QgsProcessingContext](https://qgis.org/pyqgis/3.44/core/QgsProcessingContext.html)
- [QgsRendererRegistry](https://qgis.org/pyqgis/3.44/core/QgsRendererRegistry.html)
- [QgsRasterRendererRegistry](https://qgis.org/pyqgis/3.44/core/QgsRasterRendererRegistry.html)
- [QgsMapLayer](https://qgis.org/pyqgis/3.44/core/QgsMapLayer.html)
- [QgsRuleBasedRenderer](https://qgis.org/pyqgis/3.44/core/QgsRuleBasedRenderer.html)
- [QgsColorRampShader](https://qgis.org/pyqgis/3.44/core/QgsColorRampShader.html)
- [QGIS `qgis_process` JSON 输入说明](https://docs.qgis.org/3.44/en/docs/user_manual/processing/standalone.html)

**版本范围说明：**此 PRD 锁定 QGIS 3.44.13；上述 `qgis.org/pyqgis/3.44` 链接属于 3.44 API 系列文档，落地实现以 `final-3_44_13` 标签和目标安装的 PyQGIS 绑定为最终依据。源码查阅不等于完成 Windows QGIS 真机执行验收。
