#!/usr/bin/env python3
"""Pharos — book translator with a local AI (Ollama).

Input:  PDF (with text or scanned), a folder of page images, EPUB, TXT/MD and, with Calibre installed,
        MOBI/AZW3/FB2/DOCX... Scanned books go through OCR (Tesseract) and a review screen.
Output: the project folder with extracted text, translations, a review report and EPUB/DOCX/MD/HTML.

Examples:
    python pharos.py my_book.pdf
    python pharos.py book_scan.pdf --source de          # OCR in German (tests gothic type on its own)
    python pharos.py page_photos/ --ocr-lang frk          # a folder of images, OCR in Fraktur
    python pharos.py my_book.epub --target en --model gemma3:12b
    python pharos.py my-book/            # resumes an interrupted project
    python pharos.py my-book/ --review-ocr   # reopens the OCR review of an already-extracted project
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from pharos import __version__
from pharos.config import Config, LANGUAGES, language_name, normalize_code
from pharos.detection import file_type
from pharos.export import FORMATS, export_all
from pharos.extractors import extract
from pharos.extractors.common import ExtractionError
from pharos.glossary import load as load_glossary, ensure_file
from pharos.language import detect_book
from pharos.model import Book, book_to_markdown
from pharos.ollama import Ollama, OllamaError
from pharos.profile import GENRES, Profile, suggest_profile
from pharos.preview import preview_cycle
from pharos.project import Project
from pharos.translation import TranslationCache, Corrections, Translator, fetch_translations, alerts_report
from pharos.ui import Progress, UserExit, UI

TYPE_NAMES = {
    "pdf": "PDF", "epub": "EPUB", "txt": "plain text/Markdown", "images": "page images (scan)",
    "convertible": "convertible format (via Calibre)", "unknown": "unknown",
}
PDF_TYPE_NAMES = {
    "text": "book with selectable text", "scan": "scan (image pages)",
    "mixed": "mixed (part text, part scan)", "empty": "no usable content",
}


def read_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="pharos",
        description="Translates books (PDF, EPUB, TXT...) with a local Ollama model, with a preview and style tuning.",
    )
    p.add_argument("path", help="the book file, or a project folder to resume")
    p.add_argument("--source", help="book language (e.g.: de, es, en); without it the program detects and confirms")
    p.add_argument("--target", help="translation language (e.g.: pt, en); without it the program asks")
    p.add_argument("--model", help="Ollama model (default: translategemma:12b)")
    p.add_argument("--analysis-model", help="model used only to suggest the profile (default: same as the translation)")
    p.add_argument("--ollama", help="Ollama URL (default: http://localhost:11434)")
    p.add_argument("--output", help="folder where the project is created (default: current folder)")
    p.add_argument("--formats", help="output formats separated by commas: epub,docx,md,html")
    p.add_argument("--glossary", help="TSV file: term<TAB>translation<TAB>note")
    p.add_argument("--genre", help=f"genre of the work: {', '.join(GENRES)}")
    p.add_argument("--variant", help='variant of the target language (default for pt: "português do Brasil")')
    p.add_argument("--preview-pages", type=int, help="pages of the preview (default: 3)")
    p.add_argument("--no-context", action="store_true", help="do not send previous paragraphs as context")
    p.add_argument("--no-preview", action="store_true", help="skip the preview and translate directly")
    p.add_argument("--yes", action="store_true", help="accept the suggestions without asking (useful in batch)")
    p.add_argument("--export-only", action="store_true", help="translate nothing more; just re-export what is in the cache")
    p.add_argument("--extract-only", action="store_true",
                   help="only extract the text (includes OCR and review) and stop; no translation, no Ollama needed")
    ocr = p.add_argument_group("OCR (scanned books)")
    ocr.add_argument("--ocr-lang", help="Tesseract language(s)/model, e.g.: deu, frk, deu_latf, deu+eng "
                                        "(default: derived from --source or detected; in German it tests gothic type)")
    ocr.add_argument("--review", choices=["flagged", "all", "none"],
                     help="OCR review on screen: only flagged pages, all or none (default: asks)")
    ocr.add_argument("--review-ocr", action="store_true",
                     help="reopens the OCR review of an already-extracted project (re-translates only what changed)")
    ocr.add_argument("--ocr-force", action="store_true", help="redo the OCR even if there is a saved result")
    ocr.add_argument("--double-pages", choices=["auto", "yes", "no"],
                     help="images with two pages side by side: split them? (default: auto)")
    ocr.add_argument("--use-embedded-layer", action="store_true",
                     help="scanned PDF that already has embedded text: use that text instead of redoing the OCR")
    ocr.add_argument("--ocr-images", action="store_true",
                     help="text PDF with a few image-only pages: read those pages with OCR (default: left out)")
    ocr.add_argument("--ocr-dpi", type=int, help="PDF render resolution for the OCR (default: 300)")
    ocr.add_argument("--ocr-processes", type=int, help="pages read in parallel (default: automatic)")
    ocr.add_argument("--ocr-model", help="Ollama model that SUGGESTS OCR fixes in the review screen "
                                         "(default: an installed general model, like gemma3; the translation one is not ideal)")
    ocr.add_argument("--ai-suggestions", choices=["flagged", "all"],
                     help="already ask the model for OCR fix suggestions in the background when the review opens "
                          "(without it, use the on-screen button; nothing is applied without you accepting)")
    ocr.add_argument("--no-browser", action="store_true",
                     help="do not open the browser in the review; just show the address (useful over SSH)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


# ----------------------------------------------------------------------------- stages

def stage_extraction(proj: Project, cfg: Config, ui: UI, args) -> Book:
    if proj.state()["stages"]["extraction"] and proj.book_path.exists():
        return Book.load(proj.book_path)

    original = proj.original
    if original is None:
        raise ExtractionError("I couldn't find the original file in the project folder.")
    ui.heading("1. File")
    kind = file_type(original)
    name = original.name + ("/" if original.is_dir() else "")
    ui.info(f"{name} — detected format: {TYPE_NAMES[kind]}")
    if kind == "unknown":
        raise ExtractionError("Format not recognised. Use PDF, EPUB, TXT or page images "
                              "(or install Calibre for MOBI/AZW3/DOCX).")

    route = None  # None = direct text extraction; otherwise {"ocr": [indices] | None (all), "text": {pages}}
    if kind == "images":
        ui.info("Page images: the text comes out via OCR, with a review step before the translation.")
        route = {"ocr": None, "text": set()}
    elif kind == "pdf":
        from pharos.extractors import pdf_text

        ui.info("Analysing the pages (selectable text or scan)...")
        a = pdf_text.analyze(original)
        ui.info(f"{a['summary']}\nClassification: {PDF_TYPE_NAMES[a['type']]}")
        ocr_stage = _ocr_module()
        route = ocr_stage.route_pdf(original, cfg, ui, args)
        if route is None:
            if a["type"] == "mixed":
                ui.warn("Some pages are image only; they will be left out (the list goes to review/pages_without_text.txt).")
            if not ui.confirm("Extract the text directly from the PDF?", True):
                if ui.confirm("Use OCR (Tesseract) instead?", False):
                    route = {"ocr": None, "text": set()}
                else:
                    raise UserExit("extraction refused")

    if route is not None:
        ocr_stage = _ocr_module()
        n_ocr = len(route["ocr"]) if route["ocr"] is not None else "all"
        n_text = len(route["text"])
        ui.info(f"Plan: OCR on {n_ocr} page(s)" + (f" and direct text reading on {n_text}" if n_text else "") + ".")
        if not ui.confirm("Proceed with the OCR?", True):
            raise UserExit("extraction refused")
        book, report = ocr_stage.extract_scan(proj, cfg, ui, args, original, route["ocr"], route["text"])
    else:
        def progress(i: int, total: int) -> None:
            if total >= 20 and (i % max(1, total // 10) == 0 or i == total):
                ui.info(f"  reading... {i}/{total}")

        ui.info("Extracting the content...")
        book, report = extract(original, kind, progress)
    if not book.title:
        book.title = original.stem
    words = book.total_words()
    if words < 200:
        raise ExtractionError(f"Only {words} words extracted; the file seems empty or protected.")
    book.save(proj.book_path)
    proj.book_md_path.write_text(book_to_markdown(book), encoding="utf-8")
    if report.get("pages_without_text"):
        (proj.folder("review") / "pages_without_text.txt").write_text(
            "Pages without selectable text (scan), ignored during extraction:\n"
            + ", ".join(map(str, report["pages_without_text"])) + "\n", encoding="utf-8")
    n_blocks = sum(len(c.blocks) for c in book.chapters)
    ui.info(f"Extracted: {len(book.chapters)} chapter(s), {n_blocks} passages, {words} words.")
    if report.get("headers_removed"):
        ui.info("Headers/footers removed: " + "; ".join(report["headers_removed"][:4]))
    if report.get("pages_without_text"):
        ui.warn(f"{len(report['pages_without_text'])} page(s) without text were ignored.")
    ui.info(f"Text in the original language: {proj.book_md_path}")
    if original.is_file():  # for a set of images, the folder name given when the project was created applies
        cfg.original_file = original.name
    cfg.save(proj.config_path)
    proj.mark(extraction=True)
    return book


def _ocr_module():
    try:
        from pharos import ocr_stage
    except ImportError as e:  # Pillow, numpy or pypdfium2 missing
        raise ExtractionError(f"OCR needs libraries that are not installed ({e}).\n"
                              "Run: pip install pillow numpy pypdfium2") from e
    return ocr_stage


def stage_language(proj: Project, cfg: Config, book: Book, ui: UI, args) -> None:
    if proj.state()["stages"]["language"] and cfg.source:
        return
    ui.heading("2. Book language")
    if args.source:
        code = normalize_code(args.source)
        if not code:
            raise ExtractionError(f"Source language not recognised: {args.source}")
    else:
        dist = detect_book(book)
        default = book.language if book.language in LANGUAGES else ""
        if dist:
            main, share = dist[0]
            others = [f"{language_name(l)} {round(p * 100)}%" for l, p in dist[1:] if p >= 0.08]
            ui.info(f"Detected language: {language_name(main)} ({round(share * 100)}% of the sampled passages)"
                    + (f"; also present: {', '.join(others)}" if others else ""))
            if book.language and book.language != main:
                ui.warn(f"The file metadata indicates '{book.language}'.")
            default = main
        else:
            ui.warn("I couldn't detect the language automatically.")
        while True:
            resp = ui.ask("Source language (Enter confirms; or type another code/name)", default)
            code = normalize_code(resp) if resp else None
            if code:
                break
            if ui.auto:
                raise ExtractionError("Source language undefined; use --source.")
            ui.warn(f"I didn't recognise '{resp}'. Known codes: {', '.join(sorted(LANGUAGES))}")
    cfg.source = code
    book.language = code
    book.save(proj.book_path)
    cfg.save(proj.config_path)
    proj.mark(language=True)


def stage_config(proj: Project, cfg: Config, book: Book, ui: UI, args) -> None:
    if proj.state()["stages"]["configured"]:
        return
    ui.heading("3. Configuration")
    cfg.title = ui.ask("Book title", book.title)
    cfg.author = ui.ask("Author (optional)", book.author)
    book.title, book.author = cfg.title, cfg.author
    book.save(proj.book_path)

    if args.target:
        code = normalize_code(args.target)
        if not code:
            raise ExtractionError(f"Target language not recognised: {args.target}")
    else:
        while True:
            resp = ui.ask("Translate into which language? (pt = Portuguese, en = English, or another code)", "pt")
            code = normalize_code(resp)
            if code and code != cfg.source:
                break
            if ui.auto:
                raise ExtractionError("Target language invalid or equal to the source.")
            ui.warn("Language not recognised or the same as the book's.")
    cfg.target = code
    cfg.target_variant = args.variant if args.variant is not None else ("português do Brasil" if code == "pt" else "")

    if args.glossary:
        src = Path(args.glossary).expanduser()
        if not src.exists():
            raise ExtractionError(f"Glossary not found: {src}")
        shutil.copy2(src, proj.glossary_path)
    else:
        path = "" if ui.auto else ui.ask("Glossary (.tsv: term<TAB>translation<TAB>note) — path, or Enter to start empty")
        if path and Path(path).expanduser().exists():
            shutil.copy2(Path(path).expanduser(), proj.glossary_path)
        else:
            ensure_file(proj.glossary_path)
    ui.info(f"Glossary: {len(load_glossary(proj.glossary_path))} term(s) in {proj.glossary_path.name}")

    if args.formats:
        raw = args.formats
    else:
        raw = ui.ask(f"Output formats ({', '.join(FORMATS)})", ",".join(cfg.formats))
    formats = [f.strip().lower() for f in raw.split(",") if f.strip()]
    invalid = [f for f in formats if f not in FORMATS]
    if invalid or not formats:
        raise ExtractionError(f"Invalid output format(s): {', '.join(invalid) or '(empty)'}")
    cfg.formats = formats
    cfg.save(proj.config_path)
    proj.mark(configured=True)


def check_ollama(cfg: Config, ollama: Ollama) -> None:
    try:
        ollama.models()
    except OllamaError as e:
        raise OllamaError(f"{e}\nOpen the Ollama app (or run `ollama serve`) and try again.") from e
    for name in {cfg.model, cfg.model_for_analysis}:
        if not ollama.has_model(name):
            raise OllamaError(f"The model '{name}' is not installed in Ollama. Run: ollama pull {name}  (or ./setup.sh)")


def stage_profile(proj: Project, cfg: Config, book: Book, ui: UI, ollama: Ollama, args) -> Profile:
    if proj.state()["stages"]["profile_approved"] and proj.profile_path.exists():
        return Profile.load(proj.profile_path)

    ui.heading("4. Translation profile and preview")
    check_ollama(cfg, ollama)

    if proj.profile_path.exists():
        profile = Profile.load(proj.profile_path)
        ui.info("Using the profile already saved in this project.")
    else:
        profile = None
        if args.genre:
            g = args.genre.strip().lower()
            if g not in GENRES:
                raise ExtractionError(f"Unknown genre: {g}. Options: {', '.join(GENRES)}")
            profile = Profile.default(g)
        elif ui.auto or ui.confirm("Would you like the AI to suggest the profile (genre and tone) from snippets of the book?", True):
            ui.info("Analysing snippets of the book...")
            profile = suggest_profile(ollama, cfg, book)
            if profile is None:
                ui.warn("The AI did not return a usable suggestion; the profile starts from the chosen genre.")
        if profile is None:
            profile = Profile.default("other")

        ui.info(f"\nGenre: {profile.genre} ({GENRES[profile.genre][0]})")
        genre = ui.ask(f"Genre ({', '.join(GENRES)})", profile.genre).lower()
        if genre in GENRES and genre != profile.genre:
            profile = Profile.default(genre, profile.work)
        elif genre not in GENRES:
            ui.warn(f"Genre '{genre}' unknown; keeping '{profile.genre}'.")
        default_work = profile.work or f"{cfg.title}" + (f", by {cfg.author}" if cfg.author else "")
        profile.work = ui.ask("Describe the work in one line (title, author, period, tradition)", default_work)
        ui.info(f"Tone/register asked of the model: {profile.register}")

    profile.save(proj.profile_path)
    if args.no_preview:
        ui.info("Preview skipped (--no-preview).")
    else:
        preview_cycle(proj, cfg, profile, ollama, book, ui)
    profile.save(proj.profile_path)
    cfg.save(proj.config_path)
    proj.mark(profile_approved=True)
    return profile


def make_translator(proj: Project, cfg: Config, profile: Profile, ollama: Ollama) -> Translator:
    return Translator(cfg, profile, proj.glossary_path, ollama, TranslationCache(proj.cache_path), Corrections(proj.corrections_path))


def stage_translation(translator: Translator, proj: Project, cfg: Config, book: Book, ui: UI, ollama: Ollama) -> None:
    ui.heading("5. Book translation")
    all_ = list(book.blocks())
    ready = sum(1 for b in all_ if translator.resolve(b) is not None)
    ui.info(f"{len(all_)} passages: {ready} already done, {len(all_) - ready} to translate with {cfg.model}.")
    if ready < len(all_):
        ui.info("From here it runs on its own. Ctrl+C interrupts; the same command resumes where it stopped.")
    translator.translate_list(all_, progress=Progress(ui))
    ollama.unload(cfg.model)  # free the memory (useful on the Mac, where GPU and RAM are shared)
    proj.mark(translation_complete=True)


def stage_export(translator: Translator, proj: Project, cfg: Config, book: Book, ui: UI) -> None:
    ui.heading("6. Export")
    texts, results = fetch_translations(book, translator)
    pending = sum(1 for r in results.values() if "pending" in r.alerts)
    generated = export_all(proj, cfg, book, texts, results, cfg.formats)
    proj.report_path.write_text(alerts_report(book, results), encoding="utf-8")
    review_ = sum(1 for r in results.values() if "review" in r.alerts)
    ui.info("Generated files:")
    for g in generated:
        ui.info(f"  {g}")
    ui.info(f"Review report: {proj.report_path}")
    if pending:
        ui.warn(f"{pending} passage(s) still untranslated (kept in the original). Run again to continue.")
    if review_:
        ui.warn(f"{review_} passage(s) exhausted the attempts and deserve a human review (see the report).")
    proj.mark(exported=not pending)


# ----------------------------------------------------------------------------- main

def run_(args, ui: UI) -> int:
    path = Path(args.path).expanduser()
    if not path.exists():
        ui.error(f"I couldn't find: {path}")
        return 1
    base = Path(args.output).expanduser() if args.output else Path.cwd()

    if path.is_dir() and Project.is_project(path):
        proj = Project.open(path.resolve())
        ui.info(f"Resuming the project in {proj.root}")
    elif path.is_dir() and file_type(path) != "images":
        ui.error(f"{path} is not a project folder (missing config.json) nor a folder of page images.")
        return 1
    else:
        proj = Project.locate(path, base)
        if proj and (ui.auto or ui.confirm("A project already exists for this book. Resume where it stopped?", True)):
            ui.info(f"Resuming the project in {proj.root}")
        else:
            proj = Project.create(path, base)
            Config(original_file=path.name).save(proj.config_path)
            ui.info(f"Project created in {proj.root}")

    cfg = Config.load(proj.config_path)
    if args.model:
        cfg.model = args.model
    if args.analysis_model:
        cfg.analysis_model = args.analysis_model
    if args.ollama:
        cfg.ollama_url = args.ollama
    if args.ocr_model:
        cfg.ocr_model = args.ocr_model
    if args.preview_pages:
        cfg.preview_pages = args.preview_pages
    if args.no_context:
        cfg.context_blocks = 0
    if args.double_pages:
        cfg.ocr_double = args.double_pages
    has_ocr = any(proj.ocr_dir.glob("p*.json"))
    if args.review_ocr and not has_ocr:
        ui.error("This project has no OCR-read pages to review.")
        return 1
    if (args.review_ocr or args.ocr_force) and has_ocr:
        # rebuilds the book from the saved OCR (or the redone OCR); the translation cache is by content,
        # so only what changed is re-translated
        proj.mark(extraction=False, ocr_reviewed=False, translation_complete=False, exported=False)
        if args.review_ocr:
            args.review = args.review or "all"
    cfg.save(proj.config_path)
    ollama = Ollama(cfg.ollama_url)

    book = stage_extraction(proj, cfg, ui, args)
    stage_language(proj, cfg, book, ui, args)

    if args.extract_only:
        ui.info(f"\nText extracted (language: {language_name(cfg.source)}). Nothing was translated (--extract-only).")
        ui.info(f"  {proj.book_md_path}")
        ui.info(f"  {proj.book_path}")
        ui.info("To translate, run the same command without --extract-only (the OCR is not redone).")
        return 0

    stage_config(proj, cfg, book, ui, args)

    if args.export_only:
        profile = Profile.load(proj.profile_path) if proj.profile_path.exists() else Profile.default("other")
        translator = make_translator(proj, cfg, profile, ollama)
    else:
        profile = stage_profile(proj, cfg, book, ui, ollama, args)
        translator = make_translator(proj, cfg, profile, ollama)
        stage_translation(translator, proj, cfg, book, ui, ollama)
    stage_export(translator, proj, cfg, book, ui)
    ui.info(f"\nDone. Everything is in {proj.root}")
    return 0


def main(argv=None) -> int:
    args = read_args(argv)
    ui = UI(auto=args.yes)
    try:
        return run_(args, ui)
    except UserExit:
        ui.info("\nExiting. The project is saved; run the same command to resume.")
        return 0
    except KeyboardInterrupt:
        ui.info("\n\nInterrupted. What has already been translated is saved; run the same command to continue.")
        return 130
    except (ExtractionError, OllamaError) as e:
        ui.error(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
