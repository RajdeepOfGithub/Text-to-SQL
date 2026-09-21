"""Combine back-translation + sanity checks into one confidence score.

WEIGHTS: first pass, set by judgment and not tuned or calibrated on data.
  back_translation  0.6  - the only check that tests whether the SQL answers *this* question
  magnitude         0.2  - value plausible against the concept's own history
  unit_scale        0.2  - figure in the answer text matches the fact's value and unit
  non_null          gate - an empty/null result scores 0 regardless of the rest
Component scores: pass = 1.0, fail = 0.0, insufficient_history / not_applicable = 0.5
(no evidence either way, so neither rewarded nor penalized).

FLAGGED if any of:
  - back-translation mismatch on any aspect (metric, period, scope)
  - any sanity check hard-fails
  - score < FLAG_THRESHOLD
The hard rules carry most of the weight. With binary back-translation, a mismatch
alone caps the score at 0.4, so the threshold is a backstop, not a tuned boundary.

Deliberately NOT an input: the answer's ambiguity status or its list of alternatives.
Several valid measures existing (e.g. repurchases: cash paid vs. treasury-stock cost)
is a disclosure issue handled in sql_generate, not a hallucination. A correctly
disclosed headline pick should score as high as an unambiguous answer.

Not known yet: whether this score separates right from wrong answers beyond the
handful of constructed cases in scripts/eval_confidence.py. Project 1's scorer
didn't; treat this one as unvalidated until tested on a real question set.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from back_translate import BackTranslationResult, check  # noqa: E402
from sanity_check import SanityReport, run_sanity_checks  # noqa: E402
from sql_generate import Answer  # noqa: E402

WEIGHTS = {"back_translation": 0.6, "magnitude": 0.2, "unit_scale": 0.2}
STATUS_SCORE = {"pass": 1.0, "fail": 0.0, "insufficient_history": 0.5, "not_applicable": 0.5}
FLAG_THRESHOLD = 0.7

BackTranslator = Callable[[str, str, list, list], BackTranslationResult]


@dataclass
class ConfidenceReport:
    score: float
    flagged: bool
    reasons: list[str]
    components: dict[str, float]
    back_translation: BackTranslationResult | None
    sanity: SanityReport | None
    notes: list[str] = field(default_factory=list)


def assess(answer: Answer, back_translator: BackTranslator = check) -> ConfidenceReport:
    if answer.status in ("blocked", "needs_clarification", "not_available"):
        # Nothing was answered, so there's nothing to score. Blocking and asking are the safe outcomes.
        return ConfidenceReport(1.0, False, [], {}, None, None, [f"not scored: status={answer.status}"])

    m = answer.measure
    sanity = run_sanity_checks(
        value=m.value_numeric if m else None, rows=answer.rows,
        concept=m.concept if m else None, dimensions_json=m.dimensions_json if m else None,
        unit=m.unit if m else None, period_start=m.period_start if m else None,
        period_end=m.period_end if m else None, answer_text=answer.text,
    )
    bt = back_translator(answer.question, answer.sql, answer.columns, answer.rows) if answer.rows else None

    components = {
        "back_translation": bt.score if bt else 0.0,
        "magnitude": STATUS_SCORE[sanity.get("magnitude").status],
        "unit_scale": STATUS_SCORE[sanity.get("unit_scale").status],
    }
    score = sum(WEIGHTS[k] * v for k, v in components.items())
    if sanity.get("non_null").status == "fail":
        score = 0.0

    reasons = []
    if bt and not bt.matches:
        reasons.append(f"back-translation mismatch on {', '.join(bt.mismatched_aspects)}: "
                       f"SQL answers {bt.back_translated_question!r} ({bt.judgment.explanation})")
    reasons += [f"sanity {c.name}: {c.detail}" for c in sanity.hard_failures]
    if score < FLAG_THRESHOLD and not reasons:
        reasons.append(f"score {score:.2f} below {FLAG_THRESHOLD}")
    return ConfidenceReport(round(score, 3), bool(reasons), reasons, components, bt, sanity)
