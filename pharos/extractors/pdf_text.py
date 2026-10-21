"""PDF extractor with a text layer (pdfplumber) and PDF-type analysis.

Heuristics, in order: read lines with size/bold/position -> remove headers,
footers and page numbers -> classify (title, note, body) -> group into paragraphs
(indent and spacing) -> verse -> merge paragraphs across pages -> chapters.
Known limitations: two-column books and complex tables.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ..cleanup import join_lines, clean_text
from ..model import Block, NOTE, PARAGRAPH, SEPARATOR, TITLE, VERSE
from .common import ExtractionError, split_into_chapters, is_separator, build_book, looks_like_verse

_MARGIN_ZONE = 0.07
_RE_PAGE_NUMBER = re.compile(r"^[\-–—\s\(\[]*([0-9]{1,4}|[ivxlcdm]{1,7})[\-–—\s\)\]]*$", re.IGNORECASE)
_RE_NOTE_START = re.compile(r"^(\d{1,3}|[*†‡¹²³⁴⁵⁶⁷⁸⁹⁰])[\.\)]?\s")
_END_PUNCTUATION = ".!?…:;»\"”’)]*"
_VOWELS = set("aeiouyáéíóúàèìòùâêîôûãõäöüëïœæ")


@dataclass
class _Line:
    text: str
    size: float
    bold: bool
    x0: float
    x1: float
    top: float
    bottom: float
    page: int
    cat: str = "body"


# ----------------------------------------------------------------------------- PDF-type analysis

def text_quality(text: str) -> float:
    """0..1: proportion of plausible 'words'. Bad OCR layers fall below ~0.7."""
    tokens = text.split()
    if not tokens:
        return 0.0
    good = 0
    for t in tokens:
        p = re.sub(r"^\W+|\W+$", "", t)
        if not p or p.isdigit():
            good += 1
            continue
        letters = [c for c in p if c.isalpha()]
        if len(letters) < 0.7 * len(p):
            continue
        if len(letters) > 3 and not any(c.lower() in _VOWELS for c in letters):
            continue
        if re.search(r"(.)\1{3,}", p):
            continue
        good += 1
    return good / len(tokens)


def _image_coverage(page) -> float:
    area = float(page.width * page.height) or 1.0
    total = 0.0
    for im in page.images:
        w = max(0.0, min(im["x1"], page.width) - max(im["x0"], 0))
        h = max(0.0, min(im["bottom"], page.height) - max(im["top"], 0))
        total += w * h / area
    return min(total, 1.0)


def analyze(path: Path, max_sample: int = 40) -> dict:
    """Classifies the PDF page by page (sampling large books) and recommends the extraction method."""
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        if total == 0:
            raise ExtractionError("PDF with no pages.")
        step = max(1, total // max_sample)
        indices = list(range(0, total, step))[:max_sample]
        pages = []
        for i in indices:
            page = pdf.pages[i]
            chars = len(page.chars)
            cov = _image_coverage(page)
            quality = None
            if chars < 50 and cov < 0.5:
                kind = "empty"
            elif chars < 50:
                kind = "scan_no_text"
            elif cov >= 0.6:
                quality = text_quality(page.extract_text() or "")
                kind = "scan_ocr_good" if quality >= 0.7 else "scan_ocr_bad"
            else:
                kind = "text"
            pages.append({"page": i + 1, "chars": chars, "coverage": round(cov, 2),
                          "quality": None if quality is None else round(quality, 2), "type": kind})

    counts = Counter(p["type"] for p in pages)
    useful = sum(v for k, v in counts.items() if k != "empty")
    n_text = counts["text"] + counts["scan_ocr_good"]
    n_scan = counts["scan_no_text"] + counts["scan_ocr_bad"]
    if useful == 0:
        doc_type, recommendation = "empty", "none"
    elif n_text / useful >= 0.8:
        doc_type, recommendation = "text", "direct_extraction"
    elif n_scan / useful >= 0.8:
        doc_type, recommendation = "scan", "ocr"
    else:
        doc_type, recommendation = "mixed", "direct_extraction_and_ocr"

    sample = f"{len(pages)} of {total} pages sampled" if len(pages) < total else f"{total} pages"
    parts = [f"{counts[k]} {name}" for k, name in (
        ("text", "with text"), ("scan_ocr_good", "scanned with good OCR"),
        ("scan_ocr_bad", "scanned with poor OCR"), ("scan_no_text", "scanned without text"),
        ("empty", "with little text or illustration only")) if counts[k]]
    return {
        "total_pages": total, "sampled": len(pages), "pages": pages,
        "counts": dict(counts), "type": doc_type, "recommendation": recommendation,
        "summary": f"{sample}: " + ", ".join(parts),
    }


def classify_pages(path: Path) -> list[str]:
    """Type of ALL pages, fast (pdfium): 'text' | 'scan' | 'layer' | 'empty'.

    scan  = image covering the page and no text (needs OCR)
    layer = image covering the page with embedded text (OCR from another tool)
    empty = no text at all and no large image (blank page)
    Pages with little text (title page, dedication) count as 'text'.
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    pdf = pdfium.PdfDocument(str(path))
    out: list[str] = []
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            try:
                w, h = page.get_size()
                tp = page.get_textpage()
                try:
                    chars = tp.count_chars()
                finally:
                    tp.close()
                cov = 0.0
                for obj in page.get_objects(filter=(raw.FPDF_PAGEOBJ_IMAGE,)):
                    l, b, r, t = obj.get_bounds()
                    cov += max(0.0, min(r, w) - max(l, 0.0)) * max(0.0, min(t, h) - max(b, 0.0)) / ((w * h) or 1.0)
                cov = min(cov, 1.0)
            finally:
                page.close()
            if chars == 0 and cov < 0.5:
                out.append("empty")
            elif chars < 50 and cov >= 0.5:
                out.append("scan")
            elif cov >= 0.6:
                out.append("layer")
            else:
                out.append("text")
    finally:
        pdf.close()
    return out


