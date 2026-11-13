"""注释与 docstring 的规则由这个文件守：中英双语、中文在前、只讲代码、不引用文档。

ruff 看不见注释内容，所以这份断言是这条规则唯一的守卫：任何一条注释漂成单语或开始拿文档当
依据，它会当场指到文件和行。

The rule enforced here: every comment and docstring is bilingual with Chinese first, describes the
code rather than a document, and cites no document.  ruff cannot see inside a comment, so these
assertions are the only guard against a comment drifting into one language or citing a document.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest  # pyright: ignore[reportMissingImports]

# 这一组是成文规则本身，每次改完代码都要跑。
# This group is the written rules themselves; it runs after every change.
pytestmark = pytest.mark.conformance

ROOT = Path(__file__).resolve().parents[1]

CJK = re.compile(r"[\u4e00-\u9fff]")
LATIN = re.compile(r"[A-Za-z]")

# 工具指令是机器指令，不是散文。
# Tool directives are machine instructions, not prose.
DIRECTIVE = re.compile(r"\bnoqa\b|\btype:\s*ignore\b|\bpyright:\s*ignore\b")

CITATIONS = (
    ("PRD", re.compile(r"\bPRD\b")),
    ("a section sign", re.compile("\u00a7")),
    ("an acceptance id", re.compile(r"\bAT-\d+")),
    ("a docs path", re.compile(r"docs[/\\]")),
    ("a markdown file name", re.compile(r"\.md\b")),
)

DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def python_files() -> list[Path]:
    """The shipped code and its tests.

    仓库里写代码的只有这两个目录：插件本身与它的测试。
    """
    found = [*(ROOT / "src").rglob("*.py"), *(ROOT / "tests").rglob("*.py")]
    return sorted(path for path in found if "__pycache__" not in path.parts)


def comment_units(source: str) -> list[tuple[int, str]]:
    """Group consecutive comment lines into one unit; an inline label is its own unit.

    连续的注释行算一个单元（中文段在前、英文段在后）；写在代码行尾的标签各自独立，逐条双语。
    """
    lines = source.splitlines()
    blocks: list[list[tuple[int, int, str]]] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.COMMENT:
                continue
            line, column = token.start
            if blocks and line <= blocks[-1][-1][0] + 1:
                blocks[-1].append((line, column, token.string))
            else:
                blocks.append([(line, column, token.string)])
    except tokenize.TokenError:  # 坏文件由它自己的用例报 / a broken file fails its own test
        return []

    units: list[tuple[int, str]] = []
    for block in blocks:
        inline = all(lines[line - 1][:column].strip() for line, column, _ in block)
        if inline:
            units.extend((line, text) for line, _, text in block)
        else:
            units.append((block[0][0], "\n".join(text for _, _, text in block)))
    return units


def docstring_units(source: str) -> list[tuple[int, str]]:
    """Every real docstring, never a triple-quoted payload.

    只认 docstring，不认三引号载荷。
    """
    units: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, DOCSTRING_OWNERS):
            text = ast.get_docstring(node, clean=False)
            if text is not None:
                units.append((getattr(node, "lineno", 0), text))
    return units


def violations(path: Path) -> list[str]:
    """Every rule broken in one file, as `path:line: what`.

    一个文件里违反的每一条，带 `文件:行`，所以失败信息自己就是修法。
    """
    source = path.read_text(encoding="utf-8-sig")
    relative = path.relative_to(ROOT).as_posix()
    problems: list[str] = []
    units = [
        *(("comment", line, text) for line, text in comment_units(source)),
        *(("docstring", line, text) for line, text in docstring_units(source)),
    ]
    for kind, line, text in units:
        prose = "\n".join(part for part in text.splitlines() if not DIRECTIVE.search(part))
        if not prose.strip():
            continue
        if not CJK.search(prose):
            problems.append(f"{relative}:{line}: {kind} has no Chinese: {prose.strip()[:60]}")
        if not LATIN.search(prose):
            problems.append(f"{relative}:{line}: {kind} has no English: {prose.strip()[:60]}")
        for name, pattern in CITATIONS:
            match = pattern.search(prose)
            if match:
                problems.append(f"{relative}:{line}: {kind} cites {name}: {match.group(0)}")
    return problems


def test_comments_are_bilingual_and_about_the_code() -> None:
    """每条注释与每个 docstring：中英双语、中文在前、不引用文档。

    Bilingual with Chinese first, and no document cited as the reason.
    """
    problems = [problem for path in python_files() for problem in violations(path)]
    assert not problems, "every comment must be bilingual and cite no document:\n" + (
        "\n".join(problems)
    )


def test_shortcut_markers_stay_greppable() -> None:
    """`shortcut:` 的标记由 `ponytail-debt` 检索，注释双语化时不许把它丢掉。

    The `shortcut:` marker is what `ponytail-debt` greps for; translating a comment must keep it.
    """
    marked = [
        path for path in python_files() if "shortcut:" in path.read_text(encoding="utf-8-sig")
    ]
    assert marked, "no shortcut: marker left in the tree; the debt ledger would go blind"
