"""SQL guardrails: static validation (sqlparse) + sandboxed read-only execution.

Two independent layers, so a miss in one is still caught by the other:
  1. validate_sql(): single SELECT statement, no DDL/DML/admin keywords anywhere
     (including inside CTEs), bounded subquery depth, row limit applied.
  2. execute_sql(): read-only SQLite connection (mode=ro + query_only) with an
     authorizer that only permits reads of the facts/concepts tables and an
     allowlist of pure functions, plus an instruction budget.
Every rejection is appended to logs/guardrails_blocked.jsonl with its reason.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import sqlparse
from sqlparse import tokens as T
from sqlparse.sql import Parenthesis

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xbrl_db import DB_PATH, connect_readonly  # noqa: E402
from xbrl_extract import ROOT  # noqa: E402

BLOCK_LOG = ROOT / "logs" / "guardrails_blocked.jsonl"
MAX_ROWS = 100
MAX_SUBQUERY_DEPTH = 2
MAX_VM_STEPS = 50_000_000  # progress-handler budget; aborts runaway queries

FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "REPLACE", "UPSERT", "MERGE", "TRUNCATE",
    "CREATE", "DROP", "ALTER", "RENAME", "ATTACH", "DETACH", "PRAGMA",
    "VACUUM", "REINDEX", "ANALYZE", "GRANT", "REVOKE", "BEGIN", "COMMIT",
    "ROLLBACK", "SAVEPOINT", "RELEASE", "RETURNING",
}
ALLOWED_TABLES = {"facts", "concepts", "json_each", "json_tree"}
ALLOWED_FUNCTIONS = {
    "abs", "avg", "coalesce", "count", "date", "group_concat", "iif", "ifnull",
    "instr", "json", "json_each", "json_extract", "json_tree", "json_type",
    "json_array_length", "length", "like", "lower", "max", "min", "nullif",
    "printf", "format", "replace", "round", "strftime", "substr", "substring",
    "sum", "total", "trim", "ltrim", "rtrim", "typeof", "upper", "julianday", "glob",
}


class GuardrailViolation(Exception):
    pass


@dataclass
class ValidationResult:
    ok: bool
    reason: str | None
    sql_to_run: str | None = None


@dataclass
class ExecutionResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool
    sql_run: str
    notes: list[str] = field(default_factory=list)


def log_block(sql: str, reason: str, question: str | None = None, stage: str = "validate") -> None:
    BLOCK_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "reason": reason,
        "question": question,
        "sql": sql,
    }
    with BLOCK_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _subquery_depth(token_list, depth: int = 0) -> int:
    deepest = depth
    for tok in token_list.tokens:
        if not tok.is_group:
            continue
        opens_select = False
        if isinstance(tok, Parenthesis):
            first = next((t for t in tok.tokens[1:] if not t.is_whitespace), None)
            opens_select = first is not None and (
                (first.ttype is T.DML and first.normalized == "SELECT") or first.ttype is T.Keyword.CTE
            )
        deepest = max(deepest, _subquery_depth(tok, depth + 1 if opens_select else depth))
    return deepest


def _check(sql: str) -> str | None:
    """Return a block reason, or None if the SQL passes static validation."""
    if not sql or not sql.strip():
        return "empty SQL"
    statements = [s for s in sqlparse.parse(sql) if s.token_first(skip_cm=True, skip_ws=True) is not None]
    if len(statements) != 1:
        return f"expected exactly one statement, got {len(statements)}"
    stmt = statements[0]
    for tok in stmt.flatten():
        if tok.ttype is T.Keyword.DDL or (tok.ttype is T.DML and tok.normalized.upper() != "SELECT"):
            return f"forbidden statement keyword: {tok.normalized.upper()}"
        if (tok.ttype in T.Keyword or tok.ttype in T.Name) and tok.normalized.upper() in FORBIDDEN_KEYWORDS:
            return f"forbidden keyword: {tok.normalized.upper()}"
    if stmt.get_type() != "SELECT":
        return f"only SELECT is allowed (got {stmt.get_type()})"
    depth = _subquery_depth(stmt)
    if depth > MAX_SUBQUERY_DEPTH:
        return f"subquery nesting depth {depth} exceeds {MAX_SUBQUERY_DEPTH}"
    return None


def validate_sql(sql: str, question: str | None = None) -> ValidationResult:
    reason = _check(sql)
    if reason:
        log_block(sql, reason, question)
        return ValidationResult(False, reason)
    body = sql.strip().rstrip(";").strip()
    # Row limit is applied outside the model's SQL so it cannot be overridden.
    # Newline before ')' so a trailing '--' comment cannot swallow the wrapper.
    return ValidationResult(True, None, f"SELECT * FROM (\n{body}\n) LIMIT {MAX_ROWS + 1}")


def _authorizer(action, arg1, arg2, _db, _trigger):
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_OK if arg1 in ALLOWED_TABLES else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK if (arg2 or "").lower() in ALLOWED_FUNCTIONS else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_DENY  # every write, schema change, pragma, attach, transaction


def sandbox_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = connect_readonly(db_path)  # PRAGMA query_only runs before the authorizer is installed
    conn.set_authorizer(_authorizer)
    calls = {"n": 0}
    interval = 10_000

    def budget():
        calls["n"] += 1
        return 1 if calls["n"] * interval > MAX_VM_STEPS else 0

    conn.set_progress_handler(budget, interval)
    return conn


def execute_sql(sql: str, question: str | None = None, db_path: Path = DB_PATH) -> ExecutionResult:
    """Validate then run in the sandbox. Raises GuardrailViolation if blocked at either layer."""
    v = validate_sql(sql, question)
    if not v.ok:
        raise GuardrailViolation(v.reason)
    return run_in_sandbox(v.sql_to_run, question, db_path, original_sql=sql)


def run_in_sandbox(sql_to_run: str, question: str | None = None, db_path: Path = DB_PATH,
                   original_sql: str | None = None) -> ExecutionResult:
    conn = sandbox_connection(db_path)
    try:
        cur = conn.execute(sql_to_run)
        rows = cur.fetchmany(MAX_ROWS + 1)
        columns = [d[0] for d in cur.description or []]
    except sqlite3.DatabaseError as e:
        reason = f"sandbox rejected query: {e}"
        log_block(original_sql or sql_to_run, reason, question, stage="execute")
        raise GuardrailViolation(reason) from e
    finally:
        conn.close()
    truncated = len(rows) > MAX_ROWS
    return ExecutionResult(columns, rows[:MAX_ROWS], truncated, sql_to_run,
                           [f"truncated to {MAX_ROWS} rows"] if truncated else [])
