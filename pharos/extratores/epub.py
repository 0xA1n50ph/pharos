"""Extrator de EPUB (só zipfile + xml + BeautifulSoup)."""
from __future__ import annotations

import posixpath
import re
import zipfile
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, Tag

from ..limpeza import limpar_texto
from ..modelo import Bloco, LEGENDA, NOTA, PARAGRAFO, SEPARADOR, TITULO, VERSO
from .comum import ErroExtracao, dividir_em_capitulos, e_separador, montar_livro

_NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "o": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
_TITULOS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_FILHOS_BLOCO = [
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "blockquote", "div", "section",
    "article", "ul", "ol", "table", "figure", "hr", "aside", "dl",
]
_CONTENEDORES = {
    "div", "section", "article", "blockquote", "ul", "ol", "body", "table", "tbody", "thead",
    "tr", "td", "th", "figure", "main", "aside", "dl", "header", "footer",
}
_IGNORAR = {"img", "svg", "script", "style", "nav", "head", "title", "meta", "link"}


def _texto_xml(no) -> str:
    return (no.text or "").strip() if no is not None else ""


def _e_nota(tag: Tag) -> bool:
    tipo = (tag.get("epub:type") or "") + " " + " ".join(tag.get("class") or [])
    return any(k in tipo.lower() for k in ("footnote", "endnote", "rearnote"))


def _folha(tag: Tag, saida: list[Bloco], nota: bool) -> None:
    # normaliza espaços/quebras do código-fonte (não são quebras de linha reais)
    for s in tag.find_all(string=True):
        s.replace_with(re.sub(r"\s+", " ", str(s)))
    for a in tag.find_all("a"):
        if "noteref" in (a.get("epub:type") or ""):
            a.decompose()
    for br in tag.find_all("br"):
        br.replace_with("\n")
    linhas = [limpar_texto(l) for l in tag.get_text().split("\n")]
    linhas = [l for l in linhas if l]
    if not linhas:
        return
    if len(linhas) == 1 and e_separador(linhas[0]):
        saida.append(Bloco("", SEPARADOR, "* * *"))
    elif nota:
        saida.append(Bloco("", NOTA, " ".join(linhas)))
    elif len(linhas) > 1 and max(len(l) for l in linhas) <= 80:
        saida.append(Bloco("", VERSO, "\n".join(linhas)))
    else:
        saida.append(Bloco("", PARAGRAFO, " ".join(linhas)))


def _percorrer(no: Tag, saida: list[Bloco], nota: bool = False) -> None:
    for filho in no.children:
        if not isinstance(filho, Tag):
            continue
        nome = filho.name
        eh_nota = nota or _e_nota(filho)
        if nome in _IGNORAR:
            continue
        if nome in _TITULOS:
            t = limpar_texto(filho.get_text(" "))
            if t:
                saida.append(Bloco("", TITULO, t, nivel=_TITULOS[nome]))
        elif nome == "hr":
            saida.append(Bloco("", SEPARADOR, "* * *"))
        elif nome == "pre":
            linhas = [limpar_texto(l) for l in filho.get_text().split("\n")]
            linhas = [l for l in linhas if l]
            if linhas:
                saida.append(Bloco("", VERSO, "\n".join(linhas)))
        elif nome == "figcaption":
            t = limpar_texto(filho.get_text(" "))
            if t:
                saida.append(Bloco("", LEGENDA, t))
        elif nome in ("p", "li", "dt", "dd") and not filho.find(_FILHOS_BLOCO):
            _folha(filho, saida, eh_nota)
        elif nome in _CONTENEDORES or nome in ("p", "li", "dt", "dd"):
            if filho.find(_FILHOS_BLOCO):
                _percorrer(filho, saida, eh_nota)
            else:
                _folha(filho, saida, eh_nota)
        else:  # span, a, em... soltos
            _folha(filho, saida, eh_nota)


def _blocos_do_html(conteudo: bytes) -> list[Bloco]:
    soup = BeautifulSoup(conteudo, "html.parser")
    if soup.find("nav") and len(soup.get_text(" ", strip=True)) < 4000 and soup.find("nav").find("a"):
        # documento de sumário (EPUB3): não traduzir
        return []
    corpo = soup.body or soup
    saida: list[Bloco] = []
    _percorrer(corpo, saida)
    return saida


def extrair(caminho: Path, progresso=None) -> tuple:
    try:
        z = zipfile.ZipFile(caminho)
    except zipfile.BadZipFile as e:
        raise ErroExtracao(f"EPUB inválido: {e}") from e
    with z:
        try:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            opf_caminho = container.find(".//c:rootfile", _NS).get("full-path")
            opf = ET.fromstring(z.read(opf_caminho))
        except (KeyError, AttributeError, ET.ParseError) as e:
            raise ErroExtracao(f"Não consegui ler a estrutura do EPUB: {e}") from e
        base = posixpath.dirname(opf_caminho)
        titulo = _texto_xml(opf.find(".//dc:title", _NS)) or caminho.stem
        autor = ", ".join(_texto_xml(a) for a in opf.findall(".//dc:creator", _NS))
        idioma = _texto_xml(opf.find(".//dc:language", _NS)).split("-")[0].lower()

        manifesto = {
            i.get("id"): (i.get("href"), i.get("media-type") or "", i.get("properties") or "")
            for i in opf.findall(".//o:manifest/o:item", _NS)
        }
        ordem = [r.get("idref") for r in opf.findall(".//o:spine/o:itemref", _NS)]

        capitulos = []
        for n, idref in enumerate(ordem, 1):
            href, tipo_mime, props = manifesto.get(idref, (None, "", ""))
            if not href or "html" not in tipo_mime:
                continue
            if "nav" in props.split():
                continue
            if progresso:
                progresso(n, len(ordem))
            alvo = posixpath.normpath(posixpath.join(base, unquote(href.split("#")[0])))
            try:
                conteudo = z.read(alvo)
            except KeyError:
                continue
            blocos = _blocos_do_html(conteudo)
            capitulos.extend(dividir_em_capitulos(blocos))

    livro = montar_livro(titulo, autor, idioma, capitulos)
    return livro, {}
