"""Hallucination check: SQL + result -> "what question does this answer?" -> compare.

Two separate LLM calls, neither sharing context with generation:
  1. back_translate(): sees only the SQL, the labels of the concepts/members it
     references, and the result rows. It never sees the original question.
  2. judge(): compares original vs back-translated question per aspect
     (metric, period, scope).
Period is then re-decided in code when possible: the original question's period
(schema_introspect.parse_periods) is compared with the periods actually present in
the result rows. gpt-4o-mini was inconsistent at date equivalence (it called "Q2 2026"
vs "three months ended June 30, 2026" a mismatch in one run and a match in the next),
and the result rows make an LLM unnecessary for this aspect. The LLM period verdict
is used only when the question's period can't be parsed or the result has no periods.

Comparison method: LLM judge (gpt-4o-mini), not embedding similarity. Measured on
this corpus (Phase 3 notes in README): with text-embedding-3-small/-large, wrong-period
paraphrases ("... six months ended June 30, 2026", "... quarter ended June 30, 2025")
scored 0.86-0.89 against "total net revenue for Q2 2026", higher than the correct
paraphrases (0.65-0.73). A wrong metric (dividends) also outscored correct
repurchase paraphrases. No threshold separates them, because embeddings track
surface wording and a changed year or metric barely moves the vector. The judge
costs ~1-2k tokens per check on gpt-4o-mini, which is cheap enough.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema_introspect import parse_periods  # noqa: E402
from xbrl_db import connect_readonly  # noqa: E402
from xbrl_extract import ROOT  # noqa: E402

MODEL = "gpt-4o-mini"
MAX_RESULT_ROWS_SHOWN = 5

BACK_TRANSLATE_PROMPT = """You are given a SQLite query over XBRL facts from JPMorgan Chase's SEC filings, the
human-readable labels of the concepts and dimension members it references, and its result rows.
Write the single natural-language question this query answers, as an analyst would ask it.
Be precise about three things, because they are what distinguish one financial question from another:
- METRIC: what quantity (use the filer's label wording where given)
- PERIOD: exact period - e.g. "the three months ended June 30, 2026" vs "the six months ended June 30,
  2026", or "as of December 31, 2025". Derive it from the period filters.
- SCOPE: always state it explicitly: "firm-wide consolidated total" when dimensions_json is '{}',
  otherwise name the specific segment/member/component.
Describe what the SQL actually selects, not what it might have been meant to select."""

JUDGE_PROMPT = """Compare a user's ORIGINAL question with a question BACK-TRANSLATED from the SQL that was run to
answer it, to decide whether that SQL answers the original question.

First extract, separately for each question, its METRIC (the quantity), PERIOD (exact dates or
span), and SCOPE (firm-wide consolidated total, or a named segment/member/component). A question
that names no segment or component is asking about the firm-wide consolidated total, and so is one
saying "total", "the firm" or the company name.

Then give a verdict per aspect, comparing only that aspect's extracted values:
- "match": same thing. Synonyms are a match ("total net revenue" = "revenues net of interest
  expense"; "Q2 2026" = "three months ended June 30, 2026"). A specific, legitimate measure of an
  under-specified original metric is a match (original "spend on repurchases", back-translation
  "cash paid to repurchase common stock").
- "mismatch": the extracted values differ: a different quantity (revenue vs expense), a different
  period (three vs six months, 2025 vs 2026), or a different scope (one segment vs the firm total).
- "not_specified": the original genuinely says nothing about this aspect.
An aspect is only a mismatch if its own values differ. A wrong metric doesn't make period or scope wrong."""


Verdict = Literal["match", "mismatch", "not_specified"]


class BackTranslation(BaseModel):
    question: str = Field(description="the one question this SQL answers")


class Judgment(BaseModel):
    original_metric: str
    original_period: str
    original_scope: str
    back_translated_metric: str
    back_translated_period: str
    back_translated_scope: str
    metric: Verdict
    period: Verdict
    scope: Verdict
    explanation: str = Field(description="one sentence; name the specific difference if any")


@dataclass
class BackTranslationResult:
    original_question: str
    back_translated_question: str
    judgment: Judgment
    period_source: str = "llm"  # "code" when decided from parsed question period vs result rows

    @property
    def mismatched_aspects(self) -> list[str]:
        return [a for a in ("metric", "period", "scope") if getattr(self.judgment, a) == "mismatch"]

    @property
    def matches(self) -> bool:
        return not self.mismatched_aspects

    @property
    def score(self) -> float:
        """1.0 if no aspect mismatches, else 0.0. Deliberately binary: one wrong aspect is a wrong answer."""
        return 1.0 if self.matches else 0.0


@lru_cache(maxsize=1)
def _client():
    import instructor
    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv(ROOT / ".env")
    return instructor.from_openai(OpenAI(api_key=os.environ["OPENAI_API_KEY"]))


def referenced_labels(sql: str) -> dict[str, list[str]]:
    qnames = sorted(set(re.findall(r"\b(?:us-gaap|jpm|srt|dei|ecd):[A-Za-z0-9_.]+", sql)))
    if not qnames:
        return {}
    conn = connect_readonly()
    try:
        rows = conn.execute(
            f"SELECT concept, label, labels_json FROM concepts WHERE concept IN ({','.join('?' * len(qnames))})",
            qnames).fetchall()
    finally:
        conn.close()
    return {c: list(dict.fromkeys([lb for lb in [label, *json.loads(lj)] if lb])) for c, label, lj in rows}


def back_translate(sql: str, columns: list[str], rows: list[tuple]) -> str:
    shown = [dict(zip(columns, r)) for r in rows[:MAX_RESULT_ROWS_SHOWN]]
    user = (f"SQL\n{sql}\n\nLABELS\n{json.dumps(referenced_labels(sql), indent=1)}\n\n"
            f"RESULT ({len(rows)} rows, first {len(shown)} shown)\n{json.dumps(shown, default=str, indent=1)}")
    bt = _client().chat.completions.create(
        model=MODEL, temperature=0, response_model=BackTranslation, max_retries=2,
        messages=[{"role": "system", "content": BACK_TRANSLATE_PROMPT}, {"role": "user", "content": user}],
    )
    return bt.question


def judge(original: str, back_translated: str) -> Judgment:
    return _client().chat.completions.create(
        model=MODEL, temperature=0, response_model=Judgment, max_retries=2,
        messages=[{"role": "system", "content": JUDGE_PROMPT},
                  {"role": "user", "content": f"ORIGINAL: {original}\nBACK-TRANSLATED: {back_translated}"}],
    )


def period_verdict(original_question: str, columns: list[str], rows: list[tuple]) -> str | None:
    """'match'/'mismatch' from parsed question period vs result periods; None if undecidable in code."""
    hints = parse_periods(original_question)
    if not hints or not rows or not {"period_start", "period_end"} <= set(columns):
        return None
    result_periods = {(d["period_start"], d["period_end"]) for d in (dict(zip(columns, r)) for r in rows)}
    wanted = {(h.start, h.end) for h in hints}
    return "match" if result_periods & wanted else "mismatch"


def check(original_question: str, sql: str, columns: list[str], rows: list[tuple]) -> BackTranslationResult:
    bt = back_translate(sql, columns, rows)
    judgment = judge(original_question, bt)
    code_verdict = period_verdict(original_question, columns, rows)
    if code_verdict is None:
        return BackTranslationResult(original_question, bt, judgment, "llm")
    return BackTranslationResult(original_question, bt, judgment.model_copy(update={"period": code_verdict}), "code")
