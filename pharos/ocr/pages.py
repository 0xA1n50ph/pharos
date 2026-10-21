"""Page sources for OCR: PDF (rendered) or images (files, folder, multi-page TIFF)."""
from __future__ import annotations

import re
import threading
from pathlib import Path

from PIL import Image, ImageOps

from ..extractors.common import ExtractionError

Image.MAX_IMAGE_PIXELS = 400_000_000

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
_MAX_RENDER_PIXELS = 45_000_000
_MIN_SHORT_SIDE = 2000  # small images are scaled up to here (Tesseract does better near 300 dpi)


class SourceError(ExtractionError):
    pass


def _natural(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def image_folder(path: Path) -> bool:
    return path.is_dir() and any(p.suffix.lower() in IMAGE_EXT for p in path.iterdir())


def is_image(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXT


class PageSource:
    """total, size(i) and render(i, dpi); i starts at 0."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()  # pdfium is not thread-safe
        self._pdf = None
        self._items: list[tuple[Path, int]] = []
        if self.path.is_dir() or is_image(self.path):
            self.kind = "images"
            files = (sorted((p for p in self.path.iterdir() if p.suffix.lower() in IMAGE_EXT), key=_natural)
                     if self.path.is_dir() else [self.path])
            for a in files:
                try:
                    with Image.open(a) as im:
                        frames = getattr(im, "n_frames", 1)
                except OSError as e:
                    raise SourceError(f"Unreadable image: {a.name} ({e})") from e
                self._items.extend((a, q) for q in range(frames))
            if not self._items:
                raise SourceError("No image found.")
        else:
            self.kind = "pdf"
            import pypdfium2 as pdfium

            try:
                self._pdf = pdfium.PdfDocument(str(self.path))
            except Exception as e:  # noqa: BLE001
                raise SourceError(f"Unreadable PDF: {e}") from e

    @property
    def total(self) -> int:
        return len(self._pdf) if self._pdf is not None else len(self._items)

    def size(self, i: int) -> tuple[float, float]:
        """(width, height) in arbitrary units, just to compute the ratio."""
        if self._pdf is not None:
            with self._lock:
                return tuple(self._pdf[i].get_size())  # type: ignore[return-value]
        file, frame = self._items[i]
        with Image.open(file) as im:
            return im.size

    def render(self, i: int, dpi: int = 300) -> Image.Image:
        if self._pdf is not None:
            with self._lock:
                page = self._pdf[i]
                w, h = page.get_size()
                scale = dpi / 72.0
                if w * h * scale * scale > _MAX_RENDER_PIXELS:
                    scale = (_MAX_RENDER_PIXELS / (w * h)) ** 0.5
                img = page.render(scale=scale).to_pil()
            return img.convert("L") if img.mode not in ("L", "RGB") else img
        file, frame = self._items[i]
        with Image.open(file) as im:
            im.seek(frame)
            img = ImageOps.exif_transpose(im) or im
            img = img.convert("L") if img.mode in ("1", "P", "LA", "I", "I;16", "F") else img.convert("RGB")
        short = min(img.size)
        if short < _MIN_SHORT_SIDE:
            f = min(3.0, _MIN_SHORT_SIDE / short)
            img = img.resize((round(img.width * f), round(img.height * f)), Image.LANCZOS)
        return img

    def close(self) -> None:
        if self._pdf is not None:
            try:
                self._pdf.close()
            except Exception:  # noqa: BLE001
                pass
            self._pdf = None
