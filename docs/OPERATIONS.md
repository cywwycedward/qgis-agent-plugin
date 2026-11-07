# 九个 Operation 的契约（以代码为准）

结论：本插件的全部能力就是下面九个 Operation。表里每一项都能在代码里找到出处，
默认值与校验规则来自 `src/qgis_agent_plugin/operations/*.py` 的输入模型，错误码来自
`core/models.py` 的 `ERROR_CODES`；与 PRD 的差异集中在 §12，不要照 PRD 的示例写调用方。

| # | Operation | kind | strategy | destructive | 必填参数 | 一句话 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `project.create` | command | main_thread | **是** | 无 | 换成指定 CRS 的空工程 |
| 2 | `project.open` | command | main_thread | **是** | `path` | 打开本地 `.qgs` / `.qgz` |
| 3 | `project.save` | command | main_thread | **是**（可能覆盖） | 无 | 写当前工程到本地文件 |
| 4 | `project.get_info` | query | main_thread | 否 | 无 | 读工程文件/CRS/dirty/图层数/epoch |
| 5 | `layer.add` | command | main_thread | 否 | `kind`、`source_path`、`name` | 把本地矢量/栅格加进工程 |
| 6 | `layer.list` | query | main_thread | 否 | 无 | 列出工程里的图层 |
| 7 | `layer.get_info` | query | main_thread | 否 | `layer_id` | 读一个图层的信息 |
| 8 | `layer.set_visibility` | command | main_thread | 否 | `layer_id`、`visible` | 勾选/取消一个图层的树节点 |
| 9 | `layer.sum_numeric_field` | query | **background** | 否 | `layer_id`、`field_name` | 用 `QgsTask` 求和一份快照 |

读这张表的三个前提：

- **只有 8 个能在 `run_sync` 里跑**。第 9 个必须 `submit`；`run_sync` 会被拒绝
  `UNSUPPORTED_EXECUTION_MODE`（控制面异常，不建记录，已实测）。
- **所有输入模型都是 `extra="forbid"`**（`core/models.py::Params`）：多写一个键不是被忽略，
  而是 `FAILED(INVALID_INPUT)`，`message` 里带出错字段名，例如
  `project.get_info: invalid input at discard_current`（已实测）。
- **所有布尔参数都是 `StrictBool`**：`"true"`、`1` 一律 `INVALID_INPUT`
  （`add_to_legend: "true"` 已实测被拒；`"true"`/`0`/`1` 由单测覆盖）。字符串参数是 `StrictStr`，不接受数字。

`OperationSpec`（`iface.agent_registry.get_spec(name)` / `list_specs()`）是同一份契约的
机器可读版本，字段为 `name`、`description`、`kind`、`strategy`、`destructive`、
`input_schema`、`output_schema`、`pydantic_major`（恒为 `2`）。JSON Schema 由 Pydantic 直接
生成，例如：

```python
spec = iface.agent_registry.get_spec("layer.add")
spec.input_schema["required"]  # ['kind', 'source_path', 'name']
spec.output_schema["required"]  # ['layer_id', 'name', 'kind', 'provider', 'crs_authid']
```

## 1. 通用约定

### 1.1 失败长什么样

两类失败不要混：

| 形态 | 何时 | 调用方拿到什么 |
| --- | --- | --- |
| 控制面异常 | 调用本身无效：未知 Operation、未知执行 ID、非主线程、引擎已关闭、对后台 Operation 调 `run_sync` | 抛 `FrameworkError`（有 `.code`、`.message`），**不建记录** |
| `FAILED` 记录 | 调用有效但执行失败：入参校验、路径策略、GIS 动作、输出校验、提交失败 | `ExecutionRecord.status == "FAILED"`，`record.error.code` 是 PRD 11.2 里的码 |

每个 Operation 的“错误码”表只列**业务码**（它自己会抛的那些）。控制面码、容量码、
输出校验码是所有 Operation 共用的，见 `docs/ERRORS.md`。

### 1.2 路径怎么判

`layer.add` 用 `allowed_data_roots`，`project.open` / `project.save` 用 `allowed_project_roots`；
`project.open` 还额外地用 `allowed_data_roots` 检查工程文件里每一层的数据源（见 §3）。
`resolve_path` 的规则：

- 必须是绝对路径，`Path.resolve()` 后的真实目标要落在允许根目录内（按路径组件比较，
  `..` 和软链接跳不出去）。
- 读（`layer.add`、`project.open` 的工程文件）：目标必须存在且是普通文件。
- 工程文件里的一层数据源可以是一**目录**（ESRI FileGDB 就是目录）：同样是解析后落在
  `allowed_data_roots` 内就算通过，不要求它是普通文件（`resolve_path(..., allow_directory=True)`，
  只在“写”形状上生效；读永远要求普通文件）。数据源**不存在**（连父目录都没有）也算通过：
  只问解析后的路径是否落在根内，不要求路径存在。
- 写（`project.save`）：目标可以不存在，但父目录必须存在且在根内；不创建父目录。
- URL、provider URI、`/vsi*`、以及 Windows UNC 路径（`\\host\share\…`、`//host/share/…`、
  `\\?\…`）都在**任何 I/O 之前**拒掉：UNC 的解析本身就会连主机并带上当前 Windows 凭据，
  所以它和 URL 一样不能先进白名单再判。错误消息里**不含**原始路径。
