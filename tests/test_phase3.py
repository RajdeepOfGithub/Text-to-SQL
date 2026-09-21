"""Phase 3: back-translation, sanity checks, confidence.

`llm` tests call gpt-4o-mini; the rest are deterministic.
"""
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from back_translate import BackTranslationResult, Judgment, period_verdict  # noqa: E402
from confidence import assess  # noqa: E402
from eval_confidence import COLS, COMMON, Q_REP, Q_REV, fixed, lookup  # noqa: E402
from sanity_check import check_magnitude, check_unit_scale, parse_stated_figure  # noqa: E402
from sql_generate import Answer, answer_question  # noqa: E402

load_dotenv(ROOT / ".env")
llm = pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="needs OPENAI_API_KEY")

REVENUE_SQL = lookup("us-gaap:RevenuesNetOfInterestExpense", "{}", "2026-04-01", "2026-06-30")
EXPENSE_SQL = lookup("us-gaap:NoninterestExpense", "{}", "2026-04-01", "2026-06-30")
REV = ("us-gaap:RevenuesNetOfInterestExpense", "{}", "iso4217:USD", "2026-04-01", "2026-06-30")


def always_match(question, sql, columns, rows):
    j = Judgment(original_metric="m", original_period="p", original_scope="s", back_translated_metric="m",
                 back_translated_period="p", back_translated_scope="s",
                 metric="match", period="match", scope="match", explanation="stub")
    return BackTranslationResult(question, "stub", j)


# ------------------------------------------------------------ required cases (LLM)

@llm
def test_deliberate_mismatch_revenue_question_expense_sql_is_flagged():
    a = answer_question(Q_REV, planner=fixed(EXPENSE_SQL))
    assert a.status == "answered" and a.measure.concept == "us-gaap:NoninterestExpense"
    r = assess(a)
    assert r.flagged
    assert r.components["back_translation"] == 0.0
    assert "metric" in r.back_translation.mismatched_aspects, r.back_translation.judgment
    assert r.score < 0.7
    # sanity checks alone would not catch this: it is a real, plausible expense figure
    assert not r.sanity.hard_failures


@llm
def test_repurchase_disclosure_is_not_a_hallucination_flag():
    a = answer_question(Q_REP)
    assert a.status == "answered" and a.ambiguity == "picked_headline" and a.alternatives
    r = assess(a)
    assert not r.flagged, r.reasons
    assert r.score >= 0.9
    # and the treasury-stock variant (the other valid reading) is equally not a hallucination
    r2 = assess(answer_question(Q_REP, planner=fixed(
        lookup("us-gaap:TreasuryStockValueAcquiredCostMethod", COMMON, "2025-01-01", "2025-12-31"))))
    assert not r2.flagged, r2.reasons


@llm
def test_clean_pass_revenue_high_confidence():
    a = answer_question(Q_REV)
    assert a.measure.value_numeric == 57_347_000_000
    r = assess(a)
    assert not r.flagged, r.reasons
    assert r.score >= 0.9
    assert r.back_translation.matches and r.back_translation.period_source == "code"


# ------------------------------------------------------------ deterministic

def test_confidence_ignores_ambiguity_and_alternatives():
    a = answer_question(Q_REV, planner=fixed(REVENUE_SQL))
    disclosed = replace(a, ambiguity="picked_headline", alternatives=[a.measure])
    assert assess(a, always_match).score == assess(disclosed, always_match).score == 1.0


def test_scale_error_in_answer_text_is_flagged():
    a = answer_question(Q_REV, planner=fixed(REVENUE_SQL))
    broken = replace(a, text=a.text.replace("$57,347M", "$57,347", 1))
    r = assess(broken, always_match)
    assert r.flagged and any("unit_scale" in x for x in r.reasons)


def test_empty_result_scores_zero():
    a = answer_question(Q_REV, planner=fixed(lookup("us-gaap:RevenuesNetOfInterestExpense", "{}",
                                                          "1999-01-01", "1999-03-31")))
    r = assess(a, always_match)
    assert a.status == "no_result" and r.score == 0.0 and r.flagged


def test_blocked_answer_not_scored():
    r = assess(Answer(Q_REV, "blocked", "blocked", block_reason="DROP"))
    assert not r.flagged and r.back_translation is None


def test_magnitude_uses_concept_history():
    concept, dims, unit, s, e = REV
    assert check_magnitude(57_347e6, concept, dims, unit, s, e).status == "pass"
    assert check_magnitude(-57_347e6, concept, dims, unit, s, e).status == "fail"   # sign vs history
    assert check_magnitude(57_347.0, concept, dims, unit, s, e).status == "fail"    # absurdly small
    assert check_magnitude(9e15, concept, dims, unit, s, e).status == "fail"        # absurdly large
    # a concept/period class with no other history is neither passed nor failed
    assert check_magnitude(1.0, "us-gaap:NumberOfReportableSegments", "{}", None,
                           "2025-01-01", "2025-12-31").status == "insufficient_history"


def test_unit_scale_parsing():
    assert parse_stated_figure("$57,347M for Q2") == (57_347e6, "iso4217:USD")
    assert parse_stated_figure("$-675M") == (-675e6, "iso4217:USD")
    assert parse_stated_figure("114.4M shares") == (114.4e6, "xbrli:shares")
    assert parse_stated_figure("$5.24 per share") == (5.24, "iso4217:USD/xbrli:shares")
    assert check_unit_scale(114_400_000, "xbrli:shares", "$114.4M").status == "fail"  # shares stated as dollars


def test_period_verdict_in_code():
    cols = COLS.split(", ")
    q2 = [("c", "{}", "2026-04-01", "2026-06-30", 1.0, "iso4217:USD", "d")]
    h1 = [("c", "{}", "2026-01-01", "2026-06-30", 1.0, "iso4217:USD", "d")]
    assert period_verdict(Q_REV, cols, q2) == "match"
    assert period_verdict(Q_REV, cols, h1) == "mismatch"
    assert period_verdict("what was revenue lately?", cols, q2) is None  # falls back to the LLM verdict
