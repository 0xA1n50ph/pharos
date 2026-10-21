"""EPUB extractor (only zipfile + xml + BeautifulSoup)."""
from __future__ import annotations

import posixpath
import re
import zipfile
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, Tag

from ..cleanup import clean_text
from ..model import Block, CAPTION, NOTE, PARAGRAPH, SEPARATOR, TITLE, VERSE
from .common import ExtractionError, split_into_chapters, is_separator, build_book

_NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "o": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
_TITLES = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCK_CHILDREN = [
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "blockquote", "div", "section",
    "article", "ul", "ol", "table", "figure", "hr", "aside", "dl",
]
_CONTAINERS = {
    "div", "section", "article", "blockquote", "ul", "ol", "body", "table", "tbody", "thead",
    "tr", "td", "th", "figure", "main", "aside", "dl", "header", "footer",
}
_IGNORE = {"img", "svg", "script", "style", "nav", "head", "title", "meta", "link"}


def _xml_text(node) -> str:
    return (node.text or "").strip() if node is not None else ""


def _is_note(tag: Tag) -> bool:
    type_str = (tag.get("epub:type") or "") + " " + " ".join(tag.get("class") or [])
    return any(k in type_str.lower() for k in ("footnote", "endnote", "rearnote"))


def _leaf(tag: Tag, out: list[Block], note: bool) -> None:
    # normalise source whitespace/breaks (they are not real line breaks)
    for s in tag.find_all(string=True):
        s.replace_with(re.sub(r"\s+", " ", str(s)))
    for a in tag.find_all("a"):
        if "noteref" in (a.get("epub:type") or ""):
            a.decompose()
    for br in tag.find_all("br"):
        br.replace_with("\n")
    lines = [clean_text(l) for l in tag.get_text().split("\n")]
    lines = [l for l in lines if l]
    if not lines:
        return
    if len(lines) == 1 and is_separator(lines[0]):
        out.append(Block("", SEPARATOR, "* * *"))
    elif note:
        out.append(Block("", NOTE, " ".join(lines)))
    elif len(lines) > 1 and max(len(l) for l in lines) <= 80:
        out.append(Block("", VERSE, "\n".join(lines)))
    else:
        out.append(Block("", PARAGRAPH, " ".join(lines)))


def _walk(node: Tag, out: list[Block], note: bool = False) -> None:
    for child in node.children:
        if not isinstance(child, Tag):
            continue
        name = child.name
        is_note = note or _is_note(child)
        if name in _IGNORE:
            continue
        if name in _TITLES:
            t = clean_text(child.get_text(" "))
            if t:
                out.append(Block("", TITLE, t, level=_TITLES[name]))
        elif name == "hr":
            out.append(Block("", SEPARATOR, "* * *"))
        elif name == "pre":
            lines = [clean_text(l) for l in child.get_text().split("\n")]
            lines = [l for l in lines if l]
            if lines:
                out.append(Block("", VERSE, "\n".join(lines)))
        elif name == "figcaption":
            t = clean_text(child.get_text(" "))
            if t:
                out.append(Block("", CAPTION, t))
        elif name in ("p", "li", "dt", "dd") and not child.find(_BLOCK_CHILDREN):
            _leaf(child, out, is_note)
        elif name in _CONTAINERS or name in ("p", "li", "dt", "dd"):
            if child.find(_BLOCK_CHILDREN):
                _walk(child, out, is_note)
            else:
                _leaf(child, out, is_note)
        else:  # loose span, a, em...
            _leaf(child, out, is_note)


def _blocks_from_html(content: bytes) -> list[Block]:
    soup = BeautifulSoup(content, "html.parser")
    if soup.find("nav") and len(soup.get_text(" ", strip=True)) < 4000 and soup.find("nav").find("a"):
        # table-of-contents document (EPUB3): do not translate
        return []
    body = soup.body or soup
    out: list[Block] = []
    _walk(body, out)
    return out


def extract(path: Path, progress=None) -> tuple:
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ExtractionError(f"Invalid EPUB: {e}") from e
    with z:
        try:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            opf_path = container.find(".//c:rootfile", _NS).get("full-path")
            opf = ET.fromstring(z.read(opf_path))
        except (KeyError, AttributeError, ET.ParseError) as e:
            raise ExtractionError(f"Could not read the EPUB structure: {e}") from e
        base = posixpath.dirname(opf_path)
        title = _xml_text(opf.find(".//dc:title", _NS)) or path.stem
        author = ", ".join(_xml_text(a) for a in opf.findall(".//dc:creator", _NS))
        language = _xml_text(opf.find(".//dc:language", _NS)).split("-")[0].lower()

        manifest = {
            i.get("id"): (i.get("href"), i.get("media-type") or "", i.get("properties") or "")
            for i in opf.findall(".//o:manifest/o:item", _NS)
        }
        order = [r.get("idref") for r in opf.findall(".//o:spine/o:itemref", _NS)]

        chapters = []
        for n, idref in enumerate(order, 1):
            href, mime, props = manifest.get(idref, (None, "", ""))
            if not href or "html" not in mime:
                continue
            if "nav" in props.split():
                continue
            if progress:
                progress(n, len(order))
            target = posixpath.normpath(posixpath.join(base, unquote(href.split("#")[0])))
            try:
                content = z.read(target)
            except KeyError:
                continue
            blocks = _blocks_from_html(content)
            chapters.extend(split_into_chapters(blocks))

    book = build_book(title, author, language, chapters)
    return book, {}