# ----------------------------------------------------------------------------- extraction

def _page_lines(page, number: int) -> list[_Line]:
    try:
        raw = page.extract_text_lines(return_chars=True, strip=True)
    except Exception:  # noqa: BLE001
        return []
    lines = []
    for l in raw:
        text = clean_text(l["text"])
        chars = [c for c in l.get("chars", []) if c["text"].strip()]
        if not text or not chars:
            continue
        size = statistics.fmean(c["size"] for c in chars)
        is_bold = sum(
            1 for c in chars if re.search(r"bold|black|heavy|demi", c.get("fontname", ""), re.I)
        ) / len(chars) >= 0.6
        lines.append(_Line(text, round(size, 1), is_bold, l["x0"], l["x1"], l["top"], l["bottom"], number))
    return lines


def _norm(t: str) -> str:
    return re.sub(r"\d+", "#", t.lower()).strip()


def _remove_headers(pages: list[tuple[int, float, float, list[_Line]]]) -> list[str]:
    """Removes lines repeated in the margins and page numbers; returns what was removed."""
    counts: Counter = Counter()
    for _, _, h, lines in pages:
        seen = {_norm(l.text) for l in lines if l.top < _MARGIN_ZONE * h or l.bottom > (1 - _MARGIN_ZONE) * h}
        counts.update(seen)
    limit = max(3, int(0.3 * len(pages)))
    repeated = {t for t, c in counts.items() if c >= limit}
    removed: set[str] = set()
    for i, (n, w, h, lines) in enumerate(pages):
        kept = []
        for l in lines:
            in_margin = l.top < _MARGIN_ZONE * h or l.bottom > (1 - _MARGIN_ZONE) * h
            if in_margin and (_norm(l.text) in repeated or _RE_PAGE_NUMBER.match(l.text)):
                removed.add(l.text if not _RE_PAGE_NUMBER.match(l.text) else "(page numbers)")
                continue
            kept.append(l)
        pages[i] = (n, w, h, kept)
    return sorted(removed)


