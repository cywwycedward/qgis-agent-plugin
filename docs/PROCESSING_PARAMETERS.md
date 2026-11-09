# QGIS Processing 参数：类型清单与 AlgorithmParameter 设计背景

本文是一份**自包含**的背景材料：它把设计 `AlgorithmParameter`（及其可能的子类）所需的全部事实写在一处，不依赖任何其他文档或外部链接。文中的数量与取值，都是在 **QGIS 3.44.13 LTR（Python 3.12.13 / PyQt5）** 上遍历当前 Processing 注册表实测得到的。

## 0. 统计口径

| 项 | 值 |
| --- | --- |
| 算法总数（`processingRegistry().algorithms()`） | **440** |
| 参数定义总数（所有算法的 `parameterDefinitions()` 之和） | **2594** |
| 其中"输出目标"（`isDestination()` 为真） | **450** |
| 不同的参数类型字符串（`definition.type()`） | **53** |
| 每个算法的参数个数 | 最少 0、中位 5、最多 34、平均 5.9 |
| 参数名去重后个数 | 686（最高频：`OUTPUT` 360、`INPUT` 349） |

统计方法：逐个算法取 `parameterDefinitions()`，对每个定义读取 `type()`、`isDestination()`、`flags()`、`description()`、`help()`、`defaultValue()`、`dependsOnOtherParameters()`，并在类上探测 `minimum/maximum/dataType/options/fileFilter/allowMultiple/parentLayerParameterName/providerId/layerType/minimumNumberInputs` 等成员。下文所有"实测"均指此口径。

## 1. QGIS 的参数类模型

### 1.1 一个基类 + 55 个子类

所有参数都是 `QgsProcessingParameterDefinition` 的实例或子类实例。绑定里能枚举到 **55 个子类**；本机注册表中实际出现 **48 个**（其余 7 个没有实例，多为中间基类或本机未安装 Provider 才用的类）。

关键结构：

- **基类** `QgsProcessingParameterDefinition`：只描述"元数据 + 标志 + 序列化 + 校验"，**不含任何家族专属信息**。
- **中间基类** `QgsProcessingDestinationParameter`：所有"输出目标"参数的共同父类，10 项新增能力里有 `isSupportedOutputValue`、`createFileFilter`、`defaultFileExtension`、`supportsNonFileBasedOutput`、`generateTemporaryDestination`、`toOutputDefinition`。
- **具体子类**：按家族加字段（数值加 `minimum/maximum/dataType`，枚举加 `options/allowMultiple`，文件加 `fileFilter`，等等）。
- 有两个类型在本机没有具体 Python 类：`aggregates` 与 `alignrasterlayers`，其实例的 Python 类是基类本身。对它们只有 `type()` 字符串可用于判别。

### 1.2 基类的 47 个公开成员（分六组）

| 组 | 成员 |
| --- | --- |
| 身份/元数据（12） | `name`、`setName`、`description`、`setDescription`、`help`、`setHelp`、`toolTip`、`type`、`metadata`、`setMetadata`、`algorithm`、`provider` |
| 标志与可选性（7） | `flags`、`setFlags`、`Flag`、`Flags`、`FlagOptional`（值 = 8）、`FlagAdvanced`、`FlagHidden`、`FlagIsModelOutput` |
| 依赖与动态（9） | `dependsOnOtherParameters`、`isDynamic`、`setIsDynamic`、`dynamicLayerParameterName`、`setDynamicLayerParameterName`、`dynamicPropertyDefinition`、`setDynamicPropertyDefinition`、`additionalExpressionContextVariables`、`setAdditionalExpressionContextVariables` |
| 默认值（6） | `defaultValue`、`setDefaultValue`、`defaultValueForGui`、`guiDefaultValueOverride`、`setGuiDefaultValueOverride`、`defaultGuiValueFromSetting` |
| 校验/序列化/克隆（11） | `checkValueIsAcceptable`、`clone`、`toVariantMap`、`fromVariantMap`、`asScriptCode`、`asPythonString`、`valueAsString`、`valueAsStringList`、`valueAsPythonString`、`valueAsPythonComment`、`valueAsJsonObject` |
| 输出判定（1） | `isDestination` |

