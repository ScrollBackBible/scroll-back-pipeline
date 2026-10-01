#!/usr/bin/env python3
"""
Scroll Back assembly pipeline
=============================

Clip URLs in; one finished 16:9 episode plus the vertical Shorts out.

    python3 scrollback_assemble.py E2.json                 # everything
    python3 scrollback_assemble.py E2.json --analyze       # boundary report only, no render
    python3 scrollback_assemble.py E2.json --only E2F1     # just the episode
    python3 scrollback_assemble.py E2.json --draft         # fast, smaller review renders
    python3 scrollback_assemble.py E2.json --put E2F1=<presigned PUT url>   # upload a result when done

Needs Python 3.8+, ffmpeg and ffprobe. Optional: faster-whisper (word timings for
captions and far better speech detection in noisy scenes) and numpy (speed).
The Higgsfield sandbox has all of these preinstalled.

What it does, in order:
  1. Downloads each clip (or uses a local path) and probes fps, size, audio.
  2. Analyses each clip: speech (whisper word timings, or silencedetect as a
     fallback), per-frame motion, the model's own internal shot cuts, and the
     portal's white flash.
  3. Places every cut on a rest frame near the requested point, never inside a
     spoken word, never 1-2 frames either side of an internal shot change.
  4. Chooses a dissolve where both sides are quiet at the join, a hard cut
     where anyone is speaking across it, and always a hard cut out of the portal.
  5. Renders the master at the clips' native frame rate, then the 16:9 episode:
     widescreen footage goes out as true widescreen with no bars (Episode 2 on);
     vertical footage gets a blurred pillarbox (Episode 1 style). Then the title
     riding the portal flash, the end card, and a fade out.
  6. Renders each Short at 1080x1920. Widescreen footage is letterboxed by
     default: the whole wide frame at full width, with space above and below
     ("short_fill": "blur" = a darkened blur of the same shot, "black", or a hex
     colour). Text lives in those bands and never covers the picture: the hook
     (1-3 words, caps) or trailer beats in the top band, captions (only while
     someone speaks) and the 3-second end card in the bottom band, clear of the
     Shorts player's own UI. All text follows SHORT_TEXT, the bible's Shorts spec;
     "hold": "auto" freezes the last frame when the last line runs to the clip's end.
     "short_zoom" > 1 enlarges the picture and trims the sides (crop_x steers it);
     "short_frame": "crop" switches to a full-height 9:16 cut-out instead.
     Vertical footage (Episode 1 style) fills the screen as before.
  7. Two-pass loudness normalisation on every output (-14 LUFS, -2 dBTP).
  8. Writes a shot log (JSON + Markdown) and a contact sheet per output, so the
     result can be checked frame by frame before anything is uploaded.

Everything is driven by the manifest. See E2.json for a worked example.
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.request
from collections import Counter
from fractions import Fraction

try:
    import numpy as np
except Exception:  # numpy is optional
    np = None

# ----------------------------------------------------------------------------
# Defaults (any of these can be overridden in the manifest)
# ----------------------------------------------------------------------------

DEFAULTS = {
    "text_color": "#F6F0E6",          # channel cream
    "font": None,                      # auto: Montserrat ExtraBold -> Bold -> DejaVu Sans Bold
    "loudness": {"I": -14.0, "TP": -2.0, "LRA": 11.0},   # -2 dBTP so the AAC encode still lands under -1
    "dissolve": 0.35,                  # seconds
    "search_window": 0.5,              # seconds either side of a requested cut
    "speech_pad": 0.08,                # keep cuts this far from any word
    "flash_luma": 225,                 # mean luma (0-255) that counts as the white flash
    "flash_hold": 0.25,                # seconds of white kept before the hard cut out of a portal
    "whisper_model": "base.en",
    "silence_db": -30,                 # fallback speech detector threshold
    "episode_size": [1920, 1080],
    "short_size": [1080, 1920],
    "caption_fixes": {"sana": "Sayna", "saina": "Sayna", "sayna's": "Sayna's"},
}

FONT_CANDIDATES = [
    "Montserrat-ExtraBold.ttf", "Montserrat-ExtraBold.otf",
    "Montserrat-Bold.ttf", "Montserrat-Bold.otf",
    "DejaVuSans-Bold.ttf",
]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def run(cmd, capture=False):
    """Run a command; raise with its stderr on failure."""
    p = subprocess.run(cmd, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                       stderr=subprocess.PIPE)
    if p.returncode != 0:
        tail = p.stderr.decode("utf-8", "replace")[-3000:]
        raise RuntimeError(f"command failed: {' '.join(map(str, cmd[:6]))} ...\n{tail}")
    return p


def fnum(x, nd=3):
    return f"{x:.{nd}f}"


# ----------------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------------

def fetch(src, dest):
    """Download a URL (with retries) or copy a local file."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    if re.match(r"^https?://", src):
        last = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(src, headers={"User-Agent": "scrollback-assemble/1"})
                with urllib.request.urlopen(req, timeout=120) as r, open(dest + ".part", "wb") as f:
                    shutil.copyfileobj(r, f)
                os.replace(dest + ".part", dest)
                return dest
            except Exception as e:  # network hiccup: back off and retry
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"could not download {src}: {last}")
    if not os.path.exists(src):
        raise FileNotFoundError(src)
    shutil.copyfile(src, dest)
    return dest


def probe(path):
    p = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
            capture=True)
    info = json.loads(p.stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    fps = Fraction(v.get("r_frame_rate") or v.get("avg_frame_rate") or "24/1")
    return {
        "width": int(v["width"]), "height": int(v["height"]), "fps": fps,
        "duration": float(info["format"]["duration"]), "has_audio": has_audio,
    }


def describe_font(path):
    """(family name, weight class) read from the font file itself, not its filename."""
    try:
        from fontTools.ttLib import TTFont
        f = TTFont(path, lazy=True)
        fam = f["name"].getDebugName(16) or f["name"].getDebugName(1) or ""
        return fam, int(f["OS/2"].usWeightClass)
    except Exception:
        pass
    try:
        out = subprocess.run(["fc-scan", "--format", "%{family}|%{weight}", path], capture_output=True, text=True).stdout
        fam, w = (out.split("|") + [""])[:2]
        # fontconfig weight 205 = ExtraBold (800)
        return fam, 800 if w.strip() == "205" else None
    except Exception:
        return os.path.basename(path), None


def find_font(pref=None):
    if pref and os.path.exists(pref):
        return pref
    roots = ["/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts"),
             os.path.expanduser("~/.local/share/fonts"), "/Library/Fonts", "/System/Library/Fonts",
             os.path.expanduser("~/Library/Fonts"), "C:\\Windows\\Fonts"]
    found = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for d, _, files in os.walk(root):
            for f in files:
                found.setdefault(f.lower(), os.path.join(d, f))
    for name in FONT_CANDIDATES:
        if name.lower() in found:
            return found[name.lower()]
    try:  # last resort: ask fontconfig
        p = subprocess.run(["fc-match", "-f", "%{file}", "Montserrat:weight=200"], capture_output=True, text=True)
        if p.returncode == 0 and os.path.exists(p.stdout.strip()):
            return p.stdout.strip()
    except FileNotFoundError:
        pass
    raise RuntimeError("no usable font found; set \"font\" in the manifest to a .ttf/.otf path")


# ----------------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------------