def _statistics(pages) -> tuple[float, float, float]:
    """(body size, typical left margin, median body line length)."""
    sizes: Counter = Counter()
    for _, _, _, lines in pages:
        for l in lines:
            sizes[round(l.size * 2) / 2] += len(l.text)
    if not sizes:
        return 11.0, 0.0, 0.0
    body = sizes.most_common(1)[0][0]
    margins: Counter = Counter()
    lengths = []
    for _, _, _, lines in pages:
        for l in lines:
            if abs(l.size - body) <= 0.6:
                margins[round(l.x0 / 2) * 2] += 1
                lengths.append(len(l.text))
    margin = margins.most_common(1)[0][0] if margins else 0.0
    median = statistics.median(lengths) if lengths else 0.0
    return body, float(margin), float(median)


def _classify(pages, body: float) -> dict[float, int]:
    """Fills l.cat and returns {title size: level}."""
    title_sizes = sorted(
        {round(l.size * 2) / 2 for _, _, _, ls in pages for l in ls if l.size >= body * 1.15}, reverse=True
    )
    levels = {t: i + 1 for i, t in enumerate(title_sizes)}
    for _, _, h, lines in pages:
        for l in lines:
            if l.size >= body * 1.15:
                l.cat = "title"
            elif body * 0.5 <= l.size <= body * 0.88 and l.top > 0.5 * h:
                l.cat = "note"
            elif (l.bold and l.size >= body * 0.98 and len(l.text) <= 80
                  and not l.text.rstrip().endswith((".", ",", ";"))):
                l.cat = "minor_title"
            else:
                l.cat = "body"
    return levels


def _ends_sentence(t: str) -> bool:
    return t.rstrip()[-1:] in _END_PUNCTUATION


def _paragraph_gap(pages, body: float) -> float:
    """Vertical gap above which consecutive body lines belong to different paragraphs.

    A fixed fraction of the type size breaks on 1.5- or double-spaced text, where the normal gap between
    the lines of one paragraph is already larger than that; so it is also scaled by the typical gap."""
    gaps = []
    for _, _, _, lines in pages:
        for a, b in zip(lines, lines[1:]):
            if a.cat == b.cat == "body" and abs(a.size - body) <= 0.6 and abs(b.size - body) <= 0.6:
                gap = b.top - a.bottom
                if 0 <= gap < 1.5 * body:
                    gaps.append(gap)
    typical = statistics.median(gaps) if len(gaps) >= 5 else 0.0
    return max(0.45 * body, 1.6 * typical)


def _page_blocks(lines: list[_Line], body: float, margin: float, median: float,
                 levels: dict[float, int], paragraph_gap: float) -> tuple[list[Block], set[int]]:
    """Returns the page's blocks and the set of id()s of those starting with a paragraph indent."""
    groups: list[list[_Line]] = []
    for l in lines:
        if not groups:
            groups.append([l])
            continue
        prev = groups[-1][-1]
        gap = l.top - prev.bottom
        new_group = False
        if l.cat != prev.cat:
            new_group = True
        elif l.cat in ("title", "minor_title"):
            new_group = abs(l.size - prev.size) > 0.6 or gap > 0.8 * l.size
        elif l.cat == "note":
            new_group = bool(_RE_NOTE_START.match(l.text)) and _ends_sentence(prev.text)
        else:
            indent = l.x0 - margin > 0.8 * body
            new_group = gap > paragraph_gap or (indent and _ends_sentence(prev.text))
        if new_group:
            groups.append([l])
        else:
            groups[-1].append(l)

    minor_level = len(levels) + 1
    blocks: list[Block] = []
    indented: set[int] = set()
    for g in groups:
        cat = g[0].cat
        page = g[0].page
        texts = [l.text for l in g]
        if cat == "title":
            b = Block("", TITLE, join_lines(texts), page, levels.get(round(g[0].size * 2) / 2, 1))
        elif cat == "minor_title":
            b = Block("", TITLE, join_lines(texts), page, min(minor_level, 4))
        elif cat == "note":
            b = Block("", NOTE, join_lines(texts), page)
        elif len(g) == 1 and is_separator(texts[0]):
            b = Block("", SEPARATOR, "* * *", page)
        elif looks_like_verse(texts, median):
            b = Block("", VERSE, "\n".join(texts), page)
        else:
            b = Block("", PARAGRAPH, join_lines(texts), page)
        if g[0].x0 - margin > 0.8 * body:
            indented.add(id(b))
        blocks.append(b)
    return blocks, indented


