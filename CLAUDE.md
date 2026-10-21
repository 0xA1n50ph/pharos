# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A CLI that translates whole books with a **local Ollama model** (default `translategemma:12b`).
It handles PDF (with text or scanned), folders of page images, EPUB, TXT/Markdown, and — with Calibre —
MOBI/AZW3/FB2/DOCX. Scanned books go through **Tesseract OCR** plus a browser-based human review screen
before translation. The codebase (code, UI messages, docstrings, identifiers, on-disk keys) is in **English**;
only the prompt text sent to the model was always English anyway.

## Commands

```bash
./setup.sh                 # check + install everything (Python/.venv, libs, Tesseract, Ollama, models); idempotent
./setup.sh --check         # diagnose only, change nothing
./setup.sh --dev           # also install test deps (reportlab, playwright + Chromium)
./pharos.sh book.pdf        # shortcut created by setup.sh: runs via .venv, applies saved .setup.conf
python pharos.py book.pdf           # same thing, without the shortcut
python pharos.py scan.pdf --extract-only   # OCR + extract text, then stop (no Ollama needed)
python pharos.py book/ --export-only       # skip translation, re-export the cache
```

`--extract-only` and `--export-only` are the two stage short-circuits in `run_()`: the former returns
right after extraction + language; the latter skips profile/translation. Any other partial run relies on the
`state.json` stage flags — re-running resumes from the first incomplete stage.

There is **no build step and no linter**. Runtime deps (`requirements.txt`) are stdlib-only plus a few
extraction libs. `pharos/ollama.py`, `pharos/language.py`, the OCR review server, and EPUB export are
written against the **standard library only** — keep new core code dependency-light.

### Tests

Tests are **standalone scripts**, not pytest; run each with the `.venv` Python. Each prints `ALL GOOD` or a
failure count and exits 0/1. There is no per-test selector — to run a subset, run a specific script.

```bash
python tests/test_flow.py     # full pipeline vs a fake Ollama (needs reportlab, pillow)
python tests/test_ocr.py      # OCR + review; needs real Tesseract ('eng'); browser test uses Playwright if present
python tests/test_ai.py       # AI OCR-correction suggestions (parts A–D need no Tesseract/Ollama)
python tests/test_setup.py    # setup.sh against fake brew/apt/ollama/tesseract/GitHub (1–3 min)
```

`tests/fake_ollama.py` is a fake Ollama server (`/api/tags`, `/api/chat`, `/api/generate`, `/api/pull`) that
fabricates size-matched "translations", applies glossary terms, and **injects failures** via env vars
(`FAKE_FAIL_ONCE`, `FAKE_OCR_SWAPS`, `FAKE_PULL_MODE`, etc.) to exercise validation, retries, and
resume. `tests/samples.py` generates sample books (German/English PDFs, degraded scans, EPUB, TXT).

## Architecture

### Pipeline (orchestrated by `pharos.py:run_`)

Six stages, each guarded by a boolean in the project's `state.json` so a re-run resumes instead of redoing:

1. **Extraction** — detect file type by content (`detection.py`), route to an extractor or to OCR.
2. **Language** — detect source language by sampling paragraphs (`language.py`), confirm with user.
3. **Configuration** — title, author, target language, glossary, output formats.
4. **Profile & preview** — build a translation profile, translate ~3 pages, show side-by-side HTML, let the user refine.
5. **Translation** — translate block by block with retries + validation; resumable.
6. **Export** — EPUB, DOCX, Markdown, bilingual HTML, plus `review/report.md`.

### The internal `Book` model is the pivot (`pharos/model.py`)

`Book → Chapter → Block`. **Every extractor and the OCR pipeline produce a `Book`; translation and export
consume only a `Book`.** A `Block` has a `kind` (`title`, `paragraph`, `verse`, `note`, `caption`,
`separator`) that drives validation, prompting, and Markdown rendering. Block `id`s are stable
(`c003p00045`) once `renumber()` runs — the translation cache keys off block content, not position.

### Project folder = all state on disk (`pharos/project.py`)

Each book gets a self-contained folder (`original/`, `extracted/`, `ocr/`, `pages/`, `preview/vN/`,
`translated/`, `review/`, `output/`) plus top-level `config.json`, `state.json`, `profile.json`,
`glossary.tsv`. `Project` owns every path and the stage flags. Running the tool on an existing project folder
(or the same source file) resumes it. Everything is written incrementally so `Ctrl+C` loses nothing.

### Translation engine (`pharos/translation.py`)

- `Translator.resolve()` returns a result **without calling the model** when possible: non-translatable block,
  a hand correction (`Corrections`), a cache hit (`TranslationCache`), or text already in the target language.
- `TranslationCache` is append-only JSONL (one line per block, flushed immediately → resumable). Cache **key** is
  a hash of text + block kind + langs + model + style + profile hash + glossary hash + context size, so any
  change to the profile or glossary re-translates only affected blocks, and OCR edits re-translate only edited text.
- After each model call, `_validate()` checks: empty, still-source-language, identical to source, length ratio,
  extra line breaks, verse line count, glossary application. On failure it retries up to `max_attempts`,
  feeding the specific problem back into the prompt. Exhausted blocks keep the best version and are flagged
  `review` in the report.
- `_with_retry()` wraps calls with backoff so a transient Ollama outage doesn't abort the run.

### Profile & prompts (`pharos/profile.py`)

`Profile` (genre, work description, register, fidelity rules, approved examples) is the context handed to the
model. `build_messages()` has two prompt styles: **`translategemma`** (single user message in the model's
official format, extras inserted between role sentence and request) and **`chat`** (system + few-shot examples
+ user), auto-selected by model name via `model_style()`. Prompt text is English; the `GENRES` and
`DEFAULT_RULES` tables hold the English register/rule strings.

### OCR subsystem (`pharos/ocr/`, entered via `pharos/ocr_stage.py`)

Pipeline: `engine.py` runs Tesseract per page in parallel with a per-page cache (resumable); `preprocess.py`
deskews/relights/rotates; `layout.py` turns words+boxes into typed blocks (strips headers/page numbers,
detects titles/verse/notes/paragraphs, undoes hyphenation); `assembly.py` assembles the `Book`;
`quality.py` scores pages and flags low-confidence ones. `review.py` + `server.py` run a **localhost-only,
token-protected** browser review screen (`review.html`; writes `review/ocr_review.json` on every action, resumable).
`ai_suggestions.py` asks a general model for OCR fixes but **the model only suggests** — heavy validation bars
changes to high-confidence words, numbers, old spelling, additions, or rewrites; nothing enters the text
without a human click. The OCR-suggestion model is separate from the translation model (`--ocr-model`),
because TranslateGemma tends to translate rather than correct.

### setup.sh

A single bash script (compatible with macOS's **bash 3.2** — avoid bash 4+ features), run repeatedly; each of
its 7 steps only touches what's missing. It never runs a `sudo`/download step without confirmation, and with
no TTY and no `-y` it only prepares the `.venv`. It remembers choices (remote Ollama URL, model names) in
`.setup.conf`, which the generated `./pharos.sh` shortcut reads. `test_setup.py` exercises it against fakes.
