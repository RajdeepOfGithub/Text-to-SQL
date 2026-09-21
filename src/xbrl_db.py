"""SQLite storage for extracted XBRL facts.

Writes happen only in build(). Everything that queries the DB (validation,
tests, later generated SQL) goes through connect_readonly().
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xbrl_extract import ROOT, ExtractResult, extract_all  # noqa: E402

DB_PATH = ROOT / "data" / "xbrl_facts.db"

SCHEMA = """
CREATE TABLE facts (
    id              INTEGER PRIMARY KEY,
    concept         TEXT NOT NULL,          -- prefixed QName, e.g. us-gaap:Revenues
    value           TEXT,                   -- Arelle-normalized value (scale/sign applied); NULL if nil
    value_numeric   REAL,                   -- numeric facts only
    unit            TEXT,                   -- e.g. iso4217:USD, xbrli:shares, iso4217:USD/xbrli:shares
    decimals        TEXT,                   -- as reported ('-6', 'INF', ...)
    scale           TEXT,                   -- ix:scale attribute as reported
    period_type     TEXT NOT NULL CHECK (period_type IN ('instant', 'duration', 'forever')),
    period_start    TEXT,                   -- ISO date; NULL for instant/forever
    period_end      TEXT,                   -- ISO date, inclusive; the instant date for instants
    entity          TEXT NOT NULL,          -- CIK
    dimensions_json TEXT NOT NULL,          -- {"axis": "member"}; '{}' = no explicit dimensions
    context_id      TEXT NOT NULL,
    source_document TEXT NOT NULL
);
CREATE INDEX idx_facts_concept ON facts(concept);
CREATE TABLE concepts (
    concept     TEXT PRIMARY KEY,           -- fact concepts, axes and members
    label       TEXT,                       -- standard taxonomy label
    labels_json TEXT NOT NULL,              -- all label roles, incl. the filer's terse/total labels
    period_type TEXT,
    data_type   TEXT,
    balance     TEXT
);
CREATE INDEX idx_facts_period_start ON facts(period_start);
CREATE INDEX idx_facts_period_end ON facts(period_end);
"""


def connect_readonly(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Read-only connection: SQLite rejects any write at the file-open level."""
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def build(results: list[ExtractResult], db_path: Path = DB_PATH) -> None:
    tmp = db_path.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            """INSERT INTO facts (concept, value, value_numeric, unit, decimals, scale,
                   period_type, period_start, period_end, entity, dimensions_json,
                   context_id, source_document)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (f.concept, f.value, f.value_numeric, f.unit, f.decimals, f.scale,
                 f.period_type, f.period_start, f.period_end, f.entity,
                 json.dumps(f.dimensions, sort_keys=True), f.context_id, f.source_document)
                for r in results for f in r.facts
            ],
        )
        merged: dict[str, dict] = {}
        for r in results:
            for c in r.concepts.values():
                m = merged.setdefault(c.concept, {"info": c, "labels": []})
                m["labels"] += [lb for lb in c.labels if lb not in m["labels"]]
        conn.executemany(
            "INSERT INTO concepts VALUES (?,?,?,?,?,?)",
            [(k, m["info"].label, json.dumps(m["labels"]), m["info"].period_type,
              m["info"].data_type, m["info"].balance) for k, m in merged.items()],
        )
        conn.commit()
    finally:
        conn.close()
    tmp.replace(db_path)


def main() -> None:
    results = extract_all()
    build(results)
    for r in results:
        print(json.dumps({
            "document": r.source_document,
            "raw_facts": r.raw_fact_count,
            "stored_facts": len(r.facts),
            "dimensional_facts": sum(1 for f in r.facts if f.dimensions),
            "unresolved_concepts": r.unresolved_concepts,
            "inconsistent_duplicate_sets": len(r.inconsistent_duplicates),
            "arelle_errors": r.error_codes,
        }))
    print(f"wrote {DB_PATH}")


if __name__ == "__main__":
    main()