- `project.open` 的工程文件还会在 `read()` **之前**被扫一遍（`.qgs` 是 XML，`.qgz` 是 zip 里的
  同一个 XML）。这一层用的是**标准库 XML 解析器**（`xml.parsers.expat`，事件驱动、不建树），
  不是字节启发式：前四轮的手写扫描被同样几种**合法 XML 形状**打穿（属性值里的 `>`、注释/CDATA
  里的假闭合标签、QGIS 自己写的 `>` 让深度计数器漂移），所以“有哪些值、嵌套多深”现在由
  解析器回答：
  - **一条判据，问文件里的每一个值**：每个元素的**整段文本**与每个**属性值**过同一个判定
    （不认元素名、不认属性名）：`strip` → 若开头是本地 scheme（`file:`、`attachment:`）先剥掉
    再判 → 命中以下任一即 `PATH_NOT_ALLOWED`，消息是
    `the project file names a remote data source; it was not opened`：以 `/vsi` 开头
    （不区分大小写）、以 UNC 前缀（`\\`、`//`）开头、以 `scheme://` 开头。
    属性值必须一起判，因为 `read()` 取的不止图层数据源：布局里的图片项把图片写在 `file=`
    属性里（`<LayoutItem type="65640" file="http://…">`），`read()` 读布局时就去取图——
    只问 `<datasource>` 文本的那版扫描放行了这种工程，实测同一个 5 KB 文件让 `project.open`
    发 **9 次 TCP**、返回 `SUCCEEDED`；对端接受连接但永不回应时主线程停 **180.1 s**，仍报
    `SUCCEEDED`（A11 #1）。
  - **只看值的开头，不做子串匹配**：`D:/vsi_data/x.gpkg`、`./vsi_data/…`、名字里带
    `data/vsicache` 的图层、正文里引用 URL 的 `<abstract>Downloaded from https://…</abstract>`
    都不再被误拒（A6 #3、A7 #3）。代价是**值本身就是 URL** 的元数据
    （`<identifier>https://…</identifier>`、`Option value="https://…"`）会被拒，这是这条统一
    判据的已知误拒，记在 §12。
  - 本地 scheme 先剥掉再判：`file:./points.csv`（QGIS 自己写的 Delimited Text 图层）、
    `file:///C:/…` 与 `attachment:///styles.db`（QGIS 3.44 给每个带工程样式的工程都写一条）
    是本地路径；`file://host/…` 剥不掉，仍按 `scheme://` 拒；别的 scheme 一律当远程。
  - 元素的文本不止 `element.text`，也不只是子元素的 `tail`：扫描收的是从元素的 start 到匹配
    end 之间的**每一个字符数据事件**，按文档顺序拼成一个串，在 end 时对整串判一次——这就是
    QGIS 那边 `QDomElement::text()` 的语义。所以
    `<datasource>/v<x/>sicurl/htt<y/>p:<y2/>/<z/>/127.0.0.1:9/never.tif</datasource>` 这种
    五段拼出来的 `/vsicurl` 地址被当成一个地址拒掉（逐段判时它穿过了前扫，`project.open`
    在失败之前先发了 8 次 TCP，实测，A7 #1）。
  - 解析器已经把字符引用解码了，所以 `&#47;vsicurl&#47;…` 与它指向的路径同一种判法。
  - 解析**之前**先拒带内部子集的 DOCTYPE（出现 `<!ENTITY`，或 `<!DOCTYPE` 后出现 `[`）：
    expat 会把实体展开进元素文本，展开后的体积不进任何预算（实测：1 MiB 实体读 50 次的
    1 MiB 工程让前扫峰值 100 MiB；8 MiB x 90 峰值 1,440 MiB，而标签数与深度都远低于上限）。
    QGIS 自己写的头是 `<!DOCTYPE qgis PUBLIC '…' 'SYSTEM'>`，没有 `[`，不会误拒；消息是
    `the project file declares XML entities`。
  - 深度来自解析事件（开标签 +1、闭标签 −1，**迭代**遍历，不递归）：注释/CDATA 里的尖括号
    不是元素、不算深度，`<a x="/>" …>` 只开一层。上限 `256`，按 `read()` 的**耗时**标定而不是
    崩溃点：实测 200 层嵌套分组 0.28 s、600 层 8.84 s、900 层 30.11 s（同样体积的 4.2 MB 平铺
    工程 0.14 s，所以那是深度不是体积）；QGIS 自己写的 100 层分组图层树是 105 层，256 留 2.4 倍
    余量、按曲线约半秒。超过就 `PROJECT_OPEN_FAILED`，消息里说 `nests deeper than 256 levels`。
  - 解析前先看一次 `bytes.count(b"<")`：超过 `400000` 个标签直接 `PROJECT_OPEN_FAILED`
    （消息 `more than 400000 XML tags`）。它是元素数的上界（每个元素恰好一个开标签），
    所以把解析耗时钉在有界范围（实测 400000 个标签解析 0.12–0.19 s）；真实 16 MB 工程的标签
    密度是 42.6–61 字节/标签，即 26–39 万个标签，落在这个上限内。
  - 字节里出现 NUL 说明工程不是 UTF-8（UTF-16/UTF-32），直接 `PATH_NOT_ALLOWED`，不尝试解码
    （QDomDocument 读得了这种文件，但本插件不支持）。
  - 解析不了的 XML 不是工程：`PROJECT_OPEN_FAILED`，用的是 `read()` 失败时同一句消息，
    **不会**再交给 `read()`。
  - 工程文件本身、或 `.qgz` 里**每个**成员解压后的**总量**超过 16 MB 也直接拒：
    `PATH_NOT_ALLOWED`，消息 `the project file is too large`。预算按**实际解压出来的字节**计
    （分块读，边读边累加，超了就停），不信 zip 清单里声明的大小；`.qgz` 的**每个**成员都要读
    一遍（非 `.qgs` 的读完就丢），因为 `read()` 解压的是整个档案。**档案文件自己的大小**用同一条
    16 MB 判据先查一遍（一次 `stat()`，在进 zip 之前）：解压后的预算对空成员不花钱，而
    `zipfile` 要为 40 万个空成员的 39 MB 档案花 2.5 s、222 MiB 才轮到“成员太多”那条拒绝
    （实测，A7 #2）。
  - `.qgz` 的**成员数**超过 `1000` 也在 `read()` 之前拒：`PROJECT_OPEN_FAILED`，消息里说
    `members`。字节预算按解压后的字节算，空成员不花钱，但每个成员都要 `open()` 一次——实测
    10 万个空成员的 10.4 MB 档案让前扫忙 1.75 s，按成员数线性增长；档案大小那条上限把这条的
    成本也钉住了（16 MB 档案最多约 16 万个成员）。
  - `.qgz` 里声明了加密标志（`flag_bits & 0x1`）或压缩方法不是 stored/deflate 的成员，在
    `read()` **之前** `PROJECT_OPEN_FAILED`；成员边读边校验，清单没说但流里对不上的（CRC、
    声明尺寸）同样拒。理由是 QGIS 3.44 在加密成员上直接崩（实测 `0xC0000005`），
    而这种档案不该交给它。

  总的来说：这一层是**读前尽力而为的清单**，不是“QGIS 会去取什么”的完备枚举。它只问每个值的
  **开头**，所以值中间才出现远程地址的形状它看不见：provider URI `type=xyz&url=https://…`
  （XYZ/WMS 的写法）不再在读前被拒，`read()` 之后逐层的数据源检查仍会移除该层并让整次 open
  `PATH_NOT_ALLOWED`（见 §3），但那一次请求已经发出去了；布局以外的取数落点（符号层里的图片、
  装饰项、……）没有逐个枚举，同样不在承诺内。它拒绝的形状都会在错误消息里说明是“远程数据源”、
  “嵌套太深”、“标签太多”、“太大”或“读不出来”之一。`read()` 之后还有逐层的数据源白名单检查
  （见 §3），工程里说不出的本地路径同样过不了（层会被移除）。