def _merge_continuations(blocks: list[Block], indented: set[int]) -> list[Block]:
    """Joins the paragraph that ends at the foot of a page and continues at the top of the next one.

    Continuation = the previous one does not end in sentence-final punctuation and the next one starts
    lowercase or without an indent (in German, nouns start uppercase, so case alone is not enough).
    """
    out: list[Block] = []
    last: Block | None = None
    seen_pages: set[int] = set()
    for b in blocks:
        first = False
        if b.kind != NOTE and b.page not in seen_pages:
            seen_pages.add(b.page)
            first = True
        if (first and b.kind == PARAGRAPH and last is not None and last.page != b.page
                and not _ends_sentence(last.text)
                and (b.text[:1].islower() or id(b) not in indented)):
            last.text = join_lines([last.text, b.text])
            continue
        out.append(b)
        if b.kind != NOTE:
            last = b if b.kind == PARAGRAPH else None
    return out


def extract_blocks(path: Path, pages: set[int] | None = None, progress=None, require_text: bool = True):
    """Blocks of the pages with selectable text. `pages` (1-based numbers) limits the extraction.

    Returns (blocks, indented, PDF metadata, report); `indented` = id()s of the blocks starting with an indent."""
    import pdfplumber

    if pages is not None and not pages:
        return [], set(), {}, {"total_pages": 0, "pages_without_text": [], "headers_removed": [],
                               "body_size": 0.0}
    pages_read: list[tuple[int, float, float, list[_Line]]] = []
    without_text: list[int] = []
    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        meta = pdf.metadata or {}
        for i, page in enumerate(pdf.pages, 1):
            if progress:
                progress(i, total)
            if pages is not None and i not in pages:
                continue
            n = len(page.chars)
            cov = _image_coverage(page) if n < 50 else 0.0
            if n == 0 or (n < 50 and cov >= 0.5):
                if cov >= 0.5:  # image covering the page, no text: it's a scan (needs OCR)
                    without_text.append(i)
                continue  # blank page
            pages_read.append((i, float(page.width), float(page.height), _page_lines(page, i)))

    words = sum(len(l.text.split()) for _, _, _, ls in pages_read for l in ls)
    if require_text and words < 100:
        raise ExtractionError(
            "The PDF has almost no selectable text; it looks scanned. Run again and choose OCR."
        )

    headers = _remove_headers(pages_read)
    body, margin, median = _statistics(pages_read)
    levels = _classify(pages_read, body)
    paragraph_gap = _paragraph_gap(pages_read, body)

    blocks: list[Block] = []
    indented: set[int] = set()
    for _, _, _, lines in pages_read:
        bl, ind = _page_blocks(lines, body, margin, median, levels, paragraph_gap)
        blocks.extend(bl)
        indented |= ind
    report = {
        "total_pages": total,
        "pages_without_text": without_text,
        "headers_removed": headers,
        "body_size": body,
    }
    return blocks, indented, meta, report


def extract(path: Path, progress=None) -> tuple:
    blocks, indented, meta, report = extract_blocks(path, None, progress)
    blocks = _merge_continuations(blocks, indented)
    title = (meta.get("Title") or "").strip() or Path(path).stem.replace("_", " ")
    author = (meta.get("Author") or "").strip()
    book = build_book(title, author, "", split_into_chapters(blocks))
    return book, report
