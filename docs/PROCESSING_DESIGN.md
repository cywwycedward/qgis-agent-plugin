# Processing 三个操作的简化版 MVP 设计方案

**目标版本：QGIS 3.44**

## 1. 设计目标

本次只完成三个 Processing 操作的重构：

- `processing.list_algorithms`
- `processing.get_algorithm_info`
- `processing.run_algorithm`

核心原则：尽可能使用 QGIS 已有能力，删除插件自己重复实现的逻辑，先跑通整个流程，观察实际效果。

本次确定以下设计：

| 操作 | 设计 |
|---|---|
| `list_algorithms` | 保持现有实现 |
| `get_algorithm_info` | 参考 `algorithmHelp()` 源码，直接调用相关对象方法，组装 JSON |
| `run_algorithm` | 使用 `qgis.processing.run()` 执行，只传入自定义 Feedback |
| Feedback | 继承 `QgsProcessingFeedback`，覆写七种消息方法，调用基类并记录消息 |
| 参数检查 | 完全交给 QGIS，不保留插件自己的参数检查 |
| Processing Context | 不手动创建，由 QGIS 内部创建 |

不考虑兼容旧接口，不增加额外抽象，不设计后续扩展机制。

---

## 2. `processing.list_algorithms`

保持当前实现。

继续使用：

```python
QgsApplication.processingRegistry().algorithms()
```

获取当前 QGIS 已注册的全部算法。

不增加算法白名单、过滤规则或其他算法管理逻辑。

---

## 3. `processing.get_algorithm_info`

### 3.1 设计思路

不调用：

```python
qgis.processing.algorithmHelp(algorithm_id)
```

原因是 QGIS 3.44 的这个方法只通过 `print()` 输出内容，不返回任何数据。

直接参考 `algorithmHelp()` 的源码，使用其中组织帮助信息时调用的对象方法，自己组装 JSON。

**不复制它的 print 逻辑，只复用它获取信息的逻辑。**

### 3.2 算法信息获取

首先从 Processing 注册表中获取算法：

```python
registry = QgsApplication.processingRegistry()

algorithm = registry.algorithmById(algorithm_id)

if algorithm is None:
    # 返回参数错误
    ...
```

然后读取算法基本信息：

```python
algorithm.displayName()
algorithm.id()
algorithm.shortDescription()
algorithm.shortHelpString()
```

### 3.3 参数信息获取

遍历：

```python
algorithm.parameterDefinitions()
```

每个参数读取以下信息：

| JSON 字段 | QGIS 方法或来源 |
|---|---|
| `name` | `parameter.name()` |
| `description` | `parameter.description()` |
| `help` | `parameter.help()` |
| `type` | `parameter.__class__.__name__` |
| `options` | 枚举参数的 `parameter.options()` |
| `accepted_types` | 参数类型对象的 `acceptedPythonTypes()` |

其中：

**隐藏参数**

按照 `algorithmHelp()` 的行为，跳过设置了 `FlagHidden` 的参数。

**枚举参数**

只有 `QgsProcessingParameterEnum` 才提供 `options`。

**接受的数据类型**

先通过：

```python
parameter_type = registry.parameterType(parameter.type())
```

获取参数类型对象，再调用：

```python
parameter_type.acceptedPythonTypes()
```

如果没有对应的参数类型对象，则返回空列表。

不再额外获取以下信息：

- 是否可选。
- 默认值。
- 是否为目的参数。
- 参数依赖关系。

这些信息不是 `algorithmHelp()` 当前输出内容的一部分，本次不额外扩展。

### 3.4 输出信息获取

遍历：

```python
algorithm.outputDefinitions()
```

每个输出读取：

| JSON 字段 | QGIS 方法 |
|---|---|
| `name` | `output.name()` |
| `type` | `output.__class__.__name__` |
| `description` | `output.description()` |

### 3.5 最终 JSON 结构

统一返回：

