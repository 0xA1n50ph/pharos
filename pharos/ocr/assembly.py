"""Assembles the Book (internal format) from the OCR blocks, already reviewed, and pages read directly from text."""
from __future__ import annotations

from ..extractors.common import split_into_chapters, build_book
from ..extractors.pdf_text import _merge_continuations
from ..model import Block, Book


def assemble(ocr_blocks: dict[int, list[dict]], extras: list[Block] | None = None, extra_indented: set[int] | None = None,
             title: str = "", author: str = "", language: str = "") -> Book:
    """ocr_blocks: {page: [dict]} (already edited). extras: blocks of pages with selectable text
    (mixed PDF), with the `id()`s of those starting indented in extra_indented."""
    all_blocks: list[Block] = []
    indented: set[int] = set(extra_indented or ())
    for n in sorted(ocr_blocks):
        for d in ocr_blocks[n]:
            b = Block("", d["kind"], d["text"], n, int(d.get("level", 1)))
            if d.get("indent"):
                indented.add(id(b))
            all_blocks.append(b)
    if extras:
        all_blocks.extend(extras)
        all_blocks.sort(key=lambda b: b.page or 0)  # stable: the order within each page is kept
    all_blocks = _merge_continuations(all_blocks, indented)
    return build_book(title, author, language, split_into_chapters(all_blocks))
