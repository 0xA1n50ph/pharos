"""Generates samples for the tests: a text PDF, degraded scans, EPUB and TXT (in German and in English).

Usage: python tests/samples.py OUTPUT_FOLDER
Needs reportlab, pillow, numpy and pypdfium2 (for the tests only).
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

# ----------------------------------------------------------------------------- texts (German)

TITLE = "Der Leuchtturm"
AUTHOR = "Anna Berger"

CH1 = [
    "Der Wind kam am frühen Morgen von Westen und brachte den Geruch von Salz und feuchtem Holz mit sich. "
    "Elias Brandt stand schon seit einer Stunde auf der Galerie des Leuchtturms und beobachtete, wie sich die "
    "Wolken über dem Meer langsam auseinanderschoben. Er hatte in dieser Nacht kaum geschlafen, denn das Licht "
    "musste bis zur Dämmerung brennen, und seit dem Tod seines Vorgängers gab es niemanden mehr, der ihn hätte "
    "ablösen können.",
    "Unten am Fuß der Klippen lag das kleine Dorf, dessen Bewohner den Leuchtturmwärter nur selten zu Gesicht "
    "bekamen.¹ Sie wussten, dass er da war, so wie man weiß, dass die Gezeiten kommen und gehen, ohne sie jemals "
    "gezählt zu haben. Manchmal stieg eine dünne Rauchsäule aus dem Schornstein der Wärterhütte auf, und dann "
    "sagten die Fischer zueinander, dass der Wärter noch lebe.",
    "Am Nachmittag brachte der Postbote einen Brief, dessen Umschlag keine Absenderangabe trug. Elias drehte ihn "
    "lange in den Händen, bevor er ihn öffnete, und las die wenigen Zeilen zweimal, ohne ein Wort zu verstehen. "
    "Dann legte er das Blatt auf den Tisch, strich es glatt und ging hinaus, um die Lampe für die kommende Nacht "
    "vorzubereiten.",
    "POEM",
    "Die Linse musste jeden Abend gereinigt werden, und er tat es mit der Sorgfalt eines Mannes, der weiß, dass "
    "von seiner Arbeit das Leben anderer abhängt. Das Messing glänzte im letzten Sonnenlicht, und für einen "
    "Augenblick schien der ganze Turm zu atmen, bevor die erste Flamme entzündet wurde und das Licht weit über "
    "das dunkle Wasser hinausreichte.",
    "In der Nacht kam der Nebel. Er stieg aus dem Wasser auf, lautlos und dicht, und verschluckte zuerst die "
    "Klippen, dann das Dorf und schließlich den Himmel. Elias bediente die Sirene von Hand, Stunde um Stunde, "
    "und er zählte die Atemzüge zwischen den Tönen, um nicht den Verstand zu verlieren.",
]
POEM = [
    "Wo das Licht den Nebel teilt,",
    "da ruht der Wächter ungesehen,",
    "und die See, die niemals weilt,",
    "lässt die Schiffe weiterziehen.",
]
CH2 = [
    "In den folgenden Wochen änderte sich das Wetter beinahe täglich. Stürme wechselten mit zähem Nebel ab, und "
    "die Sirene, die Elias bei schlechter Sicht von Hand bedienen musste, klang bald wie ein Teil seines eigenen "
    "Atems.",
    "Er begann, ein Tagebuch zu führen, zuerst nur mit Angaben zu Wind und Wellengang, dann mit kurzen Notizen "
    "über die Schiffe, die er sah. Es waren meist Frachter und Fischkutter, gelegentlich ein Segler, dessen "
    "weiße Segel im Abendlicht aufleuchteten.",
    "Eines Abends sah er unterhalb der Klippen ein kleines Boot ohne Licht. Es trieb langsam auf die Felsen zu, "
    "und niemand schien an Bord zu sein. Elias lief die Treppe hinunter, so schnell seine Beine ihn trugen, und "
    "erreichte den Strand, als die ersten Wellen das Holz gegen die Steine drückten.",
    "Im Boot lag nichts außer einer nassen Decke und einem Kompass, dessen Nadel sich unaufhörlich drehte. Elias "
    "nahm beides mit hinauf in den Turm und stellte sie auf das Fensterbrett, wo sie bis zum Morgen standen, "
    "ohne dass er eine Erklärung fand.",
]

# ----------------------------------------------------------------------------- texts (English, for the real OCR)

TITLE_EN = "The Lighthouse"
AUTHOR_EN = "Anna Berger"
NOTE_EN = "¹ At that time a lighthouse keeper was an official of the coastal administration."
POEM_EN = [
    "Where the light divides the fog,",
    "the keeper rests unseen,",
    "and the sea, that never lingers,",
    "lets the ships sail on.",
]
CAPS_EN = [
    ("First Chapter", [
        "The wind came from the west early in the morning and brought with it the smell of salt and damp wood. "
        "Elias Brandt had been standing on the gallery of the lighthouse for an hour, watching the clouds slowly "
        "part above the sea. He had hardly slept that night, because the light had to burn until dawn, and since "
        "the death of his predecessor there had been nobody who could have relieved him.",
        "Down at the foot of the cliffs lay the little village, whose inhabitants seldom caught sight of the "
        "keeper.¹ They knew that he was there, as one knows that the tides come and go without ever having "
        "counted them. Sometimes a thin column of smoke rose from the chimney of the keeper's hut, and then the "
        "fishermen told one another that the keeper was still alive.",
        "In the afternoon the postman brought a letter whose envelope bore no sender. Elias turned it over in his "
        "hands for a long time before he opened it, and he read the few lines twice without understanding a single "
        "word. Then he laid the sheet on the table, smoothed it flat and went outside to prepare the lamp for the "
        "coming night.",
        "POEM",
        "The lens had to be cleaned every evening, and he did it with the care of a man who knows that the lives "
        "of others depend on his work. The brass gleamed in the last of the sunlight, and for a moment the whole "
        "tower seemed to breathe, before the first flame was lit and the light reached far out across the dark "
        "water.",
        "At night the fog came. It rose out of the water, silent and dense, and swallowed first the cliffs, then "
        "the village and finally the sky. Elias worked the siren by hand, hour after hour, and he counted the "
        "breaths between the notes so as not to lose his mind.",
        "Toward morning the fog thinned, and a pale light spread over the water. He put out the lamp, wound up the "
        "clockwork for the next night and sat down on the steps of the tower with a cup of tea that had long gone "
        "cold. Far out, a single sail moved slowly toward the horizon.",
        "He slept for a few hours in the narrow bed beneath the stairs, and when he woke the sun stood high above "
        "the cliffs. The village below was busy with the day's work, and the smell of fresh bread drifted up the "
        "path, though he knew it could not possibly reach him at such a height.",
    ]),
    ("Second Chapter", [
        "In the following weeks the weather changed almost daily. Storms alternated with sluggish fog, and the "
        "siren, which Elias had to work by hand whenever the visibility was poor, soon sounded like a part of his "
        "own breathing.",
        "He began to keep a diary, at first only with notes on wind and swell, then with short remarks about the "
        "ships he saw. They were mostly freighters and fishing cutters, now and then a sailing yacht whose white "
        "sails lit up in the evening light.",
        "One evening he saw a small boat without a light beneath the cliffs. It was drifting slowly toward the "
        "rocks, and nobody seemed to be on board. Elias ran down the stairs as fast as his legs would carry him "
        "and reached the beach just as the first waves pressed the wood against the stones.",
        "There was nothing in the boat except a wet blanket and a compass whose needle turned without rest. Elias "
        "carried both up into the tower and put them on the windowsill, where they stood until morning without his "
        "finding any explanation.",
        "The next day he walked down to the village for the first time in months. The children stopped their game "
        "and stared at him, and an old woman crossed herself in the doorway of the baker's shop. He bought flour, "
        "salt and a small bottle of oil, paid with the last coins in his pocket and climbed back up without "
        "having spoken a word.",
        "That night the compass lay on the windowsill as before, but its needle no longer turned. It pointed "
        "steadily toward the sea, toward a place where nothing could be seen but the dark water and the line of "
        "the horizon.",
    ]),
    ("Third Chapter", [
        "The letter had been written by his brother, whom he had not seen for twenty years. It said only that the "
        "house in the valley was to be sold and that Elias should come if he wished to take anything with him. "
        "There was no greeting at the beginning and no name at the end.",
        "He considered the matter for three days. The light could not be left alone, and there was no one in the "
        "village who would take his place, not for any money. In the end he wrote a short reply, tore it up, "
        "wrote another and finally gave the postman an empty envelope.",
        "Winter came early that year. Ice formed on the railing of the gallery, and the wind found every gap in "
        "the old walls. Elias slept beside the stove and woke several times each night to climb the stairs and "
        "look at the flame, which burned steadily, indifferent to the cold.",
        "When the spring storms finally subsided, a ship appeared on the horizon and stopped in front of the "
        "cliffs. A boat was lowered, and a man in a dark coat rowed ashore. Elias watched him from the gallery "
        "and knew, long before the man looked up, who he was.",
    ]),
]


# ----------------------------------------------------------------------------- text PDF

def _wrap(text: str, font: str, size: float, width: float, indent: float = 0.0) -> list[str]:
    """Wraps into lines with simple hyphenation (to test word joining)."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    lines, current, limit = [], "", width - indent
    for word in text.split():
        attempt = (current + " " + word).strip()
        if stringWidth(attempt, font, size) <= limit:
            current = attempt
            continue
        if len(word) >= 9:
            for cut in range(len(word) - 3, 3, -1):
                cand = (current + " " + word[:cut] + "-").strip()
                if stringWidth(cand, font, size) <= limit:
                    lines.append(cand)
                    current, limit = word[cut:], width
                    break
            else:
                lines.append(current)
                current, limit = word, width
        else:
            lines.append(current)
            current, limit = word, width
    if current:
        lines.append(current)
    return lines


