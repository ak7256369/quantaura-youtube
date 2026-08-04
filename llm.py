"""Free-tier LLM access with automatic provider fallback.

Two providers are configured (Gemini, then Groq). A quota wall or an outage at
one is a routine event on free tiers, so the pipeline walks the list rather
than failing the day. Both are asked for raw JSON and their output is parsed
defensively — a model that wraps its answer in a markdown fence is normal, not
an error.
"""
from __future__ import annotations

import json
import re

from common import PipelineAbort, config, env, log, post_json


class LLMUnavailable(Exception):
    """Every configured provider refused or failed."""


def _call_gemini(spec: dict, prompt: str, temperature: float) -> str:
    key = env(spec["api_key_env"])
    if not key:
        raise LLMUnavailable(f"{spec['api_key_env']} not set")
    url = spec["endpoint"].format(model=spec["model"])
    res = post_json(
        url,
        {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
            },
        },
        headers={"x-goog-api-key": key},
        label="gemini",
    )
    if not res.ok:
        raise LLMUnavailable(f"gemini: {res.error}")
    try:
        cands = res.data["candidates"]
        if not cands:
            raise LLMUnavailable("gemini returned no candidates (likely a safety block)")
        return "".join(p.get("text", "") for p in cands[0]["content"]["parts"])
    except LLMUnavailable:
        raise
    except Exception as e:                                       # noqa: BLE001
        raise LLMUnavailable(f"gemini: unexpected response shape ({e})")


def _call_groq(spec: dict, prompt: str, temperature: float) -> str:
    key = env(spec["api_key_env"])
    if not key:
        raise LLMUnavailable(f"{spec['api_key_env']} not set")
    res = post_json(
        spec["endpoint"],
        {
            "model": spec["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        },
        headers={"Authorization": f"Bearer {key}"},
        label="groq",
    )
    if not res.ok:
        raise LLMUnavailable(f"groq: {res.error}")
    try:
        return res.data["choices"][0]["message"]["content"]
    except Exception as e:                                       # noqa: BLE001
        raise LLMUnavailable(f"groq: unexpected response shape ({e})")


_CALLERS = {"gemini": _call_gemini, "groq": _call_groq}


def extract_json(text: str) -> dict:
    """Parse a JSON object out of a model response.

    Handles the three shapes that actually occur: clean JSON, a ```json fenced
    block, and prose wrapped around an object.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        return json.loads(fence.group(1))

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError(f"no JSON object found in response: {text[:200]}")


def complete_json(prompt: str, *, temperature: float | None = None,
                  label: str = "llm") -> tuple[dict, str]:
    """Ask each provider in turn for a JSON object.

    Returns (parsed, provider_name). Raises PipelineAbort only when every
    provider has been exhausted.
    """
    cfg = config()["script"]
    temperature = cfg["temperature"] if temperature is None else temperature
    attempts = cfg["max_attempts_per_provider"]
    failures: list[str] = []

    for spec in cfg["providers"]:
        caller = _CALLERS.get(spec["name"])
        if caller is None:
            log.warning(f"  Unknown LLM provider '{spec['name']}' in config — skipping")
            continue
        for attempt in range(1, attempts + 1):
            try:
                raw = caller(spec, prompt, temperature)
                return extract_json(raw), spec["name"]
            except LLMUnavailable as e:
                failures.append(str(e))
                log.warning(f"  {label}: {e}")
                break                      # provider-level problem: move on
            except Exception as e:         # noqa: BLE001 — malformed JSON: retry
                failures.append(f"{spec['name']}: {e}")
                log.warning(f"  {label}: {spec['name']} returned unparseable JSON "
                            f"({e}) — attempt {attempt}/{attempts}")

    raise PipelineAbort(f"All LLM providers failed for {label}: {'; '.join(failures[-4:])}")
