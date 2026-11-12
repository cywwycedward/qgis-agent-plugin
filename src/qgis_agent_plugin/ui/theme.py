"""坞窗的颜色、字号和样式表，全部从应用调色板推出来。

这是坞窗唯一的配色来源，而且这里也没有一个字面量：每个色调都是一个 ``QPalette`` 角色、该角色
调过明度后的版本，或两个角色的混合。语义色（ok / warn / danger）保留含义，明度由调色板定；
每一对文字都被推到过 WCAG 底线，所以坞窗在 QGIS 的浅色和深色主题上都读得清，换主题不需要
第二份样式表。

Qt 故意在函数内部 import。``ui/dock.py`` 是唯一允许在模块级 import Qt 的模块；这个即使被直接
import，也不会进入裸解释器的 import 图。

This is the dock's only colour authority, and even here nothing is a literal: every
tone is a ``QPalette`` role, that role moved in lightness, or two roles mixed.  The
semantic hues (ok / warn / danger) keep their meaning while the palette decides how
light they may be, and every text pair is pushed until it clears the WCAG floor --
so the dock reads on QGIS's light theme and on its dark one, and a theme switch does
not need a second stylesheet.

Qt is imported inside the functions on purpose.  ``ui/dock.py`` is the one module
that may import Qt at module level; this one stays out of a bare interpreter's
import graph even if it is imported directly (``tests/test_package.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Template
from typing import Any

# WCAG 2.1 的两个底线：正文与次要文字 / the WCAG 2.1 floors: body and secondary text,
# 然后是大字号和状态胶囊 / then large text and the status capsule.
BODY = 4.5
LARGE = 3.0

# 状态胶囊说的话 / what the status capsule says. ``empty`` 是注册表里什么都没有 /
# 空注册表 / ``empty`` is the registry with nothing in it.
STATE_LABELS = {
    "empty": "无操作",
    "idle": "就绪",
    "running": "运行中",
    "ok": "完成",
    "failed": "失败",
    "cancelled": "已取消",
}

# 每个语义色调的色相，单位是度 / the hue of each semantic tone, in degrees.
# 色相带着含义，明度由调色板定 / the hue carries the meaning, the palette decides the
# lightness, 这才让深色主题成立 / which is what makes a dark theme work.
_HUES = {"ok": 145.0, "warn": 38.0, "danger": 358.0}

# 整个坞窗的样式 / the whole dock's styling. ``$tokens`` 由 Theme 填写 /
# ``$tokens`` are filled from a Theme, 花括号留给 Qt / braces stay Qt's.
_SHEET = Template(
    """
QDockWidget, QWidget#dockBody { background: $surface; }
QWidget#dockBody { color: $text; }

QLabel { color: $text; font-size: $bodypt; }
QLabel#meta { color: $muted; font-family: "$mono"; font-size: $smallpt; }
QLabel#summary { color: $muted; font-size: $bodypt; }
QLabel#fieldLabel { color: $muted; font-size: $bodypt; }
QLabel#resultSummary { color: $muted; font-family: "$mono"; font-size: $smallpt; }
QLabel#emptyTitle { color: $text; font-size: $titlept; font-weight: 600; }
QLabel#emptyBody { color: $muted; font-size: $bodypt; }
QLabel#capsule { border-radius: 9px; padding: 2px 10px; font-size: $smallpt; font-weight: 600; }
QLabel#errorCode {
    color: $banner_code; font-family: "$mono"; font-size: $smallpt; font-weight: 600;
}
QLabel#errorMessage { color: $banner_text; font-size: $bodypt; }
QLabel#errorHint { color: $banner_muted; font-size: $smallpt; }
QWidget#banner { background: $banner; border-radius: 6px; }

