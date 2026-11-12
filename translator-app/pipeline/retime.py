"""Step 5: TIMING SYNC — fit the Myanmar dub into each segment's time window.

Rules (owner's spec, revised 2026-11-09: the picture must NEVER stop):
  * Fit order: speed the SPEECH up first (atempo, clamped to the job's
    speed cap). Whatever timing difference remains is absorbed by the
    VIDEO's playback rate — slow where the dub runs long, fast where
    the dub runs short — so the picture keeps moving at all times.
  * FREEZE FRAMES ARE GONE: no tpad/clone anywhere. In the extreme
    tail (dub many times longer than its window even at the audio
    cap) the video simply slows further; speech is still never cut.
  * Video speed-up is capped at MAX_VFAST so a very short dub cannot
    turn the picture into a fast-forward joke; the small remainder
    pads with silence at the chunk tail (picture still moving).
  * Under the narration, the ORIGINAL track plays as a quiet bed:
    centre-cut when stereo (the original dialogue sits in the
    centre and largely cancels; wide music/effects survive) at
    BED_VOL — the owner wants the music kept as background only and
    the original voices gone. Mono sources get a whisper-level bed.
  * Non-speech gaps keep the original audio at low volume (ambience).

Implementation: split the source into chunks (gaps + segments), render each
chunk's video (rate-adjusted) and audio (atempo dub + quiet bed, or quiet
original for gaps), concat both, and emit re-timed subtitle timings.

CLI: python -m pipeline.retime <workdir>
Reads source.mp4 + segments_my.json (with tts_file/tts_duration).
Writes retimed.mp4, dub.wav, segments_retimed.json, subs_retimed.srt
"""
import sys
from pathlib import Path

from .common import (load_segments, probe_duration, run, save_segments,
                     target_maxrate_k, write_srt)

MAX_SPEED = 1.30
MIN_VSLOW = 0.80   # legacy stat only; the plan no longer floors at it
MAX_VFAST = 1.6    # fastest the picture may play for a short dub
GAP_VOL = 0.25
BED_VOL = 0.22         # music bed under narration (stereo, centre-cut)
BED_VOL_MONO = 0.06    # mono: no centre-cut possible, keep it a whisper


def _plan(segs, total_dur, max_speed=MAX_SPEED):
    """Build chunk list covering [0, total_dur]: gaps and speech segments."""
    chunks = []
    cursor = 0.0
    for idx, seg in enumerate(sorted(segs, key=lambda s: s["start"])):
        start, end = float(seg["start"]), min(float(seg["end"]), total_dur)
        if start - cursor > 0.05:
            chunks.append({"kind": "gap", "start": cursor, "end": start})
        orig = max(0.1, end - start)
        tts_dur = float(seg.get("tts_duration") or 0)
        if tts_dur <= 0:
            speed, vrate, new_dur = 1.0, 1.0, orig
        else:
            # 1) the speech speeds up first (never slows down)
            speed = min(max_speed, tts_dur / orig) if tts_dur > orig else 1.0
            need = tts_dur / speed           # seconds the dub occupies
            # 2) the PICTURE absorbs the rest by changing rate —
            #    slower for a long dub, faster for a short one.
            #    It never freezes (owner's rule).
            if need >= orig:
                vrate, new_dur = orig / need, need
            else:
                vrate = orig / need
                if vrate > MAX_VFAST:
                    vrate, new_dur = MAX_VFAST, orig / MAX_VFAST
                else:
                    new_dur = need
        chunks.append({"kind": "seg", "start": start, "end": end, "seg_idx": idx,
                       "speed": round(speed, 4), "vslow": round(vrate, 4),
                       "vrate": round(vrate, 4),
                       "excess": 0.0,
                       "new_dur": round(new_dur, 3), "orig_dur": round(orig, 3)})
        cursor = end
    if total_dur - cursor > 0.05:
        chunks.append({"kind": "gap", "start": cursor, "end": total_dur})
    for c in chunks:
        if c["kind"] == "gap":
            c["new_dur"] = round(c["end"] - c["start"], 3)
    return chunks


def time_map(plan):
    """[(src_t, dst_t)] breakpoints of a retime plan: chunk ends in
    source time against their cumulative retimed positions."""
    pts = [(0.0, 0.0)]
    dst = 0.0
    for c in plan["chunks"]:
        dst += float(c["new_dur"])
        pts.append((float(c["end"]), round(dst, 3)))
    return pts


def map_time(pts, t):
    if t <= 0:
        return 0.0
    for (s0, d0), (s1, d1) in zip(pts, pts[1:]):
        if t <= s1:
            if s1 - s0 <= 0:
                return d1
            return d0 + (d1 - d0) * (t - s0) / (s1 - s0)
    return pts[-1][1]


def remap_pieces(plan, pieces):
    """Move blur-piece windows from SOURCE time into RETIMED time.
    The pieces are detected against the original video, but dub/both
    jobs re-time the picture (segments stretch/shrink), so without
    this the blur fires at the wrong moments — blurring scenes with
    no captions and missing the caption when it is on screen (owner
    report 2026-11-09). Passthrough plans map identically."""
    if not pieces:
        return pieces
    pts = time_map(plan)
    out = []
    for p in pieces:
        t0 = map_time(pts, float(p["t0"]))
        t1 = map_time(pts, float(p["t1"]))
        if t1 - t0 < 0.2:
            t1 = t0 + 0.2
        out.append({**p, "t0": round(t0, 2), "t1": round(t1, 2)})
    return out


