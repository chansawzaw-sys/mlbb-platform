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

from .common import (load_segments, probe_duration, run, save_segments,
                     target_maxrate_k, write_srt)

MAX_SPEED = 1.30
MIN_VSLOW = 0.80   # legacy stat only; the plan no longer floors at it
MAX_VFAST = 1.6    # fastest the picture may play for a short dub


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


def _video_single_pass(src, chunks, out, cap_k=2500):
    """All chunks in ONE ffmpeg decode+encode (a trim/concat filter
    graph). ONLY safe for a FEW chunks: with many trim branches on one
    input, ffmpeg buffers frames for the latest branch while earlier
    ones process, which balloons memory and time (a ~40-chunk graph
    stalled a production job in this step for 19+ minutes). The
    chunked path below is linear and is the default for real jobs."""
    filters, _label = _video_graph(chunks)
    mr = target_maxrate_k(src, cap_k)
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
         "-filter_complex", ";".join(filters), "-map", "[outv]",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
         "-maxrate", f"{mr}k", "-bufsize", f"{2 * mr}k", str(out)],
        timeout=1800)


def _video_merged(src, chunks, dub_wav, spec, cap_k=2500):
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
            str(spec["output"])]
    run(cmd, timeout=1800)


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


def retime(workdir, max_speed=MAX_SPEED, cap_k=2500, make_finalize=None):
    """Re-time the picture to the dub and mux the narration.

    `make_finalize` (build 182): optional callable(plan) -> spec|None
    that supplies the finalize graph (fx/subtitle burn) so it can be
    appended to the single-pass retime graph and encoded ONCE into
    the final output. Consulted only when the video path is the
    single-pass one (few chunks, not passthrough); the plan then
    carries final_merged=True + final_output, and retimed.mp4 is not
    written (nothing outside run_job's classic finalize reads it).
    Spec-BUILDING errors propagate (the classic burn fails loudly the
    same way, e.g. a missing Myanmar font); a failed merged ENCODE
    falls back to the classic two-encode flow below."""
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
        dur = c["end"] - c["start"]
        aout = cdir / f"a{n:03d}.wav"
        if c["kind"] == "seg":
            seg = segs[c["seg_idx"]]
            tts_file = seg["tts_file"]
            # Narration ONLY (build 177): the TTS file alone, fitted
            # to the chunk window exactly as before (atempo first,
            # silence pad at the tail) — the source track is never
            # opened here any more.
            af = f"atempo={c['speed']:.4f},apad,atrim=0:{c['new_dur']:.3f}"
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
            # gap: SILENCE (build 177) — gaps used to carry the
            # original audio at GAP_VOL; now generated silence of
            # exactly the chunk length, same format as the narration
            # chunks so the concat stays seamless.
            run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                 "anullsrc=channel_layout=stereo:sample_rate=44100",
                 "-t", f"{c['new_dur']:.3f}",
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
    if passthrough:
        run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-i", str(dub_wav),
             "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "96k",
             "-shortest", str(workdir / "retimed.mp4")], timeout=1200)
    else:
        spec = None
        if make_finalize is not None and len(chunks) <= 12:
            # Built OUTSIDE the try: a spec error (missing font, bad
            # fx) must fail the job loudly, exactly as the classic
            # finalize would — only an encode failure falls back.
            spec = make_finalize(plan)
        if spec is not None:
            try:
                _video_merged(src, chunks, dub_wav, spec, cap_k)
                merged = True
            except Exception:
                merged = False
        if merged:
            plan["final_merged"] = True
            plan["final_output"] = str(spec["output"])
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

    (workdir / "retime_plan.json").write_text(
        __import__("json").dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan


if __name__ == "__main__":
    p = retime(Path(sys.argv[1]))
    print(f"source {p['source_duration']}s -> retimed {p['retimed_duration']}s, {len(p['chunks'])} chunks")
