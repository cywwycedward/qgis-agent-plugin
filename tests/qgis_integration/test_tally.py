"""The run's summary is counted from the verdicts, never from the printed text.

The old implementation counted ``" {verdict} "`` occurrences inside the printed line,
so the free-form evidence could change the tally.  These tests fail if that comes
back: the second one is a pass whose evidence contains ``FAIL`` and ``SKIP``.
"""

from __future__ import annotations

import pytest
import tally


def test_the_summary_counts_the_structured_verdicts() -> None:
    rows = [
        tally.Row("AT-01", "PASS", "a clean pass"),
        tally.Row("AT-02", "FAIL", "a real failure"),
        tally.Row("AT-03", "SKIP", "cannot run here"),
    ]
    assert tally.summarise(rows) == "PASS 1, FAIL 1, SKIP 1 of 3 cases"
    assert tally.failed(rows) is True


def test_a_deceptive_evidence_line_cannot_change_the_tally() -> None:
    # This row passed; its text says FAIL twice and SKIP once, as real evidence
    # lines do (an error code, a sentence about a skipped half).  A substring count
    # would report it as a failure and turn a green run's exit code into 1.
    rows = [
        tally.Row(
            "AT-27",
            "PASS",
            "FAILED(PASS) is not a FAIL, and a separate SKIP row is not this row",
        ),
        tally.Row("AT-27-v1", "SKIP", "pydantic 1.x is not installed; do not mark it PASS"),
    ]
    assert tally.summarise(rows) == "PASS 1, FAIL 0, SKIP 1 of 2 cases"
    assert tally.failed(rows) is False


def test_a_verdict_outside_the_three_words_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown verdict"):
        tally.summarise([tally.Row("AT-01", "PASSED", "a fourth word must not vanish")])
