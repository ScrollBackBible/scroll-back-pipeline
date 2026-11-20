#!/usr/bin/env python3
"""
Scroll Back: Scripture on screen, word by word (the verse text style, Brian, Nov 20 2026)
========================================================================================

Each verse word pops in as it is spoken, in Outfit (Medium; the reference in SemiBold
letter-spaced caps), at most two lines on screen (a third line slides the top one up and
out). 16:9: the lower third of the calmer side of the frame. Vertical: centred in the top
band. Cream #FAF7F0 with gold #F0C46E for the key words, or deep brown #362616 with dark
gold #965C12 when the text area is bright (average above 135/255 while the line is up).
No shadow, outline, box or scrim. The full spec is the project's verse_text_style.md.

Run it on the finished files that scrollback_assemble.py made (its default
"verse_style": "kinetic" leaves the old cards out of the picture):

    python3 scrollback_verses.py episode  E14.json --shotlog out/E14_shotlog.json  --in out/E14F1_cta.mp4 --out fin/E14F1_cta.mp4
    python3 scrollback_verses.py vertical E14.json --shotlog outv/E14_shotlog.json --name E14F1V --in outv/E14F1V.mp4 --out fin/E14F1V.mp4
    python3 scrollback_verses.py short    E14.json --shotlog outs/E14_shotlog.json --name E14S2 --in outs/E14S2.mp4 --out fin/E14S2.mp4 \\
            --also outs/E14S2_social.mp4=fin/E14S2_social.mp4 --also outs/E14S2_tiktok.mp4=fin/E14S2_tiktok.mp4
    python3 scrollback_verses.py erase    OLD_SHORT.mp4 CLEAN.mp4 4.45,9.2     # take an old verse card out of a finished Short
    python3 scrollback_verses.py words    VIDEO.mp4 words.json                 # just the word timings

Steps: word timings of the finished file (faster-whisper medium.en), matched to each
manifest "scripture" entry inside the windows where its clip plays (from the shot log),
then one PNG sequence per verse, then one ffmpeg pass that lays them over the video
(audio copied). Run the Shorts' social and TikTok cuts with --also: same timeline, so
one plan serves all three.

A manifest scripture entry: {"clip", "ref", "text", "gold": [phrases], optional
"anchor": true + "match": "<words spoken where the verse should start>" (a verse nobody
reads aloud types on at about 0.11 s a word and holds up to 1.6 s), "hold"/"khold"
(seconds after the last word; default 1.0), "side" ("L"/"R", 16:9 only), "ytop",
"qopen"/"qclose" (false for a verse split across two shots)}.

The vertical and Shorts avoid the hook (first 3.2 s) and the cold open; the 16:9 avoids
the cold open, the title card and puts a verse on the left while the Subscribe pop-up
(bottom right, 35-39.2 s) is up. Every default can be changed on the command line.
Needs ffmpeg, Pillow, numpy and faster-whisper (all in the Higgsfield sandbox).
"""
import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

FPS = 24
WHITE, GOLD = (250, 247, 240), (240, 196, 110)
DARKW, DARKG = (54, 38, 22), (150, 92, 18)
BRIGHT = 135          # average 0-255 above which a line goes brown
OUTFIT_URL = "https://github.com/google/fonts/raw/main/ofl/outfit/Outfit%5Bwght%5D.ttf"
GEOM = {
    "169": dict(W=1920, H=1080, size=50, ref=19, lh=62, boxw=720, margin=110, ytop=742),
    "v": dict(W=1080, H=1920, size=50, ref=22, lh=64, boxw=960, margin=60, ytop=330),
}
SHORT_YTOP, VERTICAL_YTOP = 251, 273      # block top in the top band (Nov 20 renders)
HOOK_SECS = 3.2                           # the hook's window at the start of every vertical
CTA_169 = (35.0, 39.2)                    # the 16:9 Subscribe pop-up, bottom right


# ---------------------------------------------------------------- helpers
def log(*a):
    print(*a, flush=True)


def duration(p):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p],
                         capture_output=True, text=True).stdout
    return float(out.strip())


def norm(w):
    return re.sub(r"[^a-z0-9']", "", w.lower().replace("’", "'")).strip("'")


