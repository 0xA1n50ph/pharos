"""Project configuration and the language table."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path

# ISO code -> (name in Portuguese, name in English)
LANGUAGES: dict[str, tuple[str, str]] = {
    "de": ("alemão", "German"),
    "en": ("inglês", "English"),
    "es": ("espanhol", "Spanish"),
    "fr": ("francês", "French"),
    "it": ("italiano", "Italian"),
    "pt": ("português", "Portuguese"),
    "nl": ("holandês", "Dutch"),
    "ca": ("catalão", "Catalan"),
    "ru": ("russo", "Russian"),
    "pl": ("polonês", "Polish"),
    "cs": ("tcheco", "Czech"),
    "sv": ("sueco", "Swedish"),
    "da": ("dinamarquês", "Danish"),
    "no": ("norueguês", "Norwegian"),
    "fi": ("finlandês", "Finnish"),
    "ro": ("romeno", "Romanian"),
    "hu": ("húngaro", "Hungarian"),
    "tr": ("turco", "Turkish"),
    "el": ("grego", "Greek"),
    "he": ("hebraico", "Hebrew"),
    "ar": ("árabe", "Arabic"),
    "ja": ("japonês", "Japanese"),
    "ko": ("coreano", "Korean"),
    "zh-cn": ("chinês (simplificado)", "Chinese"),
    "zh-tw": ("chinês (tradicional)", "Chinese"),
    "la": ("latim", "Latin"),
}

# codes used in the TranslateGemma prompt when they differ from the plain ISO code
_PROMPT_CODE = {"zh-cn": "zh-Hans", "zh-tw": "zh-Hant"}


def language_name(code: str, lang: str = "en") -> str:
    pair = LANGUAGES.get(code.lower())
    if not pair:
        return code
    return pair[0] if lang == "pt" else pair[1]


def prompt_code(code: str) -> str:
    return _PROMPT_CODE.get(code.lower(), code)


def normalize_code(text: str) -> str | None:
    """Accepts 'pt', 'PT', 'português', 'portugues', 'ingles'... and returns the ISO code."""
    t = text.strip().lower()
    if t in LANGUAGES:
        return t
    unaccented = (
        t.replace("ê", "e").replace("é", "e").replace("ã", "a").replace("á", "a")
        .replace("ç", "c").replace("ô", "o").replace("í", "i")
    )
    for code, (pt, en) in LANGUAGES.items():
        pt_unaccented = (
            pt.replace("ê", "e").replace("é", "e").replace("ã", "a").replace("á", "a")
            .replace("ç", "c").replace("ô", "o").replace("í", "i")
        )
        if unaccented in (pt_unaccented, en.lower()):
            return code
    return None


@dataclass
class Config:
    original_file: str = ""
    title: str = ""
    author: str = ""
    source: str = ""
    target: str = "pt"
    target_variant: str = "português do Brasil"
    model: str = "translategemma:12b"
    analysis_model: str = ""          # empty = use the same model as the translation
    ollama_url: str = "http://localhost:11434"
    prompt_style: str = "auto"        # auto | translategemma | chat
    words_per_page: int = 300
    preview_pages: int = 3
    context_blocks: int = 2           # previous paragraphs sent as context (0 = disabled)
    max_block_chars: int = 3000       # larger paragraphs are translated in parts, at sentence boundaries
    max_attempts: int = 3
    temperature: float = 0.2
    num_ctx: int = 8192
    validate_language: bool = True
    # OCR (scanned books)
    ocr_language: str = ""            # Tesseract language(s), e.g. deu, frk, deu+eng (empty = decide during extraction)
    ocr_psm: int = 3                  # Tesseract page segmentation mode (3 = automatic)
    ocr_dpi: int = 300
    ocr_processes: int = 0            # 0 = automatic (cores - 1, at most 8)
    ocr_double: str = "auto"          # auto | yes | no: images with two pages side by side
    ocr_double_applied: str = ""      # what was decided on the last read (yes | no); empty = not decided yet
    ocr_use_layer: bool = False       # PDF with embedded text: use that layer instead of redoing the OCR
    ocr_word_threshold: int = 70      # confidence below this = doubtful word
    ocr_page_threshold: int = 85      # score below this = page flagged for review
    ocr_model: str = ""               # model that suggests OCR fixes during review (empty = an installed general model)
    formats: list = field(default_factory=lambda: ["epub", "docx", "md", "html"])

    @property
    def model_for_analysis(self) -> str:
        return self.analysis_model or self.model

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})

    def save(self, path: Path) -> None:
        Path(path).write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )
