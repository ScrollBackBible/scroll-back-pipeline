# Scroll Back pipeline

The scripts that finish every Scroll Back episode. The production bible is the source of truth for
every decision; this repo holds the code it describes.

| File | What it does |
|---|---|
| `scrollback_assemble.py` | Clip URLs in; the 16:9 episode (title card, end card, loudness) and the five letterboxed Shorts (hooks, captions, end cards) out |
| `scrollback_thumbnail.py` | Puts the episode thumbnail text on generated art, to the bible's thumbnail spec |
| `example_manifest.json` | Episode 2's manifest with the clip links removed - copy it for each new episode |

## Running it

Needs Python 3 with Pillow, ffmpeg and ffprobe. faster-whisper (word timings for captions) and numpy
are optional; the Higgsfield sandbox has all of them.

```
curl -fsSLO https://raw.githubusercontent.com/ScrollBackBible/scroll-back-pipeline/main/scrollback_assemble.py
curl -fsSLO https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-ExtraBold.ttf
python3 scrollback_assemble.py E3.json            # episode + Shorts
python3 scrollback_assemble.py E3.json --analyze  # cut report only
```

The font must be Montserrat ExtraBold (SHA-256 `d3ac6a843d3ba6d5cafd44cf39e437055c8aed7e261010f595f57d3c7b3e2c1b`);
both scripts refuse anything else.

Real manifests (`E2.json`, `E3.json` ...) contain private clip links and are never committed here.
