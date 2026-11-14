# qgis-agent-plugin

QGIS Desktop 的进程内操作框架：把 QGIS 的能力包装成具名操作，供 agent 在 QGIS 主线程上调用。
没有网络服务，没有端口，没有远程调用方——所有能力都通过 Python 对象在同一个进程里被调用。

## 执行

**Operation（操作）**：
一个具名能力，带自己的输入模型、输出模型、执行策略与破坏性标记。操作按能力族分：`project.*`、`layer.*`、`processing.*`。
_Avoid_：action、tool、endpoint、capability

**Command（命令）/ Query（查询）**：
操作的两个种类。命令可能改变用户可见的状态；查询只读，副作用只有缓存与日志。区分的是意图，不是"有没有写磁盘"。
_Avoid_：write operation、read operation、mutating call

**Execution（执行）**：
一次操作调用的完整过程，从排队到终态，由执行 id 标识。同一个操作被调两次是两个执行。
_Avoid_：run、job、call、task

**Record（记录）**：
一次执行的对外可观察状态——id、状态、进度、结果、错误。调用方只看记录，看不到执行中的对象。
_Avoid_：result、response、log entry

**Engine（引擎）**：
唯一的调用入口，负责校验、排队、调度、状态迁移、取消与记录仓库。
_Avoid_：dispatcher、scheduler、service、broker

**Registry（注册表）**：
名字到操作的映射。插件加载时一次注册完，之后只读。
_Avoid_：catalog、plugin manager

**Spec（契约快照）**：
一个操作的机器可读声明——名字、描述、种类、执行策略、破坏性，以及输入与输出的 JSON Schema。
_Avoid_：manifest、metadata、schema

**Strategy（执行策略）**：
操作在哪里跑：主线程，或后台线程池。它与命令/查询是两个独立维度，不是"同步/异步"的同义词。
_Avoid_：sync/async、blocking/non-blocking、mode

**Status（状态）**：
一次执行在生命周期里的位置，从排队开始，到三个终态之一结束。状态只由记录仓库写。
_Avoid_：phase、stage、step

**Terminal（终态）**：
成功、失败、取消三者之一。进入终态之后，这条记录不能再写。
_Avoid_：finished、closed、done、completed

## 线程与工程

**Main thread（主线程）**：
QGIS 的 GUI 线程，也是引擎与注册表的所属线程。引擎的五个调用方法都只能在这里调。
_Avoid_：UI thread、GUI loop、event loop

**Worker（后台线程）**：
Qt 线程池里跑后台操作执行阶段的线程。这里碰不到 QGIS 对象，也没有主线程的上下文。
_Avoid_：background thread、subprocess、job thread

**Snapshot（快照）**：
在主线程上拷出来的纯数据，跨线程交给 worker。结果描述快照那一刻的数据，不是结束时的实时值。
_Avoid_：copy、cache、buffer、dump

**Epoch（工程代数）**：
当前工程的代号，每次工程被替换就加一。它回答的是"这次计算的结果还属不属于眼前这个工程"。
_Avoid_：version、revision、generation、project id

**Pre-scan（读前扫描）**：
把工程文件交给 QGIS 读取之前，先按字节审一遍内容的步骤。目的是不让主线程替文件发出网络往返。
_Avoid_：validation、sanitization、precheck、guard

## 失败与风险

**Destructive（破坏性）**：
这次调用可能让用户丢掉没保存的东西——替换当前工程，或覆盖已有文件。它是给调用方看风险的标记，不改变引擎行为。
_Avoid_：dangerous、mutating、write、risky

**Control-plane failure（控制面失败）**：
调用本身无效——名字不存在、线程不对、引擎已关闭、对后台操作用了同步执行。它以异常形式抛出，不产生记录。
_Avoid_：validation error、bad request、404

**Failed record（失败记录）**：
调用有效但执行失败。它是一份记录，错误码在记录里，调用方按错误码分支。
_Avoid_：exception、error response、crash

**Cancel（取消）**：
请求停止一次执行，回执说明请求被接受与否。它是协作式的：后台任务只有自己报告了停止才算真的取消，主线程已开始的操作不可中断。
_Avoid_：abort、kill、stop、terminate