**基类没有**：`isSupportedOutputValue`、`fileFilter`、`defaultFileExtension`、`minimum`、`maximum`、`options`、`allowMultiple`、`dataType`。这些都属于子类。

### 1.3 子类相对基类的差异规律

| 家族 | 新增 | 覆写 |
| --- | --- | --- |
| 数值类 | `minimum`/`maximum`/`dataType`（`Number`）；`distance`/`area`/`volume` 再加 `defaultUnit`、`parentParameterName`；`duration` 加 `defaultUnit`；`scale` 没有更多 | `checkValueIsAcceptable`（范围校验） |
| 枚举/属性类 | `enum`：`options`、`allowMultiple`；`field`：`dataType`、`allowMultiple`、`defaultToAllFields`、`parentLayerParameterName`；`band`：`allowMultiple`、`parentLayerParameterName`；`attribute`（点云）：`dataType`、`allowMultiple`、`defaultToAllAttributes`、`parentLayerParameterName` | `dependsOnOtherParameters`（仅 `field`） |
| 表达式/颜色 | `expression`：`expressionType`、`parentLayerParameterName`；`color`：`opacityEnabled` | 序列化与校验 |
| 图层引用类 | `FeatureSource`：`dataTypes`；`MultipleLayers`：`layerType`、`minimumNumberInputs`；其余仅 `createFileFilter` | `valueAsString`/`valueAsJsonObject` |
| 文件类 | `file`/`fileDestination`：`fileFilter` | 序列化、`checkValueIsAcceptable` |
| 输出类 | 全部经中间基类获得 `isSupportedOutputValue` 等 10 项；`fileDestination` 再加 `fileFilter` | `isDestination` 返回真 |
| 连接类 | `providerconnection`：`providerId`；`databaseschema`：`parentConnectionParameterName`；`databasetable`：`parentConnectionParameterName`、`parentSchemaParameterName`、`allowNewTableNames` | 序列化 |
| 其余（`crs`/`extent`/`point`/`matrix`/`layout`/`maptheme`/`geometry`/`authcfg`/`dxflayers`/`tininputlayers` 等） | 只有内部常量或值转换辅助（如 `fromScriptCode`、`typeName`） | 序列化与校验 |

有两条**只出现一次的覆写**值得记住：`isSupportedOutputValue` 只在中间基类被覆写（7 个输出类型共享同一套判定），`dependsOnOtherParameters` 只在 `field` 被覆写。

## 2. 53 个参数类型的完整清单

「家族」一列是本文建议的 `kind` 分组（见 §6）；「专属字段」只列**对调用方有意义**的那些（不含 `fromScriptCode`、`typeName` 这类只服务脚本导出的成员）。

