"""Plain text / Markdown extractor."""
from __future__ import annotations

import re
import statistics
from pathlib import Path

from ..cleanup import join_lines, clean_text
from ..model import Block, PARAGRAPH, SEPARATOR, TITLE, VERSE
from .common import RE_CHAPTER, split_into_chapters, is_separator, build_book, looks_like_verse


def _read(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract(path: Path) -> tuple:
    text = _read(path).replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]

    # typical body line (to tell hard-wrapped text apart from verse)
    sizes = [len(l.strip()) for p in paragraphs for l in p.split("\n") if len(p.split("\n")) >= 3 and l.strip()]
    median = statistics.median(sizes) if sizes else 0

    blocks: list[Block] = []
    for p in paragraphs:
        lines = [l.rstrip() for l in p.split("\n") if l.strip()]
        if not lines:
            continue
        if len(lines) == 1:
            single = lines[0].strip()
            if is_separator(single):
                blocks.append(Block("", SEPARATOR, "* * *"))
                continue
            m = re.match(r"^(#{1,6})\s+(.*)$", single)
            if m:
                blocks.append(Block("", TITLE, clean_text(m.group(2)), level=len(m.group(1))))
                continue
            if len(single) < 80 and RE_CHAPTER.match(single):
                blocks.append(Block("", TITLE, clean_text(single), level=1))
                continue
            if (
                len(single) < 60
                and single.upper() == single
                and any(c.isalpha() for c in single)
                and not single.endswith((".", ","))
            ):
                blocks.append(Block("", TITLE, clean_text(single), level=2))
                continue
        if looks_like_verse(lines, median):
            blocks.append(Block("", VERSE, "\n".join(clean_text(l) for l in lines)))
        else:
            blocks.append(Block("", PARAGRAPH, join_lines(lines)))

    title = path.stem.replace("_", " ").replace("-", " ").strip()
    book = build_book(title, "", "", split_into_chapters(blocks))
    return book, {}
