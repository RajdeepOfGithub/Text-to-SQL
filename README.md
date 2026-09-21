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
