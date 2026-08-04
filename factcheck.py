"""The gate between a generated script and a published video.

Three independent checks, cheapest and most certain first:

  1. Banned phrases   — deterministic regex. Advice and hype never ship,
                        regardless of what any model returns.
  2. Numeric grounding — every digit spoken must trace back to a fact. This is
                        the check that actually catches hallucination, because
                        an invented statistic is almost always an invented
                        *number*, and arithmetic is not something we let the
                        model do.
  3. Adversarial LLM  — catches invented *claims*, spin, and tone violations
                        that no regex can see.

Any failure returns problems to the caller, which regenerates. Two failed
regenerations end the day with no upload.
"""
from __future__ import annotations

import json
import re

from common import config, log, prompt
from llm import complete_json
from scriptwriter import all_sentences

# Bare small integers are ordinary English ("one of the four models", "24
# hours"), not statistics. Anything larger has to be traceable to a fact.
_SAFE_INTEGERS = set(range(0, 13))
_NUMBER_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


# ── 1. Banned phrases ─────────────────────────────────────────────────────────

def check_phrases(script: dict) -> list[str]:
    text = " ".join(all_sentences(script) + [script["title"], script["description"]])
    lowered = text.lower()
    problems = []
    for phrase in config()["factcheck"]["banned_phrases"]:
        # Word-boundary match so "guarantee" does not fire on "guaranteeing"
        # inside a legitimate quote, and "10x" does not fire inside "110x".
        if re.search(rf"(?<![\w-]){re.escape(phrase.lower())}(?![\w-])", lowered):
            problems.append(f"Banned phrase used: '{phrase}'. Remove it entirely.")
    return problems


# ── 2. Numeric grounding ──────────────────────────────────────────────────────

def _numbers_in(value) -> set[float]:
    """Every number reachable in the facts, at several roundings.

    The script is allowed to say 57 when the fact is 57.1, or 62,145 when the
    fact is 62145.37 — those are faithful readings. It is not allowed to
    produce a number that appears nowhere.
    """
    found: set[float] = set()

    def add(n: float) -> None:
        for v in (n, abs(n)):
            found.add(round(v, 2))
            found.add(round(v, 1))
            found.add(float(round(v)))

    def walk(v) -> None:
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            add(float(v))
        elif isinstance(v, str):
            for tok in _NUMBER_RE.findall(v):
                try:
                    add(float(tok.replace(",", "")))
                except ValueError:
                    pass
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(value)
    return found


def check_numbers(script: dict, facts: dict) -> list[str]:
    allowed = _numbers_in(facts)
    problems = []
    seen: set[float] = set()

    for sentence in all_sentences(script):
        for tok in _NUMBER_RE.findall(sentence):
            try:
                n = float(tok.replace(",", ""))
            except ValueError:
                continue
            if n in seen:
                continue
            if n.is_integer() and int(n) in _SAFE_INTEGERS:
                continue
            if any(round(n, d) in allowed for d in (0, 1, 2)):
                continue
            seen.add(n)
            problems.append(
                f"The number {tok} in \"{sentence.strip()[:90]}\" does not appear in the "
                f"source data. Use only figures given in FACTS, or drop the claim.")
    return problems


# ── 3. Adversarial LLM pass ───────────────────────────────────────────────────

def check_llm(script: dict, facts: dict) -> list[str]:
    payload = (
        prompt("factcheck.md")
        + "\n\n## FACTS\n"
        + json.dumps(facts, indent=2, ensure_ascii=False)
        + "\n\n## SCRIPT\n"
        + json.dumps({"title": script["title"],
                      "description": script["description"],
                      "sections": script["sections"]}, indent=2, ensure_ascii=False)
    )
    # Low temperature: this is a judgement, not a creative task.
    verdict, provider = complete_json(payload, temperature=0.0, label="factcheck")

    if verdict.get("ok") is True and not verdict.get("problems"):
        log.info(f"  Fact-check passed ({provider})")
        return []

    problems = []
    for p in verdict.get("problems") or []:
        if isinstance(p, dict):
            quote = str(p.get("quote", ""))[:80]
            issue = str(p.get("issue", "unspecified"))
            problems.append(f"\"{quote}\" — {issue}")
        else:
            problems.append(str(p))
    # A verdict of not-ok with no listed problem is itself a failure: an
    # unexplained rejection must not silently pass.
    return problems or ["Fact-checker rejected the script without listing a reason."]


# ── Entry point ───────────────────────────────────────────────────────────────

def verify(script: dict, facts: dict) -> list[str]:
    """Returns a list of problems; empty means the script may be published."""
    problems = check_phrases(script)
    problems += check_numbers(script, facts)

    if problems:
        # The deterministic checks already failed, so spending an LLM call to
        # confirm it adds cost and nothing else.
        log.warning(f"  Fact-check failed on deterministic rules ({len(problems)} problems)")
        for p in problems[:5]:
            log.warning(f"    - {p}")
        return problems

    problems = check_llm(script, facts)
    if problems:
        log.warning(f"  Fact-check failed on review ({len(problems)} problems)")
        for p in problems[:5]:
            log.warning(f"    - {p}")
    return problems