QComboBox, QLineEdit {
    background: $field; color: $text;
    border: 1px solid $border; border-radius: 4px;
    padding: 4px 26px 4px 8px; min-height: 22px;
    selection-background-color: $accent; selection-color: $on_accent;
}
QLineEdit { padding: 4px 8px; }
QComboBox::drop-down { border: none; width: 26px; }
QComboBox::down-arrow { image: none; }
QComboBox:hover, QLineEdit:hover { border-color: $muted; }
QComboBox:focus, QLineEdit:focus { border-color: $accent; }
QComboBox:disabled, QLineEdit:disabled {
    /* Disabled here means "cannot be changed while it runs", not "cannot be read":
       the submitted value stays at full text colour; only the field's skin dims. */
    color: $text; border-color: $border; background: $hover;
}
QComboBox QAbstractItemView {
    background: $field; color: $text; border: 1px solid $border;
    selection-background-color: $accent; selection-color: $on_accent;
    outline: 0;
}
QComboBox QAbstractItemView::item { min-height: 24px; padding: 0 4px; }

QCheckBox { color: $text; font-size: $bodypt; spacing: 6px; }

QPushButton { font-size: $bodypt; border-radius: 4px; padding: 6px 14px; min-height: 16px; }
QPushButton#primary {
    background: $accent; color: $on_accent; border: 1px solid $accent; font-weight: 600;
}
QPushButton#primary:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton#primary:pressed { background: $accent_pressed; border-color: $accent_pressed; }
QPushButton#primary:focus { border: 1px solid $on_accent; }
QPushButton#primary:disabled { background: $hover; color: $disabled; border-color: $border; }
QPushButton#secondary { background: transparent; color: $text; border: 1px solid $border; }
QPushButton#secondary:hover { background: $hover; border-color: $muted; }
QPushButton#secondary:pressed { background: $border; }
QPushButton#secondary:focus { border-color: $accent; }
QPushButton#secondary:disabled { color: $disabled; border-color: $border; background: transparent; }
QPushButton#text {
    background: transparent; border: 1px solid transparent; color: $text; padding: 6px 8px;
}
QPushButton#text:hover { color: $text; background: $hover; }
QPushButton#text:focus { border-color: $accent; color: $text; }
QPushButton#text:disabled { color: $disabled; background: transparent; }

QProgressBar#progress {
    background: $hover; border: none; border-radius: 2px; min-height: 4px; max-height: 4px;
}
QProgressBar#progress::chunk { background: $accent; border-radius: 2px; }

QScrollArea#params, QScrollArea#params > QWidget > QWidget {
    background: transparent; border: none;
}
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: $border; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: $muted; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: $border; border-radius: 5px; min-width: 24px; }
QScrollBar::handle:horizontal:hover { background: $muted; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }

QPlainTextEdit#result {
    background: $field; color: $text; border: 1px solid $border; border-radius: 4px;
    font-family: "$mono"; font-size: $smallpt; padding: 8px 10px;
    selection-background-color: $accent; selection-color: $on_accent;
}
QPlainTextEdit#result:focus { border-color: $accent; }

