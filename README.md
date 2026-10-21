<h1 align="center">Pharos 🗼</h1>

<p align="center">
  <strong>Translate whole books on your own computer</strong> — from gothic-type scanned PDFs to
  modern EPUBs — with a <strong>100% local</strong> AI model. Not a line of the book leaves your machine.
</p>

<p align="center">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="Platform: macOS and Linux" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey">
  <img alt="100% local AI" src="https://img.shields.io/badge/AI-100%25%20local%20(Ollama)-6E56CF">
  <img alt="MIT License" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="PRs welcome" src="https://img.shields.io/badge/PRs-welcome-brightgreen">
</p>

**Pharos** takes a book in almost any format and gives back a **faithful** translation — no summarising,
no "modernising", no inventing — using an [Ollama](https://ollama.com) model that runs offline on your
machine. Built with hard works in mind: old, occult, philosophical or literary texts, including the ones
that only exist as **gothic-type (Fraktur) scans**.

### ✨ Highlights

- 🔒 **100% local and private** — the work's text never goes to the cloud; it works without internet once installed.
- 📖 **Reads almost anything** — PDF (with text **or scanned**), folders of images, EPUB, TXT/Markdown and, with Calibre, MOBI/AZW3/FB2/DOCX.
- 🔍 **OCR with human review** — Tesseract + a browser screen to compare image and text and fix things *before* translating, with gothic-type support.
- 🎯 **Faithful translation, not "improved"** — a style profile (genre, tone, rules), a **mandatory glossary** and automatic validation that redoes the bad passages.
- 👀 **Preview before spending hours** — translates ~3 pages and shows them side by side so you can tune the tone; only then does the whole book run.
- ⏸️ **Resumable** — `Ctrl+C` at any time; the same command continues where it stopped (every paragraph is saved immediately).
- 📦 **Exports** EPUB, DOCX, Markdown and a bilingual HTML edition, plus a report of what deserves a review.

### 🚀 Quick start

```bash
git clone git@github.com:0xA1n50ph/pharos.git
cd pharos
./setup.sh                      # prepares everything (Python, Tesseract, Ollama, models), asking before installing
./pharos.sh my_book.pdf         # guided flow: detects the language, tunes the style in the preview and translates
```

> No interactive terminal? `setup.sh` never installs anything without confirmation. See [Installation](#installation) for the details and options.

### 📑 Table of contents

[Installation](#installation) · [Usage](#usage) · [The flow](#the-flow) · [Scanned books (OCR)](#scanned-books-ocr) ·
[Translation profile](#translation-profile) · [Glossary](#glossary) · [Validation and report](#validation-and-report) ·
[Project folder](#project-folder) · [Limitations](#limitations-of-this-version) · [Tests](#tests) ·
[Contributing](#contributing) · [License](#license)

## Installation

```bash
./setup.sh              # checks and prepares everything, asking before installing (macOS and Linux)
./pharos.sh book.pdf    # shortcut created by setup: uses the .venv environment without "activating" it
```

`setup.sh` checks and, for whatever is missing, prepares (each step only touches what is missing; run it as often as you like):

1. **System:** OS, memory (12B models ask for about 10 GB), `curl`, package manager (Homebrew, apt, dnf, pacman, zypper).
2. **Python 3.10+** and the `.venv` virtual environment (installs Python via the package manager, if you let it).
3. **Libraries** from `requirements.txt` inside the `.venv`, checked by import.
4. **OCR:** Tesseract and the languages `eng osd deu spa por` (via `brew`/`apt`; whatever the package does not bring is downloaded
   from Tesseract's official repository) and the **gothic-type** models `frk` and `deu_latf`.
5. **Ollama** (local or on another machine) and the models: `translategemma:12b` (required) and `gemma3:12b`
   (optional, used only for the OCR correction suggestions). Downloaded via Ollama's API, with a progress bar and
   retries; if it drops mid-way, the download resumes where it stopped.
6. **Optional:** Calibre (MOBI/AZW3/FB2/DOCX).
7. **Check:** opens the program itself and confirms it sees Tesseract; creates the `./pharos.sh` shortcut.

Nothing that asks for an administrator password (`sudo`) runs without your confirmation; with no interactive terminal and no `-y`, the
script does not install system packages or download models (it only prepares the `.venv`). Ollama's official installer on
Linux (`curl ... | sh`) only runs with `--install-ollama` or if you answer "yes" to the question. It exits with code 0
when the environment is ready and 1 when something required is missing (translation model, Python, libraries, Ollama).

| `setup.sh` option | Effect |
|---|---|
| `--check` | only diagnoses: installs and changes nothing (good for checking a machine) |
| `-y`, `--yes` | answers "yes" to routine things (risky questions, such as low disk space, stay "no") |
| `--ollama URL` | Ollama on another machine (e.g.: `http://pi.local:11434`); saved in `.setup.conf` and `./pharos.sh` starts using it |
| `--model NAME`, `--ocr-model NAME` | another translation model / another general model for the OCR suggestions (remembered in `.setup.conf`) |
| `--no-models`, `--no-ocr-model` | do not download models (only check) / do not download the OCR-suggestions model |
| `--no-ocr`, `--languages fra,ita` | skip Tesseract / add OCR languages |
| `--python CMD`, `--no-venv` | choose the interpreter / use Python directly, without `.venv` |
| `--dev` | also install what is needed to run the tests (reportlab, playwright + Chromium) |
| `--calibre`, `--install-ollama` | install Calibre / accept Ollama's official installer (Linux) |
| `--test` | do a short translation with each model to see that they load (takes a few minutes) |

If you prefer to install by hand: `pip install -r requirements.txt` (Python 3.10+), `ollama pull translategemma:12b` and,
for scanned books, `brew install tesseract tesseract-lang` (Linux: `apt install tesseract-ocr tesseract-ocr-deu ...`).
Ollama needs to be running (app open or `ollama serve`). Check the OCR languages with `tesseract --list-langs`:
for German you need `deu`; for books printed in gothic type (Fraktur), `frk` or `deu_latf` too. If
any is missing, the program tells you which and how to install it.

## Usage

```bash
python pharos.py book.pdf                     # guided flow, with questions
python pharos.py book.epub --target en
python pharos.py book.epub --model gemma3:12b --glossary my_glossary.tsv
python pharos.py book/                         # resumes an interrupted project (or run the same command)
python pharos.py book.pdf --yes               # accept the suggestions, no questions (batch)
python pharos.py scan.pdf --source de         # scan: OCR in German (tests gothic type on its own) + review
python pharos.py page_photos/ --ocr-lang frk   # a folder of images, OCR in Fraktur
python pharos.py book/ --review-ocr           # reopens the OCR review of an already-extracted project
```

| Option | Effect |
|---|---|
| `--source` / `--target` | languages (`de`, `es`, `en`, `pt`...). Without them the program detects and asks |
| `--model`, `--analysis-model` | translation model; model used only to suggest the profile |
| `--ollama URL` | Ollama on another machine (e.g.: the Pi 5) |
| `--glossary FILE.tsv` | initial glossary |
| `--genre` | `occult`, `fiction`, `philosophy`, `religious`, `academic`, `other` |
| `--formats` | `epub,docx,md,html` |
| `--preview-pages N` | preview size (default 3) |
| `--no-preview`, `--no-context` | skip the preview; do not send previous paragraphs as context |
| `--extract-only` | only extract the text (includes OCR and review) and stop; no translation, no Ollama needed |
| `--export-only` | re-export what is already translated, without calling the model |
| `--output DIR` | where to create the project folder (default: current folder) |
| `--ocr-lang CODE` | Tesseract language/model: `deu`, `frk`, `deu_latf`, `deu+eng`... Without it, derived from `--source` or detected |
| `--review flagged\|all\|none` | OCR review: only the suspect pages, all or none (default: asks; with `--yes`: none) |
| `--review-ocr` | reopens the OCR review of an already-extracted project; only what changed is re-translated |
| `--ocr-force` | redo the OCR even if there is a saved result, including in an already-extracted project (the old review edits go to `ocr_review.old.json`) |
| `--double-pages auto\|yes\|no` | images with two pages side by side: split them or not (default: auto, with confirmation) |
| `--use-embedded-layer` | scan that already carries embedded text: use that text instead of redoing the OCR |
| `--ocr-images` | text PDF with a few image-only pages: read those pages with OCR |
| `--ocr-dpi N`, `--ocr-processes N` | read resolution (default 300) and pages in parallel (default: cores − 1, max. 8) |
| `--ocr-model MODEL` | Ollama model that **suggests** OCR fixes in the review screen (default: an installed general model, like `gemma3`; the translation one is not ideal) |
| `--ai-suggestions flagged\|all` | already ask the model for fix suggestions in the background when the review opens (without it, use the on-screen button) |
| `--no-browser` | do not open the browser in the review; just show the address (useful over SSH) |

## The flow

1. **File.** Identifies the format by its content. For a PDF, it classifies each page (text, scan or text
   embedded by another OCR tool), proposes the extraction method and asks for confirmation. A scan or a folder of
   images goes to OCR (next section); a mixed PDF reads the text directly where there is any and uses OCR on the rest.
2. **Language.** Detects the language by sampling paragraphs from several parts of the book (Latin included) and asks
   for confirmation.
3. **Configuration.** Title, author, target language, glossary and output formats. That is everything the program
   asks before the preview.
4. **Profile and preview.** The program proposes a profile (the AI analyses snippets of the book, if you want), translates
   ~3 pages and shows them **side by side** in `preview/vN/preview.html`. Menu:

   | Key | Action |
   |---|---|
   | `a` | approve and translate the whole book |
   | `t` | adjust the tone/register |
   | `r` | add a fidelity rule |
   | `g` | add a term to the glossary |
   | `e` | fix a paragraph: becomes an example for the model **and** applies to the final version |
   | `c` | compare with the translation without a profile (shows what the profile changes) |
   | `o` | try another snippet of the book |
   | `m` | change the model |
   | `s` | quit (the project is saved) |

   Each preview version keeps its profile and glossary in `preview/vN/`. The translations of the approved preview are
   reused in the book's translation.
5. **Translation.** Runs on its own, paragraph by paragraph, with the previous paragraphs as context. `Ctrl+C`
   interrupts; the same command resumes where it stopped (every paragraph is written immediately).
6. **Export.** EPUB, DOCX, Markdown and a bilingual HTML edition, plus `review/report.md`.

## Scanned books (OCR)

These go to OCR: a scanned PDF, a mixed PDF, a folder of images (`.jpg`, `.png`, `.tif`...; the order comes from the
file name, `page_2` before `page_10`) and a multi-page TIFF.

1. **Language and model.** With `--source de` the program uses `deu`. Without it, it does a sample OCR on two pages
   (or reads the selectable text, in a mixed PDF) and asks for confirmation. **For German, it tests the normal model against the
   gothic-type ones** (`frk`, `deu_latf`) on sample pages and suggests the one it read with more confidence; if the book
   looks gothic and you do not have the model, it tells you how to install it.
2. **Double pages.** If most images are wider than tall, it asks whether it should split left and
   right (the spine shadow is discarded).
3. **Preparation and reading.** For each page: evens the lighting (shadows, a photo with uneven light), straightens the
   skew (up to ±5°), rotates pages that are sideways or upside down (when the reading comes out bad and the `osd` model
   is installed) and runs Tesseract, several pages in parallel. Each page is saved to `ocr/` as soon as it
   finishes: `Ctrl+C` and the same command resume from where they stopped.
4. **Structure.** From the recognised words and positions, the program removes headers and page
   numbers, separates footnotes, recognises titles (by size and centring), verse/quotations (short
   or indented lines), paragraphs (indent, spacing, end of line), joins paragraphs that cross the page
   and undoes hyphenation.
5. **Quality.** Each page gets a score (Tesseract confidence, doubtful words, nonsense-looking
   text). Below 85, or with many doubtful words, it is **flagged**. If more than half
   come out bad, the program warns you (wrong language? gothic type? weak scan?) and offers to redo the OCR with another
   model on the spot.
6. **Review.** The program asks whether you want to review only the flagged ones (recommended), all or none, and
   opens a screen in the browser (local server, this machine only, token-protected):
   - left: the list of pages, with a filter (flagged / not reviewed / all);
   - centre: the page image, with **boxes on the doubtful words** (click a box and the cursor goes to the
     word in the text) and zoom;
   - right: the text in editable blocks. You can change the block kind (paragraph, title and level, verse,
     note), **merge** with the previous one, **split** at the cursor, delete or add;
   - **suggestions** of corrections from the book itself (a rare word similar to a frequent word in the same
     book: `lighthonse` → `lighthouse`), applicable on the page or in the whole book;
   - **AI suggestions** (optional, see below): a language model points out OCR errors; you accept or
     reject each one;
   - **find and replace in the whole book**, with a count before applying;
   - `Ctrl+Enter` saves and moves to the next; `Alt+←/→` navigates; `Finish review` closes the step.

   Everything is written on every action to `review/ocr_review.json`. `Ctrl+C` in the terminal pauses; when you run again, the
   review reopens from where it was. With `--review-ocr` you go back to it even after extracting the book
   (for example, when you notice an OCR error in the translation preview); the translation cache is by content, so only the
   changed passages are re-translated.
   **AI suggestions.** At the top of the screen, "AI suggestions" asks an Ollama model to point out OCR errors
   on the current page, on the flagged pages or on all of them (the pass runs in the background, shows progress,
   has a **Stop** button and the suggestions appear while you review; `--ai-suggestions` already starts it when the
   review opens). Each suggestion appears as `current fragment → proposed fragment`, with the context and the confidence the
   OCR had for the word; **accept** applies it to the text box (you still save the page), **reject** hides it
   for good. The rule is: **the model suggests, you decide**. Nothing changes in the book without a click of yours, and when
   the review finishes the screen warns how many pending suggestions will not be applied. For a book with
   occult or literary language this matters, so the program also blocks, **before** showing them:
   - changes to words the OCR read with high confidence (90% or more);
   - any change that alters **numbers**, punctuation, or that adds/removes words (it only deletes a symbol
     the OCR invented, such as `|` or `~`);
   - the "modernisation" of old spelling (`Thür` → `Tür`, `seyn` → `sein`, `Muth` → `Mut`): the author's spelling is not an error;
   - changes to a word very different from the one read, and answers that look like a translation or a rewrite of the block;
   - and only the blocks that have some low-confidence word go to the model (the rest do not need it).

   The book's text is sent to the model as data to be fixed, never as instructions, and the result only counts after
   those validations and your click. Even so the model does not know the book and can be wrong: check against the image.
   The model is chosen by `--ocr-model`; without it, it uses an installed general model (`gemma3`, `qwen`, `llama3`,
   `mistral`, the largest that fits in 14B) because TranslateGemma tends to translate instead of fix (if it is the
   only one installed, the program uses it and warns). The suggestions live in `review/ocr_ai_suggestions.json` and the model is
   unloaded from memory when the review closes. On a Pi 5 this is slow: run the pass on the Mac (`--ollama`).
7. **Final text.** The assembled book (with your corrections) becomes `extracted/book.md` and `book.json`, and the
   translation proceeds as in the other formats. `review/ocr_quality.md` lists the flagged, reviewed and
   edited pages.

**Scan tips:** 300 dpi, black and white or grayscale, straight pages. A phone photo works, but with
even light and no fingers on the page. The program corrects a slight skew and shadows, not folds or pages
curved near the spine.

## Translation profile

`profile.json` keeps: genre, work description, **register/tone**, **fidelity rules** (do not omit, do not
add, do not modernise...) and **examples** you corrected. The texts that go to the model are in
English (models follow them better). For TranslateGemma, the prompt follows the model's official
format, with the extra instructions between the role sentence and the request. For chat models
(Gemma 3, Qwen, Llama), it uses a system message + examples.

## Glossary

`glossary.tsv`, one line per term, TAB-separated: `source term`, `required translation`, `note`.

```
Sephiroth	Sefirot	Hebrew name, usual transliteration in Portuguese
```

Only the terms present in each paragraph go into the prompt (word-ending inflections are accepted). After
the translation, the program checks that each term was applied; if not, it redoes the paragraph.

## Validation and report

Each translated passage goes through: empty answer, still in the source language, identical to the source, very
different length (omission/addition), extra line breaks, line count in verse and glossary
application. If it fails, it redoes it up to 3 times telling the model what was wrong. Whatever exhausts the attempts is
kept (the best version) and listed in `review/report.md` for a human review.

## Project folder

```
book-name/
  original/        the submitted file
  extracted/       book.md (text in the original language) and book.json (internal format)
  ocr/             (scans) OCR result of each page: words, positions and confidence
  pages/           (scans) prepared image of each page, used in the review screen
  preview/vN/      each preview round: preview.html, profile.json, glossary.tsv
  translated/      cache.jsonl (one line per paragraph) and corrections.json (your hand corrections)
  review/          report.md, pages_without_text.txt, ocr_quality.md, ocr_review.json (your OCR corrections)
                   and ocr_ai_suggestions.json (the AI suggestions, with what you accepted and rejected)
  output/          EPUB, DOCX, Markdown and the bilingual edition
  config.json  state.json  profile.json  glossary.tsv
```

`config.json` has fine-tuning: `temperature`, `num_ctx`, `context_blocks`, `max_attempts`,
`max_block_chars`, `prompt_style` (`auto`, `translategemma` or `chat`) and, for OCR, `ocr_psm`,
`ocr_dpi`, `ocr_word_threshold` (confidence below which a word is "doubtful", default 70),
`ocr_page_threshold` (score below which the page is flagged, default 85) and `ocr_model` (the model for the
AI suggestions).

## Limitations of this version

- **OCR:** only tested with Tesseract in **English**. The model choice (Fraktur included), the language
  detection and the install warning are tested with a simulated Tesseract; in practice, the quality in German and
  in gothic type depends on the installed models and the state of the scan. Review the first pages before
  trusting the rest.
- **`setup.sh`:** tested on Linux against simulated tools and servers (fake macOS, Homebrew, Ollama and GitHub;
  see `tests/test_setup.py`). It has not been run on a real Mac, on the macOS bash 3.2, with a real
  `ollama pull` (≈ 16 GB), against PyPI or with a real download of the Tesseract data. If a step fails on your
  machine, `./setup.sh --check` shows the state and the script can be run again: it resumes where it stopped.
- **AI suggestions:** only tested with a simulated Ollama (a "model" that fixes exactly the OCR's
  errors, plus traps the validations must block). How much the real `gemma3:12b` gets right in German or in Fraktur
  has not been measured yet: start with one page and see whether the suggestions help. They only catch errors in
  low-confidence words; a "confident" OCR error (a real wrong word, read at 90%+) only shows up in your reading.
- OCR does not read handwriting, nor two-column books or complex tables in the right order; footnote
  markers (¹) are sometimes read as `!` or `1`. Books with many illustrations produce "no text"
  pages that appear as flagged; mark them as reviewed.
- A text PDF with a few image-only pages (cover, plates): those pages are left out and are listed in
  `review/pages_without_text.txt` (use `--ocr-images` to read them).
- In a mixed PDF, double pages are not split and repeated headers are only removed if they appear on
  3 pages or more of each part.
- Two-column PDFs and complex tables may come out with the reading order mixed up.
- Images and the cover are not carried into the translated EPUB/DOCX.
- Files with DRM are not read.
- Language detection covers German, English, Spanish, French, Italian, Portuguese, Dutch, Latin and
  languages in other alphabets (Russian, Greek, Hebrew, Arabic, Korean, Japanese, Chinese). For other
  languages, type the code at the confirmation.
- TranslateGemma is a translation model and may follow style instructions worse than a chat model.
  Use the preview's `c` option to see whether the profile changes the result; if it does not, try `--model gemma3:12b`.

## Tests

```bash
./setup.sh --dev                                 # (or: pip install reportlab pillow numpy pypdfium2 playwright)
python tests/test_flow.py                        # translation, preview, glossary, EPUB/PDF/TXT
python tests/test_ocr.py                         # OCR and review (needs Tesseract with 'eng')
python tests/test_ai.py                          # AI correction suggestions (parts A–D run without Tesseract)
python tests/test_setup.py                       # setup.sh, with fake tools and servers (1–3 min)
```

`test_flow.py` runs the whole flow against `tests/fake_ollama.py`, a server that imitates the Ollama API
(with injected failures), without needing a model.

`test_ocr.py` generates sample scanned books (skewed, with noise and shadow) and checks, with the real Tesseract:
skew and double pages, a PDF scan, a folder of images (one upside down), a mixed PDF,
embedded text, similarity of the recognised text to the original (≥ 98.5%), a bad page flagged, the review
screen through the API and in the browser (Playwright, if installed), pause and resume, `--review-ocr` and the
scan → OCR → translation → export path. The language/Fraktur logic and the PDF route use a simulated Tesseract.

`test_ai.py` covers the AI suggestions: the validations (high confidence, numbers, old spelling, additions,
rewrite, line breaks), the cache and persistence, the batch pass (stop, Ollama failures, missing model),
the guarantee that **generating suggestions does not change the text**, the token-protected API, and, with the real Tesseract,
a degraded scan from start to finish (accepting a suggestion takes it into the book; rejecting does not; `--ai-suggestions`) and the screen
in the browser (accept, reject, accept all, Stop, per-page badge, finish warning, dark mode and mobile).

`test_setup.py` exercises `setup.sh` with no internet and without downloading anything: it builds a throwaway "world" with fake `brew`, `apt-get`,
`sudo`, `uname` (macOS), `tesseract` and `ollama` on the PATH, a fake Ollama with `/api/pull` (progress, mid-way
drops, errors, nonexistent model) and a local server standing in for Tesseract's GitHub. It checks: `--check` with no side
effects, a full install and an idempotent second run, Python/Tesseract/Ollama missing, local Ollama down and
remote, `.setup.conf` and `./pharos.sh`, low disk space, running with no terminal (never installs or downloads on its own),
y/n questions in a real terminal (pty), Ctrl+C, and a scan for bash 4+ features (macOS ships bash 3.2).

## Contributing

Contributions are welcome — OCR fixes, new languages, extractors, prompt improvements or just usage reports.

- **Found a bug or have an idea?** Open an [issue](https://github.com/0xA1n50ph/pharos/issues) describing the
  book format, the language, the model used and what happened (preferably with `review/report.md` or the error message).
- **Sending a PR?** Run the tests first (`./setup.sh --dev` and the relevant `python tests/test_*.py`) and keep the
  project's convention: **code, interface and comments in English**; only the text sent to the model is written in English too.
- **Architecture:** the starting point is `CLAUDE.md`, which summarises the pipeline (extraction → language → profile/preview →
  translation → export), the internal `Book` model and the OCR subsystem.

The principle that guides the project: **the AI suggests, you decide.** Nothing enters the final text — not even an OCR fix —
without passing the automatic validations and, where applicable, your approval.

## License

Distributed under the [MIT](LICENSE) license. Use, modify and share freely, keeping the copyright notice.
