# Golden Sample — quality regression set (build 230)

A fixed set of short reference clips whose known-good pipeline
outputs are stored as baselines. Re-running the pipeline on the
same clips and comparing against the baselines catches silent
quality degradation — a free translation engine swapped or
degraded by its provider, a TTS model change, a prompt
regression — before customers do.

## Layout

- `golden_set.json` — the set definition (ids, genres, sources).
- `clips/` — the clips, stored as `.mp4.b64` text because the
  deploy channel is text-only (same constraint as the fonts).
  `pipeline/golden_check.py` materializes the `.mp4` files next
  to them on first use; a materialized `.mp4` with a matching
  size is never re-decoded.
- `baselines/<id>.json` — captured outputs (source segments,
  Myanmar segments, per-segment provider labels) + the metrics
  the comparison uses.
- `reports/latest.json` / `latest.txt` — the most recent check.
- `work/` — scratch workdirs of the last capture/check runs.

This directory lives in the project root, OUTSIDE `jobs/`, so
the 24h job-data auto-delete (build 215, which only removes
`jobs/<id>` dirs) can never reap it.

## The set

| id | genre | lang | origin |
|---|---|---|---|
| g1-narration-en | narration | en | real speech ("Me at the zoo"), reused from `verification/` — also the TTS sample clip |
| g2-drama-zh | drama dialogue | zh | synthetic two-speaker (gTTS) |
| g3-drama-en | drama dialogue | en | synthetic two-speaker (gTTS) |
| g4-fast-en | fast speech | en | synthetic, 1.45x tempo |
| g5-music-en | music / song | en | synthetic lyric lines over a generated chord bed |

The synthetic clips are deterministic and license-clean. To use
a real drama or song clip instead, replace the clip file
(`clips/<id>.mp4`, or re-encode to `.mp4.b64` with
`base64 file.mp4 > file.mp4.b64`), keep the same id, and
re-capture baselines.

## Running

- Admin UI: Admin → API panel → "🥇 Golden Sample" card —
  "အခု စစ်မယ်" runs a check, "Baseline အသစ် မှတ်မယ်" re-captures.
- API (admin-only): `GET /api/admin/golden-check` (status +
  last report), `POST /api/admin/golden-check` (start check),
  `POST /api/admin/golden-check/capture` (start re-capture).
- CLI: `python -m pipeline.golden_check` (check; exit 0 PASS /
  1 WARN / 2 FAIL), `python -m pipeline.golden_check --capture`.

## Baselines are per-environment

The engines that serve a run differ by environment: locally
only the free chain answers (no keys), on the server Gemini /
OpenRouter lead. After deploying build 230, re-capture ONCE on
the server so its baselines reflect production engines. Also
re-capture after any intentional engine, model, or prompt
change — the new output becomes the standard on purpose,
instead of warning forever.

## What a WARN/FAIL means

- `untranslated` FAIL — the clip would trip production's
  majority-untranslated gate: translation is effectively down
  or degraded for that content type.
- `engine` WARN — a different provider/model served the clip
  than served the baseline. First place to look when quality
  "suddenly feels worse".
- `seg_count` / `src_chars` drift — Whisper's hearing changed
  (model or audio-path change).
- `my_chars` drift — translation verbosity changed materially.
- `tts` — the Thiha sample failed, went silent on segments, or
  its spoken duration drifted (voice/model change).