def outfit_path():
    """The variable Outfit font (SIL OFL), fetched once and cached next to this script or in ~/.cache."""
    env = os.environ.get("OUTFIT_TTF")
    if env and os.path.exists(env):
        return env
    for d in (os.path.dirname(os.path.abspath(__file__)), os.path.expanduser("~/.cache/scrollback")):
        p = os.path.join(d, "Outfit[wght].ttf")
        if os.path.exists(p):
            return p
    d = os.path.expanduser("~/.cache/scrollback")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "Outfit[wght].ttf")
    subprocess.run(["curl", "-sSL", "-o", p, OUTFIT_URL], check=True)
    return p


def font(size, weight):
    from PIL import ImageFont
    f = ImageFont.truetype(outfit_path(), size)
    f.set_variation_by_name(weight)
    return f


# ---------------------------------------------------------------- 1. word timings
def words(video, out_json):
    """faster-whisper medium.en word timings of a finished file: [[start, end, word], ...]."""
    from faster_whisper import WhisperModel
    m = WhisperModel("medium.en", device="cpu", compute_type="int8")
    segs, _ = m.transcribe(video, word_timestamps=True, beam_size=5, vad_filter=True)
    w = [[x.start, x.end, x.word.strip()] for s in segs for x in s.words]
    json.dump(w, open(out_json, "w"))
    log("words", video, len(w))
    return w


# ---------------------------------------------------------------- 2. where each clip plays
def windows(shotlog, key):
    """[{clip, t0, t1}] for each segment of an output, on the output's own timeline, padded 1 s."""
    d = json.load(open(shotlog))
    segs = None
    for k in (key, key + "_tiktok", key + "_social"):
        if k == "episode" and d.get("episode"):
            segs = d["episode"].get("segments")
        for v in d.get("shorts", []):
            if v.get("name") == k:
                segs = v.get("segments")
        if segs:
            break
    if not segs:
        raise SystemExit(f"no segments for {key} in {shotlog}")
    t, out = 0.0, []
    for s in segs:
        L = s["out"] - s["in"]
        out.append({"clip": s["clip"], "t0": round(max(0, t - 1.0), 3), "t1": round(t + L + 1.0, 3)})
        t += L
    return out


def title_window(shotlog, key):
    d = json.load(open(shotlog))
    if key == "episode":
        t = (d.get("episode") or {}).get("title_window")
    else:
        t = next((v.get("title_window") for v in d.get("shorts", []) if v.get("name") == key), None)
    return [[t[0] - 0.2, t[1] + 0.3]] if t else []


# ---------------------------------------------------------------- 3. plan
def align(tokens, ws):
    """per-token (start, end) from whisper words; words whisper missed are placed between their neighbours."""
    a = [norm(t) for t in tokens]
    b = [norm(w[2]) for w in ws]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    T = [None] * len(a)
    for i, j, n in sm.get_matching_blocks():
        for k in range(n):
            T[i + k] = (ws[j + k][0], ws[j + k][1])
    known = [i for i, t in enumerate(T) if t]
    for i in range(len(T)):
        if T[i] is None:
            p = max([k for k in known if k < i], default=None)
            q = min([k for k in known if k > i], default=None)
            if p is None and q is None:
                T[i] = (0, 0.2)
            elif p is None:
                T[i] = (T[q][0] - 0.25 * (q - i), T[q][0] - 0.25 * (q - i) + 0.2)
            elif q is None:
                T[i] = (T[p][1] + 0.25 * (i - p), T[p][1] + 0.25 * (i - p) + 0.2)
            else:
                f = (i - p) / (q - p)
                s = T[p][1] + (T[q][0] - T[p][1]) * f
                T[i] = (s, s + 0.15)
    return T


def _find(ws, phrase):
    p = [norm(x) for x in phrase.split() if norm(x)]
    b = [norm(w[2]) for w in ws]
    best = (0, 0)
    for j in range(max(1, len(b) - len(p) + 1)):
        r = difflib.SequenceMatcher(None, p, b[j:j + len(p)], autojunk=False).ratio()
        if r > best[0]:
            best = (r, j)
    return best


