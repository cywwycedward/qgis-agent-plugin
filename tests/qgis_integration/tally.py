"""The tally a run prints, counted from the rows and never from their text.

A row carries its verdict as a field, so the summary can only be built from what the
runner decided.  The evidence is free text and may legitimately contain the words
``PASS``, ``FAIL`` or ``SKIP`` -- an error code in a sentence is not a verdict -- and
counting them by searching the printed line would let the text choose the tally,
including the run's exit code.
"""

from __future__ import annotations

from dataclasses import dataclass

VERDICTS = ("PASS", "FAIL", "SKIP")


@dataclass(frozen=True)
class Row:
    """One case's outcome: the verdict is a field, not something to be parsed back."""

    case_id: str
    verdict: str
    evidence: str

    def line(self) -> str:
        return f"{self.case_id} {self.verdict} {self.evidence}"


def counts(rows: list[Row]) -> dict[str, int]:
    """How many rows of each verdict; a fourth word is refused, not dropped."""

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
