"""Edits made during the OCR review: storage, global replace and the session used by the web interface."""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from .ai_suggestions import AIError, AISuggestions, word_pattern

KINDS = {"paragraph", "title", "verse", "note", "caption", "separator"}
_MAX_TEXT = 200_000


def _clean_block(b: dict) -> dict | None:
    if not isinstance(b, dict):
        return None
    kind = b.get("kind") if b.get("kind") in KINDS else "paragraph"
    text = str(b.get("text", ""))[:_MAX_TEXT].replace("\r\n", "\n").replace("\r", "\n")
    if kind != "verse":
        text = " ".join(text.split())
    else:
        text = "\n".join(l.strip() for l in text.split("\n") if l.strip())
    if not text.strip():
        return None
    try:
        level = max(1, min(4, int(b.get("level", 1))))
    except (TypeError, ValueError):
        level = 1
    return {"kind": kind, "text": text, "level": level, "indent": True}


class Review:
    """Pages edited by hand (blocks) and pages marked as reviewed. Atomic write on every change."""

    def __init__(self, path: Path, ocr_hash: str):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data: dict = {"hash": ocr_hash, "pages": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if loaded.get("hash") == ocr_hash:
                    self.data = loaded
                else:  # the OCR was redone with other parameters: the old edits no longer apply
                    self.path.replace(self.path.with_suffix(".old.json"))
            except (json.JSONDecodeError, OSError):
                pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def edited(self, n: int) -> bool:
        with self.lock:
            return "blocks" in self.data["pages"].get(str(n), {})

    def reviewed(self, n: int) -> bool:
        with self.lock:
            return bool(self.data["pages"].get(str(n), {}).get("reviewed"))

    def blocks(self, n: int) -> list[dict] | None:
        with self.lock:
            p = self.data["pages"].get(str(n), {})
            return [dict(b) for b in p["blocks"]] if "blocks" in p else None

    def save(self, n: int, blocks: list[dict] | None, reviewed: bool | None = None) -> None:
        with self.lock:
            p = self.data["pages"].setdefault(str(n), {})
            if blocks is not None:
                p["blocks"] = [b for b in (_clean_block(x) for x in blocks) if b]
            if reviewed is not None:
                p["reviewed"] = bool(reviewed)
            self._save()

    def reviewed_pages(self) -> set[int]:
        with self.lock:
            return {int(n) for n, p in self.data["pages"].items() if p.get("reviewed")}

    def edited_pages(self) -> set[int]:
        with self.lock:
            return {int(n) for n, p in self.data["pages"].items() if "blocks" in p}


class Session:
    """Everything the review interface needs, behind a lock (the server handles several threads)."""

    def __init__(self, title: str, pages: dict[int, dict], auto: dict[int, list[dict]], scores: dict[int, dict],
                 suggestions: list[dict], review: Review, dir_img: Path, word_threshold: int = 70,
                 initial_filter: str = "flagged", ai: AISuggestions | None = None):
        self.title, self.pages, self.auto, self.scores = title, pages, auto, scores
        self.suggestions, self.review, self.dir_img = suggestions, review, Path(dir_img)
        self.ai = ai
        self.word_threshold, self.initial_filter = word_threshold, initial_filter
        self.lock = threading.RLock()

    def blocks(self, n: int) -> list[dict]:
        with self.lock:
            edited = self.review.blocks(n)
            return edited if edited is not None else [dict(b) for b in self.auto.get(n, [])]

    def index(self) -> dict:
        with self.lock:
            items = []
            for n in sorted(self.pages):
                p = self.scores.get(n, {})
                items.append({"n": n, "score": p.get("score", 0), "flagged": bool(p.get("flagged")),
                              "doubtful": p.get("doubtful", 0), "reviewed": self.review.reviewed(n),
                              "edited": self.review.edited(n), "reasons": p.get("reasons", []),
                              "ai": len(self.ai_for_page(n)) if self.ai else 0})
            return {"title": self.title, "pages": items, "filter": self.initial_filter,
                    "word_threshold": self.word_threshold, "suggestions_total": len(self.suggestions),
                    "ai": self.ai is not None}

    def page(self, n: int) -> dict | None:
        with self.lock:
            pg = self.pages.get(n)
            if pg is None:
                return None
            W, H = pg["width"], pg["height"]
            boxes = [[round(p[3] / W, 5), round(p[4] / H, 5), round(p[5] / W, 5), round(p[6] / H, 5), p[7], p[8]]
                     for p in pg["words"] if p[7] < self.word_threshold and any(c.isalnum() for c in p[8])]
            return {"n": n, "blocks": self.blocks(n), "boxes": boxes, "score": self.scores.get(n, {}),
                    "edited": self.review.edited(n), "reviewed": self.review.reviewed(n),
                    "suggestions": [s for s in self.suggestions if n in s["pages"]],
                    "ai": self.ai_for_page(n)}

    def save(self, n: int, blocks: list[dict], reviewed: bool | None) -> None:
        with self.lock:
            if n in self.pages:
                self.review.save(n, blocks, reviewed)

    def mark(self, n: int, reviewed: bool) -> None:
        with self.lock:
            if n in self.pages:
                self.review.save(n, None, reviewed)

    def replace(self, frm: str, to_: str, whole_word: bool = True, ignore_case: bool = False,
                apply_: bool = False) -> dict:
        """Replaces text on all pages. Without applying, it only counts."""
        if not frm:
            return {"occurrences": 0, "pages": 0, "applied": False}
        pattern = re.escape(frm)
        if whole_word:
            pattern = r"(?<![^\W_])" + pattern + r"(?![^\W_])"
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        total, affected = 0, []
        with self.lock:
            for n in sorted(self.pages):
                blocks = self.blocks(n)
                changed = False
                for b in blocks:
                    new, k = rx.subn(lambda _m: to_, b["text"])
                    if k:
                        total += k
                        changed = True
                        b["text"] = new
                if changed:
                    affected.append(n)
                    if apply_:
                        self.review.save(n, blocks, None)
        return {"occurrences": total, "pages": len(affected), "list": affected[:30], "applied": bool(apply_)}

    def final_blocks(self) -> dict[int, list[dict]]:
        with self.lock:
            return {n: self.blocks(n) for n in sorted(self.pages)}

    # -- AI suggestions (they only suggest; the person applies them, on the screen) ------------
    def _require_ai(self) -> AISuggestions:
        if self.ai is None:
            raise AIError("AI suggestions are not active in this session.")
        return self.ai

    def ai_for_page(self, n: int) -> list[dict]:
        """Suggestions not yet accepted or rejected and that still apply to the current page text."""
        if self.ai is None:
            return []
        with self.lock:
            texts = [b["text"] for b in self.blocks(n)]
        out = []
        for sg in self.ai.list_(n):
            rx = word_pattern(sg["from"])
            if any(rx.search(t) for t in texts):
                out.append(sg)
        return out

    def ai_state(self) -> dict:
        if self.ai is None:
            return {"available": False, "reason": "AI suggestions are not active in this session.",
                    "model": "", "warning": "", "work": {"active": False}, "totals": {}}
        return self.ai.state()

    def _ai_source(self, n: int) -> tuple[list[dict], list]:
        with self.lock:
            return self.blocks(n), self.pages[n]["words"]

    def ai_generate_page(self, n: int) -> dict:
        """Asks the model for a page's suggestions (blocks until done)."""
        ai = self._require_ai()
        if n not in self.pages:
            raise AIError("Page does not exist.")
        blocks, words = self._ai_source(n)
        ai.generate_page(n, blocks, words)
        return {"suggestions": self.ai_for_page(n), "info": ai.page_info(n)}

    def pages_for_ai(self, scope: str) -> list[int]:
        with self.lock:
            targets = []
            for n in sorted(self.pages):
                if self.review.reviewed(n) or (self.ai and self.ai.has(n)):
                    continue
                if scope == "flagged" and not self.scores.get(n, {}).get("flagged"):
                    continue
                if self.scores.get(n, {}).get("words", 1) < 8:
                    continue
                targets.append(n)
            return targets

    def ai_start_batch(self, scope: str) -> dict:
        ai = self._require_ai()
        if scope not in ("flagged", "all"):
            raise AIError("Invalid scope.")
        ok, reason = ai.availability()
        if not ok:
            raise AIError(reason)
        pages = self.pages_for_ai(scope)
        if not pages:
            return {"active": False, "total": 0, "done": 0, "suggestions": 0, "error": "", "scope": scope,
                    "current": None, "stop": False, "nothing": True}
        return ai.start_batch(pages, self._ai_source, scope)

    def ai_stop(self) -> None:
        if self.ai:
            self.ai.stop_()

    def ai_mark(self, ids: list, accepted: bool) -> None:
        self._require_ai().mark([i for i in ids if isinstance(i, str)][:500], accepted)