| type | Python 类 | 实例数 | 输出目标 | 可选 | 建议 kind | 专属字段 | JSON 值形态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `number` | `QgsProcessingParameterNumber` | 380 | 0 | 94 | number | dataType, minimum, maximum | 数字 ✔ |
| `string` | `QgsProcessingParameterString` | 348 | 0 | 286 | other | — | 字符串 ✔ |
| `source` | `QgsProcessingParameterFeatureSource` | 282 | 0 | 1 | other | — | 图层 id / 路径 ✔ |
| `sink` | `QgsProcessingParameterFeatureSink` | 282 | 282 | 61 | other | — | 图层目标（`memory:` 或路径）✔ |
| `boolean` | `QgsProcessingParameterBoolean` | 227 | 0 | 4 | other | — | 布尔 ✔ |
| `enum` | `QgsProcessingParameterEnum` | 163 | 0 | 4 | enum | options, allowMultiple | 选项下标（int）或下标数组 ✔ |
| `field` | `QgsProcessingParameterField` | 161 | 0 | 56 | attribute | dataType, allowMultiple, defaultToAllFields, parentLayerParameterName | 字段名 / 字段名数组 ✔ |
| `raster` | `QgsProcessingParameterRasterLayer` | 94 | 0 | 7 | other | — | 图层 id / 路径 ✔ |
| `rasterDestination` | `QgsProcessingParameterRasterDestination` | 86 | 86 | 6 | other | — | 栅格目标 ✔ |
| `distance` | `QgsProcessingParameterDistance` | 81 | 0 | 15 | measure | dataType, minimum, maximum, defaultUnit, parentParameterName | 数字 ✔ |
| `crs` | `QgsProcessingParameterCrs` | 60 | 0 | 38 | other | — | `EPSG:4326` 之类字符串 ✔ |
| `band` | `QgsProcessingParameterBand` | 55 | 0 | 6 | band | allowMultiple, parentLayerParameterName | 波段号 / 数组 |
| `extent` | `QgsProcessingParameterExtent` | 53 | 0 | 29 | other | — | `xmin,xmax,ymin,ymax [CRS]` ✔ |
| `multilayer` | `QgsProcessingParameterMultipleLayers` | 46 | 0 | 9 | multi | layerType, minimumNumberInputs | 图层 id 数组 ✔ |
| `fileDestination` | `QgsProcessingParameterFileDestination` | 44 | 44 | 10 | file | fileFilter | 文件路径 / `TEMPORARY_OUTPUT` ✔ |
| `expression` | `QgsProcessingParameterExpression` | 40 | 0 | 25 | other | expressionType, parentLayerParameterName | 表达式字符串 |
| `vector` | `QgsProcessingParameterVectorLayer` | 26 | 0 | 4 | other | — | 图层 id / 路径 ✔ |
| `vectorDestination` | `QgsProcessingParameterVectorDestination` | 17 | 17 | 1 | other | — | 矢量目标 ✔ |
| `file` | `QgsProcessingParameterFile` | 17 | 0 | 2 | file | fileFilter | 绝对路径 ✔ |
| `layer` | `QgsProcessingParameterMapLayer` | 16 | 0 | 1 | other | — | 图层 id / 路径 |
| `pointcloud` | `QgsProcessingParameterPointCloudLayer` | 13 | 0 | 0 | other | — | 图层 id / 路径 |
| `folderDestination` | `QgsProcessingParameterFolderDestination` | 10 | 10 | 2 | file | — | 目录路径 ✔ |
| `mesh` | `QgsProcessingParameterMeshLayer` | 9 | 0 | 0 | other | — | 图层 id / 路径 |
| `meshdatasettime` | `QgsProcessingParameterMeshDatasetTime` | 9 | 0 | 0 | other | — | 对象 |
| `pointCloudDestination` | `QgsProcessingParameterPointCloudDestination` | 9 | 9 | 0 | other | — | 点云目标 |
| `point` | `QgsProcessingParameterPoint` | 8 | 0 | 1 | other | — | `x,y [CRS]` |
| `meshdatasetgroups` | `QgsProcessingParameterMeshDatasetGroups` | 8 | 0 | 4 | other | — | 数组 |
| `layout` | `QgsProcessingParameterLayout` | 6 | 0 | 0 | other | — | 版式名 |
| `providerconnection` | `QgsProcessingParameterProviderConnection` | 5 | 0 | 0 | connection | providerId | 连接名 |
| `color` | `QgsProcessingParameterColor` | 5 | 0 | 5 | other | opacityEnabled | 颜色字符串 |
| `maptheme` | `QgsProcessingParameterMapTheme` | 3 | 0 | 3 | other | — | 主题名 |
| `databaseschema` | `QgsProcessingParameterDatabaseSchema` | 2 | 0 | 2 | connection | parentConnectionParameterName | schema 名 |
| `databasetable` | `QgsProcessingParameterDatabaseTable` | 2 | 0 | 2 | connection | parentConnectionParameterName, parentSchemaParameterName, allowNewTableNames | 表名 |
| `area` | `QgsProcessingParameterArea` | 2 | 0 | 0 | measure | dataType, minimum, maximum, defaultUnit, parentParameterName | 数字 |
| `vectorTileDestination` | `QgsProcessingParameterVectorTileDestination` | 2 | 2 | 0 | other | — | 瓦片目标 |
| `scale` | `QgsProcessingParameterScale` | 2 | 0 | 0 | number | dataType, minimum, maximum | 数字 |
| `coordinateoperation` | `QgsProcessingParameterCoordinateOperation` | 2 | 0 | 2 | other | — | 字符串 |
| `vectortilewriterlayers` | `QgsProcessingParameterVectorTileWriterLayers` | 2 | 0 | 0 | other | — | 数组（内嵌图层引用） |
| `attribute` | `QgsProcessingParameterPointCloudAttribute` | 2 | 0 | 1 | attribute | dataType, allowMultiple, defaultToAllAttributes, parentLayerParameterName | 属性名 / 数组 |
| `idw_interpolation_data` | `ParameterInterpolationData`（私有） | 2 | 0 | 0 | other | — | 数组（内嵌图层引用） |
| `aggregates` | 基类实例 | 1 | 0 | 0 | other | — | 对象 |
| `alignrasterlayers` | 基类实例 | 1 | 0 | 0 | other | — | 数组（内嵌图层引用） |
| `dxflayers` | `QgsProcessingParameterDxfLayers` | 1 | 0 | 0 | other | — | 数组（内嵌图层引用） |
| `geometry` | `QgsProcessingParameterGeometry` | 1 | 0 | 0 | other | allowMultipart, geometryTypes | WKT/几何文本 |
| `authcfg` | `QgsProcessingParameterAuthConfig` | 1 | 0 | 1 | connection | — | 认证配置 id |
| `layoutitem` | `QgsProcessingParameterLayoutItem` | 1 | 0 | 1 | other | — | 版式项 id |
| `matrix` | `QgsProcessingParameterMatrix` | 1 | 0 | 0 | other | numberRows, headers, hasFixedNumberRows | 二维数组 |
| `fields_mapping` | `QgsProcessingParameterFieldMapping` | 1 | 0 | 0 | other | parentLayerParameterName | 对象 |
| `duration` | `QgsProcessingParameterDuration` | 1 | 0 | 0 | measure | dataType, minimum, maximum, defaultUnit | 数字 |
| `tininputlayers` | `QgsProcessingParameterTinInputLayers` | 1 | 0 | 0 | other | — | 数组（内嵌图层引用） |
| `execute_sql` | `ParameterExecuteSql`（私有） | 1 | 0 | 0 | other | — | 字符串 |
| `raster_calc_expression` | `ParameterRasterCalculatorExpression`（私有） | 1 | 0 | 0 | other | — | 字符串 |
| `relief_colors` | `ParameterReliefColors`（私有） | 1 | 0 | 1 | other | — | 数组 |

