# Text-to-SQL over SEC XBRL Facts

## Setup

```
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt

# SEC ixt-sec inline transforms (numwordsen, durmonth, ...) are not in arelle-release
git clone https://github.com/Arelle/EDGAR vendor/EDGAR
git -C vendor/EDGAR checkout 4891c4c9889c77af247428ceff4fa7cc52d534ea
```

### Source data (not committed)

`data/raw/jpmc/` holds the two filings (copied from `../RAG/financial-rag/data/raw/jpmc/`)
plus their extension taxonomies, which Arelle needs to resolve concepts.
Without them Arelle loads 0 facts. The taxonomy files come from the same EDGAR filing folders
(SEC requires a User-Agent):

| Filing | Accession | Files |
|---|---|---|
| 10-K FY2025 (`jpmc_10k_2025.htm`) | 0001628280-26-008131 | `jpm-20251231.xsd`, `_cal/_def/_lab/_pre.xml` |
| 10-Q Q2 2026 (`jpmc_10q_q2_2026.htm`) | 0001628280-26-054343 | `jpm-20260630.xsd`, `_cal/_def/_lab/_pre.xml` |

URL pattern: `https://www.sec.gov/Archives/edgar/data/19617/<accession-no-dashes>/<file>`.
Standard taxonomies (us-gaap, dei, srt) are fetched and cached by Arelle on first run.

## Phase 1: fact extraction

```
.venv/Scripts/python src/xbrl_db.py    # builds data/xbrl_facts.db
.venv/Scripts/python -m pytest tests
```

Consistent duplicate facts (the same number reported rounded in different tables) are collapsed
to the most precise one using Arelle's XBRL Duplicates implementation. Instants use
`period_start = NULL, period_end = <date>`.

## Phase 2: SQL generation + guardrails

```
.venv/Scripts/python src/sql_generate.py "What was total net revenue for Q2 2026?"
```

Needs `OPENAI_API_KEY` in `.env` (gitignored).

- `schema_introspect.py`: BM25 over each concept's standard and filer labels, filtered to the
  question's period. Shows the generator every dimensional variant of the top concepts.
- `sql_generate.py`: instructor/gpt-4o-mini plan with the target variant, target period and
  `chosen_dimension_reason`, all validated against the schema slice. After execution, any
  other variant within 5% of the answer (same period and unit) is added to the answer as an
  alternative whether or not the model listed it. Segment breakdowns of the answered concept
  are not treated as alternatives.
- `guardrails.py`: sqlparse validation (one SELECT, no DDL/DML/admin keywords anywhere,
  subquery depth <= 2, row cap), then a read-only SQLite sandbox with an authorizer that
  allows only facts/concepts reads and allowlisted functions. Blocks are logged to
  `logs/guardrails_blocked.jsonl`.

## Phase 3: hallucination detection

```
.venv/Scripts/python scripts/eval_confidence.py   # 5 known-right vs 5 known-wrong cases
```

- `back_translate.py`: one LLM call turns the SQL, its labels and result rows into a question,
  without seeing the original. A second call judges metric and scope against the original.
  Period is compared in code (parsed question period vs result-row periods); the LLM period
  verdict is used only as a fallback.
- **Why an LLM judge, not embeddings:** with text-embedding-3-small/-large, wrong-period paraphrases
  of "total net revenue for Q2 2026" scored 0.86-0.89, above correct paraphrases (0.65-0.73),
  so no threshold works. The judge costs ~1-2k gpt-4o-mini tokens per check.
- `sanity_check.py`: non-null; magnitude vs the concept's own history (leave-one-out, same
  dimensions/unit/period length, within 10x, sign-consistent); answer-text figure vs fact
  value and unit.
- `confidence.py`: 0.6 back-translation + 0.2 magnitude + 0.2 unit/scale, with a non-null gate.
  Weights are untuned. Ambiguity/disclosure is deliberately not an input.

**Eval result (constructed set, n=10):** flag decisions 10/10 correct on two runs. Right answers
all scored 1.00; wrong ones 0.40-0.80. The scale-error case (0.80) is caught by its hard-fail
rule, not by the score threshold. Ten hand-built cases is not evidence that the score is
calibrated. The magnitude check passed every wrong case: they are real facts for the wrong
question, so it only catches value corruption. The judge sometimes names extra mismatched aspects
(e.g. scope as well as period) even when its overall verdict is right.

## Phase 4: evaluation (v1 baseline, frozen)

```
.venv/Scripts/python src/run_eval.py            # cost estimate only
.venv/Scripts/python src/run_eval.py --confirm  # -> baseline/v1/runs/{id}.json
.venv/Scripts/python src/grade.py               # -> baseline/v1/grades.json
```

`data/eval_questions.jsonl` has 15 questions with source-level ground truth (concept, period,
dimensions, value). `tests/test_phase4.py` checks that every expected value exists in the database.

| category | v1 |
|---|---|
| clean_lookup | 5/5 |
| ambiguous | 1/3 |
| guardrail | 3/3 |
| hallucination_bait | 2/2 |
| no_answer | 0/2 |
| **overall** | **11/15** |

No false blocks and no false allows. Run cost $0.0168 (46 gpt-4o-mini calls).
Failures:
- e06: disclosed all three repurchase measures but chose $31,591M cash paid over the
  expected $31,640M (Project 1 ground truth).
- e07: "how much did JPMorgan earn" was answered with diluted EPS, not net income, so the metric is
  wrong. Phase 3 confidence flagged it (0.4).
- e14, e15: no "not available" path. The schema validators reject the model's invented
  variants until instructor's retries run out, and the pipeline raises instead of answering.
