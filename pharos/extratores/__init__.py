"""Extratores: cada um devolve (Livro, relatório) no formato interno."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .comum import ErroExtracao

_CALIBRE = [
    "ebook-convert",
    "/Applications/calibre.app/Contents/MacOS/ebook-convert",
]


def _achar_calibre() -> str | None:
    for c in _CALIBRE:
        achado = shutil.which(c) or (c if Path(c).exists() else None)
        if achado:
            return achado
    return None


def _via_calibre(caminho: Path):
    exe = _achar_calibre()
    if not exe:
        raise ErroExtracao(
            f"O formato {caminho.suffix} precisa do Calibre para ser convertido "
            "(brew install --cask calibre), ou converta o arquivo para EPUB/PDF antes."
        )
    from . import epub

    with tempfile.TemporaryDirectory() as tmp:
        destino = Path(tmp) / (caminho.stem + ".epub")
        r = subprocess.run([exe, str(caminho), str(destino)], capture_output=True, text=True)
        if r.returncode != 0 or not destino.exists():
            raise ErroExtracao(f"Falha ao converter com o Calibre: {r.stderr[-300:]}")
        return epub.extrair(destino)


def extrair(caminho: Path, tipo: str, progresso=None):
    if tipo == "pdf":
        from . import pdf_texto

        return pdf_texto.extrair(caminho, progresso)
    if tipo == "epub":
        from . import epub

        return epub.extrair(caminho, progresso)
    if tipo == "txt":
        from . import txt

        return txt.extrair(caminho)
    if tipo == "convertivel":
        return _via_calibre(caminho)
    raise ErroExtracao(f"Formato não suportado: {caminho.suffix or tipo}")