说明：「JSON 值形态」中标 ✔ 的，表示本次在本机用真实算法跑通过该形态；未标的按参数定义与 QGIS 惯例推断，未逐个验证。`vectorDestination` 之所以归 `other` 而不是 `file`：它的值可以是 `memory:`（本机有一个 native 算法的该参数支持内存输出），与纯文件目标不同。

## 3. 输出目标专题（45 万个定义里最需要小心的一类）

### 3.1 七个输出类型与分布

| type | 实例数 | 产物 |
| --- | --- | --- |
| `sink` | 282 | 矢量图层（`memory:` 或文件） |
| `rasterDestination` | 86 | 栅格图层（`memory:` 或文件） |
| `fileDestination` | 44 | 文件（HTML/PDF/CSV/XLSX…） |
| `vectorDestination` | 17 | 矢量图层（文件；个别支持 `memory:`） |
| `folderDestination` | 10 | 目录 |
| `pointCloudDestination` | 9 | 点云图层（文件） |
| `vectorTileDestination` | 2 | 矢量瓦片数据集 |

合计 450 个输出定义，其中必填 370、可选 80。

### 3.2 一个值在 QGIS 里会变成什么

同一个字符串 `OUTPUT` 取值，产物由「参数类」和「Provider 能力」共同决定（本机实测）：