def _make_pdf(dest: Path, title: str, author: str, subtitle: str, chapters: list, poem: list[str],
              note: str, style: str, final_scan_page: bool, justify: bool = False) -> None:
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase.pdfmetrics import stringWidth

    W, H = 420, 595
    margin, base = 50, 14.5
    c = canvas.Canvas(str(dest), pagesize=(W, H))
    c.setTitle(title)
    c.setAuthor(author)
    page = {"n": 1}
    notes = {3: note}

    def header_footer():
        if page["n"] > 1:
            c.setFont("Times-Italic", 9)
            c.drawCentredString(W / 2, H - 30, title)
        c.setFont("Times-Roman", 9)
        c.drawCentredString(W / 2, 28, str(page["n"]))
        if page["n"] in notes:
            c.setFont("Times-Roman", 8)
            c.drawString(margin, 85, notes[page["n"]])

    def new_page():
        header_footer()
        c.showPage()
        page["n"] += 1

    c.setFont("Times-Bold", 26)
    c.drawCentredString(W / 2, H - 220, title)
    c.setFont("Times-Roman", 14)
    c.drawCentredString(W / 2, H - 250, subtitle)
    c.setFont("Times-Roman", 12)
    c.drawCentredString(W / 2, H - 330, author)
    new_page()

    y = H - 80

    def line(text, font="Times-Roman", size=11, x=margin, last=True):
        nonlocal y
        if y < 110:
            new_page()
            y = H - 80
        c.setFont(font, size)
        if justify and not last and " " in text:
            words = text.split(" ")
            free = (W - 2 * margin - x + margin) - sum(stringWidth(p, font, size) for p in words)
            space_ = free / (len(words) - 1)
            xx = x
            for p in words:
                c.drawString(xx, y, p)
                xx += stringWidth(p, font, size) + space_
        else:
            c.drawString(x, y, text)
        y -= base

    def open_chapter(ttl):
        nonlocal y
        if y < H - 100:
            new_page()
            y = H - 80
        y -= 30
        c.setFont("Times-Bold", 18)
        c.drawString(margin, y, ttl)
        y -= 34

    for ci, (ttl, paragraphs) in enumerate(chapters):
        if ci > 0:
            new_page()
            y = H - 80
        open_chapter(ttl)
        first = True
        for p in paragraphs:
            if p == "POEM":
                y -= 4
                for v in poem:
                    line(v, "Times-Italic", 11, margin + 40)
                y -= 4
                first = True
                continue
            indent = 18.0 if (style == "indent" and not first) else 0.0
            if style == "space" and not first:
                y -= 8
            ls = _wrap(p, "Times-Roman", 11, W - 2 * margin, indent)
            for i, l in enumerate(ls):
                line(l, x=margin + (indent if i == 0 else 0), last=(i == len(ls) - 1))
            first = False
    new_page()

    if final_scan_page:
        from PIL import Image, ImageDraw
        import random

        img = Image.new("L", (840, 1190), 235)
        d = ImageDraw.Draw(img)
        random.seed(1)
        for yy in range(120, 1100, 28):
            d.line([(100, yy), (100 + random.randint(300, 640), yy)], fill=60, width=6)
        file = dest.with_suffix(".scan.png")
        img.save(file)
        c.drawImage(str(file), 0, 0, width=W, height=H)
        file.unlink()
        c.showPage()
    c.save()


