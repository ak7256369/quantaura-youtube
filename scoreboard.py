"""The public accuracy record — the reason this channel is worth watching.

Design rules that keep the scoreboard trustworthy:
  * A call is written down BEFORE its outcome is knowable, and never edited.
  * Grading is arithmetic over committed rows, not a model output. The LLM
    receives these numbers as input and is forbidden from computing its own.
  * Losses are graded the same way as wins and are shown with equal prominence.

State lives in two files under state/, committed to the `channel-data`
branch after every run:
    predictions.jsonl  — append-only log, one call per line
    scoreboard.json    — derived summary, safe to delete and rebuild
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from prices import price_at as _price_at
from common import STATE_DIR, config, log, write_json

LOG_PATH = STATE_DIR / "predictions.jsonl"
SUMMARY_PATH = STATE_DIR / "scoreboard.json"


# ── Log I/O ───────────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    """Parse the log, keeping exactly one row per date.

    The de-duplication is not paranoia. `state/.gitattributes` sets
    these files to union-merge so concurrent CI and local appends cannot
    conflict, and the cost of that is a row can legitimately appear twice.
    Counting one call twice would corrupt the one number the channel exists to
    report, so a resolved row always wins over an unresolved one for the same
    date, and the later write wins between equals.
    """
    if not LOG_PATH.exists():
        return []

    import json
    by_date: dict[str, dict] = {}
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:                                        # noqa: BLE001
            log.warning(f"  Skipping malformed scoreboard row: {line[:80]}")
            continue

        key = row.get("date")
        if key is None:
            continue
        prev = by_date.get(key)
        if prev is None or (row.get("resolved") and not prev.get("resolved")):
            by_date[key] = row

    return sorted(by_date.values(), key=lambda r: r.get("made_at_ms", 0))


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

def _grade(signal: str, change_pct: float, band: float) -> bool:
    """BUY wants a rise beyond the flat band, SELL a fall, HOLD neither."""
    if signal == "BUY":
        return change_pct > band
    if signal == "SELL":
        return change_pct < -band
    return abs(change_pct) <= band


def resolve_due() -> int:
    """Grade every call whose horizon has elapsed. Returns how many were graded.

    If a call comes due within the next few minutes, wait for it. This is not a
    nicety: the daily cron fires at 13:00 and records its call a few minutes
    later, so yesterday's call is due minutes AFTER today's run starts — a
    fixed cadence is structurally always just short. Without the wait, every
    call's grading would slip a full extra day (or worse, come and go with
    cron jitter). The price is a few idle runner-minutes; the horizon stays
    exactly 24h because grading prices at the historical due-minute candle.
    """
    rows = _load()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    wait_cap_ms = int(config()["scoring"].get("wait_for_due_minutes", 20)) * 60_000
    pending_due = [r["made_at_ms"] + r.get("horizon_hours", 24) * 3600_000
                   for r in rows if not r.get("resolved")]
    soon = [d for d in pending_due if 0 < d - now_ms <= wait_cap_ms]
    if soon:
        # +90s so the 1-minute candle covering the due instant has closed.
        wait_s = (min(soon) - now_ms) / 1000 + 90
        log.info(f"  Next call due in {wait_s / 60:.1f} min — waiting to grade it "
                 f"at its exact 24h mark")
        import time
        time.sleep(wait_s)
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


# ── Paper portfolio ───────────────────────────────────────────────────────────

def _portfolio(rows: list[dict]) -> dict | None:
    """A virtual $10,000 traded mechanically on the committed calls.

    Policy: BUY → fully long, SELL → fully in cash, HOLD → unchanged.
    Long-flat only — no leverage, no shorting: it mirrors what a viewer with a
    spot account could actually have done by following the calls. Fees are
    charged per side on every position change. Marks at each call's committed
    price, so the whole curve is arithmetic over rows that were written down
    before their outcomes were knowable — same trust rule as the scoreboard.

    Published next to buy-and-hold even though the tradeability report says it
    probably loses to holding after fees. That is not a bug in the page; it is
    the page.
    """
    cfg = config().get("portfolio") or {}
    start = float(cfg.get("start_usd", 10000))
    fee_rate = float(cfg.get("fee_pct_per_side", 0.15)) / 100.0

    priced = [r for r in rows if r.get("price")]
    if not priced:
        return None

    first_price = float(priced[0]["price"])
    cash, btc = start, 0.0
    fees_paid, trades = 0.0, 0
    curve = []

    for r in priced:
        p = float(r["price"])
        sig = r.get("signal")
        if sig == "BUY" and btc == 0.0:
            fee_usd = cash * fee_rate
            btc = (cash - fee_usd) / p
            cash = 0.0
            fees_paid += fee_usd
            trades += 1
        elif sig == "SELL" and btc > 0.0:
            gross = btc * p
            fee_usd = gross * fee_rate
            cash = gross - fee_usd
            btc = 0.0
            fees_paid += fee_usd
            trades += 1
        curve.append({
            "date": r["date"],
            "value": round(cash + btc * p, 2),
            "hold": round(start * p / first_price, 2),
            "price": p,
            "signal": sig,
        })

    last_price = float(priced[-1]["price"])
    value = cash + btc * last_price
    hold_value = start * last_price / first_price
    ret = (value / start - 1) * 100
    hold_ret = (hold_value / start - 1) * 100

    return {
        "start_usd": start,
        "fee_pct_per_side": round(fee_rate * 100, 3),
        "policy": "BUY = fully long, SELL = fully in cash, HOLD = unchanged",
        "since": priced[0]["date"],
        "days": len(priced),
        "value_usd": round(value, 2),
        "return_pct": round(ret, 2),
        "hold_value_usd": round(hold_value, 2),
        "hold_return_pct": round(hold_ret, 2),
        "vs_hold_pct": round(ret - hold_ret, 2),
        "fees_paid_usd": round(fees_paid, 2),
        "trades": trades,
        "position": "BTC" if btc > 0 else "cash",
        "curve": curve,
    }


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
        "portfolio": _portfolio(rows),
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
