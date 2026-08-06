"""Shared plumbing for the channel pipeline: config, paths, logging, HTTP.

Every other module imports from here so that "where does the config live" and
"how do we retry a flaky endpoint" have exactly one answer.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import yaml

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
BUILD_DIR = BASE_DIR / "build"
ASSETS_DIR = BASE_DIR / "assets"
PROMPTS_DIR = BASE_DIR / "prompts"
MODELS_DIR = BASE_DIR / "models"          # Kokoro onnx + voices (gitignored)

for _d in (STATE_DIR, BUILD_DIR, ASSETS_DIR, MODELS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ── Logging ───────────────────────────────────────────────────────────────────

def get_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    # Windows consoles often run cp1252, which cannot encode the arrows and
    # dashes these logs use. Without this, every such line raises a noisy
    # "--- Logging error ---" traceback from inside the logging module itself.
    # CI runners are UTF-8 and unaffected.
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                            # noqa: BLE001
        pass
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                                     datefmt="%H:%M:%S"))
    log.addHandler(h)
    return log


log = get_logger("channel")


# ── Config ────────────────────────────────────────────────────────────────────

_CONFIG: dict | None = None


def config() -> dict:
    """Parsed config.yaml, loaded once."""
    global _CONFIG
    if _CONFIG is None:
        with open(BASE_DIR / "config.yaml", encoding="utf-8") as fh:
            _CONFIG = yaml.safe_load(fh)
    return _CONFIG


def env(name: str, required: bool = False, default: str | None = None) -> str | None:
    """Secrets come from the environment only — never from a committed file."""
    val = os.environ.get(name) or default
    if required and not val:
        raise PipelineAbort(f"Missing required environment variable: {name}")
    return val


# ── Errors ────────────────────────────────────────────────────────────────────

class PipelineAbort(Exception):
    """Raised when today's video cannot be produced safely.

    The orchestrator catches this, notifies, and exits cleanly. Skipping a day
    is a normal outcome — publishing something unverified is not.
    """


# ── HTTP ──────────────────────────────────────────────────────────────────────

@dataclass
class HttpResult:
    ok: bool
    status: int
    data: Any
    error: str | None = None


_session: requests.Session | None = None


def http_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": "QuantAura-Channel/1.0 (+https://quantaura.tech)"})
    return _session


def get_json(url: str, *, params: dict | None = None, headers: dict | None = None,
             retries: int | None = None, timeout: int | None = None,
             backoff: float | None = None, label: str = "") -> HttpResult:
    """GET with bounded retries. Never raises on HTTP failure — the caller
    decides whether a given source is fatal or merely degrades the video."""
    cfg = config()["api"]
    retries = cfg["retries"] if retries is None else retries
    timeout = cfg["timeout_seconds"] if timeout is None else timeout
    backoff = cfg["retry_backoff_seconds"] if backoff is None else backoff
    name = label or url

    last = "unknown error"
    for attempt in range(1, retries + 1):
        try:
            r = http_session().get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return HttpResult(True, 200, r.json())
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            # 4xx other than 429 will not fix themselves by waiting.
            if 400 <= r.status_code < 500 and r.status_code != 429:
                return HttpResult(False, r.status_code, None, last)
        except Exception as e:                                   # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        if attempt < retries:
            wait = backoff * attempt
            log.warning(f"  {name}: {last} — retrying in {wait:.0f}s ({attempt}/{retries})")
            time.sleep(wait)
    return HttpResult(False, 0, None, last)


def post_json(url: str, payload: dict, *, headers: dict | None = None,
              timeout: int = 60, retries: int = 2, label: str = "") -> HttpResult:
    name = label or url
    last = "unknown error"
    for attempt in range(1, retries + 1):
        try:
            r = http_session().post(url, json=payload, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return HttpResult(True, 200, r.json())
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if 400 <= r.status_code < 500 and r.status_code != 429:
                return HttpResult(False, r.status_code, None, last)
        except Exception as e:                                   # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(3 * attempt)
    return HttpResult(False, 0, None, last)


# ── Small helpers ─────────────────────────────────────────────────────────────

def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:                                       # noqa: BLE001
        log.warning(f"  {path.name} unreadable ({e}) — using default")
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    tmp.replace(path)                       # atomic: never leave a half-written state file


def fmt_price(v: float) -> str:
    return f"${v:,.0f}" if v >= 100 else f"${v:,.2f}"


def fmt_pct(v: float, signed: bool = True) -> str:
    return f"{v:+.2f}%" if signed else f"{v:.2f}%"


def prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


# ── Run bookkeeping ───────────────────────────────────────────────────────────
# Shared by the daily pipeline and the weekly recap: both append to the same
# committed log so the channel's whole operational history reads in one place.

RUNS_PATH = STATE_DIR / "runs.jsonl"


def published_today(kind: str = "daily") -> bool:
    """Has a video of this kind already gone out for today's UTC date?

    The daily schedule runs twice (13:00 and a 16:00 catch-up) because
    GitHub's cron is best-effort and silently drops runs under load — on
    2026-08-06 the 13:00 slot never fired at all. Two chances at publishing
    only helps if the second is a no-op when the first succeeded, so this is
    the guard that makes the retry safe.

    Reads the committed run log, which CI checks out fresh, so a successful
    earlier run is visible to a later one. The residual risk is narrow: if a
    run published but then failed to push its log, the catch-up would post a
    second video. The push has three rebase attempts and the log union-merges,
    so that requires a sustained git outage.
    """
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not RUNS_PATH.exists():
        return False
    for line in RUNS_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:                                        # noqa: BLE001
            continue
        # Rows written before `kind` existed are all daily runs.
        if (row.get("date") == today
                and row.get("kind", "daily") == kind
                and row.get("status") == "published"):
            return True
    return False


def record_run(status: str, stage: str, detail: str, extra: dict | None = None,
               kind: str = "daily") -> None:
    from datetime import datetime, timezone
    row = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "kind": kind,
        "status": status,
        "stage": stage,
        "detail": detail[:400],
        **(extra or {}),
    }
    RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RUNS_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    # GitHub renders this under the workflow run, so a failure is legible
    # without opening logs.
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        icon = {"published": "✅", "rendered": "⚠️", "skipped": "🚫",
                "crashed": "💥"}.get(status, "•")
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"### {icon} {status.title()} ({kind}) — {row['date']}\n\n"
                     f"**Stage:** {stage}\n\n{detail[:600]}\n\n")
            for k, v in (extra or {}).items():
                fh.write(f"- **{k}**: {v}\n")
