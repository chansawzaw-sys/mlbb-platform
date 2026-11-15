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
  * AUDIO IS NARRATION ONLY (build 177, owner order 2026-11-14):
    the original track NEVER enters a dub. Builds <=176 mixed a
    centre-cut source "bed" under the narration and played the
    original at low volume in the gaps — the owner heard the two
    voices layered ("အသံတွေ ထပ်နေတယ်") and ordered the source audio
    deleted outright ("မူရင်း အသံတွေ ဖျောက်ပစ်လိုက်"). Segment chunks
    render the TTS file alone; gap chunks render pure silence.
    (Subtitle-only jobs are a different path — burn.py straight on
    source.mp4 — and keep the original audio untouched.)

Implementation: split the source into chunks (gaps + segments), render each
chunk's video (rate-adjusted) and audio (atempo'd narration for speech
segments, silence for gaps), concat both, and emit re-timed subtitle
timings.

CLI: python -m pipeline.retime <workdir>
Reads source.mp4 + segments_my.json (with tts_file/tts_duration).
Writes retimed.mp4, dub.wav, segments_retimed.json, subs_retimed.srt
"""
import sys
from pathlib import Path

from .common import (encode_threads, load_segments, probe_duration, run,
                     save_segments, target_maxrate_k, write_srt)

MAX_SPEED = 1.30
MIN_VSLOW = 0.80   # legacy stat only; the plan no longer floors at it
MAX_VFAST = 1.6    # fastest the picture may play for a short dub

# Single-pass graph thresholds (build 183 canary). Up to
# MERGE_MAX_CHUNKS chunks the single-pass trim/concat graph is the
# long-standing default (build 182 merged encode included). Between
# there and CANARY_MAX_CHUNKS the same graph runs as a CANARY: a
# bounded timeout (see _canary_timeout) cuts a graph stall short
# and the classic chunked path — linear, bounded-memory — takes
# over, so the worst case costs the timeout, never the open-ended
# 19+ minute stall a ~40-chunk graph once caused in production.
MERGE_MAX_CHUNKS = 12
CANARY_MAX_CHUNKS = 40

# Build 186 grouped merging (audit fix #3): past the canary
# ceiling the video stage encodes in groups of this many chunks
# (the canary's proven graph size) instead of one encode per
# chunk — see _video_grouped.
GROUP_MAX_CHUNKS = CANARY_MAX_CHUNKS


def _canary_timeout(total_dur):
    """Wall-clock budget (seconds) for a canary single-pass encode:
    4x the source duration, floored at 240 s (a short clip on a
    busy 4-job server still gets room) and capped at 1200 s (a
    genuine stall is rescued in minutes, not tens of minutes).
    The classic <=MERGE_MAX_CHUNKS paths keep their historical
    1800 s budget untouched."""
    return int(max(240, min(1200, 4 * float(total_dur or 0))))


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


def _video_graph(chunks):
    """The single-pass video graph: one trim/setpts branch per chunk,
    concatenated. Returns (filters, out_label). Shared by
    _video_single_pass and by the build-182 merged encode, which
    appends the finalize graph (fx + subtitle burn) to the concat
    label so the retimed picture is encoded ONCE, not twice."""
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
    return filters, "[outv]"


def _video_single_pass(src, chunks, out, cap_k=2500, timeout=1800):
    """All chunks in ONE ffmpeg decode+encode (a trim/concat filter
    graph). ONLY safe for a FEW chunks: with many trim branches on one
    input, ffmpeg buffers frames for the latest branch while earlier
    ones process, which balloons memory and time (a ~40-chunk graph
    stalled a production job in this step for 19+ minutes). The
    chunked path below is linear and is the default for real jobs.
    Build 183: 13..CANARY_MAX_CHUNKS chunks may attempt this graph
    as a canary when the caller passes a bounded `timeout` — a
    subprocess timeout raises here and the caller falls back to
    the chunked path, exactly like any other encode failure."""
    filters, _label = _video_graph(chunks)
    mr = target_maxrate_k(src, cap_k)
    # Build 193 capped every libx264 encode in the pipeline at
    # "-threads 2" (here, _video_merged, _video_chunked, burn, fx
    # and the app.py shrink/small-copy) — with up to four jobs on
    # a 4-core box, an uncapped encode per job starved the web
    # server sharing the process. Build 198: the cap is adaptive
    # (common.encode_threads) — 4 threads when this is the only
    # active job, still exactly 2 whenever 2+ jobs are active.
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
         "-filter_complex", ";".join(filters), "-map", "[outv]",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
         "-maxrate", f"{mr}k", "-bufsize", f"{2 * mr}k",
         "-threads", str(encode_threads()), str(out)],
        timeout=timeout)


def _video_merged(src, chunks, dub_wav, spec, cap_k=2500, timeout=1800):
    """Build 182 merged encode: the retime graph and the finalize
    graph (fx chain and/or subtitle burn, built by the caller in
    `spec`) run in ONE ffmpeg invocation, and the dub audio is muxed
    in the same pass — the picture is encoded exactly once at the
    finalize stage's own settings (crf 24; the classic flow encoded
    it at crf 23, decoded it, and encoded it again at crf 24).
    `spec` = {"filters", "out_label", "logo_path", "output"} with the
    finalize filters rooted at the concat label [outv]; the logo, if
    any, is input 2 (0 = source, 1 = dub.wav)."""
    filters, _label = _video_graph(chunks)
    filters = filters + list(spec["filters"])
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src),
           "-i", str(dub_wav)]
    if spec.get("logo_path"):
        cmd += ["-i", str(spec["logo_path"])]
    mr = target_maxrate_k(src, cap_k)
    cmd += ["-filter_complex", ";".join(filters),
            "-map", spec["out_label"], "-map", "1:a",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
            "-maxrate", f"{mr}k", "-bufsize", f"{2 * mr}k",
            "-c:a", "aac", "-b:a", "96k", "-shortest",
            "-threads", str(encode_threads()), str(spec["output"])]
    run(cmd, timeout=timeout)


def _reroot_finalize(filters, root="[0:v]"):
    """Re-root a finalize graph (built by run_job's make_finalize
    for the retime concat label "[outv]") at a different input
    label. Exactly one filter in the chain CONSUMES the start
    label — the blur/pieces split, the logo overlay, or the bare
    ass filter — and it carries the label as its prefix; rewrite
    that one prefix. Returns None when no filter consumes the
    label (a spec shape this merge does not understand — the
    caller falls back to the classic flow rather than guessing)."""
    out = []
    done = False
    for f in filters:
        if not done and f.startswith("[outv]"):
            out.append(root + f[len("[outv]"):])
            done = True
        else:
            out.append(f)
    return out if done else None


def _video_passthrough_merged(src, dub_wav, spec, cap_k=2500,
                              timeout=1200):
    """Build 198: passthrough plans (the retime is a timeline
    identity — every chunk kept its original duration) used to
    stream-copy the picture into an intermediate retimed.mp4 and
    let run_job's classic finalize decode + encode THAT. The
    finalize graph is timestamp-global, so on an identity plan it
    can hang straight off the source stream instead: re-rooted at
    [0:v], it runs as the job's ONE encode with the dub muxed in
    the same pass, at the finalize stage's own settings (crf 24 —
    the classic burn's settings; the picture the classic flow
    encoded was a bit-exact copy of this same source). Raises on
    a spec shape it cannot re-root; encode failures propagate to
    the caller's classic fallback, exactly like _video_merged."""
    filters = _reroot_finalize(spec["filters"])
    if filters is None:
        raise RuntimeError("finalize spec cannot be re-rooted")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src),
           "-i", str(dub_wav)]
    if spec.get("logo_path"):
        cmd += ["-i", str(spec["logo_path"])]
    mr = target_maxrate_k(src, cap_k)
    cmd += ["-filter_complex", ";".join(filters),
            "-map", spec["out_label"], "-map", "1:a",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
            "-maxrate", f"{mr}k", "-bufsize", f"{2 * mr}k",
            "-c:a", "aac", "-b:a", "96k", "-shortest",
            "-threads", str(encode_threads()), str(spec["output"])]
    run(cmd, timeout=timeout)


