"""The operation dock: pick an operation, fill its parameters, read the record.

A manual test tool rather than a framework part (F10).  It reads the specs the
registry already publishes, builds one editor per ``input_schema`` property, and
calls the two engine methods the Python Console would call -- ``run_sync`` for a
main-thread operation, ``submit`` plus polling for a background one.

The surface is an Operate one (F11): selector, parameters and result stack top to
bottom, one accent (the run button), and semantic colour only where it means
something -- the status capsule and the error panel.  Every colour comes from
:mod:`qgis_agent_plugin.ui.theme`, which derives it from the application palette,
so nothing here is a literal and the dock follows the QGIS light and dark themes.

Qt is imported at module level here (``plugin`` and the runners carry the same
permission); QGIS is not imported at all, because the dock only knows the registry,
the engine and Qt, and the first two are QGIS-free.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from qgis.PyQt.QtCore import QEvent, QLocale, QPointF, QRect, QSize, Qt, QTimer
from qgis.PyQt.QtGui import (
    QDoubleValidator,
    QFont,
    QFontMetrics,
    QIntValidator,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
)
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QShortcut,
    QSizePolicy,
    QSpacerItem,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from ..core.execution_store import TERMINAL_STATUSES
from ..core.models import (
    INTERNAL_ERROR,
    INVALID_INPUT,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionStrategy,
    FrameworkError,
    OperationSpec,
)
from . import theme

# How often a submitted call is asked for its record (F10: about five times a second).
POLL_MS = 200

# How long the copy button says it copied.
COPIED_MS = 1600

# What the two numeric validators offer: wide enough for any count or coordinate,
# small enough for a 32-bit int.
_INTEGER = 2**31 - 1
_NUMBER = 1e12

# The delegate's data roles on a selector row: the name is Qt.DisplayRole.
_META_ROLE = Qt.UserRole + 1
_DESTRUCTIVE_ROLE = Qt.UserRole + 2
# Room either side of a selector row's two columns.
_ROW_PAD = 10

# The tallest the parameter block may get before it scrolls.  Without a cap a long
# form would push the record out of the dock; below the cap the block is exactly as
# tall as its rows, so the record gets everything else.
_FORM_MAX = 320

# The shortest the block gets before it scrolls: one row and a scrollbar's worth.
_FORM_MIN = 54

# What the record area says when it has nothing to show; the two are not the same
# thing, and a failed submit must not be told to run again.
_IDLE_HINT = "运行一次后，这里显示 ExecutionRecord 的 JSON。"
_NO_RECORD_HINT = "本次调用没有产生执行记录。"

# How a schema ``type`` becomes an editor; anything not listed is a single-line text.
_KINDS = {
    "boolean": "bool",
    "integer": "integer",
    "number": "number",
    "array": "json",
    "object": "json",
}

# The operation's own field names in the dock's language; a field this map has not
# met yet shows the schema's title, so a new operation still gets a usable form.
_FIELD_LABELS = {
    "kind": "类型",
    "source_path": "来源路径",
    "name": "名称",
    "sub_layer": "子图层",
    "add_to_legend": "加入图例",
    "layer_id": "图层 ID",
    "visible": "可见",
    "field_name": "字段名",
    "crs_authid": "坐标参考系",
    "discard_current": "丢弃当前工程",
    "path": "路径",
    "overwrite": "覆盖已有文件",
}

# What a failure means, in the dock's own words: ``code -> (what happened, what next)``.
# The engine's own message is kept as the label's tooltip -- it is written for a
# caller, not for the operator, and half of it is English.
_ERRORS = {
    "OPERATION_NOT_FOUND": ("注册表里没有这个操作。", "重新启用插件，让九个操作重新注册。"),
    "INVALID_OPERATION_DEFINITION": ("操作声明不合法，注册被拒。", "这是插件缺陷，看 QGIS 日志。"),
    "DUPLICATE_OPERATION": ("同一个操作被注册了两次。", "这是插件缺陷，看 QGIS 日志。"),
    "WRONG_THREAD": ("这个操作必须在主线程运行。", "重新启用插件后再试；持续出现就是缺陷。"),
    "ENGINE_CLOSING": ("插件正在关闭，调用没有执行。", "重新启用插件后再试。"),
    "UNSUPPORTED_EXECUTION_MODE": ("这个执行方式不受支持。", "这是插件缺陷，看 QGIS 日志。"),
    "EXECUTION_NOT_FOUND": ("这次调用的记录已经不在了。", "重新运行一次。"),
    "INVALID_INPUT": ("参数没有通过校验。", "按提示改掉参数，再运行一次。"),
    "INVALID_CRS": ("坐标参考系写法不对。", "用 EPSG:4326 这样的写法重试。"),
    "PROJECT_REPLACE_REQUIRED": (
        "当前工程还有未保存的内容。",
        "勾上“丢弃当前工程”，或先保存当前工程。",
    ),
    "PROJECT_OPEN_FAILED": (
        "工程文件打不开。",
        "确认文件存在、没有损坏，并且仍在允许的目录里。",
    ),
    "PROJECT_SAVE_FAILED": ("工程没有写出去。", "确认目标目录存在且可写，再运行一次。"),
    "PROJECT_PATH_REQUIRED": ("没有给出工程文件路径。", "填写 .qgs 或 .qgz 的完整路径，再运行。"),
    "TARGET_EXISTS": ("目标文件已经存在。", "换一个文件名，或勾上“覆盖已有文件”。"),
    "PATH_NOT_ALLOWED": (
        "这个路径不在允许的目录里。",
        "把这个目录写进配置 qgis_agent_plugin/allowed_data_roots 后重试。",
    ),
    "INVALID_LAYER": ("这个图层不能用来做这件事。", "确认图层仍在本工程里，并检查类型与字段。"),
    "LAYER_NOT_FOUND": ("工程里没有这个图层。", "先运行 layer.list，从结果里取 layer_id。"),
    "LAYER_NOT_IN_TREE": ("图层不在图层面板里。", "把它加回图层面板后重试。"),
    "INVALID_FIELD_TYPE": ("这个字段的类型不适合做这件事。", "换一个数值字段再运行。"),
    "SNAPSHOT_LIMIT_EXCEEDED": (
        "图层要素数超过快照上限。",
        "调大 max_snapshot_features，或换一个小图层。",
    ),
    "TOO_MANY_EXECUTIONS": ("排队或并发到上限了。", "等正在运行的调用结束后重试。"),
    "TASK_SUBMISSION_FAILED": ("后台任务没有提交成功。", "重试一次；仍然失败就重启 QGIS。"),
    "STALE_PROJECT": ("调用开始时工程已经换了一代。", "确认当前工程后重新运行。"),
    "OUTPUT_VALIDATION_FAILED": (
        "操作的返回值不符合它声明的结构。",
        "这是操作实现的缺陷，看 QGIS 日志。",
    ),
    "INTERNAL_ERROR": ("界面遇到意外错误，调用没有完成。", "重试一次；仍然出现就看 QGIS 日志。"),
}
_HINT = "按错误信息改掉参数再试；与参数无关时，重新启用插件。"

# The capsule's meaning, read out to a screen reader with the label.
_STATE_HINTS = {
    "empty": "插件没有注册任何操作。",
    "idle": "等待运行。",
    "running": "正在运行；后台调用可以取消。",
    "ok": "运行完成，结果可以复制。",
    "failed": "运行失败，错误详情在下方。",
    "cancelled": "运行已取消。",
}


class _SpecDelegate(QStyledItemDelegate):
    """One selector row: the name, then ``kind · strategy`` right-aligned in mono.

    A delegate rather than a padded string, because the meta column has to line up
    down the popup and proportional text cannot do that with spaces.  The row is
    painted by the style first, so hover and selection keep the platform's look.
    """

    def __init__(self, parent: Any, look: theme.Theme) -> None:
        super().__init__(parent)
        self._look = look

    def paint(self, painter: Any, option: Any, index: Any) -> None:
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        styled.text = ""  # the style paints the row; the two columns are drawn below
        widget = styled.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, styled, painter, widget)

        selected = bool(styled.state & QStyle.State_Selected)
        name = index.data(Qt.DisplayRole) or ""
        meta = index.data(_META_ROLE) or ""
        area = styled.rect.adjusted(_ROW_PAD, 0, -_ROW_PAD, 0)

        painter.save()
        if index.data(_DESTRUCTIVE_ROLE):
            painter.setPen(Qt.NoPen)
            painter.setBrush(self._look.on_accent if selected else self._look.danger)
            painter.drawEllipse(area.left() + 1, area.center().y() - 2, 5, 5)
            area.setLeft(area.left() + 12)

        meta_font = QFont(self._look.mono)
        meta_font.setPointSize(self._look.small)
        width = QFontMetrics(meta_font).horizontalAdvance(meta)
        painter.setFont(meta_font)
        painter.setPen(self._look.on_accent if selected else self._look.muted)
        painter.drawText(
            QRect(area.right() - width, area.top(), width, area.height()),
            Qt.AlignRight | Qt.AlignVCenter,
            meta,
        )
        painter.setFont(QFont(styled.font))
        painter.setPen(self._look.on_accent if selected else self._look.text)
        painter.drawText(
            QRect(area.left(), area.top(), max(0, area.width() - width - 12), area.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            name,
        )
        painter.restore()

    def sizeHint(self, option: Any, index: Any) -> QSize:
        size = super().sizeHint(option, index)
        meta_font = QFont(self._look.mono)
        meta_font.setPointSize(self._look.small)
        width = QFontMetrics(meta_font).horizontalAdvance(index.data(_META_ROLE) or "")
        size.setWidth(size.width() + width + 2 * _ROW_PAD + 16)
        return size


class _FormArea(QScrollArea):
    """A scroll area that asks for its rows' height, and gives it back when space is short.

    A plain scroll area reports a small hint, so the column would never give the form
    the height its rows need; this one reports the rows and lets the layout shrink it
    to a scrollable minimum when the dock is at its floor.
    """

    def sizeHint(self) -> QSize:
        inner = self.widget()
        if inner is None:
            return super().sizeHint()
        return QSize(super().sizeHint().width(), min(inner.sizeHint().height(), _FORM_MAX))


class _Combo(QComboBox):
    """A combo whose chevron is drawn here.

    Styling the native drop-down leaves a seam along the field's edge, and the
    only way to keep Qt's arrow is to leave that seam; two lines of painting give
    a clean field in both themes instead.
    """

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = self.palette().color(QPalette.WindowText if self.isEnabled() else QPalette.Mid)
        painter.setPen(QPen(color, 1.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        x = self.width() - 15
        y = self.height() / 2
        painter.drawLine(QPointF(x - 4, y - 2), QPointF(x, y + 2.5))
        painter.drawLine(QPointF(x, y + 2.5), QPointF(x + 4, y - 2))


class _Field(QLineEdit):
    """A line edit that draws its own hint.

    Qt paints a ``QLineEdit`` placeholder as half-strength text whenever a
    stylesheet is in force (3.3:1 on the light field) and ignores
    ``QPalette::PlaceholderText``, so a default value shown that way is the least
    readable text in the dock.  Painting the hint here keeps it at the theme's
    muted colour -- and the tests measure the pixels, not this comment.
    """

    def __init__(self) -> None:
        super().__init__()
        self._hint = ""
        self._hint_color: Any = None

    def set_hint(self, text: str, color: Any) -> None:
        self._hint = text
        self._hint_color = color
        self.update()

    @property
    def hint(self) -> str:
        """The hint an empty field shows; the tests read this instead of a placeholder."""

        return self._hint

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        if not self._hint or self.text() or not self.isEnabled() or self._hint_color is None:
            return
        painter = QPainter(self)
        painter.setPen(self._hint_color)
        flags = Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine
        painter.drawText(self.rect().adjusted(9, 0, -9, 0), flags, self._hint)


class _RecordView(QPlainTextEdit):
    """The record view, plus the hint it shows while there is no record.

    Qt paints a ``QPlainTextEdit`` placeholder behind the stylesheet's back: the
    widget palette is ignored and half-strength text comes out at 3.4:1 on the
    light field.  The dock states its own hint in its own colour instead.  The
    view takes whatever is left over, so its size hint stays modest -- a plain
    text edit asks for a screenful and would squeeze the form before the record.
    """

    def __init__(self) -> None:
        super().__init__()
        self.hint = QLabel("", self.viewport())
        self.hint.setObjectName("resultHint")
        self.hint.setWordWrap(True)
        self.hint.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.hint.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.hint.hide()
        self.viewport().installEventFilter(self)
        self.textChanged.connect(self._sync_hint)

    def set_hint(self, text: str, color: Any) -> None:
        self.hint.setText(text)
        self.hint.setStyleSheet(f"color: {color.name()}; background: transparent;")
        self._sync_hint()

    def sizeHint(self) -> QSize:
        return QSize(320, 120)

    def _sync_hint(self) -> None:
        empty = not self.toPlainText()
        if empty and self.hint.text():
            self.hint.setGeometry(self.viewport().rect().adjusted(10, 8, -10, -8))
        self.hint.setVisible(empty and bool(self.hint.text()))

    def eventFilter(self, source: Any, event: Any) -> bool:
        if source is self.viewport() and event.type() == QEvent.Resize:
            self._sync_hint()
        return super().eventFilter(source, event)


class OperationDock(QDockWidget):
    """One operation selected, one form for it, one record on screen.

    A ``QDockWidget`` because that is what ``QMainWindow.addDockWidget`` and
    ``QgisInterface.addDockWidget`` take; everything the dock shows lives inside the
    one widget it carries.
    """

    def __init__(self, registry: Any, engine: Any) -> None:
        super().__init__()
        self.setObjectName("QGISAgentOperationDock")
        self.setWindowTitle("QGIS Agent 操作")
        self.setMinimumWidth(340)
        self._registry = registry
        self._engine = engine
        self._specs: dict[str, OperationSpec] = {}
        # Field name to ``(kind, editor)``, rebuilt with every form.
        self.editors: dict[str, tuple[str, Any]] = {}
        # One of theme.STATE_LABELS: what the capsule says.
        self.state = "idle"
        # The live background call, or None: what the cancel button acts on.
        self._execution_id: str | None = None
        self._busy = False
        # The record being shown, and what the cancel receipt added to its line.
        self._record: ExecutionRecord | None = None
        self._note = ""
        # Where the keyboard was when the run started: disabling the form would
        # otherwise park it on the record view.
        self._focus: Any = None
        self.look = theme.build(QApplication.palette())
        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self._poll)
        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.setInterval(COPIED_MS)
        self._copy_timer.timeout.connect(lambda: self.copy_button.setText("复制 JSON"))
        self._build()
        self.setStyleSheet(self.look.stylesheet())
        self.shortcuts = [self._shortcut(sequence) for sequence in ("Ctrl+Return", "Ctrl+Enter")]
        self._fill_operations()

    # -- the surface ---------------------------------------------------------

    def _build(self) -> None:
        body = QWidget()
        body.setObjectName("dockBody")
        body.setAttribute(Qt.WA_StyledBackground, True)
        column = QVBoxLayout(body)
        column.setContentsMargins(16, 16, 16, 16)
        column.setSpacing(0)

        self.operations = _Combo()
        self.operations.setObjectName("operations")
        self.operations.setItemDelegate(_SpecDelegate(self.operations, self.look))
        self.operations.setAccessibleName("操作选择器")
        self.operations.setAccessibleDescription(
            "选择要运行的 Operation，右侧是它的类型与执行策略。"
        )
        self.operations.setToolTip("选择要运行的 Operation。方向键切换，Ctrl+Return 运行。")
        self.operations.currentIndexChanged.connect(self._selection_changed)

        self.meta = QLabel("")
        self.meta.setObjectName("meta")
        self.meta.setToolTip("operation 的类型与执行策略")
        self.summary = QLabel("")
        self.summary.setObjectName("summary")
        self.summary.setWordWrap(True)
        self.details = QWidget()
        details = QVBoxLayout(self.details)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(4)
        details.addWidget(self.meta)
        details.addWidget(self.summary)
        self.details.setVisible(False)

        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setHorizontalSpacing(12)
        self.form.setVerticalSpacing(8)
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.form.setRowWrapPolicy(QFormLayout.DontWrapRows)
        self.form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.params = _FormArea()
        self.params.setObjectName("params")
        self.params.setWidget(self.form_host)
        self.params.setWidgetResizable(True)
        self.params.setFrameShape(QFrame.NoFrame)
        self.params.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Rows decide the height; below one row it scrolls rather than squeezing the
        # record, and the layer above it can shrink it when the dock is at its floor.
        self.params.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.params.setMinimumHeight(_FORM_MIN)
        # An operation with no parameters hides the block, margins and all.
        self.params.setVisible(False)

        self.run_button = QPushButton("运行")
        self.run_button.setObjectName("primary")
        self.run_button.setAccessibleName("运行")
        self.run_button.setToolTip("运行当前操作（Ctrl+Return）")
        self.run_button.clicked.connect(self.run)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("secondary")
        self.cancel_button.setAccessibleName("取消")
        self.cancel_button.setToolTip("请求取消正在运行的后台调用")
        self.cancel_button.clicked.connect(self.cancel)
        self.copy_button = QPushButton("复制 JSON")
        self.copy_button.setObjectName("text")
        self.copy_button.setAccessibleName("复制 JSON")
        self.copy_button.setToolTip("把结果区的 JSON 复制到剪贴板")
        self.copy_button.clicked.connect(self.copy_json)
        self.capsule = QLabel(theme.STATE_LABELS["idle"])
        self.capsule.setObjectName("capsule")
        self.capsule.setAlignment(Qt.AlignCenter)
        self.capsule.setAccessibleName("运行状态")

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        actions.addWidget(self.run_button)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.copy_button)
        actions.addStretch(1)
        actions.addWidget(self.capsule)

        self.progress = QProgressBar()
        self.progress.setObjectName("progress")
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName("调用进度")
        self.progress.setVisible(False)

        self.feedback = QWidget()
        self.feedback.setObjectName("banner")
        self.feedback.setAttribute(Qt.WA_StyledBackground, True)
        banner = QVBoxLayout(self.feedback)
        banner.setContentsMargins(12, 10, 12, 10)
        banner.setSpacing(4)
        self.error_code = QLabel("")
        self.error_code.setObjectName("errorCode")
        self.error_message = QLabel("")
        self.error_message.setObjectName("errorMessage")
        self.error_message.setWordWrap(True)
        self.error_hint = QLabel("")
        self.error_hint.setObjectName("errorHint")
        self.error_hint.setWordWrap(True)
        banner.addWidget(self.error_code)
        banner.addWidget(self.error_message)
        banner.addWidget(self.error_hint)
        self.feedback.setVisible(False)

        self.result_summary = QLabel("")
        self.result_summary.setObjectName("resultSummary")
        self.result_summary.setWordWrap(True)
        self.result_summary.setVisible(False)
        self.result = _RecordView()
        self.result.setObjectName("result")
        self.result.setReadOnly(True)
        self.result.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.result.set_hint(_IDLE_HINT, self.look.muted)
        self.result.setMinimumHeight(110)
        self.result.setAccessibleName("执行记录 JSON")
        self.result.setAccessibleDescription("只读；可以全选复制，或点“复制 JSON”。")
        self.empty_state = self._build_empty_state()

        self.output = QWidget()
        output = QVBoxLayout(self.output)
        output.setContentsMargins(0, 0, 0, 0)
        output.setSpacing(8)
        output.addWidget(self.feedback)
        output.addWidget(self.result_summary)
        output.addWidget(self.result, 1)
        output.addWidget(self.empty_state, 1)

        self.space_before = QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.space_after = QSpacerItem(0, 16, QSizePolicy.Minimum, QSizePolicy.Fixed)
        column.addWidget(self.operations)
        column.addSpacing(8)
        column.addWidget(self.details)
        column.addItem(self.space_before)
        column.addWidget(self.params)
        column.addItem(self.space_after)
        column.addLayout(actions)
        column.addSpacing(8)
        column.addWidget(self.progress)
        column.addSpacing(12)
        column.addWidget(self.output, 1)
        self.setWidget(body)

    def _build_empty_state(self) -> QWidget:
        """What takes the result's place when the registry has nothing in it."""

        widget = QWidget()
        widget.setObjectName("emptyState")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(8)
        title = QLabel("还没有注册的操作")
        title.setObjectName("emptyTitle")
        title.setWordWrap(True)
        body = QLabel(
            "插件启动时没有登记任何 Operation，所以这里没有可以运行的东西。\n"
            "重新启用插件或重启 QGIS；仍然为空的话，看 QGIS 日志里的注册错误。"
        )
        body.setObjectName("emptyBody")
        body.setWordWrap(True)
        layout.addStretch(1)
        layout.addWidget(title)
        layout.addWidget(body)
        layout.addStretch(1)
        widget.setVisible(False)
        return widget

    def _shortcut(self, sequence: str) -> QShortcut:
        shortcut = QShortcut(QKeySequence(sequence), self)
        shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        shortcut.activated.connect(self.run)
        return shortcut

    def _fill_operations(self) -> None:
        specs = sorted(self._registry.list_specs(), key=lambda spec: spec.name)
        self._specs = {spec.name: spec for spec in specs}
        self.operations.blockSignals(True)
        for spec in specs:
            self.operations.addItem(spec.name)
            row = self.operations.count() - 1
            self.operations.setItemData(row, spec.name, Qt.UserRole)
            self.operations.setItemData(
                row, f"{spec.kind.value} · {spec.strategy.value}", _META_ROLE
            )
            self.operations.setItemData(row, bool(spec.destructive), _DESTRUCTIVE_ROLE)
        self.operations.blockSignals(False)
        if not specs:
            self.operations.setPlaceholderText("没有可用的操作")
            self.result_summary.setVisible(False)
            self.result.setVisible(False)
            self.empty_state.setVisible(True)
            self._apply_state("empty")
            self._fit_wrapping_blocks()
            return
        self.operations.setCurrentIndex(0)
        # Adding the first item while blocked already made it current, so the change
        # signal may never fire; the first form is built here either way.
        self._selection_changed(self.operations.currentIndex())

    def _selected_spec(self) -> OperationSpec | None:
        return self._specs.get(self.operations.currentData())

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._fit_wrapping_blocks()

    def _fit_wrapping_blocks(self) -> None:
        """Give the wrapping blocks the height their text needs at this width.

        A layout's minimum counts a word-wrapped label as one line, so at the dock's
        own minimum the error banner and the description used to lose their last
        line and push the record past the body.  The width is known here, so say it.
        """

        if not self.isVisible():
            return
        width = max(0, self.width() - 32)  # the body's 16 px margins
        width = max(0, self.width() - 32)  # the body's 16 px margins
        for block in (self.details, self.feedback, self.empty_state):
            if block.isHidden():
                block.setMinimumHeight(0)
                continue
            needed = block.heightForWidth(width)
            if needed > 0:
                block.setMinimumHeight(needed)

    def _selection_changed(self, index: int) -> None:
        """A different operation: its form, and nothing left of the previous run."""

        if self.state == "running":
            return
        spec = self._selected_spec()
        self._build_form(spec)
        self._record = None
        self._note = ""
        self.result.clear()
        self.result.set_hint(_IDLE_HINT, self.look.muted)
        self._render_summary()
        self._hide_banner()
        if spec is not None:
            self.meta.setText(
                f"{spec.kind.value} · {spec.strategy.value}"
                + (" · 可破坏" if spec.destructive else "")
            )
            self.meta.setStyleSheet(
                f"color: {self.look.danger.name()};" if spec.destructive else ""
            )
            self.summary.setText(spec.description.strip())
        else:
            self.meta.setText("")
            self.summary.setText("")
        self.details.setVisible(bool(self.meta.text() or self.summary.text()))
        self._fit_wrapping_blocks()
        self._apply_state("idle")

    def _build_form(self, spec: OperationSpec | None) -> None:
        """Replace the editors with the selected operation's, one row per property."""

        # removeRow deletes the row's widgets too, so a switch cannot leave either a
        # stale editor or an empty row behind.
        while self.form.rowCount():
            self.form.removeRow(0)
        self.editors = {}
        if spec is None:
            self.params.setVisible(False)
            self.space_before.changeSize(0, 0, QSizePolicy.Minimum, QSizePolicy.Fixed)
            return
        properties = spec.input_schema.get("properties") or {}
        required = set(spec.input_schema.get("required") or ())
        for name, schema in properties.items():
            title = _FIELD_LABELS.get(name) or schema.get("title") or name
            kind = _kind(schema)
            editor = _editor(kind, schema, self.look)
            editor.setAccessibleName(title)
            editor.setToolTip(_field_tooltip(title, name, schema))
            self.editors[name] = (kind, editor)
            label = _field_label(title, name in required, self.look)
            self.form.addRow(label, editor)
            # A widget added to the layout of an already-visible window stays hidden
            # until the next event turn, and a hidden row contributes nothing to the
            # form's height: the block would be sized for no rows at all.
            label.show()
            editor.show()
        # Nothing to fill means no block at all: a one-line "takes no parameters"
        # row under a 16 px gap read as an unfinished section, not as an answer.
        self.params.setVisible(bool(properties))
        self.space_before.changeSize(
            0, 16 if properties else 0, QSizePolicy.Minimum, QSizePolicy.Fixed
        )
        # The rows were shown as they were added, so the hint is already the new
        # height; tell the column to ask for it again instead of keeping the old one.
        self.params.updateGeometry()
        self._apply_tab_order()

    def _apply_tab_order(self) -> None:
        """Selector, then the parameters top to bottom, then the run button."""

        previous = self.operations
        for _, editor in self.editors.values():
            QWidget.setTabOrder(previous, editor)
            previous = editor
        QWidget.setTabOrder(previous, self.run_button)

    # -- running -------------------------------------------------------------

    def run(self) -> None:
        """Send the form to the engine the way its strategy requires."""

        spec = self._selected_spec()
        if spec is None or self.state == "running":
            return
        arguments = self._arguments()
        if arguments is None:
            return  # the form reported itself; the engine is not called
        self._note = ""
        self._record = None
        self.result.clear()
        self.result.set_hint(_IDLE_HINT, self.look.muted)
        self._render_summary()
        self._hide_banner()
        self._focus = QApplication.focusWidget()
        self._apply_state("running")
        self._show_progress(0)
        if spec.strategy is ExecutionStrategy.MAIN_THREAD:
            self._run_sync(spec, arguments)
        else:
            self._submit(spec, arguments)

    def _run_sync(self, spec: OperationSpec, arguments: dict[str, Any]) -> None:
        self._busy = True
        self._refresh_buttons()
        try:
            record = self._engine.run_sync(spec.name, arguments)
        except Exception as error:  # noqa: BLE001 - a Qt slot may not raise
            self._show_failure(error)
            return
        finally:
            self._busy = False
            self._refresh_buttons()
        self._show_record(record)

    def _submit(self, spec: OperationSpec, arguments: dict[str, Any]) -> None:
        try:
            record = self._engine.submit(spec.name, arguments)
        except Exception as error:  # noqa: BLE001 - a Qt slot may not raise
            self._show_failure(error)
            return
        # A submit answers with a terminal record when it was refused outright.
        self._execution_id = None if record.status in TERMINAL_STATUSES else record.execution_id
        self._show_record(record)
        if self._execution_id is not None:
            self.timer.start()
        self._refresh_buttons()

    def _poll(self) -> None:
        """Ask the engine for the live record; stop at the first terminal one."""

        execution_id = self._execution_id
        if execution_id is None:
            self.timer.stop()
            return
        try:
            record = self._engine.get(execution_id)
        except Exception as error:  # noqa: BLE001 - a Qt slot may not raise
            self._end_polling()
            self._show_failure(error)
            return
        self._show_record(record)
        if record.status in TERMINAL_STATUSES:
            self._end_polling()

    def cancel(self) -> None:
        """Ask for a stop and say what the engine answered, in the summary line."""

        execution_id = self._execution_id
        if execution_id is None:
            return
        try:
            receipt = self._engine.cancel(execution_id)
        except Exception as error:  # noqa: BLE001 - a Qt slot may not raise
            self._show_failure(error)
            return
        if receipt.accepted:
            self._note = "已请求取消（accepted=True）"
        else:
            self._note = f"取消未被受理（reason={receipt.reason or '未知'}）"
        self._render_summary()

    def copy_json(self) -> None:
        """Put the shown record on the clipboard, and say so on the button that did it."""

        text = self.result.toPlainText()
        if not text:
            return
        QApplication.clipboard().setText(text)
        self.copy_button.setText("已复制")
        self._copy_timer.start()

    def shutdown(self) -> None:
        """Stop polling and forget the run, so a removed dock leaves nothing live."""

        self._busy = False
        self._copy_timer.stop()
        self._end_polling()

    # -- what is shown --------------------------------------------------------

    def _arguments(self) -> dict[str, Any] | None:
        """The form as arguments, or None when a field did not parse.

        A field left empty is not submitted, so the model's default decides; the
        exception is a checkbox, whose unchecked state is a real answer rather than
        a missing one.
        """

        arguments: dict[str, Any] = {}
        for name, (kind, editor) in self.editors.items():
            if kind == "bool":
                arguments[name] = editor.isChecked()
            elif kind == "enum":
                value = editor.currentData()
                if value is not None:
                    arguments[name] = value
            elif kind in {"integer", "number"}:
                text = editor.text().strip()
                if not text:
                    continue
                try:
                    arguments[name] = int(text) if kind == "integer" else float(text)
                except ValueError:
                    self._show_problem(
                        INVALID_INPUT,
                        f"{_FIELD_LABELS.get(name, name)}：不是合法的"
                        f"{'整数' if kind == 'integer' else '数字'}。",
                    )
                    return None
            else:
                text = editor.text().strip()
                if not text:
                    continue
                if kind == "json":
                    try:
                        arguments[name] = json.loads(text)
                    except ValueError:
                        self._show_problem(
                            INVALID_INPUT, f"{_FIELD_LABELS.get(name, name)}：不是合法的 JSON。"
                        )
                        return None
                else:
                    arguments[name] = text
        return arguments

    def _show_record(self, record: ExecutionRecord) -> None:
        self._record = record
        self.result.setPlainText(
            json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2)
        )
        state = _state_of(record.status)
        self._apply_state(state)
        self._render_summary()
        if state == "running":
            self._show_progress(record.progress)
        else:
            self.progress.setVisible(False)
        if record.error is not None:
            self._show_engine_error(record.error.code, record.error.message)
        else:
            self._hide_banner()

    def _show_failure(self, error: Exception) -> None:
        """A failure the engine raised: no record came back, so the dock clears."""

        if isinstance(error, FrameworkError):
            code, detail = error.code, error.message
        else:
            code, detail = INTERNAL_ERROR, f"{type(error).__name__}: {error}"
        self._clear_result()
        self._show_engine_error(code, detail)

    def _show_engine_error(self, code: str, detail: str) -> None:
        """Name a failure in the dock's words; the engine's text becomes the tooltip.

        Half of what the engine writes is English, and it is written for a caller;
        this panel is the operator's.
        """

        happened, next_step = _ERRORS.get(code, (f"引擎返回：{detail}", _HINT))
        self._show_banner(code, happened, next_step, detail)

    def _show_problem(self, code: str, message: str) -> None:
        """A failure the form found itself, already in the dock's words."""

        self._clear_result()
        self._show_banner(code, message, _ERRORS.get(code, ("", _HINT))[1], message)

    def _clear_result(self) -> None:
        """Forget the record being shown: the call never produced one."""

        self._record = None
        self._note = ""
        self.result.clear()
        self.result.set_hint(_NO_RECORD_HINT, self.look.muted)
        self._render_summary()
        self._hide_banner()
        self._apply_state("failed")
        self.progress.setVisible(False)

    def _show_banner(self, code: str, message: str, hint: str, detail: str = "") -> None:
        self.error_code.setText(code)
        self.error_message.setText(message)
        self.error_message.setToolTip(detail)
        self.error_hint.setText(hint)
        self.feedback.setAccessibleDescription(f"错误 {code}：{message}。{hint}")
        self.feedback.setVisible(True)
        self._fit_wrapping_blocks()

    def _hide_banner(self) -> None:
        self.feedback.setVisible(False)
        self._fit_wrapping_blocks()

    def _apply_state(self, state: str) -> None:
        """Say the state in the capsule, in colour, and let the controls agree."""

        previous, self.state = self.state, state
        label = theme.STATE_LABELS[state]
        foreground, background = self.look.capsule(state)
        self.capsule.setText(label)
        self.capsule.setStyleSheet(
            f"color: {foreground.name()}; background: {background.name()};"
            f" border-radius: 9px; padding: 2px 10px;"
            f" font-size: {self.look.small}pt; font-weight: 600;"
        )
        self.capsule.setAccessibleDescription(f"当前状态：{label}。{_STATE_HINTS[state]}")
        self.operations.setEnabled(bool(self._specs) and state != "running")
        for _, editor in self.editors.values():
            editor.setEnabled(state != "running")
        self._refresh_buttons()
        if previous == "running" and state != "running":
            self._restore_focus()

    def _restore_focus(self) -> None:
        """Put the keyboard back where the run took it from."""

        widget, self._focus = self._focus, None
        try:
            if widget is not None and widget.isVisible() and widget.isEnabled():
                widget.setFocus()
        except RuntimeError:  # the widget went away with the operation that owned it
            self._focus = None

    def _show_progress(self, progress: float) -> None:
        """A live call: indeterminate until the runner reports a real number."""

        self.progress.setVisible(True)
        if progress > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(progress))
        else:
            self.progress.setRange(0, 0)

    def _render_summary(self) -> None:
        """Progress and time, under the result: the capsule is the only place that
        names the state, and a record with nothing to measure shows no line."""

        if self._record is None:
            self.result_summary.setVisible(False)
            return
        if _state_of(self._record.status) == "running":
            line = f"进度 {self._record.progress:.0f}%" if self._record.progress > 0 else ""
        else:
            line = f"耗时 {_duration(self._record)}"
        if self._note:
            line = f"{line} · {self._note}" if line else self._note
        if not line:
            self.result_summary.setVisible(False)
            return
        self.result_summary.setText(line)
        self.result_summary.setVisible(True)

    def _end_polling(self) -> None:
        self.timer.stop()
        self._execution_id = None
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        # One live background call at a time: while it runs, the cancel button is the
        # only way forward, so a second run cannot silently take over its record.
        live = self._execution_id is not None
        self.run_button.setEnabled(
            bool(self._specs) and not self._busy and not live and self.state != "running"
        )
        self.cancel_button.setEnabled(live and not self._busy)
        self.copy_button.setEnabled(bool(self.result.toPlainText()))


