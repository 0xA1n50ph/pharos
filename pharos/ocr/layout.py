"""From OCR (words with boxes) to internal-format blocks.

Stages: words -> lines -> removal of headers/footers/page numbers (comparing pages) ->
body size and spacing per page -> classification (title, note, body) -> grouping into paragraphs
(indent, spacing, end of line) -> verse. Merging paragraphs across pages is left to the assembly.

The thresholds use the text size of the page itself (not fixed values), so they work at any resolution.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher

from ..extractors.common import RE_CHAPTER, is_separator, looks_like_verse
from ..cleanup import join_lines, clean_text

_END_PUNCTUATION = ".!?…:;»\"”’)]*"
_RE_NOTE_START = re.compile(r"^(\d{1,3}|[*†‡¹²³⁴⁵⁶⁷⁸⁹⁰])[\.\)]?\s")
_MARGIN_ZONE = 0.09
_SWAPS = str.maketrans({"ſ": "s"})

# A line's height varies 15-20% depending on whether it has ascenders/descenders; that's why "body" is a high
# percentile of the heights and only large differences count as title (bigger) or note (smaller).
_TITLE_RATIO = 1.3
_NOTE_RATIO = 0.8
_DEFAULT_STEP_RATIO = 1.75  # line spacing / text height, when the page has no lines to measure


def clean_ocr(t: str) -> str:
    return clean_text(t.translate(_SWAPS))


def ends_sentence(t: str) -> bool:
    return t.rstrip()[-1:] in _END_PUNCTUATION


def looks_like_page_number(t: str) -> bool:
    s = re.sub(r"[\s\-–—\(\)\[\]\.\|•·*]", "", t)
    if not s or len(s) > 7:
        return False
    if s.isdigit():
        return len(s) <= 4
    if re.fullmatch(r"[ivxlcdm]+", s, re.IGNORECASE):
        return True
    return bool(re.fullmatch(r"[0-9OolI]{1,4}", s)) and any(c.isdigit() for c in s)


@dataclass
class Line:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    conf: float
    block: int
    par: int
    cat: str = "body"


def _weighted_percentile(pairs: list[tuple[float, float]], p: float, default: float) -> float:
    if not pairs:
        return default
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= p * total:
            return v
    return pairs[-1][0]


def _percentile(values: list[float], p: float) -> float:
    v = sorted(values)
    if not v:
        return 0.0
    return float(v[min(len(v) - 1, max(0, round(p * (len(v) - 1))))])


# ----------------------------------------------------------------------------- words -> lines

def _line_size(words: list) -> float:
    """Type size: height of the tallest word in the line (the one with an ascender and a descender).

    Each word's height depends on the letters it has (x-height, ascender, descender), so low percentiles vary
    a lot from line to line; the maximum is stable. Giant words from OCR glitches are left out."""
    heights = sorted(w[6] for w in words if sum(c.isalpha() for c in w[8]) >= 3) or sorted(w[6] for w in words)
    median = statistics.median(heights)
    valid = [h for h in heights if h <= 1.6 * median] or heights
    return float(valid[-1])


def page_lines(page: dict) -> list[Line]:
    groups: dict[tuple, list] = {}
    for p in page["words"]:
        groups.setdefault((p[0], p[1], p[2]), []).append(p)
    lines = []
    for (b, par, _), ws in groups.items():
        ws.sort(key=lambda w: w[3])
        text = clean_ocr(" ".join(w[8] for w in ws))
        if not text or (not any(c.isalnum() for c in text) and not is_separator(text)):
            continue
        # vertical position by the centre of the words: unaffected by ascenders/descenders or residual skew
        size = _line_size(ws)
        cy = statistics.median(w[4] + w[6] / 2 for w in ws)
        lines.append(Line(
            text=text,
            x0=min(w[3] for w in ws), x1=max(w[3] + w[5] for w in ws),
            top=cy - size / 2, bottom=cy + size / 2, size=size,
            conf=sum(w[7] for w in ws) / len(ws),
            block=b, par=par,
        ))
    return lines


# ----------------------------------------------------------------------------- headers and footers

def _norm(t: str) -> str:
    return re.sub(r"[^a-z#]", "", re.sub(r"\d+", "#", t.lower()))


def remove_headers(pages: dict[int, tuple[int, int, list[Line]]]) -> tuple[dict, list[str]]:
    """Removes page numbers from the margins and lines that repeat across several pages (tolerates OCR errors)."""
    reps: list[list] = []  # [norm, {pages}, example]
    found: dict[tuple[int, int], int] = {}  # (page, line id) -> group index
    for n, (_, h, lines) in pages.items():
        for l in lines:
            if not (l.top < _MARGIN_ZONE * h or l.bottom > (1 - _MARGIN_ZONE) * h):
                continue
            norm = _norm(l.text)
            if len(norm) < 3:
                continue
            for i, r in enumerate(reps):
                if r[0] == norm or SequenceMatcher(None, r[0], norm).ratio() >= 0.82:
                    r[1].add(n)
                    found[(n, id(l))] = i
                    break
            else:
                reps.append([norm, {n}, l.text])
                found[(n, id(l))] = len(reps) - 1
    limit = max(3, int(0.25 * len(pages)))
    repeated = {i for i, r in enumerate(reps) if len(r[1]) >= limit}

    removed: list[str] = []
    out = {}
    for n, (w, h, lines) in pages.items():
        kept = []
        for l in lines:
            in_margin = l.top < _MARGIN_ZONE * h or l.bottom > (1 - _MARGIN_ZONE) * h
            if in_margin and found.get((n, id(l))) in repeated:
                removed.append(reps[found[(n, id(l))]][2])
                continue
            if in_margin and looks_like_page_number(l.text):
                removed.append("(page numbers)")
                continue
            if in_margin and l.conf < 35 and len(l.text) <= 3:  # scan-edge junk
                continue
            kept.append(l)
        out[n] = (w, h, kept)
    examples: list[str] = []
    for t in removed:
        if t not in examples:
            examples.append(t)
    return out, examples


# ----------------------------------------------------------------------------- page context

class _Context:
    """Body size, spacing and margins of a page."""

    def __init__(self, lines: list[Line], width: int, height: int, default_body: float):
        self.width, self.height = width, height
        eligible = [(l.size, len(l.text)) for l in lines if len(l.text) >= 8]
        self.body = _weighted_percentile(eligible, 0.7, default_body) if len(eligible) >= 6 else default_body
        deltas = [b.top - a.top for a, b in zip(lines, lines[1:])
                  if a.block == b.block and 0.8 * self.body < b.top - a.top < 3.2 * self.body]
        self.step_measured = statistics.median(deltas) if len(deltas) >= 4 else None
        self.step = self.step_measured or _DEFAULT_STEP_RATIO * self.body
        self.slack = self._measure_slack(lines)
        by_block: dict[int, list[Line]] = defaultdict(list)
        for l in lines:
            by_block[l.block].append(l)
        self.block_ref: dict[int, tuple[float, float, float, float]] = {}
        for b, ls in by_block.items():
            if len(ls) >= 3:
                self.block_ref[b] = (
                    min(l.x0 for l in ls), max(l.x1 for l in ls),
                    statistics.median(l.x0 for l in ls), _percentile([l.x1 for l in ls], 0.85),
                )

    def _measure_slack(self, lines: list[Line]) -> float:
        """How far a line may fall short of the right margin and still count as 'full'.

        Justified text: little (1.4 × body). Text with a ragged right margin: the normal variation of line ends
        in the middle of sentences, otherwise every sentence ending near the line end would look like a paragraph end."""
        min_ = 1.4 * self.body
        base = [l for l in lines if len(l.text) >= 8]
        if len(base) < 6:
            return min_
        wide = _percentile([l.x1 for l in base], 0.85) - _percentile([l.x0 for l in base], 0.10)
        middle = [l.x1 for l in lines
                  if len(l.text.split()) >= 4 and not ends_sentence(l.text) and (l.x1 - l.x0) >= 0.55 * wide]
        if len(middle) < 6:
            return min_
        return max(min_, _percentile(middle, 0.85) - _percentile(middle, 0.10))

    def _ref_or_none(self, l: Line) -> tuple[float, float] | None:
        if l.block in self.block_ref:
            _, _, left, right = self.block_ref[l.block]
            return left, right
        best, overlap = None, 0.0
        for a, b, left, right in self.block_ref.values():
            o = min(b, l.x1) - max(a, l.x0)
            if o > overlap:
                overlap, best = o, (left, right)
        if best is None or overlap < 0.5 * (l.x1 - l.x0):
            return None
        return best

    def ref(self, l: Line) -> tuple[float, float]:
        """(left margin, right margin) typical of the column the line is in."""
        return self._ref_or_none(l) or (l.x0, l.x1)

    def column(self, l: Line) -> tuple[float, float]:
        """Like ref, but with no reference it uses the page (to test title centring)."""
        return self._ref_or_none(l) or (0.12 * self.width, 0.88 * self.width)

    def indent(self, l: Line) -> float:
        return l.x0 - self.ref(l)[0]


# ----------------------------------------------------------------------------- classification

def _isolated(lines: list[Line], i: int, body: float) -> bool:
    before = lines[i].top - lines[i - 1].bottom if i > 0 else 9 * body
    after = lines[i + 1].top - lines[i].bottom if i + 1 < len(lines) else 9 * body
    return before > 1.0 * body and after > 1.0 * body


def _classify_page(lines: list[Line], ctx: _Context) -> None:
    body = ctx.body
    for l in lines:
        if len(l.text) < 120 and l.size >= _TITLE_RATIO * body:
            l.cat = "title"
    by_block: dict[int, list[Line]] = defaultdict(list)
    for l in lines:
        by_block[l.block].append(l)
    for ls in by_block.values():  # footnotes: whole block smaller than the body, in the lower part
        c = [l for l in ls if l.cat == "body"]
        if (c and statistics.median(l.size for l in c) <= _NOTE_RATIO * body
                and statistics.median(len(l.text.split()) for l in c) >= 4
                and min(l.top for l in c) > 0.45 * ctx.height):
            for l in c:
                l.cat = "note"
    for i, l in enumerate(lines):
        if l.cat != "body" or len(l.text) > 60:
            continue
        if RE_CHAPTER.match(l.text) and len(l.text.split()) <= 8 and _isolated(lines, i, body):
            l.cat = "title"  # "Chapter 3", "KAPITEL II"...
            continue
        if l.text[-1:] in ",;:":
            continue
        left, right = ctx.column(l)  # minor title: short, centred in the column and isolated by whitespace
        if (abs((left + right) / 2 - (l.x0 + l.x1) / 2) <= 0.04 * ctx.width
                and (l.x1 - l.x0) <= 0.75 * (right - left)
                and sum(c.isalpha() for c in l.text) >= 3 and _isolated(lines, i, body)):
            l.cat = "minor_title"


def _new_paragraph(prev: Line, l: Line, ctx: _Context) -> bool:
    body = ctx.body
    _, right_prev = ctx.ref(prev)
    indent = ctx.indent(l)
    full = prev.x1 >= right_prev - ctx.slack
    ends = ends_sentence(prev.text)
    dy = l.top - prev.top
    if dy > 1.35 * ctx.step:
        return True
    if indent > 0.9 * body:
        # consecutive lines with the same indent (verse, quotation) form a single block
        if ctx.indent(prev) > 0.9 * body and abs(l.x0 - prev.x0) < 0.6 * body and 0 < dy:
            return False
        return True
    if l.block != prev.block:
        return not (full and not ends)
    return (not full) and ends


def _group(lines: list[Line], ctx: _Context) -> list[list[Line]]:
    groups: list[list[Line]] = []
    for l in lines:
        if not groups:
            groups.append([l])
            continue
        prev = groups[-1][-1]
        if l.cat != prev.cat or l.cat == "minor_title":
            new_group = True
        elif l.cat == "title":
            new_group = abs(l.size - prev.size) > 0.12 * prev.size or (l.top - prev.bottom) > 0.8 * max(l.size, prev.size)
        elif l.cat == "note":
            new_group = bool(_RE_NOTE_START.match(l.text)) and ends_sentence(prev.text)
        else:
            new_group = _new_paragraph(prev, l, ctx)
        if new_group:
            groups.append([l])
        else:
            groups[-1].append(l)
    return groups


# ----------------------------------------------------------------------------- document

def _title_levels(ratios: list[float]) -> list[float]:
    """Centres of the size/body ratio groups (20% tolerance), from largest to smallest."""
    centers: list[float] = []
    for r in sorted(ratios, reverse=True):
        if centers and r >= centers[-1] * 0.8:
            continue
        centers.append(r)
    return centers


def analyze_document(pages: dict[int, dict]) -> tuple[dict[int, list[dict]], dict]:
    """pages: {n: OCR JSON}. Returns ({n: [blocks]}, report)."""
    lines_by_page = {n: (p["width"], p["height"], page_lines(p)) for n, p in sorted(pages.items())}
    all_ = [(l.size, len(l.text)) for _, _, ls in lines_by_page.values() for l in ls if len(l.text) >= 8]
    default_body = _weighted_percentile(all_, 0.7, 30.0)

    lines_by_page, removed = remove_headers(lines_by_page)

    ctxs = {n: _Context(ls, w, h, default_body) for n, (w, h, ls) in lines_by_page.items()}
    step_ratios = [c.step_measured / c.body for c in ctxs.values() if c.step_measured and c.body]
    if step_ratios:  # pages without enough lines inherit the book's typical ratio
        ratio = statistics.median(step_ratios)
        for c in ctxs.values():
            if not c.step_measured:
                c.step = ratio * c.body
    for n, (_, _, ls) in lines_by_page.items():
        _classify_page(ls, ctxs[n])

    ratios = [l.size / ctxs[n].body for n, (_, _, ls) in lines_by_page.items() for l in ls
              if l.cat == "title" and ctxs[n].body > 0 and l.size >= _TITLE_RATIO * ctxs[n].body]
    centers = _title_levels(ratios)

    blocks_by_page: dict[int, list[dict]] = {}
    for n, (_, _, lines) in lines_by_page.items():
        ctx = ctxs[n]
        lengths = [len(l.text) for l in lines if l.cat == "body"]
        median_chars = statistics.median(lengths) if lengths else 0
        blocks: list[dict] = []
        for g in _group(lines, ctx):
            cat, texts = g[0].cat, [l.text for l in g]
            indent = ctx.indent(g[0]) > 0.9 * ctx.body
            if cat == "title":
                r = g[0].size / ctx.body if ctx.body else 1.0
                if r < _TITLE_RATIO:  # came from the "Chapter N" rule
                    level = 1
                else:
                    level = 1 + next((i for i, c in enumerate(centers) if r >= c * 0.8), max(0, len(centers) - 1))
                b = {"kind": "title", "text": join_lines(texts), "level": min(level, 4)}
            elif cat == "minor_title":
                b = {"kind": "title", "text": join_lines(texts), "level": min(len(centers) + 1, 4)}
            elif cat == "note":
                b = {"kind": "note", "text": join_lines(texts), "level": 1}
            elif len(g) == 1 and is_separator(texts[0]):
                b = {"kind": "separator", "text": "* * *", "level": 1}
            elif looks_like_verse(texts, median_chars):
                b = {"kind": "verse", "text": "\n".join(texts), "level": 1}
            else:
                b = {"kind": "paragraph", "text": join_lines(texts), "level": 1}
            b["indent"] = indent
            if b["text"].strip():
                blocks.append(b)
        blocks_by_page[n] = blocks

    return blocks_by_page, {"headers_removed": removed[:6], "title_levels": len(centers)}