def _video_grouped(src, chunks, cdir, out, cap_k=2500):
    """Build 186 (audit fix #3): grouped single-pass for jobs past
    the canary ceiling. The chunk list is encoded in groups of at
    most GROUP_MAX_CHUNKS chunks — each group is the ordinary
    single-pass trim/concat graph at crf 23, exactly the chunked
    path's settings — and the group files are concat-copied into
    `out`. This replaces N per-chunk encodes with
    ceil(N/GROUP_MAX_CHUNKS) graph encodes: the classic path's
    per-chunk process + encode overhead was ~45% of the phase on
    >40-chunk jobs (audit: 29 chunks classic retime 26.2 s vs
    ~17-21 s merged-family). No group ever exceeds the build-183
    canary's proven graph size, so there is no new stall surface —
    and the canary's own remedy applies anyway: each group runs
    under _canary_timeout, and any group failure raises so the
    caller falls back to the classic chunked path for the whole
    stage. The build-182 finalize merge is NOT attempted here
    (the subtitle burn is timestamp-global and cannot be split
    per group); the output feeds run_job's classic finalize
    exactly like the chunked path's did. Returns the group count."""
    groups = [chunks[i:i + GROUP_MAX_CHUNKS]
              for i in range(0, len(chunks), GROUP_MAX_CHUNKS)]
    gfiles = []
    for gi, group in enumerate(groups):
        gout = cdir / f"g{gi:03d}.mp4"
        _video_single_pass(
            src, group, gout, cap_k,
            timeout=_canary_timeout(
                sum(c["end"] - c["start"] for c in group)))
        gfiles.append(gout)
    lst = cdir / "glist.txt"
    lst.write_text("".join(f"file '{f.resolve()}'\n" for f in gfiles),
                   encoding="utf-8")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c:v", "copy", str(out)], timeout=1200)
    for f in gfiles:
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass
    return len(groups)


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
             "-threads", str(encode_threads()), str(vout)], timeout=1200)
        vlist.append(vout)
    lst = cdir / "vlist_fb.txt"
    lst.write_text("".join(f"file '{f.resolve()}'\n" for f in vlist),
                   encoding="utf-8")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c:v", "copy", str(out)], timeout=1200)


