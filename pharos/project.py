"""Project folder: everything about a book (original, extracted text, translations, outputs) stays together."""
from __future__ import annotations

import json
import re
import shutil
import unicodedata
from pathlib import Path

from .detection import IMAGE_EXT

SUBFOLDERS = ["original", "extracted", "ocr", "pages", "review", "translated", "preview", "output"]
INITIAL_STAGES = {
    "extraction": False, "language": False, "configured": False,
    "profile_approved": False, "translation_complete": False, "exported": False,
}


def slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()
    return t or "book"


class Project:
    def __init__(self, root: Path):
        self.root = Path(root)

    # paths
    def folder(self, name: str) -> Path:
        p = self.root / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def profile_path(self) -> Path:
        return self.root / "profile.json"

    @property
    def glossary_path(self) -> Path:
        return self.root / "glossary.tsv"

    @property
    def book_path(self) -> Path:
        return self.root / "extracted" / "book.json"

    @property
    def book_md_path(self) -> Path:
        return self.root / "extracted" / "book.md"

    @property
    def cache_path(self) -> Path:
        return self.root / "translated" / "cache.jsonl"

    @property
    def corrections_path(self) -> Path:
        return self.root / "translated" / "corrections.json"

    @property
    def report_path(self) -> Path:
        return self.root / "review" / "report.md"

    @property
    def ocr_dir(self) -> Path:
        return self.root / "ocr"          # OCR result per page (p0001.json...)

    @property
    def pages_dir(self) -> Path:
        return self.root / "pages"        # page images used in the review screen

    @property
    def ocr_review_path(self) -> Path:
        return self.root / "review" / "ocr_review.json"

    @property
    def ocr_ai_suggestions_path(self) -> Path:
        return self.root / "review" / "ocr_ai_suggestions.json"

    @property
    def ocr_quality_path(self) -> Path:
        return self.root / "review" / "ocr_quality.md"

    @property
    def original(self) -> Path | None:
        """The book file; for a set of images, the 'original' folder itself."""
        folder = self.root / "original"
        files = sorted(p for p in folder.glob("*") if p.is_file() and not p.name.startswith("."))
        if len(files) > 1 and all(p.suffix.lower() in IMAGE_EXT for p in files):
            return folder
        return files[0] if files else None

    # life cycle
    @classmethod
    def is_project(cls, path: Path) -> bool:
        return path.is_dir() and (path / "config.json").exists()

    @classmethod
    def open(cls, root: Path) -> "Project":
        return cls(root)

    @staticmethod
    def _base_name(file: Path) -> str:
        return slug(file.name) + "-book" if file.is_dir() else slug(file.stem)

    @classmethod
    def locate(cls, file: Path, base: Path) -> "Project | None":
        """A project already created for this file (same folder name and same original file)."""
        name = cls._base_name(file)
        for root in [base / name] + [base / f"{name}-{n}" for n in range(2, 10)]:
            if not cls.is_project(root):
                continue
            try:
                cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if cfg.get("original_file") == file.name:
                return cls(root)
        return None

    @classmethod
    def create(cls, file: Path, base: Path) -> "Project":
        name = cls._base_name(file)
        root = base / name
        n = 2
        while root.exists():
            root = base / f"{name}-{n}"
            n += 1
        for sub in SUBFOLDERS:
            (root / sub).mkdir(parents=True, exist_ok=True)
        if file.is_dir():  # set of images: copy only the images, keeping the names (order comes from them)
            for img in sorted(file.iterdir()):
                if img.is_file() and img.suffix.lower() in IMAGE_EXT:
                    shutil.copy2(img, root / "original" / img.name)
        else:
            shutil.copy2(file, root / "original" / file.name)
        p = cls(root)
        p.state_path.write_text(
            json.dumps({"stages": dict(INITIAL_STAGES)}, indent=2), encoding="utf-8"
        )
        return p

    # state
    def state(self) -> dict:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            d = {}
        stages = dict(INITIAL_STAGES)
        stages.update(d.get("stages", {}))
        d["stages"] = stages
        return d

    def mark(self, **stages: bool) -> None:
        d = self.state()
        d["stages"].update(stages)
        self.state_path.write_text(json.dumps(d, indent=2), encoding="utf-8")
