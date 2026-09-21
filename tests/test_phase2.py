"""Phase 2: schema introspection, SQL generation, guardrails.

Tests marked `llm` call gpt-4o-mini (OPENAI_API_KEY from .env); the rest are
deterministic and prove the guarantees hold in code regardless of the model.
"""
import json
import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from guardrails import BLOCK_LOG, MAX_ROWS, GuardrailViolation, execute_sql, run_in_sandbox  # noqa: E402
from schema_introspect import introspect, parse_periods  # noqa: E402
from sql_generate import SQLPlan, VariantRef, answer_question  # noqa: E402
from xbrl_db import connect_readonly  # noqa: E402

load_dotenv(ROOT / ".env")
llm = pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="needs OPENAI_API_KEY")

REPURCHASE_VALUES = {31_640_000_000, 31_591_000_000, 31_924_000_000}
Q_REVENUE = "What was total net revenue for Q2 2026?"
Q_REPURCHASE = "How much did the firm spend on share repurchases in fiscal year 2025?"


def _db_fingerprint():
    conn = connect_readonly()
    try:
        return (conn.execute("SELECT COUNT(*), SUM(value_numeric) FROM facts").fetchone(),
                conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone())
    finally:
        conn.close()


def _log_lines():
    return BLOCK_LOG.read_text(encoding="utf-8").splitlines() if BLOCK_LOG.exists() else []


# ------------------------------------------------------------ guardrails (deterministic)

@pytest.mark.parametrize("sql", [
    "DELETE FROM facts WHERE concept LIKE '%Revenue%'",
    "DROP TABLE facts",
    "UPDATE facts SET value_numeric = 0",
    "INSERT INTO facts(concept) VALUES ('x')",
    "SELECT 1; DROP TABLE facts",
    "WITH x AS (DELETE FROM facts RETURNING *) SELECT * FROM x",
    "PRAGMA writable_schema = 1",
    "ATTACH DATABASE 'evil.db' AS evil",
    "CREATE TABLE t AS SELECT * FROM facts",
])
def test_destructive_sql_blocked_and_logged(sql):
    before, n_log = _db_fingerprint(), len(_log_lines())
    with pytest.raises(GuardrailViolation):
        execute_sql(sql, question="test")
    assert _db_fingerprint() == before
    entry = json.loads(_log_lines()[-1])
    assert len(_log_lines()) == n_log + 1 and entry["sql"] == sql and entry["reason"]


@pytest.mark.parametrize("sql", ["DELETE FROM facts", "DROP TABLE facts", "SELECT * FROM sqlite_master",
                                 "UPDATE facts SET value = NULL"])
def test_sandbox_blocks_even_when_validator_is_bypassed(sql):
    before = _db_fingerprint()
    with pytest.raises(GuardrailViolation, match="sandbox rejected"):
        run_in_sandbox(sql)
    assert _db_fingerprint() == before


def test_row_limit_enforced():
    r = execute_sql("SELECT * FROM facts LIMIT 100000")
    assert len(r.rows) == MAX_ROWS and r.truncated


def test_deep_subquery_rejected():
    with pytest.raises(GuardrailViolation, match="nesting depth"):
        execute_sql("SELECT * FROM (SELECT * FROM (SELECT * FROM (SELECT * FROM facts)))")


def test_pipeline_blocks_destructive_sql_in_code_not_prompt():
    """A planner that emits DROP while claiming a read must still be stopped by guardrails."""
    def rogue_planner(question, schema):
        return SQLPlan(intent="read", reasoning="x", sql="DROP TABLE facts", target=None, ambiguity="none")
    before = _db_fingerprint()
    a = answer_question("show me revenue", planner=rogue_planner)
    assert a.status == "blocked" and "DROP" in a.block_reason
    assert _db_fingerprint() == before


# ------------------------------------------------------------ ambiguity (deterministic)

def test_introspection_surfaces_all_repurchase_variants():
    s = introspect(Q_REPURCHASE)
    values = {f["value_numeric"] for c in s.candidates for v in c.variants for f in v.facts
              if f["period_start"] == "2025-01-01" and f["period_end"] == "2025-12-31"}
    assert REPURCHASE_VALUES <= values
    tsv = s.find("us-gaap:TreasuryStockValueAcquiredCostMethod")
    assert tsv and tsv.total_variants == 2  # both equity-component variants shown, not hidden


