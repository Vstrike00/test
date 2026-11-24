"""Karaoke video pipeline: MP3 in -> MP4 with reduced vocals and synced lyrics out.

Steps
  1. Read artist/title/duration (ffprobe) and embedded cover art.
  2. Make the backing track: cancel centre-panned audio (where vocals usually sit)
     while keeping the bass, then mix a little of the original back in so the
     result sounds natural ("vocals turned down" rather than "vocals removed").
  3. Get timed lyrics: a user-supplied .lrc file, else synced lyrics from
     lrclib.net (free, no key), else transcribe the song locally with Whisper.
  4. Write an .ass subtitle file with karaoke word highlighting and render
     everything with ffmpeg.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

Log = Callable[[str], None]

WIDTH, HEIGHT = 1280, 720


# --------------------------------------------------------------------------- data


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Line:
    start: float
    end: float
    words: list[Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


# ------------------------------------------------------------------------ helpers


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise RuntimeError(f"{Path(cmd[0]).name} failed:\n{tail}")
    return proc


def check_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise RuntimeError(
                f"'{tool}' was not found. Install it with:  brew install ffmpeg"
            )


def probe(mp3: Path) -> dict:
    out = run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(mp3)]
    ).stdout
    data = json.loads(out)
    fmt = data.get("format", {})
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    has_cover = any(
        s.get("codec_type") == "video" for s in data.get("streams", [])
    )
    channels = next(
        (s.get("channels", 2) for s in data.get("streams", [])
         if s.get("codec_type") == "audio"), 2
    )
    return {
        "duration": float(fmt.get("duration", 0) or 0),
        "artist": tags.get("artist", "").strip(),
        "title": tags.get("title", "").strip(),
        "has_cover": has_cover,
        "channels": channels,
    }


def guess_from_filename(mp3: Path) -> tuple[str, str]:
    """'Artist - Title.mp3' -> ('Artist', 'Title')."""
    stem = re.sub(r"[_]+", " ", mp3.stem).strip()
    stem = re.sub(r"\s*[\(\[].*?(official|lyrics?|audio|video|hd|hq).*?[\)\]]",
                  "", stem, flags=re.I)
    if " - " in stem:
        a, t = stem.split(" - ", 1)
        return a.strip(), t.strip()
    return "", stem


# -------------------------------------------------------------------- backing track


def make_backing_track(src: Path, dst: Path, strength: float, stereo: bool,
                       log: Log) -> None:
    """strength 0..1: how much of the vocal (centre) to remove."""
    strength = max(0.0, min(1.0, strength))
    if not stereo:
        log("Song is mono - vocals can't be separated, only copying audio.")
        run(["ffmpeg", "-y", "-i", str(src), "-vn", "-c:a", "pcm_s16le", str(dst)])
        return

    keep = 1.0 - strength  # amount of the original mixed back in
    # [low]  : everything under 150 Hz untouched (kick drum + bass are centred too)
    # [side] : L-R / R-L above 150 Hz -> centre (vocals) cancels out
    # [orig] : a bit of the untouched song, so vocals are "lower" not "gone"
    graph = (
        "[0:a]aformat=channel_layouts=stereo,asplit=3[a][b][c];"
        "[a]lowpass=f=150,lowpass=f=150[low];"
        "[b]pan=stereo|c0=c0-c1|c1=c1-c0,highpass=f=150,highpass=f=150,"
        "volume=0.9[side];"
        f"[c]volume={keep:.3f}[orig];"
        f"[low]volume={strength:.3f}[lowv];"
        f"[side]volume={strength:.3f}[sidev];"
        "[lowv][sidev][orig]amix=inputs=3:normalize=0,"
        "alimiter=limit=0.95[out]"
    )
    log(f"Lowering vocals (strength {int(strength * 100)}%)...")
    run(["ffmpeg", "-y", "-i", str(src), "-filter_complex", graph,
         "-map", "[out]", "-c:a", "pcm_s16le", str(dst)])


# ------------------------------------------------------------------------- lyrics

LRC_TIME = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")


def parse_lrc(text: str, duration: float) -> list[Line]:
    """Parse LRC (line-synced) lyrics. Supports multiple timestamps per line and
    the enhanced <mm:ss.xx> per-word format."""
    entries: list[tuple[float, str]] = []
    for raw in text.splitlines():
        stamps = LRC_TIME.findall(raw)
        if not stamps:
            continue
        lyric = LRC_TIME.sub("", raw).strip()
        for m, s in stamps:
            entries.append((int(m) * 60 + float(s.replace(":", ".")), lyric))
    entries.sort(key=lambda e: e[0])

    lines: list[Line] = []
    for i, (start, lyric) in enumerate(entries):
        nxt = entries[i + 1][0] if i + 1 < len(entries) else duration or start + 5
        if not lyric:
            continue  # blank LRC lines just mark the end of the previous line

        enhanced = re.findall(r"<(\d+):(\d+(?:\.\d+)?)>\s*([^<]*)", lyric)
        if enhanced:
            words = []
            stamps = [(int(m) * 60 + float(s), w.strip()) for m, s, w in enhanced]
            for j, (ws, wt) in enumerate(stamps):
                we = stamps[j + 1][0] if j + 1 < len(stamps) else nxt
                for k, part in enumerate(wt.split()):
                    words.append(Word(part, ws, we))
            words = [w for w in words if w.text]
            if words:
                lines.append(Line(words[0].start, min(nxt, words[-1].end), words))
            continue

        tokens = lyric.split()
        # A sung line rarely lasts longer than ~0.6s per word; stop the
        # highlight there instead of dragging it across an instrumental break.
        end = min(nxt, start + max(2.0, 0.6 * len(tokens) + 1.0))
        lines.append(Line(start, end, spread_words(tokens, start, end)))
    return lines


def spread_words(tokens: list[str], start: float, end: float) -> list[Word]:
    """Estimate word timings inside a line by distributing time by word length."""
    weights = [len(t) + 2 for t in tokens]
    total = sum(weights) or 1
    span = max(0.1, end - start) * 0.92  # last word finishes a bit early
    t, words = start, []
    for tok, w in zip(tokens, weights):
        d = span * w / total
        words.append(Word(tok, t, t + d))
        t += d
    return words


def fetch_lrclib(artist: str, title: str, duration: float, log: Log) -> Optional[str]:
    if not title:
        return None
    headers = {"User-Agent": "KaraokeMaker/1.0 (local desktop app)"}

    def get(url: str):
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    log(f"Searching lrclib.net for synced lyrics: {artist or '?'} - {title}")
    try:
        if artist:
            q = urllib.parse.urlencode({
                "artist_name": artist, "track_name": title,
                "duration": int(round(duration)),
            })
            try:
                hit = get(f"https://lrclib.net/api/get?{q}")
                if hit.get("syncedLyrics"):
                    return hit["syncedLyrics"]
            except urllib.error.HTTPError:
                pass
        q = urllib.parse.urlencode(
            {"track_name": title, **({"artist_name": artist} if artist else {})})
        results = get(f"https://lrclib.net/api/search?{q}") or []
        synced = [r for r in results if r.get("syncedLyrics")]
        if duration:
            # prefer the version whose length matches this file
            synced.sort(key=lambda r: abs((r.get("duration") or 0) - duration))
            synced = [r for r in synced
                      if abs((r.get("duration") or 0) - duration) < 8]
        if synced:
            best = synced[0]
            log(f"Found: {best.get('artistName')} - {best.get('trackName')}")
            return best["syncedLyrics"]
    except Exception as e:  # network trouble shouldn't kill the job
        log(f"lrclib lookup failed ({e}).")
    return None


def transcribe(mp3: Path, model_name: str, language: Optional[str],
               log: Log) -> list[Line]:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError(
            "Whisper (automatic transcription) is not installed. Run:  "
            ".venv/bin/pip install faster-whisper  - or upload an .lrc file."
        )
    log(f"Transcribing lyrics with Whisper ({model_name}). The first run downloads "
        "the model; this can take a few minutes...")
    model = WhisperModel(model_name, device="auto", compute_type="auto")
    segments, info = model.transcribe(
        str(mp3), word_timestamps=True, language=language or None,
        vad_filter=False, condition_on_previous_text=False, beam_size=5,
    )
    log(f"Detected language: {info.language}")
    lines: list[Line] = []
    for seg in segments:
        words = [Word(w.word.strip(), w.start, w.end)
                 for w in (seg.words or []) if w.word.strip()]
        # Whisper segments can be long; break them into singable lines.
        chunk: list[Word] = []
        for w in words:
            gap = w.start - chunk[-1].end if chunk else 0
            if chunk and (len(chunk) >= 8 or gap > 1.2):
                lines.append(Line(chunk[0].start, chunk[-1].end, chunk))
                chunk = []
            chunk.append(w)
        if chunk:
            lines.append(Line(chunk[0].start, chunk[-1].end, chunk))
        if lines:
            log(f"  [{fmt_time(lines[-1].start)}] {lines[-1].text}")
    return lines


# ----------------------------------------------------------------------- subtitles


def fmt_time(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m)}:{s:05.2f}"


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(s: str) -> str:
    return s.replace("\\", "/").replace("{", "(").replace("}", ")")


def build_ass(lines: list[Line], artist: str, title: str, duration: float) -> str:
    # Colours are &HAABBGGRR. Karaoke: Secondary = not yet sung, Primary = sung.
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Lyric,Arial,58,&H0000D7FF,&H00FFFFFF,&H00301010,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,5,60,60,0,1
Style: Title,Arial,72,&H00FFFFFF,&H00FFFFFF,&H00301010,&H80000000,-1,0,0,0,100,100,0,0,1,4,3,5,60,60,0,1
Style: Artist,Arial,44,&H00D0D0D0,&H00FFFFFF,&H00301010,&H80000000,0,0,0,0,100,100,0,0,1,3,2,5,60,60,0,1
Style: Dots,Arial,64,&H0000D7FF,&H00FFFFFF,&H00301010,&H80000000,-1,0,0,0,100,100,0,0,1,3,2,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev: list[str] = []
    cx = WIDTH // 2
    rows = (HEIGHT // 2 - 50, HEIGHT // 2 + 50)

    def add(start, end, style, text, layer=0):
        if end - start > 0.05:
            ev.append(f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},"
                      f"{style},,0,0,0,,{text}")

    LEAD = 3.0  # seconds a line is shown before it is sung

    # Title card before the singing starts
    first = lines[0].start if lines else duration
    title_end = min(max(first - LEAD - 0.3, 0), 8.0)
    if title_end > 1.0:
        fade = r"{\fad(400,400)}"
        add(0, title_end, "Title",
            fade + r"{\pos(%d,%d)}" % (cx, HEIGHT // 2 - 30) + ass_escape(title or "Karaoke"))
        if artist:
            add(0, title_end, "Artist",
                fade + r"{\pos(%d,%d)}" % (cx, HEIGHT // 2 + 40) + ass_escape(artist))

    # Lyrics: two rows, alternating, upcoming line visible ahead of time.
    shown_until = [0.0, 0.0]
    for i, line in enumerate(lines):
        row = i % 2
        appear = max(line.start - LEAD, shown_until[row] + 0.05)
        if i > 0:
            appear = max(appear, lines[i - 1].start - 0.0)  # not before previous starts
        appear = min(appear, line.start)
        gone = line.end + 0.6
        if i + 1 < len(lines):
            gone = min(max(gone, lines[i + 1].start + 0.4),
                       lines[i + 1].start + 2.5, line.end + 2)
            # the line two ahead re-uses this row; leave room for it
            if i + 2 < len(lines):
                gone = min(gone, max(line.end + 0.2, lines[i + 2].start - LEAD))
        shown_until[row] = gone

        parts = []
        lead_cs = int(round((line.start - appear) * 100))
        if lead_cs > 0:
            parts.append(r"{\k%d}" % lead_cs)
        t = line.start
        for w in line.words:
            gap = int(round((w.start - t) * 100))
            if gap > 0:
                parts.append(r"{\k%d}" % gap)
            dur = max(1, int(round((w.end - max(w.start, t)) * 100)))
            parts.append(r"{\kf%d}%s " % (dur, ass_escape(w.text)))
            t = max(t, w.end)
        text = r"{\pos(%d,%d)\fad(200,250)}" % (cx, rows[row]) + "".join(parts).rstrip()
        add(appear, gone, "Lyric", text)

        # Countdown dots during long instrumental gaps
        nxt = lines[i + 1].start if i + 1 < len(lines) else None
        if nxt and nxt - line.end > 10:
            cd_start = nxt - 3.0
            for k in range(3):
                add(cd_start + k, cd_start + k + 1, "Dots",
                    r"{\pos(%d,%d)}" % (cx, rows[0] - 90) + "  ".join("●" * (3 - k)))

    # Countdown before the very first line, too
    if lines and lines[0].start > 4:
        cd_start = lines[0].start - 3.0
        for k in range(3):
            add(cd_start + k, cd_start + k + 1, "Dots",
                r"{\pos(%d,%d)}" % (cx, rows[0] - 90) + "  ".join("●" * (3 - k)))

    return header + "\n".join(ev) + "\n"


# -------------------------------------------------------------------------- video


def render_video(backing: Path, ass: Path, out: Path, cover: Optional[Path],
                 duration: float, log: Log) -> None:
    log("Rendering video...")
    # ffmpeg's filter-graph parser needs ':' '\' and quotes escaped in paths.
    ass_arg = str(ass).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    if cover:
        video_in = ["-loop", "1", "-framerate", "25", "-i", str(cover)]
        vf = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
              f"crop={WIDTH}:{HEIGHT},boxblur=25:2,eq=brightness=-0.25:saturation=1.2,"
              f"format=yuv420p,ass='{ass_arg}'")
    else:
        video_in = ["-f", "lavfi", "-i",
                    f"gradients=s={WIDTH}x{HEIGHT}:r=25:c0=0x1b0f3a:c1=0x0a3d62:"
                    f"x0=0:y0=0:x1={WIDTH}:y1={HEIGHT}:speed=0.008"]
        vf = f"format=yuv420p,ass='{ass_arg}'"
    cmd = ["ffmpeg", "-y", *video_in, "-i", str(backing),
           "-map", "0:v", "-map", "1:a", "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
           "-crf", "23", "-r", "25",
           "-c:a", "aac", "-b:a", "192k",
           "-t", f"{duration:.2f}", "-shortest",
           "-movflags", "+faststart", str(out)]
    try:
        run(cmd)
    except RuntimeError:
        if cover:
            raise
        # Older ffmpeg without the 'gradients' source: plain colour background.
        cmd[cmd.index("-i") + 1] = f"color=c=0x1b0f3a:s={WIDTH}x{HEIGHT}:r=25"
        run(cmd)


# --------------------------------------------------------------------------- main


def make_karaoke(
    mp3: Path,
    out_dir: Path,
    work_dir: Path,
    log: Log = print,
    artist: str = "",
    title: str = "",
    strength: float = 0.85,
    lyrics_source: str = "auto",          # auto | online | whisper | lrc
    lrc_text: Optional[str] = None,
    whisper_model: str = "small",
    language: Optional[str] = None,
) -> Path:
    check_ffmpeg()
    work_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    info = probe(mp3)
    duration = info["duration"]
    fa, ft = guess_from_filename(mp3)
    artist = artist or info["artist"] or fa
    title = title or info["title"] or ft
    log(f"Song: {artist or 'Unknown artist'} - {title}  ({fmt_time(duration)})")

    cover = None
    if info["has_cover"]:
        cover = work_dir / "cover.jpg"
        try:
            run(["ffmpeg", "-y", "-i", str(mp3), "-an", "-frames:v", "1", str(cover)])
            log("Using embedded album art as background.")
        except RuntimeError:
            cover = None

    # Lyrics
    lines: list[Line] = []
    if lrc_text and lrc_text.strip():
        log("Using the uploaded .lrc lyrics.")
        lines = parse_lrc(lrc_text, duration)
    if not lines and lyrics_source in ("auto", "online"):
        synced = fetch_lrclib(artist, title, duration, log)
        if synced:
            lines = parse_lrc(synced, duration)
            log(f"Got {len(lines)} synced lyric lines.")
        elif lyrics_source == "online":
            raise RuntimeError("No synced lyrics found online. Try 'Auto' or "
                               "'Transcribe', or check the artist/title.")
        else:
            log("No synced lyrics online - falling back to transcription.")
    if not lines and lyrics_source in ("auto", "whisper"):
        lines = transcribe(mp3, whisper_model, language, log)
    if not lines:
        log("Warning: no lyrics found - the video will have music only.")

    # Audio
    backing = work_dir / "backing.wav"
    make_backing_track(mp3, backing, strength, info["channels"] >= 2, log)

    # Subtitles + video
    ass = work_dir / "lyrics.ass"
    ass.write_text(build_ass(lines, artist, title, duration), encoding="utf-8")
    safe = re.sub(r"[^\w\- ]+", "", f"{artist} - {title}" if artist else title)
    safe = re.sub(r"\s+", " ", safe).strip() or "karaoke"
    out = out_dir / f"{safe} (Karaoke).mp4"
    render_video(backing, ass, out, cover, duration, log)
    # Keep the timed lyrics next to the video so they can be fixed and re-used.
    out.with_suffix(".lrc").write_text(to_lrc(lines), encoding="utf-8")
    log(f"Done! Saved to {out}")
    return out


def to_lrc(lines: list[Line]) -> str:
    def stamp(t):
        m, s = divmod(max(0.0, t), 60)
        return f"[{int(m):02d}:{s:05.2f}]"
    rows = []
    for i, l in enumerate(lines):
        rows.append(stamp(l.start) + l.text)
        nxt = lines[i + 1].start if i + 1 < len(lines) else None
        if nxt is None or nxt - l.end > 3:
            rows.append(stamp(l.end))
    return "\n".join(rows) + "\n"
