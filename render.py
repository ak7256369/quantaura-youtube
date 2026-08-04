"""Build the video.

Everything visual is drawn from real data with matplotlib and assembled with
ffmpeg. No generative video model is involved in the body of the clip, for one
decisive reason: the content *is* the numbers, and a diffusion model cannot be
trusted to render a price chart or a percentage correctly. Generative video is
used only for the fixed intro/outro branding, made once by hand.

Assembly strategy: each scene becomes its own short mp4 encoded with identical
parameters, then the scenes are concatenated. Scene durations come from the
narration timings, so picture and voice cannot drift apart.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                                   # noqa: E402
import numpy as np                                                # noqa: E402
from matplotlib.patches import FancyBboxPatch                     # noqa: E402

from common import BASE_DIR, BUILD_DIR, PipelineAbort, config, log  # noqa: E402
from voice import Narration                                       # noqa: E402

W, H = 1080, 1920
FRAMES_DIR = BUILD_DIR / "frames"


# ── ffmpeg plumbing ───────────────────────────────────────────────────────────

def ffmpeg_path() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise PipelineAbort(
            "ffmpeg not found on PATH. Install it — Windows: `winget install "
            "Gyan.FFmpeg`, Debian/Ubuntu: `sudo apt install ffmpeg`.")
    return exe


def _run(args: list[str], label: str) -> None:
    proc = subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise PipelineAbort(f"ffmpeg failed during {label}: "
                            f"{(proc.stderr or proc.stdout or '').strip()[:500]}")


# Every clip is encoded identically so the concat demuxer can stitch them
# without a re-encode negotiation.
_ENC = ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", "30", "-an"]


# ── Canvas helpers ────────────────────────────────────────────────────────────

def _theme() -> dict:
    return config()["theme"]


def _canvas():
    """A 1080x1920 figure addressed in pixel coordinates."""
    t = _theme()
    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100, facecolor=t["bg"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.invert_yaxis()                    # y grows downward, like a design tool
    ax.axis("off")
    ax.set_facecolor(t["bg"])
    return fig, ax


def _text(ax, x, y, s, size=44, color=None, weight="normal", ha="left", va="center",
          alpha=1.0, family="DejaVu Sans"):
    return ax.text(x, y, s, fontsize=size, color=color or _theme()["text"],
                   fontweight=weight, ha=ha, va=va, alpha=alpha, family=family)


def _panel(ax, x, y, w, h, color=None, alpha=1.0, radius=28, edge=None, lw=0):
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h,
        boxstyle=f"round,pad=0,rounding_size={radius}",
        facecolor=color or _theme()["panel"], alpha=alpha,
        edgecolor=edge or "none", linewidth=lw, zorder=1))


def _signal_color(signal: str) -> str:
    t = _theme()
    return {"BUY": t["up"], "SELL": t["down"]}.get(signal, t["flat"])


def _wrap(s: str, width: int) -> list[str]:
    words, lines, cur = s.split(), [], ""
    for w_ in words:
        trial = f"{cur} {w_}".strip()
        if len(trial) <= width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w_
    if cur:
        lines.append(cur)
    return lines


def _header(ax, snapshot: dict, label: str):
    # Brand left, date right, nothing in between: a centre element here
    # collided with both once the brand word rendered at its true width.
    t = _theme()
    _text(ax, 64, 96, "QUANTAURA", size=34, weight="bold", color=t["accent"])
    _text(ax, W - 64, 96, snapshot["date"], size=32, color=t["muted"], ha="right")
    ax.plot([64, W - 64], [140, 140], color=t["grid"], lw=2)
    if label:
        _text(ax, 64, 196, label.upper(), size=30, weight="bold", color=t["muted"])


def _save(fig, path: Path) -> Path:
    fig.savefig(path, facecolor=fig.get_facecolor(), dpi=100)
    plt.close(fig)
    return path


# ── Scenes ────────────────────────────────────────────────────────────────────

def scene_hook(snapshot: dict, script: dict, path: Path) -> Path:
    t = _theme()
    fig, ax = _canvas()
    _header(ax, snapshot, "daily call")

    sig = snapshot["signal"]
    color = _signal_color(sig)

    # Oversized signal word — the frame that has to work as a thumbnail at
    # 120px wide in a feed.
    _text(ax, W / 2, 620, sig, size=210, weight="bold", color=color, ha="center")
    _text(ax, W / 2, 790, f"{snapshot['asset_label']} · next "
                          f"{snapshot.get('label_horizon_hours', 24)} hours",
          size=40, color=t["muted"], ha="center")

    headline = script["overlays"].get("hook") or script["title"]
    for i, line in enumerate(_wrap(headline, 22)[:3]):
        _text(ax, W / 2, 990 + i * 84, line, size=64, weight="bold", ha="center")

    _panel(ax, 64, 1210, W - 128, 190)
    _text(ax, 110, 1272, "PRICE", size=30, color=t["muted"])
    _text(ax, 110, 1345, snapshot["price_str"], size=64, weight="bold")
    chg = snapshot.get("change_24h_pct")
    if chg is not None:
        c = t["up"] if chg >= 0 else t["down"]
        _text(ax, W - 110, 1272, "24H", size=30, color=t["muted"], ha="right")
        _text(ax, W - 110, 1345, f"{chg:+.2f}%", size=64, weight="bold", color=c, ha="right")

    return _save(fig, path)


def _draw_chart(ax, snapshot: dict, reveal: float = 1.0):
    """Price line for the configured window. `reveal` in (0,1] draws a prefix."""
    t = _theme()
    closes = [c[4] for c in snapshot["candles"]]
    if len(closes) < 4:
        return
    n = max(3, int(len(closes) * reveal))
    ys = closes[:n]

    x0, x1 = 96, W - 96
    y0, y1 = 560, 1180
    lo, hi = min(closes), max(closes)
    span = (hi - lo) or 1.0

    def px(i: int) -> float:
        return x0 + (x1 - x0) * (i / (len(closes) - 1))

    def py(v: float) -> float:
        return y1 - (y1 - y0) * ((v - lo) / span)

    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = y0 + (y1 - y0) * frac
        ax.plot([x0, x1], [y, y], color=t["grid"], lw=1.5, zorder=2)

    xs = [px(i) for i in range(n)]
    pys = [py(v) for v in ys]
    up = ys[-1] >= ys[0]
    line_color = t["up"] if up else t["down"]

    ax.plot(xs, pys, color=line_color, lw=5, zorder=4, solid_capstyle="round")
    ax.fill_between(xs, pys, [y1] * len(xs), color=line_color, alpha=0.12, zorder=3)
    ax.scatter([xs[-1]], [pys[-1]], s=220, color=line_color, zorder=5)
    ax.scatter([xs[-1]], [pys[-1]], s=70, color=t["bg"], zorder=6)

    _text(ax, x1, y0 - 34, f"${hi:,.0f}", size=28, color=t["muted"], ha="right")
    _text(ax, x1, y1 + 34, f"${lo:,.0f}", size=28, color=t["muted"], ha="right")
    _text(ax, x0, y1 + 34, f"{config()['market']['chart_days']}-day price",
          size=28, color=t["muted"])


def scene_call(snapshot: dict, script: dict, path: Path, reveal: float = 1.0) -> Path:
    t = _theme()
    fig, ax = _canvas()
    _header(ax, snapshot, "today's call")

    sig = snapshot["signal"]
    color = _signal_color(sig)

    _panel(ax, 64, 250, W - 128, 220)
    _text(ax, 110, 322, "SIGNAL", size=30, color=t["muted"])
    _text(ax, 110, 400, sig, size=86, weight="bold", color=color)
    conf = snapshot.get("confidence")
    if conf is not None:
        _text(ax, W - 110, 322, "CONFIDENCE", size=30, color=t["muted"], ha="right")
        _text(ax, W - 110, 400, f"{conf:.1f}%", size=86, weight="bold", ha="right")

    _draw_chart(ax, snapshot, reveal)

    if snapshot.get("gated"):
        _panel(ax, 64, 1250, W - 128, 150, color=t["panel"])
        _text(ax, 110, 1300, "CONFIDENCE GATE", size=28, weight="bold", color=t["flat"])
        gate = f"{snapshot.get('signal_raw')} downgraded to HOLD"
        thr = snapshot.get("confidence_threshold_pct")
        if thr:
            gate += f" · under {thr:.0f}%"
        _text(ax, 110, 1360, gate, size=34, color=t["text"])
    else:
        overlay = script["overlays"].get("call")
        if overlay:
            _text(ax, W / 2, 1320, overlay, size=44, color=t["muted"], ha="center")

    return _save(fig, path)


def scene_why(snapshot: dict, script: dict, path: Path) -> Path:
    t = _theme()
    fig, ax = _canvas()
    _header(ax, snapshot, "what the models saw")

    # Four model votes — the ensemble disagreeing with itself is usually the
    # most informative thing on screen.
    votes = snapshot.get("per_model") or {}
    order = [("lstm", "LSTM"), ("transformer", "TRANS"), ("xgboost", "XGB"), ("kan", "KAN")]
    # 224px boxes at font 40: "SELL" is the widest label and overflowed a
    # 216px box at 46.
    bw, gap = 224, 20
    total = len(order) * bw + (len(order) - 1) * gap
    x = (W - total) / 2
    for key, label in order:
        v = votes.get(key) or "—"
        _panel(ax, x, 270, bw, 240, color=t["panel"])
        _text(ax, x + bw / 2, 336, label, size=30, color=t["muted"], ha="center")
        _text(ax, x + bw / 2, 430, v, size=40, weight="bold",
              color=_signal_color(v), ha="center")
        x += bw + gap

    # Probability split as a single stacked bar.
    bd = snapshot.get("breakdown") or {}
    if bd:
        _text(ax, 96, 620, "PROBABILITY SPLIT", size=30, weight="bold", color=t["muted"])
        bx, by, bwid, bh = 96, 670, W - 192, 88
        cursor = bx
        for cls in ("BUY", "HOLD", "SELL"):
            frac = float(bd.get(cls) or 0) / 100.0
            seg = bwid * frac
            if seg <= 0:
                continue
            ax.add_patch(FancyBboxPatch(
                (cursor, by), max(seg - 4, 2), bh,
                boxstyle="round,pad=0,rounding_size=12",
                facecolor=_signal_color(cls), edgecolor="none", zorder=3))
            # Label degrades with the segment width rather than overflowing it:
            # full label, then just the number, then nothing. A SELL slice
            # around 20% is narrower than the words "SELL 20%" rendered bold.
            if seg > 250:
                label = f"{cls} {bd[cls]:.0f}%"
            elif seg > 120:
                label = f"{bd[cls]:.0f}%"
            else:
                label = ""
            if label:
                _text(ax, cursor + seg / 2, by + bh / 2, label,
                      size=28, weight="bold", color=t["bg"], ha="center")
            cursor += seg

    # Context bullets, straight from the script's overlay list.
    y = 860
    for line in (script["overlays"].get("why") or [])[:3]:
        _panel(ax, 96, y, W - 192, 116, color=t["panel"])
        ax.plot([136, 136], [y + 34, y + 82], color=t["accent"], lw=8,
                solid_capstyle="round", zorder=3)
        _text(ax, 176, y + 58, line, size=38)
        y += 140

    # Only add the Fear & Greed line when a bullet has not already said it —
    # otherwise the same number appears twice in one frame.
    fg = snapshot.get("fear_greed")
    bullets = " ".join(script["overlays"].get("why") or []).lower()
    if fg and "fear" not in bullets and "greed" not in bullets:
        _text(ax, W / 2, y + 60, f"Fear & Greed: {fg['value']} · {fg['classification']}",
              size=36, color=t["muted"], ha="center")

    return _save(fig, path)


def scene_score(snapshot: dict, script: dict, score: dict, path: Path) -> Path:
    t = _theme()
    fig, ax = _canvas()
    _header(ax, snapshot, "the public record")

    resolved = score.get("resolved_calls") or 0
    acc = score.get("accuracy_pct")

    if not resolved:
        _text(ax, W / 2, 640, "DAY ONE", size=120, weight="bold", ha="center",
              color=t["accent"])
        for i, line in enumerate(_wrap(
                "Every call from today is logged and graded 24 hours later. "
                "The record starts now.", 26)[:4]):
            _text(ax, W / 2, 820 + i * 74, line, size=44, color=t["muted"], ha="center")
        return _save(fig, path)

    # Yesterday's graded call, stated before the aggregate — the aggregate is
    # easy to spin, a single graded call is not.
    last = score.get("last_resolved") or {}
    if last:
        hit = last.get("correct")
        col = t["up"] if hit else t["down"]
        _panel(ax, 64, 260, W - 128, 240, color=t["panel"])
        _text(ax, 110, 326, f"LAST GRADED · {last.get('date', '')}", size=30, color=t["muted"])
        _text(ax, 110, 412, f"{last.get('signal', '—')} → {last.get('change_pct', 0):+.2f}%",
              size=58, weight="bold")
        _text(ax, W - 110, 412, "HIT" if hit else "MISS", size=64, weight="bold",
              color=col, ha="right")

    _text(ax, W / 2, 640, f"{acc:.0f}%", size=170, weight="bold", ha="center",
          color=t["up"] if acc and acc >= 50 else t["down"])
    _text(ax, W / 2, 780, f"{score.get('hits', 0)} hits · {score.get('misses', 0)} misses "
                          f"· {resolved} graded calls", size=40, color=t["muted"], ha="center")

    # Last 20 outcomes as a dot strip: the honest shape of the record at a glance.
    from scoreboard import _load                                   # local: avoids cycle
    rows = [r for r in _load() if r.get("resolved")][-20:]
    if rows:
        _text(ax, 96, 900, "LAST 20 CALLS", size=30, weight="bold", color=t["muted"])
        dot, dgap = 40, 12
        strip = len(rows) * dot + (len(rows) - 1) * dgap
        x = (W - strip) / 2
        for r in rows:
            ax.add_patch(plt.Circle((x + dot / 2, 990), dot / 2,
                                    color=t["up"] if r["correct"] else t["down"], zorder=3))
            x += dot + dgap

    a30 = score.get("accuracy_30d_pct")
    streak = score.get("streak") or {}
    y = 1090
    rows_txt = [("All time", f"{acc:.0f}%" if acc is not None else "—"),
                ("Last 30 days", f"{a30:.0f}%" if a30 is not None else "—"),
                ("Current streak", f"{streak.get('length', 0)} "
                                   f"{'hits' if streak.get('kind') == 'hit' else 'misses'}")]
    for label, value in rows_txt:
        _panel(ax, 96, y, W - 192, 104, color=t["panel"])
        _text(ax, 144, y + 52, label, size=36, color=t["muted"])
        _text(ax, W - 144, y + 52, value, size=42, weight="bold", ha="right")
        y += 124

    _text(ax, W / 2, y + 50,
          f"Graded on the {score.get('horizon_hours', 24)}h move · "
          f"flat band ±{score.get('flat_band_pct', 1)}%",
          size=30, color=t["muted"], ha="center")

    return _save(fig, path)


def scene_outro(snapshot: dict, path: Path) -> Path:
    """Fallback end card, used when no Veo outro asset is present."""
    t = _theme()
    fig, ax = _canvas()
    _header(ax, snapshot, "")

    _text(ax, W / 2, 700, "NOT FINANCIAL", size=76, weight="bold", ha="center")
    _text(ax, W / 2, 800, "ADVICE", size=76, weight="bold", ha="center")
    for i, line in enumerate(_wrap(
            "An automated research project. The model is often wrong — "
            "that is why the scoreboard is public.", 26)[:4]):
        _text(ax, W / 2, 950 + i * 74, line, size=40, color=t["muted"], ha="center")
    _text(ax, W / 2, 1300, "New call every day", size=48, weight="bold",
          color=t["accent"], ha="center")
    return _save(fig, path)


# ── Captions ──────────────────────────────────────────────────────────────────

def _ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_captions(narration: Narration, path: Path) -> Path:
    """Burned-in captions. Shorts are largely watched muted, so these are not
    an accessibility extra — without them the video does not communicate."""
    t = _theme()

    def ass_colour(hexstr: str) -> str:
        h = hexstr.lstrip("#")
        return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}"                     # ASS is BGR

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,DejaVu Sans,58,{ass_colour(t['text'])},{ass_colour(t['text'])},&H00000000,&HB4000000,-1,0,0,0,100,100,0,0,3,14,0,2,70,70,210,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for seg in narration.segments:
        text = "\\N".join(_wrap(seg.text, 26)[:3])
        lines.append(f"Dialogue: 0,{_ass_time(seg.start)},{_ass_time(seg.end)},"
                     f"Cap,,0,0,0,,{text}")
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    return path


# ── Assembly ──────────────────────────────────────────────────────────────────

def _still_clip(png: Path, duration: float, out: Path, drift: bool = True) -> Path:
    """A still image as a clip, with a slow drift so it does not read as a
    frozen slide.

    The drift is a pan across a slightly oversized still, not a zoompan. The
    obvious zoompan formulation has to resample at 2x resolution to avoid its
    own sub-pixel jitter, and that resampling dominated the entire render —
    minutes per clip instead of seconds. A crop window moving over a pre-scaled
    image is visually equivalent here and costs almost nothing.
    """
    fps = config()["video"]["fps"]
    if drift:
        px = max(2, int(config()["video"]["drift_px"]))
        # min(...,1) keeps the window inside the frame if ffmpeg emits a final
        # timestamp fractionally past the requested duration.
        prog = f"min(t/{max(duration, 0.001):.3f},1)"
        vf = (f"scale={W + px}:{H + px},"
              f"crop={W}:{H}:x='(iw-ow)*{prog}':y='(ih-oh)*{prog}',"
              f"fps={fps}")
    else:
        vf = f"scale={W}:{H},fps={fps}"
    _run(["-loop", "1", "-i", str(png), "-t", f"{duration:.3f}", "-vf", vf, *_ENC, str(out)],
         f"still clip {png.name}")
    return out


def _sequence_clip(pattern: str, out: Path) -> Path:
    fps = config()["video"]["fps"]
    _run(["-framerate", str(fps), "-i", pattern, "-vf", f"scale={W}:{H}", *_ENC, str(out)],
         "chart reveal")
    return out


def _concat(clips: list[Path], out: Path) -> Path:
    listing = BUILD_DIR / "concat.txt"
    listing.write_text("\n".join(f"file '{c.as_posix()}'" for c in clips), encoding="utf-8")
    _run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)], "concat")
    return out


def _normalise(src: Path, out: Path) -> Path:
    """Bring a hand-made asset (Veo intro/outro) to the pipeline's exact
    format, so concatenation cannot fail on a parameter mismatch."""
    fps = config()["video"]["fps"]
    _run(["-i", str(src),
          "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                 f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,fps={fps}",
          "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
          "-shortest", str(out)], f"normalise {src.name}")
    return out


def _has_audio(path: Path) -> bool:
    probe = shutil.which("ffprobe")
    if not probe:
        return True                       # assume yes; _normalise adds a track anyway
    proc = subprocess.run([probe, "-v", "error", "-select_streams", "a",
                           "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                          capture_output=True, text=True)
    return bool(proc.stdout.strip())


def render(snapshot: dict, script: dict, score: dict, narration: Narration,
           out_name: str = "video.mp4") -> Path:
    cfg = config()["video"]
    fps = cfg["fps"]
    ffmpeg_path()                          # fail early with a helpful message

    for stale in BUILD_DIR.glob("clip_*.mp4"):
        stale.unlink()
    if FRAMES_DIR.exists():
        shutil.rmtree(FRAMES_DIR)
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)

    log.info("Rendering scenes...")
    clips: list[Path] = []

    # ── hook ──
    d = narration.section_duration("hook") + config()["voice"]["sentence_gap_seconds"]
    png = scene_hook(snapshot, script, BUILD_DIR / "scene_hook.png")
    clips.append(_still_clip(png, max(d, 2.0), BUILD_DIR / "clip_0_hook.mp4"))

    # ── call: chart draws itself in, then holds ──
    d_call = max(narration.section_duration("call"), 4.0)
    reveal_secs = min(1.6, d_call * 0.35)
    n_frames = max(6, int(reveal_secs * fps))
    log.info(f"  Drawing chart reveal ({n_frames} frames)...")
    for i in range(n_frames):
        frac = (i + 1) / n_frames
        # Ease-out: the line races ahead then settles, which reads as motion
        # rather than a metronome.
        eased = 1 - (1 - frac) ** 2
        scene_call(snapshot, script, FRAMES_DIR / f"call_{i:04d}.png", reveal=max(eased, 0.05))
    clips.append(_sequence_clip(str(FRAMES_DIR / "call_%04d.png"),
                                BUILD_DIR / "clip_1a_call.mp4"))
    hold = max(d_call - reveal_secs, 0.6)
    clips.append(_still_clip(FRAMES_DIR / f"call_{n_frames - 1:04d}.png", hold,
                             BUILD_DIR / "clip_1b_call.mp4"))

    # ── why ──
    png = scene_why(snapshot, script, BUILD_DIR / "scene_why.png")
    clips.append(_still_clip(png, max(narration.section_duration("why"), 3.0),
                             BUILD_DIR / "clip_2_why.mp4"))

    # ── score ──
    png = scene_score(snapshot, script, score, BUILD_DIR / "scene_score.png")
    clips.append(_still_clip(png, max(narration.section_duration("score"), 3.0),
                             BUILD_DIR / "clip_3_score.mp4"))

    # ── outro (drawn card; a Veo asset, if present, is appended after) ──
    png = scene_outro(snapshot, BUILD_DIR / "scene_outro.png")
    clips.append(_still_clip(png, max(narration.section_duration("outro"), 2.5),
                             BUILD_DIR / "clip_4_outro.mp4", drift=False))

    log.info("Assembling body...")
    body = _concat(clips, BUILD_DIR / "body_silent.mp4")

    # Captions + narration onto the body.
    body_av = BUILD_DIR / "body.mp4"
    args = ["-i", str(body), "-i", str(narration.wav_path)]
    if cfg["captions"]:
        ass = build_captions(narration, BUILD_DIR / "captions.ass")
        # ffmpeg's filter parser needs the drive colon escaped on Windows.
        escaped = ass.as_posix().replace(":", r"\:")
        args += ["-vf", f"subtitles='{escaped}'", "-c:v", "libx264", "-preset", "medium",
                 "-crf", "20", "-pix_fmt", "yuv420p"]
    else:
        args += ["-c:v", "copy"]
    args += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-shortest",
             str(body_av)]
    _run(args, "captions + audio")

    # Optional branded bookends.
    parts = [body_av]
    intro_src = BASE_DIR / cfg["intro"]
    outro_src = BASE_DIR / cfg["outro"]
    if intro_src.exists():
        parts.insert(0, _normalise(intro_src, BUILD_DIR / "intro_norm.mp4"))
        log.info("  Using branded intro")
    if outro_src.exists():
        parts.append(_normalise(outro_src, BUILD_DIR / "outro_norm.mp4"))
        log.info("  Using branded outro")

    final = BUILD_DIR / out_name
    if len(parts) == 1:
        shutil.copy(body_av, final)
    else:
        # filter_complex concat rather than the demuxer: the hand-made assets
        # come from a different encoder and only a re-encode is guaranteed safe.
        inputs = []
        for p in parts:
            inputs += ["-i", str(p)]
        streams = "".join(f"[{i}:v][{i}:a]" for i in range(len(parts)))
        _run([*inputs, "-filter_complex", f"{streams}concat=n={len(parts)}:v=1:a=1[v][a]",
              "-map", "[v]", "-map", "[a]",
              "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-b:a", "160k", str(final)], "final concat")

    size_mb = final.stat().st_size / (1 << 20)
    log.info(f"  Video: {final.name} ({size_mb:.1f} MB)")
    return final


def duration_of(path: Path) -> float | None:
    probe = shutil.which("ffprobe")
    if not probe:
        return None
    proc = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                           "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None