| 参数 | 取值 | 实测结果 |
| --- | --- | --- |
| `native:buffer`（`sink`，Provider 支持非文件输出） | `memory:` | 内存图层（`providerType='memory'`，5 要素，无文件） |
| `native:buffer` | `TEMPORARY_OUTPUT` | **也是内存图层**（没有文件） |
| `native:buffer` | 具体路径 | 真实 `.gpkg`（98 304 字节）；返回的是**路径字符串**，不是图层 id |
| `gdal:buffervectors`（`vectorDestination`，Provider 不支持非文件输出） | `memory:` | `ok=True` 但结果是字面量 `'memory:'`，**没有图层也没有文件**（假成功） |
| `gdal:buffervectors` | `TEMPORARY_OUTPUT` | 真实文件，落在临时目录 `…/processing_<会话>/<uuid>/OUTPUT.gpkg` |
| `native:aspect`（`rasterDestination`，Provider 支持但该参数不支持） | `memory:` | 同样是假成功（`ok=True`、`'memory:'`、0 图层） |
| `gdal:gdalinfo`（`fileDestination`） | `memory:` | `ok=False`（文件型没有内存形态） |

由此得出三点：

1. **`memory:` 与 `TEMPORARY_OUTPUT` 不是两种"类型"，而是两个取值**；前者是否有效取决于 Provider，后者落在内存还是磁盘也取决于 Provider。
2. **写文件不会自动变成工程图层**：返回值是路径字符串，要不要再加载由调用方决定。
3. `defaultValue()` 对所有 450 个输出**都是 `None`**；界面上看到的"保存为临时文件"是控件替调用方填入的 `TEMPORARY_OUTPUT`，不是参数默认值。API 层不填 = 未提供，必填时就失败。

### 3.3 判"某个值能不能用"的唯一可靠入口

`definition.isSupportedOutputValue(value, context) -> (bool, 原因)`（QGIS 3.14 起，所有参数类都有）。用法与实测：

| 参数 | `memory:` | `TEMPORARY_OUTPUT` | 空串 | `.unknownext` |
| --- | --- | --- | --- | --- |
| `native:buffer`（`sink`） | 真 | 真 | 假（`Missing parameter value …`） | **假**（`"x.unknownext" files are not supported…`） |
| `native:aspect`（`rasterDestination`） | **假**（`“.” files are not supported…`） | 真 | 假 | 真 |
| `gdal:buffervectors`（`vectorDestination`） | **假**（`only supports disk-based outputs`） | 真 | 假 | 真 |
| `gdal:gdalinfo`（`fileDestination`） | 真（**无意义**：被当作文件名） | 真 | 假 | 真 |
| `gdal:gdal2tiles`（`folderDestination`） | 真（同上） | 真 | 假 | 真 |

使用约束：必须传入真实的 `QgsProcessingContext`（传 `None` 抛 `TypeError`）；必须用属于算法的那份定义（自己新建的定义 `algorithm()` 为 `None`，会对 `memory:` 答"真"）；它只判"这个值对这个参数"，**不判**路径是否存在、父目录、可写性；对文件/目录型，`memory:` 只是文件名，结论没有意义。

### 3.4 `fileFilter` 的实际取值与体积

只有 `file`（12 个）与 `fileDestination`（44 个）共 56 个定义有非空 `fileFilter`，19 种不同取值，其中 7 个是 `All files (*.*)`。最长的一个是 `native:savefeatures.OUTPUT`，856 个字符，列出 26 组格式。示例：

```text
native:printlayouttopdf.OUTPUT    PDF Format (*.pdf *.PDF)
native:combinestyles.OUTPUT       Style files (*.xml)
native:savelog.OUTPUT             Text files (*.txt);;HTML files (*.html *.HTML)
native:exporttospreadsheet.OUTPUT Microsoft Excel (*.xlsx);;Open Document Spreadsheet (*.ods)
native:httprequest.OUTPUT         All files (*.*)
```

它是**对话框过滤器**，不是校验规则（见 §3.3 最后一行：文件型对任意后缀都答"真"）。对调用方只有"建议后缀"的意义。

## 4. 字段级统计（用于判断哪些字段值得进 DTO）

### 4.1 潜在字段的覆盖率

