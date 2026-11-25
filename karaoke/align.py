"""Snap lyric words to the moments they are actually sung.

Online lyrics (LRC) have the right words but only a start time per line. Whisper
hears the song and gives a time for every word, but often mishears some.
This module lines the two up like a diff (Needleman-Wunsch alignment with fuzzy
word matching and a penalty for matching words that are far apart in time), then:

  * corrects a global offset (e.g. the LRC was made for a version with a longer
    intro),
  * gives every matched lyric word Whisper's start/end,
  * fills in the unmatched words between their matched neighbours.

It also handles one lyric word heard as two ("Sonnenschein" / "Sonnen schein")
and two lyric words heard as one ("gonna" / "going to"), and folds umlauts and
accents so "für" == "fuer".
"""

from __future__ import annotations

import difflib
import re
import statistics
import unicodedata
from typing import Callable, Optional

from karaoke_core import Line, Word, spread_words

Log = Callable[[str], None]

GAP = -0.45          # cost of skipping a lyric word or a heard word
MISMATCH = -0.9      # cost of pairing two different words
MERGE = -0.3         # extra cost for a 2:1 / 1:2 match
MIN_SIM = 0.62       # how similar two words must be to count as a match

FOLD = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "æ": "ae",
                      "ø": "o", "œ": "oe", "’": "'", "‘": "'", "`": "'"})
EN_SLANG = {"em": "them", "cause": "because", "cos": "because", "til": "until",
            "ya": "you", "gonna": "going", "wanna": "want", "gotta": "got"}


def norm(w: str, lang: Optional[str] = None) -> str:
    w = w.casefold().translate(FOLD)
    w = "".join(c for c in unicodedata.normalize("NFKD", w)
                if not unicodedata.combining(c))
    w = re.sub(r"[^\w]", "", w)
    if lang in (None, "en"):
        # sung contractions: "tryin'" == "trying", "em" == "them"
        if w.endswith("in") and len(w) > 4:
            w += "g"
        w = EN_SLANG.get(w, w)
    return w


# ---------------------------------------------------------------- language guess

STOPWORDS = {
    "en": "the and you i to a my me it in is that of your we be for on all so this love",
    "de": "ich und die der du nicht das ist es in mir mich zu ein sie wir den mit auf "
          "was dich so wie dann nur noch bin mein kein",
    "es": "que de la el y no me te en mi tu lo es un una por yo se con como amor",
    "fr": "je le la de et tu les pas que un une moi est des pour mon dans ne qui toi",
    "it": "di che e il la non mi ti un per una sei io è come ma sono tu amore",
    "pt": "que de e o a não eu me te um uma você meu minha com é se do da",
    "nl": "ik je de het een en niet is van dat in mijn wat jij me op ze",
    "sv": "jag och du det att en är inte på som mig med har vi min",
    "pl": "nie i się w to na że mnie ja jak ty mi co jest",
    "tr": "bir ve bu ben sen ne da de mi için gibi çok beni seni",
}
STOPWORDS = {k: set(v.split()) for k, v in STOPWORDS.items()}


def guess_language(text: str) -> Optional[str]:
    words = re.findall(r"[^\W\d_]+", text.casefold())
    if len(words) < 15:
        return None
    scores = {lang: sum(w in sw for w in words) for lang, sw in STOPWORDS.items()}
    best = max(scores, key=scores.get)
    ranked = sorted(scores.values(), reverse=True)
    if ranked[0] >= max(5, 0.08 * len(words)) and ranked[0] > 1.3 * ranked[1]:
        return best
    return None


def lyric_hints(lines: list[Line], max_chars: int = 500) -> str:
    """Distinct lyric words in order: hints that steer Whisper towards the real
    lyrics (applied to every 30 s window it listens to)."""
    seen, out, n = set(), [], 0
    for l in lines:
        for w in l.words:
            t = re.sub(r"^[^\w]+|[^\w']+$", "", w.text)
            k = t.casefold()
            if len(k) < 2 or k in seen:
                continue
            seen.add(k)
            out.append(t)
            n += len(t) + 1
            if n > max_chars:
                return " ".join(out)
    return " ".join(out)


# --------------------------------------------------------------------- alignment


class _Sim:
    def __init__(self):
        self.cache: dict[tuple[str, str], float] = {}

    def __call__(self, a: str, b: str) -> float:
        if a == b:
            return 1.0 if a else 0.0
        la, lb = len(a), len(b)
        if not la or not lb or min(la, lb) * 2.2 < max(la, lb):
            return 0.0  # too different in length to ever be a match
        key = (a, b)
        v = self.cache.get(key)
        if v is None:
            v = self.cache[key] = difflib.SequenceMatcher(None, a, b).ratio()
        return v


