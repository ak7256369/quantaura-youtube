"""Orchestrator: one run produces at most one video.

    python pipeline.py                 # full run, uploads per config.yaml
    python pipeline.py --no-upload     # build everything, publish nothing
    python pipeline.py --no-voice      # silent placeholder audio, fast render
    python pipeline.py --no-drive      # skip the Drive mirror, still upload
    python pipeline.py --check-drive   # verify Drive auth only, then exit
    python pipeline.py --dry-run       # no LLM, no upload: plumbing check

The governing rule is in the failure path, not the happy path: any stage that
cannot complete *safely* raises PipelineAbort, which ends the run with a
notification and no upload. A missed day is invisible to viewers. A video with
a wrong number in it is permanent.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import fetch
import factcheck
import notify
import render as render_mod
import scoreboard
import scriptwriter
import thumbnail as thumb_mod
import voice as voice_mod
from common import (BUILD_DIR, PipelineAbort, config, log, published_today,
                    record_run as _record_run, write_json)


def _dry_snapshot() -> dict:
    """Synthetic data so --dry-run can exercise render/upload plumbing without
    touching the live API or spending a single LLM token."""
    import math
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    candles = []
    for i in range(168):
        p = 61000 + 1800 * math.sin(i / 19) + i * 6
        candles.append([now - (168 - i) * 3600_000, p, p * 1.004, p * 0.996, p])
    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": "BTCUSDT", "asset_label": "Bitcoin",
        "signal": "HOLD", "signal_raw": "BUY", "gated": True,
        "confidence": 41.3,
        "breakdown": {"BUY": 41.3, "HOLD": 38.2, "SELL": 20.5},
        "per_model": {"lstm": "BUY", "transformer": "HOLD", "xgboost": "BUY", "kan": "SELL"},
        "weights": {"lstm": 0.3, "transformer": 0.25, "xgboost": 0.25, "kan": 0.2},
        "price": candles[-1][4], "price_str": f"${candles[-1][4]:,.0f}",
        "change_24h_pct": -1.42, "change_7d_pct": 2.31,
        "candles": candles, "indicators": {},
        "fear_greed": {"value": 54, "classification": "Neutral"},
        "ensemble_f1": 0.7593, "directional_accuracy_pct": 52.1,
        "label_horizon_hours": 24, "confidence_threshold_pct": 45.0,
        "models_updated_at": None,
    }


def _dry_script() -> dict:
    return scriptwriter.validate({
        "title": "BTC Model Call — HOLD (41%) · dry run",
        "description": "Dry-run render. No live data, no upload.",
        "tags": ["bitcoin", "machine learning"],
        "sections": {
            "hook": ["The ensemble split three ways today and the confidence gate "
                     "stepped in."],
            "call": ["The model produced a buy signal at 41.3 percent confidence.",
                     "That sits below the 45 percent threshold, so the published call "
                     "is hold.",
                     "Bitcoin is trading around 61 thousand dollars, down 1.42 percent "
                     "over the day."],
            "why": ["The LSTM and the gradient boosted model both leaned bullish.",
                    "The KAN disagreed outright and the transformer stayed neutral.",
                    "Fear and greed sits at 54, squarely neutral."],
            "score": ["This is a dry run, so no past call is being graded.",
                      "The real scoreboard grades every call 24 hours after it is made."],
        },
        "overlays": {
            "hook": "Gate blocks a split call",
            "call": "HOLD · 41% confidence",
            "why": ["LSTM + XGB bullish", "KAN disagrees", "Fear & Greed 54"],
            "score": "dry run · no record",
        },
    })


# ── Stages ────────────────────────────────────────────────────────────────────

def build_script(snapshot: dict, score: dict) -> dict:
    """Write and verify. Returns a script that passed every gate."""
    facts = scriptwriter.build_facts(snapshot, score)
    write_json(BUILD_DIR / "facts.json", facts)

    max_regen = config()["factcheck"]["max_regenerations"]
    feedback: list[str] = []

    for attempt in range(1, max_regen + 2):
        log.info(f"Writing script (attempt {attempt}/{max_regen + 1})...")
        script = scriptwriter.write(facts, feedback=feedback)

        log.info("Fact-checking...")
        problems = factcheck.verify(script, facts)
        if not problems:
            write_json(BUILD_DIR / "script.json", script)
            return script

        feedback = problems
        if attempt > max_regen:
            raise PipelineAbort(
                f"Script failed verification {attempt} times — publishing nothing. "
                f"First problem: {problems[0][:200]}")

    raise PipelineAbort("Unreachable: script loop exited without a result")


def _drive_extra(info: dict | None) -> dict:
    """Drive columns for the run log — present only when the mirror worked, so
    a missing `drive_url` in the log means the X copy needs doing by hand."""
    if not (info and info.get("ok")):
        return {}
    return {"drive_url": info.get("video_url"), "drive_mb": info.get("size_mb")}


def run(args: argparse.Namespace) -> int:
    stage = "startup"
    try:
        # The catch-up schedule exists because GitHub drops cron runs; this is
        # what stops it publishing a second video when the first slot worked.
        publishing = not (args.dry_run or args.no_upload)
        if publishing and not args.force and published_today("daily"):
            log.info("A daily video was already published today — nothing to do.")
            return 0

        # ── data ──
        stage = "fetch"
        if args.dry_run:
            log.info("DRY RUN — using synthetic data, no live API calls")
            snapshot = _dry_snapshot()
        else:
            snapshot = fetch.collect()
        write_json(BUILD_DIR / "snapshot.json",
                   {k: v for k, v in snapshot.items() if k != "candles"})

        # ── scoreboard: grade what is due, then log today ──
        stage = "scoreboard"
        log.info("Updating scoreboard...")
        if not args.dry_run:
            scoreboard.resolve_due()
            scoreboard.record(snapshot)
        score = scoreboard.summary()
        log.info(f"  Record: {score.get('hits', 0)}W/{score.get('misses', 0)}L "
                 f"over {score.get('resolved_calls', 0)} graded calls")

        # ── script ──
        stage = "script"
        script = _dry_script() if args.dry_run else build_script(snapshot, score)

        # ── voice ──
        stage = "voice"
        log.info("Synthesising narration...")
        sentences = voice_mod.sentences_from_script(
            script, config()["disclaimer"]["narration"])
        narration = voice_mod.synthesize(sentences, silent=args.no_voice or args.dry_run)

        # ── render ──
        stage = "render"
        video = render_mod.render(snapshot, script, score, narration)
        duration = render_mod.duration_of(video)
        if duration:
            log.info(f"  Duration: {duration:.1f}s")
            if duration > config()["video"]["max_seconds"]:
                log.warning(f"  Over the {config()['video']['max_seconds']}s Shorts "
                            f"limit — it will publish as a regular video")

        stage = "thumbnail"
        thumb = thumb_mod.build(snapshot, score)

        # ── mirror to Drive for the manual X post ──
        # Ahead of the YouTube upload on purpose. X is now a hand-delivered
        # surface (the API went pay-per-use — see drive.py), so this copy has no
        # retry path of its own and should not be contingent on YouTube working.
        stage = "drive"
        drive_info = None
        if args.no_drive or args.no_upload or args.dry_run:
            log.info("Skipping the Drive mirror.")
        else:
            import drive as drive_mod
            drive_info = drive_mod.mirror(video, snapshot, score)

        # ── publish ──
        stage = "upload"
        if args.no_upload or args.dry_run:
            reason = "--dry-run" if args.dry_run else "--no-upload"
            log.info(f"Skipping upload ({reason}). Video at {video}")
            _record_run("rendered", stage, f"Built locally, not uploaded ({reason}).",
                        {"video": str(video), "duration_s": round(duration or 0, 1)})
            if not args.dry_run:
                notify.rendered_only(snapshot, script, str(video), reason)
            return 0

        try:
            import upload as upload_mod
            _vid, url = upload_mod.upload(video, thumb, script, snapshot, score)
        except PipelineAbort as e:
            # The video is good; only delivery failed. Keep it as an artifact
            # so the day can still be published by hand.
            log.error(f"Upload failed: {e}")
            _record_run("rendered", "upload", str(e),
                        {"video": str(video), **_drive_extra(drive_info)})
            notify.rendered_only(snapshot, script, str(video), str(e), drive_info)
            return 0

        visibility = config()["upload"]["visibility"]
        _record_run("published", "upload", script["title"],
                    {"url": url, "visibility": visibility,
                     "signal": snapshot["signal"],
                     "duration_s": round(duration or 0, 1),
                     **_drive_extra(drive_info)})
        notify.success(snapshot, score, script, url, visibility, drive_info)
        log.info(f"Done: {url}")
        return 0

    except PipelineAbort as e:
        log.error(f"Aborted at '{stage}': {e}")
        _record_run("skipped", stage, str(e))
        notify.skipped(str(e), stage)
        return 0                      # a deliberate skip is not a build failure

    except Exception as e:                                       # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"
        log.error(f"Crashed at '{stage}': {detail}")
        traceback.print_exc()
        _record_run("crashed", stage, detail)
        notify.crashed(f"{detail}\n\n{traceback.format_exc()[-600:]}", stage)
        return 1


def main() -> int:
    p = argparse.ArgumentParser(description="QuantAura daily channel pipeline")
    p.add_argument("--no-upload", action="store_true",
                   help="build the video but do not publish it")
    p.add_argument("--no-voice", action="store_true",
                   help="use silent placeholder audio (fast render iteration)")
    p.add_argument("--no-drive", action="store_true",
                   help="skip the Google Drive mirror (still uploads to YouTube)")
    p.add_argument("--check-drive", action="store_true",
                   help="verify the Drive credentials and folder, then exit")
    p.add_argument("--dry-run", action="store_true",
                   help="synthetic data, no API calls, no upload")
    p.add_argument("--force", action="store_true",
                   help="publish even if a video already went out today")
    args = p.parse_args()

    log.info("=" * 60)
    log.info(f"QuantAura channel pipeline · {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    log.info("=" * 60)

    if args.check_drive:
        # Short-circuits ahead of run(): the check needs no data, no script and
        # no render, and a credential probe that first spends five minutes
        # building a video is not a probe anyone will run.
        import drive as drive_mod
        return 0 if drive_mod.check() else 1

    return run(args)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
