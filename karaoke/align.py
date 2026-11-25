"""Snap lyric words to the moments they are actually sung.

Online lyrics (LRC) have the right words but only a start time per line. Whisper
hears the song and gives a time for every word, but often mishears some.
This module lines the two up like a diff (Needleman-Wunsch alignment with fuzzy
word matching and a penalty for matching words that are far apart in time), then:

  * corrects a global offset (e.g. the LRC was made for a version with a longer
    intro),
  * gives every matched lyric word Whisper's start/end,
  * fills in the unmatched words between their matched neighbours.
"""

from __future__ import annotations

import difflib
import re
import statistics
from typing import Callable, Optional

from karaoke_core import Line, Word, spread_words

Log = Callable[[str], None]

GAP = -0.45          # cost of skipping a lyric word or a heard word
MISMATCH = -0.9      # cost of pairing two different words
MIN_SIM = 0.62       # how similar two words must be to count as a match


def norm(w: str) -> str:
    w = w.lower().replace("’", "'")
    w = re.sub(r"[^\w']", "", w)
    w = w.replace("'", "")
    # sung contractions: "tryin" == "trying", "em" == "them"
    w = re.sub(r"in$", "ing", w) if w.endswith("in") and len(w) > 4 else w
    return {"em": "them", "cause": "because", "cos": "because", "til": "until",
            "ya": "you", "gonna": "going", "wanna": "want"}.get(w, w)


class _Sim:
    def __init__(self):
        self.cache: dict[tuple[str, str], float] = {}

    def __call__(self, a: str, b: str) -> float:
        if a == b:
            return 1.0 if a else 0.0
        key = (a, b)
        if key not in self.cache:
            self.cache[key] = difflib.SequenceMatcher(None, a, b).ratio()
        return self.cache[key]


def _align(lyr: list[tuple[str, float, float]], heard: list[tuple[str, float]],
           slack: float, per_sec: float, hard: Optional[float]) -> list[tuple[int, int, float]]:
    """Global alignment. lyr = (norm word, window start, window end),
    heard = (norm word, time). Returns matched (lyric idx, heard idx, similarity)."""
    sim = _Sim()
    n, m = len(lyr), len(heard)
    NEG = float("-inf")
    prev = [j * GAP for j in range(m + 1)]
    back = [bytearray(m + 1) for _ in range(n + 1)]  # 0 diag, 1 up, 2 left
    for j in range(1, m + 1):
        back[0][j] = 2
    for i in range(1, n + 1):
        word, w0, w1 = lyr[i - 1]
        cur = [NEG] * (m + 1)
        cur[0] = i * GAP
        back[i][0] = 1
        row = back[i]
        for j in range(1, m + 1):
            hw, ht = heard[j - 1]
            # distance in seconds from the window where this lyric word should be
            d = (w0 - slack - ht) if ht < w0 - slack else (ht - w1 - slack) if ht > w1 + slack else 0.0
            if hard is not None and d > hard:
                s = MISMATCH * 2
            else:
                sv = sim(word, hw)
                s = (2.0 * sv if sv >= MIN_SIM else MISMATCH) - per_sec * d
            best, arrow = prev[j - 1] + s, 0
            up = prev[j] + GAP
            if up > best:
                best, arrow = up, 1
            left = cur[j - 1] + GAP
            if left > best:
                best, arrow = left, 2
            cur[j] = best
            row[j] = arrow
        prev = cur

    pairs = []
    i, j = n, m
    while i > 0 and j > 0:
        a = back[i][j]
        if a == 0:
            sv = sim(lyr[i - 1][0], heard[j - 1][0])
            if sv >= MIN_SIM:
                d_ok = True
                if hard is not None:
                    _, w0, w1 = lyr[i - 1]
                    ht = heard[j - 1][1]
                    d_ok = w0 - slack - hard <= ht <= w1 + slack + hard
                if d_ok:
                    pairs.append((i - 1, j - 1, sv))
            i, j = i - 1, j - 1
        elif a == 1:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


def shift_lines(lines: list[Line], dt: float) -> list[Line]:
    return [Line(l.start + dt, l.end + dt,
                 [Word(w.text, w.start + dt, w.end + dt) for w in l.words],
                 l.word_timed)
            for l in lines]


