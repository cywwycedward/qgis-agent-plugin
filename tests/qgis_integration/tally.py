"""运行末尾打印的汇总：只按行的字段计数，绝不数它的文本。

结论是行的一个字段，所以汇总只能由运行器真的判出来的东西拼成。证据是自由文本，允许出现 ``PASS``、
``FAIL``、``SKIP`` 这些词——句子里的错误码不是结论——按打印行去搜索它们，等于让文本决定汇总，连
退出码也一起被决定。

The tally a run prints, counted from the rows and never from their text.  The verdict is a
field of the row, so the summary can only be built from what the runner decided.  The evidence
is free text and may legitimately contain the words ``PASS``, ``FAIL`` or ``SKIP`` -- an error
code inside a sentence is not a verdict -- and counting them by searching the printed line
would let the text choose the tally, including the run's exit code.
"""

from __future__ import annotations

from dataclasses import dataclass

VERDICTS = ("PASS", "FAIL", "SKIP")


@dataclass(frozen=True)
class Row:
    """一条用例的结果：结论是一个字段，不是要靠解析文本反推的东西。

    One case's outcome: the verdict is a field, not something to be parsed back.
    """

    case_id: str
    verdict: str
    evidence: str

    def line(self) -> str:
        return f"{self.case_id} {self.verdict} {self.evidence}"


def counts(rows: list[Row]) -> dict[str, int]:
    """每种结论各有多少行；第四种词会被拒，而不是被丢掉。

    How many rows of each verdict; a fourth word is refused, not dropped.
    """

    counted: dict[str, int] = dict.fromkeys(VERDICTS, 0)
    for row in rows:
        if row.verdict not in counted:
            raise ValueError(f"unknown verdict: {row.verdict!r}")
        counted[row.verdict] += 1
    return counted


def summarise(rows: list[Row]) -> str:
    counted = counts(rows)
    parts = ", ".join(f"{verdict} {counted[verdict]}" for verdict in VERDICTS)
    return f"{parts} of {len(rows)} cases"


def failed(rows: list[Row]) -> bool:
    return counts(rows)["FAIL"] > 0
