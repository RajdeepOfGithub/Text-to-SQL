"""Question -> SQL plan (instructor/gpt-4o-mini) -> guarded execution -> answer.

Ambiguity handling has two layers:
  1. The structured plan must name the variant it targets, and must explain the
     choice (chosen_dimension_reason) whenever that concept has several
     dimensional variants in scope. Pydantic validators enforce this against the
     introspected schema; instructor re-asks on violation.
  2. After execution, a deterministic check looks for competing measures in the
     schema slice: other concept/dimension variants for the same period and unit
     whose value is within NEAR_VALUE_TOLERANCE of the answer (e.g. repurchases:
     cash paid vs. treasury-stock cost). Any the model did not disclose are added,
     and the answer is marked as a headline pick. The answer therefore never
     states one of several plausible numbers without listing the others.
     Conversely, segment/member breakdowns of the answered concept that the model
     lists as "alternatives" are dropped (unless near-value): they are parts of
     the total, not competing answers to the question.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from guardrails import GuardrailViolation, execute_sql, log_block, validate_sql  # noqa: E402
from schema_introspect import SchemaSlice, introspect  # noqa: E402
from xbrl_extract import ROOT  # noqa: E402

MODEL = "gpt-4o-mini"
NEAR_VALUE_TOLERANCE = 0.05

SYSTEM_PROMPT = """You translate questions about JPMorgan Chase's SEC filings (10-K FY2025, 10-Q Q2 2026)
into ONE SQLite query.

TABLE facts(
  concept TEXT,          -- XBRL concept QName, e.g. 'us-gaap:RevenuesNetOfInterestExpense'
  value TEXT, value_numeric REAL,   -- value_numeric is in base units: USD (not millions), shares, pure ratios
  unit TEXT,             -- 'iso4217:USD', 'xbrli:shares', 'iso4217:USD/xbrli:shares', 'xbrli:pure'
  period_type TEXT,      -- 'duration' or 'instant'
  period_start TEXT,     -- 'YYYY-MM-DD'; NULL for instants
  period_end TEXT,       -- 'YYYY-MM-DD' inclusive; the balance date for instants
  dimensions_json TEXT,  -- exact JSON string of axis->member; '{}' means no dimensions
  entity TEXT, context_id TEXT,
  source_document TEXT   -- 'JPMC_10-K_FY2025' or 'JPMC_10-Q_Q2-2026'
)
TABLE concepts(concept TEXT, label TEXT, labels_json TEXT, period_type TEXT, data_type TEXT, balance TEXT)

SQL rules:
- Set intent='modify' for any request to delete, drop, change, insert or otherwise alter data or
  tables, and write the literal SQL statement that would do it (DELETE/DROP/UPDATE...). Do not refuse,
  soften, or ask for clarification: a separate permission layer decides what may run.
- If the question gives no period, use the most recent period available and say so in `reasoning`.
- For a value lookup select exactly:
  SELECT concept, dimensions_json, period_start, period_end, value_numeric, unit, source_document FROM facts
  WHERE concept = '<exact concept>' AND dimensions_json = '<exact string from SCHEMA>'
    AND period_start = '<start>' AND period_end = '<end>'      (use period_start IS NULL for instants)
- Use only concepts and dimensions_json strings that appear in SCHEMA, copied exactly.

Choosing a variant (this matters more than the SQL):
- Set `target` to the concept + dimensions_json your SQL returns, and target_period_start/end to the
  period of the fact you are selecting (it must be one listed under that variant in SCHEMA).
- If the target concept lists more than one dimensional variant, fill chosen_dimension_reason: which
  variant you picked and why (default: the variant with no dimensions is the consolidated headline).
- Different concepts or variants in SCHEMA may each plausibly answer the question while measuring
  different things (e.g. cash paid in the cash flow statement vs. cost recorded in equity). If more
  than one plausibly answers:
    * ambiguity='picked_headline': choose the most standard headline measure for the question's
      wording, explain in chosen_dimension_reason, and list the others in `alternatives`; or
    * ambiguity='needs_clarification': if no variant is clearly the headline, set sql=null,
      write clarification_question, and list the options in `alternatives`.
- ambiguity='none' only when exactly one variant plausibly answers the question.
- Breakdowns are NOT alternatives: segment, business-line or component members of the same concept
  are parts of the total. If the question asks for the total and a no-dimension variant exists, target
  it; do not list its segment members in `alternatives`.
