"""Result sanity checks: non-null, plausible magnitude, unit/scale consistency.

Magnitude is judged against the concept's own history in the facts table,
never a hardcoded bound. The answered fact comes from that same table, so
it's excluded (leave-one-out), and comparisons use only facts with the same
dimensions, unit and period length (a quarter vs quarters, a year vs years,
instants vs instants). Otherwise a quarterly figure would be compared with
annual ones.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from statistics import median
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xbrl_db import connect_readonly  # noqa: E402

MAGNITUDE_FACTOR = 10.0  # plausible if within [min/10, max*10] of comparable history; first pass, untuned

Status = Literal["pass", "fail", "insufficient_history", "not_applicable"]


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str


@dataclass
class SanityReport:
    checks: list[CheckResult] = field(default_factory=list)

    def get(self, name: str) -> CheckResult:
        return next(c for c in self.checks if c.name == name)

    @property
    def hard_failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == "fail"]


def _duration_class(start: str | None, end: str | None) -> str:
    if start is None:
        return "instant"
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    for name, lo, hi in (("quarter", 80, 100), ("half", 170, 195), ("nine_months", 260, 285), ("year", 355, 375)):
        if lo <= days <= hi:
            return name
    return f"{days}d"


def check_non_null(value: float | None, rows: list) -> CheckResult:
    if not rows:
        return CheckResult("non_null", "fail", "query returned no rows")
    if value is None:
        return CheckResult("non_null", "fail", "result value is null")
    return CheckResult("non_null", "pass", f"{len(rows)} row(s), value present")


def check_magnitude(value: float, concept: str, dimensions_json: str, unit: str | None,
                    period_start: str | None, period_end: str) -> CheckResult:
    cls = _duration_class(period_start, period_end)
    conn = connect_readonly()
    try:
        rows = conn.execute("""
            SELECT DISTINCT period_start, period_end, value_numeric FROM facts
            WHERE concept = ? AND dimensions_json = ? AND unit IS ? AND value_numeric IS NOT NULL
              AND NOT (period_start IS ? AND period_end = ?)
        """, (concept, dimensions_json, unit, period_start, period_end)).fetchall()
    finally:
        conn.close()
    history = [v for s, e, v in rows if _duration_class(s, e) == cls]
    if not history:
        return CheckResult("magnitude", "insufficient_history",
                           f"no other {cls} facts for {concept} with these dimensions to compare against")
    lo, hi = min(abs(v) for v in history), max(abs(v) for v in history)
    problems = []
    if (all(v > 0 for v in history) and value < 0) or (all(v < 0 for v in history) and value > 0):
        problems.append(f"sign differs from all {len(history)} comparable historical values")
    if abs(value) < lo / MAGNITUDE_FACTOR or abs(value) > hi * MAGNITUDE_FACTOR:
        problems.append(f"|value| {abs(value):,.0f} outside [{lo / MAGNITUDE_FACTOR:,.0f}, {hi * MAGNITUDE_FACTOR:,.0f}]")
    detail = f"{len(history)} comparable {cls} value(s), median {median(history):,.0f}"
    return CheckResult("magnitude", "fail" if problems else "pass", "; ".join(problems) or detail)


_FIGURE = re.compile(r"(-)?\$?(-)?([\d,]+(?:\.\d+)?)\s*([KMB])?\b(\s*(?:per share|shares))?", re.I)
_SCALE = {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}


def parse_stated_figure(text: str) -> tuple[float, str] | None:
    """First figure in the answer text -> (value in base units, unit family it implies)."""
    m = _FIGURE.search(text)
    if not m:
        return None
    raw = m.group(0)
    sign = -1 if (m.group(1) or m.group(2)) else 1
    value = sign * float(m.group(3).replace(",", "")) * _SCALE[(m.group(4) or "").upper()]
    suffix = (m.group(5) or "").strip().lower()
    if suffix == "per share":
        family = "iso4217:USD/xbrli:shares"
    elif suffix == "shares":
        family = "xbrli:shares"
    elif "$" in raw:
        family = "iso4217:USD"
    else:
        family = "unitless"
    return value, family


def check_unit_scale(value: float, unit: str | None, answer_text: str) -> CheckResult:
    parsed = parse_stated_figure(answer_text)
    if parsed is None:
        return CheckResult("unit_scale", "fail", "answer text states no figure")
    stated, family = parsed
    expected_family = unit if unit in ("iso4217:USD", "xbrli:shares", "iso4217:USD/xbrli:shares") else "unitless"
    if family != expected_family:
        return CheckResult("unit_scale", "fail", f"answer implies {family}, fact unit is {unit}")
    # the display rounds (e.g. to whole millions); allow half a display step
    step = 1e6 if abs(value) >= 1e6 else 1.0
    tolerance = max(step / 2, abs(value) * 0.005)
    if abs(stated - value) > tolerance:
        return CheckResult("unit_scale", "fail", f"answer states {stated:,.2f}, fact value is {value:,.2f} ({unit})")
    return CheckResult("unit_scale", "pass", f"answer figure {stated:,.0f} matches fact ({unit})")


def run_sanity_checks(*, value: float | None, rows: list, concept: str | None, dimensions_json: str | None,
                      unit: str | None, period_start: str | None, period_end: str | None,
                      answer_text: str) -> SanityReport:
    report = SanityReport([check_non_null(value, rows)])
    if value is None or concept is None or period_end is None:
        report.checks += [CheckResult("magnitude", "not_applicable", "no single-value result"),
                          CheckResult("unit_scale", "not_applicable", "no single-value result")]
        return report
    report.checks.append(check_magnitude(value, concept, dimensions_json or "{}", unit, period_start, period_end))
    report.checks.append(check_unit_scale(value, unit, answer_text))
    return report