- **容器格式不在这一层策略内**：一个合法本地的 `.vrt` 内部可以写
  `<SourceFilename>/vsicurl/https://…</SourceFilename>`，`layer.add` 与 `project.open`
  看到的都是那个 `.vrt` 文件本身。这是已知的边界（升级条件见 `operations/layer.py`
  的 `shortcut:` 注释）。
- 允许目录**默认为空**，即默认拒绝一切文件访问（已实测：`layer.add` 与 `project.save`
  在未配置时都是 `PATH_NOT_ALLOWED`）。配置方法见 `docs/INSTALL.md`。

### 1.3 调用面

| 方法 | 能调谁 | 返回 |
| --- | --- | --- |
| `run_sync(name, args)` | 只有 main_thread 的 8 个 | 终态记录（`SUCCEEDED` / `FAILED`） |
| `submit(name, args)` | 全部 9 个 | 刚创建的记录快照，通常是 `QUEUED` |
| `get(execution_id)` | — | 记录副本；未知 ID 抛 `EXECUTION_NOT_FOUND` |
| `cancel(execution_id)` | — | `CancelReceipt`，见 `docs/ERRORS.md` §4 |
| `shutdown()` | — | 无返回值；插件 `unload` 内部调用，之后新请求得 `ENGINE_CLOSING` |

全部五个方法（`run_sync`、`submit`、`get`、`cancel`、`shutdown`）都必须从 QGIS 主线程调用；其他线程抛 `WRONG_THREAD`（已实测）。

## 2. `project.create`

替换当前工程为一个指定 CRS 的空工程。

| 参数 | 类型 | 必填 | 默认 | 约束 |
| --- | --- | --- | --- | --- |
| `crs_authid` | StrictStr，长度 ≥ 1 | 否 | `"EPSG:4326"` | 必须能构造有效的 `QgsCoordinateReferenceSystem`；无效则在替换前拒绝 |
| `discard_current` | StrictBool | 否 | `false` | 只有显式 `true` 才允许替换“有内容”的工程 |

| 返回字段 | 类型 | 说明 |
| --- | --- | --- |
| `crs_authid` | string | 工程现在的**规范** authid（传 `epsg:4326` 也返回 `EPSG:4326`） |
| `file_path` | string \| null | 新工程没有关联文件时为 `null` |
| `layer_count` | int | 新工程的图层数，默认 0 |

| 错误码 | 触发 |
| --- | --- |
| `INVALID_CRS` | `crs_authid` 构造不出有效 CRS（在动当前工程之前，已实测） |
| `PROJECT_REPLACE_REQUIRED` | 当前工程有图层、有关联文件或是 dirty，且 `discard_current` 不是 `true`（已实测） |
| `INTERNAL_ERROR` | `iface.newProject(promptToSaveFlag=False)` 返回 `False`；PRD 7.2 没给这种情况指定码 |
| `INVALID_INPUT` | 未知字段 / 类型不符 / 空 `crs_authid` |

成功路径会触发 `QgsProject.aboutToBeCleared`（`iface.newProject` 会清空旧工程），因此
`project_epoch` 加 1，旧代数的排队调用立即 `CANCELLED`。**本次调用自己不受影响**：`_stop_background`
只对后台记录写取消请求，正在执行的主线程调用照常走到 `SUCCEEDED`（实测：`crs_authid` 被规范成
`EPSG:3857`、`file_path: null`、`layer_count: 0`，epoch 1 → 2）。新建之后 `project.get_info` 的
`is_dirty` 可能是 `true`（设置 CRS 就把工程标脏了），这是 QGIS 的事实，不是没保存成功。

## 3. `project.open`

用 `QgsProject.read(path)` 打开本地工程文件；不经过 `iface.addProject()`，所以不会弹任何对话框。

| 参数 | 类型 | 必填 | 默认 | 约束 |
| --- | --- | --- | --- | --- |
| `path` | StrictStr | **是** | — | 绝对路径且后缀为 `.qgs` / `.qgz`（不区分大小写），否则 `INVALID_INPUT`（已实测）；文件必须存在、是普通文件、在 `allowed_project_roots` 内；文件里每一层的数据源（文件**或**目录）还必须在 `allowed_data_roots` 内 |
| `discard_current` | StrictBool | 否 | `false` | 同 `project.create` 的保护条件 |

| 返回字段 | 类型 | 说明 |
| --- | --- | --- |
| `file_path` | string | 规范化（`resolve()`）后的绝对路径 |
| `crs_authid` | string | 工程的 authid；没有 CRS 时可能是 `""` |
| `layer_count` | int | 注册进来的图层数 |
| `invalid_layer_count` | int | 其中 `isValid() == False` 的数量（AT-18B：工程读成功但图层坏掉不算失败，只是不完整） |

**工程里的数据源也要过白名单，而且要在 `read()` 之前拦下远程的。** 工程文件里写的图层
数据源是另一回事：`read()` 会把它们全部构造出来，远程地址就在主线程上发 HTTP、UNC 就在
主线程上做一次带 Windows 凭据的 SMB 连接。所以两步：

1. **先用 XML 解析器问工程里有什么，不碰任何值**：`read()` 之前用标准库 `expat` 解析
   工程 XML，**每个元素的整段文本**（start 到匹配 end 之间的每个字符数据事件依序拼起来，即
   `QDomElement::text()`）与**每个属性值**都过同一条判据：`strip` → 剥掉开头的本地 scheme
   （`file:`、`attachment:`）→ 以 `/vsi`、UNC 前缀（`\\`、`//`）或 `scheme://` **开头**就
   直接 `PATH_NOT_ALLOWED`，消息是
   `the project file names a remote data source; it was not opened`。所以
   `file:./points.csv`（QGIS 自己写的 CSV 图层）与 `file:///C:/…` 放行，`file://host/…` 仍拒；
   布局图片项的 `file=` 属性与 HTML 项的 `<url>http://…</url>` 同样在读前拒——它们不是数据源，
   但 `read()` 会去取（A11 #1）；值本身就是 URL 的元数据（`<identifier>https://…</identifier>`）
   也会命中，见 §12。
   这一步只读字节、不建树，**当前工程原样保留**（已实测：含 `/vsicurl` / UNC
   数据源的工程 0–16 ms 返回，旧工程与它的图层都还在；一个把 `>` 藏在数据源属性里的工程
   返回 16 ms、监听端口 **0 次 TCP 连接**，旧扫描会给它 9 次；一个把 URI 放在子元素 `tail`
   里的工程同样是 0 次 TCP，而只读 `element.text` 时 `read()` 会发出 42 次 HTTP 请求；一个把
   URI 拆成五段、夹在三个子元素之间的工程（`.qgs` 与 `.qgz`）也是 0 次连接，而逐段判时
   `project.open` 在失败之前已发了 8 次 TCP，实测）。
   另外几种情况同样在读之前拒：
   - 文件不是 UTF-8（字节里有 NUL，例如 UTF-16 工程）→ `PATH_NOT_ALLOWED`，消息
     `the project file is not UTF-8; it was not opened`；
   - 文件声明了 XML 实体（`<!ENTITY`，或 `<!DOCTYPE` 后出现 `[`）→ `PATH_NOT_ALLOWED`，消息
     `the project file declares XML entities`。expat 会把实体展开进元素文本，展开后的体积不进
     任何预算（实测：1 MiB 实体读 50 次的 1 MiB 工程让前扫峰值 100 MiB，8 MiB x 90 峰值
     1,440 MiB）；QGIS 自己写的 `<!DOCTYPE qgis PUBLIC '…' 'SYSTEM'>` 没有 `[`，不会误拒；
   - XML 嵌套超过 256 层 → `PROJECT_OPEN_FAILED`，消息里说 `nests deeper than 256 levels`。
     深度是按解析事件数的，注释/CDATA 里的尖括号不算。上限按 `read()` 的**耗时**标定：实测
     200 层嵌套分组 0.28 s、600 层 8.84 s、900 层 30.11 s（同样体积的 4.2 MB 平铺工程 0.14 s），
     而 QGIS 自己写的 100 层嵌套分组图层树只有 105 层深；5500 层还能开、6000 层才带
     `0xC00000FD` 崩，但停 30 s 的主线程已经是一个无错误、无记录、取消不了的开工程；
   - XML 标签超过 400000 个 → `PROJECT_OPEN_FAILED`，消息里说 `more than 400000 XML tags`。
     判据是一次 `bytes.count(b"<")`（元素数的上界），把解析耗时钉在有界范围（实测 400000 个
     标签 0.12–0.19 s；真实 16 MB 工程的标签密度是 42.6–61 字节/标签，即 26–39 万个）；
   - 工程文件的字节不可解析（不是合法 XML）→ `PROJECT_OPEN_FAILED`，用的是 `read()` 失败时
     同一句消息，文件**不会**再交给 `read()`；
   - 工程文件本身，或 `.qgz` 里**每个**成员解压后的总量，超过 16 MB → `PATH_NOT_ALLOWED`，
     消息 `the project file is too large; it was not opened`。预算按**实际解压出的字节**分块
     累加（不信 zip 清单声明的尺寸），超了就停；非 `.qgs` 成员也会读一遍再丢，因为 `read()`
     解压的是整个档案。逐个成员判不够：100 个 1 MB 成员的 112 KB 档案能吃掉 200 MB；
     **档案文件自己的大小**用同一条 16 MB 判据在进 zip 之前先查（一次 `stat()`）：解压后的
     预算对空成员不花钱，而 `zipfile` 要为 40 万个空成员的 39 MB 档案花 2.5 s / 222 MiB 才
     轮到“成员太多”那条拒绝（实测，A7 #2）；
   - `.qgz` 的**成员数**超过 1000 → `PROJECT_OPEN_FAILED`，消息里说 `members`。字节预算按解压
     后的字节算，空成员不花钱，但每个成员都要 `open()` 一次（实测：10 万个空成员的 10.4 MB
     档案让前扫忙 1.75 s，按成员数线性增长；档案大小上限把这条的成本钉在 16 MB 以内）；
   - `.qgz` 里有加密（`flag_bits & 0x1`）或压缩方法不是 stored/deflate 的成员，或者流里
     对不上清单（CRC、尺寸）→ `PROJECT_OPEN_FAILED`，用的是 `read()` 失败时同一句消息。
     **这种档案不会交给 `read()`**：QGIS 3.44 在它上面会直接崩掉（实测，退出码 `0xC0000005`，
     非 `.qgs` 的兄弟成员一样能打崩）。
2. **再逐层比对根目录**：`read()` 之后用 provider 自己的 `decodeUri()` 取真实路径，问
   `allowed_data_roots`。
   - 有数据源落在根外（或 provider 说不出一个本地路径，或 `decodeUri()` 自己抛异常）→ 这些
     图层用 `removeMapLayer()` 从工程里移除，然后整次 `project.open` 以 `PATH_NOT_ALLOWED`
     失败，`message` 里带被移除的图层数。**不是**“加载成功 + 只报个字段”：那等于白名单被绕过。
   - 数据源是**目录**（ESRI FileGDB）不算越界：只要目录本身在 `allowed_data_roots` 内。
   - 数据源文件**不存在**不算越界（它还在根内）：那是 AT-18B 的部分加载，照常 `SUCCEEDED` +
     `invalid_layer_count > 0`。**数据源的父目录不存在也算**：判定只问“解析后的路径是否落在根内”，
     不要求路径存在（已实测：来源为 `root/moved-away/cities.gpkg`、目录已被搬走的工程 →
     `SUCCEEDED`，`layer_count 1`、`invalid_layer_count 1`，图层没有被移除）。
   - 工程文件本身已经 `read()` 进去了，所以这一步失败时**当前工程已经被替换成“去掉越界层之后的
     剩货”**，不回滚；`message` 说的就是这件事（`they were removed and the project was replaced`）。

**要两个键都配。** 只配 `allowed_project_roots`、`allowed_data_roots` 留空的操作者，打开任何
**含图层**的工程都会失败（实测：`FAILED(PATH_NOT_ALLOWED)`，`layers` 剩 0，`fileName()` 已经是
那个被拒绝的工程文件）；空列表的语义就是“一个数据源都不许”。memory 图层、WMS/XYZ 底图、
PostGIS 图层这类说不出本地路径的层算越界，同样会让整次打开失败并把它们移除。

| 错误码 | 触发 |
| --- | --- |
| `INVALID_INPUT` | 路径不是绝对路径、后缀不对、未知字段、布尔类型不符 |
| `PATH_NOT_ALLOWED` | 文件不存在 / 不在允许根目录内 / 是网络 URI 或目录（“不存在”也归这里，已实测）；或工程文件里某个元素的整段文本 / 某个属性值在剥掉本地 `file:` / `attachment:` scheme 后以 `/vsi`、UNC 前缀或 `scheme://` **开头**（**`read()` 之前**就拒，见 §1.2；值本身就是 URL 的元数据也会命中，见 §12）；或工程文件不是 UTF-8、声明了 XML 实体、工程文件或 `.qgz` 档案本身 / 每个成员解压后的总量超过 16 MB（同样在 `read()` 之前）；或工程里某层的数据源不在 `allowed_data_roots` 内（该层会被移除，见上） |
| `PROJECT_REPLACE_REQUIRED` | 当前工程有内容且 `discard_current` 不是 `true`（已实测） |
| `PROJECT_OPEN_FAILED` | `project.read()` 返回 `False`，或工程 XML 嵌套超过 256 层、标签超过 400000 个、`.qgz` 成员超过 1000 个、不是合法 XML（都在 `read()` 之前拒绝：前者会让 QGIS 解析器爆栈、进程消失，其余不是工程），或 `.qgz` 里有取不出来的成员（加密、不支持的压缩方法）。消息里明说“当前工程可能已经改变”，**不承诺回滚**（损坏文件已实测） |

成功路径同样会触发 `QgsProject.aboutToBeCleared`（`read()` 会清空旧工程），所以 `project_epoch` 加 1、
旧代数的排队调用立即 `CANCELLED`；**本次调用自己照常成功**（实测：两个键都指向数据与工程所在目录，
打开一个含 1 个图层的工程得到 `layer_count: 1`、`invalid_layer_count: 0`，epoch 0 → 1，记录终态
`SUCCEEDED`）。四个失败路径（`INVALID_INPUT`、`PATH_NOT_ALLOWED`、`PROJECT_REPLACE_REQUIRED`、
`PROJECT_OPEN_FAILED`）与成功路径互不干扰。

## 4. `project.save`

把当前工程写到本地文件。

| 参数 | 类型 | 必填 | 默认 | 约束 |
| --- | --- | --- | --- | --- |
| `path` | StrictStr \| null | 否 | `null` | 为空则用工程当前关联文件；给了就必须是绝对 `.qgs`/`.qgz` |
| `overwrite` | StrictBool | 否 | `false` | 目标已存在（**包括**关联文件本身）时必须显式 `true` |

| 返回字段 | 类型 | 说明 |
| --- | --- | --- |
| `file_path` | string | 实际写入的绝对路径 |
| `is_dirty` | bool | 写完之后工程的真实 dirty 状态；成功写入通常为 `false` |

| 错误码 | 触发 |
| --- | --- |
| `PROJECT_PATH_REQUIRED` | 没给 `path`，且工程没有关联文件（已实测） |
| `INVALID_INPUT` | `path` 形状不对 / 未知字段 / 布尔类型不符 |
| `PATH_NOT_ALLOWED` | 父目录不存在、不在允许根目录内、目标是目录 |
| `TARGET_EXISTS` | 目标已存在且没有 `overwrite=true`（已实测；重复保存同一路径即命中） |
| `PROJECT_SAVE_FAILED` | `project.write()` 返回 `False`；**文件可能已部分写入**，不承诺原子性 |

## 5. `project.get_info`

| 参数 | 说明 |
| --- | --- |
| 无 | 任何参数都 `INVALID_INPUT`（已实测） |

| 返回字段 | 类型 | 说明 |
| --- | --- | --- |
| `file_path` | string \| null | 关联文件；未保存过为 `null` |
| `crs_authid` | string | 工程 CRS；工程没有 CRS 时为 `""`（headless 空工程实测如此） |
| `is_dirty` | bool | 是否有未保存修改 |
| `layer_count` | int | 图层数 |
| `project_epoch` | int | 插件本地的工程代数，从 0 开始，每次 `aboutToBeCleared` 加 1 |

没有业务错误码。它不保存、不切图层、不改工程属性。

## 6. `layer.add`

把本地矢量或栅格文件加进当前工程。

| 参数 | 类型 | 必填 | 默认 | 约束 |
| --- | --- | --- | --- | --- |
| `kind` | enum `vector` \| `raster` | **是** | — | 决定 provider：vector 固定 `ogr`，raster 固定 `gdal`；调用方不能指定 provider |
| `source_path` | StrictStr，长度 ≥ 1 | **是** | — | 绝对、存在、普通文件、在 `allowed_data_roots` 内 |
| `name` | StrictStr | **是** | — | 1–128 字符 |
| `sub_layer` | StrictStr \| null | 否 | `null` | 仅 vector 允许；`^[A-Za-z0-9_-]{1,64}$`；URI 由 OGR provider 的 `encodeUri` 生成，不手拼 |
| `add_to_legend` | StrictBool | 否 | `true` | `false` 时不创建图层树节点 |

| 返回字段 | 类型 | 说明 |
| --- | --- | --- |
| `layer_id` | string | QGIS 分配的图层 ID，后续操作用它 |
| `name` | string | 实际名称（与入参一致） |
| `kind` | enum `vector` \| `raster` | 入参里的 kind |
| `provider` | string | `ogr` 或 `gdal` |
| `crs_authid` | string | 图层 CRS |

| 错误码 | 触发 |
| --- | --- |
| `INVALID_INPUT` | kind 不是两个值之一、空路径/空名字、名字超 128、`sub_layer` 字符不合规或配在 raster 上、布尔类型不符（raster + `sub_layer` 与字符串布尔已实测，其余靠单测） |
| `PATH_NOT_ALLOWED` | 路径不在允许根目录内 / 不存在 / 是 URI（已实测） |
| `INVALID_LAYER` | `QgsVectorLayer`/`QgsRasterLayer` 构造后 `isValid()` 为假（坏文件已实测） |

不修改源文件，也不对同名图层做任何处理。`add_to_legend=false` 的图层没有树节点，
之后 `layer.set_visibility` 会对它返回 `LAYER_NOT_IN_TREE`（已实测）。

## 7. `layer.list`

| 参数 | 说明 |
| --- | --- |
| 无 | 任何参数都 `INVALID_INPUT` |

返回 `{"layers": [LayerSummary, ...]}`，按 `layer_id` 字典序升序（不依赖 `mapLayers()` 的迭代顺序）。

| LayerSummary 字段 | 类型 | 说明 |
| --- | --- | --- |
| `layer_id` | string | |
| `name` | string | |
| `kind` | enum `vector` \| `raster` \| `other` | `other` 是既非矢量也非栅格的注册图层 |
| `crs_authid` | string | |
| `in_layer_tree` | bool | 当前工程图层树里有没有它的节点 |
| `visible` | bool \| null | 节点自身的 `itemVisibilityChecked()`；没有节点时为 `null` |
| `effective_visible` | bool \| null | 节点的 `isVisible()`（受祖先组影响）；没有节点时为 `null` |

`visible` 与 `effective_visible` 是两个字段：父级组被隐藏时 `visible=true`、
`effective_visible=false`，不合并成一个数。没有业务错误码。

## 8. `layer.get_info`

| 参数 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `layer_id` | StrictStr | **是** | 长度 ≥ 1 |

| 返回字段 | 类型 | 说明 |
| --- | --- | --- |
| `layer_id`、`name`、`kind`、`crs_authid` | — | 同 `LayerSummary` |
| `provider` | string | 图层的 provider 类型 |
| `valid` | bool | `layer.isValid()`：数据源现在还读得到吗 |
| `in_layer_tree`、`visible`、`effective_visible` | — | 与 `layer.list` **同一套计算**，不会两处不一致 |

| 错误码 | 触发 |
| --- | --- |
| `INVALID_INPUT` | `layer_id` 缺失/为空/不是字符串 |
| `LAYER_NOT_FOUND` | 工程里没有这个 ID（已实测） |

不返回原始数据源 URI、连接串、要素或字段列表。

## 9. `layer.set_visibility`

| 参数 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `layer_id` | StrictStr | **是** | 长度 ≥ 1 |
| `visible` | StrictBool | **是** | 目标是节点自身的勾选状态 |

| 返回字段 | 类型 | 说明 |
| --- | --- | --- |
| `layer_id` | string | |
| `visible` | bool | 设置之后节点的勾选状态 |
| `effective_visible` | bool | `node.isVisible()`；父级组隐藏时仍可能是 `false` |
| `changed` | bool | 本次是否真的改了（同状态重复调用为 `false`） |

| 错误码 | 触发 |
| --- | --- |
| `INVALID_INPUT` | `layer_id` 为空 / `visible` 不是真布尔 |
| `LAYER_NOT_FOUND` | 工程里没有这个 ID（已实测） |
| `LAYER_NOT_IN_TREE` | 图层已注册但没有图层树节点，典型是 `layer.add(add_to_legend=false)`（已实测） |

只动这一个节点，不碰父级组、不碰其他图层、不改数据源。

## 10. `layer.sum_numeric_field`

唯一的后台 Operation：主线程拷一份数值快照，`QgsTask` 在别的线程求和，主线程校验代数后返回。

| 参数 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `layer_id` | StrictStr | **是** | 长度 ≥ 1 |
| `field_name` | StrictStr | **是** | 长度 ≥ 1；图层上要有这个字段，且是整型或浮点 |

| 返回字段 | 类型 | 说明 |
| --- | --- | --- |
| `layer_id`、`field_name` | string | 入参原样返回 |
| `count` | int | 参与求和的**非 null** 要素数 |
| `skipped_null` | int | 跳过的 null 数 |
| `total` | float | 快照的求和结果 |
| `snapshot_project_epoch` | int | 快照取自哪一代工程 |

| 错误码 | 触发 |
| --- | --- |
| `INVALID_INPUT` | 任一参数缺失/为空/类型不符（在 `submit` 返回前就判掉，不排队） |
| `LAYER_NOT_FOUND` | 工程里没有这个 ID |
| `INVALID_LAYER` | 不是 `QgsVectorLayer`，或图层 `isValid()` 为假 |
| `INVALID_FIELD_TYPE` | 没有该字段；字段不是数值型（文本字段已实测）；字段里存在 NaN/Infinity；或求和溢出成 Infinity（两个有限值相加也可以，见下） |
| `SNAPSHOT_LIMIT_EXCEEDED` | 要素数超过 `max_snapshot_features`（默认 10000）；**不做静默截断**（10001 个要素已实测） |
| `STALE_PROJECT` | 任务结束时 `project_epoch` 已经变了：结果算完也不交 |
| `TASK_SUBMISSION_FAILED` | 没装后台适配器，或 `QgsTaskManager.addTask()` 返回 0 |
| `UNSUPPORTED_EXECUTION_MODE` | 用 `run_sync` 调它（控制面异常，已实测） |

行为细节：

- 只有这一段是后台的：`prepare_main`（主线程读要素、跳过 null、拷成 float 元组）、
  `execute_worker`（后台求和）、`finalize_main`（主线程只比对 epoch）。
- **非有限值直接拒**：SQLite/GeoPackage 的 `DOUBLE` 可以存 NaN/Infinity，QGIS 原样交出来。
  这种值会让 `total` 变成 `NaN`，而 `NaN` 不是合法 JSON（RFC 8259），`model_dump(mode="json")`
  会把它带进记录，按 RFC 8259 解析的调用方拿到的是一条坏记录而不是一条错误——所以
  `prepare_main` 遇到 `math.isfinite(value)` 为假的要素就 `INVALID_FIELD_TYPE`，消息是
  `the field holds a non-finite (NaN or infinite) value`，结果一个都不交（已实测：`(1.0, nan, inf)`
  的内存图层 → `FAILED(INVALID_FIELD_TYPE)`，`result` 为 null）。
- **求和溢出同样拒**：每一项都有限，和仍可能是无穷（`1e308 + 1e308` 是 `inf`）。
  `execute_worker` 返回前判 `math.isfinite(total)`，不成立就抛 `INVALID_FIELD_TYPE`，消息是
  `the sum of the field is not a finite number`；后台适配器把 worker 自己的 `FrameworkError`
  按它的码写入记录，所以调用方看到的是一条错误而不是一条带 `inf` 的 `SUCCEEDED` 记录
  （`json.dumps(..., allow_nan=False)` 会拒那条记录）。
- 进度走 `QgsTask.setProgress`，记录最多每 100 ms 写一次，且**不写 100**——100 只由
  `SUCCEEDED` 那一步写，否则一个 3 ms 的任务会留下“100% 但已取消”的记录。首条报告会立刻写
  （并推进到 `RUNNING`），完成前还会补写最后一次被节流的值；一条可用报告都没有时（快照里只剩
  一个非 null 值），`RUNNING` 由完成路径补写。
