# QuantAura Channel Pipeline

Generates and publishes the channel's content automatically:

- **Daily Short** (18:00 UTC, `daily-video.yml`) — the ensemble's live Bitcoin
  call plus the public accuracy record. Timed for the US afternoon/evening watch
  window; a 21:00 UTC catch-up covers a dropped GitHub cron slot.
- **Weekly recap** (Sunday 20:00 UTC, `weekly-video.yml`) — every graded call of
  the week reviewed in a 1080p long-form video: the call table, the week on the
  chart, a rotating research segment, and the watchlist teaser. Skips the week
  if fewer than `weekly.min_resolved_calls` calls were graded — a recap of one
  call is filler. Run locally with `python weekly.py --dry-run`.

Both share one publish path (`upload.publish`), one voice, one theme, and one
state log, and their workflows share a concurrency group so state commits never
race.

Runs unattended on GitHub Actions. Total running cost: **$0/month** — free API
tiers, an open-source voice model, and a renderer that draws real charts instead
of generating them.

```
fetch → grade past calls → write script → fact-check → voice → render → upload → notify
```

## Why it is built this way

| Decision | Reason |
|---|---|
| Charts drawn with matplotlib, not a video model | The content *is* the numbers. A generative model cannot be trusted to render a price or a percentage correctly. |
| Kokoro-82M for voice | No quota, no per-character cost, Apache-2.0 — the only TTS that stays free at one video a day forever. |
| Two LLM providers | Free tiers hit quota walls. A provider outage must not cost a day. |
| Fact-check before publish | Nothing unattended should be able to state a number it cannot source. |
| Skip the day on doubt | A missing video is invisible. A wrong one is permanent. |
| Scoreboard in git | Calls are written down before outcomes are knowable, and never edited. |

## Local setup

```bash
pip install -r requirements.txt
```

ffmpeg must be on PATH:

```bash
winget install Gyan.FFmpeg
```

Check the plumbing end to end without touching a live API or spending a token:

```bash
python pipeline.py --dry-run
```

That writes `build/video.mp4`. Watch it — this is the template every
future video inherits.

Then a real build with live data, no upload:

```bash
python pipeline.py --no-upload
```

### Flags

| Flag | Effect |
|---|---|
| `--dry-run` | Synthetic data, no API calls, no LLM, silent audio, no upload |
| `--no-voice` | Silent placeholder audio — fast iteration on the visual template |
| `--no-upload` | Build everything from live data, publish nothing |
| `--no-drive` | Skip the Google Drive mirror; still uploads to YouTube |
| `--check-drive` | Verify Drive credentials and folder, then exit. Renders nothing, writes nothing. Also a `workflow_dispatch` input, which is the point: the token is minted locally but spent in Actions, and this is what proves the two agree. |
| `--drive-only` | Mirror to Drive for an X post, without uploading to YouTube. On a day already published, the workflow restores that run's artifact and this mirrors **the exact file that went to YouTube** rather than re-rendering a near-identical one; it falls back to rendering if the artifact is gone (7-day retention). Reads the scoreboard but never writes to it, so it is safe to rerun — use this rather than `--force`, which would publish a second video and move the day's recorded call to a later timestamp. |

## Secrets

All secrets come from environment variables. Nothing is ever committed.
Locally, export them or use a `.env`-style shell; in CI they are repository
secrets (Settings → Secrets and variables → Actions).

| Secret | Needed for | How to get it |
|---|---|---|
| `QA_EMAIL`, `QA_PASSWORD` | Full model output | Register an account on quantaura.tech, then grant it premium from the admin panel. Free-tier callers do not receive confidence or per-model votes. |
| `GEMINI_API_KEY` | Script writing | [aistudio.google.com](https://aistudio.google.com) → Get API key. Free tier. |
| `GROQ_API_KEY` | Script fallback | [console.groq.com](https://console.groq.com) → API keys. Free tier. |
| `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN` | Upload | See below |
| `GDRIVE_REFRESH_TOKEN` | Drive mirror for the manual X post | See below |
| `GDRIVE_FOLDER_ID` | *Optional* — pins the Drive folder | Printed by `get_drive_token.py` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Notifications | `@BotFather` → `/newbot`; get the chat id from `@userinfobot` |

Only the Gemini key is needed to try the pipeline. Everything else degrades
gracefully or is upload-only.

### YouTube credentials (one-time)

1. [Google Cloud Console](https://console.cloud.google.com) → new project.
2. APIs & Services → Library → enable **YouTube Data API v3**.
3. OAuth consent screen → External → add your own Google account as a test user.
4. Credentials → Create credentials → OAuth client ID → **Desktop app** →
   download `client_secret.json`.
5. Mint the refresh token on your own machine:

```bash
python get_youtube_token.py client_secret.json
```

It prints the three secrets to paste into GitHub. Treat the refresh token like
a password.

> **Uploads are public** (`config.yaml` → `upload.visibility: public`), live
> since 2026-09-03 after the YouTube API audit was approved (2026-08-07) and a
> clean private-upload rollout. Before the audit, Google locked an unaudited
> project's uploads to private regardless of what the pipeline requested; set the
> value back to `private` if you ever need an operator-review tap again.

### Google Drive credentials (one-time)

Every daily video is mirrored to Google Drive so it can be posted to **X by
hand**. X's API has been pay-per-use since 2026-02-06 — no free write tier, and
$0.200 for any post containing a link — so the automated poster in the
`quantaura-x` repo is switched off and this is how the daily call reaches X.

Same Google Cloud project and the same `client_secret.json` as YouTube; a
separate grant, so a broken Drive consent cannot take the channel upload down
with it.

1. APIs & Services → Library → enable **Google Drive API**.
2. Mint the Drive refresh token on your own machine:

```bash
python get_drive_token.py client_secret.json
```

It creates the destination folder, prints its id, and prints
`GDRIVE_REFRESH_TOKEN` to paste into GitHub.

The scope is `drive.file` — the narrowest Drive scope there is. It can only see
and touch files this app itself created, never the rest of your Drive. Google
classifies it **non-sensitive**, so it needs no verification review of its own
and cannot disturb the pending YouTube API audit.

Each run leaves two files in the folder:

| File | Contents |
|---|---|
| `quantaura-YYYY-MM-DD.mp4` | The daily 1080×1920 Short, ~40–70s — inside X's 140s cap for non-Premium accounts |
| `quantaura-YYYY-MM-DD.txt` | The caption, ≤280 weighted chars, ready to paste |
| `quantaura-week-YYYY-Wnn.mp4` | The Sunday recap, 1920×1080 and minutes long. **Over X's 140s cap** — mirrored for trimming or for posting elsewhere, not to go up whole |
| `quantaura-week-YYYY-Wnn.txt` | The recap caption — the week's record, no live call |

Uploads replace by name, so re-running a mirror for the same day or week
updates the file in place instead of leaving a second copy behind.

`weekly.py` takes `--no-drive` and `--drive-only` too, both also exposed as
`workflow_dispatch` inputs. The weekly `--drive-only` skips three things rather
than the daily's two: the YouTube upload, the scoreboard grading, **and the blog
post** — the recap's written post went out with the scheduled run, and
republishing it to produce a video clip would duplicate a live page.

The caption also arrives on Telegram in a copy-button block alongside the Drive
link, which is the intended workflow: tap, download, copy, post. A Drive failure
never fails the day — the video still goes to YouTube and the mp4 is still in
the workflow artifact.

## YouTube Studio settings (one-time)

- Settings → Channel → Advanced → set the channel default for **altered or
  synthetic content** disclosure. Every description also states it, but the
  channel-level flag is the one YouTube's policy checks.
- Upload defaults → category, language, and a base description.

## Files

| File | Job |
|---|---|
| `pipeline.py` | Orchestrator. Owns the failure policy. |
| `fetch.py` | ml-api call, Binance klines, Fear & Greed |
| `scoreboard.py` | Append-only prediction log, grading, running accuracy |
| `scriptwriter.py` | Facts → narration JSON, schema-validated |
| `factcheck.py` | Banned phrases, numeric grounding, adversarial LLM review |
| `llm.py` | Gemini → Groq fallback, defensive JSON parsing |
| `voice.py` | Kokoro TTS with per-sentence timings |
| `render.py` | Scene drawing, captions, ffmpeg assembly |
| `thumbnail.py` | Pillow text over an optional AI background |
| `upload.py` | YouTube Data API v3 |
| `drive.py` | Drive mirror + X caption, for posting to X by hand |
| `notify.py` | Telegram operator messages |
| `state/` | The scoreboard. Committed after every run. |

## The scoring rule

A call is graded on the realised price move over the model's forecast horizon
(24h by default):

- **BUY** is right if the move is above `+flat_band_pct`
- **SELL** is right if the move is below `-flat_band_pct`
- **HOLD** is right if the move stays inside the band

This is deliberately *not* the model's training label (a trend-regime label
built from an EMA spread). A viewer can verify a price move on any chart; they
cannot verify an EMA regime. Grading on something the audience can check is the
entire point of the channel — so the simpler, harsher rule wins.

Both numbers are in `config.yaml` under `scoring`, and both are stated on screen
and in every description.

## Failure policy

| Failure | Behaviour |
|---|---|
| ml-api down, or serving a TA fallback | Skip the day, notify |
| Gemini quota or outage | Automatic Groq fallback |
| Script fails fact-check twice | Skip the day, notify |
| Render or ffmpeg error | Skip the day, notify with the log excerpt |
| Upload fails | Keep the mp4 as a CI artifact (7 days), notify for manual upload |
| Actions outage | `workflow_dispatch` — trigger by hand from a phone |

A skipped day still commits its graded outcomes and the run log.