def motion_profile(path, fps):
    """Per-frame mean luma and mean absolute frame difference, from a tiny grayscale decode."""
    w, h = 36, 64
    raw = run(["ffmpeg", "-v", "error", "-i", path, "-vf", f"fps={fps},scale={w}:{h}:flags=area,format=gray",
               "-f", "rawvideo", "-"], capture=True).stdout
    n = len(raw) // (w * h)
    if np is not None:
        a = np.frombuffer(raw[: n * w * h], dtype=np.uint8).reshape(n, w * h).astype(np.int16)
        luma = a.mean(axis=1).tolist()
        diff = [0.0] + np.abs(np.diff(a, axis=0)).mean(axis=1).tolist()
    else:
        frames = [raw[i * w * h:(i + 1) * w * h] for i in range(n)]
        luma = [sum(f) / len(f) for f in frames]
        diff = [0.0] + [sum(abs(x - y) for x, y in zip(frames[i], frames[i - 1])) / (w * h) for i in range(1, n)]
    return luma, diff


def internal_cuts(diff):
    """Frame indices where the generated clip itself changes shot."""
    s = sorted(diff[1:]) or [0.0]
    med = s[len(s) // 2]
    thresh = max(18.0, 5.0 * med)
    return [i for i, d in enumerate(diff) if i > 0 and d > thresh]


def find_flash(luma, fps, min_luma):
    """Start time of the portal flash: first run of near-white frames in the back half."""
    n = len(luma)
    for i in range(n // 2, n):
        if luma[i] >= min_luma and (i + 1 >= n or luma[i + 1] >= min_luma - 10):
            return i / float(fps)
    return None


_whisper_model = None


def whisper_words(path, model_name):
    """[(start, end, word)] from faster-whisper, or None if it is not installed."""
    global _whisper_model
    try:
        from faster_whisper import WhisperModel
    except Exception:
        return None
    if _whisper_model is None:
        log(f"  loading whisper model {model_name} ...")
        _whisper_model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segs, _ = _whisper_model.transcribe(path, word_timestamps=True, vad_filter=False, beam_size=5)
    out = []
    for s in segs:
        for w in (s.words or []):
            out.append((float(w.start), float(w.end), w.word.strip()))
    return out


def silence_speech(path, duration, db):
    """Fallback speech intervals: the complement of silence on voice-band audio."""
    p = subprocess.run(["ffmpeg", "-v", "info", "-i", path, "-vn", "-af",
                        f"highpass=f=180,lowpass=f=3800,silencedetect=noise={db}dB:d=0.18", "-f", "null", "-"],
                       capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: ([0-9.]+)", p.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", p.stderr)]
    silences = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else duration
        silences.append((s, e))
    speech, t = [], 0.0
    for s, e in silences:
        if s - t > 0.06:
            speech.append((t, s))
        t = e
    if duration - t > 0.06:
        speech.append((t, duration))
    return speech


def merge_words(words, gap=0.28):
    iv = []
    for s, e, _ in words:
        if iv and s - iv[-1][1] <= gap:
            iv[-1][1] = max(iv[-1][1], e)
        else:
            iv.append([s, e])
    return [tuple(x) for x in iv]


def analyze_clip(clip, work, cfg):
    cid = clip["id"]
    key = hashlib.sha1(clip["src"].encode()).hexdigest()[:10]
    path = os.path.join(work, "clips", f"{cid}_{key}.mp4")
    fetch(clip["src"], path)
    cache = path + ".analysis.json"
    if os.path.exists(cache):
        a = json.load(open(cache))
        a["fps"] = Fraction(a["fps"])
        return a
    log(f"analysing {cid}")
    info = probe(path)
    luma, diff = motion_profile(path, info["fps"])
    words = whisper_words(path, cfg["whisper_model"]) if info["has_audio"] else []
    if words is None:
        speech = silence_speech(path, info["duration"], cfg["silence_db"]) if info["has_audio"] else []
        speech_src = "silencedetect"
        words = []
    else:
        speech = merge_words(words)
        speech_src = "whisper"
    a = dict(info, path=path, luma=luma, diff=diff, cuts=internal_cuts(diff),
             flash=find_flash(luma, info["fps"], cfg["flash_luma"]),
             words=words, speech=speech, speech_src=speech_src)
    json.dump(dict(a, fps=str(a["fps"])), open(cache, "w"))
    return a


# ----------------------------------------------------------------------------
# Cut placement
# ----------------------------------------------------------------------------

def in_speech(t, speech, pad):
    return any(s - pad < t < e + pad for s, e in speech)


def speech_overlaps(a, b, speech):
    return any(s < b and e > a for s, e in speech)


def place_cut(t_req, lo, hi, clip, pad, kind):
    """Best frame boundary in [lo, hi]: at rest, outside speech, clear of internal shot changes."""
    fps = float(clip["fps"])
    diff = clip["diff"]
    n = len(diff)
    lo_i, hi_i = max(0, int(round(lo * fps))), min(n, int(round(hi * fps)))
    s = sorted(diff[1:]) or [1.0]
    med = max(s[len(s) // 2], 0.5)
    cuts = set(clip["cuts"])
    best, best_score = None, None
    window = max(hi - lo, 1e-6)
    for i in range(lo_i, hi_i + 1):
        t = i / fps
        if in_speech(t, clip["speech"], pad):
            continue
        near_cut = any(0 < abs(i - c) <= 2 for c in cuts)
        if near_cut:
            continue  # would leave a 1-2 frame flash of the neighbouring shot
        m = [diff[j] for j in (i - 1, i, i + 1) if 0 < j < n]
        motion = (sum(m) / len(m) if m else 0.0) / med
        score = motion + 0.8 * abs(t - t_req) / window
        if i in cuts:
            score -= 1.0  # the model's own shot change is the cleanest cut there is
        if best_score is None or score < best_score:
            best, best_score = t, score
    if best is None:
        return t_req, f"no clean frame near {fnum(t_req, 2)}s for the {kind}; cut lands mid-speech or mid-motion"
    return best, None


def plan_segment(clip, want_in, want_out, cfg, portal=False):
    """Resolve requested in/out (None = clip start/end) into frame-accurate, speech-safe points."""
    d = clip["duration"]
    fps = float(clip["fps"])
    W, pad = cfg["search_window"], cfg["speech_pad"]
    notes = []
    t_in = 0.0 if want_in is None else max(0.0, float(want_in))
    t_out = d if want_out is None else min(d, float(want_out))

    if portal:
        if clip["flash"] is None:
            notes.append("portal clip but no white flash detected; hard cut at the requested out point")
        else:
            t_out = min(d, clip["flash"] + cfg["flash_hold"])
    else:
        # never end inside a line: extend to the end of the word if the clip allows it
        for s, e in clip["speech"]:
            if s < t_out < e:
                if e + pad <= d:
                    notes.append(f"out point moved from {fnum(t_out, 2)} to {fnum(e + pad, 2)}s to finish a line")
                    t_out = e + pad
                else:
                    notes.append(f"a line is still running at the clip's end ({fnum(t_out, 2)}s); it will be clipped")
        # search back from the out point, but never so far that a whole line drops off the end
        lo = max(t_in, t_out - W)
        ended = [e for s, e in clip["speech"] if e <= t_out]
        if ended:
            lo = max(lo, min(max(ended) + pad, t_out))
        cand, why = place_cut(t_out, lo, t_out, clip, pad, "out point")
        if why:
            notes.append(why)
        t_out = cand

    if want_in is not None or t_in > 0:
        for s, e in clip["speech"]:
            if s < t_in < e:
                notes.append(f"in point moved from {fnum(t_in, 2)} to {fnum(max(0, s - pad), 2)}s so the line starts whole")
                t_in = max(0.0, s - pad)
        # search around the in point, but never past the start of the first line after it
        hi = min(t_out - 0.5, t_in + W)
        upcoming = [s for s, e in clip["speech"] if s >= t_in]
        if upcoming:
            hi = min(hi, max(min(upcoming) - pad, t_in))
        cand, why = place_cut(t_in, max(0.0, t_in - W), hi, clip, pad, "in point")
        if why:
            notes.append(why)
        t_in = cand

    # snap to frame boundaries
    t_in = round(t_in * fps) / fps
    t_out = round(t_out * fps) / fps
    if t_out - t_in < 0.5:
        raise ValueError(f"{clip['id']}: segment too short after placement ({t_in}-{t_out})")
    return {"clip": clip["id"], "in": t_in, "out": t_out, "portal": portal, "notes": notes}


def choose_transition(a_seg, b_seg, clips, D, fps):
    """dissolve only when nobody is speaking across the join and it is not a portal."""
    frame = 1.0 / fps
    if a_seg["portal"]:
        return frame, "hard cut (portal)"
    A, B = clips[a_seg["clip"]], clips[b_seg["clip"]]
    if speech_overlaps(a_seg["out"] - D, a_seg["out"], A["speech"]):
        return frame, "hard cut (speech at the end of the outgoing shot)"
    if speech_overlaps(b_seg["in"], b_seg["in"] + D, B["speech"]):
        return frame, "hard cut (speech at the start of the incoming shot)"
    if (a_seg["out"] - a_seg["in"]) < 2 * D or (b_seg["out"] - b_seg["in"]) < 2 * D:
        return frame, "hard cut (segment too short to dissolve)"
    return D, f"dissolve {D:.2f}s"


# ----------------------------------------------------------------------------
# Rendering helpers
# ----------------------------------------------------------------------------

def normalize_segment(seg, clip, out_path, size, fps, draft, crop_x=None):
    """Trim one segment to a clean intermediate: common size, native fps, 48 kHz stereo PCM.

    crop_x (0 = left edge, 0.5 = centre, 1 = right edge) cuts a 9:16 window out of
    widescreen footage for the Shorts; None keeps the whole frame."""
    w, h = size
    dur = seg["out"] - seg["in"]
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", fnum(seg["in"], 4), "-i", clip["path"]]
    if not clip["has_audio"]:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    crop = ""
    if crop_x is not None and clip["width"] > clip["height"]:
        x = min(1.0, max(0.0, float(crop_x)))
        cw = "trunc(ih*9/16/2)*2"
        crop = f"crop={cw}:ih:(iw-{cw})*{x}:0,"
    # exactly N video frames and exactly N/fps seconds of audio, so every later
    # offset can be computed from frame counts instead of container durations
    N = int(round(dur * float(fps)))
    cmd += ["-vf", f"fps={fps},{crop}scale={w}:{h}:flags=lanczos,setsar=1,"
                   f"tpad=stop_mode=clone:stop={int(float(fps)) * 2},format=yuv420p",
            "-frames:v", str(N),
            "-af", f"aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo,apad,atrim=0:{N / float(fps):.6f}",
            "-map", "0:v:0", "-map", "0:a:0" if clip["has_audio"] else "1:a:0",
            "-c:v", "libx264", "-preset", "ultrafast" if draft else "veryfast", "-crf", "14",
            "-g", "12", "-c:a", "pcm_s16le", out_path]
    run(cmd)
    return out_path


def chain(inputs, trans, fps, out_path, draft):
    """Join normalized segments with xfade/acrossfade (a 1-frame fade is a hard cut)."""
    def frames(p):
        r = run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v",
                 "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", p], capture=True)
        return int(r.stdout.decode().strip())
    counts = [frames(p) for p in inputs]
    lengths = [c / float(fps) for c in counts]
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for p in inputs:
        cmd += ["-i", p]
    if len(inputs) == 1:
        cmd += ["-c:v", "copy", "-c:a", "copy", out_path]
        run(cmd)
        return lengths[0], [0.0]
    # xfade stalls (freezing the picture for the rest of the episode) if the outgoing clip
    # runs out mid-transition, so chain on each clip's last whole frame. Found on ffmpeg 5.1.
    f = float(fps)
    lengths = [(c - 1) / f for c in counts]   # time of each clip's last frame, from the real frame count
    # trim every audio input to the same length, or sound drifts ahead of the picture
    # by a frame per join (about 0.2 s by the end of an episode)
    g = [f"[{k}:a]atrim=0:{fnum(lengths[k], 4)},asetpts=PTS-STARTPTS[t{k}]" for k in range(len(inputs))]
    starts = [0.0]
    acc = lengths[0]
    vprev, aprev = "0:v", "t0"
    for k in range(1, len(inputs)):
        d = trans[k - 1]
        off = acc - d
        starts.append(off)
        vo, ao = f"v{k}", f"a{k}"
        g.append(f"[{vprev}][{k}:v]xfade=transition=fade:duration={fnum(d, 4)}:offset={fnum(off, 4)}[{vo}]")
        g.append(f"[{aprev}][t{k}]acrossfade=d={fnum(d, 4)}:c1=tri:c2=tri[{ao}]")
        vprev, aprev = vo, ao
        acc = acc + lengths[k] - d
    script = out_path + ".filter.txt"
    open(script, "w").write(";\n".join(g))
    cmd += ["-filter_complex_script", script, "-map", f"[{vprev}]", "-map", f"[{aprev}]",
            "-r", str(fps), "-c:v", "libx264", "-preset", "ultrafast" if draft else "veryfast", "-crf", "14",
            "-c:a", "pcm_s16le", out_path]
    run(cmd)
    return acc, starts


def q(s):
    """Quote a value for a filtergraph script."""
    return "'" + str(s).replace("'", "") + "'"


def color(c):
    return "0x" + c.lstrip("#")


def alpha_expr(s, e, f):
    f = max(0.01, min(f, (e - s) / 2))
    return (f"if(lt(t,{fnum(s)}),0,if(lt(t,{fnum(s + f)}),(t-{fnum(s)})/{fnum(f)},"
            f"if(lt(t,{fnum(e - f)}),1,if(lt(t,{fnum(e)}),({fnum(e)}-t)/{fnum(f)},0))))")


class TextLayer:
    """Centered, possibly multi-line text drawn as one drawtext per line."""

    def __init__(self, work, font, fg):
        self.work, self.font, self.fg, self.n = work, font, fg, 0

    def lines(self, text, width_chars):
        out = []
        for para in str(text).split("\n"):
            out += textwrap.wrap(para, width=width_chars) or [""]
        return out

    def draw(self, text, size, center_y, start, end, W, fade=0.25, width_chars=24,
             style="shadow", color_=None, hold_to_end=False):
        fs = int(size)
        rows = self.lines(text, width_chars)
        lh = int(fs * 1.18)
        top = center_y - lh * len(rows) / 2.0
        parts = []
        for r, line in enumerate(rows):
            self.n += 1
            tf = os.path.join(self.work, f"text_{self.n:04d}.txt")
            open(tf, "w", encoding="utf-8").write(line)
            y = int(top + r * lh)
            opts = [f"fontfile={q(self.font)}", f"textfile={q(tf)}", "expansion=none",
                    f"fontsize={fs}", f"fontcolor={color(color_ or self.fg)}",
                    "x=(w-text_w)/2", f"y={y}",
                    f"alpha={q(alpha_expr(start, end + (60.0 if hold_to_end else 0.0), fade))}",
                    f"enable={q(f'between(t,{fnum(start)},{fnum(end)})')}"]
            sh = max(2, fs // 16)
            if style in ("shadow", "shadow+border"):
                sh = max(3, fs // 14)
                opts += ["shadowcolor=0x000000@0.7", f"shadowx={sh}", f"shadowy={sh}"]
            if style in ("border", "shadow+border"):
                opts += [f"borderw={max(3, fs // 14)}", "bordercolor=0x000000@0.8"]
            if style == "box":
                opts += ["box=1", "boxcolor=0x000000@0.5", f"boxborderw={max(8, fs // 3)}"]
            parts.append("drawtext=" + ":".join(opts))
        return parts


LOWER_THIRD = {   # the production bible's title-card spec, at 1080 lines; everything scales with frame height
    "kicker_size": 20, "kicker_tracking": 0.42, "gold": (232, 182, 92, 255),
    "rule_w": 64, "rule_h": 3, "rule_gap": 14,
    "title_size": 46, "title_tracking": 0.20, "cream": (246, 240, 230, 240), "line_gap": 6,
    "left": 0.06, "centre_y": 0.80,
    "shadow_alpha": 110, "shadow_blur": 10, "shadow_dy": 4,
    "fade_in": 0.5, "rise_px": 14, "rise_time": 0.6, "fade_out": 0.4,
}


def lower_third_png(kicker, lines, font, H, out_path, spec=LOWER_THIRD):
    """Gold kicker, gold rule, cream letter-spaced caps, soft shadow -> tight RGBA PNG.
    Returns (png path, x offset of the text's left edge inside the PNG, PNG height)."""
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    k = H / 1080.0
    pad = int(round(40 * k))
    rows = [(kicker, spec["kicker_size"], spec["kicker_tracking"], spec["gold"], 0)] + \
           [(ln, spec["title_size"], spec["title_tracking"], spec["cream"], spec["line_gap"]) for ln in lines]
    fonts = [ImageFont.truetype(font, max(1, int(round(sz * k)))) for _, sz, _, _, _ in rows]
    def width(i):
        t, sz, tr, _, _ = rows[i]
        return fonts[i].getlength(t) + sz * k * tr * max(0, len(t) - 1)
    widths = [width(i) for i in range(len(rows))]
    heights = [sum(f.getmetrics()) for f in fonts]
    rule_block = (spec["rule_h"] + 2 * spec["rule_gap"]) * k
    total = sum(heights) + sum(r[4] * k for r in rows[1:-1]) + rule_block
    Wd, Hd = int(max(widths) + 2 * pad), int(total + 2 * pad)
    fg = Image.new("RGBA", (Wd, Hd), (0, 0, 0, 0))
    d = ImageDraw.Draw(fg)
    y = pad
    for i, (t, sz, tr, col, gap) in enumerate(rows):
        f, track = fonts[i], sz * k * tr
        for j, ch in enumerate(t):
            d.text((pad + f.getlength(t[:j]) + j * track, y), ch, font=f, fill=col)
        y += heights[i] + (gap * k if 0 < i < len(rows) - 1 else 0)
        if i == 0:
            y += spec["rule_gap"] * k
            d.rectangle([pad, y, pad + spec["rule_w"] * k, y + spec["rule_h"] * k], fill=spec["gold"])
            y += (spec["rule_h"] + spec["rule_gap"]) * k
    sh = Image.new("RGBA", (Wd, Hd), (0, 0, 0, 0))
    sh.putalpha(fg.split()[3].point(lambda v: v * spec["shadow_alpha"] // 255))
    sh = sh.filter(ImageFilter.GaussianBlur(spec["shadow_blur"] * k))
    out = Image.new("RGBA", (Wd, Hd), (0, 0, 0, 0))
    out.alpha_composite(sh, (0, int(round(spec["shadow_dy"] * k))))
    out.alpha_composite(fg)
    out.save(out_path)
    return out_path, pad, Hd


def overlay_chain(base_label, cards, fps, W, H, spec=LOWER_THIRD):
    """Filter text that lays lower-third PNGs (inputs 1..n) over base_label with fade and rise."""
    g, cur = [], base_label
    for n, (png, pad, ph, t0, t1) in enumerate(cards, start=1):
        x = int(round(W * spec["left"])) - pad
        y = int(round(H * spec["centre_y"] - ph / 2))
        rise, rt = spec["rise_px"] * H / 1080.0, spec["rise_time"]
        g.append(f"[{n}:v]format=rgba,fade=t=in:st={fnum(t0)}:d={spec['fade_in']}:alpha=1,"
                 f"fade=t=out:st={fnum(t1 - spec['fade_out'])}:d={spec['fade_out']}:alpha=1[lt{n}]")
        g.append(f"[{cur}][lt{n}]overlay=x={x}:y='{y}+{rise:.2f}*pow(max(0\\,1-(t-{fnum(t0)})/{rt})\\,2)'"
                 f":eval=frame:shortest=1:enable='between(t,{fnum(t0)},{fnum(t1)})'[ov{n}]")
        cur = f"ov{n}"
    return g, cur


SHORT_TEXT = {   # the production bible's Shorts text spec, in px at 1080x1920; scales with frame height
    "gold": (232, 182, 92, 255), "cream": (246, 240, 230, 255), "ink": (0, 0, 0, 205),
    "shadow_alpha": 110, "shadow_blur": 16, "shadow_dy": 6, "pad": 40,
    "kicker_size": 44, "kicker_tracking": 0.42, "rule_w": 110, "rule_h": 5, "gap": 22,
    "title_size": 92, "title_tracking": 0.20,
    "pitch_size": 56, "pitch_maxw": 880, "line_size": 42,
    "hook_size": 80, "hook_tracking": 0.16, "hook_maxw": 940, "hook_secs": 3.0,
    "caption_size": 58, "caption_outline": 3, "caption_maxw": 940,
    "card_kicker_size": 32, "card_title_size": 80, "card_secs": 3.0,
    "end_title_size": 96, "end_title_gap": 26,
    "rise_px": 18, "card_rise_px": 14,
}


def text_w(f, t, tr):
    return f.getlength(t) + f.size * tr * max(0, len(t) - 1)


def wrap_px(f, text, maxw, tr=0.0):
    out = []
    for para in str(text).split("\n"):
        cur = ""
        for w in para.split():
            t = (cur + " " + w).strip()
            if cur and text_w(f, t, tr) > maxw:
                out.append(cur)
                cur = w
            else:
                cur = t
        out.append(cur)
    return out


def st_kicker(t, size=None, spec=SHORT_TEXT):
    return ("t", t.upper(), size or spec["kicker_size"], spec["kicker_tracking"], spec["gold"], spec["gap"])


def st_rule(spec=SHORT_TEXT):
    return ("r", spec["rule_w"], spec["rule_h"], spec["gold"], spec["gap"])


def st_title(t, size=None, gap=0, spec=SHORT_TEXT):
    return ("t", t.upper(), size or spec["title_size"], spec["title_tracking"], spec["cream"], gap)


def st_plain(lines, size, gap=8, spec=SHORT_TEXT):
    return [("t", ln, size, 0.0, spec["cream"], gap) for ln in lines]


def short_block_png(rows, font, W, k, path, outline=0, keep=None, spec=SHORT_TEXT):
    """A centred text block as a full-width RGBA PNG with the soft shadow; returns its height.
    rows: ("t", text, size, tracking, colour, gap_after) or ("r", w, h, colour, gap_after), sizes at 1920 lines.
    keep: indices of rows to draw (the rest only hold their space, so blocks can build in stages)."""
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    pad = int(round(spec["pad"] * k))
    y, items = pad, []
    for i, r in enumerate(rows):
        if r[0] == "r":
            items.append((i, "r", y, r[1] * k, r[2] * k, r[3]))
            y += (r[2] + r[4]) * k
        else:
            f = ImageFont.truetype(font, max(1, int(round(r[2] * k))))
            a, d = f.getmetrics()
            items.append((i, "t", y, r[1], f, r[3], r[4]))
            y += a + d + r[5] * k
    hc = int(y + pad)
    fg = Image.new("RGBA", (W, hc), (0, 0, 0, 0))
    dr = ImageDraw.Draw(fg)
    for it in items:
        if keep is not None and it[0] not in keep:
            continue
        if it[1] == "r":
            _, _, yy, w, h, col = it
            x0 = (W - w) / 2
            dr.rectangle([x0, yy, x0 + w - 1, yy + h - 1], fill=col)
        else:
            _, _, yy, text, f, tr, col = it
            x = (W - text_w(f, text, tr)) / 2
            kw = dict(font=f, fill=col)
            if outline:
                kw.update(stroke_width=max(1, int(round(outline * k))), stroke_fill=spec["ink"])
            if tr == 0:
                dr.text((x, yy), text, **kw)   # untracked lines keep the font's kerning
            else:
                for j, ch in enumerate(text):
                    dr.text((x + f.getlength(text[:j]) + j * f.size * tr, yy), ch, **kw)
    sh = Image.new("RGBA", (W, hc), (0, 0, 0, 0))
    sh.putalpha(fg.split()[3].point(lambda v: v * spec["shadow_alpha"] // 255))
    sh = sh.filter(ImageFilter.GaussianBlur(spec["shadow_blur"] * k))
    out = Image.new("RGBA", (W, hc), (0, 0, 0, 0))
    out.alpha_composite(sh, (0, int(round(spec["shadow_dy"] * k))))
    out.alpha_composite(fg)
    out.save(path)
    return hc


def loudnorm_finish(src, dst, loud, video_copy=True, fade_out=None):
    """Two-pass EBU R128 normalisation; video is copied, only audio is re-encoded."""
    I, TP, LRA = loud["I"], loud["TP"], loud["LRA"]
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", src, "-vn", "-af",
                        f"loudnorm=I={I}:TP={TP}:LRA={LRA}:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", p.stderr, re.S)
    if not m:
        raise RuntimeError("loudnorm measurement failed:\n" + p.stderr[-1500:])
    meas = json.loads(m.group(0))
    af = (f"loudnorm=I={I}:TP={TP}:LRA={LRA}:measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:"
          f"measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:"
          f"offset={meas['target_offset']}:linear=true:print_format=summary,aresample=48000,"
          f"aformat=sample_fmts=fltp:channel_layouts=stereo")
    if fade_out:
        af += f",afade=t=out:st={fnum(fade_out[0])}:d={fnum(fade_out[1])}"
    run(["ffmpeg", "-y", "-v", "error", "-i", src, "-c:v", "copy", "-af", af,
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", dst])
    return float(meas["input_i"])


def contact_sheet(video, png, cols=6, every=2.0, thumb_w=240):
    dur = probe(video)["duration"]
    n = max(1, int(dur / every))
    rows = max(1, (n + cols - 1) // cols)
    run(["ffmpeg", "-y", "-v", "error", "-i", video, "-vf",
         f"fps=1/{every},scale={thumb_w}:-2,tile={cols}x{rows}:padding=4:margin=4", "-frames:v", "1", png])


# ----------------------------------------------------------------------------
# Captions
# ----------------------------------------------------------------------------

def norm_tok(w):
    return re.sub(r"[^a-z0-9']", "", w.lower())


def timed_script_words(clip, fixes):
    """Script words carrying whisper timings (script spelling wins); whisper words if no script."""
    words = clip.get("words") or []
    script = " ".join(clip.get("lines") or [])
    if not words:
        return []
    # whisper often stretches a word back over the pause before it; keep at most 0.5s of that
    words = [(max(s, e - 0.5) if e - s > 0.7 else s, e, w) for s, e, w in words]
    if not script:
        return [(s, e, fixes.get(norm_tok(w), w)) for s, e, w in words]
    sw = []
    for t in script.replace(" - ", " — ").split():
        if sw and not norm_tok(t):
            sw[-1] += " " + t      # a lone dash rides on the word before it
        else:
            sw.append(t)
    a = [norm_tok(w) for _, _, w in words]
    b = [norm_tok(w) for w in sw]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    timing = [None] * len(sw)
    for i, j, n in sm.get_matching_blocks():
        for k in range(n):
            timing[j + k] = (words[i + k][0], words[i + k][1])
    known = [k for k, t in enumerate(timing) if t]
    if not known:
        return []
    out = []
    for k, w in enumerate(sw):
        if timing[k] is None:  # interpolate between the nearest timed neighbours
            prev = max((x for x in known if x < k), default=None)
            nxt = min((x for x in known if x > k), default=None)
            if prev is None or nxt is None:
                continue  # before the first / after the last heard word: not spoken in this clip
            t0, t1 = timing[prev][1], timing[nxt][0]
            span = (t1 - t0) / (nxt - prev)
            timing[k] = (t0 + span * (k - prev - 1), t0 + span * (k - prev))
        out.append((timing[k][0], timing[k][1], w))
    return out


def caption_chunks(words, fit, maxw, max_gap=0.8):
    """One line per caption: a sentence stays whole if it fits, otherwise it splits into the
    fewest balanced parts that fit, preferring a break after a comma or dash and never
    leaving a single word on its own."""
    from itertools import combinations
    groups, cur = [], []
    for s, e, w in words:
        if cur and (s - cur[-1][1] > max_gap or re.search(r"[.?!;]$", cur[-1][2])):
            groups.append(cur)
            cur = []
        cur.append((s, e, w))
    if cur:
        groups.append(cur)

    def txt(g):
        return " ".join(x[2] for x in g)

    def split(g):
        for n in range(1, len(g) + 1):
            best = None
            for cuts in combinations(range(1, len(g)), n - 1):
                parts = [g[a:b] for a, b in zip((0,) + cuts, cuts + (len(g),))]
                wd = [fit(txt(p)) for p in parts]
                if max(wd) > maxw:
                    continue
                sc = (max(wd) - 120 * sum(bool(re.search("[,—]$", g[c - 1][2])) for c in cuts)
                      + 400 * sum(len(p) == 1 for p in parts if n > 1))
                if best is None or sc < best[0]:
                    best = (sc, parts)
            if best:
                return best[1]
        return [[x] for x in g]

    return [(c[0][0], c[-1][1], txt(c)) for g in groups for c in split(g)]


# ----------------------------------------------------------------------------
# Outputs
# ----------------------------------------------------------------------------

def render_episode(man, cfg, clips, work, outdir, font, draft, report):
    ep = man["episode_cut"]
    name = man.get("episode_name", f"E{man['episode']}F1")
    ids = ep["clips"]
    segs = []
    for cid in ids:
        c = next(x for x in man["clips"] if x["id"] == cid)
        segs.append(plan_segment(clips[cid], c.get("in"), c.get("out"), cfg, portal=bool(c.get("portal"))))
    fps = master_fps(clips, ids)
    D = cfg["dissolve"]
    trans, labels = [], []
    for a, b in zip(segs, segs[1:]):
        d, why = choose_transition(a, b, clips, D, float(fps))
        trans.append(d)
        labels.append(why)
    size = master_size(clips, ids)
    report["episode"] = {"name": name, "fps": str(fps), "size": size, "segments": segs, "transitions": labels}
    if man.get("_analyze"):
        return
    log(f"rendering {name}")
    parts = [normalize_segment(s, clips[s["clip"]], os.path.join(work, f"{name}_seg{i}.mkv"), size, fps, draft)
             for i, s in enumerate(segs)]
    master = os.path.join(work, f"{name}_vertical.mkv")
    total, starts = chain(parts, trans, fps, master, draft)

    W, H = (1280, 720) if draft else tuple(cfg["episode_size"])
    tl = TextLayer(work, font, cfg["text_color"])
    cards = []   # (png, pad, height, t0, t1) in the lower-third style
    # title rides the portal flash
    portal_idx = next((i for i, s in enumerate(segs) if s["portal"]), None)
    title = man.get("title")
    if title and portal_idx is not None and clips[segs[portal_idx]["clip"]]["flash"] is not None:
        seg = segs[portal_idx]
        flash_t = starts[portal_idx] + (clips[seg["clip"]]["flash"] - seg["in"])
        t0 = max(0.0, flash_t - ep.get("title_lead", 3.0))
        t1 = flash_t + ep.get("title_hold_after", 0.8)
        kicker = ep.get("kicker", f"SCROLL BACK  \u00b7  EPISODE {man['episode']}")
        png = lower_third_png(kicker, [l.upper() for l in title.split("\n")], font, H,
                              os.path.join(work, f"{name}_title.png"))
        cards.append(png + (t0, t1))
        report["episode"]["title_window"] = [round(t0, 2), round(t1, 2)]
    elif title:
        report.setdefault("warnings", []).append("no portal flash found, so the title was not placed")
    # end card, same block
    card = ep.get("end_card") or {}
    if card.get("lines"):
        day = re.search(r"\b(mon|tues|wednes|thurs|fri|satur|sun)days?\b", " ".join(card["lines"]), re.I)
        if day:   # the bible: never name a weekday on screen, so the card stays true if the schedule moves
            msg = f"end card names a weekday ({day.group(0)}); use 'New episodes every week'"
            log("WARNING: " + msg)
            report.setdefault("warnings", []).append(msg)
        secs = card.get("seconds", 4.0)
        kick, *rest = card["lines"] if len(card["lines"]) > 1 else ["", card["lines"][0]]
        png = lower_third_png(kick.upper(), [r.upper() for r in rest], font, H, os.path.join(work, f"{name}_endcard.png"))
        cards.append(png + (max(0.0, total - secs), total - 0.05))
    fade = ep.get("fade_out", 0.6)
    if size[0] >= size[1]:
        # widescreen footage (Episode 2 on): true 16:9, no bars
        vf = [f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p[base]"]
    else:
        # vertical footage (Episode 1 style): blurred pillarbox
        vf = [
            f"[0:v]split=2[bg][fg]",
            f"[bg]scale=480:270:force_original_aspect_ratio=increase,crop=480:270,boxblur=10:2,"
            f"eq=brightness=-0.06:saturation=0.9,scale={W}:{H}:flags=bicubic[bgb]",
            f"[fg]scale=-2:{H}:flags=lanczos[fgs]",
            f"[bgb][fgs]overlay=(W-w)/2:0,format=yuv420p[base]",
        ]
    g, last = overlay_chain("base", cards, fps, W, H)
    vf += g + [f"[{last}]fade=t=out:st={fnum(total - fade)}:d={fnum(fade)},format=yuv420p[v]"]
    script = os.path.join(work, f"{name}_16x9.filter.txt")
    open(script, "w").write(";\n".join(vf))
    stage = os.path.join(work, f"{name}_stage.mkv")
    inputs = []
    for png, _, _, _, _ in cards:
        inputs += ["-loop", "1", "-framerate", str(fps), "-t", fnum(total + 1), "-i", png]
    run(["ffmpeg", "-y", "-v", "error", "-i", master] + inputs + ["-filter_complex_script", script,
         "-map", "[v]", "-map", "0:a", "-r", str(fps), "-shortest", "-c:v", "libx264",
         "-preset", "veryfast" if draft else "medium", "-crf", "20" if draft else "18",
         "-profile:v", "high", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", stage])
    out = os.path.join(outdir, f"{name}.mp4")
    measured = loudnorm_finish(stage, out, cfg["loudness"], fade_out=(total - fade, fade))
    contact_sheet(out, os.path.join(outdir, f"{name}_contact.png"), every=3.0)
    report["episode"].update(output=out, duration=round(total, 2), loudness_in=measured)


def render_short(sh, man, cfg, clips, work, outdir, font, draft, report):
    name = sh["name"]
    segs = []
    for s in sh["segments"]:
        c = next(x for x in man["clips"] if x["id"] == s["clip"])
        portal = bool(s.get("portal", c.get("portal") and s.get("out") is None))
        segs.append(plan_segment(clips[s["clip"]], s.get("in"), s.get("out"), cfg, portal=portal))
    # trailers are capped from the front so they still end on the flash
    if sh.get("max_len") and len(segs) == 1:
        seg = segs[0]
        if seg["out"] - seg["in"] > sh["max_len"] + 0.5:
            want_in = seg["out"] - sh["max_len"]
            segs[0] = plan_segment(clips[seg["clip"]], want_in, None, cfg, portal=seg["portal"])
    fps = master_fps(clips, [s["clip"] for s in segs])
    frame = 1.0 / float(fps)
    size = master_size(clips, [s["clip"] for s in segs])
    src_w, src_h = size
    wide = src_w > src_h
    # How widescreen footage becomes a vertical Short:
    #   "letterbox" (default) - the whole wide frame, full width, space above and below
    #   "crop"                - a 9:16 window cut out of the frame (zooms in, loses the sides)
    mode = sh.get("frame", man.get("short_frame", "letterbox")) if wide else "vertical"
    zoom = max(1.0, float(sh.get("zoom", man.get("short_zoom", 1.0)))) if mode == "letterbox" else 1.0
    fill = sh.get("fill", man.get("short_fill", "blur"))
    if mode == "crop":
        size = [int(src_h * 9 / 16) // 2 * 2, src_h]
    crops = [sh["segments"][i].get("crop_x", sh.get("crop_x", 0.5)) if mode == "crop" else None
             for i in range(len(segs))]
    Wt = cfg["short_size"][0]
    desc = {"vertical": f"{src_w}x{src_h} vertical source",
            "crop": f"{size[0]}x{size[1]} (9:16 crop of widescreen), scaled x{Wt / size[0]:.2f}",
            "letterbox": f"{src_w}x{src_h} full frame, letterboxed, scaled x{Wt * zoom / src_w:.2f}"
                         + (f" (zoom {zoom:g})" if zoom > 1 else "")}[mode]
    rep = {"name": name, "kind": sh.get("kind", "content"), "segments": segs, "frame": desc}
    report.setdefault("shorts", []).append(rep)
    if man.get("_analyze"):
        return
    log(f"rendering {name}")
    parts = [normalize_segment(s, clips[s["clip"]], os.path.join(work, f"{name}_seg{i}.mkv"), size, fps, draft,
                               crop_x=crops[i])
             for i, s in enumerate(segs)]
    base = os.path.join(work, f"{name}_joined.mkv")
    total, starts = chain(parts, [frame] * (len(parts) - 1), fps, base, draft)

    W, H = (720, 1280) if draft else tuple(cfg["short_size"])
    # Layout: where the picture sits and where each text zone is centred.
    if mode == "letterbox":
        cx = min(1.0, max(0.0, float(sh.get("crop_x", 0.5))))
        sw = int(W * zoom) // 2 * 2
        pic_h = min(H, int(round(sw * src_h / src_w / 2)) * 2)
        band = (H - pic_h) / 2.0
        # text lives in the bands, never over the picture: words on screen in the top band,
        # captions and the end card in the upper part of the bottom band (clear of the
        # Shorts player's own title and buttons, which cover roughly the bottom fifth)
        zones = {"top": band / 2.0, "middle": H / 2.0, "bottom": H - band + min(band * 0.25, H * 0.09)}
        hook_y, card_y, cap_y = zones["top"], zones["bottom"], zones["bottom"]
    else:
        zones = {"top": H * 0.17, "middle": H * 0.47, "bottom": H * 0.78}
        hook_y, card_y, cap_y = H * 0.16, H * 0.47, H * 0.74
    # Text: the production bible's Shorts text spec (SHORT_TEXT). Every layer is a PNG drawn in
    # Montserrat ExtraBold with the soft shadow; trailer beats and end cards use the title-card
    # block (gold kicker, gold rule, cream letter-spaced caps); captions add a thin dark outline.
    from PIL import ImageFont
    T = SHORT_TEXT
    k = H / 1920.0
    layers = []   # (png, top y, start, end, fade, rise px)

    def add(rows, cy, s, e, fade=0.25, rise=0.0, outline=0, keep=None):
        p = os.path.join(work, f"{name}_t{len(layers) + 1:02d}.png")
        hc = short_block_png(rows, font, W, k, p, outline, keep)
        layers.append((p, cy - hc / 2.0, s, e, fade, rise * k))

    # caption words first: they decide whether the last frame has to hold for the end card
    cap_words = []
    if sh.get("kind") != "trailer" and sh.get("captions", True):
        for i, s in enumerate(segs):
            ws = [(a, b, w) for a, b, w in timed_script_words(clips[s["clip"]], cfg["caption_fixes"])
                  if a >= s["in"] - 0.02 and b <= s["out"] + 0.05]
            shift = starts[i] - s["in"]
            cap_words += [(a + shift, b + shift, w) for a, b, w in ws]
        if not any(clips[s["clip"]].get("words") for s in segs):
            report.setdefault("warnings", []).append(f"{name}: no word timings (install faster-whisper); captions skipped")
    card = None if sh.get("kind") == "trailer" else sh.get("end_card", ["Full episode on the channel", "Scroll Back"])
    card_secs = T["card_secs"] if card else 0.0
    hold = sh.get("hold", 0.0)
    if hold == "auto":
        # freeze the last frame just long enough for the end card to come after the last word
        hold = max(0.0, (cap_words[-1][1] if cap_words else 0.0) + 0.17 + card_secs - total) if card else 0.0
    hold = round(float(hold or 0.0), 3)
    total += hold
    rep["hold"] = hold

    if sh.get("kind") == "trailer":
        tt = dict({"kicker": "A new series", "title": "Scroll Back",
                   "pitch": "Two students. One Bible.\nEvery question you were afraid to ask.",
                   "end_kicker": f"Episode {man['episode']}", "end_title": "Tomorrow",
                   "end_line": "Subscribe for new episodes"}, **sh.get("trailer_text", {}))
        blk = [st_kicker(tt["kicker"]), st_rule(), st_title(tt["title"])]
        add(blk, hook_y, 0.0, 4.6, keep={0})                              # A NEW SERIES
        add(blk, hook_y, 1.8, 4.6, rise=T["rise_px"], keep={1, 2})        # ... into SCROLL BACK
        fp = ImageFont.truetype(font, int(round(T["pitch_size"] * k)))
        add(st_plain(wrap_px(fp, tt["pitch"], T["pitch_maxw"] * k), T["pitch_size"]), hook_y, 5.0, 9.5)
        fl = ImageFont.truetype(font, int(round(T["line_size"] * k)))
        end = ([st_kicker(tt["end_kicker"]), st_rule(), st_title(tt["end_title"], T["end_title_size"], T["end_title_gap"])]
               + st_plain(wrap_px(fl, tt["end_line"], T["pitch_maxw"] * k), T["line_size"]))
        add(end, hook_y, max(0.0, total - 4.0), total + 1.0, rise=T["rise_px"])   # holds through the flash
    else:
        if sh.get("hook"):   # 1-3 words, all caps, letter-spaced like the title card
            hook = sh["hook"].upper()
            if len(hook.split()) > 3:
                report.setdefault("warnings", []).append(f"{name}: hook is {len(hook.split())} words; the spec is 1-3")
            fh = ImageFont.truetype(font, int(round(T["hook_size"] * k)))
            add([("t", ln, T["hook_size"], T["hook_tracking"], T["cream"], 10)
                 for ln in wrap_px(fh, hook, T["hook_maxw"] * k, T["hook_tracking"])],
                hook_y, 0.0, sh.get("hook_until", T["hook_secs"]), rise=T["card_rise_px"])
        card_s = total - card_secs
        if card:
            kick, *rest = card if len(card) > 1 else ["", card[0]]
            add([st_kicker(kick, T["card_kicker_size"]), st_rule()] + [st_title(r, T["card_title_size"]) for r in rest],
                card_y, card_s, total - 0.05, fade=0.3, rise=T["card_rise_px"])
        fc = ImageFont.truetype(font, int(round(T["caption_size"] * k)))
        chunks = caption_chunks(cap_words, fc.getlength, T["caption_maxw"] * k)
        caps = []
        for j, (a, b, text) in enumerate(chunks):
            b2 = min(b + 0.12, chunks[j + 1][0] - 0.03 if j + 1 < len(chunks) else 1e9, card_s - 0.05)
            if b2 - a < 0.2:
                continue
            add(st_plain(wrap_px(fc, text, T["caption_maxw"] * k), T["caption_size"], 4), cap_y,
                max(0.0, a), b2, fade=0.05, outline=T["caption_outline"])
            caps.append([round(max(0.0, a), 2), round(b2, 2), text])
        rep["captions"] = caps

    ins = ["-i", base]
    for p, *_ in layers:
        ins += ["-loop", "1", "-framerate", str(fps), "-t", fnum(total + 1), "-i", p]
    g = [f"[0:v]setpts=PTS-STARTPTS" + (f",tpad=stop_mode=clone:stop_duration={hold:.3f}" if hold else "") + "[src]",
         "[0:a]asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
         + (f",apad=pad_dur={hold:.3f}" if hold else "") + "[aout]"]
    if mode == "letterbox":
        fgf = f"scale={sw}:{pic_h}:flags=lanczos"
        if sw > W:  # zoomed past full width: trim the sides, following crop_x
            fgf += f",crop={W}:{pic_h}:(iw-{W})*{cx}:0"
        if fill == "blur":
            g += ["[src]split=2[bg][fg]",
                  f"[bg]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=12:2,"
                  f"eq=brightness=-0.16:saturation=0.85,scale={W}:{H}:flags=bicubic[bgb]",
                  f"[fg]{fgf},setsar=1[fgs]",
                  "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[c0]"]
        else:
            col = "black" if fill == "black" else color(fill)
            g.append(f"[src]{fgf},pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={col},setsar=1,format=yuv420p[c0]")
    else:
        g.append(f"[src]scale={W}:{H}:flags=lanczos,setsar=1,format=yuv420p[c0]")
    for i, (p, y0, s, e, fade, rise) in enumerate(layers, start=1):
        fd = max(0.01, min(fade, (e - s) / 2))
        g.append(f"[{i}:v]format=rgba,fade=t=in:st={fnum(s)}:d={fnum(fd)}:alpha=1,"
                 f"fade=t=out:st={fnum(e - fd)}:d={fnum(fd)}:alpha=1[l{i}]")
        yexpr = (f"'{y0:.1f}+{rise:.1f}*pow(max(0\\,1-(t-{fnum(s)})/0.6)\\,2)':eval=frame" if rise else f"{y0:.1f}")
        g.append(f"[c{i - 1}][l{i}]overlay=x=0:y={yexpr}:shortest=1:enable='between(t,{fnum(s)},{fnum(e)})'[c{i}]")
    script = os.path.join(work, f"{name}.filter.txt")
    open(script, "w").write(";\n".join(g))
    stage = os.path.join(work, f"{name}_stage.mkv")
    run(["ffmpeg", "-y", "-v", "error"] + ins + ["-filter_complex_script", script,
         "-map", f"[c{len(layers)}]", "-map", "[aout]", "-r", str(fps), "-c:v", "libx264",
         "-preset", "veryfast" if draft else "medium", "-crf", "20" if draft else "18",
         "-profile:v", "high", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", stage])
    out = os.path.join(outdir, f"{name}.mp4")
    measured = loudnorm_finish(stage, out, cfg["loudness"])
    contact_sheet(out, os.path.join(outdir, f"{name}_contact.png"), cols=6, every=1.5, thumb_w=180)
    rep.update(output=out, duration=round(total, 2), loudness_in=measured)


def master_fps(clips, ids):
    c = Counter(clips[i]["fps"] for i in ids)
    fps, _ = c.most_common(1)[0]
    return fps


def master_size(clips, ids):
    # if resolutions are mixed (480p drafts beside 720p finals), work at the largest
    w, h = max(((clips[i]["width"], clips[i]["height"]) for i in ids), key=lambda x: x[0] * x[1])
    return [w - w % 2, h - h % 2]


def write_report(report, clips, outdir, name):
    js = os.path.join(outdir, f"{name}_shotlog.json")
    json.dump(report, open(js, "w"), indent=2, default=str)
    md = [f"# {name} shot log", "",
          f"Font: {report.get('font')} ({report.get('font_family')}, weight {report.get('font_weight')})", ""]
    md += ["## Clips", "", "| Clip | Length | fps | Size | Speech source | Internal cuts | Flash |", "|---|---|---|---|---|---|---|"]
    for cid, c in clips.items():
        f = float(c["fps"])
        cuts = ", ".join("%.2fs" % (i / f) for i in c["cuts"]) or "-"
        flash = ("%.2fs" % c["flash"]) if c["flash"] is not None else "-"
        md.append(f"| {cid} | {c['duration']:.2f}s | {c['fps']} | {c['width']}x{c['height']} | "
                  f"{c['speech_src']} | {cuts} | {flash} |")
    ep = report.get("episode")
    if ep:
        md += ["", f"## {ep['name']} ({ep.get('duration', '?')}s, {ep['fps']} fps)", "",
               "| # | Clip | In | Out | Into next | Notes |", "|---|---|---|---|---|---|"]
        for i, s in enumerate(ep["segments"]):
            nxt = ep["transitions"][i] if i < len(ep["transitions"]) else "end"
            md.append(f"| {i + 1} | {s['clip']} | {s['in']:.3f} | {s['out']:.3f} | {nxt} | {'; '.join(s['notes']) or '-'} |")
    for sh in report.get("shorts", []):
        md += ["", f"## {sh['name']} ({sh['kind']}, {sh.get('duration', '?')}s)", "",
               "| Clip | In | Out | Notes |", "|---|---|---|---|"]
        for s in sh["segments"]:
            md.append(f"| {s['clip']} | {s['in']:.3f} | {s['out']:.3f} | {'; '.join(s['notes']) or '-'} |")
    if report.get("warnings"):
        md += ["", "## Warnings", ""] + [f"- {w}" for w in report["warnings"]]
    open(os.path.join(outdir, f"{name}_shotlog.md"), "w").write("\n".join(md) + "\n")
    return js


def put_file(path, url):
    size = os.path.getsize(path)
    ctype = "video/mp4" if path.endswith(".mp4") else ("image/png" if path.endswith(".png") else "application/octet-stream")
    with open(path, "rb") as f:
        req = urllib.request.Request(url, data=f, method="PUT",
                                     headers={"Content-Type": ctype, "Content-Length": str(size), "If-None-Match": "*"})
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Scroll Back: clips in, episode + Shorts out.")
    ap.add_argument("manifest")
    ap.add_argument("--out", default="out")
    ap.add_argument("--work", default="work")
    ap.add_argument("--only", default="", help="comma list of outputs, e.g. E2F1,E2S1")
    ap.add_argument("--analyze", action="store_true", help="report cut placement only, render nothing")
    ap.add_argument("--draft", action="store_true", help="smaller, faster review renders")
    ap.add_argument("--put", action="append", default=[], help="NAME=URL: PUT an output when done")
    args = ap.parse_args()

    man = json.load(open(args.manifest))
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in man.items() if k in DEFAULTS})
    man["_analyze"] = args.analyze
    os.makedirs(os.path.join(args.work, "clips"), exist_ok=True)
    os.makedirs(args.out, exist_ok=True)
    pref = man.get("font")
    if pref and not os.path.isabs(pref):
        # a relative "font" path is resolved against the manifest's own folder
        pref = os.path.join(os.path.dirname(os.path.abspath(args.manifest)), pref)
    font = find_font(pref)
    family, weight = describe_font(font)
    log(f"font: {font} ({family}, weight {weight})")
    if ("montserrat" not in family.lower() or weight not in (800, None)) and not man.get("allow_fallback_font"):
        raise SystemExit(
            f"font resolved to {font}, not Montserrat ExtraBold. The channel uses one typeface everywhere.\n"
            "Install it (https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-ExtraBold.ttf)\n"
            "and set \"font\" in the manifest to its path, or set \"allow_fallback_font\": true for a throwaway draft.")

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    ep_name = man.get("episode_name", f"E{man['episode']}F1")
    wanted_shorts = [s for s in man.get("shorts", []) if not only or s["name"] in only]
    need = set()
    if man.get("episode_cut") and (not only or ep_name in only):
        need |= set(man["episode_cut"]["clips"])
    for s in wanted_shorts:
        need |= {g["clip"] for g in s["segments"]}

    clips = {}
    for c in man["clips"]:
        if c["id"] not in need:
            continue
        if not c.get("url") or c["url"].startswith("TODO"):
            raise SystemExit(f"clip {c['id']} has no url yet")
        c["src"] = c["url"]
        a = analyze_clip(c, args.work, cfg)
        a["id"] = c["id"]
        a["lines"] = c.get("lines", [])
        clips[c["id"]] = a

    report = {"manifest": os.path.abspath(args.manifest), "draft": args.draft, "font": font,
              "font_family": family, "font_weight": weight}
    if man.get("episode_cut") and (not only or ep_name in only):
        render_episode(man, cfg, clips, args.work, args.out, font, args.draft, report)
    for s in wanted_shorts:
        render_short(s, man, cfg, clips, args.work, args.out, font, args.draft, report)

    js = write_report(report, clips, args.out, f"E{man['episode']}")
    print(open(os.path.join(args.out, f"E{man['episode']}_shotlog.md")).read())

    for spec in args.put:
        n, url = spec.split("=", 1)
        path = os.path.join(args.out, n if "." in n else n + ".mp4")
        status = put_file(path, url)
        log(f"PUT {n}: HTTP {status}")


if __name__ == "__main__":
    main()
