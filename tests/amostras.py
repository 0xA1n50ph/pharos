"""Gera amostras para os testes: PDF com texto, scans degradados, EPUB e TXT (em alemão e em inglês).

Uso: python tests/amostras.py PASTA_DE_SAIDA
Precisa de reportlab, pillow, numpy e pypdfium2 (só para os testes).
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

# ----------------------------------------------------------------------------- textos (alemão)

TITULO = "Der Leuchtturm"
AUTOR = "Anna Berger"

CAP1 = [
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
    "POEMA",
    "Die Linse musste jeden Abend gereinigt werden, und er tat es mit der Sorgfalt eines Mannes, der weiß, dass "
    "von seiner Arbeit das Leben anderer abhängt. Das Messing glänzte im letzten Sonnenlicht, und für einen "
    "Augenblick schien der ganze Turm zu atmen, bevor die erste Flamme entzündet wurde und das Licht weit über "
    "das dunkle Wasser hinausreichte.",
    "In der Nacht kam der Nebel. Er stieg aus dem Wasser auf, lautlos und dicht, und verschluckte zuerst die "
    "Klippen, dann das Dorf und schließlich den Himmel. Elias bediente die Sirene von Hand, Stunde um Stunde, "
    "und er zählte die Atemzüge zwischen den Tönen, um nicht den Verstand zu verlieren.",
]
POEMA = [
    "Wo das Licht den Nebel teilt,",
    "da ruht der Wächter ungesehen,",
    "und die See, die niemals weilt,",
    "lässt die Schiffe weiterziehen.",
]
CAP2 = [
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

# ----------------------------------------------------------------------------- textos (inglês, para o OCR real)

TITULO_EN = "The Lighthouse"
AUTOR_EN = "Anna Berger"
NOTA_EN = "¹ At that time a lighthouse keeper was an official of the coastal administration."
POEMA_EN = [
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
        "POEMA",
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


# ----------------------------------------------------------------------------- PDF com texto

def _quebrar(texto: str, fonte: str, tamanho: float, largura: float, recuo: float = 0.0) -> list[str]:
    """Quebra em linhas com hifenização simples (para testar a junção de palavras)."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    linhas, atual, limite = [], "", largura - recuo
    for palavra in texto.split():
        tentativa = (atual + " " + palavra).strip()
        if stringWidth(tentativa, fonte, tamanho) <= limite:
            atual = tentativa
            continue
        if len(palavra) >= 9:
            for corte in range(len(palavra) - 3, 3, -1):
                cand = (atual + " " + palavra[:corte] + "-").strip()
                if stringWidth(cand, fonte, tamanho) <= limite:
                    linhas.append(cand)
                    atual, limite = palavra[corte:], largura
                    break
            else:
                linhas.append(atual)
                atual, limite = palavra, largura
        else:
            linhas.append(atual)
            atual, limite = palavra, largura
    if atual:
        linhas.append(atual)
    return linhas