def _frames(video, t0, t1, w, h):
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(max(0, t0)), "-to", str(t1), "-i", video,
                          "-vf", f"fps=4,scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w).astype(float)


def plan(geom, video, wall, wins, verses, avoid=(), end=1e9, ytop_v=SHORT_YTOP, cta=None, anchor_hold_max=1.6):
    """One event per verse occurrence: tokens, word times, t0/t1, side, block top, brightness line."""
    import numpy as np
    ev = []
    for v in verses:
        toks = v["text"].split()
        for w in wins:
            if w["clip"] != v["clip"]:
                continue
            ws = [x for x in wall if w["t0"] - 0.3 <= x[0] <= w["t1"] + 0.3]
            if not ws:
                continue
            if v.get("anchor"):
                r, j = _find(ws, v.get("match") or v["text"])
                if r < 0.55:
                    log("skip", v["ref"], "anchor", round(r, 2))
                    continue
                a0 = ws[j][0]
                times = [(a0 + 0.11 * i, a0 + 0.11 * i + 0.1) for i in range(len(toks))]
                hold = min(anchor_hold_max, float(v.get("hold", anchor_hold_max)))
            else:
                r, j = _find(ws, " ".join(toks[:min(6, len(toks))]))
                if r < 0.55:
                    log("skip", v["ref"], round(r, 2))
                    continue
                times = align(toks, ws[j:j + len(toks) + 6])
                hold = float(v.get("khold", v.get("hold", 1.0)))
                if len(times) > 1 and times[1][0] - times[0][0] > 0.5:
                    times[0] = (times[1][0] - 0.3, times[1][0] - 0.05)
            ev.append(dict(clip=v["clip"], ref=v["ref"], tokens=toks, gold=v.get("gold", []), times=times,
                           t0=times[0][0] - 0.15, t1=min(times[-1][1] + hold, w["t1"] - 0.1), wend=w["t1"] - 0.1,
                           qopen=v.get("qopen", True), qclose=v.get("qclose", True), ov=v))
    ev.sort(key=lambda e: e["t0"])
    for a, b in avoid:
        for e in ev:
            if e["t0"] < b and e["t1"] > a:
                e["t0"] = max(e["t0"], b + 0.1)
    for i, e in enumerate(ev):
        lim = min(e["wend"], end - 0.1)
        if i + 1 < len(ev):
            lim = min(lim, ev[i + 1]["t0"] - 0.05)
        e["t1"] = min(e["t1"], lim)
    ev = [e for e in ev if e["t1"] - e["t0"] >= 0.8]
    for e in ev:
        o = e.pop("ov")
        e.pop("wend")
        if geom == "169":
            fr = _frames(video, e["t0"], e["t1"], 960, 540)
            r0, r1 = 371, 495
            reg = {"L": (55, 415), "R": (545, 905)}

            def busy(x0, x1):
                g = fr[:, r0:r1, x0:x1]
                return np.abs(np.diff(g, axis=1)).mean() + np.abs(np.diff(g, axis=2)).mean()
            side = o.get("side") or ("L" if busy(*reg["L"]) <= busy(*reg["R"]) else "R")
            if cta and not o.get("side") and e["t0"] < cta[1] and e["t1"] > cta[0]:
                side = "L"          # the Subscribe pop-up sits bottom right
            if "ytop" in o:
                e["ytop"] = o["ytop"]
                r0 = int(o["ytop"] / 2)
                r1 = r0 + 124
            x0, x1 = reg[side]
        else:
            fr = _frames(video, e["t0"], e["t1"], 540, 960)
            yt = o.get("ytop", ytop_v)
            e["ytop"] = yt
            r0, r1, x0, x1, side = int(yt / 2), int(yt / 2) + 100, 30, 510, "C"
        e["side"] = side
        e["lumline"] = [[round(e["t0"] + i / 4, 3), round(float(fr[i, r0:r1, x0:x1].mean()), 1)]
                        for i in range(fr.shape[0])]
        log(e["ref"], round(e["t0"], 2), round(e["t1"], 2), side)
    return ev


# ---------------------------------------------------------------- 4. render
def _gold_marks(tokens, phrases):
    nt = [norm(t) for t in tokens]
    m = [False] * len(tokens)
    for ph in phrases:
        p = [norm(x) for x in ph.split()]
        for i in range(len(nt) - len(p) + 1):
            if nt[i:i + len(p)] == p:
                for k in range(i, i + len(p)):
                    m[k] = True
    return m


