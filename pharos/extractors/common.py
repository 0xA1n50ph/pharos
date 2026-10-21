"""Functions shared by the extractors."""
from __future__ import annotations

import re

from ..model import Block, Chapter, Book, TITLE


class ExtractionError(RuntimeError):
    pass


_RE_SEPARATOR = re.compile(r"^[\s\*\-_=~•·\.#]{3,}$")
RE_CHAPTER = re.compile(
    r"^(cap[ií]tulo|chapter|kapitel|chapitre|capitolo|livro|libro|buch|book|teil|parte|part|partie|tomo|abschnitt)\b",
    re.IGNORECASE,
)


def is_separator(text: str) -> bool:
    t = text.strip()
    return bool(_RE_SEPARATOR.match(t)) and not any(c.isalnum() for c in t)


def split_into_chapters(blocks: list[Block]) -> list[Chapter]:
    """Opens a chapter at every top-level title. Consecutive titles stay together."""
    if not blocks:
        return []
    levels = [b.level for b in blocks if b.kind == TITLE]
    if not levels:
        return [Chapter(blocks=list(blocks))]
    # chapter = the highest level that appears 2+ times (the book title, which appears once, does not count)
    counts = {n: levels.count(n) for n in set(levels)}
    repeated = [n for n, c in counts.items() if c >= 2]
    chapter_level = min(repeated) if repeated else min(levels)
    chapters: list[Chapter] = []
    current = Chapter()
    for b in blocks:
        if b.kind == TITLE and b.level == chapter_level and current.blocks:
            only_titles = all(x.kind == TITLE for x in current.blocks)
            # only accumulated titles stay with the next one ("Part I" + "Chapter 1"), except a title
            # page, which carries subtitle/author at a lower level than the chapters
            title_page = only_titles and any(x.level > chapter_level for x in current.blocks)
            if not only_titles or title_page:
                chapters.append(current)
                current = Chapter()
        current.blocks.append(b)
    if current.blocks:
        chapters.append(current)
    return chapters


def build_book(title: str, author: str, language: str, chapters: list[Chapter]) -> Book:
    book = Book(
        title=title.strip(),
        author=author.strip(),
        language=language,
        chapters=[c for c in chapters if c.blocks],
    )
    book.renumber()
    return book


def looks_like_verse(lines: list[str], median: float) -> bool:
    """A block of 3+ lines, most of them much shorter than the typical body line."""
    if len(lines) < 3 or median <= 0:
        return False
    middle = lines[:-1]  # the last line of a normal paragraph is usually short
    short = sum(1 for l in middle if len(l) < 0.6 * median)
    return short / len(middle) >= 0.6