def _gerar_pdf(destino: Path, titulo: str, autor: str, subtitulo: str, capitulos: list, poema: list[str],
               nota: str, estilo: str, pagina_final_scan: bool, justificar: bool = False) -> None:
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase.pdfmetrics import stringWidth

    L, A = 420, 595
    margem, base = 50, 14.5
    c = canvas.Canvas(str(destino), pagesize=(L, A))
    c.setTitle(titulo)
    c.setAuthor(autor)
    pagina = {"n": 1}
    notas = {3: nota}

    def cabecalho_rodape():
        if pagina["n"] > 1:
            c.setFont("Times-Italic", 9)
            c.drawCentredString(L / 2, A - 30, titulo)
        c.setFont("Times-Roman", 9)
        c.drawCentredString(L / 2, 28, str(pagina["n"]))
        if pagina["n"] in notas:
            c.setFont("Times-Roman", 8)
            c.drawString(margem, 85, notas[pagina["n"]])

    def nova_pagina():
        cabecalho_rodape()
        c.showPage()
        pagina["n"] += 1

    c.setFont("Times-Bold", 26)
    c.drawCentredString(L / 2, A - 220, titulo)
    c.setFont("Times-Roman", 14)
    c.drawCentredString(L / 2, A - 250, subtitulo)
    c.setFont("Times-Roman", 12)
    c.drawCentredString(L / 2, A - 330, autor)
    nova_pagina()

    y = A - 80

    def linha(texto, fonte="Times-Roman", tam=11, x=margem, ultima=True):
        nonlocal y
        if y < 110:
            nova_pagina()
            y = A - 80
        c.setFont(fonte, tam)
        if justificar and not ultima and " " in texto:
            palavras = texto.split(" ")
            livre = (L - 2 * margem - x + margem) - sum(stringWidth(p, fonte, tam) for p in palavras)
            espaco = livre / (len(palavras) - 1)
            xx = x
            for p in palavras:
                c.drawString(xx, y, p)
                xx += stringWidth(p, fonte, tam) + espaco
        else:
            c.drawString(x, y, texto)
        y -= base

    def abrir_capitulo(tit):
        nonlocal y
        if y < A - 100:
            nova_pagina()
            y = A - 80
        y -= 30
        c.setFont("Times-Bold", 18)
        c.drawString(margem, y, tit)
        y -= 34

    for ci, (tit, paragrafos) in enumerate(capitulos):
        if ci > 0:
            nova_pagina()
            y = A - 80
        abrir_capitulo(tit)
        primeiro = True
        for p in paragrafos:
            if p == "POEMA":
                y -= 4
                for v in poema:
                    linha(v, "Times-Italic", 11, margem + 40)
                y -= 4
                primeiro = True
                continue
            recuo = 18.0 if (estilo == "recuo" and not primeiro) else 0.0
            if estilo == "espaco" and not primeiro:
                y -= 8
            ls = _quebrar(p, "Times-Roman", 11, L - 2 * margem, recuo)
            for i, l in enumerate(ls):
                linha(l, x=margem + (recuo if i == 0 else 0), ultima=(i == len(ls) - 1))
            primeiro = False
    nova_pagina()

    if pagina_final_scan:
        from PIL import Image, ImageDraw
        import random

        img = Image.new("L", (840, 1190), 235)
        d = ImageDraw.Draw(img)
        random.seed(1)
        for yy in range(120, 1100, 28):
            d.line([(100, yy), (100 + random.randint(300, 640), yy)], fill=60, width=6)
        arq = destino.with_suffix(".scan.png")
        img.save(arq)
        c.drawImage(str(arq), 0, 0, width=L, height=A)
        arq.unlink()
        c.showPage()
    c.save()


def gerar_pdf(destino: Path, estilo: str = "recuo", idioma: str = "de", justificar: bool = False) -> None:
    """PDF com texto. Alemão: 2 capítulos + página 'escaneada' no fim. Inglês: 3 capítulos, sem página extra."""
    if idioma == "de":
        _gerar_pdf(destino, TITULO, AUTOR, "Eine Erzählung",
                   [("Erstes Kapitel", CAP1), ("Zweites Kapitel", CAP2)], POEMA,
                   "¹ Als Leuchtturmwärter bezeichnete man damals einen Beamten der Küstenverwaltung.",
                   estilo, True, justificar)
    else:
        _gerar_pdf(destino, TITULO_EN, AUTOR_EN, "A Tale", CAPS_EN, POEMA_EN, NOTA_EN, estilo, False, justificar)


def texto_esperado_en() -> list[str]:
    """Parágrafos do livro em inglês, em ordem (sem poema, sem nota), para medir o erro do OCR."""
    return [p for _, ps in CAPS_EN for p in ps if p != "POEMA"]


# ----------------------------------------------------------------------------- scans

def degradar(img, angulo: float = 1.4, ruido: float = 6.0, sombra: float = 0.18, desfoque: float = 0.7, seed: int = 0):
    """Imita um scan/foto: inclina, escurece o papel com gradiente, acrescenta ruído e desfoque."""
    import numpy as np
    from PIL import Image, ImageFilter

    rng = np.random.default_rng(seed)
    img = img.convert("L").rotate(angulo, resample=Image.BICUBIC, fillcolor=255)
    a = np.asarray(img, dtype=np.float32)
    h, w = a.shape
    gx = np.linspace(1 - sombra, 1.0, w, dtype=np.float32)[None, :]
    gy = np.linspace(1.0, 1 - sombra / 2, h, dtype=np.float32)[:, None]
    a = a * (238 / 255.0) * gx * gy + rng.normal(0, ruido, a.shape)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(desfoque))