def make_pdf(dest: Path, style: str = "indent", language: str = "de", justify: bool = False) -> None:
    """Text PDF. German: 2 chapters + a 'scanned' page at the end. English: 3 chapters, no extra page."""
    if language == "de":
        _make_pdf(dest, TITLE, AUTHOR, "Eine Erzählung",
                  [("Erstes Kapitel", CH1), ("Zweites Kapitel", CH2)], POEM,
                  "¹ Als Leuchtturmwärter bezeichnete man damals einen Beamten der Küstenverwaltung.",
                  style, True, justify)
    else:
        _make_pdf(dest, TITLE_EN, AUTHOR_EN, "A Tale", CAPS_EN, POEM_EN, NOTE_EN, style, False, justify)


def expected_text_en() -> list[str]:
    """Paragraphs of the English book, in order (no poem, no note), to measure the OCR error."""
    return [p for _, ps in CAPS_EN for p in ps if p != "POEM"]


# ----------------------------------------------------------------------------- scans

def degrade(img, angle: float = 1.4, noise: float = 6.0, shadow: float = 0.18, blur: float = 0.7, seed: int = 0):
    """Imitates a scan/photo: skews, darkens the paper with a gradient, adds noise and blur."""
    import numpy as np
    from PIL import Image, ImageFilter

    rng = np.random.default_rng(seed)
    img = img.convert("L").rotate(angle, resample=Image.BICUBIC, fillcolor=255)
    a = np.asarray(img, dtype=np.float32)
    h, w = a.shape
    gx = np.linspace(1 - shadow, 1.0, w, dtype=np.float32)[None, :]
    gy = np.linspace(1.0, 1 - shadow / 2, h, dtype=np.float32)[:, None]
    a = a * (238 / 255.0) * gx * gy + rng.normal(0, noise, a.shape)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(blur))


