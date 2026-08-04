"""Collect everything the day's video is built from.

Sources, in order of importance:
  1. QuantAura ml-api (via the public node API) — the model's call. Fatal if absent.
  2. Binance public klines — price history for the chart. Fatal if absent.
  3. Fear & Greed index — colour commentary. Degrades silently.

The premium login exists because /api/signals redacts confidence, the
per-model votes and the probability breakdown for free-tier callers — i.e.
exactly the fields the video is about. The pipeline authenticates as a
dedicated service account rather than reaching into ml-api directly, so it
consumes the same public contract every other client does.
"""
from __future__ import annotations

from datetime import datetime, timezone

from common import (PipelineAbort, config, env, fmt_price, get_json, log,
                    post_json)


def _login() -> str | None:
    """Returns a JWT for the channel's premium service account, or None."""
    cfg = config()["api"]
    email = env("QA_EMAIL")
    password = env("QA_PASSWORD")
    if not (email and password):
        log.warning("  QA_EMAIL/QA_PASSWORD not set — falling back to free-tier data "
                    "(no confidence or per-model votes)")
        return None

    res = post_json(f"{cfg['base_url']}/api/auth/login",
                    {"email": email, "password": password},
                    timeout=cfg["timeout_seconds"], label="auth/login")
    if not res.ok:
        log.warning(f"  Login failed ({res.error}) — continuing on free-tier data")
        return None
    token = ((res.data or {}).get("data") or {}).get("token")
    if not token:
        log.warning("  Login response had no token — continuing on free-tier data")
        return None
    log.info("  Authenticated as channel service account")
    return token


def _signal(token: str | None) -> dict:
    cfg = config()["api"]
    symbol = config()["channel"]["symbol"]
    headers = {"Authorization": f"Bearer {token}"} if token else None
    res = get_json(f"{cfg['base_url']}/api/signals", params={"symbol": symbol},
                   headers=headers, label="api/signals")
    if not res.ok:
        raise PipelineAbort(f"Could not reach the signals API: {res.error}")

    data = (res.data or {}).get("data") or {}
    if data.get("locked"):
        raise PipelineAbort("Signals API returned a locked/upgrade response — the "
                            "service account is not premium.")
    if not data.get("signal"):
        raise PipelineAbort("Signals API returned no signal field.")
    if data.get("source") == "ta_fallback":
        # The whole channel premise is the ML ensemble's call. A TA-heuristic
        # stand-in would be a different product wearing the same branding.
        raise PipelineAbort("ml-api was unavailable; the API served a TA fallback. "
                            "Refusing to publish a non-ensemble call.")

    # Tier redaction is silent by design: the free tier gets a 200 with
    # `confidence: null` and no `ensemble`/`breakdown`. Those three fields are
    # the entire subject of the video, so an unauthenticated run must fail
    # loudly here rather than render "None% confidence".
    if data.get("confidence") is None or not data.get("ensemble"):
        raise PipelineAbort(
            "The signals API returned a free-tier response (no confidence or "
            "per-model votes). Set QA_EMAIL/QA_PASSWORD to a premium account.")
    return data


def _stats() -> dict:
    cfg = config()["api"]
    res = get_json(f"{cfg['base_url']}/api/signals/stats", label="api/signals/stats")
    if not res.ok:
        log.warning(f"  /stats unavailable ({res.error}) — model metrics omitted")
        return {}
    return (res.data or {}).get("data") or {}


def klines(symbol: str, interval: str, limit: int) -> list[list]:
    """Raw Binance klines: [open_time, o, h, l, c, v, close_time, ...]."""
    m = config()["market"]
    res = get_json(m["klines_url"],
                   params={"symbol": symbol, "interval": interval, "limit": limit},
                   label="binance/klines")
    if not res.ok or not isinstance(res.data, list):
        raise PipelineAbort(f"Could not fetch price history: {res.error}")
    return res.data


def _fear_greed() -> dict | None:
    res = get_json(config()["market"]["fear_greed_url"], retries=1, label="fear&greed")
    if not res.ok:
        return None
    try:
        row = (res.data or {}).get("data", [])[0]
        return {"value": int(row["value"]), "classification": row["value_classification"]}
    except Exception:                                            # noqa: BLE001
        return None


def collect() -> dict:
    """Build the day's snapshot. Raises PipelineAbort if a fatal source fails."""
    cfg = config()
    symbol = cfg["channel"]["symbol"]
    log.info("Fetching source data...")

    token = _login()
    sig = _signal(token)
    stats = _stats()

    # 7 days of hourly candles for the chart.
    days = cfg["market"]["chart_days"]
    candles = klines(symbol, cfg["market"]["chart_interval"], days * 24)
    closes = [float(c[4]) for c in candles]
    price = float(sig.get("price") or closes[-1])

    def pct_from(hours_ago: int) -> float | None:
        if len(closes) <= hours_ago:
            return None
        past = closes[-(hours_ago + 1)]
        return round((price - past) / past * 100, 2) if past else None

    tradeability = stats.get("tradeability") or {}
    model_cfg = stats.get("config") or {}

    snapshot = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": symbol,
        "asset_label": cfg["channel"]["asset_label"],

        # ── the call ──
        "signal": sig.get("signal"),
        "confidence": sig.get("confidence"),
        "breakdown": sig.get("breakdown") or {},
        # The node API repackages per-model votes under `ensemble`; only the
        # `signal` field there is meaningful (its `confidence` numbers are the
        # ensemble's class probabilities relabelled, not per-model confidences).
        "per_model": {k: (v or {}).get("signal")
                      for k, v in (sig.get("ensemble") or {}).items()},
        "weights": sig.get("weights_used") or stats.get("weights") or {},

        # ── market ──
        "price": price,
        "price_str": fmt_price(price),
        "change_24h_pct": pct_from(24),
        "change_7d_pct": pct_from(days * 24 - 1),
        "candles": [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])]
                    for c in candles],
        "indicators": sig.get("indicators") or {},
        "fear_greed": _fear_greed(),

        # ── model provenance ──
        "ensemble_f1": stats.get("ensemble_f1"),
        # tradeability.json stores this as a fraction (0.5226), unlike every
        # other percentage in the API. Normalise once, here.
        "directional_accuracy_pct": (
            round(float(tradeability["directional_accuracy"]) * 100, 1)
            if tradeability.get("directional_accuracy") is not None else None),
        "label_horizon_hours": model_cfg.get("label_horizon_hours", 24),
        "confidence_threshold_pct": round(
            (model_cfg.get("confidence_threshold") or 0) * 100, 1) or None,
        "models_updated_at": stats.get("models_updated_at"),
    }

    # The confidence gate turning a directional call into HOLD is one of the
    # more interesting things the model does on a given day, so surface it.
    raw = sig.get("signal_raw")
    snapshot["gated"] = bool(raw and raw != snapshot["signal"])
    snapshot["signal_raw"] = raw or snapshot["signal"]

    log.info(f"  {symbol}: {snapshot['signal']} @ {snapshot['confidence']}% "
             f"| price {snapshot['price_str']} ({snapshot['change_24h_pct']}% 24h)")
    return snapshot
