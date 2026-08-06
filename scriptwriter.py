"""Turn the day's facts into a narration script.

The LLM is given a deliberately narrow job: phrase facts it is handed. It never
computes a statistic, never sees the raw candle series, and its output is
schema-validated before anything downstream touches it. Free-form text from a
model is not trusted anywhere in this pipeline.
"""
from __future__ import annotations

import json

from common import PipelineAbort, config, log, prompt
from llm import complete_json

SECTIONS = ("hook", "call", "why", "score")


def build_facts(snapshot: dict, score: dict) -> dict:
    """The ONLY view of the world the scriptwriter gets.

    Kept small and pre-formatted on purpose: anything the model has to compute
    is something it can get wrong, so every figure it might want to say is
    already a string or a rounded number here.
    """
    last = score.get("last_resolved") or {}
    fg = snapshot.get("fear_greed") or {}

    facts = {
        "date": snapshot["date"],
        "asset": snapshot["asset_label"],
        "symbol": snapshot["symbol"],

        "signal": snapshot["signal"],
        "confidence_pct": snapshot.get("confidence"),
        "gated": snapshot.get("gated", False),
        "signal_before_gate": snapshot.get("signal_raw"),
        "confidence_threshold_pct": snapshot.get("confidence_threshold_pct"),
        "probability_split": snapshot.get("breakdown") or {},
        "per_model_votes": snapshot.get("per_model") or {},
        "forecast_horizon_hours": snapshot.get("label_horizon_hours"),

        "price": snapshot["price_str"],
        "change_24h_pct": snapshot.get("change_24h_pct"),
        "change_7d_pct": snapshot.get("change_7d_pct"),
        "fear_greed_value": fg.get("value"),
        "fear_greed_label": fg.get("classification"),

        "has_history": bool(score.get("resolved_calls")),
        "resolved_calls": score.get("resolved_calls"),
        "hits": score.get("hits"),
        "misses": score.get("misses"),
        "accuracy_pct": score.get("accuracy_pct"),
        "accuracy_30d_pct": score.get("accuracy_30d_pct"),
        "streak_kind": (score.get("streak") or {}).get("kind"),
        "streak_length": (score.get("streak") or {}).get("length"),
        "last_resolved": {
            "date": last.get("date"),
            "signal": last.get("signal"),
            "change_pct": last.get("change_pct"),
            "correct": last.get("correct"),
        } if last else None,
        "flat_band_pct": score.get("flat_band_pct"),
    }
    # Null fields are dropped rather than sent: the prompt forbids mentioning a
    # null topic, and an absent key is a stronger signal than a null value.
    return {k: v for k, v in facts.items() if v is not None}


# ── Validation ────────────────────────────────────────────────────────────────

def _as_sentences(value, field: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ValueError(f"'{field}' must be a non-empty list of sentences")
    out = []
    for s in value:
        if not isinstance(s, str) or not s.strip():
            raise ValueError(f"'{field}' contains a non-string or empty sentence")
        out.append(" ".join(s.split()))
    return out


def validate(script: dict) -> dict:
    """Normalise and hard-check the model's JSON. Raises ValueError to trigger
    a regeneration; the caller decides when to give up."""
    if not isinstance(script, dict):
        raise ValueError("script is not a JSON object")

    for field in ("title", "description"):
        if not isinstance(script.get(field), str) or not script[field].strip():
            raise ValueError(f"missing '{field}'")

    sections_in = script.get("sections")
    if not isinstance(sections_in, dict):
        raise ValueError("missing 'sections' object")
    sections = {s: _as_sentences(sections_in.get(s), f"sections.{s}") for s in SECTIONS}

    overlays_in = script.get("overlays") or {}
    why_overlay = overlays_in.get("why")
    if isinstance(why_overlay, str):
        why_overlay = [why_overlay]
    overlays = {
        "hook": str(overlays_in.get("hook") or "")[:40],
        "call": str(overlays_in.get("call") or "")[:34],
        "why": [str(x)[:36] for x in (why_overlay or [])][:3],
        "score": str(overlays_in.get("score") or "")[:36],
    }

    tags = [str(t).lstrip("#").strip().lower()
            for t in (script.get("tags") or []) if str(t).strip()]
    tags = list(dict.fromkeys(tags + config()["upload"]["base_tags"]))[:15]

    words = sum(len(s.split()) for sec in sections.values() for s in sec)
    if words < 90:
        raise ValueError(f"narration too short ({words} words)")
    if words > 260:
        raise ValueError(f"narration too long ({words} words)")

    return {
        "title": " ".join(script["title"].split())[:95],
        "description": script["description"].strip(),
        "tags": tags,
        "sections": sections,
        "overlays": overlays,
        "word_count": words,
    }


def all_sentences(script: dict) -> list[str]:
    # Iterates the script's own sections rather than the daily SECTIONS tuple:
    # the fact-checker runs over weekly scripts too, whose sections differ.
    return [s for sec in script["sections"].values() for s in sec]


# ── Entry point ───────────────────────────────────────────────────────────────

def write(facts: dict, *, feedback: list[str] | None = None) -> dict:
    """Generate one validated script. `feedback` carries fact-check problems
    from a previous attempt so the retry is corrective rather than random."""
    parts = [prompt("daily_script.md"), json.dumps(facts, indent=2, ensure_ascii=False)]

    if feedback:
        parts.append(
            "\n## CORRECTIONS REQUIRED\n"
            "Your previous attempt was rejected by the fact-checker. Fix every point "
            "below. Do not repeat the same claims.\n"
            + "\n".join(f"- {f}" for f in feedback)
        )

    full = "\n".join(parts)
    last_err = None

    # Two shots at producing schema-valid JSON before escalating; the provider
    # fallback inside complete_json handles the provider-level failures.
    for attempt in range(1, 3):
        raw, provider = complete_json(full, label="scriptwriter")
        try:
            script = validate(raw)
            script["provider"] = provider
            log.info(f"  Script written by {provider}: \"{script['title']}\" "
                     f"({script['word_count']} words)")
            return script
        except ValueError as e:
            last_err = str(e)
            log.warning(f"  Script rejected by schema ({e}) — attempt {attempt}/2")
            full = "\n".join(parts + [
                f"\n## YOUR PREVIOUS OUTPUT WAS INVALID\n{last_err}\nReturn valid JSON "
                f"matching the specified shape exactly."])

    raise PipelineAbort(f"Scriptwriter could not produce a valid script: {last_err}")
