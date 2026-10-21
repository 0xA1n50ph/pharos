"""Check of the boundaries between paragraphs, after any extractor (PDF text, OCR, EPUB, TXT).

A paragraph cut in the middle of a sentence gets translated in pieces, without the rest of the sentence (in
German the verb often comes at the end). Whatever the cause (line spacing, page break, footnote in the middle),
the text itself says when two blocks are one:
  - "Ge-" + "ruch von Salz ..."  (hyphen + lowercase)       -> same word, joined
  - "... Draußen" + "zogen die Wolken ..."  (no punctuation + lowercase)  -> same sentence, joined
  - "... besser als jeder" + "Fischer ..." or "... ein Kompass," + "Seile ..."  (ends in a comma or in a word
    that cannot end a sentence: article, preposition, conjunction)  -> same sentence, joined even though the
    next starts uppercase
  - "... mit seinen grauen" + "Häusern ..."  (no punctuation + uppercase) -> only flagged: in German every noun
    is capitalised, so this looks the same as a heading or list item followed by a paragraph.

The extraction runs before the language is known, so the function-word list covers the supported languages.
"""
from __future__ import annotations

import json
from pathlib import Path

from .cleanup import _hyphenated, join_lines
from .model import Book, NOTE, PARAGRAPH

_END_PUNCTUATION = ".!?…:;»«\"”“’)]*"
_MIN_WORDS_SUSPECT = 6  # shorter blocks without punctuation are usually headings, list or table-of-contents entries

POSSIBLE_SPLIT = "possible_split"

# words that never end a sentence; a block ending in one continues in the next
_FUNCTION_WORDS = set("""
der die das den dem des ein eine einen einem einer eines kein keine und oder aber sondern sowie denn
von vom zu zum zur mit für auf an am in im ins aus bei beim nach über unter vor durch gegen ohne um bis
seit dass daß als wie wenn weil ob damit dieser diese dieses diesen diesem jeder jede jedes sein seine
ihr ihre unser unsere mein meine
the a an and or but nor of to in on at for with by from into that as than this these those its their
o os as um uma uns umas de do da dos das em no na nos nas num numa e ou mas que com por pelo pela para
el la los las un una unos unas del al y ni en con sin sobre entre
le les des du au aux et ou mais à dans sur sous avec pour par sans ce cette ces son sa ses leur
""".split())


def _ends_sentence(t: str) -> bool:
    return t.rstrip()[-1:] in _END_PUNCTUATION


def _first_letter(t: str) -> str:
    """First character that is a letter or digit ('„holten' -> 'h'); '' if none."""
    for c in t:
        if c.isalnum():
            return c
    return ""


def _all_caps(t: str) -> bool:
    letters = [c for c in t if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def _cut_by_grammar(t: str) -> bool:
    """Ends in a comma or in a function word: the sentence certainly continues."""
    t = t.rstrip()
    if t.endswith(","):
        return True
    words = t.split()
    return bool(words) and words[-1].lower() in _FUNCTION_WORDS


def check_boundaries(book: Book) -> dict:
    """Joins the paragraphs that are clearly one, renumbers the book and returns
    {"hyphen": n, "sentence": n, "suspects": [{id, next, page, end, start}]}.

    Footnotes between the two halves do not stop the join (the note stays after the joined paragraph)."""
    joined = {"hyphen": 0, "sentence": 0}
    pending: list[tuple] = []  # (previous paragraph, next paragraph) to flag after renumbering
    for ch in book.chapters:
        out = []
        last = None  # last paragraph, if only notes came after it
        for b in ch.blocks:
            if b.kind == NOTE:
                out.append(b)
                continue
            if b.kind == PARAGRAPH and last is not None and not _ends_sentence(last.text):
                first = _first_letter(b.text)
                if _hyphenated(last.text, b.text):
                    last.text = join_lines([last.text, b.text])
                    joined["hyphen"] += 1
                    continue
                if first.islower() or (first.isalpha() and _cut_by_grammar(last.text)):
                    last.text = join_lines([last.text, b.text])
                    joined["sentence"] += 1
                    continue
                if (len(last.text.split()) >= _MIN_WORDS_SUSPECT and not _all_caps(last.text)
                        and not last.text.rstrip().endswith(("-", "‐", "‑"))):
                    pending.append((last, b))
            out.append(b)
            last = b if b.kind == PARAGRAPH else None
        ch.blocks = out
    book.renumber()
    suspects = [{"id": a.id, "next": b.id, "page": a.page,
                 "end": a.text[-80:], "start": b.text[:80]} for a, b in pending]
    return {**joined, "suspects": suspects}


def save(path: Path, result: dict) -> None:
    Path(path).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


def load_suspects(path: Path) -> set[str]:
    """Ids of the blocks that end abruptly (flagged, not joined). Empty if the check never ran."""
    try:
        return {s["id"] for s in json.loads(Path(path).read_text(encoding="utf-8"))["suspects"]}
    except (OSError, ValueError, KeyError):
        return set()


def report_markdown(result: dict) -> str:
    lines = ["# Paragraph boundaries", ""]
    lines.append(f"Joined automatically: {result['hyphen']} word(s) split by a hyphen, "
                 f"{result['sentence']} sentence(s) split across blocks.")
    lines.append("")
    if not result["suspects"]:
        lines.append("No suspicious boundary left.")
        return "\n".join(lines) + "\n"
    lines.append("Passages that end without punctuation and are followed by one starting with a capital letter. "
                 "They may be one paragraph cut in two (in German, a noun starts with a capital too) — "
                 "check them in the original:")
    lines.append("")
    for s in result["suspects"]:
        page = f" (p. {s['page']})" if s.get("page") else ""
        lines.append(f"- **{s['id']} → {s['next']}**{page}: …{s['end']} ‖ {s['start']}…")
    return "\n".join(lines) + "\n"
