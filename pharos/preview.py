"""Preview of the first pages: translates a snippet, shows it side by side and lets you refine tone, rules,
glossary and examples before spending hours on the whole book."""
from __future__ import annotations

import shutil
import webbrowser
from datetime import datetime

from . import glossary as gl
from .boundaries import POSSIBLE_SPLIT, load_suspects
from .config import Config, language_name
from .export import _esc, page_html
from .model import Block, Book, PARAGRAPH, TITLE
from .ollama import Ollama
from .profile import Profile, model_style
from .project import Project
from .translation import TranslationCache, Corrections, Translator
from .ui import Progress, UserExit, UI

MENU = """
What would you like to do?
  [a] Approve and translate the whole book
  [t] Adjust the tone / register (style description)
  [r] Add a fidelity rule
  [g] Add a term to the glossary
  [e] Fix a paragraph (becomes an example for the model and applies to the final version)
  [c] Compare with the translation without a profile (shows what the profile changes)
  [o] Try another snippet of the book
  [m] Change the model
  [s] Quit (the project is saved)"""


def select_snippet(book: Book, cfg: Config, offset: int = 0) -> tuple[list[Block], int]:
    """A snippet of ~N pages from the body of the book. Returns (blocks, long paragraphs used)."""
    all_blocks = list(book.blocks())
    long_ones = [i for i, b in enumerate(all_blocks) if b.kind == PARAGRAPH and len(b.text) >= 200]
    if not long_ones:
        return all_blocks[:12], 0
    start = long_ones[min(offset, len(long_ones) - 1)]
    if start > 0 and all_blocks[start - 1].kind == TITLE:
        start -= 1
    page0 = all_blocks[start].page
    word_goal = cfg.preview_pages * cfg.words_per_page
    blocks, words = [], 0
    for b in all_blocks[start:start + 60]:
        if blocks and page0 is not None and b.page is not None and b.page >= page0 + cfg.preview_pages:
            break
        if blocks and page0 is None and words >= word_goal:
            break
        blocks.append(b)
        words += len(b.text.split())
    used = sum(1 for b in blocks if b.kind == PARAGRAPH and len(b.text) >= 200)
    return blocks, used


def _html_lines(blocks, results, base, suspects=frozenset()):
    lines = []
    for n, b in enumerate(blocks, 1):
        r = results[b.id]
        lines.append({
            "n": n, "kind": b.kind, "orig": b.text, "trans": r.text,
            "alerts": r.alerts + ([POSSIBLE_SPLIT] if b.id in suspects else []),
            "base": base[b.id].text if base else None,
        })
    return lines


def _show_in_terminal(ui: UI, blocks, results, max_pairs: int = 3) -> None:
    ui.info()
    shown = 0
    for n, b in enumerate(blocks, 1):
        if b.kind != PARAGRAPH or shown >= max_pairs:
            continue
        shown += 1
        ui.info(f"[{n}] ORIGINAL:  {b.text[:320]}{'…' if len(b.text) > 320 else ''}")
        t = results[b.id].text
        ui.info(f"    TRANSLATION:  {t[:320]}{'…' if len(t) > 320 else ''}\n")
    alerts = sum(1 for b in blocks if set(results[b.id].alerts) - {"manually_corrected", "already_in_target"})
    if alerts:
        ui.warn(f"{alerts} passage(s) in the preview have an alert (see the badges in the HTML).")


def preview_cycle(proj: Project, cfg: Config, profile: Profile, ollama: Ollama, book: Book, ui: UI,
                  open_browser: bool = True) -> bool:
    """Returns True if the profile was approved. UserExit if the user quits."""
    version = 0
    offset = 0
    compare = False
    corrections = Corrections(proj.corrections_path)
    first = True

    while True:
        version += 1
        profile.version = version
        folder = proj.folder("preview") / f"v{version}"
        folder.mkdir(parents=True, exist_ok=True)
        profile.save(folder / "profile.json")
        profile.save(proj.profile_path)
        cfg.save(proj.config_path)
        if proj.glossary_path.exists():
            shutil.copy2(proj.glossary_path, folder / "glossary.tsv")

        cache = TranslationCache(proj.cache_path)
        translator = Translator(cfg, profile, proj.glossary_path, ollama, cache, corrections)
        blocks, used = select_snippet(book, cfg, offset)
        words = sum(len(b.text.split()) for b in blocks)
        ui.heading(f"Preview v{version}")
        ui.info(f"Translating {len(blocks)} passages (~{words} words) with {cfg.model}...")
        results = translator.translate_list(blocks, progress=Progress(ui, "Preview"))
        base = None
        if compare:
            ui.info("Also generating the translation without a profile, for comparison...")
            base = translator.translate_list(blocks, use_profile=False, progress=Progress(ui, "Base"))

        subtitle = (
            f"{_esc(language_name(cfg.source))} → {_esc(language_name(cfg.target))} · model {_esc(cfg.model)} · "
            f"profile v{version} ({_esc(profile.genre)}) · {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        )
        labels = (language_name(cfg.source).capitalize(), "Translation with profile", "Without profile (base)")
        html_path = folder / "preview.html"
        html_path.write_text(
            page_html(f"Preview v{version} — {book.title}", subtitle, _html_lines(blocks, results, base, load_suspects(proj.boundaries_path)), labels),
            encoding="utf-8",
        )
        _show_in_terminal(ui, blocks, results)
        ui.info(f"Full preview, side by side: {html_path}")
        if open_browser and first and ui.tty and not ui.auto:
            try:
                webbrowser.open(html_path.as_uri())
            except Exception:  # noqa: BLE001
                pass
        first = False

        if ui.auto:
            ui.info("Automatic mode: preview approved.")
            return True

        while True:  # menu; only leaves this loop when something requires redoing the preview
            ui.info(MENU)
            choice = ui.ask("Choice", "a").lower()[:1]
            if choice == "a":
                return True
            if choice == "s":
                raise UserExit("quit requested in the preview")
            if choice == "t":
                ui.info(f"\nCurrent tone: {profile.register}")
                new = ui.ask("New tone/register text (Enter keeps it)")
                if not new:
                    continue
                profile.register = new
                break
            if choice == "r":
                rule = ui.ask("New rule (e.g.: do not translate names of shops and orders)")
                if not rule:
                    continue
                profile.rules.append(rule)
                break
            if choice == "g":
                source = ui.ask("Term in the original")
                target = ui.ask("Required translation") if source else ""
                if not (source and target):
                    continue
                gl.add(proj.glossary_path, source, target, ui.ask("Note (optional)"))
                break
            if choice == "e":
                try:
                    n = int(ui.ask("Passage number (column #)"))
                    block = blocks[n - 1]
                except (ValueError, IndexError):
                    ui.warn("Invalid number.")
                    continue
                ui.info(f"\nORIGINAL:  {block.text}\nCURRENT:   {results[block.id].text}")
                new = ui.multiline("Write the corrected translation (end with a blank line; just Enter cancels):")
                if not new:
                    continue
                corrections.put(block, new)
                profile.examples.append({"source": block.text, "translation": new})
                break
            if choice == "c":
                compare = True
                break
            if choice == "o":
                offset += max(1, used)
                break
            if choice == "m":
                model = ui.ask("Model name in Ollama", cfg.model)
                if model == cfg.model:
                    continue
                if not ollama.has_model(model):
                    ui.warn(f"The model {model} is not installed (ollama pull {model}).")
                    continue
                cfg.model = model
                ui.info(f"Prompt style for this model: {model_style(cfg)}")
                break
            ui.warn("Unrecognised option.")