def _piece_vf(c, dur):
    # trim INSIDE the filter chain, then re-rate: setpts=PTS/<rate>
    # stretches (rate<1) or compresses (rate>1) the chunk to exactly
    # its planned new_dur. No tpad — the picture never freezes.
    vf = f"trim=duration={dur:.3f},setpts=PTS-STARTPTS"
    vr = c.get("vrate", 1.0) if c["kind"] == "seg" else 1.0
    if abs(vr - 1.0) > 0.001:
        vf += f",setpts=PTS/{vr:.4f}"
    vf += ",fps=30,format=yuv420p"
    return vf


def _video_single_pass(src, chunks, out, cap_k=2500):
    """All chunks in ONE ffmpeg decode+encode (a trim/concat filter
    graph). ONLY safe for a FEW chunks: with many trim branches on one
    input, ffmpeg buffers frames for the latest branch while earlier
    ones process, which balloons memory and time (a ~40-chunk graph
    stalled a production job in this step for 19+ minutes). The
    chunked path below is linear and is the default for real jobs."""
    filters, labels = [], []
    for n, c in enumerate(chunks):
        f = (f"[0:v]trim=start={c['start']:.3f}:end={c['end']:.3f},"
             "setpts=PTS-STARTPTS")
        vr = c.get("vrate", 1.0) if c["kind"] == "seg" else 1.0
        if abs(vr - 1.0) > 0.001:
            f += f",setpts=PTS/{vr:.4f}"
        f += ",fps=30,format=yuv420p"
        filters.append(f + f"[v{n}]")
        labels.append(f"[v{n}]")
    filters.append("".join(labels) + f"concat=n={len(chunks)}:v=1:a=0[outv]")
    mr = target_maxrate_k(src, cap_k)
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
         "-filter_complex", ";".join(filters), "-map", "[outv]",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
         "-maxrate", f"{mr}k", "-bufsize", f"{2 * mr}k", str(out)],
        timeout=1800)


def _video_chunked(src, chunks, cdir, out, cap_k=2500):
    """Original path: one encode per chunk + concat-copy. Kept as the
    fallback for very many chunks, or if the single pass ever fails."""
    vlist = []
    mr = target_maxrate_k(src, cap_k)
    for n, c in enumerate(chunks):
        dur = c["end"] - c["start"]
        vout = cdir / f"v{n:03d}.mp4"
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{c['start']:.3f}",
             "-i", str(src), "-vf", _piece_vf(c, dur), "-an",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
             "-maxrate", f"{mr}k", "-bufsize", f"{2 * mr}k",
             str(vout)], timeout=1200)
        vlist.append(vout)
    lst = cdir / "vlist_fb.txt"
    lst.write_text("".join(f"file '{f.resolve()}'\n" for f in vlist),
                   encoding="utf-8")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c:v", "copy", str(out)], timeout=1200)


def _atempo_chain(factor):
    """atempo only accepts 0.5..100 per instance — chain for less."""
    parts = []
    f = float(factor)
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.4f}")
    return ",".join(parts)


def _bed_chain(c, dur, stereo):
    """Filter chain turning the source window into the quiet bed:
    re-time it to the chunk's new duration exactly like the picture,
    suppress the centre (dialogue) on stereo, drop to bed level."""
    f = dur / max(0.05, c["new_dur"])      # audio tempo to fit window
    chain = _atempo_chain(f)
    if stereo:
        # Side signal (L-R) in BOTH channels with the SAME sign:
        # centre dialogue cancels in the subtraction, wide music
        # survives — and unlike anti-phase stereo (c1=-(c0-c1)) it
        # does not annihilate itself on mono fold-down (phone
        # speakers, amix layout folds).
        chain += (",pan=stereo|c0=c0-c1|c1=c0-c1"
                  f",volume={BED_VOL}")
    else:
        chain += f",volume={BED_VOL_MONO}"
    chain += f",apad,atrim=0:{c['new_dur']:.3f}"
    return chain


