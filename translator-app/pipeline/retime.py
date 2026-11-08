"""Step 5: TIMING SYNC — fit the Myanmar dub into each segment's time window.

Rules (user's core requirement):
  * dub shorter than window  -> speed 1.0, pad with silence at the end.
  * dub longer than window   -> speed speech up with ffmpeg atempo,
                                clamped to [1.0, 1.30].
  * still too long at 1.30x  -> FREEZE the video: extend that segment by
                                cloning its last frame for the excess time,
                                so the speech is never cut off.
  * non-speech gaps keep the original audio at low volume (ambience).

Implementation: split the source into chunks (gaps + segments), render each
chunk's video (with tpad freeze when needed) and audio (atempo-adjusted dub,
or quiet original for gaps), concat both, and emit re-timed subtitle timings.

CLI: python -m pipeline.retime <workdir>
Reads source.mp4 + segments_my.json (with tts_file/tts_duration).
Writes retimed.mp4, dub.wav, segments_retimed.json, subs_retimed.srt
"""
import sys
from pathlib import Path

from .common import (load_segments, probe_duration, run, save_segments,
                     write_srt)

MAX_SPEED = 1.30
GAP_VOL = 0.25


def _plan(segs, total_dur):
    """Build chunk list covering [0, total_dur]: gaps and speech segments."""
    chunks = []
    cursor = 0.0
    for idx, seg in enumerate(sorted(segs, key=lambda s: s["start"])):
        start, end = float(seg["start"]), min(float(seg["end"]), total_dur)
        if start - cursor > 0.05:
            chunks.append({"kind": "gap", "start": cursor, "end": start})
        orig = max(0.1, end - start)
        tts_dur = float(seg.get("tts_duration") or 0)
        if tts_dur <= orig or tts_dur == 0:
            speed, excess = 1.0, 0.0
        else:
            speed = min(MAX_SPEED, tts_dur / orig)
            excess = max(0.0, tts_dur / speed - orig)
        chunks.append({"kind": "seg", "start": start, "end": end, "seg_idx": idx,
                       "speed": round(speed, 4), "excess": round(excess, 3),
                       "new_dur": round(orig + excess, 3), "orig_dur": round(orig, 3)})
        cursor = end
    if total_dur - cursor > 0.05:
        chunks.append({"kind": "gap", "start": cursor, "end": total_dur})
    for c in chunks:
        if c["kind"] == "gap":
            c["new_dur"] = round(c["end"] - c["start"], 3)
    return chunks


def retime(workdir):
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
    chunks = _plan(segs, total)

    cdir = workdir / "chunks"
    cdir.mkdir(exist_ok=True)
    vlist, alist = [], []
    new_segs = []
    t_cursor = 0.0

    for n, c in enumerate(chunks):
        dur = c["end"] - c["start"]
        vout = cdir / f"v{n:03d}.mp4"
        # NOTE: trim INSIDE the filter chain, not output -t: an output -t would
        # also cut the tpad freeze frames we are trying to add.
        vf = f"trim=duration={dur:.3f},setpts=PTS-STARTPTS,fps=30,format=yuv420p"
        if c["kind"] == "seg" and c["excess"] > 0.01:
            vf += f",tpad=stop_mode=clone:stop_duration={c['excess']:.3f}"
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{c['start']:.3f}", "-i", str(src),
             "-vf", vf, "-an",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", str(vout)])
        vlist.append(vout)

        aout = cdir / f"a{n:03d}.wav"
        if c["kind"] == "seg":
            seg = segs[c["seg_idx"]]
            tts_file = seg["tts_file"]
            af = f"atempo={c['speed']:.4f},apad,atrim=0:{c['new_dur']:.3f}"
            run(["ffmpeg", "-y", "-v", "error", "-i", tts_file, "-af", af,
                 "-ac", "2", "-ar", "44100", str(aout)])
            new_segs.append({**seg,
                             "start_new": round(t_cursor, 3),
                             "end_new": round(t_cursor + c["new_dur"], 3),
                             "speed": c["speed"], "freeze_extra": c["excess"]})
        else:
            # gap: original audio, lowered, padded/trimmed to chunk length
            run(["ffmpeg", "-y", "-v", "error", "-ss", f"{c['start']:.3f}", "-i", str(src),
                 "-t", f"{dur:.3f}", "-vn", "-af",
                 f"volume={GAP_VOL},aresample=44100,apad,atrim=0:{c['new_dur']:.3f}",
                 "-ac", "2", "-ar", "44100", str(aout)])
        alist.append(aout)
        t_cursor += c["new_dur"]

    def _concat(files, out, is_video):
        lst = workdir / ("vlist.txt" if is_video else "alist.txt")
        lst.write_text("".join(f"file '{f.resolve()}'\n" for f in files), encoding="utf-8")
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst)]
        cmd += ["-c:v", "copy"] if is_video else ["-c:a", "pcm_s16le"]
        cmd += [str(out)]
        run(cmd)

    retimed_video = workdir / "retimed_video.mp4"
    dub_wav = workdir / "dub.wav"
    _concat(vlist, retimed_video, True)
    _concat(alist, dub_wav, False)

    run(["ffmpeg", "-y", "-v", "error", "-i", str(retimed_video), "-i", str(dub_wav),
         "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-shortest",
         str(workdir / "retimed.mp4")])

    save_segments(new_segs, workdir / "segments_retimed.json")
    write_srt(new_segs, workdir / "subs_retimed.srt", start_key="start_new", end_key="end_new")
    plan = {"chunks": chunks, "source_duration": round(total, 3),
            "retimed_duration": round(t_cursor, 3)}
    (workdir / "retime_plan.json").write_text(
        __import__("json").dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan


if __name__ == "__main__":
    p = retime(Path(sys.argv[1]))
    print(f"source {p['source_duration']}s -> retimed {p['retimed_duration']}s, {len(p['chunks'])} chunks")