QToolTip { background: $field; color: $text; border: 1px solid $border; padding: 4px 6px; }
"""
)


def relative_luminance(color: Any) -> float:
    """不透明 QColor 的 WCAG 2.1 相对亮度。

    WCAG 2.1 relative luminance of an opaque QColor.
    """

    def channel(value: int) -> float:
        linear = value / 255
        return linear / 12.92 if linear <= 0.03928 else ((linear + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(color.red())
        + 0.7152 * channel(color.green())
        + 0.0722 * channel(color.blue())
    )


def contrast(first: Any, second: Any) -> float:
    """两个不透明 QColor 之间的 WCAG 2.1 对比度。

    WCAG 2.1 contrast ratio between two opaque QColors.
    """

    high, low = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def readable(color: Any, background: Any, floor: float = BODY) -> Any:
    """把 ``color`` 从 ``background`` 推开，直到它过 ``floor``。

    只有明度在动，所以语义色相仍然认得出：浅色主题要同一个红的深版，深色主题要浅版。

    Only lightness moves, so a semantic hue stays recognisable: a light theme needs
    the darker version of the same red, a dark theme the lighter one.
    """

    from qgis.PyQt.QtGui import QColor

    result = QColor(color)
    toward_dark = relative_luminance(background) > 0.18
    for _ in range(48):
        if contrast(result, background) >= floor:
            return result
        result = result.darker(112) if toward_dark else result.lighter(112)
    return result


def mix(base: Any, tint: Any, ratio: float) -> Any:
    """``base`` 向 ``tint`` 混合 ``ratio`` 那么多——表面就是这样染上色调的。

    ``base`` blended ``ratio`` of the way to ``tint`` -- how a surface gets a tone.
    """

    from qgis.PyQt.QtGui import QColor

    return QColor(
        round(base.red() * (1 - ratio) + tint.red() * ratio),
        round(base.green() * (1 - ratio) + tint.green() * ratio),
        round(base.blue() * (1 - ratio) + tint.blue() * ratio),
    )


def accent_for(text: Any, accent: Any) -> Any:
    """把 ``accent`` 推到调色板自己的高亮文字能在上面读清。

    浅色主题的高亮色常常太浅，白色文字过不了 4.5:1。移动按钮背景能保住平台本来的配对；改去调暗
    文字，会把每个主按钮的文字变成接近黑色。

    A light theme's highlight is often too light for white text at 4.5:1.  Moving
    the button's background keeps the pairing the platform intended; darkening the
    label instead would turn every primary button's text near-black.
    """

    from qgis.PyQt.QtGui import QColor

    result = QColor(accent)
    toward_dark = relative_luminance(text) > 0.5
    for _ in range(48):
        if contrast(text, result) >= BODY:
            return result
        result = result.darker(112) if toward_dark else result.lighter(112)
    return result


@dataclass(frozen=True)
class Theme:
    """一个调色板对应的颜色与字号；由 :func:`build` 建出来。

    One palette's worth of colour and type; built by :func:`build`.
    """

    surface: Any
    field: Any
    text: Any
    muted: Any
    border: Any
    hover: Any
    disabled: Any
    accent: Any
    on_accent: Any
    accent_hover: Any
    accent_pressed: Any
    ok: Any
    warn: Any
    danger: Any
    mono: str
    small: int
    body: int
    title: int

    def capsule(self, state: str) -> tuple[Any, Any]:
        """一个状态胶囊的前景色和背景色。

        Foreground and background for the status capsule of one state.
        """

        tone = {
            "running": self.accent,
            "ok": self.ok,
            "failed": self.danger,
            "cancelled": self.warn,
        }.get(state)
        if tone is None:
            background = mix(self.surface, self.text, 0.07)
            return readable(self.muted, background), background
        background = mix(self.surface, tone, 0.16)
        return readable(tone, background), background

    def banner(self) -> tuple[Any, Any, Any, Any]:
        """错误面板：它的背景色，然后是量在它上面的三种颜色。

        The error panel: its background, then the three colours measured on it.
        """

        background = mix(self.surface, self.danger, 0.10)
        return (
            background,
            readable(self.text, background),
            readable(self.muted, background),
            readable(self.danger, background),
        )

    def pairs(self) -> dict[str, tuple[Any, Any, float]]:
        """QSS 画出来的每一对「文字在背景上」，连同它的底线。

        这里只列样式表规则真的设过的颜色；Qt 在样式表背后画的那两处——行编辑提示和记录占位
        符——是在 ``test_ui_dock`` 里按渲染出来的像素量的，因为断言一个 Qt 根本不用的调色板
        角色什么都证明不了。

        Only colours a stylesheet rule sets appear here; the two places Qt paints
        behind the stylesheet's back -- the line-edit hint and the record
        placeholder -- are measured from rendered pixels in ``test_ui_dock``
        instead, because asserting a palette role Qt ignores proved nothing.
        """

        pairs = {
            "text on surface": (self.text, self.surface, BODY),
            "muted on surface": (self.muted, self.surface, BODY),
            "text on field": (self.text, self.field, BODY),
            "text on disabled field": (self.text, self.hover, BODY),
            "label on accent": (self.on_accent, self.accent, BODY),
            "label on accent hover": (self.on_accent, self.accent_hover, BODY),
            "label on accent pressed": (self.on_accent, self.accent_pressed, BODY),
        }
        background, text, muted, code = self.banner()
        pairs.update(
            {
                "text on banner": (text, background, BODY),
                "muted on banner": (muted, background, BODY),
                "code on banner": (code, background, BODY),
            }
        )
        for state in ("idle", "running", "ok", "failed", "cancelled"):
            foreground, capsule = self.capsule(state)
            pairs[f"capsule {state}"] = (foreground, capsule, LARGE)
        return pairs

    def stylesheet(self) -> str:
        """坞窗的 QSS，每个颜色都取自已主题。

        The dock's QSS, with every colour taken from this theme.
        """

        background, text, muted, code = self.banner()
        tokens = {
            "surface": self.surface.name(),
            "field": self.field.name(),
            "text": self.text.name(),
            "muted": self.muted.name(),
            "border": self.border.name(),
            "hover": self.hover.name(),
            "disabled": self.disabled.name(),
            "accent": self.accent.name(),
            "accent_hover": self.accent_hover.name(),
            "accent_pressed": self.accent_pressed.name(),
            "on_accent": self.on_accent.name(),
            "banner": background.name(),
            "banner_text": text.name(),
            "banner_muted": muted.name(),
            "banner_code": code.name(),
            "mono": self.mono,
            "smallpt": f"{self.small}pt",
            "bodypt": f"{self.body}pt",
            "titlept": f"{self.title}pt",
        }
        return _SHEET.substitute(tokens)


def build(palette: Any) -> Theme:
    """``palette`` 对应的坞窗主题——通常就是 ``QApplication.palette()``。

    The dock's theme for ``palette`` -- normally ``QApplication.palette()``.
    """

    from qgis.PyQt.QtGui import QColor, QFontDatabase, QPalette
    from qgis.PyQt.QtWidgets import QApplication

    surface = QColor(palette.color(QPalette.Window))
    field = QColor(palette.color(QPalette.Base))
    text = readable(readable(palette.color(QPalette.WindowText), surface), field)
    muted = readable(readable(mix(surface, text, 0.55), surface), field)
    accent = readable(palette.color(QPalette.Highlight), surface, LARGE)
    on_accent = QColor(palette.color(QPalette.HighlightedText))
    accent = accent_for(on_accent, accent)
    # 两个按钮状态背离标签颜色，所以对比度只会升不会降 / the two button states step away
    # from the label colour, so their contrast can only rise. （把填充推向文字，正是浅色主题
    # hover 填充落到 4.31:1、静止填充停在 5.39 的原因 / what put the hover fill at 4.31:1
    # 而静止填充是 5.39 / while the resting fill sat at 5.39.
    toward_dark = relative_luminance(on_accent) > 0.5
    accent_hover = accent.darker(112) if toward_dark else accent.lighter(112)
    accent_pressed = accent.darker(125) if toward_dark else accent.lighter(125)
    tones = {
        name: readable(QColor.fromHslF(hue / 360.0, 0.62, 0.50), surface)
        for name, hue in _HUES.items()
    }
    point = QApplication.font().pointSize()
    body = point if point > 0 else 9
    return Theme(
        surface=surface,
        field=field,
        text=text,
        muted=muted,
        border=mix(surface, text, 0.16),
        hover=mix(surface, text, 0.06),
        disabled=mix(surface, text, 0.35),
        accent=accent,
        on_accent=on_accent,
        accent_hover=accent_hover,
        accent_pressed=accent_pressed,
        ok=tones["ok"],
        warn=tones["warn"],
        danger=tones["danger"],
        mono=QFontDatabase.systemFont(QFontDatabase.FixedFont).family(),
        small=max(7, body - 1),
        body=body,
        title=body + 4,
    )
