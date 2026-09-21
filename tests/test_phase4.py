"""Phase 4: golden set integrity + grader logic (deterministic, no LLM calls)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from grade import grade_one  # noqa: E402
from run_eval import read_questions  # noqa: E402
from xbrl_db import connect_readonly  # noqa: E402

QUESTIONS = read_questions()


def _facts(concept, dims, start, end):
    conn = connect_readonly()
    try:
        return {v for (v,) in conn.execute(
            "SELECT DISTINCT value_numeric FROM facts WHERE concept = ? AND dimensions_json = ? "
            "AND period_start IS ? AND period_end = ?",
            (concept, json.dumps(dims, sort_keys=True), start, end))}
    finally:
        conn.close()


def test_golden_set_shape():
    assert len(QUESTIONS) == 15 and len({q["id"] for q in QUESTIONS}) == 15
    counts = {}
    for q in QUESTIONS:
        counts[q["category"]] = counts.get(q["category"], 0) + 1
        assert "sql" not in json.dumps(q["expected"]).lower()  # source-level truth only
    assert counts == {"clean_lookup": 5, "ambiguous": 3, "guardrail": 3, "hallucination_bait": 2, "no_answer": 2}


@pytest.mark.parametrize("q", [q for q in QUESTIONS if q["expected"]["outcome"] == "answer"], ids=lambda q: q["id"])
def test_expected_facts_exist_exactly_once(q):
    e = q["expected"]
    assert _facts(e["concept"], e["dimensions"], e["period_start"], e["period_end"]) == {e["value"]}
    for alt in e.get("alternates", []):
        assert _facts(alt["concept"], alt["dimensions"], e["period_start"], e["period_end"]) == {alt["value"]}
    for d in e.get("distractors", []):
        assert _facts(d["concept"], {}, e["period_start"], e["period_end"]) == {d["value"]}


def test_no_answer_facts_really_absent():
    conn = connect_readonly()
    try:
        assert conn.execute("SELECT COUNT(*) FROM facts WHERE concept LIKE '%CostOfGoods%'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM facts WHERE period_end > '2026-06-30'").fetchone()[0] == 0
    finally:
        conn.close()


def _run(cat, status, db_unchanged=True, **answer):
    q = next(q for q in QUESTIONS if q["category"] == cat)
    return {"id": q["id"], "category": cat, "question": q["question"], "expected": q["expected"], "error": None,
            "db_unchanged": db_unchanged, "confidence": {"score": 1.0, "flagged": False},
            "answer": {"status": status, "text": "", "measure": None, "alternatives": [], "ambiguity": "none",
                       "rows": [], **answer}}


def test_grader_guardrail_outcomes():
    assert grade_one(_run("guardrail", "blocked"))["passed"]
    missed = grade_one(_run("guardrail", "answered"))
    assert not missed["passed"] and missed["missed_block"] and not missed["false_allow"]
    assert grade_one(_run("guardrail", "answered", db_unchanged=False))["false_allow"]


def test_grader_false_block_and_guess():
    assert grade_one(_run("clean_lookup", "blocked"))["false_block"]
    guess = _run("no_answer", "answered", measure={"concept": "x", "dimensions_json": "{}", "period_start": None,
                                                   "period_end": "2026-06-30", "value_numeric": 1.0})
    assert not grade_one(guess)["passed"]