def _render_chunk_audio_loop(chunks, segs, cdir):
    """The original per-chunk rendering: one ffmpeg process per
    chunk. Narration ONLY (build 177): a segment chunk is its TTS
    file alone, fitted to the chunk window (atempo first, silence
    pad at the tail) — the source track is never opened here; a
    gap chunk is generated silence of exactly the chunk length,
    same format as the narration chunks so the concat stays
    seamless. Kept unchanged as the fallback for the one-shot
    renderer below."""
    for n, c in enumerate(chunks):
        aout = cdir / f"a{n:03d}.wav"
        if c["kind"] == "seg":
            seg = segs[c["seg_idx"]]
            af = f"atempo={c['speed']:.4f},apad,atrim=0:{c['new_dur']:.3f}"
            run(["ffmpeg", "-y", "-v", "error", "-i", seg["tts_file"],
                 "-af", af, "-ac", "2", "-ar", "44100", str(aout)],
                timeout=300)
        else:
            run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                 "anullsrc=channel_layout=stereo:sample_rate=44100",
                 "-t", f"{c['new_dur']:.3f}",
                 "-ac", "2", "-ar", "44100", str(aout)], timeout=300)


def _render_chunk_audio_oneshot(chunks, segs, cdir):
    """Build 186 (audit fix #6): render ALL chunk WAVs in ONE
    ffmpeg invocation — one input per chunk, one filter branch
    per chunk, one output per chunk file, with the same filters
    and the same -ac 2 / -ar 44100 output options as the loop.
    Proven sample-identical to the loop before shipping
    (bench186b item6 experiment: 31/31 chunks, PCM bytes and
    format params equal), so the dub.wav concat downstream is
    bit-for-bit the same render with N-1 process spawns and
    per-call decode setups removed."""
    cmd = ["ffmpeg", "-y", "-v", "error"]
    branches = []
    for n, c in enumerate(chunks):
        if c["kind"] == "seg":
            seg = segs[c["seg_idx"]]
            cmd += ["-i", str(seg["tts_file"])]
            branches.append(
                f"[{n}:a]atempo={c['speed']:.4f},apad,"
                f"atrim=0:{c['new_dur']:.3f}[a{n}]")
        else:
            cmd += ["-f", "lavfi", "-t", f"{c['new_dur']:.3f}", "-i",
                    "anullsrc=channel_layout=stereo:sample_rate=44100"]
            branches.append(f"[{n}:a]anull[a{n}]")
    cmd += ["-filter_complex", ";".join(branches)]
    for n in range(len(chunks)):
        cmd += ["-map", f"[a{n}]", "-ac", "2", "-ar", "44100",
                str(cdir / f"a{n:03d}.wav")]
    run(cmd, timeout=1200)