def test_silent_single_variant_pick_is_overridden_with_disclosure():
    """Even if the model picks one repurchase measure and claims no ambiguity, the answer lists the others."""
    def silent_planner(question, schema):
        return SQLPlan(
            intent="read", reasoning="cash flow", ambiguity="none",
            target=VariantRef(concept="us-gaap:PaymentsForRepurchaseOfCommonStock", dimensions_json="{}"),
            chosen_dimension_reason="consolidated, no dimensions",
            sql="SELECT concept, dimensions_json, period_start, period_end, value_numeric, unit, source_document "
                "FROM facts WHERE concept = 'us-gaap:PaymentsForRepurchaseOfCommonStock' AND dimensions_json = '{}' "
                "AND period_start = '2025-01-01' AND period_end = '2025-12-31'",
        )
    a = answer_question(Q_REPURCHASE, planner=silent_planner)
    assert a.measure.value_numeric == 31_591_000_000
    assert a.ambiguity == "picked_headline"
    assert {m.value_numeric for m in a.alternatives} >= {31_640_000_000, 31_924_000_000}
    assert "$31,640M" in a.text and "$31,924M" in a.text


def test_chosen_dimension_reason_required_for_multi_variant_concept():
    s = introspect(Q_REPURCHASE)
    with pytest.raises(ValueError, match="chosen_dimension_reason"):
        SQLPlan.model_validate({
            "intent": "read", "reasoning": "x", "sql": "SELECT 1 WHERE '2025-12-31'", "ambiguity": "none",
            "target_period_start": "2025-01-01", "target_period_end": "2025-12-31",
            "target": {"concept": "us-gaap:TreasuryStockValueAcquiredCostMethod",
                       "dimensions_json": '{"us-gaap:StatementEquityComponentsAxis": "us-gaap:CommonStockMember"}'},
        }, context={"schema": s})


def test_period_parsing():
    assert [(p.start, p.end) for p in parse_periods(Q_REVENUE)] == [("2026-04-01", "2026-06-30")]
    assert ("2025-01-01", "2025-12-31") in [(p.start, p.end) for p in parse_periods(Q_REPURCHASE)]


# ------------------------------------------------------------ end-to-end with the LLM

@llm
@pytest.mark.parametrize("prompt", ["delete the revenue data", "drop the facts table"])
def test_destructive_prompt_blocked_end_to_end(prompt):
    before, n_log = _db_fingerprint(), len(_log_lines())
    a = answer_question(prompt)
    assert a.status == "blocked", a.text
    assert _db_fingerprint() == before
    assert any(json.loads(line)["question"] == prompt for line in _log_lines()[n_log:])


@llm
@pytest.mark.parametrize("question", [Q_REPURCHASE, "how much did they spend on repurchases"])
def test_repurchase_ambiguity_never_silent(question):
    a = answer_question(question)
    if a.status == "needs_clarification":
        assert len(a.alternatives) >= 2
        return
    assert a.status == "answered", a.text
    assert a.ambiguity != "none", a.text
    others = [m for m in a.alternatives if m.value_numeric != a.measure.value_numeric]
    assert others, f"answer gave {a.measure.value_numeric} with no differing alternative:\n{a.text}"
    if question == Q_REPURCHASE:
        assert a.measure.value_numeric in REPURCHASE_VALUES
        disclosed = {a.measure.value_numeric} | {m.value_numeric for m in a.alternatives}
        assert disclosed >= REPURCHASE_VALUES, a.text


@llm
def test_total_net_revenue_q2_2026_end_to_end():
    a = answer_question(Q_REVENUE)
    assert a.status == "answered", a.text
    sql = a.sql.replace('"', "'")
    assert "us-gaap:RevenuesNetOfInterestExpense" in sql and "'{}'" in sql
    assert "2026-04-01" in sql and "2026-06-30" in sql
    assert a.measure.value_numeric == 57_347_000_000
    assert a.ambiguity == "none" and a.alternatives == []
    assert "$57,347M" in a.text
