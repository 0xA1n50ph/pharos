"""Internal book format: every extractor produces this; translation and export only know this."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterator

# block kinds
TITLE = "title"
PARAGRAPH = "paragraph"
VERSE = "verse"          # short lines whose breaks matter (poems, invocations)
NOTE = "note"            # footnote or endnote
CAPTION = "caption"
SEPARATOR = "separator"  # "* * *", horizontal rule

TRANSLATABLE_TYPES = {TITLE, PARAGRAPH, VERSE, NOTE, CAPTION}


@dataclass
class Block:
    id: str
    kind: str
    text: str
    page: int | None = None
    level: int = 1  # only for a title (1 = highest)


@dataclass
class Chapter:
    blocks: list[Block] = field(default_factory=list)

    @property
    def title(self) -> str:
        for b in self.blocks:
            if b.kind == TITLE:
                return b.text
        return ""


@dataclass
class Book:
    title: str = ""
    author: str = ""
    language: str = ""
    chapters: list[Chapter] = field(default_factory=list)

    def blocks(self) -> Iterator[Block]:
        for ch in self.chapters:
            yield from ch.blocks

    def total_words(self) -> int:
        return sum(len(b.text.split()) for b in self.blocks() if b.kind != SEPARATOR)

    def renumber(self) -> None:
        """Assigns stable ids (c003p00045) once the structure is final."""
        for ci, ch in enumerate(self.chapters, 1):
            for bi, b in enumerate(ch.blocks, 1):
                b.id = f"c{ci:03d}p{bi:05d}"

    def save(self, path: Path) -> None:
        Path(path).write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path) -> "Book":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            title=d.get("title", ""),
            author=d.get("author", ""),
            language=d.get("language", ""),
            chapters=[
                Chapter(blocks=[Block(**b) for b in c["blocks"]]) for c in d["chapters"]
            ],
        )


def block_to_markdown(b: Block, text: str | None = None) -> str:
    t = b.text if text is None else text
    if b.kind == TITLE:
        return "#" * max(1, min(b.level, 6)) + " " + t
    if b.kind == VERSE:
        return "  \n".join(t.split("\n"))
    if b.kind == NOTE:
        return "> " + t.replace("\n", "\n> ")
    if b.kind == CAPTION:
        return "*" + t + "*"
    if b.kind == SEPARATOR:
        return "* * *"
    return t


def book_to_markdown(book: Book, translations: dict[str, str] | None = None) -> str:
    parts = []
    if book.title:
        parts.append(f"% {book.title}\n% {book.author}".rstrip())
    for b in book.blocks():
        text = None
        if translations is not None:
            text = translations.get(b.id, b.text)
        parts.append(block_to_markdown(b, text))
    return "\n\n".join(parts) + "\n"