def render_pdf(pdf: Path, dpi: int = 200, pages: list[int] | None = None):
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf))
    idx = pages if pages is not None else range(len(doc))
    out = [doc[i].render(scale=dpi / 72).to_pil().convert("L") for i in idx]
    doc.close()
    return out


def make_scan(src_pdf: Path, dest: Path, dpi: int = 200, angle: float = 1.4, noise: float = 6.0,
              doubles: bool = False, pages: list[int] | None = None, jpeg: int = 70) -> list:
    """Image-only PDF (no text) from a text PDF. Returns the generated images."""
    from PIL import Image

    imgs = [degrade(im, angle + (0.3 if i % 2 else 0), noise, seed=i)
            for i, im in enumerate(render_pdf(src_pdf, dpi, pages))]
    if doubles:  # joins them two by two, side by side, with a dark gutter in the middle
        joined = []
        for a, b in zip(imgs[0::2], imgs[1::2]):
            sheet = Image.new("L", (a.width + b.width + 60, a.height), 120)
            sheet.paste(a, (0, 0))
            sheet.paste(b, (a.width + 60, 0))
            joined.append(sheet)
        imgs = joined
    imgs[0].save(dest, "PDF", resolution=dpi, save_all=True, append_images=imgs[1:], quality=jpeg)
    return imgs