def align_lines(lines: list[Line], heard_words: list[Word], log: Log = print) -> list[Line]:
    """Return new lines whose word timings follow `heard_words` where possible."""
    flat = [(li, wi) for li, l in enumerate(lines) for wi in range(len(l.words))]
    heard = [(norm(w.text), w.start) for w in heard_words]
    if not flat or not heard:
        return lines

    def lyric_seq(ls: list[Line]):
        # window = when this word could plausibly be sung: its line's span
        out = []
        for li, wi in flat:
            l = ls[li]
            out.append((norm(l.words[wi].text), l.start, max(l.end, l.start + 0.5)))
        return out

    # Pass 1: loose time constraint -> find a global offset
    pairs = _align(lyric_seq(lines), heard, slack=2.0, per_sec=0.05, hard=None)
    diffs = []
    for k, j, _ in pairs:
        li, wi = flat[k]
        diffs.append(heard_words[j].start - lines[li].words[wi].start)
    offset = 0.0
    if len(diffs) >= 8:
        med = statistics.median(diffs)
        close = sum(abs(d - med) < 1.0 for d in diffs) / len(diffs)
        if abs(med) > 0.3 and close > 0.4:
            offset = med
            log(f"Lyrics were {abs(med):.2f}s {'early' if med > 0 else 'late'} "
                "for this recording - corrected.")
    work = shift_lines(lines, offset) if offset else lines

    # Pass 2: tight time constraint around the (corrected) line times
    pairs = _align(lyric_seq(work), heard, slack=1.0, per_sec=0.35, hard=4.0)
    anchor: dict[tuple[int, int], Word] = {}
    for k, j, _ in pairs:
        anchor[flat[k]] = heard_words[j]
    total = len(flat)
    log(f"Matched {len(anchor)} of {total} lyric words to the singing "
        f"({100 * len(anchor) // max(total, 1)}%).")

    out: list[Line] = []
    for li, line in enumerate(work):
        nxt_start = work[li + 1].start if li + 1 < len(work) else line.end + 5
        out.append(_retime_line(line, li, anchor, nxt_start))
    return _tidy(out)


def _retime_line(line: Line, li: int, anchor: dict, nxt_start: float) -> Line:
    words = line.words
    idx = [wi for wi in range(len(words)) if (li, wi) in anchor]
    if not idx:
        return line  # nothing heard: keep the LRC estimate
    times: list[Optional[tuple[float, float]]] = [None] * len(words)
    for wi in idx:
        h = anchor[(li, wi)]
        times[wi] = (h.start, min(h.end, h.start + 4.0))

    def fill(a: int, b: int, t0: float, t1: float):
        """Spread words[a:b] across t0..t1."""
        if b <= a:
            return
        t1 = max(t1, t0 + 0.12 * (b - a))
        for k, w in enumerate(spread_words([w.text for w in words[a:b]], t0, t1)):
            times[a + k] = (w.start, w.end)

    first, last = idx[0], idx[-1]
    # words before the first heard word
    if first > 0:
        ta = times[first][0]
        t0 = max(min(line.start, ta - 0.25 * first), ta - 0.6 * first)
        fill(0, first, t0, ta)
    # words after the last heard word
    if last < len(words) - 1:
        k = len(words) - 1 - last
        tb = times[last][1]
        t1 = min(max(line.end, tb + 0.3 * k), tb + 0.6 * k, nxt_start - 0.05)
        fill(last + 1, len(words), tb, t1)
    # gaps between heard words
    for a, b in zip(idx, idx[1:]):
        if b - a > 1:
            fill(a + 1, b, times[a][1], times[b][0])

    new = [Word(w.text, s, e) for w, (s, e) in zip(words, times)]
    return Line(new[0].start, new[-1].end, new, word_timed=True)


def _tidy(lines: list[Line]) -> list[Line]:
    """Make all word times increasing and non-overlapping."""
    t = 0.0
    for li, line in enumerate(lines):
        for k, w in enumerate(line.words):
            w.start = max(w.start, t)
            w.end = max(w.end, w.start + 0.05)
            if k + 1 < len(line.words):
                w.end = min(w.end, max(line.words[k + 1].start, w.start + 0.05))
            t = w.start + 0.01
        line.start = line.words[0].start
        line.end = line.words[-1].end
        if li + 1 < len(lines):
            nxt = lines[li + 1].words[0].start
            if line.end > nxt:
                line.end = max(nxt, line.start + 0.1)
                line.words[-1].end = min(line.words[-1].end, max(nxt, line.words[-1].start + 0.05))
    return lines
