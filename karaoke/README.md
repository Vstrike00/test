# 🎤 Karaoke Maker (Mac)

Drop in an MP3 and get a karaoke video (`.mp4`) with:

- **Vocals turned down**: the centre of the stereo mix, where vocals usually sit, is cancelled and the bass is kept. A slider sets how strong this is.
- **Synced lyrics** that light up word by word in time with the song.
- Album art (blurred) as the background, or a gradient if the file has none. You also get a title card and a countdown before each verse.

## Start it

1. Open **Terminal** (press ⌘ Space, type `Terminal`, press Enter). Type `bash ` with a space after it, drag **`Start Karaoke Maker.command`** into the Terminal window, then press **Enter**.
   - This works even when macOS won't let you double-click the file.
   - To make double-clicking work from then on, run this once in Terminal (again, drag the file in where the path goes):
     `xattr -c "path/to/Start Karaoke Maker.command"; chmod +x "path/to/Start Karaoke Maker.command"`
2. The first start installs what it needs. This takes a few minutes and only happens once:
   - `ffmpeg` through [Homebrew](https://brew.sh). Install Homebrew first if you don't have it.
   - A Python environment in `.venv/`.
3. Your browser opens **http://127.0.0.1:5123**. Pick a song, then click **Make karaoke video**.

Videos are saved to **`~/Movies/Karaoke/`**. You can also download them or show them in Finder from the page.
Close the Terminal window to quit.

## Where the lyrics come from

In **Auto** mode it tries these in order:

1. **Your own `.lrc` file**, if you upload one (under "More options").
2. **lrclib.net**: a free database of time-synced lyrics. It searches by artist and title, which are read from the MP3 tags or from a file named `Artist - Title.mp3`. If the tags are missing, type them into the form.
3. **Whisper transcription**: if the song isn't found online, the lyrics are worked out from the audio itself, on your Mac and offline. The first time, it downloads a speech model (~500 MB for "small"). Expect a few minutes per song. Fast songs and heavy mixes can come out with some wrong words.

Each video also saves a matching `.lrc` file next to it. To fix a wrong word or timing, edit that file in TextEdit, upload it under **More options → Own lyrics file**, and render again.

## Notes

- If your ffmpeg can't draw subtitles (recent Homebrew builds leave out `libass`), the app draws the lyrics itself with Python. It looks the same, it's just a little slower. If you'd rather use ffmpeg for this, run `brew install ffmpeg-full`.

- The vocal reduction is the classic "karaoke button" trick, not AI separation. It works well on most studio recordings. Vocals with lots of stereo reverb may still be faintly audible, and mono recordings can't be processed.
- Command line use, without the browser:
  ```bash
  .venv/bin/python -c "from pathlib import Path; from karaoke_core import make_karaoke; make_karaoke(Path('song.mp3'), Path('out'), Path('/tmp/kw'))"
  ```