def _state_of(status: ExecutionStatus) -> str:
    """The capsule state a record status means."""

    if status is ExecutionStatus.SUCCEEDED:
        return "ok"
    if status is ExecutionStatus.FAILED:
        return "failed"
    if status is ExecutionStatus.CANCELLED:
        return "cancelled"
    return "running"


def _duration(record: ExecutionRecord) -> str:
    """How long the call took, or an em dash while it has not finished."""

    if record.finished_at is None:
        return "—"
    try:
        started = datetime.fromisoformat(record.started_at or record.created_at)
        finished = datetime.fromisoformat(record.finished_at)
    except ValueError:
        return "—"
    seconds = max(0.0, (finished - started).total_seconds())
    return f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.1f} s"


def _field_label(title: str, required: bool, look: theme.Theme) -> QLabel:
    """One form label: the field's name, with a small marker when it is required."""

    mark = (
        f' <span style="color:{look.muted.name()}; font-size:{look.small}pt">必填</span>'
        if required
        else ""
    )
    label = QLabel(f"{title}{mark}")
    label.setObjectName("fieldLabel")
    label.setTextFormat(Qt.RichText)
    label.setAccessibleName(f"{title}，必填" if required else title)
    return label


def _field_tooltip(title: str, name: str, schema: dict[str, Any]) -> str:
    tooltip = f"{title}（{name}）"
    description = schema.get("description")
    if description:
        tooltip += f"：{description}"
    if schema.get("default") is not None:
        tooltip += f"；留空用默认值 {schema['default']}"
    return tooltip


