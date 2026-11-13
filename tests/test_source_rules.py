"""源码级硬规则：导入位置、`__future__`、日志器、主题配色。

这里把四类只能靠人盯着的规则变成断言。它与裸解释器守卫互为补集：守卫证明"真的能加载"，本文件
证明"没人把 import 放错位置"——前者看不见 `if` 块里的模块级 import，后者的正则也分不清缩进层级。

Four rules that were prose until now: import placement, the future import, the logger name and the
colour source.  The bare-interpreter guard proves the package loads; these assertions prove no
import sits in the wrong place, which the runtime check cannot see and a regex cannot tell apart.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PACKAGE = SRC / "qgis_agent_plugin"

# 模块级 Qt 只许可这些位置，且只能来自 `qgis.PyQt`。
# Module-level Qt is allowed only here, and only from `qgis.PyQt`.
QT_AT_MODULE_LEVEL = {
    "qgis_agent_plugin/plugin.py",
    "qgis_agent_plugin/ui/dock.py",
}
QT_AT_MODULE_LEVEL_PREFIXES = ("qgis_agent_plugin/core/runners/",)

QT_PACKAGES = ("PyQt5", "PyQt6", "PySide2", "PySide6")


def python_files(root: Path) -> list[Path]:
    """Every module under one root, newest first excluded.

    一个目录下的全部模块。
    """
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def relative(path: Path) -> str:
    """The package-relative name used by the allow-lists.

    允许清单里用的包内相对名。
    """
    return path.relative_to(SRC).as_posix()


def body_statements(tree: ast.Module) -> list[ast.stmt]:
    """Statements that run at import time: everything outside a function body.

    导入时就会执行的语句：函数体以外的一切（类体、`if`、`try` 里的也算）。
    """
    found: list[ast.stmt] = []

    def walk(statements: list[ast.stmt]) -> None:
        for statement in statements:
            found.append(statement)
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for field in ("body", "orelse", "finalbody", "handlers"):
                nested = getattr(statement, field, None)
                if isinstance(nested, list) and nested and isinstance(nested[0], ast.stmt):
                    walk(nested)
            for handler in getattr(statement, "handlers", []):
                walk(handler.body)

    walk(tree.body)
    return found


def imported_names(statement: ast.stmt) -> list[str]:
    """The dotted names an `import` or `from ... import` statement pulls in.

    一条 import 语句真正引入的点分名。
    """
    if isinstance(statement, ast.Import):
        return [alias.name for alias in statement.names]
    if isinstance(statement, ast.ImportFrom) and statement.module:
        return [statement.module]
    return []


def docstring_only(path: Path) -> bool:
    """A module that is just a docstring has nothing to put behind a future import.

    只有 docstring 的模块没有需要延迟注解的代码。
    """
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    statements = [
        statement
        for statement in tree.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
    ]
    return not statements


def has_future_annotations(tree: ast.Module) -> bool:
    """`from __future__ import annotations` at the top of the module.

    模块顶部有 `from __future__ import annotations`。
    """
    return any(
        isinstance(statement, ast.ImportFrom) and statement.module == "__future__"
        for statement in tree.body
    )


def test_every_module_with_code_starts_with_the_future_import() -> None:
    """有代码的模块都以 `from __future__ import annotations` 开头。

    Every module with code postpones its annotations.
    """
    missing = [
        path.relative_to(ROOT).as_posix()
        for path in [*python_files(SRC), *python_files(ROOT / "tests")]
        if not docstring_only(path)
        and not has_future_annotations(ast.parse(path.read_text(encoding="utf-8-sig")))
    ]
    assert not missing, "add `from __future__ import annotations` to: " + ", ".join(missing)


def test_no_qgis_import_runs_at_import_time() -> None:
    """QGIS 只在函数体内 import，模块级一条都不许有。

    QGIS is imported inside the function that uses it, never at import time.
    """
    offenders: list[str] = []
    for path in python_files(PACKAGE):
        name = relative(path)
        for statement in body_statements(ast.parse(path.read_text(encoding="utf-8-sig"))):
            for module in imported_names(statement):
                if not (module == "qgis" or module.startswith("qgis.")):
                    continue
                if module.startswith("qgis.PyQt") and (
                    name in QT_AT_MODULE_LEVEL or name.startswith(QT_AT_MODULE_LEVEL_PREFIXES)
                ):
                    continue
                offenders.append(f"{name}:{statement.lineno}: {module}")
    assert not offenders, "QGIS belongs inside the function that uses it:\n" + "\n".join(offenders)


def test_qt_is_never_imported_around_qgis_pyqt() -> None:
    """Qt 只能经 `qgis.PyQt` 出现，直接 import PyQt5/PySide 会绕过 QGIS 的绑定。

    Qt arrives through `qgis.PyQt` only; importing a binding directly bypasses QGIS.
    """
    offenders = [
        f"{relative(path)}:{statement.lineno}"
        for path in python_files(PACKAGE)
        for statement in body_statements(ast.parse(path.read_text(encoding="utf-8-sig")))
        for module in imported_names(statement)
        if module.split(".")[0] in QT_PACKAGES
    ]
    assert not offenders, "import Qt through qgis.PyQt, not directly:\n" + "\n".join(offenders)


def test_the_theme_writes_no_hex_colour() -> None:
    """配色只有一个来源（palette 的角色），任何地方都不写字面十六进制颜色。

    Colours come from palette roles, never from a hex literal.
    """
    import re

    hex_colour = re.compile(r"#[0-9a-fA-F]{3}\b|#[0-9a-fA-F]{6}\b")
    offenders: list[str] = []
    for path in python_files(PACKAGE / "ui"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                match = hex_colour.search(node.value)
                if match:
                    offenders.append(f"{relative(path)}:{node.lineno}: {match.group(0)}")
    assert not offenders, "derive colours from the palette instead:\n" + "\n".join(offenders)


def test_a_module_logger_is_named_and_uses_its_module_name() -> None:
    """会打日志的模块用 `_LOGGER = logging.getLogger(__name__)`。

    A logger is named `_LOGGER` and carries the module name, so log lines stay traceable.
    """
    offenders: list[str] = []
    for path in python_files(SRC):
        for statement in body_statements(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if not isinstance(statement, ast.Assign):
                continue
            value = statement.value
            if not (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "getLogger"
            ):
                continue
            target = statement.targets[0]
            named = isinstance(target, ast.Name) and target.id == "_LOGGER"
            argument = value.args[0] if value.args else None
            from_name = isinstance(argument, ast.Name) and argument.id == "__name__"
            if not (named and from_name):
                offenders.append(f"{relative(path)}:{statement.lineno}")
    assert not offenders, "use `_LOGGER = logging.getLogger(__name__)` in: " + ", ".join(offenders)