- 任务声明了 `dependent_layer_ids`，适配器调用 `setDependentLayers`：图层被删时 QGIS 自己
  取消任务。
- 结果描述的是快照，不是任务结束那一刻图层里的值。
- 实测量级：4 要素（1 个 null）→ `count=3`、`skipped_null=1`、`total=225.0`；9000 要素 →
  `total=40495500.0`，状态观察到 `QUEUED → SCHEDULED → SUCCEEDED`，worker 在 Qt 线程池线程
  （`Dummy-1`）上跑，`finished` 回调在 `MainThread`。

## 11. 一次调用会碰到的其它码

这些不看 Operation 是谁，只看你怎么调（细节见 `docs/ERRORS.md`）：

| 码 | 何时 |
| --- | --- |
| `OPERATION_NOT_FOUND` | `name` 不在注册表里（控制面） |
| `WRONG_THREAD` | 不在创建引擎的那个线程上调（控制面） |
| `ENGINE_CLOSING` | 插件正在卸载（控制面） |
| `EXECUTION_NOT_FOUND` | `get`/`cancel` 的 ID 不存在或已被淘汰（控制面） |
| `TOO_MANY_EXECUTIONS` | `submit` 时排队数已达上限（实测：`max_pending=32` 时第 33 个 `submit` 得到 `FAILED`） |
| `OUTPUT_VALIDATION_FAILED` | Operation 的返回值不合它自己声明的输出模型 |
| `INTERNAL_ERROR` | 未预期异常；`message` 是下面几种之一（详见 `docs/ERRORS.md` §3）：`<异常类型名>: unexpected failure`、`QGIS did not create a new project`、`the task ended without a result`、`the completion of this execution failed`，或状态机拒绝的 `<id>: <旧状态> -> <新状态> is not a transition` |

## 12. 与 PRD 的差异（代码是事实）

| PRD | 代码 | 说明 |
| --- | --- | --- |
| 7.3 打开含坏图层的工程应 `PROJECT_OPEN_FAILED` | 返回 `SUCCEEDED` + `invalid_layer_count > 0` | 按 AT-18B 实现：读成功就是成功，但不谎称全部数据源可用 |
| 7.4 成功样例 `is_dirty: false` | 返回 `project.write()` 之后的真实值 | 通常情况下仍是 `false`，但这是读出来的，不是写死的 |
| 6.1 “兼容 Pydantic v1/v2” | 只支持 v2，`pydantic_major` 恒为 2 | 已记录偏差；非 v2 的 pydantic 在插件导入时直接 `RuntimeError` |
| 11.1 `error.details` | 字段存在但永远是 `null` | `FrameworkError` 只带 `code` + `message`，没有 details 通路 |
| 7.3 / 11.2 “含无法加载的图层 → `PROJECT_OPEN_FAILED`” | `SUCCEEDED` + `invalid_layer_count > 0` | 同一份 PRD 里这两句与 AT-18B（`read()` 为真但有坏图层 → 成功 + 计数）互斥。**以 AT-18B 与这里的代码为准**：读成功就是成功，坏图层只计数、不谎称可用。评审时不要按 7.3/11.2 那两句改代码 |
| 7.3 未提工程内数据源 | `project.open` 在 `read()` 前拒远程数据源，`read()` 后逐层校验数据源，越界则移除该层并整体 `PATH_NOT_ALLOWED` | 比 PRD 更严：PRD 2.3/10.2 要的是数据白名单，而数据源是工程文件替调用方写的路径，所以它们也得过 `allowed_data_roots`（已实测，见 `PROJECT_OPEN_FAILED` 之外的这条码）。读前那一半只是尽力而为的清单，边界见 §1.2 |
| 未定义：数据源是目录 | 数据源可以是目录（ESRI FileGDB），只要目录在 `allowed_data_roots` 内 | 保存目标仍然必须是文件；只有“工程里的数据源”放宽到目录，否则普通工程里一张 FileGDB 图层就会让整次打开失败 |
| 未定义：容器格式内部的数据源 | 不检查：一个本地 `.vrt` 内部的 `<SourceFilename>` 不在这层策略内 | 已知边界，不是承诺；升级条件写在 `operations/layer.py` 的 `layer_source_uri` 的 `shortcut:` 注释里（有人真的把 VRT 当不可信输入时） |
| 未定义：provider 说不出本地路径 | 这种图层算越界（移除 + 失败） | 宁可拒绝：白名单无法为一个说不出来源的层担保；工程里出现 `memory`、WMS/XYZ 等非文件 provider 时 `project.open` 会失败 |
| 未定义：值本身就是 URL 的元数据 | 读前扫描会拒（`PATH_NOT_ALLOWED`） | 统一判据的已知误拒：判据不认元素名/属性名，任何值以 `scheme://` 开头就拒，所以 `<identifier>https://…</identifier>`、`Option value="https://…"` 这种工程打不开；正文里引用 URL（`<abstract>Downloaded from https://…</abstract>`）仍然放行。要打开这种工程，先把元数据里的 URL 去掉 |
| 未定义：provider URI 里 `key=` 之后的远程地址 | 读前不拒；`read()` 之后逐层检查会移除该层并整体 `PATH_NOT_ALLOWED` | 判据只看值的开头（`type=xyz&url=https://…` 是 QGIS 写 XYZ/WMS 的方式），所以这一次请求会先发出去。要堵住它得做子串匹配，而子串匹配会把正文里引用 URL 的正常工程拒掉（A6 #3）——取舍记在这里，不是没看见 |