def _kind(schema: dict[str, Any]) -> str:
    """The editor kind for one property, looking through a nullable ``anyOf``."""

    for option in (schema, *(schema.get("anyOf") or ())):
        if "enum" in option:
            return "enum"
        kind = _KINDS.get(str(option.get("type")))
        if kind is not None:
            return kind
    return "text"


def _editor(kind: str, schema: dict[str, Any], look: theme.Theme) -> Any:
    """One editor for a field; the schema's default is shown, not filled in.

    Except a checkbox: it has no empty state, so its default is its initial state.
    """

    default = schema.get("default")
    if kind == "bool":
        editor = QCheckBox()
        editor.setChecked(bool(default))
        return editor
    if kind == "enum":
        editor = _Combo()
        editor.setEditable(False)
        if default is not None:
            editor.addItem(f"默认（{default}）", None)
        for value in _enum_values(schema):
            editor.addItem(str(value), value)
        editor.setCurrentIndex(0)
        return editor
    editor = _Field()
    if kind == "integer":
        editor.setValidator(QIntValidator(-_INTEGER, _INTEGER, editor))
    elif kind == "number":
        validator = QDoubleValidator(-_NUMBER, _NUMBER, 6, editor)
        validator.setNotation(QDoubleValidator.StandardNotation)
        # A dot is a dot: the float() in ``_arguments`` reads the C locale.
        validator.setLocale(QLocale("C"))
        editor.setValidator(validator)
    if default is not None:
        editor.set_hint(json.dumps(default) if kind == "json" else str(default), look.muted)
    return editor


def _enum_values(schema: dict[str, Any]) -> list[Any]:
    for option in (schema, *(schema.get("anyOf") or ())):
        if "enum" in option:
            return list(option["enum"])
    return []
