"""Translation engine: context, validation, retries and cache (to resume where it stopped)."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import glossary as gl
from .config import Config
from .language import detect_snippet
from .model import Block, Book, NOTE, PARAGRAPH, TRANSLATABLE_TYPES, TITLE, VERSE
from .ollama import Ollama, OllamaError
from .profile import Profile, build_messages, model_style

_CJK = {"ja", "ko", "zh-cn", "zh-tw"}
_RATIO = (0.5, 2.2)

_RE_PREAMBLE = re.compile(
    r"^(here(?:'s| is)[^\n]{0,80}:|aqui est[aá][^\n]{0,80}:|claro[,!][^\n]{0,80}:|translation:|"
    r"tradu[cç][aã]o:|traducci[oó]n:|[uü]bersetzung:)\s*\n?",
    re.IGNORECASE,
)


@dataclass
class Result:
    text: str
    alerts: list = field(default_factory=list)
    attempts: int = 0
    from_cache: bool = False


class TranslationCache:
    """One JSON per line; each translation is written immediately, so Ctrl+C loses nothing."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue  # last line cut off by an interruption
                self.data[d["key"]] = d

    def get(self, key: str) -> dict | None:
        return self.data.get(key)

    def put(self, key: str, block_id: str, result: Result) -> None:
        d = {"key": key, "id": block_id, "translation": result.text,
             "alerts": result.alerts, "attempts": result.attempts}
        self.data[key] = d
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


