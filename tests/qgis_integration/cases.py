# 静态分析器自己的解释器里没有 qgis/pydantic，所以本文件只关掉“找不到模块”这一类报告；能不能
# 真的加载由 QGIS 解释器的验收跑证明。
# The analyzer's own interpreter has no qgis/pydantic, so this file turns off only the
# missing-module report; whether it really loads is proven by the QGIS interpreter's run.
# pyright: reportMissingImports=false

"""验收用例集：六个能力族各一处，按这个顺序跑。

一条用例一个函数，只返回一行证据；判定是汇总器的字段，不从文本里数。用例属于哪个族看它的
编号前缀，同族内按编号跑，函数名跟着编号走。

The acceptance case set: one module per capability family, run in this order.  A case is one
function returning one line of evidence, and the verdict is a field the tally counts rather
than a word in the text.  A case's family is its id prefix; within a family the ids keep their
order, and a function name follows its id.
"""

from __future__ import annotations

from cases_engine import ENGINE_CASES
from cases_layer import LAYER_CASES
from cases_processing import PROCESSING_CASES
from cases_project import PROJECT_CASES
from cases_qml import QML_CASES
from cases_renderer import RENDERER_CASES

# 顺序就是跑的顺序：引擎与会话、工程、图层、处理、样式、渲染器；同族内按编号。
# The run order: engine and session, project, layer, processing, style, renderer; ids keep
# their order inside a family.
CASES = (
    *ENGINE_CASES,
    *PROJECT_CASES,
    *LAYER_CASES,
    *PROCESSING_CASES,
    *QML_CASES,
    *RENDERER_CASES,
)
