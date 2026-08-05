"""Price history, sourced so it works from anywhere.

Binance answers GitHub's US-hosted runners with HTTP 451 ("restricted
location"), which killed the first real CI run at the fetch stage. The VPS is
not blocked — it is what feeds the model — so the pipeline goes through
quantaura.tech's own /api/market/candles instead of calling Binance directly.

That is not merely a workaround. It makes the chart, the graded outcome and
the model's own inputs all come from one venue. Mixing exchanges would put a
BTC-USD print in the numerator and a BTCUSDT print in the denominator of the
same percentage, which is exactly the sort of quiet inaccuracy this channel
claims not to have.

Binance direct remains as a fallback for local runs, where it is reachable and
saves a round trip through the VPS.
"""
from __future__ import annotations

from common import PipelineAbort, config, get_json, log


def _qa(symbol: str, interval: str, limit: int, start_ms: int | None = None) -> list[list] | None:
    """Candles via quantaura.tech. Returns [[openTime, o, h, l, c], ...]."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms is not None:
        params["startTime"] = start_ms
    res = get_json(f"{config()['api']['base_url']}/api/market/candles",
                   params=params, retries=2, label="quantaura/candles")
    if not res.ok:
        log.warning(f"  quantaura candles unavailable: {res.error}")
        return None
    rows = (res.data or {}).get("data") or []
    if not rows:
        return None
    try:
        return [[int(r["openTime"]), float(r["open"]), float(r["high"]),
                 float(r["low"]), float(r["close"])] for r in rows]
    except (KeyError, TypeError, ValueError) as e:
        log.warning(f"  quantaura candles malformed: {e}")
        return None


def _binance(symbol: str, interval: str, limit: int,
             start_ms: int | None = None) -> list[list] | None:
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms is not None:
        params["startTime"] = start_ms
    res = get_json(config()["market"]["klines_url"], params=params,
                   retries=1, label="binance/klines")
    if not res.ok or not isinstance(res.data, list) or not res.data:
        # 451 from a US runner is expected, not alarming — it is precisely why
        # quantaura is tried first.
        log.info(f"  Binance direct unavailable ({res.error or 'empty'})")
        return None
    try:
        return [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])]
                for k in res.data]
    except (IndexError, TypeError, ValueError) as e:
        log.warning(f"  Binance klines malformed: {e}")
        return None


def candles(symbol: str, interval: str, limit: int) -> list[list]:
    """Recent candles for the chart. Fatal if no source answers."""
    for source in (_qa, _binance):
        rows = source(symbol, interval, limit)
        if rows:
            return rows
    raise PipelineAbort(
        "Could not fetch price history from quantaura.tech or Binance. "
        "The chart cannot be drawn, so nothing is published.")


def price_at(symbol: str, ms: int) -> float | None:
    """Close of the 1-minute candle covering `ms`, for grading a past call.

    Returns None rather than raising: a call that cannot be priced today is
    simply left unresolved and retried tomorrow, which must never block the
    rest of the run.
    """
    for source in (_qa, _binance):
        rows = source(symbol, "1m", 1, start_ms=ms)
        if rows:
            return rows[0][4]
    return None
