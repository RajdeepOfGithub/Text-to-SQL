
# Corpus

Not committed. Same JPMorgan Chase filings as `../RAG/financial-rag` (10-K
FY2025, 10-Q Q2-2026), fetched to `data/raw/jpmc/`. Text-to-SQL also needs
the XBRL taxonomy linkbase files (`.xsd`, `_cal.xml`, `_def.xml`, `_lab.xml`,
`_pre.xml`) alongside each filing so Arelle can resolve contexts and
dimensions — the RAG project doesn't need these since it never parses XBRL.

SEC requires a User-Agent header identifying the requester, or it rejects the
request.

```
curl.exe -A "your.email@example.com" -o data/raw/jpmc/jpmc_10k_2025.htm "https://www.sec.gov/Archives/edgar/data/19617/000162828026008131/jpm-20251231.htm"
curl.exe -A "your.email@example.com" -o data/raw/jpmc/jpm-20251231.xsd "https://www.sec.gov/Archives/edgar/data/19617/000162828026008131/jpm-20251231.xsd"
curl.exe -A "your.email@example.com" -o data/raw/jpmc/jpm-20251231_cal.xml "https://www.sec.gov/Archives/edgar/data/19617/000162828026008131/jpm-20251231_cal.xml"
curl.exe -A "your.email@example.com" -o data/raw/jpmc/jpm-20251231_def.xml "https://www.sec.gov/Archives/edgar/data/19617/000162828026008131/jpm-20251231_def.xml"
curl.exe -A "your.email@example.com" -o data/raw/jpmc/jpm-20251231_lab.xml "https://www.sec.gov/Archives/edgar/data/19617/000162828026008131/jpm-20251231_lab.xml"
curl.exe -A "your.email@example.com" -o data/raw/jpmc/jpm-20251231_pre.xml "https://www.sec.gov/Archives/edgar/data/19617/000162828026008131/jpm-20251231_pre.xml"

curl.exe -A "your.email@example.com" -o data/raw/jpmc/jpmc_10q_q2_2026.htm "https://www.sec.gov/Archives/edgar/data/19617/000162828026054343/jpm-20260630.htm"
curl.exe -A "your.email@example.com" -o data/raw/jpmc/jpm-20260630.xsd "https://www.sec.gov/Archives/edgar/data/19617/000162828026054343/jpm-20260630.xsd"
curl.exe -A "your.email@example.com" -o data/raw/jpmc/jpm-20260630_cal.xml "https://www.sec.gov/Archives/edgar/data/19617/000162828026054343/jpm-20260630_cal.xml"
curl.exe -A "your.email@example.com" -o data/raw/jpmc/jpm-20260630_def.xml "https://www.sec.gov/Archives/edgar/data/19617/000162828026054343/jpm-20260630_def.xml"
curl.exe -A "your.email@example.com" -o data/raw/jpmc/jpm-20260630_lab.xml "https://www.sec.gov/Archives/edgar/data/19617/000162828026054343/jpm-20260630_lab.xml"
curl.exe -A "your.email@example.com" -o data/raw/jpmc/jpm-20260630_pre.xml "https://www.sec.gov/Archives/edgar/data/19617/000162828026054343/jpm-20260630_pre.xml"
```

Browser print-to-PDF silently truncates these filings. Use curl.

Expected sizes: 10-K htm ~12.9MB, 10-Q htm ~11.5MB; taxonomy files range
~200KB-2.8MB each.

After fetching, regenerate the SQLite facts DB with `python -m
src.xbrl_extract` (see Phase 1 in project docs) — `data/xbrl_facts.db` is
also gitignored and rebuilt from the raw filings, not committed.
