"""Tesseract via the command line: locate, list languages, run (TSV output) and detect orientation.

Each recognised word becomes a list: [block, par, line, left, top, width, height, conf, text]
(coordinates in pixels of the image sent to Tesseract; conf from 0 to 100).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from ..extractors.common import ExtractionError

INSTALL_INSTRUCTIONS = (
    "OCR needs Tesseract installed.\n"
    "  macOS:  brew install tesseract tesseract-lang\n"
    "  Linux:  sudo apt install tesseract-ocr tesseract-ocr-deu   (and the languages you need)\n"
    "  Or run ./setup.sh, which installs Tesseract and the OCR languages.\n"
    "Check the available languages with: tesseract --list-langs"
)

_COMMON_PATHS = ["/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract", "/usr/bin/tesseract"]

# program ISO code -> Tesseract code
CODES = {
    "de": "deu", "en": "eng", "es": "spa", "fr": "fra", "it": "ita", "pt": "por", "nl": "nld",
    "ca": "cat", "ru": "rus", "pl": "pol", "cs": "ces", "sv": "swe", "da": "dan", "no": "nor",
    "fi": "fin", "ro": "ron", "hu": "hun", "tr": "tur", "el": "ell", "he": "heb", "ar": "ara",
    "ja": "jpn", "ko": "kor", "zh-cn": "chi_sim", "zh-tw": "chi_tra", "la": "lat",
}
_INVERSE = {v: k for k, v in CODES.items()}

# Fraktur (gothic) models worth trying, per language, in order of preference
GOTHIC = {"de": ("frk", "deu_latf", "script/Fraktur")}

# candidate languages when the program needs to figure out the language of a scan
DETECTION_CANDIDATES = ("deu", "eng", "fra", "spa", "ita", "por", "nld", "lat")


class OCRError(ExtractionError):
    pass


def locate(exe: str | None = None) -> str:
    found = exe or os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
    if not found:
        found = next((c for c in _COMMON_PATHS if Path(c).exists()), None)
    if not found:
        raise OCRError(INSTALL_INSTRUCTIONS)
    return found


@lru_cache(maxsize=8)
def installed_languages(exe: str) -> frozenset:
    try:
        r = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OCRError(f"Could not run Tesseract ({exe}): {e}") from e
    lines = (r.stdout + "\n" + r.stderr).splitlines()
    return frozenset(
        l.strip() for l in lines
        if l.strip() and not l.lower().startswith(("list of available", "error", "warning"))
    )


def code_for(iso: str) -> str | None:
    return CODES.get(iso.lower())


def iso_from_code(tess: str) -> str | None:
    first = tess.split("+")[0]
    if first in _INVERSE:
        return _INVERSE[first]
    for iso, models in GOTHIC.items():
        if first in models:
            return iso
    return None


def missing(exe: str, spec: str) -> list[str]:
    inst = installed_languages(exe)
    return [p for p in spec.split("+") if p and p not in inst]


def verify(exe: str, spec: str) -> None:
    if not spec:
        raise OCRError("OCR language not set.")
    absent = missing(exe, spec)
    if absent:
        inst = sorted(i for i in installed_languages(exe) if i != "osd")
        raise OCRError(
            f"Tesseract does not have the language: {', '.join(absent)}.\n"
            f"Installed: {', '.join(inst) or '(none)'}.\n"
            "On macOS: brew install tesseract-lang   |   Linux: sudo apt install tesseract-ocr-<language>"
        )


def installed_gothic(exe: str, iso: str) -> list[str]:
    inst = installed_languages(exe)
    return [g for g in GOTHIC.get(iso, ()) if g in inst]


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}  # several processes in parallel, 1 thread each
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        raise OCRError(f"Tesseract took more than {timeout}s on a page.") from e
    except OSError as e:
        raise OCRError(f"Failed to run Tesseract: {e}") from e


def run_tsv(exe: str, image: Path, lang: str, psm: int = 3, dpi: int = 300, timeout: int = 600) -> list[list]:
    r = _run([exe, str(image), "stdout", "-l", lang, "--psm", str(psm), "--dpi", str(dpi), "tsv"], timeout)
    if r.returncode != 0:
        raise OCRError(f"Tesseract failed (code {r.returncode}): {(r.stderr or r.stdout)[-300:].strip()}")
    words: list[list] = []
    for line in r.stdout.splitlines()[1:]:
        c = line.split("\t", 11)
        if len(c) < 12 or c[0] != "5":
            continue
        text = c[11].strip()
        if not text:
            continue
        try:
            conf = float(c[10])
            if conf < 0:
                continue
            words.append([int(c[2]), int(c[3]), int(c[4]), int(c[6]), int(c[7]), int(c[8]), int(c[9]),
                          round(conf, 1), text])
        except ValueError:
            continue
    return words


def orientation(exe: str, image: Path, timeout: int = 120) -> tuple[int, float] | None:
    """(degrees to rotate clockwise, confidence) or None. Needs the 'osd' model."""
    if "osd" not in installed_languages(exe):
        return None
    try:
        r = _run([exe, str(image), "stdout", "--psm", "0"], timeout)
    except OCRError:
        return None
    out = r.stdout + "\n" + r.stderr
    rot = re.search(r"Rotate:\s*(\d+)", out)
    conf = re.search(r"Orientation confidence:\s*([\d.]+)", out)
    if not rot or not conf:
        return None
    return int(rot.group(1)), float(conf.group(1))


def mean_confidence(words: list[list]) -> float:
    """Mean confidence weighted by word length (0 if there are no words)."""
    total = sum(len(p[8]) for p in words)
    if not total:
        return 0.0
    return sum(p[7] * len(p[8]) for p in words) / total