def _sprite(text, f, col, track=0):
    from PIL import Image, ImageDraw
    tw = int(f.getlength(text) + track * len(text)) + 30
    asc, desc = f.getmetrics()
    im = Image.new("RGBA", (tw, asc + desc + 30), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if track:
        x = 12
        for ch in text:
            d.text((x, 10), ch, font=f, fill=col + (255,))
            x += f.getlength(ch) + track
    else:
        d.text((12, 10), text, font=f, fill=col + (255,))
    return im


def _ease(x):
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def render(ev, geom, outdir):
    """One PNG sequence (the verse's region only) for one event; returns where and when it goes."""
    from PIL import Image
    g = GEOM[geom]
    f, fr = font(g["size"], b"Medium"), font(g["ref"], b"SemiBold")
    toks, times = list(ev["tokens"]), ev["times"]
    marks = _gold_marks(toks, ev.get("gold", []))
    if ev.get("qopen", True):
        toks[0] = "“" + toks[0]
    if ev.get("qclose", True):
        toks[-1] = toks[-1] + "”"
    sw = f.getlength(" ")
    lines, cur, cw = [], [], 0
    for i, t in enumerate(toks):
        w = f.getlength(t)
        if cur and cw + sw + w > g["boxw"]:
            lines.append(cur)
            cur, cw = [], 0
        cw += (sw if cur else 0) + w
        cur.append(i)
    if cur:
        lines.append(cur)
    lineof = {i: li for li, ln in enumerate(lines) for i in ln}
    LL = ev.get("lumline") or []

    def dark(ln):
        a, b = times[ln[0]][0], times[ln[-1]][1] + 0.6
        v = [l for t, l in LL if a - 0.15 <= t <= b] or [l for t, l in LL]
        return bool(v) and sum(v) / len(v) > BRIGHT
    ldark = [dark(ln) for ln in lines]
    spr = [_sprite(t, f, ((DARKG if ldark[lineof[i]] else GOLD) if marks[i] else (DARKW if ldark[lineof[i]] else WHITE)))
           for i, t in enumerate(toks)]
    rs = _sprite(ev["ref"].upper(), fr, DARKG if ldark[0] else GOLD, track=g["ref"] * 0.28)
    right, centre = ev["side"] == "R", ev["side"] == "C"
    RW, RH = g["boxw"] + 80, g["lh"] * 3 + 90
    rx = (g["W"] - g["boxw"]) // 2 - 40 if centre else (g["W"] - g["margin"] - g["boxw"] - 40 if right else g["margin"] - 40)
    ry = ev.get("ytop", g["ytop"]) - 40
    xpos = {}
    for ln in lines:
        widths = [f.getlength(toks[i]) for i in ln]
        total = sum(widths) + sw * (len(ln) - 1)
        x = (g["boxw"] - total) / 2 if centre else ((g["boxw"] - total) if right else 0)
        for i, wd in zip(ln, widths):
            xpos[i] = x
            x += wd + sw
    t0, t1 = ev["t0"], ev["t1"]
    n = int(round((t1 - t0) * FPS)) + 1
    first = [times[ln[0]][0] for ln in lines]
    if os.path.isdir(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir)
    for k in range(n):
        t = t0 + k / FPS
        im = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
        fade_all = _ease((t1 - t) / 0.2)
        L = max([li for li, s in enumerate(first) if s <= t + 0.02] or [0])
        slide = _ease((t - first[L]) / 0.22) if L >= 2 else 1.0
        ra = _ease((t - t0) / 0.15) * fade_all
        if ra > 0:
            r = rs.copy()
            r.putalpha(r.split()[3].point(lambda v, a=ra: int(v * a)))
            rxo = (g["boxw"] - rs.width) / 2 + 12 if centre else (g["boxw"] - rs.width + 24 if right else -12)
            im.alpha_composite(r, (int(40 + rxo), 10))
        for i in range(len(toks)):
            li, ts = lineof[i], times[i][0]
            if ts > t + 0.02 or li < L - 2 or (li == L - 2 and slide >= 1):
                continue
            row = li - (L - 1) if L >= 1 else li
            yoff = (row + (1 - slide)) * g["lh"] if L >= 2 else li * g["lh"]
            a = _ease((t - ts) / 0.12)
            if L >= 2 and li == L - 2:
                a *= max(0.0, 1 - slide * 3.0)
            a *= fade_all
            if a <= 0:
                continue
            rise = (1 - _ease((t - ts) / 0.16)) * 12
            s = spr[i].copy()
            s.putalpha(s.split()[3].point(lambda v, a=a: int(v * a)))
            im.alpha_composite(s, (int(40 + xpos[i] - 12), int(54 + yoff + rise)))
        im.save(os.path.join(outdir, f"{k:04d}.png"))
    return dict(x=rx, y=ry, t0=t0, t1=t1, n=n, dir=outdir, ref=ev["ref"])


def render_all(evs, geom, kdir):
    out = []
    for j, ev in enumerate(evs):
        out.append(render(ev, geom, os.path.join(kdir, f"{geom}_{j:02d}")))
        log("rendered", ev["ref"], out[-1]["n"], "frames")
    json.dump(out, open(os.path.join(kdir, f"events_{geom}.json"), "w"), indent=1)
    return out


# ---------------------------------------------------------------- 5. burn
def burn(src, events, out, crf="18"):
    """Lay the PNG sequences over src (one encode, audio copied)."""
    if not events:
        shutil.copyfile(src, out)
        log("no verses: copied", out)
        return
    inp, g, cur = ["-i", src], [], "0:v"
    for j, e in enumerate(events):
        inp += ["-framerate", str(FPS), "-start_number", "0", "-i", f"{e['dir']}/%04d.png"]
        g.append(f"[{j + 1}:v]format=rgba,setpts=PTS+{e['t0']}/TB[k{j}]")
        g.append(f"[{cur}][k{j}]overlay={e['x']}:{e['y']}:eof_action=pass:"
                 f"enable='between(t,{e['t0']},{e['t1'] + 0.05})'[o{j}]")
        cur = f"o{j}"
    g.append(f"[{cur}]format=yuv420p[v]")
    fs = out + ".filter"
    open(fs, "w").write(";\n".join(g))
    subprocess.run(["ffmpeg", "-v", "error", "-y"] + inp + ["-filter_complex_script", fs, "-map", "[v]", "-map", "0:a",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-c:a", "copy",
                    "-movflags", "+faststart", "-use_editlist", "0", out], check=True)
    os.remove(fs)
    log("burned", out)


# ---------------------------------------------------------------- erase an old card from a finished Short
def erase(src, out, wins, zoom=1.15, crop_x=0.5, fade=0.12):
    """Cover the top band during wins with a band rebuilt from the Short's own picture
    (the assembler's blur fill: same crop, scale, blur and grade), so an old verse card disappears.
    zoom/crop_x: the manifest's short_zoom and crop_x for that Short."""
    W, H = 1080, 1920
    ph = int(round(W * zoom * 9 / 16))
    y0 = (H - ph) // 2
    sw = W * zoom
    off = (sw - W) * crop_x
    x0, x1 = 656 * sw / 1920 - off, 1264 * sw / 1920 - off
    chain = (f"crop={round(x1 - x0)}:{ph}:{round(x0)}:{y0},scale=270:480,boxblur=12:2,"
             f"eq=brightness=-0.16:saturation=0.85,scale={W}:{H}:flags=bicubic,crop={W}:{y0 - 1}:0:0")
    n = len(wins)
    fc = f"[0:v]split={n + 1}[base]" + "".join(f"[s{i}]" for i in range(n)) + ";"
    for i, (a, b) in enumerate(wins):
        fc += (f"[s{i}]{chain},format=yuva420p,fade=in:st={a - fade}:d={fade}:alpha=1,"
               f"fade=out:st={b}:d={fade}:alpha=1[r{i}];")
    prev = "base"
    for i, (a, b) in enumerate(wins):
        o = "v" if i == n - 1 else f"v{i}"
        fc += f"[{prev}][r{i}]overlay=0:0:enable='between(t,{a - fade - 0.05},{b + fade + 0.05})'[{o}];"
        prev = o
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-filter_complex", fc.rstrip(";"), "-map", "[v]",
                    "-map", "0:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
                    "-c:a", "copy", "-movflags", "+faststart", out], check=True)
    log("erased", out)


