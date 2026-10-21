#!/usr/bin/env python3
"""End-to-end test, without a real Ollama (uses tests/fake_ollama.py).

Usage:  python tests/test_flow.py
Needs, for the tests only: reportlab and pillow (they generate the sample PDF).
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
PY = sys.executable

import samples  # noqa: E402

failures = 0


def ok(cond: bool, text: str) -> None:
    global failures
    print(("  ok    " if cond else "  FAIL  ") + text)
    if not cond:
        failures += 1


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Fake:
    def __init__(self, tmp: Path, **env):
        self.log = tmp / f"fake_{time.time_ns()}.jsonl"
        self.env = {**os.environ, "FAKE_LOG": str(self.log), **env}

    def __enter__(self):
        self.port = free_port()
        self.proc = subprocess.Popen([PY, str(ROOT / "tests" / "fake_ollama.py"), str(self.port)], env=self.env)
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/tags", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        return self

    def __exit__(self, *a):
        self.proc.terminate()
        self.proc.wait()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def requests(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines() if l.strip()]


def run(args: list[str], cwd: Path, input_: str | None = None, timeout: int = 300):
    r = subprocess.run([PY, str(ROOT / "pharos.py"), *args], input=input_, capture_output=True,
                       text=True, cwd=cwd, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def last_content(req: dict) -> str:
    return req["messages"][-1]["content"]


def all_content(req: dict) -> str:
    return "\n".join(m["content"] for m in req["messages"])


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pharos_test_"))
    sm = tmp / "samples"
    sm.mkdir()
    samples.make_pdf(sm / "leuchtturm_indent.pdf", "indent")
    samples.make_txt(sm / "leuchtturm.txt")
    samples.make_epub(sm / "leuchtturm.epub")
    print(f"Test folder: {tmp}\n")

    # ------------------------------------------------------------------ A. units
    print("A. cleanup and language detection")
    from pharos.language import detect_snippet
    from pharos.cleanup import join_lines

    ok(join_lines(["Der Leucht-", "turm steht am Meer"]) == "Der Leuchtturm steht am Meer", "hyphenation undone")
    ok(join_lines(["die Ein-", "und Ausgänge"]) == "die Ein- und Ausgänge", "enumeration hyphen preserved")
    language_samples = {
        "de": "Der Wind kam am frühen Morgen von Westen und brachte den Geruch von Salz und feuchtem Holz mit sich.",
        "en": "The wind came from the west early in the morning and brought with it the smell of salt and wet wood.",
        "es": "El viento llegó del oeste temprano por la mañana y trajo consigo el olor de la sal y de la madera húmeda.",
        "pt": "O vento chegou do oeste de manhã cedo e trouxe consigo o cheiro do sal e da madeira úmida.",
        "fr": "Le vent est venu de l'ouest tôt le matin et a apporté avec lui l'odeur du sel et du bois humide.",
        "it": "Il vento arrivò da ovest di primo mattino e portò con sé l'odore del sale e del legno umido.",
        "la": "Et in principio erat verbum, et verbum erat apud deum, et deus erat verbum, quod non est in hoc mundo.",
    }
    for code, text in language_samples.items():
        r = detect_snippet(text)
        ok(bool(r) and r[0] == code, f"language {code} recognised ({r[0] if r else None})")

    # ------------------------------------------------------------------ B. EPUB, automatic mode
    print("\nB. EPUB, automatic mode (--yes), with injected failures")
    with Fake(tmp, FAKE_FAIL_ONCE="Postbote", FAKE_FAIL_ALWAYS="Kompass") as srv:
        cod, out = run([str(sm / "leuchtturm.epub"), "--output", str(tmp / "b"), "--ollama", srv.url,
                        "--yes", "--target", "pt"], tmp)
        reqs = srv.requests
    proj = tmp / "b" / "leuchtturm"
    ok(cod == 0, f"exits with code 0 (got {cod})")
    for ext in ("epub", "docx", "md", "bilingual.html"):
        ok(any(proj.glob(f"output/*.{ext}")), f"generated .{ext}")
    rep = (proj / "review" / "report.md").read_text(encoding="utf-8")
    ok("Kompass" in rep and "review" in rep, "report lists the passage that exhausted the attempts")
    ok(sum("IMPORTANT: your previous answer was rejected" in last_content(r) for r in reqs) == 3,
       "3 retries with the reason in the prompt (1 from 'Postbote' + 2 from 'Kompass')")
    ok(any("Context (previous passages" in last_content(r) for r in reqs), "previous paragraphs sent as context")
    from pharos.extractors import epub as ex_epub
    book_orig, _ = ex_epub.extract(sm / "leuchtturm.epub")
    book_trad, _ = ex_epub.extract(next(proj.glob("output/*.epub")))
    ok(sum(len(c.blocks) for c in book_orig.chapters) == sum(len(c.blocks) for c in book_trad.chapters),
       "translated EPUB has the same structure as the original (same block count)")
    ok([b.kind for b in book_orig.blocks() if b.kind != "note"] == [b.kind for b in book_trad.blocks() if b.kind != "note"]
       or [b.kind for b in book_orig.blocks()] == [b.kind for b in book_trad.blocks()], "block kinds (title, verse, separator) preserved")
    from docx import Document
    ok(len(Document(str(next(proj.glob("output/*.docx")))).paragraphs) > 10, "DOCX opens and has content")

    # ------------------------------------------------------------------ C. PDF with a scanned page
    print("\nC. PDF with one scanned page")
    with Fake(tmp) as srv:
        cod, out = run([str(sm / "leuchtturm_indent.pdf"), "--output", str(tmp / "c"), "--ollama", srv.url,
                        "--yes", "--target", "pt"], tmp)
    proj = tmp / "c" / "leuchtturm-indent"
    ok(cod == 0, f"exits with code 0 (got {cod})")
    ok("mixed" in out and "--ocr-images" in out, "text PDF with one image page: warns and proceeds with the text only")
    ok((proj / "review" / "pages_without_text.txt").exists(), "lists the pages without text in review/")
    book = json.loads((proj / "extracted" / "book.json").read_text(encoding="utf-8"))
    ok(len(book["chapters"]) == 3, f"chapters detected in the PDF: {len(book['chapters'])} (expected 3: title + 2)")
    texts = " ".join(b["text"] for c in book["chapters"] for b in c["blocks"])
    ok("Leuchtturm" in texts and "Leucht-" not in texts and "¹" in texts, "hyphenation undone and note preserved")
    ok("Küstenverwaltung" in texts, "footnote extracted")

    # ------------------------------------------------------------------ D. image-only PDF
    print("\nD. image-only PDF (pure scan) and Ollama off")
    from reportlab.pdfgen import canvas
    from PIL import Image
    scan = sm / "scan.pdf"
    c = canvas.Canvas(str(scan), pagesize=(420, 595))
    Image.new("L", (840, 1190), 230).save(sm / "p.png")
    for _ in range(3):
        c.drawImage(str(sm / "p.png"), 0, 0, width=420, height=595)
        c.showPage()
    c.save()
    cod, out = run([str(scan), "--output", str(tmp / "d"), "--yes", "--target", "pt"], tmp)
    ok(cod == 1 and ("OCR" in out or "Tesseract" in out), "pure scan with no known language: asks for --source/--ocr-lang (code 1)")
    cod, out = run([str(sm / "leuchtturm.txt"), "--output", str(tmp / "d2"), "--ollama", "http://127.0.0.1:1",
                    "--yes", "--target", "pt"], tmp)
    ok(cod == 1 and "Ollama" in out, "Ollama unavailable: clear message, no traceback")

    # ------------------------------------------------------------------ E. interactive: preview and refinement
    print("\nE. TXT, interactive: 3-page preview and the tuning menu")
    input_ = "\n".join([
        "",                         # source language (accept the detection)
        "", "",                     # title, author
        "",                         # target language (pt)
        "",                         # glossary (empty)
        "",                         # formats
        "n",                        # no automatic profile suggestion
        "occult",                   # genre
        "Test novella",             # work
        "t", "New tone: solemn test text",
        "r", "Never translate proper names",
        "g", "Leuchtturm", "Farol", "",
        "c",
        "e", "2", "Translation corrected by hand by the test user.", "",
        "o",
        "m", "gemma3:12b",
        "a",
    ]) + "\n"
    with Fake(tmp) as srv:
        cod, out = run([str(sm / "leuchtturm.txt"), "--output", str(tmp / "e"), "--ollama", srv.url], tmp, input_)
        reqs = srv.requests
    proj = tmp / "e" / "leuchtturm"
    ok(cod == 0, f"exits with code 0 (got {cod})\n{out[-600:] if cod else ''}")
    versions = sorted(p.name for p in (proj / "preview").iterdir())
    ok(len(versions) == 8, f"one folder per preview version: {versions}")
    everything = [all_content(r) for r in reqs]
    ok(any("New tone: solemn test text" in t for t in everything), "[t] new tone reached the prompt")
    ok(any("Never translate proper names" in t for t in everything), "[r] new rule reached the prompt")
    ok(any("Leuchtturm -> Farol" in t for t in everything), "[g] glossary term reached the prompt")
    ok(any("Examples of the desired translation style" in t and "corrected by hand" in t for t in everything),
       "[e] correction became an example in the prompt")
    ok(any("commentary. Please translate the following" in t for t in everything), "[c] base (official) prompt used in the comparison")
    ok("Without profile (base)" in (proj / "preview" / "v5" / "preview.html").read_text(encoding="utf-8"),
       "[c] preview HTML has the 'without profile' column")
    ok(any(r["model"] == "gemma3:12b" and r["messages"][0]["role"] == "system" for r in reqs),
       "[m] model changed and 'chat' prompt style (system message)")
    profile = json.loads((proj / "profile.json").read_text(encoding="utf-8"))
    ok(profile["register"].startswith("New tone") and len(profile["examples"]) == 1 and "Never translate proper names" in profile["rules"],
       "profile.json keeps the tone, rule and example")
    md = next(proj.glob("output/*.md")).read_text(encoding="utf-8")
    ok("Translation corrected by hand by the test user." in md, "hand correction appears in the final translation")
    ok("Farol" in md, "glossary applied and validated in the final translation")
    state = json.loads((proj / "state.json").read_text(encoding="utf-8"))["stages"]
    ok(all(state.values()), f"all stages completed: {state}")

    # ------------------------------------------------------------------ F. interruption and resume
    print("\nF. Ctrl+C in the middle of the translation and resume")
    with Fake(tmp, FAKE_DELAY="0.7") as srv:
        p = subprocess.Popen([PY, str(ROOT / "pharos.py"), str(sm / "leuchtturm.epub"), "--output", str(tmp / "f"),
                              "--ollama", srv.url, "--yes", "--target", "pt", "--no-preview", "--genre", "fiction"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=tmp)
        time.sleep(5)
        p.send_signal(signal.SIGINT)
        out_p = p.communicate(timeout=30)[0]
        done = len(srv.requests)
    proj = tmp / "f" / "leuchtturm"
    cache_lines = len((proj / "translated" / "cache.jsonl").read_text(encoding="utf-8").splitlines())
    ok(p.returncode == 130 and "Interrupted" in out_p, f"Ctrl+C: code 130 and the resume message (code {p.returncode})")
    ok(0 < cache_lines < 13, f"part of the translation was saved in the cache ({cache_lines} of 13)")
    with Fake(tmp) as srv:
        cod, out = run([str(sm / "leuchtturm.epub"), "--output", str(tmp / "f"), "--ollama", srv.url, "--yes",
                        "--target", "pt", "--no-preview", "--genre", "fiction"], tmp)
        new = len(srv.requests)
    ok(cod == 0 and "Resuming" in out, "resumes the existing project without asking again")
    ok(new == 13 - cache_lines, f"only translated what was missing ({new} new requests; {13 - cache_lines} were missing)")
    ok(any((proj / "output").glob("*.epub")), "exported at the end")

    print(f"\n{'ALL GOOD' if not failures else str(failures) + ' check(s) failed'} — files in {tmp}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