"""


class VariantRef(BaseModel):
    concept: str
    dimensions_json: str = Field(description="exact dimensions_json string from SCHEMA, '{}' if none")

    @field_validator("dimensions_json")
    @classmethod
    def canonical(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            return "{}"
        try:  # match the DB's canonical form: sorted keys, default separators
            return json.dumps(json.loads(v), sort_keys=True)
        except json.JSONDecodeError:
            return v


class SQLPlan(BaseModel):
    intent: Literal["read", "modify"] = Field(description="'modify' for any request to change data or tables")
    reasoning: str = Field(description="one or two sentences: which concept/period and why")
    sql: str | None = Field(description="single SQLite query, or null when asking for clarification")
    target: VariantRef | None = Field(description="variant the SQL returns; null if not a single-variant lookup")
    target_period_start: str | None = Field(default=None, description="period_start of the target fact; null for instants")
    target_period_end: str | None = Field(default=None, description="period_end of the target fact (instant date for instants)")
    ambiguity: Literal["none", "picked_headline", "needs_clarification"]
    chosen_dimension_reason: str | None = Field(
        default=None, description="required when the target concept has multiple dimensional variants")
    alternatives: list[VariantRef] = Field(default_factory=list)
    clarification_question: str | None = None

    @model_validator(mode="after")
    def check_against_schema(self, info: ValidationInfo):
        slice_: SchemaSlice | None = (info.context or {}).get("schema")
        if self.intent == "modify":
            if not self.sql:
                raise ValueError("intent='modify' requires the literal SQL statement; do not refuse")
            return self  # schema checks do not apply; guardrails will block it
        if self.ambiguity == "needs_clarification":
            if not self.clarification_question or len(self.alternatives) < 2:
                raise ValueError("needs_clarification requires clarification_question and >=2 alternatives")
        elif not self.sql:
            raise ValueError("sql is required unless ambiguity is needs_clarification")
        # The label is advisory; answer_question() recomputes it from alternatives + competitors.
        if self.ambiguity != "needs_clarification":
            self.ambiguity = "picked_headline" if self.alternatives else "none"
        if slice_ is None:
            return self
        known = {(c.concept, v.dimensions_json) for c in slice_.candidates for v in c.variants}
        for ref in [self.target, *self.alternatives]:
            if ref is not None and (ref.concept, ref.dimensions_json) not in known:
                raise ValueError(f"{ref.concept} {ref.dimensions_json} is not a variant listed in SCHEMA")
        if self.target is not None:
            facts = [f for c in slice_.candidates if c.concept == self.target.concept
                     for v in c.variants if v.dimensions_json == self.target.dimensions_json for f in v.facts]
            periods = {(f["period_start"], f["period_end"]) for f in facts}
            if (self.target_period_start, self.target_period_end) not in periods:
                listed = ", ".join(f"{a}..{b}" if a else f"instant {b}" for a, b in sorted(periods, key=str))
                raise ValueError(f"target period {self.target_period_start}..{self.target_period_end} has no fact "
                                 f"for this variant; available: {listed}")
            if self.target_period_end not in (self.sql or ""):
                raise ValueError(f"SQL must filter on the target period (period_end = '{self.target_period_end}')")
            cand = slice_.find(self.target.concept)
            if cand and cand.total_variants > 1 and not (self.chosen_dimension_reason or "").strip():
                raise ValueError(
                    f"{self.target.concept} has {cand.total_variants} dimensional variants in scope; "
                    "chosen_dimension_reason must say which was picked and why")
            dims = json.loads(self.target.dimensions_json)
            reason = (self.chosen_dimension_reason or "").lower()
            if dims and cand:
                members = [m.split(":")[1].removesuffix("Member").lower() for m in dims.values()]
                labels = [lb.lower().replace(" [member]", "") for v in cand.variants
                          if v.dimensions_json == self.target.dimensions_json for lb in v.dimension_labels.values()]
                if not any(t in reason or t in reason.replace(" ", "") for t in members + labels):
                    raise ValueError(
                        f"target has dimensions {dims} but chosen_dimension_reason does not name the chosen "
                        "member; describe the variant actually selected")
        return self


PlanFn = Callable[[str, SchemaSlice], SQLPlan]


def llm_plan(question: str, schema: SchemaSlice) -> SQLPlan:
    import instructor
    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv(ROOT / ".env")
    client = instructor.from_openai(OpenAI(api_key=os.environ["OPENAI_API_KEY"]))
    return client.chat.completions.create(
        model=MODEL,
        temperature=0,
        response_model=SQLPlan,
        max_retries=2,
        context={"schema": schema},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"SCHEMA\n{schema.to_prompt()}\n\nQUESTION\n{question}"},
        ],
    )


# ---------------------------------------------------------------- answer

@dataclass
class Measure:
    concept: str
    dimensions_json: str
    period_start: str | None
    period_end: str
    value_numeric: float
    unit: str | None
    label: str = ""
    dimension_labels: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        dims = ", ".join(self.dimension_labels.values())
        return f"{self.label or self.concept} ({self.concept}{'; ' + dims if dims else ''})"


@dataclass
class Answer:
    question: str
    status: Literal["answered", "needs_clarification", "blocked", "no_result"]
    text: str
    sql: str | None = None
    plan: SQLPlan | None = None
    measure: Measure | None = None
    alternatives: list[Measure] = field(default_factory=list)
    ambiguity: str = "none"
    rows: list[tuple] = field(default_factory=list)
    block_reason: str | None = None
    system_notes: list[str] = field(default_factory=list)


def format_value(v: float, unit: str | None) -> str:
    if unit == "iso4217:USD":
        return f"${v / 1e6:,.0f}M" if abs(v) >= 1e6 and v % 1e6 == 0 else f"${v:,.0f}"
    if unit == "xbrli:shares":
        return f"{v / 1e6:,.1f}M shares" if abs(v) >= 1e6 else f"{v:,.0f} shares"
    if unit == "iso4217:USD/xbrli:shares":
        return f"${v:,.2f} per share"
    return f"{v:,g}"


def _period_str(start: str | None, end: str) -> str:
    return f"{start} to {end}" if start else f"as of {end}"


def _variant_facts(schema: SchemaSlice, concept: str, dims: str):
    cand = schema.find(concept)
    v = next((v for v in cand.variants if v.dimensions_json == dims), None) if cand else None
    return cand, v


def _variant_measure(schema: SchemaSlice, concept: str, dims: str, start: str | None, end: str) -> Measure | None:
    cand, v = _variant_facts(schema, concept, dims)
    for f in (v.facts if v else []):
        if f["period_start"] == start and f["period_end"] == end:
            return Measure(concept, dims, start, end, f["value_numeric"], f["unit"], cand.label, v.dimension_labels)
    return None


def near_value_competitors(schema: SchemaSlice, chosen: Measure) -> list[Measure]:
    """Other variants, same period + unit, value different but within tolerance."""
    out = []
    for c in schema.candidates:
        for v in c.variants:
            if (c.concept, v.dimensions_json) == (chosen.concept, chosen.dimensions_json):
                continue
            for f in v.facts:
                x = f["value_numeric"]
                if (f["period_start"], f["period_end"], f["unit"]) != (chosen.period_start, chosen.period_end, chosen.unit):
                    continue
                if x is None or chosen.value_numeric == 0 or x == chosen.value_numeric:
                    continue
                if abs(x - chosen.value_numeric) / abs(chosen.value_numeric) <= NEAR_VALUE_TOLERANCE:
                    out.append(Measure(c.concept, v.dimensions_json, f["period_start"], f["period_end"], x,
                                       f["unit"], c.label, v.dimension_labels))
    return out


def _is_breakdown(total: Measure, other: Measure) -> bool:
    """Same concept with strictly more dimensions: a member of the total (e.g. one segment's revenue).

    In XBRL the dimensionless fact is the default member, i.e. the total over the domain, so these
    are components of the answer rather than competing measures of it.
    """
    if total.concept != other.concept:
        return False
    t, o = json.loads(total.dimensions_json), json.loads(other.dimensions_json)
    return len(o) > len(t) and all(o.get(k) == v for k, v in t.items())


def _measure_from_rows(columns: list[str], rows: list[tuple], plan: SQLPlan, schema: SchemaSlice) -> Measure | None:
    if not rows or not {"value_numeric", "period_end"} <= set(columns):
        return None
    dicts = [dict(zip(columns, r)) for r in rows]
    # the same fact can be returned once per filing; collapse identical facts
    distinct = {(d.get("concept"), d.get("dimensions_json"), d.get("period_start"), d["period_end"], d["value_numeric"])
                for d in dicts}
    if len(distinct) != 1:
        return None  # multi-row result, not a single-value lookup
    concept, dims, start, end, value = distinct.pop()
    concept = concept or (plan.target.concept if plan.target else None)
    if dims is None:
        dims = plan.target.dimensions_json if plan.target else "{}"
    m = _variant_measure(schema, concept, dims, start, end) if concept else None
    if m is None:
        cand = schema.find(concept) if concept else None
        m = Measure(concept or "?", dims, start, end, value, dicts[0].get("unit"), cand.label if cand else "")
    m.value_numeric = value
    return m


def _clarify(question: str, plan: SQLPlan, schema: SchemaSlice) -> Answer:
    options = []
    for ref in plan.alternatives:
        cand, v = _variant_facts(schema, ref.concept, ref.dimensions_json)
        for f in (v.facts if v else [])[:1]:
            options.append(Measure(ref.concept, ref.dimensions_json, f["period_start"], f["period_end"],
                                   f["value_numeric"], f["unit"], cand.label, v.dimension_labels))
    listing = "\n".join(f"  - {m.describe()}: {format_value(m.value_numeric, m.unit)} "
                        f"({_period_str(m.period_start, m.period_end)})" for m in options)
    return Answer(question, "needs_clarification", f"{plan.clarification_question}\nOptions:\n{listing}",
                  plan=plan, alternatives=options, ambiguity=plan.ambiguity)


def answer_question(question: str, planner: PlanFn = llm_plan) -> Answer:
    schema = introspect(question)
    plan = planner(question, schema)
    if plan.intent == "modify":
        # Blocked in code whatever SQL the model wrote; validate_sql also logs the statement.
        reason = "data modification requested; the database is read-only"
        v = validate_sql(plan.sql, question)
        if v.ok:  # model softened a modify request into a SELECT - still refuse, still log
            log_block(plan.sql, reason, question, stage="intent")
        else:
            reason = f"{reason} ({v.reason})"
        return Answer(question, "blocked", f"Query blocked by guardrails: {reason}", sql=plan.sql, plan=plan,
                      block_reason=reason)
    if plan.ambiguity == "needs_clarification":
        return _clarify(question, plan, schema)

    try:
        result = execute_sql(plan.sql, question)
    except GuardrailViolation as e:
        return Answer(question, "blocked", f"Query blocked by guardrails: {e}", sql=plan.sql, plan=plan,
                      block_reason=str(e))

    measure = _measure_from_rows(result.columns, result.rows, plan, schema)
    if measure is None:
        if not result.rows:
            return Answer(question, "no_result", "The query returned no rows.", sql=plan.sql, plan=plan)
        preview = "\n".join(str(r) for r in result.rows[:10])
        return Answer(question, "answered", f"{len(result.rows)} rows:\n{preview}", sql=plan.sql, plan=plan,
                      rows=result.rows, ambiguity=plan.ambiguity)

    ambiguity = plan.ambiguity
    notes = []
    competitors = near_value_competitors(schema, measure)
    competitor_keys = {(c.concept, c.dimensions_json) for c in competitors}
    alternatives = []
    for ref in plan.alternatives:
        m = _variant_measure(schema, ref.concept, ref.dimensions_json, measure.period_start, measure.period_end)
        if m is None:
            continue
        if _is_breakdown(measure, m) and (m.concept, m.dimensions_json) not in competitor_keys:
            notes.append(f"dropped breakdown listed as alternative: {m.concept} {m.dimensions_json}")
            continue
        alternatives.append(m)
    disclosed = {(m.concept, m.dimensions_json) for m in alternatives}
    for comp in competitors:
        if (comp.concept, comp.dimensions_json) not in disclosed:
            alternatives.append(comp)
            disclosed.add((comp.concept, comp.dimensions_json))
            notes.append(f"system added undisclosed near-value alternative {comp.concept} {comp.dimensions_json}")
    if alternatives:
        ambiguity = "picked_headline"
    elif ambiguity == "picked_headline":
        ambiguity = "none"  # everything the model listed was a breakdown of the answer

    lines = [f"{format_value(measure.value_numeric, measure.unit)} for "
             f"{_period_str(measure.period_start, measure.period_end)}.",
             f"Measure: {measure.describe()}."]
    if plan.chosen_dimension_reason:
        lines.append(f"Why this variant: {plan.chosen_dimension_reason}")
    if alternatives:
        lines.append("Other measures in the filing that could also answer this question:")
        lines += [f"  - {m.describe()}: {format_value(m.value_numeric, m.unit)}" for m in alternatives]
    return Answer(question, "answered", "\n".join(lines), sql=plan.sql, plan=plan, measure=measure,
                  alternatives=alternatives, ambiguity=ambiguity, rows=result.rows, system_notes=notes)


if __name__ == "__main__":
    a = answer_question(" ".join(sys.argv[1:]) or "What was total net revenue for Q2 2026?")
    print(f"[{a.status} / ambiguity={a.ambiguity}]\nSQL: {a.sql}\n\n{a.text}")
    if a.system_notes:
        print("\nsystem notes:", json.dumps(a.system_notes, indent=1))
