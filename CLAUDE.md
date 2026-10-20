# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A CLI that translates whole books with a **local Ollama model** (default `translategemma:12b`).
It handles PDF (with text or scanned), folders of page images, EPUB, TXT/Markdown, and — with Calibre —
MOBI/AZW3/FB2/DOCX. Scanned books go through **Tesseract OCR** plus a browser-based human review screen
before translation. Everything (code, UI prompts, docstrings, variable names) is in **Portuguese**; only the
text sent to the model is in English (models follow it better). Match that convention when editing.

## Commands

```bash
./setup.sh                 # verify + install everything (Python/.venv, libs, Tesseract, Ollama, models); idempotent
./setup.sh --verificar     # diagnose only, change nothing
./setup.sh --dev           # also install test deps (reportlab, playwright + Chromium)
./pharos.sh livro.pdf       # shortcut created by setup.sh: runs via .venv, applies saved .setup.conf
python pharos.py livro.pdf          # same thing, without the shortcut
python pharos.py scan.pdf --extrair-apenas   # OCR + extract text, then stop (no Ollama needed)
python pharos.py livro/ --exportar-apenas    # skip translation, re-export the cache
```

`--extrair-apenas` and `--exportar-apenas` are the two stage short-circuits in `executar()`: the former returns
right after extração + idioma; the latter skips perfil/tradução. Any other partial run relies on the
`estado.json` stage flags — re-running resumes from the first incomplete stage.

There is **no build step and no linter**. Runtime deps (`requirements.txt`) are stdlib-only plus a few
extraction libs. `pharos/ollama.py`, `pharos/idioma.py`, the OCR review server, and EPUB export are
written against the **standard library only** — keep new core code dependency-light.

### Tests

Tests are **standalone scripts**, not pytest; run each with the `.venv` Python. Each prints `TUDO CERTO` or a
failure count and exits 0/1. There is no per-test selector — to run a subset, run a specific script.

```bash
python tests/teste_fluxo.py    # full pipeline vs a fake Ollama (needs reportlab, pillow)
python tests/teste_ocr.py      # OCR + review; needs real Tesseract ('eng'); browser test uses Playwright if present
python tests/teste_ia.py       # AI OCR-correction suggestions (parts A–D need no Tesseract/Ollama)
python tests/teste_setup.py    # setup.sh against fake brew/apt/ollama/tesseract/GitHub (1–3 min)
```

`tests/ollama_falso.py` is a fake Ollama server (`/api/tags`, `/api/chat`, `/api/generate`, `/api/pull`) that
fabricates size-matched "translations", applies glossary terms, and **injects failures** via env vars
(`FALSO_FALHA_UMA_VEZ`, `FALSO_OCR_TROCAS`, `FALSO_PULL_MODO`, etc.) to exercise validation, retries, and
resume. `tests/amostras.py` generates sample books (German/English PDFs, degraded scans, EPUB, TXT).

## Architecture

### Pipeline (orchestrated by `pharos.py:executar`)

Six stages, each guarded by a boolean in the project's `estado.json` so a re-run resumes instead of redoing:

1. **Extração** — detect file type by content (`deteccao.py`), route to an extractor or to OCR.
2. **Idioma** — detect source language by sampling paragraphs (`idioma.py`), confirm with user.
3. **Configuração** — title, author, target language, glossary, output formats.
4. **Perfil e prévia** — build a translation profile, translate ~3 pages, show side-by-side HTML, let the user refine.
5. **Tradução** — translate block by block with retries + validation; resumable.
6. **Exportação** — EPUB, DOCX, Markdown, bilingual HTML, plus `revisao/relatorio.md`.

### The internal `Livro` model is the pivot (`pharos/modelo.py`)

`Livro → Capitulo → Bloco`. **Every extractor and the OCR pipeline produce a `Livro`; translation and export
consume only a `Livro`.** A `Bloco` has a `tipo` (`titulo`, `paragrafo`, `verso`, `nota`, `legenda`,
`separador`) that drives validation, prompting, and Markdown rendering. Block `id`s are stable
(`c003p00045`) once `renumerar()` runs — the translation cache keys off block content, not position.

### Project folder = all state on disk (`pharos/projeto.py`)

Each book gets a self-contained folder (`original/`, `extraido/`, `ocr/`, `paginas/`, `previa/vN/`,
`traduzido/`, `revisao/`, `saida/`) plus top-level `config.json`, `estado.json`, `perfil.json`,
`glossario.tsv`. `Projeto` owns every path and the stage flags. Running the tool on an existing project folder
(or the same source file) resumes it. Everything is written incrementally so `Ctrl+C` loses nothing.

### Translation engine (`pharos/traducao.py`)

- `Tradutor.resolver()` returns a result **without calling the model** when possible: non-translatable block,
  a hand correction (`Correcoes`), a cache hit (`CacheTraducao`), or text already in the target language.
- `CacheTraducao` is append-only JSONL (one line per block, flushed immediately → resumable). Cache **key** is
  a hash of text + block type + langs + model + style + profile hash + glossary hash + context size, so any
  change to the profile or glossary re-translates only affected blocks, and OCR edits re-translate only edited text.
- After each model call, `_validar()` checks: empty, still-source-language, identical to source, length ratio,
  extra line breaks, verse line count, glossary application. On failure it retries up to `max_tentativas`,
  feeding the specific problem back into the prompt. Exhausted blocks keep the best version and are flagged
  `revisar` in the report.
- `_com_reentrada()` wraps calls with backoff so a transient Ollama outage doesn't abort the run.

### Profile & prompts (`pharos/perfil.py`)

`Perfil` (genre, work description, register, fidelity rules, approved examples) is the context handed to the
model. `construir_mensagens()` has two prompt styles: **`translategemma`** (single user message in the model's
official format, extras inserted between role sentence and request) and **`chat`** (system + few-shot examples
+ user), auto-selected by model name via `estilo_do_modelo()`. Prompt text is English; the `GENEROS` and
`REGRAS_PADRAO` tables hold the English register/rule strings.

### OCR subsystem (`pharos/ocr/`, entered via `pharos/etapa_ocr.py`)

Pipeline: `motor.py` runs Tesseract per page in parallel with a per-page cache (resumable); `preprocessar.py`
deskews/relights/rotates; `layout.py` turns words+boxes into typed blocks (strips headers/page numbers,
detects titles/verse/notes/paragraphs, undoes hyphenation); `montagem.py` assembles the `Livro`;
`qualidade.py` scores pages and flags low-confidence ones. `revisao.py` + `servidor.py` run a **localhost-only,
token-protected** browser review screen (writes `revisao/ocr_revisao.json` on every action, resumable).
`sugestoes_ia.py` asks a general model for OCR fixes but **the model only suggests** — heavy validation bars
changes to high-confidence words, numbers, old spelling, additions, or rewrites; nothing enters the text
without a human click. The OCR-suggestion model is separate from the translation model (`--modelo-ocr`),
because TranslateGemma tends to translate rather than correct.

### setup.sh

A single bash script (compatible with macOS's **bash 3.2** — avoid bash 4+ features), run repeatedly; each of
its 7 steps only touches what's missing. It never runs a `sudo`/download step without confirmation, and with
no TTY and no `-y` it only prepares the `.venv`. It remembers choices (remote Ollama URL, model names) in
`.setup.conf`, which the generated `./pharos.sh` shortcut reads. `teste_setup.py` exercises it against fakes.
</content>
