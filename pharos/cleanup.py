"""Cleanup of extracted text: ligatures, end-of-line hyphenation, spaces."""
from __future__ import annotations

import re
import unicodedata

LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
}

# After "Ein-" + "und Ausgang" the parts must not be joined: the next word is a conjunction.
_CONJUNCTIONS = {"und", "oder", "bzw", "sowie", "y", "e", "o", "u", "and", "or", "et", "ou", "ni"}
_RE_SPACES = re.compile(r"[ \t   ]+")
_RE_FIRST_WORD = re.compile(r"[^\W\d_]+")


def clean_text(t: str) -> str:
    t = unicodedata.normalize("NFC", t)
    for k, v in LIGATURES.items():
        t = t.replace(k, v)
    t = t.replace("­", "")  # soft hyphen
    t = _RE_SPACES.sub(" ", t)
    return t.strip()


def _hyphenated(previous: str, nxt: str) -> bool:
    if not previous.endswith(("-", "‐", "‑")):
        return False
    if len(previous) < 2 or not previous[-2].isalpha():
        return False
    m = _RE_FIRST_WORD.match(nxt)
    if not m:
        return False
    word = m.group(0)
    return word[0].islower() and word.lower() not in _CONJUNCTIONS


def join_lines(lines: list[str]) -> str:
    """Joins the lines of a paragraph and undoes end-of-line hyphenation."""
    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not out:
            out = line
        elif _hyphenated(out, line):
            out = out[:-1] + line
        else:
            out += " " + line
    return clean_text(out)
