
# Project: Text-to-SQL over SEC XBRL Facts

Second portfolio project, same JPMC corpus as ../financial-rag (10-K FY2025,
10-Q Q2-2026). Source files: data/raw/jpmc/*.htm (already downloaded, contain
inline XBRL — do not re-fetch).

## Stack

Python 3.11+, Arelle (XBRL parsing — do not hand-roll context/dimension
resolution), SQLite, sqlparse (SQL validation), OpenAI gpt-4o-mini,
pydantic/instructor for structured output.

## Conventions

- Reports after each task: 5 lines max. No "notes and judgment calls" essays
  unless something is a genuine correctness bug or ambiguous design fork —
  those get 2-3 lines, not a page.
- Read-only DB user / sandboxed transactions for all generated SQL, no
  exceptions, even during testing.
- Ground truth for eval questions is source-level (XBRL concept + period +
  dimension), never a raw SQL string or row ID — schemas may be regenerated.
- Commit after each phase completes and passes its own tests. One commit per
  phase, clear message.
- Do not guess on ambiguous XBRL semantics (e.g. which context a dimensionless
  fact defaults to) — flag it, don't silently pick one.
