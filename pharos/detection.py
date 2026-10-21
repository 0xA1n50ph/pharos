"""File type detection (by content, not just the extension)."""
from __future__ import annotations

import zipfile
from pathlib import Path

_CONVERTIBLE = {".mobi", ".azw", ".azw3", ".fb2", ".docx", ".rtf", ".odt", ".html", ".htm", ".djvu"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
_IMAGE_SIGNATURES = (b"\x89PNG", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*", b"BM", b"RIFF")


def image_folder(path: Path) -> bool:
    return path.is_dir() and any(p.suffix.lower() in IMAGE_EXT for p in path.iterdir() if p.is_file())


def file_type(path: Path) -> str:
    """Returns: pdf | epub | txt | images | convertible | unknown.

    'images' = a folder of page images, or a single image/multi-page TIFF (everything goes to OCR)."""
    if path.is_dir():
        return "images" if image_folder(path) else "unknown"
    ext = path.suffix.lower()
    with open(path, "rb") as f:
        header = f.read(8)
    if header.startswith(b"%PDF"):
        return "pdf"
    if ext in IMAGE_EXT and header.startswith(_IMAGE_SIGNATURES):
        return "images"
    if header.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                if "mimetype" in names and z.read("mimetype").startswith(b"application/epub"):
                    return "epub"
                if "word/document.xml" in names:
                    return "convertible"
        except zipfile.BadZipFile:
            pass
    if ext in (".txt", ".md", ".markdown"):
        return "txt"
    if ext in _CONVERTIBLE:
        return "convertible"
    return "unknown"
