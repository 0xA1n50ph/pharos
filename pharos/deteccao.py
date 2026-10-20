"""Detecção do tipo de arquivo (pelo conteúdo, não só pela extensão)."""
from __future__ import annotations

import zipfile
from pathlib import Path

_CONVERTIVEIS = {".mobi", ".azw", ".azw3", ".fb2", ".docx", ".rtf", ".odt", ".html", ".htm", ".djvu"}
IMAGENS_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
_ASSINATURAS_IMAGEM = (b"\x89PNG", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*", b"BM", b"RIFF")


def pasta_de_imagens(caminho: Path) -> bool:
    return caminho.is_dir() and any(p.suffix.lower() in IMAGENS_EXT for p in caminho.iterdir() if p.is_file())


def tipo_arquivo(caminho: Path) -> str:
    """Devolve: pdf | epub | txt | imagens | convertivel | desconhecido.

    'imagens' = pasta de imagens de páginas, ou uma imagem/TIFF de várias páginas (tudo vai para o OCR)."""
    if caminho.is_dir():
        return "imagens" if pasta_de_imagens(caminho) else "desconhecido"
    ext = caminho.suffix.lower()
    with open(caminho, "rb") as f:
        cabecalho = f.read(8)
    if cabecalho.startswith(b"%PDF"):
        return "pdf"
    if ext in IMAGENS_EXT and cabecalho.startswith(_ASSINATURAS_IMAGEM):
        return "imagens"
    if cabecalho.startswith(b"PK"):
        try:
            with zipfile.ZipFile(caminho) as z:
                nomes = z.namelist()
                if "mimetype" in nomes and z.read("mimetype").startswith(b"application/epub"):
                    return "epub"
                if "word/document.xml" in nomes:
                    return "convertivel"
        except zipfile.BadZipFile:
            pass
    if ext in (".txt", ".md", ".markdown"):
        return "txt"
    if ext in _CONVERTIVEIS:
        return "convertivel"
    return "desconhecido"