```json
{
  "algorithm": {
    "id": "native:buffer",
    "name": "Buffer",
    "short_description": "",
    "help": ""
  },
  "parameters": [
    {
      "name": "INPUT",
      "description": "Input layer",
      "help": "",
      "type": "QgsProcessingParameterFeatureSource",
      "options": null,
      "accepted_types": [
        "QgsVectorLayer",
        "QgsMapLayer",
        "str"
      ]
    }
  ],
  "outputs": [
    {
      "name": "OUTPUT",
      "type": "QgsProcessingOutputVectorLayer",
      "description": "Buffered layer"
    }
  ]
}
```

以上仅展示结构示例，实际字段内容完全从 QGIS 算法定义中获取。

约定：

- 没有描述或帮助时返回空字符串。
- 非枚举参数的 `options` 返回 `null`。
- 没有可接受数据类型时返回空列表。
- 不存在的算法返回现有参数错误。

最终由现有接口的数据模型负责将该字典转换成 JSON。

### 3.6 本次实现范围

只做上述信息的读取和组装。

不实现帮助文本，不调用标准输出，不编写帮助文本解析器，也不增加其他参数信息。

---

## 4. Processing Feedback

### 4.1 设计目标

保留原来的 `ProcessingMessage` 数据模型。

新增或调整 `ProcessingRunFeedback`，使其同时完成两件事：

1. 使用 QGIS 基类的功能，正常写入 QGIS 日志和 Feedback 自身日志。
2. 将消息保存到自己的 `records` 列表，供接口读取。

### 4.2 初始化方式

```python
class ProcessingRunFeedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.records = []
```

这里不传 `logFeedback=False`。

QGIS 3.44 的构造函数默认：

```python
logFeedback = True
```

因此，基类原有的日志功能继续生效。

### 4.3 七种消息全部覆写

所有方法统一采用：

**先调用基类方法，再记录自己的结构化消息。**

例如：

```python
def pushInfo(self, info: str) -> None:
    super().pushInfo(info)
    self._record("info", info)
```

其他方法按照同样的方式实现。

| 方法 | 基类调用 | 自己记录的 level |
|---|---|---|
| `pushInfo` | `super().pushInfo(info)` | `info` |
| `pushDebugInfo` | `super().pushDebugInfo(info)` | `debug` |
| `pushCommandInfo` | `super().pushCommandInfo(info)` | `command` |
| `pushConsoleInfo` | `super().pushConsoleInfo(info)` | `console` |
| `pushFormattedMessage` | `super().pushFormattedMessage(html, text)` | `formatted` |
| `pushWarning` | `super().pushWarning(warning)` | `warning` |
| `reportError` | `super().reportError(message, fatalError)` | `error` |

其中：

- `pushFormattedMessage()` 的结构化记录只保存纯文本 `text`。
- `reportError()` 保留 `fatalError`，写入消息的 `fatal` 字段。
- 其他消息的 `fatal` 统一为 `False`。

### 4.4 消息记录

统一使用：

```python
def _record(
    self,
    level: str,
    text: str,
    fatal: bool = False,
) -> None:
    self.records.append(
        ProcessingMessage(
            level=level,
            text=text,
            fatal=fatal,
        )
    )
```

读取接口仍然只保留：

- `records`
- `errors`
- `has_errors`

不额外增加消息处理功能。

### 4.5 基类日志行为

本次明确保留 QGIS Feedback 基类的全部正常行为：

- 写入 QGIS 全局消息日志。
- 写入基类 HTML 日志。
- 写入基类纯文本日志。
- 保留 QGIS 原有日志数量控制。

自己的 `records` 只是额外保存一份结构化消息，不替代基类日志。

`setProgressText()` 不需要覆写，继续使用基类实现。

### 4.6 类定义位置

继续遵循项目现有导入纪律：

- `ProcessingMessage` 放在模块级。
- `ProcessingRunFeedback` 放在 `run_algorithm()` 方法内部。
- 在方法内部导入 `QgsProcessingFeedback`。
- 不增加工厂函数或额外类创建器。

---

## 5. `processing.run_algorithm`

### 5.1 设计思路

