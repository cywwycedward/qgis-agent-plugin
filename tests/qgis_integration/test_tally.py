"""运行的汇总只数结论，绝不数打印出来的文本。

汇总不能从打印行里去找字样：那样自由形式的证据就能改掉汇总。这些测试守的就是这一点：第二条是
一个通过的行，它的证据里带着 ``FAIL`` 和 ``SKIP``。

The run's summary is counted from the verdicts, never from the printed text.  Counting a marker
inside the printed line lets free-form evidence change the tally, so these tests guard against it:
the second one is a pass whose evidence contains ``FAIL`` and ``SKIP``.
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
    # 这行是通过的，它的文本里却两次出现 FAIL、一次 SKIP，真实证据行就是这样（一个错误码、一句
    # 关于跳过的话）。
    # This row passed; its text says FAIL twice and SKIP once, as real evidence lines do (an error
    # code, a sentence about a skipped half).
    # 数子串会把它算成失败，于是绿色运行的退出码变成 1。
    # A substring count would report it as a failure and turn a green run's exit code into 1.
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