| 字段 | 覆盖 | 分布/体积 |
| --- | --- | --- |
| `name` | 2594 | 686 个不同名字 |
| `type` | 2594 | 53 个值 |
| `description` | **2594（100% 非空）** | 平均 19.4 字符、最长 102；输出目标子集平均 14.7、最长 58；合计约 50 294 字符 |
| `optional` | 2594 | 可选 684（26.4%）、必填 1910；输出目标里可选 80/450 |
| `default` | 2594 | 可无损 JSON 2547（98.2%）；47 个不可（`QVariant` 40、`QColor` 5 等）；值类型：None 1539、int 439、bool 224、str 193、float 134、list 18 |
| `help` | 2594 | **只有 88 个非空（3.4%）**；非空时平均 141、最长 524 字符 |
| `is_destination` | 450 为真 | 与 `type` 完全冗余（见 §4.3） |
| `depends_on` | 362 非空（14.0%） | 1 个父参数 349、2 个 4、3 个 9 |
| `options` | 163（6.3%） | 恰为全部 `enum`；每项 2–27 个选项 |
| `file_filter`（拟新增） | 56（2.2%） | 19 种取值；最长 856 字符 |
| `minimum`/`maximum` | 466（17.9%） | `minimum`：0.0 有 224、其他真下界 114、`-1.797e308` 哨兵 128；`maximum`：真上界 102、`1.797e308` 哨兵 364 |
| `data_type` | 466 数值族 + 161 `field`（另 `range` 未在本机出现） | Double 326 / Integer 140（数值族） |
| `allow_multiple` | 163 `enum` 中 34 为真；`field` 161、`band` 55、`attribute` 2 | 布尔 |
| 父参数名 | `field` 161、`band` 55、`expression` 40、`attribute` 2、`fields_mapping` 1、`databaseschema`/`databasetable` 4 | 255 个 `field` 的父参数**全部**已包含在 `depends_on` 里 |
| `provider_id` | 5 | 仅 `providerconnection` |
| `layer_type` / `minimum_inputs` | 46 / 46 | 仅 `multilayer` |

### 4.2 体积

全部 2594 个参数的 `description` + `help` 合计 **62 728 字符**。一旦 `get_algorithm_info` 返回一个算法（中位 5 个参数），文本量约 100–150 字符/参数，响应在 1 KB 量级。真正的体积尖点是 `native:savefeatures` 的 856 字符 filter。

### 4.3 冗余关系（实测）

| 关系 | 结论 |
| --- | --- |
| `is_destination` ⟺ `type` ∈ {sink, rasterDestination, fileDestination, vectorDestination, folderDestination, pointCloudDestination, vectorTileDestination} | 2594 个定义**零例外**（450 真 / 2144 假） |
| `fileFilter` 存在 ⟺ `type` ∈ {file, fileDestination} | 56 = 12 + 44，无例外 |
| `options` 存在 ⟺ `type` = `enum` | 163 = 163 |
| `field` 的父参数 ⊆ `depends_on` | 255/255 成立，父参数名没有带来新信息 |
| `minimum/maximum` 存在 ⟺ 数值族 | 466 = 380+81+2+2+1 |

## 5. 设计目标

### 5.1 目标

用一个输出模型（或一族模型）向调用方描述"这个算法的这个参数该怎么填"，满足：

1. 调用方不读 QGIS 源码就能构造合法请求；
2. 模型只装 JSON 兼容数据，可以进执行记录、可以序列化；
3. 不依赖 QGIS 的 Python 对象（对象有生命周期问题：算法实例被回收后，定义会失效）；
4. 未知/第三方参数类型不至于让模型校验失败；
5. 家族专属字段只出现在该家族上，其他家族不出现"恒为 null 的字段"。

### 5.2 现有模型（9 个字段，扁平）

```text
AlgorithmParameter
  name            字符串，必填
  type            字符串，必填（QGIS 的 type()，53 个值）
  description     字符串，必填（实测总有值）
  optional        布尔，必填（FlagOptional）
  default         任意 JSON，必填（可为 null；不可 JSON 化的原生默认值归一为 null）
  help            字符串，必填（3.4% 非空，其余为空串）
  is_destination  布尔，必填（与 type 冗余）
  depends_on      字符串数组，必填（14% 非空）
  options         字符串数组或 null（仅 enum）
```

问题：`options` 对 2431 个参数为 null；若再补 `minimum/maximum/data_type/allow_multiple/...`，将有 6–10 个字段对绝大多数参数无意义。

