# Scroll Back pipeline

The scripts that finish every Scroll Back episode. The production bible is the source of truth for
every decision; this repo holds the code it describes.

| File | What it does |
|---|---|
| `scrollback_assemble.py` | Clip URLs in. Out: the 16:9 episode (title card, end card, theme music, loudness), the five letterboxed YouTube Shorts (hooks, captions, end cards), the vertical full episode for TikTok and Instagram (`E{n}F1V`, whose first frame is its profile-grid cover), with `--social` the Instagram versions of the Shorts (end cards point to YouTube) and with `--tiktok` the TikTok versions (end cards point to the full episode on the TikTok page). Also runs the script check |
| `scrollback_thumbnail.py` | Puts the episode thumbnail text on generated art, to the bible's thumbnail spec |
| `example_manifest.json` | Episode 2's manifest with the clip links removed, including the theme block and the vertical episode - copy it for each new episode and set `season` and `season_episode` (8 episodes a season; Episode 9 is Season 2, Episode 1), which the title card, trailer and cover show |

## Running it

Needs Python 3 with Pillow, ffmpeg and ffprobe. faster-whisper (word timings for captions and the
script check) and numpy are optional; the Higgsfield sandbox has all of them.

```
curl -fsSLO https://raw.githubusercontent.com/ScrollBackBible/scroll-back-pipeline/main/scrollback_assemble.py
curl -fsSLO https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-ExtraBold.ttf
python3 scrollback_assemble.py E3.json --check    # script check only, right after generation
python3 scrollback_assemble.py E3.json            # episode, Shorts and the vertical episode
python3 scrollback_assemble.py E3.json --social   # Instagram Shorts (end cards point to YouTube)
python3 scrollback_assemble.py E3.json --tiktok   # TikTok Shorts (end cards point to the full episode on the TikTok page)
python3 scrollback_assemble.py E3.json --analyze  # cut report only
python3 scrollback_assemble.py E3.json --put E3F1=<presigned PUT url>   # upload a result (uses curl)
```

The font must be Montserrat ExtraBold (SHA-256 `d3ac6a843d3ba6d5cafd44cf39e437055c8aed7e261010f595f57d3c7b3e2c1b`);
both scripts refuse anything else.

Real manifests (`E2.json`, `E3.json` ...) contain private clip links and are never committed here.
