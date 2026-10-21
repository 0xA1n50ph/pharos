"""Extractors: each one returns (Book, report) in the internal format."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .common import ExtractionError

_CALIBRE = [
    "ebook-convert",
    "/Applications/calibre.app/Contents/MacOS/ebook-convert",
]


def _find_calibre() -> str | None:
    for c in _CALIBRE:
        found = shutil.which(c) or (c if Path(c).exists() else None)
        if found:
            return found
    return None


def _via_calibre(path: Path):
    exe = _find_calibre()
    if not exe:
        raise ExtractionError(
            f"The {path.suffix} format needs Calibre to be converted "
            "(brew install --cask calibre), or convert the file to EPUB/PDF first."
        )
    from . import epub

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / (path.stem + ".epub")
        r = subprocess.run([exe, str(path), str(dest)], capture_output=True, text=True)
        if r.returncode != 0 or not dest.exists():
            raise ExtractionError(f"Failed to convert with Calibre: {r.stderr[-300:]}")
        return epub.extract(dest)


def extract(path: Path, kind: str, progress=None):
    if kind == "pdf":
        from . import pdf_text

        return pdf_text.extract(path, progress)
    if kind == "epub":
        from . import epub

        return epub.extract(path, progress)
    if kind == "txt":
        from . import txt

        return txt.extract(path)
    if kind == "convertible":
        return _via_calibre(path)
    raise ExtractionError(f"Unsupported format: {path.suffix or kind}")