# ---------------------------------------------------------------- command line
def _pairs(xs):
    return [[float(a) for a in x.split(",")] for x in xs]


def main():
    ap = argparse.ArgumentParser(description="Scroll Back: verses word by word on finished files.")
    ap.add_argument("mode", choices=["episode", "vertical", "short", "erase", "words"])
    ap.add_argument("args", nargs="*", help="manifest (episode/vertical/short); IN OUT a,b... (erase); VIDEO OUT (words)")
    ap.add_argument("--shotlog")
    ap.add_argument("--name", help="the output's name in the shot log (vertical and Shorts)")
    ap.add_argument("--in", dest="src")
    ap.add_argument("--out")
    ap.add_argument("--also", action="append", default=[], help="SRC=DST: another cut on the same timeline (social, TikTok)")
    ap.add_argument("--avoid", action="append", default=[], help="a,b: seconds no verse may cover (replaces the defaults)")
    ap.add_argument("--end", type=float, help="no verse after this second (default: before the end card)")
    ap.add_argument("--cta", default="35,39.2", help="16:9 Subscribe pop-up a,b (verses there go left), or 'none'")
    ap.add_argument("--ytop", type=int, help="vertical block top in px (default 273 vertical episode, 251 Shorts)")
    ap.add_argument("--kdir", help="work folder (default kin/<name>)")
    ap.add_argument("--crf", default="18")
    ap.add_argument("--zoom", type=float, default=1.15, help="erase: the Short's short_zoom")
    ap.add_argument("--crop-x", type=float, default=0.5, help="erase: the Short's crop_x")
    a = ap.parse_args()

    if a.mode == "words":
        words(a.args[0], a.args[1])
        return
    if a.mode == "erase":
        erase(a.args[0], a.args[1], _pairs(a.args[2:]), a.zoom, a.crop_x)
        return

    man = json.load(open(a.args[0]))
    verses = [v for v in man.get("scripture", []) if v.get("text")]
    if not (a.shotlog and a.src and a.out):
        raise SystemExit("--shotlog, --in and --out are needed")
    key = "episode" if a.mode == "episode" else a.name
    if not key:
        raise SystemExit("--name is needed for vertical and short")
    D = duration(a.src)
    kdir = a.kdir or os.path.join("kin", key)
    os.makedirs(kdir, exist_ok=True)
    wf = os.path.join(kdir, "words.json")
    wall = json.load(open(wf)) if os.path.exists(wf) else words(a.src, wf)
    cold = float((man.get("cold_open") or {}).get("length", 0) or 0)
    tw = title_window(a.shotlog, key)
    if a.mode == "short":
        avoid = [[0, HOOK_SECS]]
        end = D - 2.0
    elif a.mode == "vertical":
        avoid = [[0, max(HOOK_SECS, cold + 0.1)]] + tw
        end = D - 3.7
    else:
        avoid = ([[0, cold + 0.1]] if cold else []) + tw
        end = D - 4.0
    if a.avoid:
        avoid = _pairs(a.avoid)
    if a.end is not None:
        end = a.end
    geom = "169" if a.mode == "episode" else "v"
    cta = None if (a.mode != "episode" or a.cta == "none") else [float(x) for x in a.cta.split(",")]
    ytop = a.ytop or (VERTICAL_YTOP if a.mode == "vertical" else SHORT_YTOP)
    log("avoid", avoid, "end", round(end, 2))
    evs = plan(geom, a.src, wall, windows(a.shotlog, key), verses, avoid=avoid, end=end, ytop_v=ytop, cta=cta)
    json.dump(evs, open(os.path.join(kdir, "plan.json"), "w"), indent=0)
    events = render_all(evs, geom, kdir)
    burn(a.src, events, a.out, a.crf)
    for pair in a.also:
        s, d = pair.split("=", 1)
        burn(s, events, d, a.crf)
    log("VERSESDONE", len(events), "verses")


if __name__ == "__main__":
    main()
