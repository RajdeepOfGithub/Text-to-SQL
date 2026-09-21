"""Does the confidence score separate known-right from known-wrong answers?

Small constructed set, not a benchmark: 5 right, 5 wrong. The wrong cases pair a
question with SQL that answers something else (metric, period, scope), or corrupt
the answer text's scale. Prints per-case results plus the score gap between the
lowest right and the highest wrong. It reports the numbers, it doesn't assert them.

    .venv/Scripts/python scripts/eval_confidence.py
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from confidence import assess  # noqa: E402
from sql_generate import SQLPlan, answer_question  # noqa: E402

COLS = "concept, dimensions_json, period_start, period_end, value_numeric, unit, source_document"
CCB = json.dumps({"srt:ConsolidationItemsAxis": "us-gaap:OperatingSegmentsMember",
                  "us-gaap:StatementBusinessSegmentsAxis": "jpm:ConsumerCommunityBankingMember"}, sort_keys=True)
COMMON = json.dumps({"us-gaap:StatementEquityComponentsAxis": "us-gaap:CommonStockMember"})

Q_REV = "What was total net revenue for Q2 2026?"
Q_REP = "How much did the firm spend on share repurchases in fiscal year 2025?"


def lookup(concept: str, dims: str, start: str | None, end: str) -> str:
    period = "period_start IS NULL" if start is None else f"period_start = '{start}'"
    return (f"SELECT {COLS} FROM facts WHERE concept = '{concept}' AND dimensions_json = '{dims}' "
            f"AND {period} AND period_end = '{end}'")


def fixed(sql: str):
    """Planner stub that returns the given SQL, bypassing the LLM generator."""
    return lambda q, s: SQLPlan(intent="read", reasoning="fixed", sql=sql, target=None, ambiguity="none")


# (case id, expected, question, planner or None for the real LLM pipeline, optional answer-text mutation)
CASES = [
    ("R1 revenue, real pipeline", "right", Q_REV, None, None),
    ("R2 repurchases, real pipeline", "right", Q_REP, None, None),
    ("R3 repurchases, treasury-stock variant", "right", Q_REP,
     fixed(lookup("us-gaap:TreasuryStockValueAcquiredCostMethod", COMMON, "2025-01-01", "2025-12-31")), None),
    ("R4 net income Q2 2026", "right", "What was net income for Q2 2026?",
     fixed(lookup("us-gaap:NetIncomeLoss", "{}", "2026-04-01", "2026-06-30")), None),
    ("R5 total assets 2026-06-30", "right", "What were total assets as of June 30, 2026?",
     fixed(lookup("us-gaap:Assets", "{}", None, "2026-06-30")), None),
    ("W1 metric: expense SQL for revenue question", "wrong", Q_REV,
     fixed(lookup("us-gaap:NoninterestExpense", "{}", "2026-04-01", "2026-06-30")), None),
    ("W2 period: six months instead of Q2", "wrong", Q_REV,
     fixed(lookup("us-gaap:RevenuesNetOfInterestExpense", "{}", "2026-01-01", "2026-06-30")), None),
    ("W3 period: Q2 2025 instead of Q2 2026", "wrong", Q_REV,
     fixed(lookup("us-gaap:RevenuesNetOfInterestExpense", "{}", "2025-04-01", "2025-06-30")), None),
    ("W4 scope: CCB segment instead of firm total", "wrong", Q_REV,
     fixed(lookup("us-gaap:RevenuesNetOfInterestExpense", CCB, "2026-04-01", "2026-06-30")), None),
    ("W5 scale: answer text drops the 'M'", "wrong", Q_REV,
     fixed(lookup("us-gaap:RevenuesNetOfInterestExpense", "{}", "2026-04-01", "2026-06-30")),
     lambda t: t.replace("$57,347M", "$57,347", 1)),
]


def run_case(question, planner, mutate):
    a = answer_question(question) if planner is None else answer_question(question, planner=planner)
    if mutate:
        a = replace(a, text=mutate(a.text))
    return a, assess(a)


def main():
    results = []
    for cid, expected, q, planner, mutate in CASES:
        a, r = run_case(q, planner, mutate)
        results.append((cid, expected, r))
        bt = r.back_translation
        bt_str = "-" if not bt else ("match" if bt.matches else "MISMATCH:" + ",".join(bt.mismatched_aspects))
        print(f"{cid:45s} expected={expected:5s} score={r.score:.2f} flagged={r.flagged}  bt={bt_str}  "
              f"sanity={[c.status for c in r.sanity.checks] if r.sanity else '-'}")
        for reason in r.reasons:
            print(f"    - {reason[:180]}")
    right = [r.score for _, e, r in results if e == "right"]
    wrong = [r.score for _, e, r in results if e == "wrong"]
    flag_correct = sum((e == "wrong") == r.flagged for _, e, r in results)
    print(f"\nflag decision correct: {flag_correct}/{len(results)}")
    print(f"right scores: min {min(right):.2f} max {max(right):.2f} | wrong scores: min {min(wrong):.2f} "
          f"max {max(wrong):.2f} | gap (min right - max wrong): {min(right) - max(wrong):+.2f}")


if __name__ == "__main__":
    main()
