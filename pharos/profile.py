"""Translation profile: the context the program gives the AI (genre, register, rules, examples) and the prompts.

The texts sent to the model are in English (models follow them better); the conversation with the user
is handled by the UI layer.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .config import Config, prompt_code, language_name
from .glossary import Term
from .model import Block, NOTE, TITLE, VERSE

GENRES = {
    "occult": (
        "Occult / esotericism",
        "Esoteric/occult text. Keep the specialised vocabulary of the tradition (kabbalistic, alchemical, "
        "astrological, ritual and magical terms) exact and consistent; never replace technical terms with "
        "everyday synonyms. Keep a solemn, precise, initiatory register; do not explain or modernise.",
    ),
    "fiction": (
        "Fiction",
        "Fiction. Preserve the author's voice, diction, rhythm, sentence length, punctuation and period flavour, "
        "including archaisms and idiosyncrasies. Do not normalise, modernise, embellish or explain. Keep the "
        "narrator's and the characters' registers and the dialogue conventions.",
    ),
    "philosophy": (
        "Philosophy / essay",
        "Philosophical or essayistic text. Keep conceptual terminology literal and consistent; preserve the "
        "argument structure, long sentences and rhetorical devices; do not simplify.",
    ),
    "religious": (
        "Religious / spiritual",
        "Religious or spiritual text. Preserve sacred and liturgical terminology, the solemn tone and traditional "
        "phrasing; render names and technical terms consistently.",
    ),
    "academic": (
        "Academic / technical",
        "Academic or technical text. Neutral, precise register; keep terminology consistent and leave citations, "
        "numbers and references unchanged.",
    ),
    "other": ("Other / general", "Preserve the author's tone and style faithfully."),
}

DEFAULT_RULES = [
    "Translate faithfully and completely: do not omit, add, summarise, explain or modernise anything.",
    "Preserve the author's voice, sentence structure, rhythm, punctuation and rhetorical devices; "
    "do not simplify or smooth the text.",
    "Keep paragraph breaks and line breaks exactly as in the source.",
    "Keep proper names, quotations in other languages and technical terms without a good equivalent as in the "
    "source, unless the glossary says otherwise.",
    "Follow the glossary strictly: every glossary term must appear in the translation exactly as given.",
    "Output only the translation, with no comments, notes, quotation marks or preamble.",
]

_VARIANTS_EN = {
    "português do brasil": "Brazilian Portuguese",
    "portugues do brasil": "Brazilian Portuguese",
    "português de portugal": "European Portuguese",
    "portugues de portugal": "European Portuguese",
}


@dataclass
class Profile:
    genre: str = "other"
    work: str = ""
    register: str = GENRES["other"][1]
    rules: list = field(default_factory=lambda: list(DEFAULT_RULES))
    examples: list = field(default_factory=list)  # [{"source": ..., "translation": ...}]
    version: int = 1

    @classmethod
    def default(cls, genre: str = "other", work: str = "") -> "Profile":
        genre = genre if genre in GENRES else "other"
        return cls(genre=genre, work=work, register=GENRES[genre][1])

    def hash(self) -> str:
        base = json.dumps(
            [self.genre, self.work, self.register, self.rules, self.examples], ensure_ascii=False, sort_keys=True
        )
        return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Profile":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ----------------------------------------------------------------------------- prompts

def _variant_en(variant: str) -> str:
    return _VARIANTS_EN.get(variant.strip().lower(), variant.strip())


def _truncate(t: str, limit: int, from_end: bool = False) -> str:
    if len(t) <= limit:
        return t
    return ("…" + t[-limit:]) if from_end else (t[:limit] + "…")


def _type_hint(block: Block) -> str:
    if block.kind == VERSE:
        n = len([l for l in block.text.split("\n") if l.strip()])
        return f"This passage is verse: keep exactly {n} lines, one for each source line."
    if block.kind == TITLE:
        return "This passage is a heading: translate it as a heading, on a single line."
    if block.kind == NOTE:
        return "This passage is a footnote: translate it as a footnote and keep its numbering."
    return ""


def _instructions(cfg: Config, profile: Profile, terms: list[Term], block: Block, warning: str) -> list[str]:
    lines = []
    if cfg.target_variant:
        lines.append(f"Target variant: {_variant_en(cfg.target_variant)}.")
    if profile.work:
        lines.append(f"Work: {profile.work}.")
    if profile.register:
        lines.append(f"Genre and register: {profile.register}")
    if profile.rules:
        lines.append("Rules:")
        lines.extend(f"- {r}" for r in profile.rules)
    if terms:
        lines.append("Mandatory glossary for this passage (use these renderings exactly):")
        for t in terms:
            lines.append(f"- {t.source} -> {t.target}" + (f" ({t.note})" if t.note else ""))
    hint = _type_hint(block)
    if hint:
        lines.append(hint)
    if warning:
        lines.append(f"IMPORTANT: your previous answer was rejected: {warning} Fix this in the new translation.")
    return lines


def _context_block(context: list[tuple[str, str | None]]) -> list[str]:
    if not context:
        return []
    lines = [
        "Context (previous passages of the same book, provided ONLY for consistency of terms and tone; "
        "do not translate, repeat or mention them):"
    ]
    for source, translation in context:
        lines.append(f"- Source: {_truncate(source, 600, from_end=True)}")
        if translation:
            lines.append(f"  Translation: {_truncate(translation, 600, from_end=True)}")
    return lines


def _examples_block(profile: Profile) -> list[str]:
    examples = profile.examples[-4:]
    if not examples:
        return []
    lines = ["Examples of the desired translation style (approved by the translator):"]
    for e in examples:
        lines.append(f"Source: {_truncate(e['source'], 700)}")
        lines.append(f"Translation: {_truncate(e['translation'], 700)}")
    return lines


def build_messages(
    cfg: Config,
    profile: Profile,
    terms: list[Term],
    block: Block,
    context: list[tuple[str, str | None]],
    use_profile: bool,
    style: str,
    warning: str = "",
) -> list[dict]:
    """Builds the messages for Ollama.

    style "translategemma": a single user message in the model's official format, with the extra
    instructions between the role sentence and the request. style "chat": system + examples + user.
    Without a profile (use_profile=False) it returns the plain prompt, used as a comparison baseline.
    """
    src, tgt = language_name(cfg.source, "en"), language_name(cfg.target, "en")
    src_c, tgt_c = prompt_code(cfg.source), prompt_code(cfg.target)
    text = block.text

    if style == "translategemma":
        base = (
            f"You are a professional {src} ({src_c}) to {tgt} ({tgt_c}) translator. Your goal is to accurately "
            f"convey the meaning and nuances of the original {src} text while adhering to {tgt} grammar, "
            f"vocabulary, and cultural sensitivities.\n"
            f"Produce only the {tgt} translation, without any additional explanations or commentary."
        )
        request = f"Please translate the following {src} text into {tgt}:\n\n\n{text}"
        if not use_profile:
            return [{"role": "user", "content": f"{base} {request}"}]
        extras = (
            _instructions(cfg, profile, terms, block, warning)
            + _examples_block(profile)
            + _context_block(context)
        )
        body = base + ("\n\n" + "\n".join(extras) if extras else "") + "\n\n" + request
        return [{"role": "user", "content": body}]

    # chat style (Gemma 3, Qwen, Llama...)
    if not use_profile:
        system_msg = f"You are a professional translator from {src} to {tgt}. Output only the translation."
        return [{"role": "system", "content": system_msg}, {"role": "user", "content": text}]
    system_msg = "\n".join(
        [f"You are an expert literary translator from {src} to {tgt}."]
        + _instructions(cfg, profile, terms, block, warning)
        + ["Output only the translation."]
    )
    messages = [{"role": "system", "content": system_msg}]
    for e in profile.examples[-4:]:
        messages.append({"role": "user", "content": _truncate(e["source"], 700)})
        messages.append({"role": "assistant", "content": _truncate(e["translation"], 700)})
    context_txt = "\n".join(_context_block(context))
    user_msg = (context_txt + "\n\nText to translate:\n\n" if context_txt else "") + text
    messages.append({"role": "user", "content": user_msg})
    return messages


def model_style(cfg: Config) -> str:
    if cfg.prompt_style in ("translategemma", "chat"):
        return cfg.prompt_style
    return "translategemma" if "translategemma" in cfg.model.lower() else "chat"


# ----------------------------------------------------------------------------- automatic suggestion

def suggest_profile(ollama, cfg: Config, book, max_snippets: int = 5) -> Profile | None:
    """Asks the model for a profile proposal from snippets of the book. None if no valid JSON comes back."""
    from .model import PARAGRAPH

    paragraphs = [b for b in book.blocks() if b.kind == PARAGRAPH and len(b.text) >= 200]
    if not paragraphs:
        return None
    step = max(1, len(paragraphs) // max_snippets)
    samples = [_truncate(b.text, 700) for b in paragraphs[::step][:max_snippets]]
    genres = ", ".join(GENRES)
    request = (
        f"Book: {book.title or 'unknown'} by {book.author or 'unknown'}. Language: "
        f"{language_name(cfg.source, 'en')}.\nExcerpts:\n"
        + "\n---\n".join(samples)
        + "\n\nAnalyse these excerpts to help a translator keep the author's style. Answer with ONLY a JSON "
        f'object with the keys "genre" (one of: {genres}), "work" (one line: what the work is, its period and '
        'tradition), and "register" (2-3 sentences, in English: tone, vocabulary and stylistic traits that the '
        "translation must preserve)."
    )
    try:
        response = ollama.chat(
            cfg.model_for_analysis,
            [{"role": "user", "content": request}],
            temperature=0.2,
            num_ctx=cfg.num_ctx,
        )
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"\{.*\}", response, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    genre = str(d.get("genre", "other")).strip().lower()
    profile = Profile.default(genre if genre in GENRES else "other", str(d.get("work", "")).strip())
    register = str(d.get("register", "")).strip()
    if register:
        profile.register = register
    return profile
