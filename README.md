# Jeopardy! for Mac

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

## How to play
- Click a tile to reveal the clue; **Space** reveals the answer, **Esc** closes it.
- Use ✓ / ✗ buttons to award or deduct points. Click a team name to set the active team.
- One hidden **Daily Double** (with wagering) is placed randomly.
- **Final Jeopardy** button runs the final round with per-team wagers.
- **Edit Board** to change teams, categories, clues, values, timer; export/import games as JSON.
- Progress is saved automatically in the browser/app.
