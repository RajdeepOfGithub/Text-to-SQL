"""Phase 1: XBRL extraction into SQLite.

Validation values come from ../RAG/financial-rag/data/baseline_questions.jsonl
(q02, q08), pinned at source level: concept + period + dimensions.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from xbrl_db import DB_PATH, connect_readonly  # noqa: E402
from xbrl_extract import extract_all  # noqa: E402


@pytest.fixture(scope="module")
def results():
    return extract_all()


@pytest.fixture(scope="module")
def db():
    if not DB_PATH.exists():
        pytest.fail(f"{DB_PATH} missing; run: python src/xbrl_db.py")
    conn = connect_readonly()
    yield conn
    conn.close()


def _one(db, **where):
    clauses = " AND ".join(f"{k} = ?" for k in where)
    rows = db.execute(f"SELECT value_numeric FROM facts WHERE {clauses}", tuple(where.values())).fetchall()
    assert len(rows) == 1, f"expected exactly one fact for {where}, got {rows}"
    return rows[0][0]


def test_q2_2026_total_net_revenue(db):
    assert _one(
        db, source_document="JPMC_10-Q_Q2-2026", concept="us-gaap:RevenuesNetOfInterestExpense",
        period_start="2026-04-01", period_end="2026-06-30", dimensions_json="{}",
    ) == 57_347_000_000


def test_fy2025_common_stock_repurchases(db):
    # The repurchase table's "aggregate purchase price" is tagged on the equity
    # statement axis, not as a dimensionless fact.
    assert _one(
        db, source_document="JPMC_10-K_FY2025", concept="us-gaap:TreasuryStockValueAcquiredCostMethod",
        period_start="2025-01-01", period_end="2025-12-31",
        dimensions_json=json.dumps({"us-gaap:StatementEquityComponentsAxis": "us-gaap:CommonStockMember"}),
    ) == 31_640_000_000


def test_all_concepts_resolved_and_no_arelle_errors(results):
    for r in results:
        assert r.unresolved_concepts == [], r.source_document
        assert r.error_codes == {}, (r.source_document, r.error_codes)
        assert r.inconsistent_duplicates == [], r.source_document


def test_db_matches_extraction(db, results):
    for r in results:
        (n,) = db.execute("SELECT COUNT(*) FROM facts WHERE source_document = ?", (r.source_document,)).fetchone()
        assert n == len(r.facts) > 0
        assert len(r.facts) <= r.raw_fact_count


def test_sec_numeric_transform_applied(db):
    # "three" in the 10-K text, via ixt-sec:numwordsen from the EDGAR plugin
    assert _one(
        db, source_document="JPMC_10-K_FY2025", concept="us-gaap:NumberOfReportableSegments",
        period_start="2025-01-01", period_end="2025-12-31", dimensions_json="{}",
    ) == 3


def test_period_shape(db):
    bad = db.execute("""SELECT COUNT(*) FROM facts WHERE
        (period_type = 'instant' AND (period_start IS NOT NULL OR period_end IS NULL)) OR
        (period_type = 'duration' AND (period_start IS NULL OR period_end IS NULL OR period_start > period_end))
    """).fetchone()[0]
    assert bad == 0


def test_dimensions_are_valid_json_objects(db):
    for (dj,) in db.execute("SELECT DISTINCT dimensions_json FROM facts"):
        assert isinstance(json.loads(dj), dict)


def test_readonly_connection_rejects_writes(db):
    with pytest.raises(sqlite3.OperationalError):
        db.execute("DELETE FROM facts")
