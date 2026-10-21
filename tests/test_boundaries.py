#!/usr/bin/env python3
"""Tests of the paragraph-boundary check (pharos/boundaries.py) and of paragraph grouping on spaced-out PDFs.

Usage:  python tests/test_boundaries.py
Part A needs nothing. Part B builds a 1.5-spaced PDF with reportlab (skipped if missing).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pharos import boundaries  # noqa: E402
from pharos.model import Block, Book, Chapter, NOTE, PARAGRAPH, TITLE, VERSE  # noqa: E402
from pharos.preview import _html_lines  # noqa: E402
from pharos.translation import Result, alerts_report  # noqa: E402

failures = 0


def ok(cond: bool, text: str) -> None:
    global failures
    print(("  ok    " if cond else "  FAIL  ") + text)
    if not cond:
        failures += 1


def book_of(*items) -> Book:
    blocks = [Block("", kind, text, 1) for kind, text in items]
    b = Book(title="t", chapters=[Chapter(blocks=blocks)])
    b.renumber()
    return b


def texts(book: Book) -> list[str]:
    return [b.text for b in book.blocks()]


def part_a() -> None:
    print("A. joining and flagging")
    P = PARAGRAPH
    b = book_of((P, "Der Wind kam am frühen Morgen von Westen und brachte den Ge-"),
                (P, "ruch von Salz und feuchtem Holz mit sich, als der"),
                (P, "Wärter die Treppe hinaufstieg und lange am Fenster stand. Draußen"),
                (P, "zogen die Wolken langsam über das Meer."))
    r = boundaries.check_boundaries(b)
    ok(texts(b) == ["Der Wind kam am frühen Morgen von Westen und brachte den Geruch von Salz und feuchtem Holz "
                    "mit sich, als der Wärter die Treppe hinaufstieg und lange am Fenster stand. Draußen zogen die "
                    "Wolken langsam über das Meer."],
       "hyphenated word, function word and lowercase continuation become one paragraph")
    ok((r["hyphen"], r["sentence"], r["suspects"]) == (1, 2, []), f"counts reported {r['hyphen'], r['sentence']}")
    ok([x.id for x in b.blocks()] == ["c001p00001"], "the book is renumbered after joining")

    b = book_of((P, "Elias kannte die Gefahren der Küste besser als jeder"), (P, "Fischer im Dorf."))
    boundaries.check_boundaries(b)
    ok(len(texts(b)) == 1, "ends in a function word ('jeder') + capitalised noun: joined")

    b = book_of((P, "Er prüfte die Linse, die Lampe, das Messing und die"), (P, "Uhrwerke des Turms."))
    boundaries.check_boundaries(b)
    ok(len(texts(b)) == 1, "ends in a function word ('die') after commas: joined")

    b = book_of((P, "Im Boot lagen eine nasse Decke, ein Kompass,"), (P, "Seile und ein leerer Krug."))
    boundaries.check_boundaries(b)
    ok(len(texts(b)) == 1, "ends in a comma + capitalised noun: joined")

    b = book_of((P, "Der Ein- und Ausgang des Turms lag im Schatten der Klippen, und der Ein-"),
                (P, "und Ausgang des Hafens ebenso."))
    boundaries.check_boundaries(b)
    ok(texts(b) == ["Der Ein- und Ausgang des Turms lag im Schatten der Klippen, und der Ein- und Ausgang des "
                    "Hafens ebenso."],
       "'Ein-' + 'und Ausgang': joined with the hyphen and a space kept")

    b = book_of((P, "Unten am Fuß der Klippen lag das kleine Dorf mit seinen grauen"),
                (P, "Häusern und engen Gassen."))
    r = boundaries.check_boundaries(b)
    ok(len(texts(b)) == 2, "no punctuation + capitalised noun: NOT joined (ambiguous in German)")
    ok([s["id"] for s in r["suspects"]] == ["c001p00001"] and r["suspects"][0]["next"] == "c001p00002",
       "…but flagged as a suspect")

    b = book_of((TITLE, "INHALT"), (P, "1. DER LEUCHTTURM"), (P, "2. DER NEBEL"),
                (P, "Inhalt des Buches"), (P, "Das Buch beginnt hier."))
    r = boundaries.check_boundaries(b)
    ok(len(texts(b)) == 5 and not r["suspects"], "table-of-contents lines: not joined, not flagged")

    b = book_of((P, "Der Text endet hier ordentlich mit einem Punkt."), (P, "neuer Absatz in Kleinschrift."))
    boundaries.check_boundaries(b)
    ok(len(texts(b)) == 2, "a paragraph ending in a full stop is never joined")

    b = book_of((P, "Er sagte zu ihr: »Komm morgen wieder«"), (P, "und ging hinaus."))
    boundaries.check_boundaries(b)
    ok(len(texts(b)) == 2, "closing quotation mark counts as the end of a sentence")

    b = book_of((P, "Das Licht reichte weit über das dunkle Was-"), (NOTE, "1 Siehe Anhang."),
                (P, "ser hinaus."))
    boundaries.check_boundaries(b)
    ok(texts(b) == ["Das Licht reichte weit über das dunkle Wasser hinaus.", "1 Siehe Anhang."],
       "a footnote between the halves: paragraph joined, note kept after it")

    b = book_of((VERSE, "Wo das Licht den Nebel teilt,\nda ruht der Wächter"), (P, "und dann zog der Nebel auf."))
    boundaries.check_boundaries(b)
    ok(len(texts(b)) == 2, "verse is never joined")

    b = Book(chapters=[Chapter(blocks=[Block("", P, "Am Ende des Kapitels steht ein Satz ohne Punkt und", 1)]),
                       Chapter(blocks=[Block("", P, "weiter im nächsten Kapitel.", 2)])])
    b.renumber()
    boundaries.check_boundaries(b)
    ok(len(texts(b)) == 2, "never joins across chapters")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "boundaries.json"
        ok(boundaries.load_suspects(path) == set(), "missing file (older project): no suspects")
        b = book_of((P, "Unten am Fuß der Klippen lag das kleine Dorf mit seinen grauen"),
                    (P, "Häusern und engen Gassen."))
        r = boundaries.check_boundaries(b)
        boundaries.save(path, r)
        ids = boundaries.load_suspects(path)
        ok(ids == {"c001p00001"}, "suspects saved and loaded back")
        md = boundaries.report_markdown(r)
        ok("c001p00001 → c001p00002" in md and "grauen ‖ Häusern" in md, "markdown report lists the suspect")
        first = next(b.blocks())
        results = {x.id: Result("tradução", []) for x in b.blocks()}
        lines = _html_lines(list(b.blocks()), results, None, ids)
        ok(lines[0]["alerts"] == ["possible_split"] and lines[1]["alerts"] == [], "preview: badge on the suspect only")
        ok(results[first.id].alerts == [], "preview does not change the translation result")
        rep = alerts_report(b, results, ids)
        ok("possible_split" in rep and first.id in rep, "review report lists the suspect")


def part_b(tmp: Path) -> None:
    print("B. 1.5-spaced PDF (line gap larger than the old fixed threshold)")
    try:
        from reportlab.pdfgen import canvas
    except ImportError:
        print("  (reportlab missing; part B skipped)")
        return
    from pharos.extractors import pdf_text

    paragraphs = [
        ["Der Wind kam am frühen Morgen von Westen und brachte den Ge-",
         "ruch von Salz und feuchtem Holz mit sich. Elias stand schon seit",
         "einer Stunde auf der Galerie des Leuchtturms und sah, wie die Wol-",
         "ken über dem Meer langsam auseinanderzogen."],
        ["Unten am Fuß der Klippen lag das kleine Dorf, dessen Bewohner den",
         "Wärter nur selten zu Gesicht bekamen. Sie wussten, dass er da war,",
         "so wie man weiß, dass die Gezeiten kommen und gehen, ohne sie",
         "jemals gezählt zu haben."],
    ]
    path = tmp / "spaced.pdf"
    c = canvas.Canvas(str(path), pagesize=(432, 576))
    for page in range(3):
        y = 500  # below the header zone: a line repeated at the top of every page would be removed as a header
        c.setFont("Times-Roman", 12)
        for lines in paragraphs:
            for line in lines:
                c.drawString(37, y, line)
                y -= 18  # 1.5 spacing: 6 pt between lines of one paragraph
            y -= 18  # one blank line between paragraphs
        c.showPage()
    c.save()
    book, _ = pdf_text.extract(path)
    got = [b.text for b in book.blocks()]
    ok(len(got) == 6, f"6 paragraphs (2 per page), not one per line — got {len(got)}")
    ok(got and got[0].startswith("Der Wind") and "Geruch von Salz" in got[0]
       and got[0].endswith("auseinanderzogen."), "hyphenated words rejoined inside the paragraph")
    ok(len(got) > 1 and got[1].endswith("ohne sie jemals gezählt zu haben."), "second paragraph whole")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pharos_boundaries_"))
    part_a()
    part_b(tmp)
    print(f"\n{'ALL GOOD' if not failures else str(failures) + ' check(s) failed'} — files in {tmp}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