def make_images(src_pdf: Path, folder: Path, dpi: int = 200, angle: float = 1.0) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for i, im in enumerate(render_pdf(src_pdf, dpi), 1):
        degrade(im, angle, 5.0, seed=i).save(folder / f"page_{i:03d}.jpg", quality=75)


# ----------------------------------------------------------------------------- TXT and EPUB

def make_txt(dest: Path) -> None:
    lines = [TITLE.upper(), "", "KAPITEL 1", ""]
    from textwrap import fill

    def body(items):
        for p in items:
            if p == "POEM":
                lines.extend(POEM + [""])
            else:
                lines.extend(fill(p, 68).split("\n") + [""])

    body(CH1)
    lines += ["KAPITEL 2", ""]
    body(CH2)
    dest.write_text("\n".join(lines), encoding="utf-8")


def make_epub(dest: Path) -> None:
    """EPUB 3 written by hand (independent of the program's exporter)."""
    def cap_html(title, items, with_note=False):
        parts = [f'<h1>{title}</h1>']
        for p in items:
            if p == "POEM":
                parts.append("<p class=\"verso\">" + "<br/>\n".join(POEM) + "</p>")
            else:
                parts.append("<p>\n  " + p.replace("¹", '<a epub:type="noteref" href="#n1">1</a>') + "\n</p>")
        if with_note:
            parts.append('<aside epub:type="footnote" id="n1"><p>1 Als Leuchtturmwärter bezeichnete man damals '
                         'einen Beamten der Küstenverwaltung.</p></aside>')
        parts.append("<hr/>")
        body = "\n".join(parts)
        return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="de">'
                f'<head><title>{title}</title></head><body>{body}</body></html>')

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="id">urn:uuid:12345</dc:identifier><dc:title>{TITLE}</dc:title>
<dc:creator>{AUTHOR}</dc:creator><dc:language>de</dc:language></metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="c1" href="text/cap1.xhtml" media-type="application/xhtml+xml"/>
<item id="c2" href="text/cap2.xhtml" media-type="application/xhtml+xml"/>
</manifest>
<spine><itemref idref="nav"/><itemref idref="c1"/><itemref idref="c2"/></spine></package>"""
    nav = ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" '
           'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Inhalt</title></head><body>'
           '<nav epub:type="toc"><ol><li><a href="text/cap1.xhtml">Erstes Kapitel</a></li>'
           '<li><a href="text/cap2.xhtml">Zweites Kapitel</a></li></ol></nav></body></html>')
    container = ('<?xml version="1.0"?><container version="1.0" '
                 'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                 '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                 '</rootfiles></container>')
    with zipfile.ZipFile(dest, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/nav.xhtml", nav)
        z.writestr("OEBPS/text/cap1.xhtml", cap_html("Erstes Kapitel", CH1, with_note=True))
        z.writestr("OEBPS/text/cap2.xhtml", cap_html("Zweites Kapitel", CH2))


if __name__ == "__main__":
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "samples")
    folder.mkdir(parents=True, exist_ok=True)
    make_pdf(folder / "leuchtturm_indent.pdf", "indent")
    make_pdf(folder / "leuchtturm_space.pdf", "space")
    make_pdf(folder / "lighthouse.pdf", "indent", "en")
    make_scan(folder / "lighthouse.pdf", folder / "lighthouse_scan.pdf")
    make_txt(folder / "leuchtturm.txt")
    make_epub(folder / "leuchtturm.epub")
    print("Samples generated in", folder)
