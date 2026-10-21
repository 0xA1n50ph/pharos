"""OCR stage: from the scan (PDF, images) to the Book in the internal format, with quality scoring and human review."""
from __future__ import annotations

import os
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Config, LANGUAGES, language_name, normalize_code
from .extractors import pdf_text
from .extractors.common import ExtractionError
from .language import detect_snippet
from .model import Book, TITLE
from .ocr import layout, assembly, engine, preprocess, quality, server, tesseract
from .ocr.engine import OCRParams
from .ocr.pages import PageSource
from .ocr.review import Review, Session
from .ocr.ai_suggestions import AISuggestions
from .ocr.tesseract import OCRError
from .ollama import Ollama
from .project import Project
from .ui import Bar, UserExit, UI

_GOTHIC_DIFFERENCE = 8.0   # mean-confidence points the gothic model must gain to be suggested
_LOW_CONFIDENCE = 65.0     # below this the sample OCR deserves a warning


def _ranges(numbers: list[int]) -> str:
    """[1,2,3,7,9,10] -> '1–3, 7, 9–10'."""
    if not numbers:
        return ""
    parts, start, prev = [], numbers[0], numbers[0]
    for n in numbers[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(str(start) if start == prev else f"{start}–{prev}")
        if n is not None:
            start = prev = n
    return ", ".join(parts)


def _list_summary(numbers: list[int]) -> str:
    text = _ranges(sorted(numbers))
    return text if len(text) <= 120 else text[:117].rsplit(",", 1)[0] + f" … (+{len(numbers)} total)"


# ----------------------------------------------------------------------------- route: OCR or direct text

def route_pdf(original: Path, cfg: Config, ui: UI, args) -> dict | None:
    """Decides how to extract a PDF. None = direct text extraction; otherwise {'ocr': [indices], 'text': {pages}}.

    OCR indices start at 0; text pages start at 1."""
    ui.info("Classifying each page (text, scan, embedded OCR layer)...")
    classes = pdf_text.classify_pages(original)
    counts = Counter(classes)
    n_text, n_scan, n_layer = counts["text"], counts["scan"], counts["layer"]
    useful = n_text + n_scan + n_layer
    with_text = {i + 1 for i, c in enumerate(classes) if c == "text"}
    with_layer = {i + 1 for i, c in enumerate(classes) if c == "layer"}
    ocr_scan = [i for i, c in enumerate(classes) if c == "scan"]
    ocr_scan_layer = [i for i, c in enumerate(classes) if c in ("scan", "layer")]

    if useful == 0:
        ui.warn("No page has selectable text or a full-page image.")
        if not ui.confirm("Try OCR on all pages anyway?", False):
            raise ExtractionError("PDF with no usable content.")
        return {"ocr": list(range(len(classes))), "text": set()}

    if n_layer / useful >= 0.5:
        ui.info(f"This is a scan that already carries a text layer (OCR from another tool) on {n_layer} page(s).")
        ui.info("That layer usually has errors, especially in old books, and does not report each word's confidence:\n"
                "without that we cannot highlight what is doubtful in the review. Redoing the OCR with Tesseract is slower,\n"
                "but gives that control.")
        if cfg.ocr_language and not cfg.ocr_use_layer:
            use_ = False  # already started redoing the OCR in a previous run
        elif args.use_embedded_layer:
            use_ = True
        elif ui.auto:
            use_ = False
        else:
            use_ = not ui.confirm("Redo the OCR with Tesseract? (No = use the embedded text)", True)
        cfg.ocr_use_layer = use_
        if use_:
            return None
        return {"ocr": ocr_scan_layer, "text": with_text}

    # text and image pages mixed: with several image pages (or almost all image), it's a scanned book
    if n_scan / useful >= 0.2 and (n_scan >= 3 or n_scan / useful >= 0.8):
        return {"ocr": ocr_scan, "text": with_text | with_layer}

    if n_scan:  # a few image pages in a text book: cover, plates, illustrations
        ui.info(f"{n_scan} page(s) are image only (cover, illustrations, plates?): {_list_summary([i + 1 for i in ocr_scan])}")
        if args.ocr_images or (not ui.auto and ui.confirm("Run OCR on them too?", False)):
            return {"ocr": ocr_scan, "text": with_text | with_layer}
        ui.info("They are left out of the text (use --ocr-images to read them with OCR).")
    return None


# ----------------------------------------------------------------------------- OCR language and model

def _sample(indices: list[int], n: int) -> list[int]:
    """n pages spread between 15% and 85% of the book (avoids cover, index and back cover)."""
    if len(indices) <= n:
        return list(indices)
    start, end = int(len(indices) * 0.15), max(int(len(indices) * 0.85), int(len(indices) * 0.15) + n)
    middle = indices[start:end] or indices
    step = len(middle) / n
    return [middle[int(step * k + step / 2)] for k in range(n)]


def _sample_ocr(exe: str, files: list[Path], specs: list[str], cfg: Config) -> dict[str, list[list]]:
    """Runs each language/model on each sample page, in parallel. {spec: words of all pages}."""
    tasks = [(spec, f) for spec in specs for f in files]
    n = max(1, min(len(tasks), (os.cpu_count() or 2) - 1 or 1, 6))

    def one(t):
        return t[0], tesseract.run_tsv(exe, t[1], t[0], cfg.ocr_psm, cfg.ocr_dpi)

    out: dict[str, list[list]] = {s: [] for s in specs}
    with ThreadPoolExecutor(max_workers=n) as ex:
        for spec, words in ex.map(one, tasks):
            out[spec].extend(words)
    return out


def _ask_language(ui: UI, default: str) -> str:
    while True:
        resp = ui.ask("Book language (Enter confirms; or type another code/name)", default)
        code = normalize_code(resp) if resp else None
        if code:
            return code
        if ui.auto:
            raise ExtractionError("Source language undefined; use --source or --ocr-lang.")
        ui.warn(f"I didn't recognise '{resp}'. Known codes: {', '.join(sorted(LANGUAGES))}")


def _detect_language(source: PageSource, indices: list[int], half_of, exe: str, cfg: Config, ui: UI) -> str:
    installed = tesseract.installed_languages(exe)
    candidates = [c for c in tesseract.DETECTION_CANDIDATES if c in installed]
    if not candidates:
        raise OCRError("No OCR language installed. " + tesseract.INSTALL_INSTRUCTIONS)
    spec = "+".join(candidates)
    ui.info(f"Figuring out the language with a sample OCR ({', '.join(candidates)})...")
    with tempfile.TemporaryDirectory(prefix="sample_") as tmp:
        files = [engine.prepare_sample(source, i, cfg.ocr_dpi, Path(tmp), half_of(i)) for i in _sample(indices, 2)]
        words = _sample_ocr(exe, files, [spec], cfg)[spec]
    r = detect_snippet(" ".join(p[8] for p in words))
    default = ""
    if r:
        default = r[0]
        ui.info(f"Detected language: {language_name(r[0])} ({round(r[1] * 100)}% of the function words)")
    else:
        ui.warn("I couldn't detect the language automatically (common with gothic-type books).")
    return _ask_language(ui, default)


def _choose_gothic(source, indices, half_of, exe: str, base: str, iso: str, cfg: Config, ui: UI) -> str:
    """Compares the normal model with the gothic (Fraktur) ones on a sample and returns the chosen spec."""
    gothic = tesseract.installed_gothic(exe, iso)
    sample = _sample(indices, 4)
    if not gothic:
        # no gothic model: only test the normal one so we can warn
        specs = [base]
    else:
        specs = [base] + gothic
    ui.info(f"Testing OCR on {len(sample)} sample page(s) ({', '.join(specs)})...")
    with tempfile.TemporaryDirectory(prefix="sample_") as tmp:
        files = [engine.prepare_sample(source, i, cfg.ocr_dpi, Path(tmp), half_of(i)) for i in sample]
        words = _sample_ocr(exe, files, specs, cfg)
    means = {s: tesseract.mean_confidence(w) for s, w in words.items()}
    ui.info("Mean confidence on the sample: " + "; ".join(f"{s} {m:.0f}%" for s, m in means.items()))
    if not gothic:
        if means[base] < _LOW_CONFIDENCE and iso in tesseract.GOTHIC:
            ui.warn(
                f"Low confidence ({means[base]:.0f}%). If the book is printed in gothic type (Fraktur), install a model "
                f"({' or '.join(tesseract.GOTHIC[iso])}): brew install tesseract-lang  — and run again with --ocr-force."
            )
        return base
    best = max(means, key=lambda s: means[s])
    if best != base and means[best] >= means[base] + _GOTHIC_DIFFERENCE:
        ui.info(f"The model '{best}' read much better: the book seems printed in gothic type (Fraktur).")
        if ui.auto or ui.confirm(f"Use '{best}' for the OCR?", True):
            return best
    return base


def set_language(source, indices, half_of, exe: str, proj: Project, cfg: Config, ui: UI, args,
                 sample_text: str = "") -> tuple[str, str]:
    """(Tesseract language/model, book ISO code or ''). sample_text: selectable text from a mixed PDF."""
    if cfg.ocr_language and not args.ocr_lang:  # decided in a previous run
        tesseract.verify(exe, cfg.ocr_language)
        return cfg.ocr_language, cfg.source or tesseract.iso_from_code(cfg.ocr_language) or ""
    if args.ocr_lang:
        tesseract.verify(exe, args.ocr_lang)
        iso = ""
        if args.source:
            iso = normalize_code(args.source) or ""
        iso = iso or tesseract.iso_from_code(args.ocr_lang) or ""
        ui.info(f"OCR in language/model: {args.ocr_lang}")
        spec = args.ocr_lang
    else:
        if args.source:
            iso = normalize_code(args.source)
            if not iso:
                raise ExtractionError(f"Source language not recognised: {args.source}")
        else:
            r = detect_snippet(sample_text) if sample_text else None
            if r:
                ui.info(f"Language detected from the PDF's selectable text: {language_name(r[0])} ({round(r[1] * 100)}% of the function words)")
                iso = _ask_language(ui, r[0])
            else:
                iso = _detect_language(source, indices, half_of, exe, cfg, ui)
        base = tesseract.code_for(iso)
        if not base:
            raise OCRError(f"I don't know the Tesseract model for {language_name(iso)}. Use --ocr-lang with its code.")
        tesseract.verify(exe, base)
        spec = _choose_gothic(source, indices, half_of, exe, base, iso, cfg, ui) if iso in tesseract.GOTHIC else base
    cfg.ocr_language = spec
    if iso:
        cfg.source = iso
    cfg.save(proj.config_path)
    return spec, iso


# ----------------------------------------------------------------------------- double pages

def set_doubles(source: PageSource, indices: list[int], proj: Project, cfg: Config, ui: UI, args, mixed: bool) -> bool:
    if mixed:
        return False  # numbering of text and OCR pages would not line up
    mode = args.double_pages or cfg.ocr_double
    prop = engine.double_ratio(source, indices)
    if mode == "yes":
        result = prop > 0
    elif mode == "no":
        result = False
    elif cfg.ocr_double_applied in ("yes", "no"):
        result = cfg.ocr_double_applied == "yes"  # already asked in a previous run
    elif prop >= 0.5:
        ui.info(f"{round(prop * 100)}% of the images are much wider than tall: there seem to be two pages per image.")
        result = ui.auto or ui.confirm("Split each image into two pages (left and right)?", True)
    else:
        result = False
    previous = cfg.ocr_double_applied
    cfg.ocr_double_applied = "yes" if result else "no"
    if previous and previous != cfg.ocr_double_applied and any(proj.ocr_dir.glob("p*.json")):
        ui.info("The double-page mode changed: the page numbering changes, so the OCR will be redone.")
        args.ocr_force = True
    cfg.save(proj.config_path)
    return result


# ----------------------------------------------------------------------------- report

def write_report(proj: Project, spec: str, scores: dict, summary: dict, session: Session, errors: dict, reviewed: bool,
                 ai: AISuggestions | None = None) -> None:
    reviewed_pages = session.review.reviewed_pages()
    edited_pages = session.review.edited_pages()
    lines = [
        "# OCR quality", "",
        f"- Tesseract language/model: `{spec}`",
        f"- Pages: {summary['pages']} · mean confidence {summary['mean']:.0f}%",
        f"- Flagged: {summary['flagged']} · reviewed: {len(reviewed_pages)} · edited by hand: {len(edited_pages)}",
        f"- Review done on screen: {'yes' if reviewed else 'no'}",
    ]
    if ai is not None and ai.summary()["generated"]:
        r = ai.summary()
        lines.append(f"- AI correction suggestions ({r['model']}): {r['generated']} generated, {r['accepted']} accepted, "
                     f"{r['rejected']} rejected (nothing is applied without your decision)")
    if errors:
        lines.append(f"- Pages with an OCR error (left out): {_ranges(sorted(errors))}")
    lines += ["", "## Flagged or edited pages", "", "| Page | Score | Confidence | Doubtful | Reasons | Reviewed | Edited |",
              "|---|---|---|---|---|---|---|"]
    for n, p in scores.items():
        if p["flagged"] or n in edited_pages:
            lines.append(f"| {n} | {p['score']:.0f} | {p['conf']:.0f}% | {p['doubtful']} | {'; '.join(p['reasons']) or '—'} | "
                         f"{'yes' if n in reviewed_pages else 'no'} | {'yes' if n in edited_pages else 'no'} |")
    proj.ocr_quality_path.parent.mkdir(parents=True, exist_ok=True)
    proj.ocr_quality_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------- full stage

def _pdf_metadata(original: Path) -> tuple[str, str]:
    if original.is_dir() or original.suffix.lower() != ".pdf":
        return "", ""
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(original))
        try:
            m = pdf.get_metadata_dict()
        finally:
            pdf.close()
        return (m.get("Title") or "").strip(), (m.get("Author") or "").strip()
    except Exception:  # noqa: BLE001
        return "", ""