def _align(lyr: list[tuple[str, float, float]], heard: list[tuple[str, float, float]],
           slack: float, per_sec: float, hard: Optional[float]
           ) -> dict[int, tuple[float, float]]:
    """Global alignment. lyr = (norm word, window start, window end),
    heard = (norm word, start, end). Returns {lyric index: (start, end)}."""
    sim = _Sim()
    n, m = len(lyr), len(heard)
    NEG = float("-inf")

    def dist(w0, w1, ht):
        if ht < w0 - slack:
            return w0 - slack - ht
        if ht > w1 + slack:
            return ht - w1 - slack
        return 0.0

    rows = [[j * GAP for j in range(m + 1)]]
    back = [bytearray([2] * (m + 1))]   # 0 1:1, 1 skip lyric, 2 skip heard,
    back[0][0] = 0                      # 3 lyric<->2 heard, 4 2 lyric<->heard
    for i in range(1, n + 1):
        word, w0, w1 = lyr[i - 1]
        prev = rows[i - 1]
        prev2 = rows[i - 2] if i >= 2 else None
        cur = [NEG] * (m + 1)
        cur[0] = i * GAP
        row = bytearray(m + 1)
        row[0] = 1
        for j in range(1, m + 1):
            hw, ht, _ = heard[j - 1]
            d = dist(w0, w1, ht)
            if hard is not None and d > hard:
                s = MISMATCH * 2
                far = True
            else:
                sv = sim(word, hw)
                s = (2.0 * sv if sv >= MIN_SIM else MISMATCH) - per_sec * d
                far = False
            best, arrow = prev[j - 1] + s, 0
            v = prev[j] + GAP
            if v > best:
                best, arrow = v, 1
            v = cur[j - 1] + GAP
            if v > best:
                best, arrow = v, 2
            if not far and j >= 2:  # one lyric word heard as two
                sv = sim(word, heard[j - 2][0] + hw)
                if sv >= MIN_SIM + 0.08:
                    v = prev[j - 2] + 2.0 * sv + MERGE - per_sec * dist(w0, w1, heard[j - 2][1])
                    if v > best:
                        best, arrow = v, 3
            if not far and prev2 is not None:  # two lyric words heard as one
                pw, pw0, _ = lyr[i - 2]
                sv = sim(pw + word, hw)
                if sv >= MIN_SIM + 0.08:
                    v = prev2[j - 1] + 2.0 * sv + MERGE - per_sec * dist(pw0, w1, ht)
                    if v > best:
                        best, arrow = v, 4
            cur[j] = best
            row[j] = arrow
        rows.append(cur)
        back.append(row)
        if i >= 3:
            rows[i - 3] = None  # free memory; only two rows back are needed

    def ok(k, ht):
        if hard is None:
            return True
        _, w0, w1 = lyr[k]
        return w0 - slack - hard <= ht <= w1 + slack + hard

    spans: dict[int, tuple[float, float]] = {}
    i, j = n, m
    while i > 0 and j > 0:
        a = back[i][j]
        if a == 0:
            hw, hs, he = heard[j - 1]
            if sim(lyr[i - 1][0], hw) >= MIN_SIM and ok(i - 1, hs):
                spans[i - 1] = (hs, he)
            i, j = i - 1, j - 1
        elif a == 1:
            i -= 1
        elif a == 2:
            j -= 1
        elif a == 3:
            spans[i - 1] = (heard[j - 2][1], heard[j - 1][2])
            i, j = i - 1, j - 2
        else:  # a == 4: split the heard word's time by letter count
            _, hs, he = heard[j - 1]
            l1, l2 = len(lyr[i - 2][0]) or 1, len(lyr[i - 1][0]) or 1
            mid = hs + (he - hs) * l1 / (l1 + l2)
            spans[i - 2] = (hs, mid)
            spans[i - 1] = (mid, he)
            i, j = i - 2, j - 1
    return spans


def shift_lines(lines: list[Line], dt: float) -> list[Line]:
    return [Line(l.start + dt, l.end + dt,
                 [Word(w.text, w.start + dt, w.end + dt) for w in l.words],
                 l.word_timed)
            for l in lines]


def align_lines(lines: list[Line], heard_words: list[Word], log: Log = print,
                lang: Optional[str] = None, stats: Optional[dict] = None) -> list[Line]:
    """Return new lines whose word timings follow `heard_words` where possible.
    If `stats` is given, stats["matched"] is set to the fraction of lyric words
    that were heard."""
    flat = [(li, wi) for li, l in enumerate(lines) for wi in range(len(l.words))]
    heard = [(norm(w.text, lang), w.start, w.end) for w in heard_words]
    heard = [h for h in heard if h[0]]
    if not flat or not heard:
        return lines

    def lyric_seq(ls: list[Line]):
        # window = when this word could plausibly be sung: its line's span
        out = []
        for li, wi in flat:
            l = ls[li]
            out.append((norm(l.words[wi].text, lang), l.start, max(l.end, l.start + 0.5)))
        return out

    # Pass 1: loose time constraint -> find a global offset
    spans = _align(lyric_seq(lines), heard, slack=2.0, per_sec=0.05, hard=None)
    diffs = []
    for k, (hs, _) in spans.items():
        li, wi = flat[k]
        diffs.append(hs - lines[li].words[wi].start)
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
    spans = _align(lyric_seq(work), heard, slack=1.0, per_sec=0.35, hard=4.0)
    anchor = {flat[k]: v for k, v in spans.items()}
    total = len(flat)
    pct = 100 * len(anchor) // max(total, 1)
    log(f"Matched {len(anchor)} of {total} lyric words to the singing ({pct}%).")
    if stats is not None:
        stats["matched"] = len(anchor) / max(total, 1)

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
        s, e = anchor[(li, wi)]
        times[wi] = (s, min(max(e, s + 0.05), s + 4.0))

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
                line.words[-1].end = min(line.words[-1].end,
                                         max(nxt, line.words[-1].start + 0.05))
    return lines