不再自己组织算法执行流程。

直接使用：

```python
qgis.processing.run()
```

插件只需要创建自定义 Feedback，然后把它传给 QGIS。

### 5.2 执行方式

核心代码：

```python
feedback = ProcessingRunFeedback()

results = qgis.processing.run(
    algorithm_id,
    parameters,
    feedback=feedback,
)
```

只传入 `feedback`。

不传入 `context`，不调用 `createContext()`，不传入 `onFinish`，不设置 `is_child_algorithm`。

其他参数使用 QGIS 的默认行为。

### 5.3 参数检查

删除插件原有的全部参数检查。

包括：

- 算法存在性预检查。
- 参数名称白名单。
- 文件路径允许目录检查。
- 图层引用解析。
- 远端地址检查。
- 连接类参数拒绝。
- 目的参数形态检查。
- `canExecute()` 检查。

不再保留这些检查的替代实现。

算法不存在、参数值不符合要求等问题，统一交给 QGIS Processing 框架处理。

QGIS 3.44 的 `Processing.runAlgorithm()` 内部已经会：

1. 根据算法 ID 创建算法实例。
2. 创建默认 Processing Context。
3. 执行 `checkParameterValues()`。
4. 执行输入 CRS 检查。
5. 执行算法。
6. 处理算法结果。

需要接受一个行为差异：QGIS 的参数值校验不保证拒绝请求中多出来的未知参数键。另外，之前插件自行实施的路径和远端连接限制也会随本次重构一起删除。

### 5.4 Processing Context

插件不再创建 Context。

QGIS 在 `Processing.runAlgorithm()` 中发现 `context=None` 时，会自行执行：

```python
context = dataobjects.createContext(feedback)
```

因此，QGIS 会自动创建 Context，并使用传入的 Feedback。

默认 Context 使用：

```python
QgsProject.instance()
```

作为工程。

本次接受这个默认行为，不再主动将 Context 绑定到插件引擎工程，也不重新设置表达式上下文、椭球体或单位。

**这意味着：如果插件引擎工程与 `QgsProject.instance()` 不是同一个工程，算法执行时不会自动使用插件引擎工程。**

尤其是输入参数使用工程内图层 ID 时，需要确保这些图层能够在 QGIS 默认 Context 中解析。

本次先接受这一限制，不增加工程绑定逻辑。

### 5.5 算法执行

调用：

```python
qgis.processing.run(
    algorithm_id,
    parameters,
    feedback=feedback,
)
```

不再自己创建算法副本，不再手工执行算法的准备、主体和后处理阶段。

也不调用 `runAndLoadResults()`。

### 5.6 成功与失败

本次简化为一个规则：

- `qgis.processing.run()` 正常返回：执行成功。
- `qgis.processing.run()` 抛出异常：执行失败。

不再单独检查 Feedback 中是否存在错误，也不再增加空结果判定。

即使正常返回的结果是空字典，也按 QGIS 正常返回处理。

**异常处理：**

执行失败时：

1. 捕获异常。
2. 将 Feedback 已记录的消息写入本地日志。
3. 将异常信息写入本地日志。
4. 对外仍然返回项目现有的失败记录。
5. 不把原始异常和 Feedback 消息放进失败记录。

执行成功时：

1. 获取结果字典。
2. 获取 `feedback.records`。
3. 将结果和消息列表一起返回。

这样可以直接使用 QGIS 的执行结果判断运行是否成功，不再维护一套复杂的成功与失败判定表。

### 5.7 结果图层处理

这里保留原有接口需要的结果图层处理，不重新设计。

QGIS 3.44 的 `qgis.processing.run()` 在默认执行模式下，会将能够从 Processing Context 中取出的结果图层引用转换成实际的 `QgsMapLayer` 对象。

但它不会自动把这些图层加入插件的引擎工程。

因此，如果当前接口仍然要求返回工程内图层 ID，就保留一个简单的后处理步骤：