def _render_chunk_audio(chunks, segs, cdir):
    try:
        _render_chunk_audio_oneshot(chunks, segs, cdir)
    except Exception:
        _render_chunk_audio_loop(chunks, segs, cdir)


def retime(workdir, max_speed=MAX_SPEED, cap_k=2500, make_finalize=None):
    """Re-time the picture to the dub and mux the narration.

    `make_finalize` (build 182): optional callable(plan) -> spec|None
    that supplies the finalize graph (fx/subtitle burn) so it can be
    appended to the single-pass retime graph and encoded ONCE into
    the final output. Consulted only when the video path is the
    single-pass one (not passthrough); the plan then
    carries final_merged=True + final_output, and retimed.mp4 is not
    written (nothing outside run_job's classic finalize reads it).
    Spec-BUILDING errors propagate (the classic burn fails loudly the
    same way, e.g. a missing Myanmar font); a failed merged ENCODE
    falls back to the classic two-encode flow below.

    Build 183 canary: the single-pass family (merged encode, or the
    plain single-pass graph for dub-without-fx) is attempted up to
    CANARY_MAX_CHUNKS chunks; beyond MERGE_MAX_CHUNKS the attempt
    runs under _canary_timeout and any failure lands on the classic
    chunked path. Which path produced the video is recorded in
    plan["video_path"] (passthrough / merged / merged_canary /
    single_pass / single_pass_canary / grouped / chunked),
    persisted to retime_plan.json with the rest of the plan.
    Build 186: past the canary ceiling the video stage uses
    grouped single-pass (_video_grouped) before the chunked
    fallback."""
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

    cdir = workdir / "chunks"
    cdir.mkdir(exist_ok=True)
    alist = []
    new_segs = []
    t_cursor = 0.0

    for n, c in enumerate(chunks):
        aout = cdir / f"a{n:03d}.wav"
        if c["kind"] == "seg":
            seg = segs[c["seg_idx"]]
            new_segs.append({**seg,
                             "start_new": round(t_cursor, 3),
                             "end_new": round(t_cursor + c["new_dur"], 3),
                             "speed": c["speed"], "video_slow": c.get("vrate", 1.0),
                             "video_rate": c.get("vrate", 1.0),
                             "freeze_extra": 0.0})
        alist.append(aout)
        t_cursor += c["new_dur"]
    # Render every chunk's WAV (build 186 one-shot, audit fix #6;
    # falls back to the per-chunk loop on any failure).
    _render_chunk_audio(chunks, segs, cdir)

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
    # The re-timed subtitle timings depend only on the plan, so they
    # are written BEFORE the video stage now (build 182): the merged
    # encode's finalize graph burns subs_retimed.srt in the same
    # pass that produces the retimed picture.
    save_segments(new_segs, workdir / "segments_retimed.json")
    write_srt(new_segs, workdir / "subs_retimed.srt", start_key="start_new", end_key="end_new")
    plan = {"chunks": chunks, "source_duration": round(total, 3),
            "retimed_duration": round(t_cursor, 3),
            "video_passthrough": passthrough, "max_speed": max_speed}

    merged = False
    canary_failed = False
    n_chunks = len(chunks)
    if passthrough:
        # Build 198: when a finalize graph exists (mode=both, or
        # dub with fx), run it as ONE encode straight off the
        # source — the plan is a timeline identity, so the graph's
        # [outv] root is the source stream itself — instead of a
        # stream-copy into an intermediate retimed.mp4 that the
        # classic finalize then decoded again. The spec is built
        # OUTSIDE the try (a spec error fails loudly, exactly as
        # in the merged path below); only an encode failure falls
        # back to the classic copy-mux + finalize. Dub without fx
        # has no finalize graph: the copy-mux below already IS
        # the finished file's cheapest form (zero encodes).
        spec = make_finalize(plan) if make_finalize is not None \
            else None
        if spec is not None:
            try:
                _video_passthrough_merged(src, dub_wav, spec, cap_k)
                merged = True
                plan["final_merged"] = True
                plan["final_output"] = str(spec["output"])
                plan["video_path"] = "passthrough_merged"
            except Exception:
                merged = False
                try:
                    Path(spec["output"]).unlink(missing_ok=True)
                except Exception:
                    pass
        if not merged:
            plan["video_path"] = "passthrough"
            run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-i", str(dub_wav),
                 "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "96k",
                 "-shortest", str(workdir / "retimed.mp4")], timeout=1200)
    else:
        spec = None
        if make_finalize is not None and n_chunks <= CANARY_MAX_CHUNKS:
            # Built OUTSIDE the try: a spec error (missing font, bad
            # fx) must fail the job loudly, exactly as the classic
            # finalize would — only an encode failure falls back.
            # Build 183: the spec is now also built in the canary
            # range (13..CANARY_MAX_CHUNKS chunks); the merged
            # encode there runs under _canary_timeout instead of
            # the classic 1800 s budget.
            spec = make_finalize(plan)
        if spec is not None:
            canary = n_chunks > MERGE_MAX_CHUNKS
            budget = _canary_timeout(total) if canary else 1800
            if canary:
                plan["merge_timeout_s"] = budget
            try:
                _video_merged(src, chunks, dub_wav, spec, cap_k,
                              timeout=budget)
                merged = True
                plan["video_path"] = "merged_canary" if canary \
                    else "merged"
            except Exception:
                merged = False
                canary_failed = canary
                # A timed-out/failed merged encode may leave a
                # partial file at the final path — remove it so it
                # can never pose as the finished video; the classic
                # flow below rewrites that path from scratch anyway.
                try:
                    Path(spec["output"]).unlink(missing_ok=True)
                except Exception:
                    pass
        if merged:
            plan["final_merged"] = True
            plan["final_output"] = str(spec["output"])
        else:
            retimed_video = workdir / "retimed_video.mp4"
            # Single-pass only for a handful of chunks (see its docstring:
            # many-branch trim graphs stall); the chunked path is linear
            # in the chunk count and bounded in memory. Build 183
            # canary: in the 13..CANARY_MAX_CHUNKS range the graph
            # gets ONE bounded attempt when no merged encode was
            # tried (dub without fx) — a canary MERGED failure goes
            # straight to chunked, since the same graph just failed.
            if n_chunks <= MERGE_MAX_CHUNKS:
                try:
                    _video_single_pass(src, chunks, retimed_video, cap_k)
                    plan["video_path"] = "single_pass"
                except Exception:
                    _video_chunked(src, chunks, cdir, retimed_video, cap_k)
                    plan["video_path"] = "chunked"
            elif spec is None and not canary_failed \
                    and n_chunks <= CANARY_MAX_CHUNKS:
                budget = _canary_timeout(total)
                plan["merge_timeout_s"] = budget
                try:
                    _video_single_pass(src, chunks, retimed_video, cap_k,
                                       timeout=budget)
                    plan["video_path"] = "single_pass_canary"
                except Exception:
                    _video_chunked(src, chunks, cdir, retimed_video, cap_k)
                    plan["video_path"] = "chunked"
            elif n_chunks > CANARY_MAX_CHUNKS:
                # Build 186 grouped single-pass (audit fix #3):
                # past the canary ceiling the stage encodes in
                # canary-sized groups instead of one encode per
                # chunk; any failure lands on the classic chunked
                # path for the whole stage, unchanged.
                try:
                    plan["merge_groups"] = _video_grouped(
                        src, chunks, cdir, retimed_video, cap_k)
                    plan["video_path"] = "grouped"
                except Exception:
                    _video_chunked(src, chunks, cdir, retimed_video, cap_k)
                    plan["video_path"] = "chunked"
            else:
                _video_chunked(src, chunks, cdir, retimed_video, cap_k)
                plan["video_path"] = "chunked"
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

    (workdir / "retime_plan.json").write_text(
        __import__("json").dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan


if __name__ == "__main__":
    p = retime(Path(sys.argv[1]))
    print(f"source {p['source_duration']}s -> retimed {p['retimed_duration']}s, {len(p['chunks'])} chunks")
