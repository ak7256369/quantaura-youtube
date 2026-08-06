"""Narration via Kokoro-82M (ONNX, CPU, Apache-2.0).

Chosen over every hosted TTS because it has no quota, no per-character cost and
no commercial-use ambiguity — the three things that would otherwise put a
recurring bill or a licensing question on a daily channel.

Synthesis is per sentence rather than per paragraph. That costs nothing extra
and buys exact caption timings: the renderer needs to know when each sentence
starts, and measuring an isolated clip is precise where forced alignment is a
guess.
"""
from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from common import BUILD_DIR, MODELS_DIR, PipelineAbort, config, http_session, log

SAMPLE_RATE_FALLBACK = 24000


@dataclass
class Segment:
    text: str
    start: float
    end: float
    section: str


@dataclass
class Narration:
    wav_path: Path
    duration: float
    segments: list[Segment]

    def section_duration(self, section: str) -> float:
        segs = [s for s in self.segments if s.section == section]
        return (segs[-1].end - segs[0].start) if segs else 0.0

    def section_spans(self, sections: list[str]) -> dict[str, float]:
        """Durations that tile the whole track, leaving nothing over.

        section_duration measures first-sentence-start to last-sentence-end,
        so it omits the pause that follows each section. Building a video from
        those leaves the picture shorter than the voice — by about a second
        across five sections — and ffmpeg's -shortest then trims the audio to
        match, cutting the closing words of the disclaimer.

        These spans run start-of-section to start-of-next instead, with the
        last section extending to the end of the audio, so the sum is the full
        narration length by construction.
        """
        starts = {}
        for s in sections:
            segs = [g for g in self.segments if g.section == s]
            if segs:
                starts[s] = segs[0].start
        present = [s for s in sections if s in starts]
        spans = {}
        for i, s in enumerate(present):
            end = starts[present[i + 1]] if i + 1 < len(present) else self.duration
            spans[s] = max(end - starts[s], 0.1)
        return spans

    def section_bounds(self, section: str) -> tuple[float, float]:
        segs = [s for s in self.segments if s.section == section]
        return (segs[0].start, segs[-1].end) if segs else (0.0, 0.0)


# ── Model files ───────────────────────────────────────────────────────────────

def _download(url: str, dest: Path) -> None:
    log.info(f"  Downloading {dest.name} (one-time, ~{'320' if dest.suffix == '.onnx' else '27'}MB)...")
    with http_session().get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
        tmp.replace(dest)


def ensure_model() -> tuple[Path, Path]:
    """Fetch the Kokoro weights if absent. CI caches MODELS_DIR, so this
    normally runs once per runner image rather than once per day."""
    cfg = config()["voice"]
    model = MODELS_DIR / "kokoro-v1.0.onnx"
    voices = MODELS_DIR / "voices-v1.0.bin"
    try:
        if not model.exists():
            _download(cfg["model_url"], model)
        if not voices.exists():
            _download(cfg["voices_url"], voices)
    except Exception as e:                                       # noqa: BLE001
        raise PipelineAbort(f"Could not obtain Kokoro model files: {e}")
    return model, voices


# ── Synthesis ─────────────────────────────────────────────────────────────────

def _write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def synthesize(sentences: list[tuple[str, str]], *, out_name: str = "narration.wav",
               silent: bool = False) -> Narration:
    """`sentences` is a list of (section, text). Returns the joined audio plus
    per-sentence timings."""
    cfg = config()["voice"]
    gap = float(cfg["sentence_gap_seconds"])
    out_path = BUILD_DIR / out_name

    if silent:
        # Lets the renderer be exercised without loading a 320MB model — used
        # by `pipeline.py --no-voice` when iterating on the visual template.
        rate = SAMPLE_RATE_FALLBACK
        chunks, segments, cursor = [], [], 0.0
        for section, text in sentences:
            dur = max(1.4, len(text.split()) / 2.7)
            chunks.append(np.zeros(int(dur * rate), dtype=np.float32))
            segments.append(Segment(text, cursor, cursor + dur, section))
            cursor += dur + gap
            chunks.append(np.zeros(int(gap * rate), dtype=np.float32))
        audio = np.concatenate(chunks) if chunks else np.zeros(rate, dtype=np.float32)
        _write_wav(out_path, audio, rate)
        log.info(f"  Silent placeholder narration: {cursor:.1f}s")
        return Narration(out_path, len(audio) / rate, segments)

    try:
        from kokoro_onnx import Kokoro
    except ImportError as e:
        raise PipelineAbort(
            f"kokoro-onnx is not installed ({e}). Run: pip install -r channel/requirements.txt")

    model_path, voices_path = ensure_model()
    log.info(f"  Synthesising {len(sentences)} sentences with Kokoro ({cfg['voice_id']})...")

    try:
        kokoro = Kokoro(str(model_path), str(voices_path))
    except Exception as e:                                       # noqa: BLE001
        raise PipelineAbort(f"Kokoro failed to load: {e}")

    chunks: list[np.ndarray] = []
    segments: list[Segment] = []
    cursor = 0.0
    rate = SAMPLE_RATE_FALLBACK

    for section, text in sentences:
        try:
            samples, rate = kokoro.create(text, voice=cfg["voice_id"],
                                          speed=float(cfg["speed"]), lang=cfg["lang"])
        except Exception as e:                                   # noqa: BLE001
            raise PipelineAbort(f"Kokoro failed on \"{text[:60]}...\": {e}")

        samples = np.asarray(samples, dtype=np.float32)
        dur = len(samples) / rate
        segments.append(Segment(text, cursor, cursor + dur, section))
        chunks.append(samples)
        cursor += dur

        # Trailing pause after each sentence, so the renderer's scene cuts land
        # in silence rather than mid-word.
        chunks.append(np.zeros(int(gap * rate), dtype=np.float32))
        cursor += gap

    if not chunks:
        raise PipelineAbort("No narration sentences to synthesise")

    audio = np.concatenate(chunks)
    _write_wav(out_path, audio, rate)
    duration = len(audio) / rate
    log.info(f"  Narration: {duration:.1f}s across {len(segments)} sentences")
    return Narration(out_path, duration, segments)


def sentences_from_script(script: dict, disclaimer: str) -> list[tuple[str, str]]:
    """Flatten the script into (section, sentence) pairs.

    The closing disclaimer is appended here, in code. It is never generated,
    never optional, and never something a model can drop.
    """
    from scriptwriter import SECTIONS
    pairs = [(sec, s) for sec in SECTIONS for s in script["sections"][sec]]
    pairs.append(("outro", disclaimer))
    return pairs