1. 遍历结果字典。
2. 遇到 `QgsMapLayer` 对象，将其加入 `engine_project`。
3. 将图层对象替换成加入工程后的图层 ID。
4. 普通字符串、数字等其他结果保持不变。

多图层输出按照相同方式处理列表中的图层对象。

不增加自动加载文件路径、图层分组、排序等功能。

如果原有代码已经具备图层加入工程和 ID 转换逻辑，只需要适配 QGIS 现在返回的图层对象，不必重新编写整套结果处理机制。

---

## 6. 最终执行流程

### `get_algorithm_info`

```text
收到算法 ID
    |
    v
从注册表获取算法
    |
    v
算法不存在？返回参数错误
    |
    v
读取算法基本信息
    |
    v
遍历可见参数并读取信息
    |
    v
遍历输出并读取信息
    |
    v
组装 JSON
    |
    v
返回
```

### `run_algorithm`

```text
收到算法 ID 和参数
    |
    v
创建 ProcessingRunFeedback
    |
    v
调用 qgis.processing.run()
    |
    +---------------------+
    |                     |
    v                     v
  正常返回              抛出异常
    |                     |
    v                     v
  处理结果图层         记录本地日志
    |                     |
    v                     v
  返回结果和消息       返回失败记录
```

整个运行流程不再存在插件自己的算法检查、参数检查和 Context 创建步骤。

---

## 7. 本次明确不做的事情

为了保证能够最快看到 MVP 效果，本次不实现：

- 算法帮助文本解析。
- 参数默认值和可选性提取。
- 插件自己的参数校验。
- 插件自己的算法可执行性检查。
- 自定义 Processing Context。
- 工程上下文修正。
- 表达式上下文重建。
- 异常文本分类。
- Feedback 基类日志替代实现。
- 新的日志框架。
- 复杂的执行结果判定。
- 新的图层事务管理机制。
- QGIS 多版本兼容。
- 其他新增功能。

---

## 8. QGIS 3.44 源码依据

本次直接参考以下 `release-3_44` 源码：

1. `python/plugins/processing/tools/general.py`

   `algorithmHelp()` 的算法、参数、枚举和输出信息组织方式。

   https://github.com/qgis/QGIS/blob/release-3_44/python/plugins/processing/tools/general.py

2. `python/plugins/processing/core/Processing.py`

   `Processing.runAlgorithm()` 的算法创建、默认 Context 创建、参数检查、CRS 检查、算法执行及结果处理。

   https://github.com/qgis/QGIS/blob/release-3_44/python/plugins/processing/core/Processing.py

3. `python/plugins/processing/gui/AlgorithmExecutor.py`

   算法执行过程及异常处理。

   https://github.com/qgis/QGIS/blob/release-3_44/python/plugins/processing/gui/AlgorithmExecutor.py

4. `src/core/processing/qgsprocessingfeedback.cpp`

   Feedback 基类的日志写入、HTML 日志、文本日志及消息记录行为。

   https://github.com/qgis/QGIS/blob/release-3_44/src/core/processing/qgsprocessingfeedback.cpp

5. `QgsProcessingParameterType`

   `acceptedPythonTypes()` 的返回类型为字符串列表。

   https://qgis.org/pyqgis/3.44/core/QgsProcessingParameterType.html

---

## 9. 最终结论

本次改造只做三个事情：

**第一：算法信息直接读取 QGIS 定义。**

参考 `algorithmHelp()` 的源码，将原来通过 `print()` 输出的信息直接组织成 JSON。

**第二：Feedback 只做简单扩展。**

继承 QGIS Feedback，七种消息方法都调用基类，同时将消息保存到自己的列表。

**第三：算法执行直接交给 QGIS。**

只创建 Feedback，调用 `qgis.processing.run()`，不手动创建 Context，不做插件自己的参数检查。

结果图层只保留原接口所需的加入工程和 ID 转换。

最终目标：

**先用最少的代码把算法信息读取、算法执行、消息记录和结果返回全部跑通。看到真实效果之后，再根据实际使用情况决定下一步改进什么。**

本次不提前为尚未遇到的问题设计解决方案。