### 5.3 候选方案

| 方案 | 模型数 | 家族字段是否恒存在 | 未知类型 | Schema 形状 |
| --- | --- | --- | --- | --- |
| A 扁平 + 可空字段 | 1 | 否（多数为 null） | 天然支持 | 1 个 object |
| B 判别联合，按家族建模型 | 8–11 | 是 | 需要一个"其他"兜底成员 | `oneOf` |
| C 每个 QGIS 类一个模型 | 48–53 | 是 | 需要 40+ 个没有额外信息的空模型 | `oneOf` 巨大 |

实测支持 B：40 多个类型没有对调用方有用的专属字段（它们的"新增成员"是内部常量与脚本导出辅助），所以 C 里绝大多数是空壳。

### 5.4 建议的家族划分（B）

判别字段用一个我们自己的 `kind`（不是 QGIS 的 `type`，因为 `type` 有 53 个值且第三方会新增）：

| kind | 覆盖的 type | 实例数 | 专属字段（全部必填或可空明确） |
| --- | --- | --- | --- |
| `number` | `number`、`scale` | 382 | `data_type`（integer/double）、`minimum`（无下界时 null）、`maximum`（无上界时 null） |
| `measure` | `distance`、`area`、`duration` | 84 | 上述三项 + `unit`（默认单位）+ `parent_parameter`（距离/面积才有，否则 null） |
| `enum` | `enum` | 163 | `options`、`allow_multiple` |
| `attribute` | `field`、`attribute` | 163 | `data_type`、`allow_multiple`、`default_to_all`、`parent_parameter` |
| `band` | `band` | 55 | `allow_multiple`、`parent_parameter` |
| `file` | `file`、`fileDestination`、`folderDestination` | 71 | `file_filter`（目录型为 null） |
| `multi` | `multilayer` | 46 | `layer_type`、`minimum_inputs` |
| `connection` | `providerconnection`、`databaseschema`、`databasetable` | 9 | `provider_id`（仅连接型）、`parent_connection`、`parent_schema`、`allow_new_table_names` |
| `other` | 其余 37 个 type（含 4 个私有类、2 个无具体类的类型） | 约 1621 | 无 |

公共字段（九个）留在基模型：`name`、`type`、`kind`、`description`、`optional`、`default`、`help`、`is_destination`、`depends_on`；`options` 从基模型移到 `enum` 模型里。

归一化规则（必须与字段一起定义）：

1. `minimum == -1.7976931348623157e+308` → `null`（无下界）；`maximum == 1.7976931348623157e+308` → `null`（无上界）。
2. `default` 不可无损 JSON 化时归一为 `null`（47 个定义）。
3. `unit`、`file_filter` 的原生值原样给出，不解析、不截断。
4. 未知 `type` → `kind = "other"`，只带公共字段。

### 5.5 未进入模型的字段及理由

| 字段 | 覆盖率 | 不入模型的理由 |
| --- | --- | --- |
| `is_destination` | 450 | 与 `type` 完全冗余；保留仅为可读性（可选删除） |
| `help` | 88 非空 | 信息密度最低，但成本也低；建议保留并在说明里写明"多数为空串" |
| 父参数名（`field`/`band`/`expression`） | 255+ | `field` 的 255 个父参数全部已在 `depends_on` 中；其余家族的父参数可作为 `parent_parameter` 进对应模型 |
| `expression_type`、`opacity_enabled`、`geometry_types`、`allow_multipart`、`number_rows`、`headers` | 各 1–5 个定义 | 只影响少数算法，暂不进入模型；若需要，按"家族专属字段"规则再补 |
| `layer_type`/`minimum_inputs` | 46 | 进入 `multi` 模型（见 §5.4） |

### 5.6 未知类型与第三方 Provider

本机 53 个 type 里已有 4 个来自私有参数类（`idw_interpolation_data`、`execute_sql`、`raster_calc_expression`、`relief_colors`），说明"类型清单是开放的"。因此：`kind` 的映射表必须为未知 `type` 提供 `other` 兜底，且 `type` 本身保持字符串原样透传，不做枚举校验。

