#!/usr/bin/env python3
"""Tests of the OCR and the review screen.

Usage:  python tests/test_ocr.py
Needs Tesseract (with 'eng' and, preferably, 'osd') and: pillow, numpy, pypdfium2, reportlab.
The tests with the real Tesseract use English; the German/gothic-type logic is tested with a simulated Tesseract.
The browser-screen test uses Playwright (skipped if not installed).
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import io
import json
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
PY = sys.executable
CLI = str(ROOT / "pharos.py")

import samples  # noqa: E402

# pharos.py (the CLI script) can't be imported as `pharos` — that name is the package; load it by path
import importlib.util as _ilu  # noqa: E402
_cli_spec = _ilu.spec_from_file_location("pharos_cli", ROOT / "pharos.py")
pharos_cli = _ilu.module_from_spec(_cli_spec)
_cli_spec.loader.exec_module(pharos_cli)
read_args = pharos_cli.read_args

failures = 0


def ok(cond: bool, text: str) -> None:
    global failures
    print(("  ok    " if cond else "  FAIL  ") + text)
    if not cond:
        failures += 1


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def run(args: list[str], cwd: Path, input_: str | None = None, timeout: int = 300):
    r = subprocess.run([PY, CLI, *args], input=input_, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def api(url: str, path: str, body: dict | None = None):
    """Calls the review-screen API from the URL the program prints (http://127.0.0.1:PORT/?t=TOKEN)."""
    m = re.match(r"(http://127\.0\.0\.1:\d+)/\?t=(\S+)", url)
    req = urllib.request.Request(m.group(1) + path, headers={"X-Token": m.group(2)},
                                 data=None if body is None else json.dumps(body).encode())
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def run_interactive(args: list[str], cwd: Path, action, input_: str = "\n" * 40, timeout: int = 300):
    """Runs the program with default answers (Enter) and, when the review screen opens, calls action(url, process)."""
    # SIGINT back to default: non-interactive shells (CI, background) inherit it as "ignored" and the simulated Ctrl+C would not arrive
    p = subprocess.Popen([PY, CLI, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, cwd=cwd, preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    lines: list[str] = []
    threading.Thread(target=lambda: [lines.append(l) for l in p.stdout], daemon=True).start()
    p.stdin.write(input_)
    p.stdin.close()
    url, deadline = None, time.time() + timeout
    while time.time() < deadline and p.poll() is None and not url:
        for l in list(lines):
            m = re.search(r"Review screen: (http://\S+)", l)
            if m:
                url = m.group(1)
        time.sleep(0.2)
    if url and action:
        action(url, p)
    try:
        p.wait(timeout=60 if url else timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        lines.append("\n[test] the program did not finish in time and was terminated\n")
    time.sleep(0.3)
    return p.returncode, "".join(lines), url


def paragraphs(book_json: Path) -> list[str]:
    d = json.loads(book_json.read_text(encoding="utf-8"))
    return [b["text"] for c in d["chapters"] for b in c["blocks"] if b["kind"] == "paragraph"]


def blocks_of_kind(book_json: Path, kind: str) -> list[str]:
    d = json.loads(book_json.read_text(encoding="utf-8"))
    return [b["text"] for c in d["chapters"] for b in c["blocks"] if b["kind"] == kind]


def merge_pdfs(dest: Path, parts: list[tuple[Path, list[int]]]) -> None:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    srcs = []
    for pdf, idx in parts:
        src = pdfium.PdfDocument(str(pdf))
        srcs.append(src)
        doc.import_pages(src, idx)
    doc.save(str(dest))
    doc.close()
    for f in srcs:
        f.close()


def main() -> int:
    from pharos.ocr import tesseract

    try:
        exe = tesseract.locate()
        installed = tesseract.installed_languages(exe)
    except Exception as e:  # noqa: BLE001
        print(f"Tesseract not found ({e}); OCR tests skipped.")
        return 0
    if "eng" not in installed:
        print("Tesseract does not have the 'eng' language; OCR tests skipped.")
        return 0
    has_osd = "osd" in installed

    tmp = Path(tempfile.mkdtemp(prefix="pharos_ocr_"))
    sm = tmp / "samples"
    sm.mkdir()
    samples.make_pdf(sm / "lighthouse.pdf", "indent", "en")
    samples.make_scan(sm / "lighthouse.pdf", sm / "scan.pdf")
    expected = samples.expected_text_en()
    expected_joined = " ".join(expected)
    print(f"Test folder: {tmp}\n")

    # ------------------------------------------------------------------ A. pre-processing
    print("A. pre-processing: skew, blank page, double pages")
    from PIL import Image

    from pharos.ocr import preprocess as pp

    base = samples.render_pdf(sm / "lighthouse.pdf", 200, [1])[0]
    for ang in (-2.5, -1.0, 0.6, 1.4, 3.2):
        d = samples.degrade(base, ang, 6.0, seed=1)
        est = pp.estimate_skew(pp.even_lighting(pp.grayscale(d)))
        ok(abs(est + ang) <= 0.3, f"skew of {ang:+.1f}° estimated as {-est:+.2f}°")
    ok(pp.estimate_skew(Image.new("L", (1200, 1600), 255)) == 0.0, "blank page is not rotated")
    wide = Image.new("L", (2 * 1000 + 60, 1400), 120)
    wide.paste(base.resize((1000, 1400)), (0, 0))
    wide.paste(base.resize((1000, 1400)), (1060, 0))
    ok(pp.is_double(*wide.size) and not pp.is_double(1000, 1400), "wide image recognised as double; normal page not")
    left, right = pp.split_double(pp.grayscale(wide), 0), pp.split_double(pp.grayscale(wide), 1)
    ok(960 <= left.width <= 1005 and 960 <= right.width <= 1005,
       f"double with a spine shadow: each half keeps only its page ({left.width} and {right.width} px)")
    no_shadow = Image.new("L", (2 * 1000 + 120, 1400), 255)
    no_shadow.paste(base.resize((1000, 1400)), (0, 0))
    no_shadow.paste(base.resize((1000, 1400)), (1120, 0))
    e2, d2 = pp.split_double(pp.grayscale(no_shadow), 0), pp.split_double(pp.grayscale(no_shadow), 1)
    ok(1000 <= e2.width <= 1120 and 1000 <= d2.width <= 1120 and abs(e2.width - 1060) <= 40,
       f"double with no shadow: cuts in the middle of the gap ({e2.width} and {d2.width} px)")

    # ------------------------------------------------------------------ B. quality, suggestions, edits
    print("\nB. quality, suggestions, edits and global replace")
    from pharos.ocr import quality
    from pharos.ocr.review import Review, Session, _clean_block

    def word(txt, conf, x=10):
        return [1, 1, 1, x, 10, 40, 12, conf, txt]

    good = [word(w, 95) for w in "the keeper climbed the old stair every night".split()] * 3
    page_good = {"n": 1, "words": good, "ink": 0.1, "width": 1000, "height": 1400}
    page_bad = {"n": 2, "words": [word(w, 40) for w in "tbe kecper clinbed tbe o1d stalr evry n1ght".split()] * 3,
                "ink": 0.1, "width": 1000, "height": 1400}
    page_mute = {"n": 3, "words": [], "ink": 0.001, "width": 1000, "height": 1400}
    page_ink = {"n": 4, "words": [], "ink": 0.15, "width": 1000, "height": 1400}
    pts = quality.score_all({1: page_good, 2: page_bad, 3: page_mute, 4: page_ink})
    ok(not pts[1]["flagged"] and pts[2]["flagged"], "good page passes; low-confidence page is flagged")
    ok(not pts[3]["flagged"], "blank page is not flagged")
    ok(pts[4]["flagged"], "page with an image but no recognised text is flagged")
    ok(quality.summarize(pts)["flagged"] == 2, "summary counts the flagged ones")
    pages_s = {
        1: {"n": 1, "words": [word("lighthouse", 96)] * 6 + [word("lighthonse", 41)], "ink": .1, "width": 1000, "height": 1400},
        2: {"n": 2, "words": [word("down", 95)] * 5 + [word("dawn", 82)], "ink": .1, "width": 1000, "height": 1400},
    }
    sug = quality.generate_suggestions(pages_s)
    ok(any(s["from"] == "lighthonse" and s["to"] == "lighthouse" for s in sug), "suggests lighthonse → lighthouse")
    ok(not any(s["from"] == "dawn" for s in sug), "does not 'fix' a legitimate word read with good confidence (dawn)")
    ok(_clean_block({"kind": "xyz", "text": "  a   b \n c ", "level": 99}) == {"kind": "paragraph", "text": "a b c", "level": 4, "indent": True},
       "an edited block is sanitised (kind, spaces, level)")
    ok(_clean_block({"kind": "paragraph", "text": "   "}) is None, "an empty block is discarded")
    auto = {1: [{"kind": "paragraph", "text": "The man and the woman saw the lighthouse.", "level": 1, "indent": True}],
            2: [{"kind": "paragraph", "text": "Lighthouse keepers; a Lighthouse.", "level": 1, "indent": True}]}
    rev_json = tmp / "rev_test.json"
    ses = Session("T", pages_s, auto, quality.score_all(pages_s), sug, Review(rev_json, "h1"), tmp)
    ok(ses.replace("man", "men")["occurrences"] == 1, "'man' only as a whole word (does not touch 'woman')")
    ok(ses.replace("lighthouse", "farol", True, True)["occurrences"] == 3, "ignore case counts the 3 occurrences")
    ok(ses.replace("lighthouse", "farol", True, False)["occurrences"] == 1, "with case counts only 1")
    ses.replace("lighthouse", "farol", True, True, apply_=True)
    ok("farol" in ses.blocks(1)[0]["text"] and ses.review.edited(1) and ses.review.edited(2), "apply writes the changed pages")
    ses.mark(2, True)
    ok(Review(rev_json, "h1").reviewed(2) and Review(rev_json, "h1").edited(1), "edits and review persist on disk")
    Review(rev_json, "h2")
    ok(rev_json.with_suffix(".old.json").exists() and not rev_json.exists(), "edits of an OCR with other parameters are set aside")

    # ------------------------------------------------------------------ C. language, gothic type, PDF route (simulated Tesseract)
    print("\nC. language choice, gothic type and PDF route (simulated Tesseract)")
    from pharos import ocr_stage
    from pharos.config import Config
    from pharos.extractors import pdf_text
    from pharos.extractors.common import ExtractionError
    from pharos.project import Project
    from pharos.ui import UI

    class FakeSource:
        total = 10

        def size(self, i):
            return (600, 800)

    def conf_by_lang(table):
        def fake(exe, image, lang, psm=3, dpi=300, timeout=600):
            text = ("der die und nicht ist mit von zu für auf das ein eine wird nach auch aber wie oder dass "
                    "sich noch nur sind war haben")
            return [[1, 1, 1, i * 10, 0, 9, 9, table.get(lang, 50), w] for i, w in enumerate(text.split())]
        return fake

    saved = (tesseract.installed_languages, tesseract.run_tsv, ocr_stage.engine.prepare_sample)
    ocr_stage.engine.prepare_sample = lambda source, i, dpi, dest, half=None: Path(dest) / f"a{i}.png"

    def scenario(installed_, confs, args_extra=(), cfg=None):
        tesseract.installed_languages = lambda exe: frozenset(installed_)
        tesseract.run_tsv = conf_by_lang(confs)
        args = read_args(["x", *args_extra])
        cfg = cfg or Config()
        folder = Path(tempfile.mkdtemp(dir=tmp))
        cfg.save(folder / "config.json")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = ocr_stage.set_language(FakeSource(), list(range(10)), lambda i: None, "tess", Project(folder), cfg,
                                         UI(auto=True), args)
        return res, buf.getvalue(), cfg

    try:
        (spec, iso), out, cfg = scenario({"deu", "frk", "eng", "osd"}, {"deu": 45, "frk": 82}, ["--source", "de"])
        ok(spec == "frk" and iso == "de", f"Fraktur book: the gothic model wins (chosen: {spec})")
        ok(cfg.ocr_language == "frk" and cfg.source == "de", "choice saved in the project configuration")
        (spec, iso), out, _ = scenario({"deu", "frk", "eng"}, {"deu": 90, "frk": 80}, ["--source", "de"])
        ok(spec == "deu", "normal type: keeps the common model")
        (spec, iso), out, _ = scenario({"deu", "frk", "eng"}, {"deu": 55, "frk": 58}, ["--source", "de"])
        ok(spec == "deu", "a small difference does not switch models")
        (spec, iso), out, _ = scenario({"deu", "deu_latf", "eng"}, {"deu": 40, "deu_latf": 85}, ["--source", "de"])
        ok(spec == "deu_latf", "also considers the deu_latf model")
        (spec, iso), out, _ = scenario({"deu", "eng"}, {"deu": 40}, ["--source", "de"])
        ok(spec == "deu" and "gothic" in out and "frk" in out, "no gothic model installed and low confidence: warns how to install")
        (spec, iso), out, cfg = scenario({"deu", "frk", "eng"}, {}, ["--ocr-lang", "frk"])
        ok(spec == "frk" and iso == "de", "--ocr-lang frk is respected and implies German")
        (spec, iso), out, _ = scenario({"deu", "eng"}, {}, ["--ocr-lang", "deu+eng", "--source", "de"])
        ok(spec == "deu+eng", "several languages together (deu+eng)")
        cfg_prev = Config(ocr_language="frk", source="de")
        calls = []
        tesseract.run_tsv = lambda *a, **k: calls.append(1) or []
        tesseract.installed_languages = lambda exe: frozenset({"deu", "frk"})
        with contextlib.redirect_stdout(io.StringIO()):
            r = ocr_stage.set_language(FakeSource(), list(range(10)), lambda i: None, "tess", Project(tmp), cfg_prev,
                                       UI(auto=True), read_args(["x"]))
        ok(r == ("frk", "de") and not calls, "a resumed run uses the previous choice without re-running the tests")
        try:
            scenario({"eng"}, {}, ["--source", "ru"])
            ok(False, "a language with no installed model should raise an error")
        except ExtractionError as e:
            ok("rus" in str(e) and "does not have the language" in str(e), "a language with no installed model: a clear error")
        # language detection by sample OCR (German text)
        (spec, iso), out, _ = scenario({"deu", "eng", "fra", "osd"}, {"deu": 90, "eng": 90, "fra": 90}, [])
        ok(iso == "de" and "German" in out, f"detects the language from the recognised text (detected: {iso})")
    finally:
        tesseract.installed_languages, tesseract.run_tsv, ocr_stage.engine.prepare_sample = saved

    original_classify = pdf_text.classify_pages

    def route(classes, extra=(), auto=True, cfg=None):
        pdf_text.classify_pages = lambda path: classes
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                return ocr_stage.route_pdf(Path("x.pdf"), cfg or Config(), UI(auto=auto), read_args(["x", *extra]))
        finally:
            pdf_text.classify_pages = original_classify

    ok(route(["text"] * 10) is None, "text-only PDF: direct extraction")
    r = route(["scan"] * 10)
    ok(r and r["ocr"] == list(range(10)) and not r["text"], "scan-only PDF: OCR on all")
    r = route(["text"] * 5 + ["scan"] * 5)
    ok(r and r["ocr"] == [5, 6, 7, 8, 9] and r["text"] == {1, 2, 3, 4, 5}, "mixed PDF: OCR on the image ones, direct text on the others")
    ok(route(["scan"] + ["text"] * 19) is None, "text book with an image cover: does not trigger OCR (automatic mode)")
    ok(route(["scan", "scan", "text", "text", "text"]) is None, "two image pages in a text book: left out")
    r = route(["scan"] + ["text"] * 19, ["--ocr-images"])
    ok(r and r["ocr"] == [0] and len(r["text"]) == 19, "--ocr-images reads the image-only pages with OCR")
    r = route(["scan"] * 3 + ["text"] * 7)
    ok(r and r["ocr"] == [0, 1, 2], "three or more image pages (30%): OCR on them")
    r = route(["scan"] * 2)
    ok(r and r["ocr"] == [0, 1], "small image-only PDF: OCR")
    r = route(["layer"] * 10)
    ok(r and r["ocr"] == list(range(10)), "scan with an embedded OCR layer: redoes the OCR by default")
    ok(route(["layer"] * 10, ["--use-embedded-layer"]) is None, "--use-embedded-layer uses the embedded text")
    try:
        route(["empty"] * 4)
        ok(False, "a PDF with no content should raise an error")
    except ExtractionError:
        ok(True, "PDF with no text or images: refuses, unless the user asks for OCR")

    # ------------------------------------------------------------------ D. end-to-end with the real Tesseract
    print("\nD. end-to-end with real OCR (English)")
    common = ["--yes", "--source", "en", "--target", "pt", "--review", "none", "--no-preview", "--export-only"]

    t0 = time.time()
    cod, out = run([str(sm / "scan.pdf"), "--output", str(tmp / "d1"), *common], tmp)
    proj = tmp / "d1" / "scan"
    book = proj / "extracted" / "book.json"
    ok(cod == 0, f"PDF scan: exits with code 0 (got {cod}; {time.time() - t0:.0f}s)")
    if cod != 0:
        print(out[-1500:])
    ps = paragraphs(book) if book.exists() else []
    ok(len(ps) == len(expected), f"{len(expected)} paragraphs recognised (got {len(ps)})")
    sim = similarity(" ".join(ps), expected_joined)
    ok(sim >= 0.985, f"text faithful to the original: {sim:.2%} similarity (minimum 98.5%)")
    ok(not any("The Lighthouse" in p for p in ps), "repeated headers removed from the body")
    ok(not any(re.fullmatch(r"\d{1,3}", p.strip()) for p in ps), "page numbers removed")
    ok(len(blocks_of_kind(book, "verse")) == 1 and "keeper rests unseen" in blocks_of_kind(book, "verse")[0], "poem recognised as verse, with the breaks")
    ok(len(blocks_of_kind(book, "note")) == 1 and "coastal administration" in blocks_of_kind(book, "note")[0], "footnote recognised")
    caps = json.loads(book.read_text(encoding="utf-8"))["chapters"]
    ok(len(caps) == 4 and caps[0]["blocks"][0]["text"] == "The Lighthouse", f"title page + 3 chapters (got {len(caps)})")
    state = json.loads((proj / "state.json").read_text(encoding="utf-8"))["stages"]
    cfg = json.loads((proj / "config.json").read_text(encoding="utf-8"))
    ok(state["extraction"] and state["ocr_reviewed"] and state["language"], "extraction, OCR and language stages marked")
    ok(cfg["source"] == "en" and cfg["ocr_language"] == "eng", "OCR language and model saved in the project")
    ok((proj / "review" / "ocr_quality.md").exists() and len(list((proj / "ocr").glob("p*.json"))) == 6, "quality report and per-page OCR saved")
    ok(len(list((proj / "pages").glob("p*.jpg"))) == 6, "page images saved for the review")
    ok(any((proj / "output").glob("*.epub")), "exported the EPUB of the scanned book")
    t0 = time.time()
    cod, out = run([str(sm / "scan.pdf"), "--output", str(tmp / "d1"), *common], tmp)
    ok(cod == 0 and "Resuming" in out and "OCR done" not in out, "running again resumes without redoing the OCR")

    # loose images in a folder, one of them upside down
    img_folder = tmp / "scan_pages"
    samples.make_images(sm / "lighthouse.pdf", img_folder)
    if has_osd:
        p3 = img_folder / "page_003.jpg"
        Image.open(p3).rotate(180).save(p3, quality=75)
    cod, out = run([str(img_folder), "--output", str(tmp / "d2"), *common], tmp)
    proj2 = tmp / "d2" / "scan-pages-book"
    ok(cod == 0 and proj2.exists(), f"folder of images: exits with code 0 and creates the project (code {cod})")
    if cod != 0:
        print(out[-1500:])
    ps2 = paragraphs(proj2 / "extracted" / "book.json") if proj2.exists() else []
    sim2 = similarity(" ".join(ps2), expected_joined)
    ok(len(ps2) == len(expected) and sim2 >= 0.97,
       f"folder of images{' (one upside down)' if has_osd else ''}: {len(ps2)} paragraphs, {sim2:.2%} similarity")
    ok(len(list((proj2 / "original").glob("*.jpg"))) == 6, "the images were copied into the project")
    cod, out = run([str(img_folder), "--output", str(tmp / "d2"), *common], tmp)
    ok(cod == 0 and "Resuming" in out and not (tmp / "d2" / "scan-pages-book-2").exists(), "the same folder of images reopens the project (no duplicate)")

    # two pages per image
    samples.make_scan(sm / "lighthouse.pdf", sm / "doubles.pdf", doubles=True)
    cod, out = run([str(sm / "doubles.pdf"), "--output", str(tmp / "d3"), *common], tmp)
    proj3 = tmp / "d3" / "doubles"
    ps3 = paragraphs(proj3 / "extracted" / "book.json") if (proj3 / "extracted" / "book.json").exists() else []
    ok(cod == 0 and "two pages per image" in out, "detects images with two pages and proposes to split")
    ok(len(list((proj3 / "ocr").glob("p*.json"))) == 6, "3 images became 6 pages")
    ok(similarity(" ".join(ps3), expected_joined) >= 0.97 and len(ps3) == len(expected),
       f"text of the double pages in the right order ({similarity(' '.join(ps3), expected_joined):.2%})")
    cod, out = run([str(sm / "doubles.pdf"), "--output", str(tmp / "d3"), *common, "--ocr-force", "--double-pages", "no"], tmp)
    ok(cod == 0 and "double-page mode changed" in out and len(list((proj3 / "ocr").glob("p*.json"))) == 3,
       "changing the double-page mode redoes the OCR with the new numbering (no leftovers from the previous one)")

    # mixed PDF: 6 text pages followed by 6 scanned ones (the same book twice)
    merge_pdfs(sm / "mixed.pdf", [(sm / "lighthouse.pdf", list(range(6))), (sm / "scan.pdf", list(range(6)))])
    without_source = [a for a in common if a not in ("--source", "en")]
    cod, out = run([str(sm / "mixed.pdf"), "--output", str(tmp / "d4"), *without_source], tmp)
    proj4 = tmp / "d4" / "mixed"
    ps4 = paragraphs(proj4 / "extracted" / "book.json") if (proj4 / "extracted" / "book.json").exists() else []
    ps4 = [p for p in ps4 if len(p) > 40]  # ignore the short lines of the title page
    ok(cod == 0 and "OCR on 6 page(s) and direct text reading on 6" in out, "mixed PDF: plan with OCR and direct text")
    ok("Language detected from the PDF's selectable text" in out and "English" in out, "mixed PDF: the language comes from the selectable text, no sample OCR")
    ok(len(list((proj4 / "ocr").glob("p*.json"))) == 6, "OCR only on the 6 scanned pages")
    sim4 = similarity(" ".join(ps4), " ".join(expected + expected))
    ok(len(ps4) == 2 * len(expected) and sim4 >= 0.985, f"mixed: text from both sources, each in its place ({len(ps4)} paragraphs, {sim4:.2%})")
    ok(bool(ps4) and ps4[0].startswith("The wind came") and ps4[len(expected)].startswith("The wind came"), "mixed: first the text passage, then the scanned one")

    # scanned PDF with an embedded text layer (generated by Tesseract itself)
    layer_ok = False
    try:
        parts = []
        imgs = samples.render_pdf(sm / "scan.pdf", 200, [1, 2, 3])
        for k, im in enumerate(imgs):
            png = sm / f"c{k}.png"
            im.save(png)
            r = subprocess.run([exe, str(png), str(sm / f"c{k}"), "-l", "eng", "pdf"], capture_output=True, text=True)
            if r.returncode == 0 and (sm / f"c{k}.pdf").exists():
                parts.append((sm / f"c{k}.pdf", [0]))
        if len(parts) == 3:
            merge_pdfs(sm / "layer.pdf", parts)
            layer_ok = pdf_text.classify_pages(sm / "layer.pdf") == ["layer"] * 3
    except Exception:  # noqa: BLE001
        layer_ok = False
    if layer_ok:
        cod, out = run([str(sm / "layer.pdf"), "--output", str(tmp / "d5"), *common], tmp)
        ok(cod == 0 and "text layer" in out and "OCR done" in out, "scan with embedded text: redoes the OCR by default and warns")
        cod, out = run([str(sm / "layer.pdf"), "--output", str(tmp / "d6"), *common, "--use-embedded-layer"], tmp)
        ok(cod == 0 and "OCR done" not in out and "Extracting the content" in out, "--use-embedded-layer extracts the embedded text without OCR")
    else:
        print("  (skipped: this machine's Tesseract did not generate a PDF with a text layer)")

    # ------------------------------------------------------------------ E. review: bad page, screen, edit, pause, resume
    print("\nE. OCR review: bad page flagged, review screen, pause and resume")
    bad_folder = tmp / "bad_scan"
    bad_folder.mkdir()
    for i, im in enumerate(samples.render_pdf(sm / "lighthouse.pdf", 200), 1):
        d = (samples.degrade(im, 1.0, 60.0, blur=2.2, seed=3) if i == 3 else samples.degrade(im, 1.0, 5.0, seed=i))
        d.save(bad_folder / f"page_{i:03d}.jpg", quality=75)
    args_e = [str(bad_folder), "--output", str(tmp / "e"), "--source", "en", "--target", "pt", "--no-browser",
              "--no-preview", "--export-only"]
    proj_e = tmp / "e" / "bad-scan-book"
    screen: dict = {}

    def pause(url, p):
        idx = api(url, "/api/index")
        screen["flagged"] = [x["n"] for x in idx["pages"] if x["flagged"]]
        screen["filter"] = idx["filter"]
        pg = api(url, "/api/page/3")
        screen["boxes3"] = len(pg["boxes"])
        p.send_signal(signal.SIGINT)

    cod, out, url = run_interactive(args_e, tmp, pause)
    ok(url is not None and "127.0.0.1" in url, "the review screen opens (local address with a token)")
    ok(3 in screen.get("flagged", []), f"the degraded page (3) is among the flagged ones: {screen.get('flagged')}")
    ok(len(screen.get("flagged", [])) < 6 and screen.get("filter") == "flagged", "the initial list shows only the flagged ones")
    ok(screen.get("boxes3", 0) > 0, f"the bad page brings boxes on the doubtful words ({screen.get('boxes3')})")
    ok(cod == 0 and "Exiting" in out, "Ctrl+C in the review pauses without an error")
    est = json.loads((proj_e / "state.json").read_text(encoding="utf-8"))["stages"]
    ok(not est["extraction"] and len(list((proj_e / "ocr").glob("p*.json"))) == 6, "pause: extraction not marked, OCR saved")

    mark = "PASSAGE-REVIEWED-IN-TEST"

    def edit(url, p):
        idx = api(url, "/api/index")
        pg = api(url, "/api/page/3")
        blocks = pg["blocks"]
        para = next(b for b in blocks if b["kind"] == "paragraph")
        para["text"] = mark + " " + para["text"]
        r = api(url, "/api/page/3", {"blocks": blocks, "reviewed": True})
        screen["saved"] = r["index"]["reviewed"] and r["index"]["edited"]
        cont = api(url, "/api/replace", {"from": "lighthouse", "to": "farol", "whole_word": True,
                                         "ignore_case": True, "apply": False})
        screen["count"] = cont["occurrences"]
        api(url, "/api/replace", {"from": "lighthouse", "to": "farol", "whole_word": True,
                                  "ignore_case": True, "apply": True})
        for x in idx["pages"]:
            if x["flagged"]:
                api(url, f"/api/mark/{x['n']}", {"reviewed": True})
        api(url, "/api/finish", {})

    cod, out, url = run_interactive(args_e, tmp, edit)
    ok(cod == 0 and "Resuming" in out and "already done before" in out, "resume: reopens the review without redoing the OCR")
    ok(screen.get("saved") is True and screen.get("count", 0) >= 2, f"edit saved and global replace counted {screen.get('count')}")
    book_e = proj_e / "extracted" / "book.json"
    ps_e = paragraphs(book_e)
    ok(any(mark in p for p in ps_e), "the correction made on the screen reached the final book")
    ok(not any(re.search(r"\blighthouse\b", p, re.I) for p in ps_e) and any("farol" in p for p in ps_e),
       "the global replace applied to the whole book")
    ok(abs(len(ps_e) - len(expected)) <= 3, f"the book structure stays coherent despite the bad page ({len(ps_e)} paragraphs for {len(expected)})")
    rep = (proj_e / "review" / "ocr_quality.md").read_text(encoding="utf-8")
    ok("Review done on screen: yes" in rep and "| 3 |" in rep, "the report records the review and page 3")
    est = json.loads((proj_e / "state.json").read_text(encoding="utf-8"))["stages"]
    ok(est["extraction"] and est["ocr_reviewed"], "after finishing the review, extraction is marked as done")

    # reopen the review of an already-extracted project (--review-ocr)
    mark2 = "SECOND-REVIEW"

    def edit2(url, p):
        pg = api(url, "/api/page/2")
        b = next(x for x in pg["blocks"] if x["kind"] == "paragraph")
        b["text"] = mark2 + " " + b["text"]
        api(url, "/api/page/2", {"blocks": pg["blocks"], "reviewed": True})
        api(url, "/api/finish", {})

    cod, out, url = run_interactive([str(proj_e), "--no-browser", "--no-preview", "--export-only", "--review-ocr"], tmp, edit2)
    ps_e2 = paragraphs(book_e)
    ok(cod == 0 and any(mark2 in p for p in ps_e2) and any(mark in p for p in ps_e2),
       "--review-ocr reopens the review and keeps the previous edits")

    # ------------------------------------------------------------------ F. the screen in the browser (Playwright)
    print("\nF. review screen in the browser")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sync_playwright = None
        print("  (skipped: Playwright is not installed)")
    if sync_playwright:
        from pharos.ocr import layout, engine, server

        ns = sorted(int(p.stem[1:]) for p in (proj_e / "ocr").glob("p*.json"))
        pages = engine.load_pages(proj_e / "ocr", ns)
        auto_b, _ = layout.analyze_document(pages)
        pts = quality.score_all(pages)
        sug_ui = [{"from": "Anna", "to": "Anne", "n": 1, "pages": [3], "conf": 40, "freq_to": 5}]
        rev_ui = Review(tmp / "rev_ui.json", pages[ns[0]]["hash"])
        ses_ui = Session("Screen test", pages, auto_b, pts, sug_ui, rev_ui, proj_e / "pages", initial_filter="flagged")
        srv = server.start_in_thread(ses_ui)
        js_errors: list[str] = []
        chromium_exe = Path("/opt/pw-browsers/chromium")
        try:
            with sync_playwright() as pw:
                nav = pw.chromium.launch(executable_path=str(chromium_exe)) if chromium_exe.exists() else pw.chromium.launch()
                pg = nav.new_context(viewport={"width": 1400, "height": 900}).new_page()
                pg.on("pageerror", lambda e: js_errors.append(str(e)))
                pg.on("console", lambda m: js_errors.append(m.text) if m.type == "error" else None)
                pg.on("dialog", lambda d: d.accept())
                pg.goto(srv.url)
                pg.wait_for_selector("#blocks textarea")
                pg.wait_for_function("document.querySelector('#img').naturalWidth > 0")
                ok(pg.locator("#list .item").count() >= 1, "page list drawn")
                ok("Screen test" in pg.title(), "the page title shows the book")
                pg.locator('#list .item[data-n="3"]').click()
                pg.wait_for_function("document.querySelector('#page-title').textContent.includes('Page 3')")
                pg.wait_for_timeout(300)
                ok(pg.locator(".box").count() > 0, f"boxes on the doubtful words of page 3 ({pg.locator('.box').count()})")
                ok(pg.locator("#sug .chip").count() == 1, "correction suggestion shown")
                n0 = pg.locator("#blocks textarea").count()
                # block 0 of the degraded page is a short, badly read header; block 1 is a paragraph
                ta = pg.locator("#blocks textarea").nth(1)
                ta.click()
                pg.keyboard.press("Home")
                for _ in range(25):
                    pg.keyboard.press("ArrowRight")
                pg.get_by_text("✂ split").nth(1).click()
                ok(pg.locator("#blocks textarea").count() == n0 + 1, "split block at the cursor")
                pg.get_by_text("↑ merge").nth(1).click()
                ok(pg.locator("#blocks textarea").count() == n0, "merge with the previous block")
                pg.locator("#blocks textarea").nth(1).fill("Text corrected on the screen.")
                pg.keyboard.press("Control+Enter")
                pg.wait_for_timeout(600)
                ok(ses_ui.review.reviewed(3) and ses_ui.review.blocks(3)[1]["text"] == "Text corrected on the screen.",
                   "Ctrl+Enter saves the page and marks it as reviewed")
                ok("1 of" in pg.locator("#prog-txt").inner_text(), "review progress updated")
                pg.select_option("#filter", "all")
                pg.locator("#global summary").click()
                pg.fill("#from", "lighthouse")
                pg.fill("#to", "farol")
                pg.check("#case")
                pg.click("#count")
                pg.wait_for_timeout(300)
                ok("occurrence" in pg.locator("#sub-res").inner_text(), "count of the global replace")
                pg.click("#replace")
                pg.wait_for_timeout(600)
                ok(all("lighthouse" not in b["text"].lower() for n in ns for b in ses_ui.blocks(n)), "global replace applied")
                pg.click("#finish")
                pg.wait_for_timeout(800)
                ok(srv.finished and pg.locator(".end-screen h1").count() == 1, "finishing closes the review")
                nav.close()
        finally:
            with contextlib.suppress(Exception):
                srv.server_close()
        ok(not js_errors, f"no errors in the browser console {js_errors[:2] if js_errors else ''}")

    # ------------------------------------------------------------------ G. full translation of a scan (simulated Ollama)
    print("\nG. scan → OCR → translation (simulated Ollama) → export")
    from test_flow import Fake

    with Fake(tmp) as srv_f:
        cod, out = run([str(sm / "scan.pdf"), "--output", str(tmp / "g"), "--ollama", srv_f.url, "--yes", "--source", "en",
                        "--target", "pt", "--review", "none", "--no-preview", "--genre", "fiction"], tmp, timeout=400)
        reqs = srv_f.requests
    proj_g = tmp / "g" / "scan"
    ok(cod == 0, f"scan translated end-to-end (code {cod})")
    if cod != 0:
        print(out[-1500:])
    est = json.loads((proj_g / "state.json").read_text(encoding="utf-8"))["stages"] if (proj_g / "state.json").exists() else {}
    ok(est and all(est.values()), f"all stages completed: {est}")
    ok(len(reqs) > 0 and any("lighthouse" in json.dumps(r, ensure_ascii=False).lower() for r in reqs), f"the OCR text was sent to the model ({len(reqs)} requests)")
    ok(any((proj_g / "output").glob("*.docx")) and any((proj_g / "output").glob("*.epub")), "DOCX and EPUB generated")

    print(f"\n{'ALL GOOD' if not failures else str(failures) + ' check(s) failed'} — files in {tmp}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