class Corrections:
    """Translations fixed by hand during the preview: always apply, in any version of the profile."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, str] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.data = {}

    @staticmethod
    def _key(block: Block) -> str:
        return block.id + ":" + hashlib.sha1(block.text.encode("utf-8")).hexdigest()[:10]

    def get(self, block: Block) -> str | None:
        return self.data.get(self._key(block))

    def put(self, block: Block, text: str) -> None:
        self.data[self._key(block)] = text
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")


INFORMATIONAL_ALERTS = {"already_in_target", "manually_corrected"}


def split_into_sentences(text: str, limit: int) -> list[str]:
    """Splits a long text into chunks of up to `limit` characters, preferring sentence boundaries."""
    parts: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?…»”\"])\s+", text):
        while len(sentence) > limit:  # huge sentence, no punctuation: cut at the nearest space
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if current:
                parts.append(current)
                current = ""
            parts.append(sentence[:cut])
            sentence = sentence[cut:].lstrip()
        if current and len(current) + 1 + len(sentence) > limit:
            parts.append(current)
            current = sentence
        else:
            current = (current + " " + sentence).strip()
    if current:
        parts.append(current)
    return parts


def _clean_output(raw: str, block: Block) -> str:
    t = raw.strip()
    t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
    t = re.sub(r"\n?```$", "", t).strip()
    t = _RE_PREAMBLE.sub("", t).strip()
    if len(t) > 2 and t[0] in "\"“«" and t[-1] in "\"”»" and block.text[:1] not in "\"“«„»'‘":
        t = t[1:-1].strip()
    return t


class Translator:
    def __init__(self, cfg: Config, profile: Profile, glossary_path: Path, ollama: Ollama, cache: TranslationCache,
                 corrections: Corrections | None = None):
        self.cfg = cfg
        self.profile = profile
        self.corrections = corrections
        self.glossary = gl.load(glossary_path)
        self.glossary_hash = hashlib.sha1(gl.file_text(glossary_path).encode()).hexdigest()[:12]
        self.ollama = ollama
        self.cache = cache
        self.style = model_style(cfg)

    # -- cache keys ----------------------------------------------------------------------------
    def key(self, block: Block, use_profile: bool = True) -> str:
        parts = [block.text, block.kind, self.cfg.source, self.cfg.target, self.cfg.model, self.style]
        if use_profile:
            parts += ["profile", self.profile.hash(), self.glossary_hash, self.cfg.target_variant,
                      str(self.cfg.context_blocks)]
        else:
            parts += ["base"]
        return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()

    # -- what can be resolved without calling the model ---------------------------------------
    def resolve(self, block: Block, use_profile: bool = True) -> Result | None:
        if block.kind not in TRANSLATABLE_TYPES or not any(c.isalpha() for c in block.text):
            return Result(block.text, [], 0, True)
        if use_profile and self.corrections:
            corrected = self.corrections.get(block)
            if corrected:
                return Result(corrected, ["manually_corrected"], 0, True)
        hit = self.cache.get(self.key(block, use_profile))
        if hit:
            return Result(hit["translation"], hit["alerts"], hit["attempts"], True)
        if self.cfg.validate_language and self.cfg.source != self.cfg.target and len(block.text) >= 80:
            r = detect_snippet(block.text)
            if r and r[0] == self.cfg.target and r[1] >= 0.8:
                return Result(block.text, ["already_in_target"], 0, True)
        return None

    # -- validation ----------------------------------------------------------------------------
    def _validate(self, block: Block, text: str, terms: list[gl.Term]) -> list[tuple[str, str]]:
        p: list[tuple[str, str]] = []
        cfg = self.cfg
        if not text.strip():
            return [("empty", "the answer was empty.")]
        if block.kind != VERSE and "\n" in text.strip():
            p.append(("breaks", "the answer has extra line breaks or extra paragraphs; output a single paragraph."))
        if block.kind == VERSE:
            n_src = len([l for l in block.text.split("\n") if l.strip()])
            n_dst = len([l for l in text.split("\n") if l.strip()])
            if n_src != n_dst:
                p.append(("lines", f"the source has {n_src} lines but the answer has {n_dst}."))
        if (len(block.text) >= 80 and cfg.source not in _CJK and cfg.target not in _CJK
                and block.kind != TITLE):
            ratio = len(text) / max(1, len(block.text))
            if ratio < _RATIO[0]:
                p.append(("length", "the answer is much shorter than the source; something was omitted."))
            elif ratio > _RATIO[1]:
                p.append(("length", "the answer is much longer than the source; something was added."))
        if cfg.source != cfg.target:
            if len(block.text) >= 40 and text.strip() == block.text.strip():
                p.append(("not_translated", "the answer is identical to the source text."))
            elif cfg.validate_language and len(text) >= 80:
                r = detect_snippet(text)
                if r and r[0] == cfg.source and r[0] != cfg.target and r[1] >= 0.7:
                    p.append(("language", f"the answer is still in the source language; write it in {cfg.target}."))
        missing = gl.missing(terms, text)
        if missing:
            listed = "; ".join(f"{t.source} -> {t.target}" for t in missing)
            p.append(("glossary", f"glossary terms not applied: {listed}."))
        return p

    # -- model call ----------------------------------------------------------------------------
    def translate(self, block: Block, context: list[tuple[str, str | None]] | None = None,
                  use_profile: bool = True) -> Result:
        ready = self.resolve(block, use_profile)
        if ready is not None:
            return ready
        if block.kind in (PARAGRAPH, NOTE) and len(block.text) > self.cfg.max_block_chars:
            # huge paragraph (bad OCR, TXT without breaks): translate in parts, at sentence boundaries
            parts = split_into_sentences(block.text, self.cfg.max_block_chars)
            rs = [
                self._translate_one(Block(f"{block.id}.{i}", block.kind, p, block.page), context, use_profile, False)
                for i, p in enumerate(parts, 1)
            ]
            result = Result(
                " ".join(r.text for r in rs), sorted({a for r in rs for a in r.alerts}), max(r.attempts for r in rs)
            )
            self.cache.put(self.key(block, use_profile), block.id, result)
            return result
        return self._translate_one(block, context, use_profile, True)

    def _translate_one(self, block: Block, context, use_profile: bool, cache_it: bool) -> Result:
        cfg = self.cfg
        terms = gl.terms_in_text(self.glossary, block.text) if use_profile else []
        context = context if (use_profile and cfg.context_blocks) else []
        warning = ""
        best: tuple[int, str, list[str]] | None = None
        for attempt in range(1, cfg.max_attempts + 1):
            messages = build_messages(
                cfg, self.profile, terms, block, context or [], use_profile, self.style, warning
            )
            raw = self.ollama.chat(cfg.model, messages, cfg.temperature, cfg.num_ctx)
            text = _clean_output(raw, block)
            problems = self._validate(block, text, terms)
            if not problems:
                result = Result(text, [], attempt)
                if cache_it:
                    self.cache.put(self.key(block, use_profile), block.id, result)
                return result
            if best is None or len(problems) < best[0]:
                best = (len(problems), text, [c for c, _ in problems])
            warning = " ".join(d for _, d in problems)

        assert best is not None
        _, text, codes = best
        if block.kind != VERSE:
            text = " ".join(text.split())
        if not text.strip():  # nothing usable: keep the original, flagged for review
            text, codes = block.text, codes + ["failed"]
        result = Result(text, sorted(set(codes + ["review"])), cfg.max_attempts)
        if cache_it:
            self.cache.put(self.key(block, use_profile), block.id, result)
        return result

    def translate_list(self, blocks: list[Block], use_profile: bool = True, progress=None) -> dict[str, Result]:
        """Translates in order, passing the previous paragraphs as context."""
        results: dict[str, Result] = {}
        history: list[tuple[str, str | None]] = []
        for i, b in enumerate(blocks, 1):
            n = self.cfg.context_blocks
            context = history[-n:] if (n and use_profile and b.kind in (PARAGRAPH, NOTE, VERSE)) else []
            r = self._with_retry(b, context, use_profile)
            results[b.id] = r
            if b.kind in (PARAGRAPH, VERSE):
                history.append((b.text, r.text))
            elif b.kind == TITLE and b.level <= 1:
                history.clear()
            if progress:
                progress(i, len(blocks), b, r)
        return results

    def _with_retry(self, block, context, use_profile) -> Result:
        """If Ollama goes down mid-run, wait and try again before giving up (what's done is saved)."""
        for wait in (5, 15, 45, None):
            try:
                return self.translate(block, context, use_profile)
            except OllamaError:
                if wait is None:
                    raise
                time.sleep(wait)
        raise AssertionError("unreachable")


def fetch_translations(book: Book, translator: Translator) -> tuple[dict[str, str], dict[str, Result]]:
    """Reads from the cache what has already been translated (without calling the model). Missing -> keep the original."""
    texts: dict[str, str] = {}
    results: dict[str, Result] = {}
    for b in book.blocks():
        r = translator.resolve(b)
        if r is None:
            r = Result(b.text, ["pending"], 0)
        texts[b.id] = r.text
        results[b.id] = r
    return texts, results


def alerts_report(book: Book, results: dict[str, Result]) -> str:
    """Markdown with the passages that deserve a human review."""
    lines = ["# Review report", ""]
    counts: dict[str, int] = {}
    items = []
    for ch in book.chapters:
        for b in ch.blocks:
            r = results.get(b.id)
            if r and set(r.alerts) - INFORMATIONAL_ALERTS:
                items.append((ch.title or "(start)", b, r))
                for a in r.alerts:
                    counts[a] = counts.get(a, 0) + 1
    if not items:
        lines.append("No alerts: every passage passed the automatic validation.")
        return "\n".join(lines) + "\n"
    lines.append("Alert summary: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    lines.append("")
    lines.append("`review` = attempts exhausted and the best version was kept; `pending` = not translated yet.")
    lines.append("")
    for ch_title, b, r in items:
        lines.append(f"## {b.id} — {ch_title}" + (f" (p. {b.page})" if b.page else ""))
        lines.append(f"Alerts: {', '.join(r.alerts)}")
        lines.append("")
        lines.append("> **Original:** " + b.text.replace("\n", " ")[:600])
        lines.append("")
        lines.append("> **Translation:** " + r.text.replace("\n", " ")[:600])
        lines.append("")
    return "\n".join(lines) + "\n"
