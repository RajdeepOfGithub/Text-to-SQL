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