def renderizar_pdf(pdf: Path, dpi: int = 200, paginas: list[int] | None = None):
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf))
    idx = paginas if paginas is not None else range(len(doc))
    saida = [doc[i].render(scale=dpi / 72).to_pil().convert("L") for i in idx]
    doc.close()
    return saida


def gerar_scan(pdf_origem: Path, destino: Path, dpi: int = 200, angulo: float = 1.4, ruido: float = 6.0,
               duplas: bool = False, paginas: list[int] | None = None, jpeg: int = 70) -> list:
    """PDF só de imagens (sem texto) a partir de um PDF com texto. Devolve as imagens geradas."""
    from PIL import Image

    imgs = [degradar(im, angulo + (0.3 if i % 2 else 0), ruido, seed=i)
            for i, im in enumerate(renderizar_pdf(pdf_origem, dpi, paginas))]
    if duplas:  # junta de duas em duas, lado a lado, com uma calha escura no meio
        juntas = []
        for a, b in zip(imgs[0::2], imgs[1::2]):
            folha = Image.new("L", (a.width + b.width + 60, a.height), 120)
            folha.paste(a, (0, 0))
            folha.paste(b, (a.width + 60, 0))
            juntas.append(folha)
        imgs = juntas
    imgs[0].save(destino, "PDF", resolution=dpi, save_all=True, append_images=imgs[1:], quality=jpeg)
    return imgs


def gerar_imagens(pdf_origem: Path, pasta: Path, dpi: int = 200, angulo: float = 1.0) -> None:
    pasta.mkdir(parents=True, exist_ok=True)
    for i, im in enumerate(renderizar_pdf(pdf_origem, dpi), 1):
        degradar(im, angulo, 5.0, seed=i).save(pasta / f"pagina_{i:03d}.jpg", quality=75)


# ----------------------------------------------------------------------------- TXT e EPUB

def gerar_txt(destino: Path) -> None:
    linhas = [TITULO.upper(), "", "KAPITEL 1", ""]
    from textwrap import fill

    def corpo(lista):
        for p in lista:
            if p == "POEMA":
                linhas.extend(POEMA + [""])
            else:
                linhas.extend(fill(p, 68).split("\n") + [""])

    corpo(CAP1)
    linhas += ["KAPITEL 2", ""]
    corpo(CAP2)
    destino.write_text("\n".join(linhas), encoding="utf-8")


def gerar_epub(destino: Path) -> None:
    """EPUB 3 escrito à mão (independente do exportador do programa)."""
    def cap_html(titulo, lista, com_nota=False):
        partes = [f'<h1>{titulo}</h1>']
        for p in lista:
            if p == "POEMA":
                partes.append("<p class=\"verso\">" + "<br/>\n".join(POEMA) + "</p>")
            else:
                partes.append("<p>\n  " + p.replace("¹", '<a epub:type="noteref" href="#n1">1</a>') + "\n</p>")
        if com_nota:
            partes.append('<aside epub:type="footnote" id="n1"><p>1 Als Leuchtturmwärter bezeichnete man damals '
                          'einen Beamten der Küstenverwaltung.</p></aside>')
        partes.append("<hr/>")
        corpo = "\n".join(partes)
        return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="de">'
                f'<head><title>{titulo}</title></head><body>{corpo}</body></html>')

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="id">urn:uuid:12345</dc:identifier><dc:title>{TITULO}</dc:title>
<dc:creator>{AUTOR}</dc:creator><dc:language>de</dc:language></metadata>
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
    with zipfile.ZipFile(destino, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/nav.xhtml", nav)
        z.writestr("OEBPS/text/cap1.xhtml", cap_html("Erstes Kapitel", CAP1, com_nota=True))
        z.writestr("OEBPS/text/cap2.xhtml", cap_html("Zweites Kapitel", CAP2))


if __name__ == "__main__":
    pasta = Path(sys.argv[1] if len(sys.argv) > 1 else "amostras")
    pasta.mkdir(parents=True, exist_ok=True)
    gerar_pdf(pasta / "leuchtturm_recuo.pdf", "recuo")
    gerar_pdf(pasta / "leuchtturm_espaco.pdf", "espaco")
    gerar_pdf(pasta / "lighthouse.pdf", "recuo", "en")
    gerar_scan(pasta / "lighthouse.pdf", pasta / "lighthouse_scan.pdf")
    gerar_txt(pasta / "leuchtturm.txt")
    gerar_epub(pasta / "leuchtturm.epub")
    print("Amostras geradas em", pasta)
