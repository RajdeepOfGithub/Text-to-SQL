"""Question -> filtered schema slice: candidate concepts, their dimensional
variants, and the periods/values available for each.

Retrieval is BM25 over every label the taxonomy and the filer attach to a
concept (standard, terse, total, negated...), so the filer's own table wording
("Aggregate purchase price of common stock repurchases") is searchable. The
point is to show the generator every plausible variant rather than hide the
ambiguity - but only for the handful of concepts that match the question.
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xbrl_db import connect_readonly  # noqa: E402

TOP_K_CONCEPTS = 8
MAX_VARIANTS_PER_CONCEPT = 12
STOPWORDS = set("""
a an the of for in on at to by and or what was were is are how much many did does do
their its it they firm company jpmorgan jpmc chase value amount quarter year fiscal
reported report during as with from that this which be been ended end q1 q2 q3 q4 fy
""".split())
QUARTERS = {1: ("01-01", "03-31"), 2: ("04-01", "06-30"), 3: ("07-01", "09-30"), 4: ("10-01", "12-31")}
ORDINAL_Q = {"first": 1, "second": 2, "third": 3, "fourth": 4}


@dataclass(frozen=True)
class PeriodHint:
    label: str
    period_type: str  # duration | instant
    start: str | None
    end: str


@dataclass
class Variant:
    dimensions_json: str
    dimension_labels: dict[str, str]  # axis label -> member label
    facts: list[dict]  # {period_type, period_start, period_end, value_numeric, value, unit, source_document}
    total_periods: int


@dataclass
class ConceptCandidate:
    concept: str
    label: str
    filer_labels: list[str]
    period_type: str | None
    data_type: str | None
    score: float
    variants: list[Variant]
    total_variants: int


@dataclass
class SchemaSlice:
    question: str
    periods: list[PeriodHint]
    candidates: list[ConceptCandidate] = field(default_factory=list)

    def find(self, concept: str) -> ConceptCandidate | None:
        return next((c for c in self.candidates if c.concept == concept), None)

    def to_prompt(self) -> str:
        out = []
        if self.periods:
            out.append("Periods mentioned in the question: " + "; ".join(
                f"{p.label} = {p.period_type} " + (f"{p.start}..{p.end}" if p.start else p.end) for p in self.periods))
        else:
            out.append("No period in the question; available periods are listed per variant.")
        for c in self.candidates:
            out.append(f"\nCONCEPT {c.concept}  [{c.period_type}, {c.data_type}]")
            out.append(f"  labels: {c.label!r}" + (f"; filer wording: {c.filer_labels}" if c.filer_labels else ""))
            n = c.total_variants
            out.append(f"  {n} dimensional variant(s)" + (f", showing {len(c.variants)}" if n > len(c.variants) else ""))
            for v in c.variants:
                dims = ", ".join(f"{a} = {m}" for a, m in v.dimension_labels.items()) or "no dimensions (consolidated / default)"
                out.append(f"  - dimensions_json = '{v.dimensions_json}'  ({dims})")
                for f in v.facts:
                    per = f"{f['period_start']}..{f['period_end']}" if f["period_start"] else f"instant {f['period_end']}"
                    out.append(f"      {per}: {f['value']} {f['unit'] or ''}  [{f['source_document']}]")
                if v.total_periods > len(v.facts):
                    out.append(f"      (+{v.total_periods - len(v.facts)} other periods)")
        return "\n".join(out)


# ---------------------------------------------------------------- periods

def parse_periods(question: str) -> list[PeriodHint]:
    q = question.lower()
    hints: list[PeriodHint] = []

    def quarter(n: int, y: int):
        s, e = QUARTERS[n]
        hints.append(PeriodHint(f"Q{n} {y}", "duration", f"{y}-{s}", f"{y}-{e}"))

    def year(raw: str) -> int:
        y = int(raw)
        return y + 2000 if y < 100 else y

    for m in re.finditer(r"\bq([1-4])[\s\-']*(?:fy)?\s*'?(20\d{2}|\d{2})\b", q):
        quarter(int(m.group(1)), year(m.group(2)))
    for m in re.finditer(r"\b([1-4])q\s*'?(20\d{2}|\d{2})\b", q):
        quarter(int(m.group(1)), year(m.group(2)))
    for m in re.finditer(r"\b(first|second|third|fourth) quarter(?: of)?(?: fiscal)?(?: year)? (20\d{2})\b", q):
        quarter(ORDINAL_Q[m.group(1)], int(m.group(2)))
    for m in re.finditer(r"\b(?:first half|six months ended june 30,?|h1)\s*(?:of\s*)?(20\d{2})\b", q):
        y = int(m.group(1))
        hints.append(PeriodHint(f"H1 {y}", "duration", f"{y}-01-01", f"{y}-06-30"))
    for m in re.finditer(r"\bas of june 30,?\s*(20\d{2})\b", q):
        y = int(m.group(1))
        hints.append(PeriodHint(f"as of {y}-06-30", "instant", None, f"{y}-06-30"))
    for m in re.finditer(r"\b(?:as of|at|end of)\s+(?:(?:december 31|year[- ]end),?\s*)?(20\d{2})\b", q):
        y = int(m.group(1))
        hints.append(PeriodHint(f"as of {y}-12-31", "instant", None, f"{y}-12-31"))
    if not hints:  # bare year / fiscal year: JPMC's fiscal year is the calendar year
        for m in re.finditer(r"\b(20\d{2})\b", q):
            y = int(m.group(1))
            hints.append(PeriodHint(f"FY{y}", "duration", f"{y}-01-01", f"{y}-12-31"))
            hints.append(PeriodHint(f"year-end {y}", "instant", None, f"{y}-12-31"))
    return list(dict.fromkeys(hints))


# ---------------------------------------------------------------- retrieval

def _stem(w: str) -> str:
    for suf in ("ings", "ing", "ies", "es", "s", "ed"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def tokenize(text: str) -> list[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)  # split CamelCase concept names
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS and not w.isdigit()]


@lru_cache(maxsize=1)
def _index():
    conn = connect_readonly()
    try:
        rows = conn.execute("""
            SELECT c.concept, c.label, c.labels_json, c.period_type, c.data_type
            FROM concepts c WHERE EXISTS (SELECT 1 FROM facts f WHERE f.concept = c.concept)
        """).fetchall()
        labels = dict(conn.execute("SELECT concept, COALESCE(label, concept) FROM concepts").fetchall())
    finally:
        conn.close()
    docs, meta, phrases = {}, {}, {}
    for concept, label, labels_json, ptype, dtype in rows:
        all_labels = json.loads(labels_json)
        docs[concept] = Counter(tokenize(" ".join([concept.split(":")[1], *all_labels])))
        meta[concept] = (label or concept, [lb for lb in all_labels if lb != label], ptype, dtype)
        phrases[concept] = {" ".join(re.findall(r"[a-z0-9]+", lb.lower())) for lb in all_labels}
    df = Counter(t for d in docs.values() for t in d)
    avgdl = sum(sum(d.values()) for d in docs.values()) / len(docs)
    return docs, meta, df, avgdl, labels, phrases


PHRASE_BOOST = 10.0  # a filer label appearing verbatim in the question is near-certain intent


def retrieve_concepts(question: str, k: int = TOP_K_CONCEPTS) -> list[tuple[str, float]]:
    docs, _, df, avgdl, _, phrases = _index()
    n = len(docs)
    q_terms = set(tokenize(question))
    q_norm = " " + " ".join(re.findall(r"[a-z0-9]+", question.lower())) + " "
    k1, b = 1.2, 0.75
    scores = {}
    for concept, tf in docs.items():
        dl = sum(tf.values())
        s = 0.0
        for t in q_terms:
            if t in tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * dl / avgdl))
        if s > 0:
            if any(len(ph.split()) >= 2 and f" {ph} " in q_norm for ph in phrases[concept]):
                s += PHRASE_BOOST
            scores[concept] = s
    return sorted(scores.items(), key=lambda kv: -kv[1])[:k]


# ---------------------------------------------------------------- variants

def _in_scope(row: dict, periods: list[PeriodHint]) -> bool:
    return not periods or any(
        p.period_type == row["period_type"] and p.end == row["period_end"] and p.start == row["period_start"]
        for p in periods
    )


def _variants(conn, concept: str, periods: list[PeriodHint], labels: dict[str, str]) -> tuple[list[Variant], int]:
    cur = conn.execute("""
        SELECT dimensions_json, period_type, period_start, period_end, value_numeric, value, unit, source_document
        FROM facts WHERE concept = ? AND value_numeric IS NOT NULL
        ORDER BY period_end DESC, period_start DESC
    """, (concept,))
    cols = [d[0] for d in cur.description]
    by_dims: dict[str, list[dict]] = defaultdict(list)
    for r in cur.fetchall():
        by_dims[r[0]].append(dict(zip(cols, r)))
    variants = []
    for dims_json, rows in by_dims.items():
        in_scope = [r for r in rows if _in_scope(r, periods)]
        if periods and not in_scope:
            continue
        seen, facts = set(), []
        for r in (in_scope if periods else rows[:3]):  # same fact can appear in both filings; show once
            key = (r["period_start"], r["period_end"], r["value_numeric"])
            if key not in seen:
                seen.add(key)
                facts.append(r)
        dims = json.loads(dims_json)
        variants.append(Variant(
            dimensions_json=dims_json,
            dimension_labels={labels.get(a, a): labels.get(m, m) for a, m in dims.items()},
            facts=facts,
            total_periods=len({(r["period_start"], r["period_end"]) for r in rows}),
        ))
    variants.sort(key=lambda v: (len(json.loads(v.dimensions_json)), v.dimensions_json))
    return variants[:MAX_VARIANTS_PER_CONCEPT], len(variants)


def introspect(question: str, k: int = TOP_K_CONCEPTS) -> SchemaSlice:
    periods = parse_periods(question)
    _, meta, _, _, labels, _ = _index()
    slice_ = SchemaSlice(question=question, periods=periods)
    conn = connect_readonly()
    try:
        for concept, score in retrieve_concepts(question, k * 3):
            variants, total = _variants(conn, concept, periods, labels)
            if not variants:
                continue  # no numeric facts in the requested period(s)
            label, filer_labels, ptype, dtype = meta[concept]
            slice_.candidates.append(ConceptCandidate(
                concept, label, filer_labels, ptype, dtype, round(score, 2), variants, total))
            if len(slice_.candidates) >= k:
                break
    finally:
        conn.close()
    return slice_


if __name__ == "__main__":
    print(introspect(" ".join(sys.argv[1:]) or "What was total net revenue for Q2 2026?").to_prompt())