def _decide_review(args, ui: UI, summary: dict) -> str:
    """flagged | all | none"""
    if args.review:
        return args.review
    if ui.auto:
        return "none"
    n_flagged, n = summary["flagged"], summary["pages"]
    ui.info("\nOCR review: you compare each page image with the recognised text and fix what you need.")
    if n_flagged:
        ui.info(f"  1) Review only the {n_flagged} flagged page(s) (recommended)")
        ui.info(f"  2) Review all {n} pages")
        ui.info("  3) Skip the review and translate the text as is")
        r = ui.ask("Choice", "1")
        return {"1": "flagged", "2": "all", "3": "none"}.get(r.strip(), "flagged")
    ui.info("  No page fell below the quality threshold.")
    ui.info(f"  1) Check anyway (all {n} pages, in the review screen)")
    ui.info("  2) Skip the review (recommended)")
    r = ui.ask("Choice", "2")
    return "all" if r.strip() == "1" else "none"


def extract_scan(proj: Project, cfg: Config, ui: UI, args, original: Path, indices: list[int] | None = None,
                 text_pages: set[int] | None = None) -> tuple[Book, dict]:
    """OCR + layout + quality + review + assembly. Returns (Book, report for the extraction stage)."""
    exe = tesseract.locate()
    source = PageSource(original)
    try:
        return _extract(proj, cfg, ui, args, original, source, exe, indices, text_pages or set())
    finally:
        source.close()


