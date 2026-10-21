"""Glossary as TSV: source_term <TAB> required_translation <TAB> note (optional).

Lines starting with # are comments. The program injects into the prompt only the terms present in each
passage and then checks that the translation applied them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

HEADER = (
    "# Glossary: source term <TAB> required translation <TAB> note (optional)\n"
    "# Example:\tSephiroth\tSefirot\tHebrew name, usual transliteration in Portuguese\n"
)


@dataclass(frozen=True)
class Term:
    source: str
    target: str
    note: str = ""


def ensure_file(path: Path) -> None:
    if not path.exists():
        path.write_text(HEADER, encoding="utf-8")


def load(path: Path) -> list[Term]:
    if not path.exists():
        return []
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) >= 2 and parts[0] and parts[1]:
            terms.append(Term(parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
    return terms


def add(path: Path, source: str, target: str, note: str = "") -> None:
    ensure_file(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{source.strip()}\t{target.strip()}\t{note.strip()}\n")


def file_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


@lru_cache(maxsize=2048)
def _regex(source: str) -> re.Pattern:
    # accepts inflections at the end of the word (Engel -> Engels, Engeln)
    return re.compile(r"(?<!\w)" + re.escape(source) + r"\w*", re.IGNORECASE | re.UNICODE)


def terms_in_text(glossary: list[Term], text: str) -> list[Term]:
    return [t for t in glossary if _regex(t.source).search(text)]


def missing(terms: list[Term], translation: str) -> list[Term]:
    lower = translation.lower()
    return [t for t in terms if t.target.lower() not in lower]
