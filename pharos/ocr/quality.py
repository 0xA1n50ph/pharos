"""OCR quality per page and correction suggestions.

Score: Tesseract's mean confidence, discounted by the fraction of doubtful words and by implausible-looking
text. The suggestions use only the book itself (no dictionary): a rare word that differs by one letter from a
frequent word in the same book, or that becomes a frequent word by swapping typical OCR confusions (rn/m, cl/d...).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from ..extractors.pdf_text import text_quality
from .tesseract import mean_confidence

WORD_THRESHOLD = 70   # confidence below this = doubtful word
PAGE_THRESHOLD = 85   # score below this = flagged page

_RE_TOKEN = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)
_CONFUSIONS = [("rn", "m"), ("m", "rn"), ("ii", "n"), ("n", "ii"), ("li", "h"), ("h", "li"), ("cl", "d"), ("d", "cl"),
               ("vv", "w"), ("w", "vv"), ("l", "i"), ("i", "l"), ("c", "e"), ("e", "c"), ("ſ", "f"), ("f", "s"),
               ("s", "f"), ("u", "n"), ("n", "u")]
_MAX_CONFUSION_CONF = 90  # a word Tesseract read with more confidence than this does not enter the suggestions


def score_page(page: dict, word_threshold: int = WORD_THRESHOLD, page_threshold: int = PAGE_THRESHOLD) -> dict:
    words = page["words"]
    n = len(words)
    base = {"n": page["n"], "words": n, "ink": page.get("ink", 0.0)}
    if n < 8:
        mute = page.get("ink", 0.0) < 0.012
        return {**base, "conf": mean_confidence(words), "doubtful": 0, "doubtful_fraction": 0.0,
                "plausible": 1.0, "score": 100.0 if mute else 0.0, "flagged": not mute,
                "reasons": [] if mute else ["almost no text recognised, but the page has an image or ink"]}
    letters = sum(len(p[8]) for p in words) or 1
    doubtful = [p for p in words if p[7] < word_threshold and any(c.isalnum() for c in p[8])]
    frac = sum(len(p[8]) for p in doubtful) / letters
    mean = mean_confidence(words)
    plausible = text_quality(" ".join(p[8] for p in words))
    score = round(max(0.0, mean - 30 * (1 - plausible) - 30 * frac), 1)
    reasons = []
    if mean < page_threshold:
        reasons.append(f"mean confidence {mean:.0f}%")
    if frac >= 0.08:
        reasons.append(f"{len(doubtful)} doubtful words ({frac:.0%} of the text)")
    if plausible < 0.9:
        reasons.append("many passages that look like noise")
    return {**base, "conf": round(mean, 1), "doubtful": len(doubtful), "doubtful_fraction": round(frac, 3),
            "plausible": round(plausible, 3), "score": score, "flagged": score < page_threshold or bool(reasons),
            "reasons": reasons}


def score_all(pages: dict[int, dict], word_threshold: int = WORD_THRESHOLD,
              page_threshold: int = PAGE_THRESHOLD) -> dict[int, dict]:
    return {n: score_page(p, word_threshold, page_threshold) for n, p in sorted(pages.items())}


def summarize(scores: dict[int, dict]) -> dict:
    if not scores:
        return {"pages": 0, "flagged": 0, "mean": 0.0, "worst": [], "overall_bad": False}
    flagged = [n for n, p in scores.items() if p["flagged"]]
    with_text = [p for p in scores.values() if p["words"] >= 8]
    mean = sum(p["conf"] for p in with_text) / len(with_text) if with_text else 0.0
    worst = sorted(scores.values(), key=lambda p: p["score"])[:5]
    return {
        "pages": len(scores), "flagged": len(flagged), "flagged_list": flagged, "mean": round(mean, 1),
        "worst": [(p["n"], p["score"]) for p in worst],
        "overall_bad": len(flagged) > 0.5 * len(scores) and len(scores) >= 4,
    }


# ----------------------------------------------------------------------------- suggestions

def _match_case(original: str, candidate: str) -> str:
    if original.isupper() and len(original) > 1:
        return candidate.upper()
    if original[:1].isupper() and not candidate[:1].isupper():
        return candidate[:1].upper() + candidate[1:]
    if original[:1].islower() and candidate[:1].isupper():
        return candidate[:1].lower() + candidate[1:]
    return candidate


def generate_suggestions(pages: dict[int, dict], word_threshold: int = 80, maximum: int = 300) -> list[dict]:
    freq: Counter = Counter()
    forms: dict[str, Counter] = defaultdict(Counter)
    where: dict[str, list[int]] = defaultdict(list)
    confs: dict[str, list[float]] = defaultdict(list)
    for n, page in pages.items():
        for p in page["words"]:
            for tok in _RE_TOKEN.findall(p[8]):
                if len(tok) < 4:
                    continue
                k = tok.lower()
                freq[k] += 1
                forms[k][tok] += 1
                where[k].append(n)
                confs[k].append(p[7])
    frequent = {k for k, c in freq.items() if c >= 3}
    index: dict[str, set[str]] = defaultdict(set)
    for k in frequent:
        index[k].add(k)
        for i in range(len(k)):
            index[k[:i] + k[i + 1:]].add(k)

    suggestions = []
    for k, c in freq.items():
        if c > 2 or k in frequent:
            continue
        conf_min = min(confs[k])
        if conf_min >= _MAX_CONFUSION_CONF:
            continue
        doubtful = conf_min < word_threshold
        neighbours = set(index.get(k, ())) | {t for i in range(len(k)) for t in index.get(k[:i] + k[i + 1:], ())}
        neighbours.discard(k)
        by_confusion: set[str] = set()
        for a, b in _CONFUSIONS:
            start = 0
            while (i := k.find(a, start)) != -1:
                t = k[:i] + b + k[i + len(a):]
                if t in frequent and t != k:
                    by_confusion.add(t)
                start = i + 1
        candidates = (neighbours if doubtful else set()) | by_confusion
        if not candidates:
            continue
        best = max(candidates, key=lambda t: freq[t])
        if freq[best] < max(3, 3 * c):
            continue
        original = forms[k].most_common(1)[0][0]
        to_ = _match_case(original, forms[best].most_common(1)[0][0])
        suggestions.append({"from": original, "to": to_, "n": c, "pages": sorted(set(where[k]))[:12],
                            "conf": round(min(confs[k]), 1), "freq_to": freq[best]})
    suggestions.sort(key=lambda s: (-s["n"], s["conf"]))
    return suggestions[:maximum]
