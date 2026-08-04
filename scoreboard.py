"""The public accuracy record — the reason this channel is worth watching.

Design rules that keep the scoreboard trustworthy:
  * A call is written down BEFORE its outcome is knowable, and never edited.
  * Grading is arithmetic over committed rows, not a model output. The LLM
    receives these numbers as input and is forbidden from computing its own.
  * Losses are graded the same way as wins and are shown with equal prominence.

State lives in two files under channel/state/, committed to the `channel-data`
branch after every run:
    predictions.jsonl  — append-only log, one call per line
    scoreboard.json    — derived summary, safe to delete and rebuild
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import STATE_DIR, config, get_json, log, write_json

LOG_PATH = STATE_DIR / "predictions.jsonl"
SUMMARY_PATH = STATE_DIR / "scoreboard.json"


# ── Log I/O ───────────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    rows = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            import json
            rows.append(json.loads(line))
        except Exception:                                        # noqa: BLE001
            log.warning(f"  Skipping malformed scoreboard row: {line[:80]}")
    return rows


def _save(rows: list[dict]) -> None:
    import json
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOG_PATH.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                   encoding="utf-8")
    tmp.replace(LOG_PATH)


# ── Recording ─────────────────────────────────────────────────────────────────

def record(snapshot: dict) -> None:
    """Append today's call. Idempotent: re-running the pipeline on the same UTC
    date updates that row in place rather than double-counting it."""
    rows = _load()
    entry = {
        "date": snapshot["date"],
        "made_at": snapshot["generated_at"],
        "made_at_ms": int(datetime.fromisoformat(snapshot["generated_at"]).timestamp() * 1000),
        "symbol": snapshot["symbol"],
        "signal": snapshot["signal"],
        "signal_raw": snapshot.get("signal_raw"),
        "gated": snapshot.get("gated", False),
        "confidence": snapshot.get("confidence"),
        "price": snapshot["price"],
        "horizon_hours": config()["scoring"]["horizon_hours"],
        "flat_band_pct": config()["scoring"]["flat_band_pct"],
        "resolved": False,
    }
    for i, r in enumerate(rows):
        if r.get("date") == entry["date"]:
            # Preserve an already-graded outcome — a same-day re-run must never
            # rewrite history that has since been resolved.
            if r.get("resolved"):
                log.info(f"  {entry['date']} already recorded and resolved — left untouched")
                return
            rows[i] = entry
            _save(rows)
            log.info(f"  Updated today's call: {entry['signal']}")
            return
    rows.append(entry)
    _save(rows)
    log.info(f"  Recorded call: {entry['signal']} @ {entry['confidence']}%")


# ── Grading ───────────────────────────────────────────────────────────────────

def _price_at(symbol: str, ms: int) -> float | None:
    """Close of the 1-minute candle covering `ms`."""
    res = get_json(config()["market"]["klines_url"],
                   params={"symbol": symbol, "interval": "1m", "startTime": ms, "limit": 1},
                   retries=2, label="binance/price_at")
    if not res.ok or not res.data:
        return None
    try:
        return float(res.data[0][4])
    except Exception:                                            # noqa: BLE001
        return None


def _grade(signal: str, change_pct: float, band: float) -> bool:
    """BUY wants a rise beyond the flat band, SELL a fall, HOLD neither."""
    if signal == "BUY":
        return change_pct > band
    if signal == "SELL":
        return change_pct < -band
    return abs(change_pct) <= band


def resolve_due() -> int:
    """Grade every call whose horizon has elapsed. Returns how many were graded."""
    rows = _load()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    graded = 0

    for r in rows:
        if r.get("resolved"):
            continue
        due_ms = r["made_at_ms"] + r.get("horizon_hours", 24) * 3600 * 1000
        if now_ms < due_ms:
            continue

        then = _price_at(r["symbol"], due_ms)
        if then is None:
            log.warning(f"  Could not price {r['date']} at horizon — will retry tomorrow")
            continue

        change = round((then - r["price"]) / r["price"] * 100, 2)
        band = r.get("flat_band_pct", 1.0)
        r.update({
            "resolved": True,
            "resolved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "price_then": then,
            "change_pct": change,
            "correct": _grade(r["signal"], change, band),
        })
        graded += 1
        log.info(f"  Graded {r['date']}: {r['signal']} → {change:+.2f}% "
                 f"= {'HIT' if r['correct'] else 'MISS'}")

    if graded:
        _save(rows)
    return graded


# ── Summary ───────────────────────────────────────────────────────────────────

def _streak(resolved: list[dict]) -> dict:
    """Current run of consecutive same-outcome calls, most recent first."""
    if not resolved:
        return {"kind": None, "length": 0}
    kind = resolved[-1]["correct"]
    n = 0
    for r in reversed(resolved):
        if r["correct"] != kind:
            break
        n += 1
    return {"kind": "hit" if kind else "miss", "length": n}


def summary() -> dict:
    rows = _load()
    resolved = [r for r in rows if r.get("resolved")]
    resolved.sort(key=lambda r: r["made_at_ms"])

    def acc(subset: list[dict]) -> float | None:
        if not subset:
            return None
        return round(sum(1 for r in subset if r["correct"]) / len(subset) * 100, 1)

    cutoff = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000)
    last30 = [r for r in resolved if r["made_at_ms"] >= cutoff]

    by_signal = {}
    for sig in ("BUY", "HOLD", "SELL"):
        subset = [r for r in resolved if r["signal"] == sig]
        by_signal[sig] = {"n": len(subset), "accuracy_pct": acc(subset)}

    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_calls": len(rows),
        "resolved_calls": len(resolved),
        "pending_calls": len(rows) - len(resolved),
        "hits": sum(1 for r in resolved if r["correct"]),
        "misses": sum(1 for r in resolved if not r["correct"]),
        "accuracy_pct": acc(resolved),
        "accuracy_30d_pct": acc(last30),
        "resolved_30d": len(last30),
        "streak": _streak(resolved),
        "by_signal": by_signal,
        # The single most important row for the video: the call being graded
        # on camera today.
        "last_resolved": resolved[-1] if resolved else None,
        "flat_band_pct": config()["scoring"]["flat_band_pct"],
        "horizon_hours": config()["scoring"]["horizon_hours"],
    }
    write_json(SUMMARY_PATH, out)
    return out


def seed_if_empty(snapshot: dict) -> None:
    """First run has nothing to grade. Rather than fake a history, the video
    simply says day one — handled downstream by `has_history`."""
    if not LOG_PATH.exists():
        log.info("  No prediction history yet — this is day one of the scoreboard")


def has_history(summary_data: dict) -> bool:
    return (summary_data.get("resolved_calls") or 0) > 0
