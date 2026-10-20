"""Fontes de páginas para o OCR: PDF (renderizado) ou imagens (arquivos, pasta, TIFF de várias páginas)."""
from __future__ import annotations

import re
import threading
from pathlib import Path

from PIL import Image, ImageOps

from ..extratores.comum import ErroExtracao

Image.MAX_IMAGE_PIXELS = 400_000_000

IMAGENS_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
_MAX_PIXELS_RENDER = 45_000_000
_LADO_CURTO_MINIMO = 2000  # imagens pequenas são ampliadas até aqui (o Tesseract rende melhor perto de 300 dpi)


class ErroFonte(ErroExtracao):
    pass


def _natural(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def pasta_de_imagens(caminho: Path) -> bool:
    return caminho.is_dir() and any(p.suffix.lower() in IMAGENS_EXT for p in caminho.iterdir())


def e_imagem(caminho: Path) -> bool:
    return caminho.is_file() and caminho.suffix.lower() in IMAGENS_EXT


class FontePaginas:
    """total, tamanho(i) e renderizar(i, dpi); i começa em 0."""

    def __init__(self, caminho: Path):
        self.caminho = Path(caminho)
        self._trava = threading.Lock()  # pdfium não é thread-safe
        self._pdf = None
        self._itens: list[tuple[Path, int]] = []
        if self.caminho.is_dir() or e_imagem(self.caminho):
            self.tipo = "imagens"
            arquivos = (sorted((p for p in self.caminho.iterdir() if p.suffix.lower() in IMAGENS_EXT), key=_natural)
                        if self.caminho.is_dir() else [self.caminho])
            for a in arquivos:
                try:
                    with Image.open(a) as im:
                        quadros = getattr(im, "n_frames", 1)
                except OSError as e:
                    raise ErroFonte(f"Imagem ilegível: {a.name} ({e})") from e
                self._itens.extend((a, q) for q in range(quadros))
            if not self._itens:
                raise ErroFonte("Nenhuma imagem encontrada.")
        else:
            self.tipo = "pdf"
            import pypdfium2 as pdfium

            try:
                self._pdf = pdfium.PdfDocument(str(self.caminho))
            except Exception as e:  # noqa: BLE001
                raise ErroFonte(f"PDF ilegível: {e}") from e

    @property
    def total(self) -> int:
        return len(self._pdf) if self._pdf is not None else len(self._itens)

    def tamanho(self, i: int) -> tuple[float, float]:
        """(largura, altura) em unidades arbitrárias, só para calcular proporção."""
        if self._pdf is not None:
            with self._trava:
                return tuple(self._pdf[i].get_size())  # type: ignore[return-value]
        arq, q = self._itens[i]
        with Image.open(arq) as im:
            return im.size

    def renderizar(self, i: int, dpi: int = 300) -> Image.Image:
        if self._pdf is not None:
            with self._trava:
                pagina = self._pdf[i]
                w, h = pagina.get_size()
                escala = dpi / 72.0
                if w * h * escala * escala > _MAX_PIXELS_RENDER:
                    escala = (_MAX_PIXELS_RENDER / (w * h)) ** 0.5
                img = pagina.render(scale=escala).to_pil()
            return img.convert("L") if img.mode not in ("L", "RGB") else img
        arq, q = self._itens[i]
        with Image.open(arq) as im:
            im.seek(q)
            img = ImageOps.exif_transpose(im) or im
            img = img.convert("L") if img.mode in ("1", "P", "LA", "I", "I;16", "F") else img.convert("RGB")
        curto = min(img.size)
        if curto < _LADO_CURTO_MINIMO:
            f = min(3.0, _LADO_CURTO_MINIMO / curto)
            img = img.resize((round(img.width * f), round(img.height * f)), Image.LANCZOS)
        return img

    def fechar(self) -> None:
        if self._pdf is not None:
            try:
                self._pdf.close()
            except Exception:  # noqa: BLE001
                pass
            self._pdf = None
