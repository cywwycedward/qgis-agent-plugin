"""文档之间的指路关系由这个文件守。

规范集中在一个目录，入口文件只做指针。索引漏一份规范，agent 就读不到它；还指着一份已被吸收的旧
文档，agent 就读到不存在的规则。两种错都是纯文本问题，所以可以当场断言。

The pointer structure between the documents, asserted: the entry file indexes every standard in
the standards folder, and no document still points at one that was absorbed and deleted.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest  # pyright: ignore[reportMissingImports]

# 这一组是成文规则本身，每次改完代码都要跑。
# This group is the written rules themselves; it runs after every change.
pytestmark = pytest.mark.conformance

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "AGENTS.md"
STANDARDS = ("DEVELOP.md", "TEST.md", "ENVIRONMENT.md", "DELEGATION.md")
REMOVED = (
    "OPERATIONS.md",
    "ERRORS.md",
    "PROCESSING_DESIGN",
    "PROCESSING_PARAMETERS",
    "qgis-sync-async",
)

LINK = re.compile(r"\]\(([^)\s]+)\)")
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def markdown_files() -> list[Path]:
    """Every markdown file that ships: the entry file plus both documentation folders.

    入库的全部 Markdown：入口文件与两个文档目录。
    """
    found = [AGENTS, *(ROOT / "docs").glob("*.md"), *(ROOT / "docs" / "agents").glob("*.md")]
    return sorted(path for path in found if path.is_file())


def test_every_standard_lives_in_docs_agents() -> None:
    """四份规范都在规范目录里，且入口文件逐个引用它们。

    Every standard sits in the standards folder and the entry file points at it.
    """
    index = AGENTS.read_text(encoding="utf-8-sig")
    missing = [name for name in STANDARDS if not (ROOT / "docs" / "agents" / name).is_file()]
    unindexed = [name for name in STANDARDS if f"docs/agents/{name}" not in index]
    assert not missing, "standards must live in docs/agents: " + ", ".join(missing)
    assert not unindexed, "AGENTS.md does not point at: " + ", ".join(unindexed)


def test_no_document_points_at_an_absorbed_document() -> None:
    """被吸收的旧文档已经删掉，任何 Markdown 里都不该再出现它们的名字。

    The absorbed documents are gone; no markdown may still name one.
    """
    offenders: list[str] = []
    for path in markdown_files():
        text = path.read_text(encoding="utf-8-sig")
        for name in REMOVED:
            if name in text:
                offenders.append(f"{path.relative_to(ROOT).as_posix()}: {name}")
    assert not offenders, "these documents no longer exist:\n" + "\n".join(offenders)


def test_relative_markdown_links_resolve() -> None:
    """文档里的相对链接要指到存在的文件；代码示例里的占位链接不算。

    Every relative markdown link resolves; links inside fenced examples are placeholders.
    """
    offenders: list[str] = []
    for path in markdown_files():
        text = FENCE.sub("", path.read_text(encoding="utf-8-sig"))
        for target in LINK.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            resolved = (path.parent / target.split("#")[0]).resolve()
            if not resolved.exists():
                offenders.append(f"{path.relative_to(ROOT).as_posix()}: {target}")
    assert not offenders, "broken relative links:\n" + "\n".join(offenders)