### 5.7 校验与序列化策略

| 项 | 建议 | 理由 |
| --- | --- | --- |
| 未知字段 | 拒绝（`extra="forbid"`） | 契约错误要在输出校验时暴露，而不是静默丢字段 |
| 版本漂移 | 接受风险 | QGIS 若给某家族加字段，我们的模型会拒；届时按类型补字段 |
| `default` | 递归 JSON 校验，非有限数拒绝 | 与调用方传入的参数同一套 JSON 规则 |
| `minimum/maximum` | 有限浮点或 null | 哨兵归一化后不再出现 `1.797e308` |
| `options` | 只在 `enum` 模型，字符串数组 | 原生顺序即显示顺序，保持原序 |

## 6. 调用方视角：这些字段怎么用

1. 取算法的参数清单，只看 `is_destination`/`kind`：需要填位置的参数一目了然。
2. 看 `kind`：`number`/`measure` 给数字（并遵守 `minimum`/`maximum`/`data_type`）；`enum` 给下标（`allow_multiple` 为真时给数组）；`attribute`/`band` 给字段名/波段号；`file` 给路径或 `TEMPORARY_OUTPUT`（`file_filter` 提示后缀）；`multi` 给图层 id 数组；`connection` 给连接/schema/表名；`other` 按该参数自己的说明填。
3. 图层型输出：`memory:` 是否合法由参数自身判定（不是看 Provider）；文件型/目录型没有内存形态。
4. 不填 + 必填 = 原生校验失败；想"随便放"就显式给 `TEMPORARY_OUTPUT`。
5. 跑完按 `kind` 解释结果：`memory:` 给图层 id（已在工程里），文件/目录给路径，其余给标量或数组。

## 7. 未决问题（需要逐条定）

1. **`kind` 是否引入**：它是新字段，理论上可由 `type` 派生，但判别联合需要一个封闭值；不引入就只能用 53 个 `Literal` 或退回扁平模型。
2. **家族划分粒度**：`measure` 里 `duration` 没有父参数（84 个实例中 1 个），是否值得为它单列一个模型。
3. **`connection` 三个类型合并还是拆开**：合计只有 9 个定义，拆开会让每个模型只服务 2–5 个参数。
4. **`is_destination` 保留还是删除**：删掉不损失信息（与 `type` 完全等价），保留只是可读性。
5. **`help` 保留与否**：96.6% 是空串；删掉会少一个"人话"来源，保留会让响应多约 12 000 字符（全量）。
6. **`extra="forbid"` 与版本漂移**：QGIS 升级加字段时，严格模式会让输出校验失败；是否接受。
7. **`file_filter` 是否截断**：最长 856 字符；截断会误导，建议原样。
8. **`default` 的 `null` 两义**：`null` 既表示"没默认值"（1539 个），也表示"有默认值但不可 JSON 化"（47 个）。是否需要一个区分字段。
9. **私有参数类**（4 个）与**无 Python 类的类型**（2 个）：只能靠 `type` 字符串识别，是否需要单独的 `kind`。
10. **输出目标是否单独建家族**：本表把 7 个输出类型分散在 `other`/`file` 里；也可以按"产物形态"（图层/文件/目录）再分一次，代价是与输入类型不共用 `kind` 词表。

## 8. 检查清单（供逐条确认）

- [ ] §2 的 53 行清单与建议 `kind` 是否同意
- [ ] §5.4 的 9 个 `kind` 与专属字段是否同意（尤其 `measure`/`band`/`connection` 的取舍）
- [ ] §5.4 的四条归一化规则（哨兵、`default`、原样值、未知类型）
- [ ] §5.5 中未进入模型的字段是否有需要补的（`expression_type`、`opacity_enabled`、`geometry_types` 等）
- [ ] §5.7 的 `extra="forbid"` 与版本漂移取舍
- [ ] §7 的十条未决问题

---

本文所有数字与取值均来自本机 QGIS 3.44.13 的实时注册表遍历与真实算法调用；文中已标注哪些值形态经过实跑验证（标 ✔）、哪些按定义推断。"建议 kind"与"未决问题"是设计判断，不是实测事实。
