# Jeopardy! for Mac

> Also in this repo: **[🎤 Karaoke Maker](karaoke/)**, which turns an MP3 into a karaoke video.

A single-file Jeopardy game host. Works offline.

## Quick start (no install)
Double-click `index.html` — it opens in Safari/Chrome. Press ⛶ for full screen.

## Native Mac app (.app / .dmg)
Requires [Node.js](https://nodejs.org). On your Mac:

```bash
npm install
npm start        # run as a desktop app
npm run build    # produces a .dmg in dist/
```

## Playing on a TV (second screen)
1. Connect the TV via HDMI. In **System Settings → Displays**, make sure it is **extended** (not mirrored).
2. Click **📺 TV Display** on your Mac. A second window opens (the packaged app puts it fullscreen on the TV automatically).
3. In a browser: drag that window onto the TV, then click it or press **F** for full screen. Allow pop-ups if asked.

The TV shows only the board, scores, the clue, and the answer once you reveal it. You keep the controls, and the answer is shown to you (host only) on your screen.

## How to play
- Click a tile to reveal the clue; **Space** reveals the answer, **Esc** closes it.
- Use ✓ / ✗ buttons to award or deduct points. Click a team name to set the active team.
- One hidden **Daily Double** (with wagering) is placed randomly.
- **Final Jeopardy** button runs the final round with per-team wagers.
- **Edit Board** to change teams, categories, clues, values, timer; export/import games as JSON.
- Progress is saved automatically in the browser/app.
