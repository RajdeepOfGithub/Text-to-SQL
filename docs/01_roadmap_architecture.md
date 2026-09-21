# Project 2: Text-to-SQL with Guardrails and Hallucination Detection
## Roadmap & Architecture

---

## 1. What this project is

A natural-language-to-SQL system over SEC XBRL facts — the structured
financial data tagged inside SEC filings, as opposed to Project 1's
unstructured retrieval over filing prose. Same corpus (JPMC 10-K FY2025,
10-Q Q2-2026), different data shape: exact facts with concept/period/unit
instead of retrievable text chunks.

**Why this project exists, concretely:** Project 1's eval surfaced a real,
measured limitation — numeric questions retrieved from flattened or
fragmented tables sometimes bound the wrong value to the wrong period or
segment (see `financial-rag`'s decisions log, Sept 15-16 entries). An XBRL
fact carries its period, unit and dimensions explicitly; there is no column
to misalign and no row to truncate. This project is the structural answer to
that class of question, not a fix bolted onto Project 1.

**One-line pitch (resume/interviews):**
"Built a text-to-SQL system over SEC XBRL data with guardrails blocking 100%
of destructive queries and hallucination detection catching mismatched
SQL-to-question translations."

## 2. Relationship to Project 1

Separate repo, separate eval, separate README. Joined by a thin intent
router: numeric/lookup questions → this project (SQL), narrative/reasoning
questions → Project 1 (RAG). The router is what makes the two-project story
coherent rather than "two unrelated things on a resume."

**What this project does NOT cover:** figures stated only in prose (an
earnings-call remark, an MD&A narrative aside) never become tagged XBRL
facts. Project 1 still owns those. Confirmed empirically in Project 1's own
corpus — roughly a quarter of its 10-K tables and half of its 10-Q tables
carry no iXBRL tagging at all.

## 3. Architecture — four phases

### Phase 1: Extraction (offline, runs once)
```
SEC filing .htm (inline XBRL)
        |
Arelle loads + resolves contexts/dimensions
        |
Extract every ix:nonFraction / ix:nonNumeric fact
        |
   concept, value, unit, period, entity, dimensions
        |
SQLite: facts table, indexed on concept + period
```

### Phase 2: Schema-aware generation + guardrails (built together — safety
is not a layer bolted on after generation, it constrains generation itself)
```
Natural language question
        |
Schema introspection (relevant concepts/periods, not the whole table)
        |
LLM generates SQL (structured output, Pydantic-enforced)
        |
Guardrail check: read-only, row limit, no DDL/DML, query complexity cap
        |
Blocked? --Yes--> reject with reason, log it
        |No
Execute in read-only sandboxed transaction
```

### Phase 3: Hallucination detection
```
Generated SQL + result
        |
Back-translate SQL to a question ("what does this SQL ask?")
        |
Compare back-translation to original question
        |
Result sanity checks (plausible magnitude, correct unit scale, non-null)
        |
Confidence score (combines back-translation match + sanity checks)
        |
Low confidence? --Yes--> flag for review, don't assert silently
```

### Phase 4: Evaluation loop
```
Golden question set, source-level ground truth
   (concept + period + dimension, NEVER a raw SQL string or row id —
   schemas may be regenerated, exactly the lesson from Project 1)
        |
Run eval: execution accuracy, guardrail effectiveness, hallucination
detection rate
        |
Graded, not eyeballed — same discipline as Project 1's grading suite
```

## 4. Tech stack

| Component | Tool | Notes |
|---|---|---|
| Language | Python 3.11+ | |
| XBRL parsing | Arelle (Python API) | Do not hand-roll context/dimension resolution |
| Storage | SQLite | facts table, indexed |
| SQL validation | sqlparse | Syntax + guardrail checks before execution |
| Structured output | Pydantic + instructor | Enforced SQL generation schema |
| LLM | gpt-4o-mini | Same model as Project 1, for consistency |
| Sandbox | Read-only transaction | Non-negotiable, no exceptions during testing |

## 5. Compressed plan (batched prompts, not Project 1's exploratory pace)

### Phase 1 — Extraction
- Arelle-based fact extraction from both existing .htm filings (no re-fetch)
- SQLite schema, loaded and indexed
- Validation against two already-known-correct values from Project 1's own
  manually-verified ground truth (Q2-2026 revenue $57,347M, FY2025
  repurchases $31,640M) as a sanity check, not as eval questions
- **Done when:** both validation facts are extractable via direct query

### Phase 2 — Generation + guardrails
- Schema-aware prompt construction (filtered to relevant concepts, not the
  full fact table)
- Structured SQL generation
- Guardrail middleware: block DDL/DML, enforce row limits, reject deep
  nesting, read-only sandboxed execution
- **Done when:** a natural-language question produces safe, executable SQL
  end-to-end, and a deliberately destructive prompt is blocked

### Phase 3 — Hallucination detection
- SQL-to-question back-translation
- Result sanity checks (magnitude, unit, non-null)
- Confidence scoring
- **Done when:** a deliberately mismatched SQL/question pair is caught

### Phase 4 — Eval
- Source-level golden question set (concept/period/dimension ground truth)
- Frozen baseline, graded suite (reuse Project 1's grading approach where
  it transfers — e.g. period/scope correctness are the same underlying
  problem in a different data shape)
- **Done when:** real numbers exist — this is the resume data

## 6. Definition of done (per phase)

- **Phase 1 done when:** facts are queryable and both validation values
  resolve correctly
- **Phase 2 done when:** safe SQL generation works end-to-end and unsafe SQL
  is provably blocked
- **Phase 3 done when:** a known-bad SQL/question mismatch is caught, not
  just a happy-path demo
- **Phase 4 done when:** graded numbers exist, not eyeballed impressions

## 7. Known constraints going in (learned from Project 1, applied here from day one)

- **Ground truth must be source-level, never implementation-level.** Project
  1 lost two days to chunk-ID-based ground truth becoming unusable after a
  refactor. Here: never grade against a raw SQL string or a row ID — grade
  against concept + period + dimension.
- **Don't guess on ambiguous XBRL semantics.** Which context a dimensionless
  fact defaults to, or how a restated prior-period figure should be handled,
  gets flagged rather than silently resolved one way.
- **Full output over truncated snippets, always.** Three separate wrong
  conclusions in Project 1 traced back to reading a truncated print
  statement. Same discipline applies here.
- **One variable per experiment.** Project 1's reranker/crowding/scope
  failures only became legible once changes were isolated one at a time.
