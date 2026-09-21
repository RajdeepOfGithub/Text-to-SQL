"""Token/cost accounting for every OpenAI call made through instructor.

Every instructor client registers track(); each completion (including instructor
retries) is appended to CALLS under the current label, so run_eval can report
per-question and total cost.
"""
from __future__ import annotations

from contextlib import contextmanager

# USD per 1M tokens, gpt-4o-mini list price (input, cached input, output)
PRICES = {"gpt-4o-mini": (0.15, 0.075, 0.60)}

CALLS: list[dict] = []
_label = {"current": None}


@contextmanager
def label(name: str):
    prev, _label["current"] = _label["current"], name
    try:
        yield
    finally:
        _label["current"] = prev


def _on_response(response) -> None:
    u = getattr(response, "usage", None)
    if u is None:
        return
    cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    CALLS.append({"label": _label["current"], "model": getattr(response, "model", ""),
                  "prompt_tokens": u.prompt_tokens, "cached_tokens": cached,
                  "completion_tokens": u.completion_tokens})


def track(client):
    client.on("completion:response", _on_response)
    return client


def cost(calls: list[dict]) -> float:
    total = 0.0
    for c in calls:
        key = next((k for k in PRICES if c["model"].startswith(k)), None)
        if key is None:
            continue
        p_in, p_cached, p_out = PRICES[key]
        total += ((c["prompt_tokens"] - c["cached_tokens"]) * p_in + c["cached_tokens"] * p_cached
                  + c["completion_tokens"] * p_out) / 1e6
    return total


def summarize(calls: list[dict]) -> dict:
    return {"calls": len(calls), "prompt_tokens": sum(c["prompt_tokens"] for c in calls),
            "completion_tokens": sum(c["completion_tokens"] for c in calls), "cost_usd": round(cost(calls), 5)}
