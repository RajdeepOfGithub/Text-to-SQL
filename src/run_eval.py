"""Run the golden questions through the full pipeline and capture everything.

introspect -> generate (LLM) -> guardrails -> sandboxed execute -> disclosure
-> back-translation + sanity checks -> confidence. Per question, the full
intermediate state goes to <out>/runs/{id}.json. No grading here (see grade.py).

Usage (from the project root):
    python src/run_eval.py                    # cost estimate, then stops
    python src/run_eval.py --confirm          # writes baseline/v1/runs/
    python src/run_eval.py --confirm --out baseline/v1.1 --ids e06 e07
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
import usage  # noqa: E402
from confidence import assess  # noqa: E402
from schema_introspect import introspect  # noqa: E402
from sql_generate import MODEL, answer_question  # noqa: E402
from xbrl_db import DB_PATH, connect_readonly  # noqa: E402
from xbrl_extract import ROOT  # noqa: E402

QUESTIONS_PATH = ROOT / "data" / "eval_questions.jsonl"
DEFAULT_OUT = ROOT / "baseline" / "v1"

# Estimate from Phase 2/3 runs: plan ~7k prompt / 400 completion tokens, back-translation
# ~1k / 60, judge ~0.6k / 200. Upper bound allows every call to use all instructor retries.
EST_TOKENS_PER_QUESTION = (8_600, 660)
MAX_ATTEMPTS = 3


def read_questions(path: Path = QUESTIONS_PATH) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def print_cost_estimate(n: int) -> None:
    p_in, _, p_out = usage.PRICES[MODEL]
    per_q = (EST_TOKENS_PER_QUESTION[0] * p_in + EST_TOKENS_PER_QUESTION[1] * p_out) / 1e6
    print(f"{n} questions x ~3 {MODEL} calls (plan, back-translate, judge)")
    print(f"estimated cost: ${n * per_q:.3f} typical, ${n * per_q * MAX_ATTEMPTS:.3f} if every call retries twice")


def jsonable(obj):
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    return obj


def db_fingerprint() -> dict:
    conn = connect_readonly()
    try:
        n, total = conn.execute("SELECT COUNT(*), SUM(value_numeric) FROM facts").fetchone()
        (n_concepts,) = conn.execute("SELECT COUNT(*) FROM concepts").fetchone()
        (n_schema,) = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    finally:
        conn.close()
    st = DB_PATH.stat()
    return {"facts": n, "sum_value": total, "concepts": n_concepts, "schema_objects": n_schema,
            "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def run_one(q: dict) -> dict:
    record = {"id": q["id"], "category": q["category"], "question": q["question"], "expected": q["expected"]}
    start_calls = len(usage.CALLS)
    before = db_fingerprint()
    t0 = time.perf_counter()
    try:
        s = introspect(q["question"])
        record["schema_slice"] = {
            "periods": jsonable(s.periods),
            "candidates": [{"concept": c.concept, "label": c.label, "score": c.score,
                            "variants_shown": len(c.variants), "total_variants": c.total_variants}
                           for c in s.candidates],
        }
        with usage.label(f"{q['id']}:generate"):
            answer = answer_question(q["question"])
        record["answer"] = jsonable(answer)
        record["executed"] = answer.status in ("answered", "no_result")
        with usage.label(f"{q['id']}:verify"):
            report = assess(answer)
        record["confidence"] = jsonable(report)
        record["error"] = None
    except Exception as e:  # recorded, not raised: a crash is a result for this question
        record["error"] = {"type": type(e).__name__, "message": str(e)[-2000:], "traceback": traceback.format_exc()[-4000:]}
    record["elapsed_s"] = round(time.perf_counter() - t0, 2)
    after = db_fingerprint()
    record["db_before"], record["db_after"] = before, after
    record["db_unchanged"] = before == after
    record["usage"] = usage.summarize(usage.CALLS[start_calls:])
    return record


def git_state() -> dict:
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the golden questions and capture full pipeline output.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--ids", nargs="+")
    ap.add_argument("--confirm", action="store_true", help="required; without it only the cost estimate prints")
    args = ap.parse_args()

    questions = read_questions()
    if args.ids:
        questions = [q for q in questions if q["id"] in args.ids]
    print_cost_estimate(len(questions))
    if not args.confirm:
        print("\nNo API calls made. Re-run with --confirm to execute.")
        raise SystemExit(1)

    runs = args.out / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    for q in questions:
        rec = run_one(q)
        (runs / f"{q['id']}.json").write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
        status = rec["error"]["type"] if rec["error"] else rec["answer"]["status"]
        print(f"{q['id']} {q['category']:18s} {status:20s} ${rec['usage']['cost_usd']:.4f}  {rec['elapsed_s']}s")

    totals = usage.summarize(usage.CALLS)
    config = {"version": args.out.name, "timestamp": datetime.now(timezone.utc).isoformat(), "git": git_state(),
              "model": MODEL, "questions": len(questions), "usage": totals}
    (args.out / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"\ntotal: {totals['calls']} calls, {totals['prompt_tokens']:,} prompt + "
          f"{totals['completion_tokens']:,} completion tokens, ${totals['cost_usd']:.4f} -> {args.out}")


if __name__ == "__main__":
    main()