def _extract(proj, cfg, ui, args, original, source, exe, indices, text_pages):
    if args.ocr_dpi:
        cfg.ocr_dpi = args.ocr_dpi
    if args.ocr_processes is not None:
        cfg.ocr_processes = args.ocr_processes
    all_ = indices if indices is not None else list(range(source.total))
    if not all_:
        raise ExtractionError("No page for the OCR.")

    ui.heading("1b. OCR")
    ui.info(f"Tesseract: {exe}")
    doubles = set_doubles(source, all_, proj, cfg, ui, args, mixed=bool(text_pages))

    def half_of(i: int) -> int | None:
        return 0 if doubles and preprocess.is_double(*source.size(i)) else None

    extras, indented = [], set()
    sample_text = ""
    if text_pages:
        extras, indented, _meta, _rel = pdf_text.extract_blocks(original, set(text_pages), require_text=False)
        long_paragraphs = [b.text for b in extras if b.kind == "paragraph" and len(b.text) >= 120]
        sample_text = " ".join(long_paragraphs[:: max(1, len(long_paragraphs) // 40)][:40])
    spec, iso = set_language(source, all_, half_of, exe, proj, cfg, ui, args, sample_text)
    plan = engine.page_plan(source, all_, doubles)

    if args.ocr_force:
        for folder, pattern in ((proj.ocr_dir, "p*.json"), (proj.pages_dir, "p*.jpg")):
            for f in folder.glob(pattern):
                f.unlink()
        if proj.ocr_review_path.exists():  # the edits were on the old OCR
            proj.ocr_review_path.replace(proj.ocr_review_path.with_suffix(".old.json"))
        args.ocr_force = False

    while True:
        params = OCRParams(spec, cfg.ocr_psm, cfg.ocr_dpi)
        ready = sum(1 for it in plan if engine.page_done(proj.ocr_dir, proj.pages_dir, it["n"], params, it))
        ui.info(f"OCR of {len(plan)} page(s) with '{spec}' at {cfg.ocr_dpi} dpi"
                + (f" ({ready} already done before)." if ready else "."))
        if ready < len(plan):
            ui.info("This takes a while; you can interrupt with Ctrl+C and resume later (what was read is saved).")
        bar = Bar(ui, "OCR")
        results, errors = engine.run(
            source, plan, params, exe, proj.ocr_dir, proj.pages_dir, cfg.ocr_processes,
            lambda done, total, n, d: bar(done, total, f"p. {n}"))
        if errors:
            ui.warn(f"{len(errors)} page(s) failed in the OCR: {_list_summary(sorted(errors))}")
            for n, e in list(sorted(errors.items()))[:3]:
                ui.info(f"  p. {n}: {e[:160]}")
        if not results:
            raise OCRError("The OCR could not read any page.")
        pages = dict(sorted(results.items()))
        scores = quality.score_all(pages, cfg.ocr_word_threshold, cfg.ocr_page_threshold)
        summary = quality.summarize(scores)
        if summary["overall_bad"] and not ui.auto:
            ui.warn(f"More than half of the pages came out low quality (mean confidence {summary['mean']:.0f}%). "
                    "It is usually the wrong language, gothic type (Fraktur) or a low-resolution scan.")
            new = ui.ask("Redo the OCR with another language/model? Type the Tesseract code (e.g.: frk, deu_latf, deu+eng) "
                         "or Enter to continue as is", "").strip()
            if new:
                try:
                    tesseract.verify(exe, new)
                except OCRError as e:
                    ui.warn(str(e))
                    continue
                spec = new
                cfg.ocr_language = spec
                cfg.save(proj.config_path)
                continue
        break

    if errors:
        if ui.auto:
            raise ExtractionError("OCR failures on some pages; run again to try once more.")
        if not ui.confirm("Continue without those pages? (No = stop now and try again later)", False):
            raise UserExit("incomplete OCR")

    ui.info(f"\nOCR done: {summary['pages']} page(s), mean confidence {summary['mean']:.0f}%.")
    if summary["flagged"]:
        ui.info(f"{summary['flagged']} page(s) flagged for review: {_list_summary(summary['flagged_list'])}")
    auto_blocks, rel_layout = layout.analyze_document(pages)
    suggestions = quality.generate_suggestions(pages)
    if suggestions:
        ui.info(f"{len(suggestions)} correction suggestion(s) from the book's own consistency (shown in the review screen).")

    mode = _decide_review(args, ui, summary)
    book_title = cfg.title or _pdf_metadata(original)[0] or original.stem.replace("_", " ")
    filter_ = "flagged" if (mode == "flagged" and summary["flagged"]) else "all"
    rev = Review(proj.ocr_review_path, params.hash())
    ai = AISuggestions(proj.ocr_ai_suggestions_path, params.hash(), Ollama(cfg.ollama_url, timeout=300), cfg.ocr_model,
                       cfg.analysis_model or cfg.model, iso or cfg.source, spec)
    session = Session(book_title, pages, auto_blocks, scores, suggestions, rev, proj.pages_dir, cfg.ocr_word_threshold, filter_, ai)
    reviewed = False
    if mode != "none":
        ui.heading("1c. OCR Review")
        try:
            ok = server.run(session, open_browser=not args.no_browser, output=ui.info, ai_batch=args.ai_suggestions)
        finally:
            if ai.work_state()["active"]:
                ui.info("Waiting for the AI to finish the current page...")
                ai.stop_()
                ai.wait_(180)
            ai.unload()  # free the memory before the translation
        if not ok:
            raise UserExit("OCR review paused")
        reviewed = True
        ui.info(f"Review finished: {len(rev.edited_pages())} page(s) edited, {len(rev.reviewed_pages())} marked as reviewed.")
        r = ai.summary()
        if r["generated"]:
            ui.info(f"AI suggestions ({r['model']}): {r['generated']} generated, {r['accepted']} accepted, {r['rejected']} rejected by you.")
    elif summary["flagged"]:
        ui.warn("The review was skipped; the flagged pages keep the OCR text.")
    elif rev.edited_pages():
        ui.info(f"Applying {len(rev.edited_pages())} page(s) edited in a previous review.")
    write_report(proj, spec, scores, summary, session, errors, reviewed, ai)
    ui.info(f"OCR quality report: {proj.ocr_quality_path}")

    book = assembly.assemble(session.final_blocks(), extras, indented, book_title, cfg.author or _pdf_metadata(original)[1], iso)
    if not cfg.title and not _pdf_metadata(original)[0] and book.chapters:
        title_page = book.chapters[0].blocks  # title page (titles only): the first is the book title
        if title_page and all(b.kind == TITLE for b in title_page):
            book.title = title_page[0].text.strip() or book.title
    proj.mark(ocr_reviewed=True)
    if iso:
        cfg.source = iso
        proj.mark(language=True)
    report = {
        "headers_removed": rel_layout.get("headers_removed", []),
        "pages_without_text": [],
        "ocr": {"pages": summary["pages"], "mean": summary["mean"], "flagged": summary["flagged"], "language": spec},
    }
    return book, report