def retime(workdir, max_speed=MAX_SPEED, cap_k=2500):
    workdir = Path(workdir)
    src = workdir / "source.mp4"
    segs = load_segments(workdir / "segments_my.json")
    total = probe_duration(src)
    # the video stream can be slightly shorter than the container; plan on it
    from .common import probe_streams
    for s in probe_streams(src):
        if s.get("codec_type") == "video" and s.get("duration"):
            total = min(total, float(s["duration"]))
            break
    max_speed = max(1.0, min(4.0, float(max_speed or MAX_SPEED)))
    chunks = _plan(segs, total, max_speed)
    src_stereo = False
    src_has_audio = False
    for s in probe_streams(src):
        if s.get("codec_type") == "audio":
            src_has_audio = True
            src_stereo = int(s.get("channels") or 0) >= 2
            break

    cdir = workdir / "chunks"
    cdir.mkdir(exist_ok=True)
    alist = []
    new_segs = []
    t_cursor = 0.0

    for n, c in enumerate(chunks):
        dur = c["end"] - c["start"]
        aout = cdir / f"a{n:03d}.wav"
        if c["kind"] == "seg":
            seg = segs[c["seg_idx"]]
            tts_file = seg["tts_file"]
            af = f"atempo={c['speed']:.4f},apad,atrim=0:{c['new_dur']:.3f}"
            made = False
            if src_has_audio:
                # Narration on top of the quiet original bed (music
                # kept as background, original voices centre-cut).
                fc = (f"[0:a]{af}[n];"
                      f"[1:a]{_bed_chain(c, dur, src_stereo)}[b];"
                      "[n][b]amix=inputs=2:duration=first:"
                      "normalize=0[a]")
                try:
                    run(["ffmpeg", "-y", "-v", "error",
                         "-i", tts_file,
                         "-ss", f"{c['start']:.3f}", "-t", f"{dur:.3f}",
                         "-i", str(src),
                         "-filter_complex", fc, "-map", "[a]",
                         "-ac", "2", "-ar", "44100", str(aout)],
                        timeout=300)
                    made = True
                except Exception:
                    made = False   # fall through to narration-only
            if not made:
                run(["ffmpeg", "-y", "-v", "error", "-i", tts_file,
                     "-af", af, "-ac", "2", "-ar", "44100", str(aout)],
                    timeout=300)
            new_segs.append({**seg,
                             "start_new": round(t_cursor, 3),
                             "end_new": round(t_cursor + c["new_dur"], 3),
                             "speed": c["speed"], "video_slow": c.get("vrate", 1.0),
                             "video_rate": c.get("vrate", 1.0),
                             "freeze_extra": 0.0})
        else:
            # gap: original audio, lowered, padded/trimmed to chunk length
            run(["ffmpeg", "-y", "-v", "error", "-ss", f"{c['start']:.3f}", "-i", str(src),
                 "-t", f"{dur:.3f}", "-vn", "-af",
                 f"volume={GAP_VOL},aresample=44100,apad,atrim=0:{c['new_dur']:.3f}",
                 "-ac", "2", "-ar", "44100", str(aout)], timeout=300)
        alist.append(aout)
        t_cursor += c["new_dur"]

    def _concat(files, out, is_video):
        lst = workdir / ("vlist.txt" if is_video else "alist.txt")
        lst.write_text("".join(f"file '{f.resolve()}'\n" for f in files), encoding="utf-8")
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst)]
        cmd += ["-c:v", "copy"] if is_video else ["-c:a", "pcm_s16le"]
        cmd += [str(out)]
        run(cmd, timeout=1200)

    dub_wav = workdir / "dub.wav"
    _concat(alist, dub_wav, False)

    # Fast path: if NO segment changed the picture's timing at all
    # (every chunk keeps its original duration), the retimed timeline
    # IS the original timeline — the video passes through untouched
    # (stream copy in the mux below) and the most expensive encode
    # of the whole job simply does not happen.
    passthrough = all(c["kind"] == "gap"
                      or abs(c["new_dur"] - c["orig_dur"]) <= 0.01
                      for c in chunks)
    if passthrough:
        run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-i", str(dub_wav),
             "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "96k",
             "-shortest", str(workdir / "retimed.mp4")], timeout=1200)
    else:
        retimed_video = workdir / "retimed_video.mp4"
        # Single-pass only for a handful of chunks (see its docstring:
        # many-branch trim graphs stall); the chunked path is linear
        # in the chunk count and bounded in memory.
        if len(chunks) <= 12:
            try:
                _video_single_pass(src, chunks, retimed_video, cap_k)
            except Exception:
                _video_chunked(src, chunks, cdir, retimed_video, cap_k)
        else:
            _video_chunked(src, chunks, cdir, retimed_video, cap_k)
        run(["ffmpeg", "-y", "-v", "error", "-i", str(retimed_video), "-i", str(dub_wav),
             "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "96k", "-shortest",
             str(workdir / "retimed.mp4")], timeout=1200)
        # Intermediates are dead weight once the mux exists (the chunked
        # fallback's pieces are a second full copy of the video) — drop
        # them now instead of letting them sit until job cleanup.
        try:
            retimed_video.unlink(missing_ok=True)
            for piece in cdir.glob("v*.mp4"):
                piece.unlink(missing_ok=True)
        except Exception:
            pass

    save_segments(new_segs, workdir / "segments_retimed.json")
    write_srt(new_segs, workdir / "subs_retimed.srt", start_key="start_new", end_key="end_new")
    plan = {"chunks": chunks, "source_duration": round(total, 3),
            "retimed_duration": round(t_cursor, 3),
            "video_passthrough": passthrough, "max_speed": max_speed}
    (workdir / "retime_plan.json").write_text(
        __import__("json").dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan


if __name__ == "__main__":
    p = retime(Path(sys.argv[1]))
    print(f"source {p['source_duration']}s -> retimed {p['retimed_duration']}s, {len(p['chunks'])} chunks")
