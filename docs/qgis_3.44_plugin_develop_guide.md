# QGIS 3.44 插件开发最佳实践指南

> **适用版本：** QGIS 3.44.x  
> **主要开发语言：** Python 3  
> **主要开发接口：** PyQGIS、PyQt（通过 `qgis.PyQt` 访问）  
> **适用对象：** QGIS Desktop Python 插件开发者  
> **参考基线：** QGIS 3.44 官方开发者指南、PyQGIS Developer Cookbook、QGIS `release-3_44` 源码分支。

---

# 目录

1. [指南说明与开发原则](#1-指南说明与开发原则)
2. [QGIS 插件类型与架构选择](#2-qgis-插件类型与架构选择)
3. [开发环境搭建](#3-开发环境搭建)
4. [插件目录结构与组织方式](#4-插件目录结构与组织方式)
5. [metadata.txt 元数据规范](#5-metadatatxt-元数据规范)
6. [插件加载机制与生命周期](#6-插件加载机制与生命周期)
7. [插件主界面与 Qt GUI 开发](#7-插件主界面与-qt-gui-开发)
8. [资源文件与路径管理](#8-资源文件与路径管理)
9. [信号与槽机制](#9-信号与槽机制)
10. [QGIS 项目与图层管理](#10-qgis-项目与图层管理)
11. [矢量图层开发](#11-矢量图层开发)
12. [栅格图层开发](#12-栅格图层开发)
13. [几何处理与坐标参考系](#13-几何处理与坐标参考系)
14. [地图画布与地图交互工具](#14-地图画布与地图交互工具)
15. [地图渲染、符号系统与布局](#15-地图渲染符号系统与布局)
16. [QGIS 表达式系统](#16-qgis-表达式系统)
17. [Processing 算法与 Provider 开发](#17-processing-算法与-provider-开发)
18. [后台任务与耗时操作](#18-后台任务与耗时操作)
19. [配置管理与数据持久化](#19-配置管理与数据持久化)
20. [网络访问与认证管理](#20-网络访问与认证管理)
21. [日志、异常处理与调试](#21-日志异常处理与调试)
22. [国际化与多语言支持](#22-国际化与多语言支持)
23. [Python 依赖与第三方库管理](#23-python-依赖与第三方库管理)
24. [测试与质量控制](#24-测试与质量控制)
25. [性能优化](#25-性能优化)
26. [安全性与稳定性](#26-安全性与稳定性)
27. [插件打包、发布与版本管理](#27-插件打包发布与版本管理)
28. [完整插件开发示例](#28-完整插件开发示例)
29. [常见问题与错误处理](#29-常见问题与错误处理)
30. [插件发布检查清单](#30-插件发布检查清单)
31. [官方文档与源码参考](#31-官方文档与源码参考)

---

# 1. 指南说明与开发原则

## 1.1 指南范围

QGIS 提供多种扩展开发方式，本指南主要讨论 QGIS Desktop 的 Python 插件开发。

Python 插件运行在 QGIS 进程内部，能够访问 QGIS 已经初始化的核心库、GUI 库、项目对象、地图画布、图层管理器和 Processing 框架。

一个典型的 Python 插件可以完成：

- 添加菜单、工具栏按钮和快捷操作。
- 创建独立对话框、停靠面板和配置界面。
- 读取、创建、修改和删除 QGIS 图层。
- 操作当前 QGIS 项目及其图层树。
- 执行矢量、栅格和空间分析。
- 创建地图交互工具。
- 控制地图渲染、符号和布局。
- 注册 Processing 算法。
- 执行后台任务。
- 访问网络服务和数据库。
- 保存用户配置和项目级配置。
- 扩展 QGIS 的表达式、符号和图层功能。

本指南不把独立 PyQGIS 应用程序、QGIS Server 插件和 C++ 插件作为主要开发对象，但会说明它们与 Desktop Python 插件的区别。

## 1.2 QGIS 3.44 的开发基线

开发时应当始终使用 QGIS 3.44 对应的 API 文档和源码。

主要模块如下：

| 模块 | 主要用途 |
|---|---|
| `qgis.core` | 项目、图层、几何、CRS、数据提供者、Processing、任务等核心功能 |
| `qgis.gui` | 地图画布、地图工具、图层树视图及 GUI 相关的 QGIS 控件 |
| `qgis.PyQt` | Qt 和 PyQt 的兼容访问入口 |
| `qgis.utils` | 插件加载、卸载、运行时环境及插件访问辅助功能 |
| `processing` | Processing 框架及算法执行接口 |

QGIS 的 PyQGIS API 与 C++ API 在设计上基本对应，但并不是简单地把 C++ 代码逐行翻译成 Python。Python 绑定还受到 SIP、Python 类型转换、对象所有权和生命周期的影响。

开发过程中应当：

1. 优先使用 QGIS 3.44 官方 Python API。
2. 使用 `qgis.PyQt`，不要自行混用不同版本的 PyQt。
3. 优先使用 QGIS 提供的对象和工具，不重复实现 QGIS 已经具备的能力。
4. 尽量使用公开的 `QgisInterface` 接口访问 QGIS 主程序。
5. 对可能被卸载、删除或替换的对象，明确处理其生命周期。
6. 把数据处理逻辑与 GUI 操作分开。
7. 对耗时操作使用 QGIS 任务系统。
8. 保证插件能够正确卸载，而不只是能够成功加载。

## 1.3 最重要的开发原则

可以把 QGIS 插件开发理解成三件事情：

- **接入 QGIS：** 插件能够被 QGIS 正确发现、加载、初始化和卸载。
- **使用 QGIS：** 正确使用项目、图层、地图画布、数据提供者和 Processing 等功能。
- **管理自身：** 正确管理界面、信号、任务、资源、配置和外部依赖。

实际开发中，第三件事经常被忽略。

插件第一次运行成功，并不代表插件开发完成。插件还需要能够重复启用、重复关闭、切换项目、删除图层、执行后台任务，并在这些操作之后保持正常。

---

# 2. QGIS 插件类型与架构选择

## 2.1 Python 插件

这是最常见的 QGIS 插件类型。

Python 插件通过一个 Python 包接入 QGIS，使用 `classFactory(iface)` 创建插件对象。

适合：

- GIS 业务工具。
- 数据处理工具。
- 数据检查和质量控制。
- 数据导入导出。
- 地图交互工具。
- 数据库管理工具。
- Web GIS 服务调用。
- 自定义 GUI。
- Processing 算法扩展。

绝大多数业务型 QGIS 插件都可以采用这种方式开发。

## 2.2 Processing 算法插件

如果插件的主要工作是处理数据，而不是提供复杂的交互界面，优先考虑 Processing。

Processing 算法可以直接集成到：

- Processing Toolbox。
- Processing Model Designer。
- Batch Processing。
- Processing 历史记录。
- Processing 脚本和自动化工作流。

例如：

- 缓冲区分析。
- 空间叠加分析。
- 数据格式转换。
- 字段计算。
- 栅格重分类。
- 数据质量检查。
- 批量空间分析。

相比自己创建一套输入输出界面，Processing 已经提供参数管理、输入检查、输出管理、批处理、模型集成和执行反馈。

**建议：**

如果一个功能本质上是输入数据、设置参数、执行计算、输出结果，就优先考虑 `QgsProcessingAlgorithm`。

如果一个功能主要涉及用户交互、配置管理、地图操作或多个步骤之间的协调，就使用普通 Python 插件。

两者也可以组合：插件提供业务界面，Processing Provider 提供可复用算法。

## 2.3 Python 表达式函数

如果需要扩展 QGIS 表达式计算能力，可以注册自定义表达式函数。

例如：

- 自定义字段计算函数。
- 特定业务规则计算。
- 自定义空间关系判断。
- 特殊字符串和日期处理。

可以通过 `qgsfunction` 装饰器或 `QgsExpression` 的注册机制实现。

不需要为了提供一个简单计算函数而开发完整的复杂插件界面。

## 2.4 自定义地图工具

如果主要功能是让用户在地图画布上点击、拖动、绘制或者选择地物，可以继承 `QgsMapTool` 或其派生类。

典型功能：

- 鼠标点击识别地物。
- 框选地图范围。
- 绘制线和面。
- 交互式测量。
- 地图上选择控制点。
- 绘制业务范围。
- 交互式地物编辑。

## 2.5 自定义插件图层

当现有 `QgsVectorLayer`、`QgsRasterLayer` 等图层类型不能满足需求，并且确实需要创建一种新的图层对象时，可以考虑继承 `QgsPluginLayer`。

例如：

- 特殊数据源的自定义渲染。
- 需要自定义地图绘制流程的图层。
- 不属于常规矢量和栅格数据的可视化内容。

这类扩展涉及图层创建、渲染、项目 XML 读写和图层类型注册，不应当作为普通业务插件的默认选择。

## 2.6 C++ 插件

当 Python API 无法满足性能要求、需要访问未暴露给 Python 的底层能力，或者需要与 C++ 库深度集成时，再考虑 C++ 插件。

对于普通业务功能，不建议一开始就使用 C++。

## 2.7 QGIS Server 插件

QGIS Server 插件与 Desktop Python 插件的运行环境不同。

Server 插件使用 `serverClassFactory(serverIface)`，而不是 Desktop 插件的 `classFactory(iface)`。

如果插件同时需要支持 Desktop 和 Server，应当明确区分两套入口和运行环境，不要把两种生命周期混为一谈。

---

# 3. 开发环境搭建

## 3.1 安装 QGIS 3.44

建议安装 QGIS 3.44 对应的正式发行版本，并以该版本作为主要开发和测试环境。

开发环境至少应当包括：

- QGIS 3.44。
- QGIS 自带的 Python 运行环境。
- 对应的 Qt 和 PyQt 库。
- Python 开发工具。
- Qt Designer（需要设计界面时）。
- Git。

Windows 环境建议使用 OSGeo4W 安装体系。

Linux 和 macOS 环境应使用当前 QGIS 安装所提供的 Python 和依赖环境。

## 3.2 不要把系统 Python 当成 QGIS Python

这是 QGIS 插件开发中最容易遇到的问题之一。

QGIS 使用自己的 Python 运行环境，并且需要正确加载：

- QGIS Python 包。
- QGIS Python 插件目录。
- QGIS 原生动态库。
- GDAL。
- PROJ。
- GEOS。
- Qt 和 PyQt。
- 其他 QGIS 运行时依赖。

在系统 Python 中直接执行：

```python
import qgis.core
```

可能会出现：

```text
ModuleNotFoundError: No module named 'qgis'
```

即使通过 `PYTHONPATH` 找到了 `qgis`，也不代表底层动态库已经正确配置。

因此，开发和调试时应优先使用 QGIS 自带的 Python 环境。

在 QGIS Python 控制台中，可以检查当前运行环境：

```python
import sys
from qgis.core import Qgis
from qgis.PyQt.QtCore import QT_VERSION_STR

print("QGIS version:", Qgis.QGIS_VERSION)
print("Python version:", sys.version)
print("Python executable:", sys.executable)
print("Qt version:", QT_VERSION_STR)
print("Python paths:", sys.path)
```

这些信息可以帮助定位解释器和依赖不一致的问题。

## 3.3 Qt 和 PyQt 的导入规范

统一使用：

```python
from qgis.PyQt.QtCore import QObject, QTimer, QUrl
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import (
    QAction,
    QDialog,
    QPushButton,
    QVBoxLayout,
)
```

不要在插件中随意使用：

```python
from PyQt5.QtWidgets import *
```

也不要把 PyQt5 和 PyQt6 的类混在同一个插件中。

QGIS 3.44 源码中的 `python/utils.py` 对 Qt 主版本进行了检查，会阻止不匹配的 PyQt 导入。

使用 `qgis.PyQt` 可以让插件根据当前 QGIS 构建环境选择匹配的绑定。

## 3.4 插件开发目录

建议把实际开发目录与 QGIS 安装目录分开。

例如：

```text
D:/dev/qgis_plugins/
    my_plugin/
        __init__.py
        metadata.txt
        main_plugin.py
```

然后将插件目录链接或复制到当前 QGIS 用户配置目录下的：

```text
python/plugins/
```

也可以在 QGIS 中通过用户配置文件目录定位当前插件安装位置。

这样做有几个好处：

- 不需要修改 QGIS 安装目录。
- 可以使用 Git 管理源代码。
- 可以使用 IDE 直接编辑项目。
- 可以方便地切换不同 QGIS 用户配置。
- 可以在不同版本的 QGIS 环境中测试。

## 3.5 IDE 配置

推荐使用：

- PyCharm。
- VS Code。
- Qt Creator。
- 其他支持 Python 的 IDE。

IDE 应使用与目标 QGIS 一致的 Python 解释器。

需要配置的内容通常包括：

1. Python 解释器。
2. QGIS Python 模块路径。
3. QGIS Python 插件路径。
4. 对应平台的动态库搜索路径。
5. 断点调试环境。

Linux 环境下，QGIS 文档给出的典型 Python 路径包括：

```bash
/usr/share/qgis/python
/usr/share/qgis/python/plugins
```

不同安装方式的路径可能不同，应当以实际安装目录为准。

Windows 环境建议使用 OSGeo4W Shell 启动开发工具，确保 Python、GDAL、Qt 和 QGIS 动态库处于正确的运行环境。

## 3.6 使用 Python 控制台进行快速验证

QGIS 自带 Python 控制台，是插件开发最方便的调试工具之一。

例如：

```python
from qgis.core import QgsProject

project = QgsProject.instance()

print(project.fileName())
print(project.count())
print(project.mapLayers().keys())
```

也可以检查当前活动图层：

```python
layer = iface.activeLayer()

if layer:
    print(layer.name())
    print(layer.isValid())
    print(layer.crs().authid())
```

建议先在 Python 控制台验证 API 和处理逻辑，再将稳定的代码整理到插件中。

---

# 4. 插件目录结构与组织方式

## 4.1 最小插件目录

一个最小可运行的 Python 插件至少需要：

```text
my_plugin/
    __init__.py
    metadata.txt
    main_plugin.py
```

其中：

| 文件 | 作用 |
|---|---|
| `metadata.txt` | 插件元数据 |
| `__init__.py` | Python 包入口和插件工厂 |
| `main_plugin.py` | 插件主类和主要功能 |

`metadata.txt` 和 `__init__.py` 是插件识别和加载的关键文件。

## 4.2 推荐目录结构

对于具备 GUI、数据处理和 Processing 算法的中小型插件，可以采用：

```text
my_plugin/
│
├── __init__.py
├── metadata.txt
├── main_plugin.py
│
├── core/
│   ├── __init__.py
│   ├── layer_manager.py
│   ├── geometry_utils.py
│   └── data_processor.py
│
├── dialogs/
│   ├── __init__.py
│   ├── main_dialog.py
│   ├── settings_dialog.py
│   └── main_dialog.ui
│
├── processing_provider/
│   ├── __init__.py
│   ├── provider.py
│   └── example_algorithm.py
│
├── resources/
│   ├── icon.png
│   └── logo.png
│
├── resources.qrc
├── resources.py
│
├── i18n/
│   ├── my_plugin_zh_CN.ts
│   └── my_plugin_zh_CN.qm
│
├── tests/
│   ├── test_geometry.py
│   └── test_processing.py
│
├── README.md
└── LICENSE
```

不需要所有插件都使用这套目录。

一个只有菜单按钮和简单功能的小插件，没有必要建立大量空目录。

建议根据实际功能逐步拆分。

## 4.3 主类只负责协调

`main_plugin.py` 不应该承载所有业务代码。

比较合理的职责分配是：

- `main_plugin.py`：插件初始化、菜单、工具栏、窗口、功能入口和卸载。
- `dialogs/`：GUI 界面和用户交互。
- `core/`：数据处理、几何计算、图层操作。
- `processing_provider/`：Processing Provider 和算法。
- `resources/`：图标和其他静态资源。
- `i18n/`：翻译文件。
- `tests/`：测试代码。

这样做不是为了建立复杂的工程架构，而是让代码能够被清楚地找到和修改。

## 4.4 模块导入

推荐使用相对导入：

```python
from .core.layer_manager import LayerManager
from .dialogs.main_dialog import MainDialog
```

子目录应包含 `__init__.py`，使其成为有效的 Python 包。

不要在插件中大量修改：

```python
sys.path
```

更不要通过修改全局 Python 路径解决本来可以通过正确包结构解决的问题。

---

# 5. metadata.txt 元数据规范

## 5.1 基本结构

`metadata.txt` 使用 INI 格式。

必须包含：

```ini
[general]
```

所有元数据采用 UTF-8 编码。

QGIS 插件管理器通过该文件获取插件信息。

## 5.2 元数据字段

QGIS 3.44 官方 Cookbook 给出的字段如下。

| 字段 | 是否必需 | 说明 |
|---|---|---|
| `name` | 是 | 插件名称 |
| `qgisMinimumVersion` | 是 | 最低 QGIS 版本 |
| `qgisMaximumVersion` | 否 | 最高支持 QGIS 版本 |
| `description` | 是 | 简短描述，不允许 HTML |
| `about` | 是 | 插件详细介绍，不允许 HTML |
| `version` | 是 | 插件版本 |
| `author` | 是 | 作者 |
| `email` | 是 | 作者邮箱 |
| `repository` | 是 | 源代码仓库地址 |
| `changelog` | 否 | 版本更新记录 |
| `experimental` | 否 | 当前版本是否为实验版本 |
| `deprecated` | 否 | 插件是否已废弃 |
| `tags` | 否 | 搜索标签 |
| `homepage` | 否 | 插件主页 |
| `tracker` | 否 | 问题跟踪地址 |
| `icon` | 否 | 插件图标 |
| `category` | 否 | 插件分类 |
| `plugin_dependencies` | 否 | 依赖的其他 QGIS 插件 |
| `server` | 否 | 是否提供 Server 插件接口 |
| `hasProcessingProvider` | 否 | 是否提供 Processing Provider |

## 5.3 QGIS 版本约束

如果插件只针对 QGIS 3.44 开发和验证，可以设置：

```ini
qgisMinimumVersion=3.44
qgisMaximumVersion=3.44
```

如果插件经过多个 3.x 版本的实际测试，可以根据测试范围扩大支持范围。

例如：

```ini
qgisMinimumVersion=3.44
qgisMaximumVersion=3.99
```

不要为了让插件在更多版本中显示，就随意扩大最高版本。

如果上传到官方插件仓库时没有填写 `qgisMaximumVersion`，仓库会根据最低版本的主版本号自动设置默认最高版本。因此，需要结合实际兼容性决定是否填写。

## 5.4 完整示例

```ini
[general]
name=My GIS Tools
qgisMinimumVersion=3.44
qgisMaximumVersion=3.44

description=GIS data management and analysis tools

about=Provides GIS data processing, layer management and spatial analysis functions.

version=1.0.0

author=Example Developer
email=developer@example.com

repository=https://github.com/example/my-gis-tools
homepage=https://github.com/example/my-gis-tools
tracker=https://github.com/example/my-gis-tools/issues

icon=resources/icon.png

category=Vector

tags=vector,analysis,geometry,data management

experimental=False
deprecated=False

hasProcessingProvider=True
server=False

changelog=1.0.0 - Initial release
```

这里的作者、邮箱和仓库地址需要替换为实际信息。

## 5.5 category 的使用

官方元数据分类包括：

- Raster
- Vector
- Database
- Mesh
- Web

它用于告诉用户插件主要属于哪一类功能。

插件默认显示在 Plugins 菜单下。

如果需要放入特定菜单，可以使用 `QgisInterface` 提供的菜单方法，例如：

```python
iface.addPluginToRasterMenu(...)
iface.addPluginToVectorMenu(...)
iface.addPluginToDatabaseMenu(...)
iface.addPluginToWebMenu(...)
```

菜单分类和插件实际功能应保持一致。

## 5.6 plugin_dependencies 的含义

例如：

```ini
plugin_dependencies=OtherPlugin==1.12,AnotherPlugin
```

这里声明的是对其他 QGIS 插件的依赖，而不是 Python pip 包依赖。

依赖插件通过其 `metadata.txt` 中的 `name` 字段识别。

不要把：

```ini
plugin_dependencies=geopandas,shapely
```

当成 Python 包依赖声明使用。

Python 第三方库的安装和打包需要单独处理。

---

# 6. 插件加载机制与生命周期

这是 QGIS 插件开发最重要的基础内容之一。

## 6.1 QGIS 如何发现插件

QGIS 3.44 源码中的：

`python/utils.py`

实现了 Python 插件发现和加载相关逻辑。

其中 `findPlugins()` 会检查插件目录：

1. 是否为目录。
2. 是否存在 `__init__.py`。
3. 是否存在 `metadata.txt`。
4. 是否能够读取 UTF-8 元数据。

因此，一个普通的 Python 文件并不能直接作为 QGIS 插件加载。

插件必须符合 QGIS 的包结构要求。

## 6.2 插件加载流程

整体流程可以理解为：

```text
QGIS 启动或用户启用插件
        |
        v
扫描插件目录
        |
        v
检查 __init__.py
        |
        v
检查 metadata.txt
        |
        v
导入插件 Python 包
        |
        v
调用 classFactory(iface)
        |
        v
创建插件对象
        |
        v
调用 initGui()
        |
        v
插件正常工作
```

卸载时：

```text
用户禁用插件
        |
        v
调用 unload()
        |
        v
移除菜单、工具栏和 DockWidget
        |
        v
断开信号
        |
        v
取消或清理任务
        |
        v
注销 Processing Provider
        |
        v
清理插件模块
        |
        v
清理 Qt 资源
```

需要注意，QGIS 的实际加载流程还包括专门的 Processing 插件初始化路径。

## 6.3 classFactory()

`classFactory(iface)` 是插件的标准入口。

它接收 QGIS 主程序传入的 `QgisInterface` 对象，并返回插件实例。

`__init__.py`：

```python
def classFactory(iface):
    from .main_plugin import MyPlugin
    return MyPlugin(iface)
```

建议采用延迟导入。

不要在 `__init__.py` 中执行复杂的数据处理、创建窗口或者连接大量信号。

这样可以减少插件导入阶段的副作用，也方便定位加载错误。

## 6.4 __init__()

插件类初始化时，主要保存运行环境和初始化内部状态。

```python
class MyPlugin:

    def __init__(self, iface):

        self.iface = iface

        self.action = None
        self.dialog = None

        self.provider = None

        self.tasks = []

        self.connections = []
```

建议：

- 保存 `iface`。
- 初始化插件自身的状态。
- 初始化需要长期保存的对象引用。
- 不要在这里创建大量 GUI。
- 不要在这里执行耗时的数据处理。
- 不要在这里直接修改当前项目。

初始化阶段尽量简单。

## 6.5 initGui()

QGIS 加载普通 Desktop 插件时，会调用：

```python
initGui()
```

这个方法负责：

- 创建 QAction。
- 添加菜单。
- 添加工具栏按钮。
- 创建 DockWidget。
- 注册界面控件。
- 连接需要的信号。
- 初始化必要的 GUI 功能。

示例：

```python
from qgis.PyQt.QtWidgets import QAction


def initGui(self):

    self.action = QAction(
        self.tr("Open My Plugin"),
        self.iface.mainWindow()
    )

    self.action.setObjectName(
        "my_plugin_open_action"
    )

    self.action.triggered.connect(self.run)

    self.iface.addPluginToMenu(
        self.tr("&My Plugin"),
        self.action
    )

    self.iface.addToolBarIcon(self.action)
```

注意，`initGui()` 应当能够清楚地完成界面注册。

不要把数据分析过程放在这个方法中。

## 6.6 unload()

`unload()` 是插件生命周期中非常重要的方法。

QGIS 在卸载插件时会调用它。

插件必须主动清理自己创建的资源。

例如：

```python
def unload(self):

    if self.action:

        self.iface.removePluginMenu(
            self.tr("&My Plugin"),
            self.action
        )

        self.iface.removeToolBarIcon(
            self.action
        )

        self.action.deleteLater()

        self.action = None
```

真实插件还需要清理：

- 信号连接。
- DockWidget。
- 地图工具。
- RubberBand。
- 定时器。
- Processing Provider。
- 后台任务。
- 网络请求。
- 自定义注册项。
- 临时文件。

**一个重要的实践：卸载逻辑应当能够重复执行，并且不依赖某个对象一定存在。**

例如：

```python
if self.dialog is not None:
    self.dialog.close()
    self.dialog.deleteLater()
    self.dialog = None
```

不要假设用户一定打开过插件窗口。

## 6.7 为什么 unload() 必须可靠

QGIS 3.44 的 `qgis.utils.unloadPlugin()` 会调用插件的 `unload()`，随后清理插件实例、活动插件记录和 Python 模块。

插件资源模块如果提供 `qCleanupResources()`，QGIS 的模块清理逻辑还会尝试调用它。

因此，`unload()` 中如果发生异常，可能导致后续模块清理无法正常完成。

建议：

- 逐项清理。
- 对可能已经被销毁的对象进行检查。
- 避免卸载阶段出现未处理异常。
- 不要把清理代码全部放在一个容易提前中断的长方法中。
- 对任务和信号的清理尤其要认真处理。

## 6.8 initProcessing()

如果插件提供 Processing Provider，应实现：

```python
def initProcessing(self):
    pass
```

并在 `metadata.txt` 中设置：

```ini
hasProcessingProvider=True
```

QGIS 的 Processing 启动机制可以通过 `initProcessing()` 单独初始化插件的算法组件。

这与普通 GUI 插件的 `initGui()` 不是同一件事情。

因此：

- `initGui()` 管理用户界面。
- `initProcessing()` 管理 Processing 算法注册。

如果两处都可能触发 Provider 初始化，应当让初始化方法具备重复调用保护。

## 6.9 canBeUninstalled()

QGIS 插件加载源码还提供了可选的：

```python
canBeUninstalled()
```

它可以用于判断插件是否适合在当前状态下被卸载。

例如，插件存在尚未完成的关键任务时，可以根据具体业务决定是否允许卸载。

不过，一般插件应优先做好任务取消和资源清理，而不是依赖这个方法解决所有卸载问题。

---

# 7. 插件主界面与 Qt GUI 开发

## 7.1 优先使用 QgisInterface

`iface` 是插件与 QGIS Desktop 主程序交互的主要入口。

其实际接口定义位于 QGIS 源码：

`src/gui/qgisinterface.h`

`QgisInterface` 是 QGIS 为插件暴露的公共接口。

常用方法：

| 方法 | 用途 |
|---|---|
| `iface.mainWindow()` | 获取 QGIS 主窗口 |
| `iface.mapCanvas()` | 获取主地图画布 |
| `iface.activeLayer()` | 获取当前活动图层 |
| `iface.addPluginToMenu()` | 添加插件菜单项 |
| `iface.removePluginMenu()` | 移除插件菜单项 |
| `iface.addToolBarIcon()` | 添加工具栏按钮 |
| `iface.removeToolBarIcon()` | 移除工具栏按钮 |
| `iface.messageBar()` | 获取消息栏 |
| `iface.layerTreeView()` | 获取图层树视图 |
| `iface.addDockWidget()` | 添加停靠窗口 |
| `iface.removeDockWidget()` | 移除停靠窗口 |
| `iface.currentLayerChanged` | 当前图层变化信号 |

推荐通过公开接口完成插件与主程序之间的交互。

不要依赖 QGIS 内部主窗口的私有实现。

## 7.2 QAction 开发

菜单和工具栏功能一般使用 `QAction`。

```python
from qgis.PyQt.QtWidgets import QAction

action = QAction(
    "Run Analysis",
    self.iface.mainWindow()
)

action.setObjectName(
    "my_plugin_run_analysis"
)

action.setToolTip(
    "Run the spatial analysis"
)

action.setStatusTip(
    "Execute spatial analysis"
)

action.triggered.connect(
    self.run_analysis
)
```

建议：

- 为 QAction 设置唯一的 `objectName`。
- 为操作设置明确的提示。
- 给 QAction 指定合适的父对象。
- 保存 QAction 引用。
- 卸载时移除并销毁 QAction。

`objectName` 不只是装饰性属性，它有助于 Qt 对象查找和界面定制。

## 7.3 插件菜单

添加菜单：

```python
self.iface.addPluginToMenu(
    self.tr("&My Plugin"),
    self.action
)
```

移除菜单：

```python
self.iface.removePluginMenu(
    self.tr("&My Plugin"),
    self.action
)
```

添加和移除时应使用相同的菜单名称和 QAction 对象。

## 7.4 工具栏

添加：

```python
self.iface.addToolBarIcon(
    self.action
)
```

移除：

```python
self.iface.removeToolBarIcon(
    self.action
)
```

如果一个插件有多个工具栏操作，建议统一管理它们的 QAction 引用。

不要在每次打开对话框时重复添加工具栏按钮。

## 7.5 DockWidget

如果插件需要长期显示状态、属性或数据列表，可以使用 DockWidget。

例如：

```python
from qgis.PyQt.QtWidgets import QDockWidget

dock = QDockWidget(
    "My Plugin",
    self.iface.mainWindow()
)

self.iface.addDockWidget(
    Qt.DockWidgetArea.RightDockWidgetArea,
    dock
)
```

实际使用时还应保存 DockWidget 引用，并在卸载时移除：

```python
self.iface.removeDockWidget(dock)

dock.close()
dock.deleteLater()
```

DockWidget 适合：

- 数据管理面板。
- 分析结果面板。
- 图层属性辅助面板。
- 长时间工作的工具面板。

对于一次性的简单操作，使用 QDialog 通常更合适。

## 7.6 Qt Designer

复杂界面建议使用 Qt Designer。

Qt Designer 可以创建：

- `.ui` 文件。
- 布局。
- 按钮。
- 输入框。
- 表格。
- 树形控件。
- 标签。
- 分组框。
- 选项卡。

常见开发方式有两种：

**方式一：动态加载 UI**

通过 QGIS 自带的 PyQt 接口加载 `.ui` 文件。

**方式二：编译 UI**

通过与当前 QGIS Qt 环境匹配的 UI 编译工具生成 Python 文件。

无论使用哪种方式，运行时都应当保证 Qt 绑定与当前 QGIS 一致。

QGIS Cookbook 给出的传统工具示例包括 `pyuic5`，但具体工具应使用目标 QGIS 安装环境所提供的版本，不要直接拿系统中其他 Qt 版本的生成工具使用。

## 7.7 GUI 设计建议

参考 QGIS Human Interface Guidelines：

1. 相关控件放在同一组。
2. 常用功能放在明显位置。
3. 高级选项与普通选项分开。
4. 默认值应当合理。
5. 删除等危险操作与普通操作保持明显距离。
6. 使用 `QDialogButtonBox` 管理确定和取消按钮。
7. 控件应当提供必要的 Tooltip。
8. 界面不要堆积过多选项。
9. 长列表使用滚动区域。
10. 界面文字尽量使用普通用户能够理解的表达。

对于常见操作，优先使用 QGIS 消息栏，而不是不断弹出模态消息框。

---

# 8. 资源文件与路径管理

## 8.1 资源文件

插件常见资源包括：

- PNG 图标。
- SVG 图标。
- 界面图片。
- 样式文件。
- 帮助文档。
- 翻译文件。

可以使用 Qt 资源系统。

例如：

```xml
<RCC>
    <qresource prefix="/plugins/my_plugin">
        <file>resources/icon.png</file>
        <file>resources/logo.png</file>
    </qresource>
</RCC>
```

资源前缀应当具有插件唯一性。

不要随意使用过于通用的资源前缀，以免和其他插件或 QGIS 自身资源冲突。

## 8.2 资源编译

QGIS Cookbook 提供了：

```bash
pyrcc5 -o resources.py resources.qrc
```

实际使用时应选择与当前 QGIS Qt 环境匹配的工具。

插件中引用：

```python
from . import resources
```

然后：

```python
icon = QIcon(":/plugins/my_plugin/resources/icon.png")
```

如果使用的是外部文件资源，则必须保证文件确实被打包并且能够通过正确路径访问。

## 8.3 不要依赖当前工作目录

错误示例：

```python
icon_path = "resources/icon.png"
```

因为 QGIS 的当前工作目录不一定是插件目录。

正确方式：

```python
import os

PLUGIN_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

icon_path = os.path.join(
    PLUGIN_DIR,
    "resources",
    "icon.png"
)
```

也可以使用 `pathlib`：

```python
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent

icon_path = PLUGIN_DIR / "resources" / "icon.png"
```

所有插件内部文件都应基于插件自身目录定位。

## 8.4 临时文件

临时文件不要随意写到插件源码目录。

建议使用：

```python
import tempfile

temp_dir = tempfile.gettempdir()
```

或者根据 QGIS 的项目和用户配置环境选择合适的目录。

临时文件应有明确的清理时机。

---

# 9. 信号与槽机制

Qt 的信号与槽机制是 QGIS 插件开发的核心机制之一。

## 9.1 基本连接方式

```python
button.clicked.connect(
    self.on_button_clicked
)
```

断开：

```python
button.clicked.disconnect(
    self.on_button_clicked
)
```

QGIS 的很多对象都提供信号。

例如：

```python
self.iface.currentLayerChanged.connect(
    self.on_current_layer_changed
)
```

```python
QgsProject.instance().layersAdded.connect(
    self.on_layers_added
)
```

```python
QgsProject.instance().layersRemoved.connect(
    self.on_layers_removed
)
```

## 9.2 使用命名槽

推荐：

```python
def on_current_layer_changed(self, layer):

    if layer is None:
        return

    print(layer.name())
```

不推荐在复杂业务逻辑中到处使用匿名 lambda。

因为匿名连接不方便追踪，也容易忘记断开。

如果确实需要 lambda，应当保存连接引用或可复用的回调对象。

## 9.3 统一管理信号连接

建议在插件中维护连接记录。

例如：

```python
self.connections = []
```

连接时：

```python
connection = (
    self.iface.currentLayerChanged.connect(
        self.on_current_layer_changed
    )
)

self.connections.append(connection)
```

卸载时统一断开：

```python
from qgis.PyQt.QtCore import QObject

for connection in self.connections:
    QObject.disconnect(connection)

self.connections.clear()
```

对于普通 Python 信号，也可以直接使用：

```python
signal.disconnect(slot)
```

QGIS 自身的 Processing 插件就使用连接句柄列表管理 GUI 信号，并在 `unload()` 中统一断开。

## 9.4 特别注意项目生命周期信号

建议关注：

```python
QgsProject.instance().aboutToBeCleared
QgsProject.instance().cleared
QgsProject.instance().layersAdded
QgsProject.instance().layersWillBeRemoved
QgsProject.instance().layersRemoved
QgsProject.instance().writeProject
```

它们分别适用于：

- 项目即将清空。
- 项目已经清空。
- 图层增加。
- 图层即将删除。
- 图层删除完成。
- 项目保存。

如果插件长期保存当前图层对象引用，应当考虑图层被删除和项目被替换的情况。

## 9.5 不要重复连接

如果 `initGui()` 被重复调用，可能出现：

- 同一个按钮触发两次。
- 同一个信号执行两次。
- 重复创建 DockWidget。
- 重复添加菜单项。

建议在初始化前检查对象状态，确保界面初始化具有幂等性。

---

# 10. QGIS 项目与图层管理

## 10.1 QgsProject.instance()

QGIS Desktop 当前项目使用单例：

```python
from qgis.core import QgsProject

project = QgsProject.instance()
```

这是插件操作当前项目的标准入口。

常见操作：

```python
project.fileName()

project.crs()

project.ellipsoid()

project.mapLayers()

project.layerTreeRoot()
```

不要在插件中随意创建另一个 `QgsProject()` 对象，然后把它当成当前 Desktop 项目使用。

## 10.2 加载矢量图层

```python
from qgis.core import QgsVectorLayer

layer = QgsVectorLayer(
    "/data/roads.gpkg",
    "Roads",
    "ogr"
)

if layer.isValid():

    QgsProject.instance().addMapLayer(layer)

else:

    print("Layer loading failed")
```

## 10.3 加载栅格图层

```python
from qgis.core import QgsRasterLayer

layer = QgsRasterLayer(
    "/data/dem.tif",
    "DEM",
    "gdal"
)

if layer.isValid():

    QgsProject.instance().addMapLayer(layer)
```

## 10.4 添加图层

```python
project = QgsProject.instance()

project.addMapLayer(layer)
```

添加多个图层：

```python
project.addMapLayers(layers)
```

如果需要先注册图层，但暂时不放入图层树，可以使用：

```python
project.addMapLayer(
    layer,
    False
)
```

图层注册和图层树显示是相关但不同的事情。

## 10.5 获取图层

获取所有图层：

```python
layers = QgsProject.instance().mapLayers()
```

根据 ID 获取：

```python
layer = QgsProject.instance().mapLayer(layer_id)
```

根据名称查找：

```python
layers = QgsProject.instance().mapLayersByName(
    "Roads"
)
```

需要注意：图层名称并不保证唯一。

如果业务需要准确识别图层，优先保存图层 ID，而不是只保存名称。

同时，不要长期依赖可能已经被删除的图层对象引用。

## 10.6 活动图层

```python
layer = iface.activeLayer()
```

适合用户当前正在操作的场景。

但不应该把 `activeLayer()` 当作所有业务操作的固定输入。

例如，一个后台分析任务如果需要使用指定图层，应明确保存其图层 ID，并在执行前确认该图层仍然存在。

## 10.7 删除图层

```python
project.removeMapLayer(layer.id())
```

删除多个图层：

```python
project.removeMapLayers(layer_ids)
```

删除前应当确认是否需要：

- 保存编辑内容。
- 取消相关任务。
- 断开图层信号。
- 清理与图层关联的临时对象。

## 10.8 图层树

QGIS 的图层注册表与图层树不是同一个概念。

图层注册表负责管理图层对象。

图层树负责显示图层、分组和顺序。

创建分组：

```python
root = project.layerTreeRoot()

group = root.addGroup("Analysis Results")
```

添加图层后，可以调整其图层树节点位置。

对于插件生成的结果图层，建议放入明确的业务分组，而不是无条件添加到图层树顶部。

## 10.9 项目清空和切换

插件不能假设当前项目始终不变。

例如用户可能：

- 关闭当前项目。
- 打开另一个项目。
- 清空当前项目。
- 删除插件正在使用的图层。

推荐监听项目清空和图层删除信号。

当项目发生变化时：

- 清理旧项目关联的临时状态。
- 重新获取需要的图层。
- 更新界面。
- 取消依赖旧图层的任务。
- 避免使用已经失效的 Python 包装对象。

---

# 11. 矢量图层开发

矢量图层开发主要围绕 `QgsVectorLayer`、`QgsFeature`、`QgsGeometry` 和 `QgsVectorDataProvider` 展开。

## 11.1 获取字段

```python
layer = iface.activeLayer()

if layer and layer.type() == QgsMapLayerType.VectorLayer:

    for field in layer.fields():

        print(
            field.name(),
            field.typeName()
        )
```

实际开发中还应当检查：

```python
layer.isValid()
```

并确认图层的几何类型、字段结构和数据提供者。

## 11.2 遍历地物

```python
for feature in layer.getFeatures():

    print(feature.id())

    print(feature.attributes())

    print(feature.geometry().asWkt())
```

对于大量地物，不要在循环中频繁访问与当前地物无关的 GUI 对象。

## 11.3 使用 QgsFeatureRequest

如果只需要部分数据，不要把所有地物和所有字段全部读取出来。

例如，只获取指定字段：

```python
from qgis.core import QgsFeatureRequest

request = QgsFeatureRequest()

request.setSubsetOfAttributes(
    ["name", "type"],
    layer.fields()
)

for feature in layer.getFeatures(request):

    print(feature["name"])
```

不需要几何时：

```python
request.setFlags(
    QgsFeatureRequest.NoGeometry
)
```

根据空间范围过滤：

```python
request.setFilterRect(
    rectangle
)
```

如果需要严格的空间相交判断，可以使用：

```python
request.setFlags(
    QgsFeatureRequest.ExactIntersect
)
```

还可以使用：

```python
request.setFilterFid(fid)
request.setLimit(100)
```

这些方法可以明显减少数据读取量。

## 11.4 查询属性

通过字段名称：

```python
name = feature["name"]
```

通过字段索引：

```python
value = feature.attribute(0)
```

推荐优先使用字段名称。

因为字段索引可能随着字段增删而改变。

## 11.5 修改属性

对于 QGIS Desktop 中的正常编辑操作，推荐使用图层编辑缓冲区。

```python
if not layer.isEditable():

    layer.startEditing()

layer.changeAttributeValue(
    feature.id(),
    field_index,
    "New Value"
)

if layer.commitChanges():

    print("Commit successful")

else:

    print(layer.commitErrors())
```

编辑缓冲区的优点是：

- 可以撤销。
- 可以重做。
- 可以由用户决定提交还是回滚。
- 修改不会立即写入底层数据源。

## 11.6 使用 edit() 上下文管理器

对于比较简单的编辑操作，可以使用：

```python
from qgis.core.additions.edit import edit

with edit(layer):

    layer.changeAttributeValue(
        feature.id(),
        field_index,
        "New Value"
    )
```

它能够帮助管理编辑开始、提交和异常回滚。

## 11.7 批量修改

不要为了修改大量地物而反复执行：

```python
layer.startEditing()
layer.commitChanges()
```

更合理的做法是：

1. 开始一次编辑。
2. 批量修改。
3. 必要时建立编辑命令。
4. 完成后统一提交。
5. 出错时回滚。

例如：

```python
layer.beginEditCommand(
    "Update attributes"
)

try:

    for feature in layer.getFeatures():

        layer.changeAttributeValue(
            feature.id(),
            field_index,
            "Updated"
        )

    layer.endEditCommand()

except Exception:

    layer.destroyEditCommand()

    raise
```

## 11.8 数据提供者直接修改

也可以直接调用：

```python
provider = layer.dataProvider()
```

例如：

```python
provider.changeAttributeValues({
    10: {
        0: "Updated"
    }
})
```

但直接修改数据提供者与通过图层编辑缓冲区修改数据不是一回事。

直接调用 Provider 的修改方法通常会直接写入底层数据源，不经过正常的图层编辑缓冲区和撤销机制。

因此：

- 需要与 QGIS 编辑界面配合时，使用 `QgsVectorLayer` 编辑接口。
- 需要明确直接写入数据源时，才考虑 `QgsVectorDataProvider`。
- 调用前检查数据提供者的能力。

## 11.9 检查 Provider 能力

不同数据源支持的操作不完全一样。

```python
caps = layer.dataProvider().capabilities()
```

例如：

```python
if caps & QgsVectorDataProvider.DeleteFeatures:

    print("Delete is supported")
```

不要假设所有图层都支持新增字段、修改几何、删除地物和事务操作。

## 11.10 创建内存图层

内存图层适合：

- 临时计算结果。
- 中间数据。
- 用户交互结果。
- 不需要立即保存的数据。

示例：

```python
from qgis.core import QgsVectorLayer

layer = QgsVectorLayer(
    "Point?crs=EPSG:4326",
    "Temporary Points",
    "memory"
)

if not layer.isValid():

    raise RuntimeError("Invalid memory layer")
```

创建字段：

```python
from qgis.core import QgsField
from qgis.PyQt.QtCore import QMetaType

provider = layer.dataProvider()

provider.addAttributes([
    QgsField(
        "name",
        QMetaType.Type.QString
    ),
    QgsField(
        "value",
        QMetaType.Type.Double
    )
])

layer.updateFields()
```

创建地物：

```python
from qgis.core import QgsFeature, QgsGeometry, QgsPointXY

feature = QgsFeature(layer.fields())

feature.setGeometry(
    QgsGeometry.fromPointXY(
        QgsPointXY(120, 30)
    )
)

feature.setAttributes([
    "Point A",
    100.0
])

provider.addFeature(feature)

layer.updateExtents()
```

## 11.11 保存矢量图层

推荐使用：

```python
QgsVectorFileWriter.writeAsVectorFormatV3()
```

示例：

```python
from qgis.core import (
    QgsVectorFileWriter,
    QgsProject,
)

options = QgsVectorFileWriter.SaveVectorOptions()

options.driverName = "GPKG"

options.fileEncoding = "UTF-8"

result = QgsVectorFileWriter.writeAsVectorFormatV3(
    layer,
    "/data/output.gpkg",
    QgsProject.instance().transformContext(),
    options
)
```

应当根据返回值判断写入是否成功。

不要在新开发代码中继续照搬已经过时的文件写入 API。

## 11.12 空间索引

对于大量地物的空间查询，可以使用：

```python
from qgis.core import QgsSpatialIndex

index = QgsSpatialIndex(
    layer.getFeatures()
)
```

查询：

```python
ids = index.nearestNeighbor(
    QgsPointXY(120, 30),
    5
)
```

或者：

```python
ids = index.intersects(
    rectangle
)
```

如果是单点数据，并且索引不需要动态更新，可以进一步考虑 `QgsSpatialIndexKDBush`。

---

# 12. 栅格图层开发

## 12.1 创建栅格图层

```python
from qgis.core import QgsRasterLayer

layer = QgsRasterLayer(
    "/data/dem.tif",
    "DEM",
    "gdal"
)

if not layer.isValid():

    raise RuntimeError("Raster layer is invalid")
```

## 12.2 基本属性

```python
print(layer.width())
print(layer.height())

print(layer.bandCount())

print(layer.extent())

print(layer.crs().authid())
```

## 12.3 波段处理

栅格数据通常由一个或多个波段组成。

常见场景：

- 单波段高程。
- 单波段分类。
- RGB 影像。
- 多光谱数据。
- 多波段指数计算。

开发时必须明确波段编号和波段含义。

## 12.4 栅格数据读取

QGIS 提供栅格数据提供者和栅格块等对象。

对于较大的栅格，不要随意把整个栅格一次性读入 Python 内存。

应当根据需求使用：

- 栅格块读取。
- 指定波段读取。
- 指定窗口读取。
- Processing 栅格算法。
- GDAL 提供的适当接口。

## 12.5 栅格渲染

常见渲染器包括：

- `QgsSingleBandGrayRenderer`
- `QgsSingleBandPseudoColorRenderer`
- `QgsMultiBandColorRenderer`

对于连续变量，常使用单波段伪彩色渲染。

对于 RGB 影像，使用多波段颜色渲染。

不要直接修改底层数据值来改变显示效果。显示效果应当通过 Renderer 管理。

---

# 13. 几何处理与坐标参考系

## 13.1 QgsGeometry

QGIS 使用：

```python
QgsGeometry
```

表示空间几何。

支持：

- 点。
- 线。
- 面。
- 多点。
- 多线。
- 多面。
- 集合几何。

几何可以包含 Z 和 M 维度。

`QgsPoint` 支持 Z 和 M，而 `QgsPointXY` 主要用于二维坐标。

## 13.2 创建几何

点：

```python
geom = QgsGeometry.fromPointXY(
    QgsPointXY(120, 30)
)
```

线：

```python
geom = QgsGeometry.fromPolylineXY([
    QgsPointXY(120, 30),
    QgsPointXY(121, 31)
])
```

面：

```python
geom = QgsGeometry.fromPolygonXY([
    [
        QgsPointXY(120, 30),
        QgsPointXY(121, 30),
        QgsPointXY(121, 31),
        QgsPointXY(120, 31)
    ]
])
```

还可以通过 WKT 创建：

```python
geom = QgsGeometry.fromWkt(
    "POINT(120 30)"
)
```

## 13.3 常见空间运算

```python
geom.buffer(100, 8)

geom.centroid()

geom.area()

geom.length()

geom.intersects(other_geom)

geom.contains(other_geom)

geom.within(other_geom)

geom.distance(other_geom)

geom.intersection(other_geom)

geom.union(other_geom)

geom.difference(other_geom)
```

具体使用前应确认几何类型和有效性。

## 13.4 注意几何坐标属于哪个 CRS

这是 GIS 插件中非常重要的问题。

当通过图层读取地物时，地物几何坐标属于该图层的 CRS。

例如：

- 图层 CRS 是 EPSG:4326，几何坐标通常是经纬度。
- 图层 CRS 是 EPSG:3857，几何坐标是投影平面坐标。
- 图层 CRS 是本地投影坐标系，坐标单位可能是米或其他单位。

不要把不同 CRS 的坐标直接拿来做空间运算。

## 13.5 坐标转换

```python
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
)

project = QgsProject.instance()

source_crs = QgsCoordinateReferenceSystem(
    "EPSG:4326"
)

target_crs = QgsCoordinateReferenceSystem(
    "EPSG:3857"
)

transform = QgsCoordinateTransform(
    source_crs,
    target_crs,
    project.transformContext()
)

point = transform.transform(
    QgsPointXY(120, 30)
)
```

使用项目的 `transformContext()`，不要随意忽略当前项目中的坐标转换配置。

## 13.6 几何转换

```python
geom = feature.geometry()

geom.transform(transform)
```

注意：`transform()` 会修改当前几何对象。

如果原始几何还需要继续使用，应先复制：

```python
new_geom = QgsGeometry(geom)

new_geom.transform(transform)
```

## 13.7 距离和面积计算

不要直接把：

```python
geom.area()
```

当作所有场景下的真实地表面积。

几何面积和长度运算通常与坐标单位有关。

如果需要考虑椭球体和地理测量，应使用：

```python
QgsDistanceArea
```

例如：

```python
from qgis.core import QgsDistanceArea, QgsProject

project = QgsProject.instance()

measure = QgsDistanceArea()

measure.setSourceCrs(
    layer.crs(),
    project.transformContext()
)

measure.setEllipsoid(
    project.ellipsoid()
)

area = measure.measureArea(
    feature.geometry()
)

length = measure.measureLength(
    feature.geometry()
)
```

## 13.8 几何有效性

对于用户数据，应当考虑：

- 空几何。
- 无效几何。
- 自相交面。
- 多部件几何。
- 不同几何类型。
- Z/M 维度。
- 坐标转换失败。

复杂空间分析中，几何有效性可能直接影响计算结果。

---

# 14. 地图画布与地图交互工具

## 14.1 获取地图画布

```python
canvas = iface.mapCanvas()
```

QGIS 主地图画布由：

```python
QgsMapCanvas
```

实现。

它负责：

- 显示地图。
- 控制当前显示范围。
- 渲染图层。
- 管理当前地图工具。
- 处理地图交互。

## 14.2 设置地图范围

```python
canvas.setExtent(
    layer.extent()
)

canvas.refresh()
```

如果需要在指定范围周围留出空间，可以使用 `QgsRectangle` 的扩展方法。

## 14.3 获取地图坐标

屏幕坐标和地图坐标不是同一个概念。

地图工具中应使用：

```python
map_point = self.toMapCoordinates(
    event.pos()
)
```

不要把鼠标屏幕坐标直接当成地理坐标。

## 14.4 RubberBand

`QgsRubberBand` 用于在地图画布上临时绘制几何。

适合：

- 框选范围。
- 交互式画线。
- 交互式画面。
- 空间分析预览。
- 鼠标操作反馈。

示例：

```python
from qgis.gui import QgsRubberBand
from qgis.core import QgsWkbTypes

rubber_band = QgsRubberBand(
    canvas,
    QgsWkbTypes.PolygonGeometry
)

rubber_band.setColor(
    QColor(255, 0, 0)
)

rubber_band.setWidth(2)
```

设置几何：

```python
rubber_band.setToGeometry(
    geometry,
    None
)
```

## 14.5 VertexMarker

用于显示点位置：

```python
from qgis.gui import QgsVertexMarker

marker = QgsVertexMarker(canvas)

marker.setCenter(
    QgsPointXY(120, 30)
)
```

适合显示控制点、识别点和临时位置。

## 14.6 清理画布对象

需要特别注意：

```python
del rubber_band
```

并不一定会从地图画布中删除对应的绘制对象。

因为地图画布的场景可能仍然持有该对象。

官方 Cookbook 明确说明，Python 中仅删除引用并不等于销毁画布对象。

需要从场景中移除：

```python
canvas.scene().removeItem(
    rubber_band
)
```

插件应保存这些对象的引用，并在卸载时清理。

## 14.7 QgsMapTool

自定义地图工具通常继承：

```python
QgsMapTool
```

或者：

```python
QgsMapToolEmitPoint
```

常用事件：

```python
canvasPressEvent()
canvasReleaseEvent()
canvasMoveEvent()
keyPressEvent()
keyReleaseEvent()
deactivate()
```

例如，地图上框选一个矩形范围时，可以：

1. 鼠标按下时记录起点。
2. 鼠标移动时更新终点。
3. RubberBand 显示矩形。
4. 鼠标释放时得到最终范围。

## 14.8 地图工具切换

通过：

```python
canvas.setMapTool(map_tool)
```

激活地图工具。

建议：

- 保存地图工具引用。
- 保存必要时需要恢复的原地图工具。
- 插件关闭时恢复合适的地图工具状态。
- 不要在插件中随意覆盖用户当前使用的地图工具。

---

# 15. 地图渲染、符号系统与布局

## 15.1 矢量渲染器

QGIS 常用矢量渲染器包括：

| 渲染器 | 用途 |
|---|---|
| `QgsSingleSymbolRenderer` | 所有地物使用相同符号 |
| `QgsCategorizedSymbolRenderer` | 按字段分类显示 |
| `QgsGraduatedSymbolRenderer` | 按数值区间分级显示 |

获取渲染器：

```python
renderer = layer.renderer()
```

设置单一符号渲染：

```python
from qgis.core import (
    QgsSingleSymbolRenderer,
    QgsMarkerSymbol,
)

symbol = QgsMarkerSymbol.createSimple({
    "name": "circle",
    "color": "255,0,0,255",
    "size": "3"
})

renderer = QgsSingleSymbolRenderer(
    symbol
)

layer.setRenderer(renderer)

layer.triggerRepaint()
```

## 15.2 修改符号

可以通过：

- `QgsMarkerSymbol`
- `QgsLineSymbol`
- `QgsFillSymbol`
- `QgsSymbolLayer`

构建符号。

对于复杂样式，建议使用符号层组合，而不是手动绘制所有视觉元素。

## 15.3 修改后刷新

修改渲染器、属性和几何后，通常需要刷新地图画布。

例如：

```python
layer.triggerRepaint()
```

必要时：

```python
iface.mapCanvas().refresh()
```

不要在每一个地物循环中频繁调用画布刷新。

应当批量修改后统一刷新。

## 15.4 自定义符号层与渲染器

如果现有符号系统不能满足需求，可以扩展：

- 自定义符号层。
- 自定义 Renderer。
- 自定义符号层属性控件。

但需要注意注册机制、克隆机制和项目保存恢复机制。

自定义渲染类型必须能够在项目重新打开时正确恢复，否则用户保存的工程可能无法正常显示。

## 15.5 地图渲染任务

如果需要将地图输出为图片，可以使用：

```python
QgsMapSettings
```

和：

```python
QgsMapRendererParallelJob
```

基本流程：

1. 创建地图渲染设置。
2. 设置图层。
3. 设置输出范围。
4. 设置输出尺寸。
5. 创建渲染任务。
6. 启动任务。
7. 在 finished 信号中取得渲染结果。

不要为了输出一张图片而自行重新实现 QGIS 的地图渲染机制。

## 15.6 Print Layout

QGIS 3.x 使用新的布局系统。

核心类：

- `QgsPrintLayout`
- `QgsLayoutItemMap`
- `QgsLayoutItemLabel`
- `QgsLayoutItemLegend`
- `QgsLayoutItemScaleBar`
- `QgsLayoutExporter`

创建布局：

```python
from qgis.core import (
    QgsPrintLayout,
    QgsProject,
)

project = QgsProject.instance()

layout = QgsPrintLayout(project)

layout.initializeDefaults()
```

添加到项目：

```python
project.layoutManager().addLayout(
    layout
)
```

导出 PDF：

```python
from qgis.core import QgsLayoutExporter

exporter = QgsLayoutExporter(layout)

settings = QgsLayoutExporter.PdfExportSettings()

result = exporter.exportToPdf(
    "/data/map.pdf",
    settings
)
```

对于地图报告和批量制图，优先使用 QGIS Layout 和 Atlas 功能。

---

# 16. QGIS 表达式系统

## 16.1 QgsExpression

QGIS 有自己的表达式系统。

它不是完整 SQL。

支持：

- 算术表达式。
- 字段引用。
- 字符串处理。
- 逻辑判断。
- 空值判断。
- 几何函数。
- 日期函数。
- 聚合函数。
- 空间关系函数。

示例：

```python
from qgis.core import QgsExpression

expression = QgsExpression(
    '"population" > 10000'
)
```

## 16.2 检查表达式语法

```python
if expression.hasParserError():

    print(
        expression.parserErrorString()
    )
```

不要直接假设用户输入的表达式一定合法。

## 16.3 表达式求值

```python
value = QgsExpression(
    "2 * 3"
).evaluate()
```

如果表达式需要访问地物属性，需要创建表达式上下文。

```python
from qgis.core import (
    QgsExpression,
    QgsExpressionContext,
)

expression = QgsExpression(
    '"population" * 2'
)

context = QgsExpressionContext()

context.setFeature(feature)

value = expression.evaluate(context)
```

实际开发中，应根据表达式用途添加全局、项目、图层等 Scope。

## 16.4 表达式上下文

常见上下文包括：

- 全局上下文。
- 项目上下文。
- 图层上下文。
- 地物上下文。
- Processing 算法上下文。

推荐使用：

```python
QgsExpressionContextUtils
```

构造合适的表达式上下文。

不要在需要项目变量或图层变量的表达式中只提供一个地物对象。

## 16.5 自定义表达式函数

当业务需要自定义计算函数时，可以使用 QGIS 表达式函数注册机制。

例如：

```python
from qgis.core import qgsfunction

@qgsfunction(
    args="auto",
    group="My Plugin",
    usesgeometry=False
)
def my_function(value, feature, parent):

    return value * 2
```

注册和卸载时需要管理函数生命周期。

如果插件关闭后函数仍然注册，可能影响用户后续的表达式计算。

---

# 17. Processing 算法与 Provider 开发

这是 QGIS 插件开发中非常值得掌握的部分。

## 17.1 为什么优先考虑 Processing

Processing 不只是一个算法列表。

它是 QGIS 的统一算法执行框架。

一个 Processing 算法可以被：

- Toolbox 调用。
- Model Designer 调用。
- Batch Processing 调用。
- Python 代码调用。
- 其他 Processing 算法组合调用。

这意味着算法不必绑定某个具体对话框。

对于具有明确输入、参数、处理和输出的功能，Processing 通常是更好的实现方式。

## 17.2 Processing 的核心结构

主要包含：

```text
QgsProcessingProvider
        |
        |-- QgsProcessingAlgorithm
                |
                |-- Parameters
                |
                |-- processAlgorithm()
                |
                |-- Outputs
```

其中：

- Provider 负责组织和提供算法。
- Algorithm 负责定义算法和执行过程。
- Parameter 负责输入参数定义。
- ProcessingContext 负责执行上下文。
- Feedback 负责反馈进度、信息和取消状态。

## 17.3 Provider 的基本实现

```python
from qgis.core import QgsProcessingProvider

from .buffer_algorithm import BufferAlgorithm


class MyProvider(QgsProcessingProvider):

    def loadAlgorithms(self):

        self.addAlgorithm(
            BufferAlgorithm()
        )

    def id(self):

        return "my_plugin"

    def name(self):

        return self.tr("My Plugin")
```

Provider 的 `id()` 应当：

- 唯一。
- 简短。
- 使用稳定的英文标识。
- 不随界面语言改变。

Provider 的 `name()` 是给用户看的名称，可以翻译。

算法 ID 的形式通常为：

```text
provider_id:algorithm_name
```

例如：

```text
my_plugin:buffer
```

不要随意改变已经发布的算法 ID，因为其他模型和脚本可能引用它。

## 17.4 注册 Provider

```python
from qgis.core import QgsApplication

self.provider = MyProvider()

QgsApplication.processingRegistry().addProvider(
    self.provider
)
```

卸载时：

```python
QgsApplication.processingRegistry().removeProvider(
    self.provider
)

self.provider = None
```

应当保存 Provider 对象引用。

## 17.5 Processing 插件的元数据

在：

```ini
metadata.txt
```

中声明：

```ini
hasProcessingProvider=True
```

同时实现：

```python
def initProcessing(self):
    pass
```

官方 Cookbook 提供的注册方式是：

```python
def initProcessing(self):

    self.provider = MyProvider()

    QgsApplication.processingRegistry().addProvider(
        self.provider
    )
```

卸载时移除 Provider。

建议增加状态检查，避免重复注册。

## 17.6 QgsProcessingAlgorithm

一个算法通常需要实现：

```python
initAlgorithm()
processAlgorithm()
name()
displayName()
group()
groupId()
createInstance()
```

其中：

### initAlgorithm()

定义算法参数和输出。

### processAlgorithm()

执行实际计算。

### name()

算法稳定标识。

### displayName()

用户看到的算法名称。

### group()

算法所属分组。

### groupId()

分组稳定标识。

### createInstance()

创建一个新的算法实例。

## 17.7 参数定义

常见参数类：

| 参数类 | 用途 |
|---|---|
| `QgsProcessingParameterFeatureSource` | 矢量输入 |
| `QgsProcessingParameterVectorLayer` | 矢量图层输入 |
| `QgsProcessingParameterRasterLayer` | 栅格输入 |
| `QgsProcessingParameterNumber` | 数值 |
| `QgsProcessingParameterString` | 字符串 |
| `QgsProcessingParameterBoolean` | 布尔值 |
| `QgsProcessingParameterEnum` | 枚举选项 |
| `QgsProcessingParameterField` | 字段 |
| `QgsProcessingParameterDistance` | 距离 |
| `QgsProcessingParameterExtent` | 范围 |
| `QgsProcessingParameterFile` | 文件 |
| `QgsProcessingParameterFeatureSink` | 矢量输出 |
| `QgsProcessingParameterRasterDestination` | 栅格输出 |
| `QgsProcessingParameterFolderDestination` | 文件夹输出 |

参数定义中应当明确：

- 参数名称。
- 用户显示名称。
- 参数类型。
- 默认值。
- 是否必需。
- 可接受的图层类型。
- 参数说明。

参数名称是程序使用的标识，建议使用大写英文。

## 17.8 参数读取

在 `processAlgorithm()` 中，不建议直接从参数字典中随意取值并自行转换所有类型。

应优先使用算法提供的辅助方法：

```python
source = self.parameterAsSource(
    parameters,
    self.INPUT,
    context
)
```

```python
distance = self.parameterAsDouble(
    parameters,
    self.DISTANCE,
    context
)
```

```python
text = self.parameterAsString(
    parameters,
    self.NAME,
    context
)
```

这样能够利用 Processing 对参数类型的处理机制。

## 17.9 输出数据

推荐使用：

```python
self.parameterAsSink()
```

例如：

```python
sink, destination_id = self.parameterAsSink(
    parameters,
    self.OUTPUT,
    context,
    source.fields(),
    source.wkbType(),
    source.sourceCrs()
)
```

然后写入：

```python
sink.addFeature(
    feature,
    QgsFeatureSink.FastInsert
)
```

最后返回：

```python
return {
    self.OUTPUT: destination_id
}
```

不要把临时输出图层、文件路径和实际图层对象混为一谈。

Processing 的输出管理需要通过 `context` 和 destination ID 配合完成。

## 17.10 Feedback

Processing 算法应当向用户提供运行反馈。

例如：

```python
feedback.pushInfo(
    "Start processing"
)
```

```python
feedback.pushWarning(
    "Some features were skipped"
)
```

```python
feedback.setProgress(
    50
)
```

检查取消状态：

```python
if feedback.isCanceled():
    break
```

耗时算法必须考虑用户取消操作。

## 17.11 算法开发注意事项

建议：

1. 算法不依赖 GUI。
2. 算法不要使用 `iface`。
3. 算法参数必须清晰。
4. 输出必须通过 Processing 输出机制管理。
5. 对输入图层和字段进行验证。
6. 支持合理的取消机制。
7. 使用 Feedback 报告进度。
8. `createInstance()` 返回全新实例。
9. 算法不要依赖上一次执行留下的实例状态。
10. 需要批处理时，避免设计只能处理单个固定图层的逻辑。

QGIS 3.44 源码中，`QgsProcessingProvider::addAlgorithm()` 会初始化算法、设置 Provider 关联，并管理算法实例。重复的算法名称会被拒绝。

因此，不要在多个 Provider 之间重复使用同一个算法对象。

---

# 18. 后台任务与耗时操作

## 18.1 为什么需要后台任务

QGIS Desktop 的 GUI 主线程负责：

- 窗口绘制。
- 鼠标事件。
- 键盘事件。
- 菜单交互。
- 界面刷新。
- 用户操作响应。

如果插件在主线程执行长时间计算，界面就可能卡住。

例如：

- 处理百万级地物。
- 大范围空间分析。
- 大文件读写。
- 网络数据下载。
- 批量坐标转换。
- 复杂几何运算。

这类任务应当考虑后台执行。

## 18.2 QgsTask

QGIS 提供：

```python
QgsTask
```

和：

```python
QgsTaskManager
```

统一管理后台任务。

通常使用全局任务管理器：

```python
QgsApplication.taskManager()
```

## 18.3 三种常见任务创建方式

### 方式一：继承 QgsTask

适合复杂任务。

```python
class MyTask(QgsTask):

    def run(self):

        # 后台计算

        return True

    def finished(self, result):

        # 主线程处理结果
        pass
```

### 方式二：QgsTask.fromFunction()

适合较简单的函数型任务。

```python
def heavy_function(task, data):

    result = []

    for item in data:

        if task.isCanceled():
            return None

        result.append(item * 2)

    return result
```

创建任务：

```python
task = QgsTask.fromFunction(
    "My Background Task",
    heavy_function,
    data=[1, 2, 3]
)
```

### 方式三：Processing 算法任务

对于 Processing 算法，可以使用：

```python
QgsProcessingAlgRunnerTask
```

## 18.4 最重要的线程规则

QGIS 官方 Cookbook 对后台任务有非常明确的要求：

后台任务不能直接访问属于主线程的 QObject。

尤其不要在后台线程中：

- 操作 `QgsProject`。
- 操作主线程中的 `QgsVectorLayer`。
- 创建或修改 Qt Widget。
- 操作地图画布。
- 更新 GUI 控件。
- 随意访问主线程拥有的 QObject。

这些操作可能导致崩溃。

正确的处理方式是：

```text
主线程
   |
   | 读取图层数据
   | 提取必要属性和几何
   | 准备纯数据对象
   |
   v
后台线程
   |
   | 执行计算
   | 生成结果
   |
   v
主线程
   |
   | 创建输出图层
   | 添加到项目
   | 更新界面
```

例如：

```python
data = []

for feature in layer.getFeatures():

    data.append({
        "id": feature.id(),
        "geometry": feature.geometry().asWkb()
    })
```

后台线程只处理已经准备好的数据。

如果需要处理复杂的 PyQGIS 对象，应当根据相关 API 的线程安全要求设计执行方式，不要简单地把图层对象传入线程就认为可以安全访问。

## 18.5 context 和 feedback 的生命周期

对于 `QgsProcessingContext` 和 `QgsProcessingFeedback`，必须保证它们在任务执行期间一直有效。

例如：

```python
self.context = QgsProcessingContext()

self.context.setProject(
    QgsProject.instance()
)

self.feedback = QgsProcessingFeedback()
```

启动任务后，不要提前销毁这两个对象。

官方文档特别指出，任务完成时，如果任务管理器无法访问对应的 context 和 feedback，可能造成 QGIS 崩溃。

## 18.6 任务进度

使用：

```python
self.setProgress(50)
```

或者 Processing Feedback：

```python
feedback.setProgress(50)
```

进度值一般为 0 到 100。

## 18.7 任务取消

后台循环应定期检查：

```python
if self.isCanceled():
    return False
```

不要只在任务开始时检查一次取消状态。

对于大循环，应该在适当的处理间隔检查。

## 18.8 任务依赖图层

如果任务依赖某些图层，可以使用：

```python
task.setDependentLayers(
    [layer]
)
```

当依赖图层被删除时，QGIS 可以自动取消任务。

## 18.9 任务生命周期管理

插件应当管理自己启动的任务。

建议：

- 保存任务引用。
- 任务完成后从任务列表移除。
- 插件卸载时取消仍在运行的任务。
- 避免任务回调引用已经销毁的 GUI 对象。
- 不要在任务回调中执行大量耗时工作。

通过：

```python
QgsApplication.taskManager().addTask(
    task
)
```

添加到任务管理器后，任务的所有权交由任务管理器管理。

不要把已经由任务管理器管理的任务当作普通局部变量随意删除。

---

# 19. 配置管理与数据持久化

QGIS 插件中的数据持久化可以分为三类。

## 19.1 全局用户设置

使用：

```python
QgsSettings
```

例如：

```python
from qgis.core import QgsSettings

settings = QgsSettings()

settings.setValue(
    "my_plugin/default_buffer",
    100
)
```

读取：

```python
value = settings.value(
    "my_plugin/default_buffer",
    100
)
```

适合保存：

- 用户默认参数。
- 上次使用的目录。
- 界面选项。
- 用户偏好。
- 最近使用的设置。

建议统一使用插件自己的键前缀。

例如：

```text
my_plugin/
```

不要使用过于宽泛的配置键。

## 19.2 项目级设置

如果某个设置属于特定 QGIS 项目，应当保存在项目中。

例如：

```python
project = QgsProject.instance()

project.writeEntry(
    "my_plugin",
    "analysis_mode",
    "advanced"
)
```

读取：

```python
value, ok = project.readEntry(
    "my_plugin",
    "analysis_mode",
    "normal"
)
```

项目级设置适合：

- 项目分析参数。
- 项目特定业务配置。
- 项目级显示状态。
- 项目专用计算规则。

## 19.3 图层级设置

如果某个属性属于特定图层，可以使用：

```python
layer.setCustomProperty(
    "my_plugin/style",
    "special"
)
```

读取：

```python
value = layer.customProperty(
    "my_plugin/style",
    "default"
)
```

图层级设置与数据源本身无关，而是与当前项目中的图层实例关联。

## 19.4 三种配置的选择

| 数据类型 | 推荐方式 |
|---|---|
| 用户长期偏好 | QgsSettings |
| 当前项目的业务参数 | QgsProject |
| 某个图层专属状态 | QgsMapLayer.customProperty |
| 用户认证信息 | QgsAuthManager |
| 大量业务数据 | 数据库或文件 |
| 临时计算结果 | 临时目录或内存 |

不要把大量业务数据全部塞进 QGIS 设置中。

配置存储应该保持简单清楚。

---

# 20. 网络访问与认证管理

## 20.1 网络访问

QGIS 提供网络访问相关能力。

建议优先考虑：

```python
QgsNetworkAccessManager
```

它能够与 QGIS 的网络访问和认证体系结合。

对于简单的 HTTP 请求，可以根据业务需要使用：

- GET。
- POST。
- 自定义请求头。
- 异步响应。
- 超时处理。
- 错误处理。

网络请求不要长时间阻塞 GUI。

## 20.2 不要自己保存密码

不要把用户名和密码直接保存在：

```python
QgsSettings
```

或者：

```python
QgsProject.writeEntry()
```

也不要把密码直接写进：

- 插件源码。
- 项目文件。
- 日志。
- 数据源 URI。
- 普通 JSON 配置文件。

## 20.3 使用 QgsAuthManager

QGIS 提供认证管理系统。

入口：

```python
auth_manager = QgsApplication.authManager()
```

认证信息保存在 QGIS 的认证数据库中，并由主密码保护。

可以通过认证配置 ID：

```text
authcfg
```

引用已有认证配置。

例如构造 WMS 数据源时：

```python
uri.setParam(
    "authcfg",
    auth_config_id
)
```

应该把认证信息的展开交给 QGIS 认证管理器，不要提前把密码展开到普通 URI 中。

## 20.4 认证控件

QGIS GUI 还提供：

- `QgsAuthConfigEditor`
- `QgsAuthConfigSelect`
- `QgsAuthSettingsWidget`

插件可以使用这些控件集成认证配置。

---

# 21. 日志、异常处理与调试

## 21.1 不要只使用 print()

在 Python 控制台中：

```python
print("Hello")
```

当然可以。

但正式插件不应该把 `print()` 当成主要日志系统。

尤其是多线程、表达式、Renderer、符号层和 Processing 算法中，频繁使用 `print()` 可能影响性能，也不利于日志管理。

## 21.2 QgsMessageLog

推荐使用：

```python
from qgis.core import (
    QgsMessageLog,
    Qgis,
)

QgsMessageLog.logMessage(
    "Plugin initialized",
    "MyPlugin",
    Qgis.MessageLevel.Info
)
```

常用级别：

- Info
- Warning
- Critical
- Success

插件可以使用独立日志分类，方便用户在 QGIS 日志面板中查找。

## 21.3 Python logging

对于插件内部调试，可以使用 Python 标准日志模块。

```python
import logging

logger = logging.getLogger(
    "my_plugin"
)

logger.setLevel(logging.DEBUG)

logger.debug("Plugin debug message")
```

异常处理：

```python
try:

    run_processing()

except Exception:

    logger.exception(
        "Processing failed"
    )

    raise
```

不要无条件吞掉异常。

例如：

```python
try:
    do_something()

except Exception:
    pass
```

这种写法会让问题很难定位。

## 21.4 QgsMessageBar

面向普通用户的操作反馈，优先使用消息栏。

```python
iface.messageBar().pushMessage(
    "My Plugin",
    "Processing completed",
    level=Qgis.MessageLevel.Success,
    duration=3
)
```

错误提示：

```python
iface.messageBar().pushMessage(
    "My Plugin",
    "Input layer is invalid",
    level=Qgis.MessageLevel.Critical
)
```

一般来说：

- 成功提示：消息栏。
- 普通提示：消息栏。
- 警告：消息栏。
- 严重错误：消息栏加详细日志。
- 需要用户填写或确认的信息：对话框。

## 21.5 异常处理原则

建议：

1. 对输入数据进行检查。
2. 对可能失败的 API 调用处理返回值。
3. 对外部文件和网络操作进行异常处理。
4. 记录完整异常信息。
5. 向用户展示清楚的错误原因。
6. 不要用模糊的“执行失败”代替具体原因。
7. 不要为了让插件看起来正常而吞掉所有异常。

## 21.6 常用调试方式

可以使用：

- QGIS Python Console。
- Python Console Editor。
- Plugin Reloader。
- First Aid。
- IDE 断点。
- `pdb`。
- `qgis.utils.plugins`。

例如：

```python
import qgis.utils

plugin = qgis.utils.plugins.get(
    "my_plugin"
)

print(plugin)
```

注意这里使用的是插件包目录对应的模块名称。

---

# 22. 国际化与多语言支持

## 22.1 使用翻译接口

插件界面文字不要全部写死。

可以使用：

```python
from qgis.PyQt.QtCore import QCoreApplication

text = QCoreApplication.translate(
    "MyPlugin",
    "Open Plugin"
)
```

也可以在插件类中封装：

```python
def tr(self, message):

    return QCoreApplication.translate(
        "MyPlugin",
        message
    )
```

之后：

```python
self.action = QAction(
    self.tr("Open Plugin"),
    self.iface.mainWindow()
)
```

## 22.2 翻译文件

通常包括：

```text
i18n/
    my_plugin.pro
    my_plugin_zh_CN.ts
    my_plugin_en.ts
    my_plugin_zh_CN.qm
    my_plugin_en.qm
```

其中：

- `.pro`：翻译项目文件。
- `.ts`：翻译源文件。
- `.qm`：编译后的翻译文件。

## 22.3 翻译提取

QGIS Cookbook 介绍的传统工具包括：

```bash
pylupdate5 my_plugin.pro
```

编译：

```bash
lrelease my_plugin_zh_CN.ts
```

实际使用时应选择与当前 QGIS Qt 环境匹配的工具。

## 22.4 翻译注意事项

需要翻译的内容包括：

- 菜单名称。
- 工具栏文字。
- 对话框标题。
- 按钮文字。
- 提示信息。
- 错误信息。
- Processing 算法显示名称。
- 参数说明。
- 帮助文档。

不应翻译：

- Provider ID。
- 算法 ID。
- Python 模块名。
- 配置键。
- 数据库字段标识。
- 需要保持稳定的程序标识。

## 22.5 翻译文件更新

每次新增界面文字后，应重新提取翻译字符串。

不能只修改 Python 代码而忘记更新 `.ts` 和 `.qm` 文件。

---

# 23. Python 依赖与第三方库管理

## 23.1 首选 QGIS 已提供的功能

QGIS 已经集成大量 GIS 能力。

在引入第三方 Python 包之前，应先确认是否已经有对应 API。

例如：

| 功能 | 优先考虑 |
|---|---|
| 矢量数据读取 | QgsVectorLayer |
| 栅格数据读取 | QgsRasterLayer |
| 几何运算 | QgsGeometry |
| 坐标转换 | QgsCoordinateTransform |
| 空间索引 | QgsSpatialIndex |
| 矢量文件写入 | QgsVectorFileWriter |
| GIS 算法 | Processing |
| 地图绘制 | QgsMapCanvas |
| 网络访问 | QgsNetworkAccessManager |

避免为了一个简单操作引入一整套第三方 GIS 依赖。

## 23.2 Python 包依赖

如果确实需要第三方库，例如：

- requests。
- pandas。
- numpy。
- scipy。
- shapely。
- geopandas。

应考虑目标 QGIS 环境是否已经具备该依赖。

尤其要注意：

- Python 主版本。
- Python ABI。
- 操作系统。
- CPU 架构。
- Qt 版本。
- 原生动态库依赖。

带有 C/C++ 扩展的 Python 包不能像纯 Python 文件一样随意复制到插件目录。

## 23.3 不要随意覆盖 QGIS 依赖

不要为了让某个插件正常运行，随意覆盖 QGIS 自己使用的：

- GDAL。
- PROJ。
- GEOS。
- PyQt。
- Qt。
- SIP。

错误的版本组合可能影响 QGIS 本身和其他插件。

## 23.4 依赖管理建议

优先级建议：

1. 使用 QGIS 内置 API。
2. 使用 QGIS 已有的 Python 包。
3. 对纯 Python 小型依赖进行必要的独立打包。
4. 对大型或原生依赖明确说明安装要求。
5. 在干净的目标 QGIS 环境中验证安装流程。

---

# 24. 测试与质量控制

## 24.1 不要只测试插件能否打开

插件开发至少应当覆盖：

- 是否能够加载。
- 是否能够正常显示界面。
- 功能是否正确。
- 是否能处理无效数据。
- 是否能处理空项目。
- 是否能处理没有活动图层的情况。
- 是否能切换项目。
- 是否能删除正在使用的图层。
- 是否能正常卸载。
- 是否能重复加载。
- 后台任务是否能够取消。
- 是否能在目标系统运行。

## 24.2 业务逻辑测试

对于不依赖 QGIS GUI 的纯 Python 代码，可以使用：

- unittest。
- pytest。

例如：

```python
def calculate_area(value):

    return value * 2
```

这类逻辑可以直接使用普通 Python 测试。

不要把所有业务逻辑都写在 QGIS 窗口事件处理函数里。

## 24.3 PyQGIS 集成测试

涉及：

- QgsProject。
- QgsVectorLayer。
- QgsGeometry。
- QgsProcessingAlgorithm。

的测试需要在正确初始化的 QGIS Python 环境中运行。

可以使用 QGIS 提供的 `qgis.testing` 测试工具。

## 24.4 Processing 算法测试

QGIS 自己的 Processing 测试体系使用 YAML 描述算法输入和预期输出。

一个典型测试：

```yaml
- name: buffer_test
  algorithm: my_plugin:buffer
  params:
    DISTANCE: 100
  results:
    OUTPUT:
      type: vector
      name: expected/buffer.gml
```

具体测试还需要定义相应输入图层和参数。

可以使用：

- 固定测试数据。
- 预期输出数据。
- 属性精度比较。
- 几何精度比较。
- 栅格结果校验。

插件开发者可以借鉴这种数据驱动的测试方式，为自己的算法建立独立测试。

## 24.5 Plugin Reloader

开发过程中可以安装：

Plugin Reloader。

修改代码后，使用它重新加载插件，不必每次都关闭整个 QGIS。

但需要注意：

如果插件存在没有正确清理的信号、任务或资源，重复重载可能暴露生命周期问题。

## 24.6 测试环境

建议至少测试：

- QGIS 3.44 的主要目标操作系统。
- 不同用户配置文件。
- 不同语言环境。
- 空白 QGIS 配置。
- 不同类型数据源。
- 网络不可用时的行为。
- 大数据量场景。

如果插件声明支持其他 QGIS 版本，也应在对应版本中进行实际测试。

## 24.7 测试典型场景

| 测试场景 | 需要验证的内容 |
|---|---|
| 插件首次加载 | 是否成功创建界面 |
| 重复加载 | 是否出现重复按钮和信号 |
| 正常卸载 | 是否清理所有资源 |
| 空项目 | 是否能正常提示 |
| 无活动图层 | 是否有清晰提示 |
| 无效图层 | 是否能够识别 |
| 删除目标图层 | 是否处理失效引用 |
| 切换项目 | 是否更新状态 |
| 关闭项目 | 是否清理旧状态 |
| 取消任务 | 是否停止处理 |
| 网络断开 | 是否有错误反馈 |
| 文件写入失败 | 是否保留错误信息 |
| 编辑提交失败 | 是否正确处理提交状态 |

---

# 25. 性能优化

## 25.1 不要重复读取数据

如果只需要部分字段，使用 `QgsFeatureRequest`。

如果只需要某个范围，使用空间过滤。

如果不需要几何，使用 `NoGeometry`。

这些优化通常比单纯优化 Python 循环更有效。

## 25.2 使用空间索引

大量空间查询时，应当根据场景建立空间索引。

不要对大量地物反复执行全量两两空间关系判断。

## 25.3 减少 GUI 刷新

错误做法：

```python
for feature in layer.getFeatures():

    # 修改

    iface.mapCanvas().refresh()
```

更好的做法：

```python
for feature in layer.getFeatures():

    # 批量修改

layer.triggerRepaint()
```

## 25.4 减少不必要的对象创建

不要在每个地物循环中反复创建：

- CRS 对象。
- 坐标转换对象。
- 表达式对象。
- 空间索引。
- 文件写入对象。

可以在合理的范围内复用这些对象。

## 25.5 合理使用 Processing

对于大量空间数据处理，优先考虑 Processing 的数据源、Sink 和上下文机制。

不要为了一个批量算法，在 GUI 主线程中自行构建复杂的同步执行流程。

## 25.6 大量数据使用合适的数据结构

避免：

- 不必要地复制所有属性。
- 把所有几何转换为大型字符串。
- 对大栅格进行不必要的完整内存加载。
- 在每次循环中执行重复的文件系统查询。
- 在每个地物上执行大量无关数据库请求。

---

# 26. 安全性与稳定性

## 26.1 用户输入必须检查

对用户输入的：

- 文件路径。
- 数据库连接。
- 网络地址。
- SQL 条件。
- 表达式。
- 文件名。
- 输出目录。

进行必要检查。

不能直接假设用户输入正确。

## 26.2 不要执行不可信代码

不要把用户输入直接交给：

```python
eval()
```

或：

```python
exec()
```

如果用户需要表达式计算，优先使用 QGIS 表达式系统。

如果需要用户定义 Processing 参数，应使用正式参数类型和明确的执行逻辑。

## 26.3 文件路径安全

对于用户提供的文件路径，应当检查：

- 文件是否存在。
- 是否有读取权限。
- 是否有写入权限。
- 文件类型是否符合预期。
- 输出路径是否会覆盖重要文件。
- 临时文件是否需要清理。

## 26.4 数据库安全

数据库访问应当使用参数化查询。

不要通过字符串拼接构造不可信的 SQL。

不要在日志中打印完整数据库密码。

## 26.5 网络安全

网络访问应当：

- 使用 HTTPS。
- 正确处理证书验证。
- 设置合理的超时。
- 检查响应状态。
- 处理网络中断。
- 避免把认证信息写入日志。

## 26.6 数据安全

修改原始数据前，应当明确：

- 是否直接修改数据源。
- 是否使用编辑缓冲区。
- 是否需要备份。
- 是否需要用户确认。
- 是否能够回滚。

对重要数据的批量修改，应当提供清晰的执行结果和错误信息。

---

# 27. 插件打包、发布与版本管理

## 27.1 插件 ZIP 结构

官方 QGIS Python 插件仓库要求压缩包具有正确的目录结构。

正确结构：

```text
my_plugin.zip
    my_plugin/
        __init__.py
        metadata.txt
        LICENSE
        README.md
        main_plugin.py
        resources/
        i18n/
        processing_provider/
```

错误结构：

```text
my_plugin.zip
    release/
        my_plugin/
            __init__.py
            metadata.txt
```

因为插件安装器会将 ZIP 内容解压到用户插件目录。

不能让插件根目录多套一层无意义目录。

## 27.2 插件目录名称

官方仓库要求主目录名称：

- 只使用 ASCII 字符。
- 允许英文字母。
- 允许数字。
- 允许下划线。
- 允许连字符。
- 不能以数字开头。

实际开发中，推荐使用简单的英文小写名称和下划线。

例如：

```text
my_plugin
```

不要使用中文目录名。

## 27.3 LICENSE

官方仓库要求包含：

```text
LICENSE
```

注意文件名没有扩展名。

例如：

```text
LICENSE.txt
```

不符合官方文档给出的文件命名要求。

应当根据插件实际使用的代码、资源和第三方依赖选择合适的许可证。

## 27.4 不要打包无关文件

通常不需要把以下内容放进发布包：

- `.git/`
- `.idea/`
- `.vscode/`
- `__pycache__/`
- 临时日志。
- 测试缓存。
- 本地开发配置。
- 无关数据。
- 临时输出。

对于自动生成的 UI、资源和帮助文件，应当区分源码仓库和最终运行包。

需要在运行时导入的生成文件必须包含在最终安装包中，或者由明确的构建步骤生成。

## 27.5 版本号

建议采用：

```text
主版本.次版本.修订版本
```

例如：

```text
1.0.0
1.0.1
1.1.0
2.0.0
```

每次发布都应更新：

```ini
version=1.0.1
```

版本号不能重复。

## 27.6 发布前验证

建议在干净的 QGIS 用户配置文件中安装插件。

检查：

- 插件管理器能否识别。
- 是否能正确安装。
- 是否能正常启用。
- 是否能正常运行。
- 是否能正常禁用。
- 是否能再次启用。
- 是否存在依赖问题。
- 图标是否正常显示。
- 元数据是否正确。

## 27.7 官方插件仓库

官方仓库：

https://plugins.qgis.org/

上传插件通常需要 OSGeo ID。

插件上传后会经过仓库的审核或版本批准流程。

## 27.8 qgis-plugin-ci

可以使用：

`qgis-plugin-ci`

自动化：

- 插件打包。
- 版本发布。
- 翻译。
- 插件仓库 XML 生成。
- CI 发布流程。

对于需要持续维护的插件，推荐使用自动化发布。

---

# 28. 完整插件开发示例

下面通过一个实际可运行的示例，说明普通插件和 Processing Provider 的组织方式。

示例插件实现：

1. 在 QGIS 中添加插件菜单。
2. 添加工具栏按钮。
3. 点击按钮创建一个内存点图层。
4. 注册一个 Processing Provider。
5. 提供一个缓冲区算法。
6. 插件卸载时清理界面和 Provider。

## 28.1 项目结构

```text
my_plugin/
    __init__.py
    metadata.txt
    main_plugin.py

    processing_provider/
        __init__.py
        provider.py
        buffer_algorithm.py

    LICENSE
```

## 28.2 metadata.txt

```ini
[general]
name=My Plugin
qgisMinimumVersion=3.44
qgisMaximumVersion=3.44

description=Example QGIS 3.44 plugin

about=An example plugin with GUI and Processing provider.

version=1.0.0

author=Example Developer
email=developer@example.com

repository=https://github.com/example/my_plugin

category=Vector

tags=example,vector,processing

experimental=False
deprecated=False

hasProcessingProvider=True
```

## 28.3 __init__.py

```python
def classFactory(iface):

    from .main_plugin import MyPlugin

    return MyPlugin(iface)
```

## 28.4 processing_provider/__init__.py

```python
# Processing provider package
```

## 28.5 processing_provider/buffer_algorithm.py

```python
from qgis.core import (
    Qgis,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFeatureSink,
    QgsFeatureSink,
    QgsApplication,
)


class BufferAlgorithm(QgsProcessingAlgorithm):

    INPUT = "INPUT"

    DISTANCE = "DISTANCE"

    OUTPUT = "OUTPUT"

    def name(self):

        return "buffer"

    def displayName(self):

        return self.tr("Buffer")

    def group(self):

        return self.tr("Vector Analysis")

    def groupId(self):

        return "vector_analysis"

    def shortHelpString(self):

        return self.tr(
            "Create a buffer around input features."
        )

    def tr(self, message):

        from qgis.PyQt.QtCore import QCoreApplication

        return QCoreApplication.translate(
            "BufferAlgorithm",
            message
        )

    def createInstance(self):

        return BufferAlgorithm()

    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT,
                self.tr("Input layer")
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.DISTANCE,
                self.tr("Buffer distance"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=100.0,
                minValue=0.0
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr("Output layer")
            )
        )

    def processAlgorithm(
        self,
        parameters,
        context,
        feedback
    ):

        source = self.parameterAsSource(
            parameters,
            self.INPUT,
            context
        )

        if source is None:

            raise QgsProcessingException(
                self.tr("Invalid input layer")
            )

        distance = self.parameterAsDouble(
            parameters,
            self.DISTANCE,
            context
        )

        sink, destination_id = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            source.fields(),
            source.wkbType(),
            source.sourceCrs()
        )

        if sink is None:

            raise QgsProcessingException(
                self.tr("Could not create output layer")
            )

        total = source.featureCount()

        for current, feature in enumerate(
            source.getFeatures()
        ):

            if feedback.isCanceled():

                raise QgsProcessingException(
                    self.tr("Operation canceled")
                )

            geometry = feature.geometry()

            if not geometry.isNull():

                feature.setGeometry(
                    geometry.buffer(
                        distance,
                        8
                    )
                )

            sink.addFeature(
                feature,
                QgsFeatureSink.FastInsert
            )

            if total > 0:

                feedback.setProgress(
                    (current + 1) * 100.0 / total
                )

        feedback.pushInfo(
            self.tr("Buffer processing completed")
        )

        return {
            self.OUTPUT: destination_id
        }
```

注意：此示例中的 `buffer()` 使用输入图层 CRS 的坐标单位。如果输入 CRS 是经纬度坐标系，距离参数不是米。需要米制缓冲区时，应先设计合适的投影转换或采用适合的测地处理方式。

## 28.6 processing_provider/provider.py

```python
from qgis.core import QgsProcessingProvider

from .buffer_algorithm import BufferAlgorithm


class MyProvider(QgsProcessingProvider):

    def id(self):

        return "my_plugin"

    def name(self):

        return self.tr("My Plugin")

    def loadAlgorithms(self):

        self.addAlgorithm(
            BufferAlgorithm()
        )
```

## 28.7 main_plugin.py

```python
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtCore import QCoreApplication

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsProject,
    QgsVectorLayer,
)

from .processing_provider.provider import MyProvider


class MyPlugin:

    def __init__(self, iface):

        self.iface = iface

        self.action = None

        self.provider = None

    def tr(self, message):

        return QCoreApplication.translate(
            "MyPlugin",
            message
        )

    def initProcessing(self):

        if self.provider is not None:

            return

        provider = MyProvider()

        success = (
            QgsApplication
            .processingRegistry()
            .addProvider(provider)
        )

        if success:

            self.provider = provider

    def initGui(self):

        self.initProcessing()

        if self.action is not None:

            return

        self.action = QAction(
            self.tr("Create Sample Layer"),
            self.iface.mainWindow()
        )

        self.action.setObjectName(
            "my_plugin_create_sample_layer"
        )

        self.action.triggered.connect(
            self.run
        )

        self.iface.addPluginToMenu(
            self.tr("&My Plugin"),
            self.action
        )

        self.iface.addToolBarIcon(
            self.action
        )

    def run(self):

        layer = QgsVectorLayer(
            "Point?crs=EPSG:4326",
            "My Plugin Sample",
            "memory"
        )

        if not layer.isValid():

            self.iface.messageBar().pushMessage(
                self.tr("My Plugin"),
                self.tr("Failed to create layer"),
                level=Qgis.MessageLevel.Critical
            )

            return

        QgsProject.instance().addMapLayer(
            layer
        )

        self.iface.messageBar().pushMessage(
            self.tr("My Plugin"),
            self.tr("Sample layer created"),
            level=Qgis.MessageLevel.Success,
            duration=3
        )

    def unload(self):

        if self.action is not None:

            self.iface.removePluginMenu(
                self.tr("&My Plugin"),
                self.action
            )

            self.iface.removeToolBarIcon(
                self.action
            )

            self.action.deleteLater()

            self.action = None

        if self.provider is not None:

            QgsApplication.processingRegistry().removeProvider(
                self.provider
            )

            self.provider = None
```

## 28.8 运行效果

安装插件后：

1. QGIS Plugins 菜单出现 My Plugin。
2. 工具栏出现插件按钮。
3. 点击按钮创建一个 EPSG:4326 内存点图层。
4. Processing Toolbox 中出现 My Plugin Provider。
5. Provider 中提供 Buffer 算法。
6. 插件禁用后，菜单、工具栏和 Provider 被清理。

## 28.9 示例还需要补充的生产功能

真实业务插件还应当考虑：

- 输入图层类型检查。
- 字段检查。
- 参数边界检查。
- 结果命名。
- 输出保存路径。
- 异常日志。
- 国际化。
- 测试。
- 图层和任务生命周期。
- Processing 算法单元测试。

这个示例的主要目的是展示插件入口、GUI 生命周期和 Processing Provider 的正确组织方式，而不是把所有业务需求都塞进一个类中。

---

# 29. 常见问题与错误处理

## 29.1 插件没有出现在插件管理器中

检查：

- 是否放在当前用户配置的 `python/plugins` 目录。
- 是否存在 `__init__.py`。
- 是否存在 `metadata.txt`。
- 元数据是否符合 INI 格式。
- 文件编码是否为 UTF-8。
- 插件目录是否存在非法名称。

## 29.2 Couldn't load plugin

常见原因：

- `classFactory()` 不存在。
- `classFactory()` 没有返回插件对象。
- Python 导入失败。
- 第三方依赖不存在。
- 导入了不兼容的 PyQt。
- 模块级代码执行时报错。

优先查看 Python 错误日志和完整 Traceback。

## 29.3 initGui() 报错

常见原因：

- QAction 创建失败。
- UI 文件不存在。
- 资源文件没有正确加载。
- 界面控件初始化失败。
- 重复连接或重复创建对象。
- 调用了不存在的 API。

应当把 GUI 创建步骤逐项拆开定位。

## 29.4 插件卸载后仍有按钮

说明卸载时没有清理完整。

检查：

- `removePluginMenu()`。
- `removeToolBarIcon()`。
- DockWidget 移除。
- QAction 销毁。
- 信号断开。

## 29.5 重载插件后信号触发多次

通常是因为：

- 每次 `initGui()` 都重新连接。
- `unload()` 没有断开信号。
- 使用匿名 lambda，无法有效断开。
- 插件对象没有正确释放。

## 29.6 Layer is invalid

检查：

- 数据源路径。
- Provider 类型。
- 数据文件是否存在。
- 数据源是否可访问。
- CRS 是否有效。
- 数据格式是否受支持。
- 数据库连接是否成功。

## 29.7 修改数据后地图没有刷新

尝试：

```python
layer.triggerRepaint()
```

必要时：

```python
iface.mapCanvas().refresh()
```

还应检查当前地图是否启用了缓存。

## 29.8 Processing 算法找不到

检查：

- `hasProcessingProvider=True`。
- Provider 是否成功注册。
- `loadAlgorithms()` 是否被执行。
- `id()` 是否唯一。
- 算法是否被成功添加。
- 算法是否具有正确的 `createInstance()`。
- Provider 是否处于有效状态。

## 29.9 后台任务导致崩溃

重点检查：

- 是否在后台线程操作 QgsProject。
- 是否访问 GUI 控件。
- 是否使用了已经删除的图层。
- context 是否提前释放。
- feedback 是否提前释放。
- 插件卸载后回调是否仍访问插件对象。

## 29.10 找不到 Python 包

检查：

- 当前 QGIS Python 解释器。
- 包是否安装到对应环境。
- 包是否依赖原生动态库。
- Python 版本是否一致。
- Qt/PyQt 版本是否匹配。

不要通过不断修改 `sys.path` 掩盖实际依赖问题。

---

# 30. 插件发布检查清单

## 30.1 基本结构

- [ ] 插件目录名称符合要求。
- [ ] 存在 `__init__.py`。
- [ ] 存在 `metadata.txt`。
- [ ] `classFactory()` 正确。
- [ ] 主插件类可以正常创建。
- [ ] 目录结构清晰。

## 30.2 元数据

- [ ] 插件名称明确。
- [ ] 最低 QGIS 版本正确。
- [ ] 最高 QGIS 版本与测试范围一致。
- [ ] description 清楚。
- [ ] about 完整。
- [ ] version 已更新。
- [ ] author 和 email 正确。
- [ ] repository 有效。
- [ ] tracker 有效。
- [ ] icon 存在。
- [ ] category 正确。
- [ ] 依赖声明正确。

## 30.3 生命周期

- [ ] classFactory 正常。
- [ ] initGui 正常。
- [ ] unload 正常。
- [ ] 重复启用不会重复创建界面。
- [ ] 信号连接能够清理。
- [ ] 任务能够取消。
- [ ] Provider 能够注销。
- [ ] 地图工具能够恢复。
- [ ] 临时文件能够清理。

## 30.4 GUI

- [ ] 菜单显示正确。
- [ ] 工具栏按钮显示正确。
- [ ] 图标正确。
- [ ] QAction objectName 唯一。
- [ ] 对话框尺寸合理。
- [ ] 控件布局正常。
- [ ] 用户提示明确。
- [ ] 界面文字支持翻译。

## 30.5 数据处理

- [ ] 图层有效性检查。
- [ ] 输入数据检查。
- [ ] 字段检查。
- [ ] 几何类型检查。
- [ ] CRS 检查。
- [ ] 输出路径检查。
- [ ] 文件写入结果检查。
- [ ] 编辑提交和回滚逻辑正确。

## 30.6 Processing

- [ ] Provider ID 唯一。
- [ ] 算法 ID 稳定。
- [ ] 参数定义正确。
- [ ] 输出定义正确。
- [ ] Feedback 正常。
- [ ] 支持取消。
- [ ] createInstance 返回新对象。
- [ ] Batch Processing 正常。
- [ ] Model Designer 中能够调用。

## 30.7 稳定性

- [ ] 空项目测试。
- [ ] 无活动图层测试。
- [ ] 图层删除测试。
- [ ] 项目切换测试。
- [ ] 网络异常测试。
- [ ] 无效数据测试。
- [ ] 重复加载测试。
- [ ] 重复卸载测试。
- [ ] 后台任务取消测试。

## 30.8 发布

- [ ] LICENSE 存在。
- [ ] README 存在。
- [ ] 版本号唯一。
- [ ] ZIP 目录结构正确。
- [ ] 不包含开发缓存。
- [ ] 不包含无关文件。
- [ ] 运行依赖完整。
- [ ] 在干净的 QGIS 配置中安装成功。

---

# 31. 官方文档与源码参考

本指南主要依据以下 QGIS 3.44 官方资料整理。

## 31.1 QGIS Developers Guide

官方开发者指南：

https://docs.qgis.org/3.44/en/docs/developers_guide/index.html

重点章节：

| 章节 | 内容 |
|---|---|
| QGIS Coding Standards | 编码规范 |
| HIG | GUI 设计规范 |
| Development Process | 开发流程 |
| QtCreator and QGIS | 开发环境 |
| Localization | 国际化 |
| Unit Testing | 单元测试 |
| Processing Algorithms Testing | Processing 测试 |

## 31.2 PyQGIS Developer Cookbook

官方 PyQGIS 开发手册：

https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/index.html

重点章节：

| 章节 | 内容 |
|---|---|
| Introduction | PyQGIS 基础 |
| Loading Projects | 项目加载 |
| Loading Layers | 图层加载 |
| Accessing the Table of Contents | 图层树 |
| Using Raster Layers | 栅格 |
| Using Vector Layers | 矢量 |
| Geometry Handling | 几何 |
| Projections Support | 坐标参考系 |
| Using the Map Canvas | 地图画布 |
| Map Rendering and Printing | 渲染和布局 |
| Expressions | 表达式 |
| Reading and Storing Settings | 配置 |
| Communicating with the User | 消息和日志 |
| Authentication Infrastructure | 认证 |
| Tasks | 后台任务 |
| Developing Python Plugins | Python 插件 |
| Writing a Processing Plugin | Processing 插件 |
| Using Plugin Layers | 插件图层 |

## 31.3 插件开发官方章节

插件结构：

https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/plugins/plugins.html

插件调试：

https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/plugins/ide_debugging.html

插件发布：

https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/plugins/releasing.html

Processing 插件：

https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/processing.html

## 31.4 重要源码文件

以下源码链接均指向 QGIS 官方仓库的 `release-3_44` 分支。

### Python 插件加载机制

`python/utils.py`

https://github.com/qgis/QGIS/blob/release-3_44/python/utils.py

重点关注：

- `findPlugins()`
- `updateAvailablePlugins()`
- `loadPlugin()`
- `_startPlugin()`
- `startPlugin()`
- `startProcessingPlugin()`
- `unloadPlugin()`
- `_unloadPluginModules()`
- `reloadPlugin()`

这是理解 QGIS Python 插件加载、初始化和卸载过程最重要的源码文件。

### Processing 插件实现

`python/plugins/processing/ProcessingPlugin.py`

https://github.com/qgis/QGIS/blob/release-3_44/python/plugins/processing/ProcessingPlugin.py

重点关注：

- `ProcessingPlugin`
- `initProcessing()`
- `initGui()`
- `unload()`
- `_gui_connections`

它展示了 QGIS 自身如何组织一个具有大量功能的 Processing 插件。

### Processing Provider

`src/core/processing/qgsprocessingprovider.cpp`

https://github.com/qgis/QGIS/blob/release-3_44/src/core/processing/qgsprocessingprovider.cpp

重点关注：

- `refreshAlgorithms()`
- `addAlgorithm()`
- `algorithms()`
- Provider 与 Algorithm 的关系。
- 算法注册和生命周期。

### Processing Algorithm

`src/core/processing/qgsprocessingalgorithm.cpp`

https://github.com/qgis/QGIS/blob/release-3_44/src/core/processing/qgsprocessingalgorithm.cpp

重点关注：

- `create()`
- `id()`
- `checkParameterValues()`
- `createExpressionContext()`
- 算法参数验证。
- 算法实例创建。

### QGIS 插件接口

`src/gui/qgisinterface.h`

https://github.com/qgis/QGIS/blob/release-3_44/src/gui/qgisinterface.h

用于了解 QGIS 向插件开放的公共接口。

### 地图画布

`src/gui/qgsmapcanvas.cpp`

https://github.com/qgis/QGIS/blob/release-3_44/src/gui/qgsmapcanvas.cpp

用于理解地图画布、渲染和交互机制。

### 后台任务

`src/core/qgstaskmanager.cpp`

https://github.com/qgis/QGIS/blob/release-3_44/src/core/qgstaskmanager.cpp

用于理解：

- QgsTask。
- 任务启动。
- 任务取消。
- 子任务。
- 任务管理机制。

## 31.5 PyQGIS 3.44 API 文档

建议开发时使用对应版本的 API 文档，而不是只查最新版文档。

常用入口：

- [QgsProject](https://qgis.org/pyqgis/3.44/core/QgsProject.html)
- [QgsVectorLayer](https://qgis.org/pyqgis/3.44/core/QgsVectorLayer.html)
- [QgsTask](https://qgis.org/pyqgis/3.44/core/QgsTask.html)
- [QgsProcessingAlgorithm](https://qgis.org/pyqgis/3.44/core/QgsProcessingAlgorithm.html)
- [QgsProcessingProvider](https://qgis.org/pyqgis/3.44/core/QgsProcessingProvider.html)
- [QgsMapCanvas](https://qgis.org/pyqgis/3.44/gui/QgsMapCanvas.html)
- [QgisInterface](https://qgis.org/pyqgis/3.44/gui/QgisInterface.html)

---

# 结语

开发 QGIS 插件，不需要一开始就设计非常复杂的架构。

对于绝大多数业务插件，最实用的做法是：

1. 使用标准 Python 插件结构。
2. 用 `classFactory()` 创建插件对象。
3. 用 `initGui()` 注册界面。
4. 用 `unload()` 清理所有资源。
5. 通过 `QgisInterface` 与 QGIS 主程序交互。
6. 使用 `QgsProject`、`QgsVectorLayer`、`QgsRasterLayer` 和 `QgsGeometry` 完成 GIS 操作。
7. 把可复用的分析逻辑写成 Processing 算法。
8. 使用 `QgsTask` 处理耗时任务。
9. 使用 QGIS 的设置和认证体系管理数据。
10. 在发布前认真检查项目切换、图层删除、任务取消和插件卸载。

真正优秀的 QGIS 插件，不是使用了多少 API、创建了多少类，而是能够让用户顺畅完成工作，同时不破坏 QGIS 原有的项目、图层、地图交互和数据管理体验。

**把 QGIS 已经提供的能力用好，把插件自身的职责做好，把生命周期管理完整，通常就是最好的开发方式。**