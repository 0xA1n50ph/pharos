#!/usr/bin/env bash
# =====================================================================================================
#  setup.sh — checks and prepares the Pharos environment (macOS and Linux)
#
#  Does this, in order (each step only touches what is missing, so you can run it as often as you like):
#    1. System       OS, memory, disk space, package manager
#    2. Python       3.10+ (installs via brew/apt/dnf/pacman, if you let it) and the .venv virtual environment
#    3. Libraries    requirements.txt inside the .venv, checked by import
#    4. OCR          Tesseract + languages (eng osd deu spa por) + gothic type (frk, deu_latf)
#    5. Ollama       installed, up (local or on another machine) and with the gemma models downloaded
#    6. Optional     Calibre (MOBI/AZW3/...), test tools
#    7. Check        uses the program itself to confirm everything sees each other, and creates the ./pharos.sh shortcut
#
#  Nothing that needs an administrator password (sudo) runs without your confirmation. With no interactive
#  terminal and no -y, the script only checks and shows the command to run by hand.
#
#  Usage:  ./setup.sh [options]      (see ./setup.sh --help)
#  Compatible with the bash 3.2 that ships with macOS.
# =====================================================================================================
set -u -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"
CONF="$ROOT/.setup.conf"
PY_MIN_MAJOR=3
PY_MIN_MINOR=10

TRANS_MODEL="translategemma:12b"
GENERAL_MODEL="gemma3:12b"
BASE_LANGS="eng osd deu spa por"
GOTHIC_LANGS="frk deu_latf"
EXTRA_LANGS=""
TESSDATA_URL_BASE="${TESSDATA_URL_BASE:-https://github.com/tesseract-ocr}"
GB_PER_MODEL=9

CHECK=0; YES=0; NO_MODELS=0; NO_GENERAL_MODEL=0; NO_OCR=0; NO_VENV=0; DEV=0; CALIBRE=0
TEST=0; INSTALL_OLLAMA=0; PYTHON_ARG=""; TRANS_MODEL_SET=0; GENERAL_MODEL_SET=0
OLLAMA_URL_ARG=""

FAILURES=0; WARNINGS=0; FAIL_SUMMARY=""; WARN_SUMMARY=""
PM=""; OS_="$(uname -s)"; ARCH="$(uname -m)"
PY_SHIM=0; PY=""; VPY=""; PYANY=""; TESS=""; TESSDATA_DIR=""; INSTALLED_MODELS=""; OLLAMA_URL=""; OLLAMA_LOCAL=0
LOG="$(mktemp "${TMPDIR:-/tmp}/pharos-setup.XXXXXX")"
trap 'rm -f "$LOG"' EXIT
trap 'printf "\n  Interrupted. Run ./setup.sh again to continue where it stopped (model downloads resume).\n"; exit 130' INT TERM

# ---------------------------------------------------------------------------------------- output
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_OK=$'\033[32m'; C_AV=$'\033[33m'; C_ER=$'\033[31m'; C_B=$'\033[1m'; C_D=$'\033[2m'; C_0=$'\033[0m'
else
  C_OK=""; C_AV=""; C_ER=""; C_B=""; C_D=""; C_0=""
fi
step()   { printf '\n%s%s%s\n' "$C_B" "$*" "$C_0"; }
ok()     { printf '  %s✓%s %s\n' "$C_OK" "$C_0" "$*"; }
info()   { printf '    %s\n' "$*"; }
warn()   { printf '  %s!%s %s\n' "$C_AV" "$C_0" "$*"; WARNINGS=$((WARNINGS + 1)); WARN_SUMMARY="$WARN_SUMMARY  ! $*"$'\n'; }
note()   { printf '  %s!%s %s\n' "$C_AV" "$C_0" "$*"; }   # like warn, but not added to the summary (just a heads-up that I'll try to fix it)
fail()   { printf '  %s✗%s %s\n' "$C_ER" "$C_0" "$*"; FAILURES=$((FAILURES + 1)); FAIL_SUMMARY="$FAIL_SUMMARY  ✗ $*"$'\n'; }
run_cmd() { printf '    %s$ %s%s\n' "$C_D" "$*" "$C_0"; "$@"; }

usage() {
  cat <<'END'
Usage: ./setup.sh [options]

Checks and prepares the environment: Python + libraries (.venv), Tesseract and languages, Ollama and gemma models.

  --check                only checks and shows what is missing; installs and changes nothing
  -y, --yes              answers "yes" to everything (does not ask)
  --ollama URL           Ollama on another machine (e.g.: http://pi.local:11434). Default: $OLLAMA_HOST or localhost.
                         Saved in .setup.conf and the ./pharos.sh shortcut starts using it
  --model NAME           translation model (default: translategemma:12b)
  --ocr-model NAME       general model for the OCR correction suggestions (default: gemma3:12b)
  --no-ocr-model         do not download the general model for the OCR suggestions
  --no-models            do not download any model (only checks which exist)
  --no-ocr               do not check or install Tesseract
  --languages LIST       extra OCR languages, comma-separated (e.g.: fra,ita,lat). Already included: eng osd deu spa por
  --python CMD           Python interpreter to use (default: the first 3.10+ found)
  --no-venv              use the chosen Python directly, without creating the .venv (not recommended)
  --dev                  also install what the tests need (reportlab, playwright + chromium)
  --calibre              also install Calibre (reads MOBI/AZW3/FB2/DOCX)
  --install-ollama       (Linux) accept running Ollama's official installer (https://ollama.com/install.sh)
  --test                 do a short translation with each model to see that they load (takes a few minutes)
  -h, --help             show this help

Examples:
  ./setup.sh                                  # prepares everything, asking before installing
  ./setup.sh --check                          # only diagnoses
  ./setup.sh -y --ollama http://pi.local:11434 --no-ocr-model
Exit code: 0 = environment ready; 1 = something required is missing.
END
}

# ---------------------------------------------------------------------------------------- arguments
needs_value() { [ $# -ge 2 ] && [ -n "$2" ] || { echo "The option $1 needs a value. See: ./setup.sh --help" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1 ;;
    -y|--yes) YES=1 ;;
    --ollama) needs_value "$@"; OLLAMA_URL_ARG="$2"; shift ;;
    --model) needs_value "$@"; TRANS_MODEL="$2"; TRANS_MODEL_SET=1; shift ;;
    --ocr-model) needs_value "$@"; GENERAL_MODEL="$2"; GENERAL_MODEL_SET=1; shift ;;
    --no-ocr-model) NO_GENERAL_MODEL=1 ;;
    --no-models) NO_MODELS=1 ;;
    --no-ocr) NO_OCR=1 ;;
    --languages) needs_value "$@"; EXTRA_LANGS="$(printf '%s' "$2" | tr ',' ' ')"
               case "$EXTRA_LANGS" in *[!a-zA-Z_\ ]*) echo "--languages: use Tesseract codes separated by commas (e.g.: fra,ita,lat)" >&2; exit 2 ;; esac
               shift ;;
    --python) needs_value "$@"; PYTHON_ARG="$2"; shift ;;
    --no-venv) NO_VENV=1 ;;
    --dev) DEV=1 ;;
    --calibre) CALIBRE=1 ;;
    --install-ollama) INSTALL_OLLAMA=1 ;;
    --test) TEST=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; echo "See: ./setup.sh --help" >&2; exit 2 ;;
  esac
  shift
done


# ---------------------------------------------------------------------------------------- Python snippets
# (kept in variables so they are not inside $(...): the macOS bash 3.2 trips over a heredoc there)
IFS= read -r -d '' PY_IMPORTS <<'PY' || true
import importlib, importlib.metadata as md
for module, dist in (("PIL", "pillow"), ("numpy", "numpy"), ("pypdfium2", "pypdfium2"), ("pdfplumber", "pdfplumber"),
                     ("bs4", "beautifulsoup4"), ("docx", "python-docx")):
    try:
        importlib.import_module(module)
        try:
            v = md.version(dist)
        except Exception:
            v = "?"
        print("ok|%s|%s" % (dist, v))
    except Exception as e:
        print("missing|%s|%s" % (dist, type(e).__name__))
PY

IFS= read -r -d '' PY_PULL <<'PY' || true
import json, sys, urllib.request
url, model = sys.argv[1], sys.argv[2]
req = urllib.request.Request(url + "/api/pull", data=json.dumps({"model": model, "name": model, "stream": True}).encode(),
                             headers={"Content-Type": "application/json"})
tty = sys.stdout.isatty()
last, nxt, success = "", {}, False
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        for line in r:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if "error" in d:
                print("\n    Ollama error: %s" % d["error"])
                sys.exit(1)
            st = d.get("status", "")
            tot, done = d.get("total"), d.get("completed")
            if tot and done is not None:
                pct = 100.0 * done / tot
                msg = "    %-26s %5.1f%%  %.1f/%.1f GB" % (st[:26], pct, done / 1e9, tot / 1e9)
                if not tty:
                    if pct < nxt.get(st, 0):
                        continue
                    nxt[st] = (int(pct) // 10 + 1) * 10
            else:
                msg = "    " + st[:70]
            if msg != last:
                if tty:
                    sys.stdout.write("\r" + msg.ljust(80))
                else:
                    sys.stdout.write(msg + "\n")
                sys.stdout.flush()
                last = msg
            if st == "success":
                success = True
except KeyboardInterrupt:
    print("\n    interrupted")
    sys.exit(130)
except Exception as e:
    print("\n    connection failure: %s" % e)
    sys.exit(1)
if tty:
    print()
sys.exit(0 if success else 1)
PY

IFS= read -r -d '' PY_TEST <<'PY' || true
import json, sys, time, urllib.request
url, model = sys.argv[1], sys.argv[2]
def post(path, body, timeout=900):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)
t0 = time.time()
try:
    r = post("/api/chat", {"model": model, "stream": False, "options": {"num_predict": 60, "temperature": 0},
                           "messages": [{"role": "user", "content": "Translate from German to Portuguese. Reply with the translation only: Der Leuchtturm stand allein am Rand der Klippe."}]})
    txt = (r.get("message", {}).get("content") or "").strip().replace("\n", " ")
    if not txt:
        print("    empty response")
        sys.exit(1)
    print("    %.0f s · «%s»" % (time.time() - t0, txt[:90]))
    post("/api/generate", {"model": model, "keep_alive": 0}, 30)
except Exception as e:
    print("    failed: %s" % e)
    sys.exit(1)
PY

IFS= read -r -d '' PY_OCR <<'PY' || true
import sys
sys.path.insert(0, sys.argv[1])
try:
    from pharos.ocr import tesseract
    exe = tesseract.locate()
    inst = sorted(tesseract.installed_languages(exe))
    print("ok|" + ", ".join(i for i in inst if i != "osd"))
except Exception as e:
    print("error|" + (str(e).splitlines() or ["?"])[0])
PY

# ---------------------------------------------------------------------------------------- saved configuration
conf_value() { [ -f "$CONF" ] || return 0; ( . "$CONF" 2>/dev/null; eval "printf '%s' \"\${$1:-}\"" ); }
SAVED_OLLAMA="$(conf_value OLLAMA_URL)"; SAVED_MODEL="$(conf_value MODEL)"; SAVED_OCR_MODEL="$(conf_value OCR_MODEL)"
[ -z "$OLLAMA_URL_ARG" ] && [ -n "$SAVED_OLLAMA" ] && OLLAMA_URL_ARG="$SAVED_OLLAMA"
if [ "$TRANS_MODEL_SET" != 1 ] && [ -n "$SAVED_MODEL" ]; then TRANS_MODEL="$SAVED_MODEL"; TRANS_MODEL_SET=1; fi
if [ "$GENERAL_MODEL_SET" != 1 ] && [ -n "$SAVED_OCR_MODEL" ]; then GENERAL_MODEL="$SAVED_OCR_MODEL"; GENERAL_MODEL_SET=1; fi

# ---------------------------------------------------------------------------------------- utilities
has() { command -v "$1" >/dev/null 2>&1; }

confirm() {  # confirm "question" [y|n = default answer]
  local question="$1" default="${2:-y}" r
  if [ "$YES" = 1 ]; then [ "$default" = y ]; return; fi   # -y accepts routine things; risky ones (default "n") stay out
  [ -t 0 ] || return 1                       # no terminal and no -y: never installs on its own
  if [ "$default" = y ]; then printf '  ? %s [Y/n] ' "$question"; else printf '  ? %s [y/N] ' "$question"; fi
  read -r r || return 1
  case "$r" in
    "") [ "$default" = y ] ;;
    [yY]*) return 0 ;;
    *) return 1 ;;
  esac
}

as_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"
  elif has sudo; then sudo "$@"
  else echo "    (needs administrator and sudo does not exist)" >&2; return 1
  fi
}

# Asks and installs with the package manager. $1 = description; the rest = command (ready to run).
install_with() {
  local description="$1"; shift
  if [ "$CHECK" = 1 ]; then return 1; fi
  confirm "Install $description now?" y || { info "To install by hand:  $*"; return 1; }
  run_cmd "$@"
}

apt_install() {  # apt_install "description" package...   (with 'apt-get update' and a retry if the index is stale)
  local d="$1"; shift
  [ "$CHECK" = 1 ] && return 1
  confirm "Install $d now?" y || { info "To install by hand:  sudo apt-get update && sudo apt-get install -y $*"; return 1; }
  run_cmd as_root apt-get install -y "$@" || { run_cmd as_root apt-get update -qq && run_cmd as_root apt-get install -y "$@"; }
}

free_gb() {  # free space (GB) on the volume of directory $1 (or the first parent that exists)
  local d="$1"
  while [ ! -d "$d" ] && [ "$d" != "/" ]; do d="$(dirname "$d")"; done
  df -Pk "$d" 2>/dev/null | awk 'NR==2 {printf "%d", $4/1048576}'
}

ram_gb() {
  if [ "$OS_" = Darwin ]; then echo $(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1073741824 ))
  else awk '/MemTotal/ {printf "%d", $2/1048576}' /proc/meminfo 2>/dev/null || echo 0
  fi
}

major_version() { local v="${1%%.*}"; case "$v" in ''|*[!0-9]*) echo 0 ;; *) echo "$v" ;; esac; }

# ---------------------------------------------------------------------------------------- 1. system
detect_pm() {
  if has brew; then PM=brew
  elif [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; PM=brew
  elif [ -x /usr/local/bin/brew ]; then eval "$(/usr/local/bin/brew shellenv)"; PM=brew
  elif has apt-get; then PM=apt
  elif has dnf; then PM=dnf
  elif has pacman; then PM=pacman
  elif has zypper; then PM=zypper
  else PM=""
  fi
}

step_system() {
  step "1. System"
  case "$OS_" in
    Darwin) ok "macOS $(sw_vers -productVersion 2>/dev/null || true) ($ARCH)" ;;
    Linux)  ok "Linux ($ARCH)" ;;
    *)      warn "System '$OS_' not tested; the script carries on as if it were Linux." ;;
  esac
  detect_pm
  if [ -n "$PM" ]; then ok "Package manager: $PM"
  elif [ "$OS_" = Darwin ]; then
    warn "Homebrew not found. Install it from https://brew.sh and run this script again (it makes everything easier on macOS)."
  else
    warn "No known package manager (apt, dnf, pacman, zypper): install the dependencies by hand."
  fi
  has curl && ok "curl present" || fail "curl not found (needed to download models and OCR data)"
  local ram; ram="$(ram_gb)"
  if [ "$ram" -gt 0 ]; then
    if [ "$ram" -lt 12 ]; then warn "${ram} GB of RAM: the 12B models ask for about 10 GB. Consider a smaller model (e.g.: --model gemma3:4b) or Ollama on another machine (--ollama)."
    else ok "${ram} GB of RAM (12B models run one at a time; close heavy apps during the translation)"; fi
  fi
}

# ---------------------------------------------------------------------------------------- 2. Python
python_ok() { "$1" -c "import sys; sys.exit(0 if sys.version_info >= ($PY_MIN_MAJOR, $PY_MIN_MINOR) else 1)" >/dev/null 2>&1; }

# Like python_ok, but without running the macOS /usr/bin "python3" when the Command Line Tools are missing:
# in that case it is just a shim that opens a window asking to install them.
python_usable() {
  if [ "$OS_" = Darwin ] && [ "$(command -v "$1" 2>/dev/null)" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then
    PY_SHIM=1
    return 1
  fi
  python_ok "$1"
}

find_python() {
  local c
  if [ -n "$PYTHON_ARG" ]; then
    if { has "$PYTHON_ARG" || [ -x "$PYTHON_ARG" ]; } && python_usable "$PYTHON_ARG"; then PY="$(command -v "$PYTHON_ARG" 2>/dev/null || echo "$PYTHON_ARG")"; return 0; fi
    return 1
  fi
  for c in python3.13 python3.12 python3.11 python3.10 python3 python /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if { has "$c" || [ -x "$c" ]; } && python_usable "$c"; then PY="$(command -v "$c" 2>/dev/null || echo "$c")"; return 0; fi
  done
  return 1
}

install_python() {
  case "$PM" in
    brew)   install_with "Python 3.12 (brew)" brew install python@3.12 ;;
    apt)    apt_install "Python 3 with venv and pip (apt)" python3 python3-venv python3-pip ;;
    dnf)    install_with "Python 3 (dnf)" as_root dnf install -y python3 python3-pip ;;
    pacman) install_with "Python 3 (pacman)" as_root pacman -S --noconfirm python python-pip ;;
    zypper) install_with "Python 3 (zypper)" as_root zypper install -y python3 python3-pip ;;
    *)      return 1 ;;
  esac
}

step_python() {
  step "2. Python and virtual environment"
  if ! find_python; then
    if [ -n "$PYTHON_ARG" ]; then
      [ "$PY_SHIM" = 1 ] && info "That macOS python3 is just a shim (asks for the Command Line Tools); the script does not run it."
      fail "'$PYTHON_ARG' does not exist or is older than Python $PY_MIN_MAJOR.$PY_MIN_MINOR"; return 1
    fi
    note "Python $PY_MIN_MAJOR.$PY_MIN_MINOR+ not found ($(command -v python3 >/dev/null 2>&1 && python3 --version 2>&1 || echo 'no python3'))."
    install_python && { hash -r; find_python; } || true
  fi
  if [ -z "$PY" ]; then
    [ "$PY_SHIM" = 1 ] && info "The macOS /usr/bin python3 is just a shim (asks for the Command Line Tools); the script does not run it."
    fail "Python $PY_MIN_MAJOR.$PY_MIN_MINOR+ missing. macOS: brew install python@3.12 · Debian/Ubuntu: sudo apt install python3 python3-venv"
    return 1
  fi
  ok "Python $("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])') at $PY"
  PYANY="$PY"

  if [ "$NO_VENV" = 1 ]; then
    VPY="$PY"
    warn "No virtual environment (--no-venv): the libraries go into the chosen Python."
    return 0
  fi

  if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c 'pass' >/dev/null 2>&1 && python_ok "$VENV/bin/python"; then
    ok "The .venv virtual environment already exists"
    VPY="$VENV/bin/python"
    return 0
  fi
  if [ -e "$VENV" ] && [ "$CHECK" != 1 ]; then
    note ".venv exists, but it is broken or from an old Python: I'll recreate it."
    rm -rf "$VENV"
  fi
  if [ "$CHECK" = 1 ]; then
    fail "The .venv virtual environment does not exist or is broken (run ./setup.sh)"
    return 1
  fi
  printf '    %s$ %s -m venv .venv%s\n' "$C_D" "$PY" "$C_0"
  if ! "$PY" -m venv "$VENV" >"$LOG" 2>&1; then
    if [ "$PM" = apt ] && grep -qi "ensurepip\|python3-venv" "$LOG"; then
      apt_install "the Python venv module (apt)" python3-venv python3-pip && "$PY" -m venv "$VENV" >"$LOG" 2>&1
    fi
  fi
  if [ -x "$VENV/bin/python" ]; then
    ok "Virtual environment created in .venv"
    VPY="$VENV/bin/python"
  else
    tail -5 "$LOG" | sed 's/^/    /'
    fail "I could not create the .venv virtual environment"
    return 1
  fi
}

# ---------------------------------------------------------------------------------------- 3. libraries
step_libs() {
  step "3. Python libraries"
  [ -n "$VPY" ] || { info "Skipped: there is no Python environment yet (see step 2)."; return 0; }
  if [ "$CHECK" != 1 ]; then
    "$VPY" -m pip --version >/dev/null 2>&1 || "$VPY" -m ensurepip --upgrade >/dev/null 2>&1
    "$VPY" -m pip install --disable-pip-version-check --quiet --upgrade pip >"$LOG" 2>&1 || true
    printf '    installing requirements.txt (may take a minute)...\n'
    if "$VPY" -m pip install --disable-pip-version-check -r "$ROOT/requirements.txt" >"$LOG" 2>&1; then
      ok "requirements.txt installed"
    else
      tail -8 "$LOG" | sed 's/^/    /'
      fail "pip could not install requirements.txt (no internet? a Python version without prebuilt packages?)"
    fi
  fi
  local out line missing=""
  out="$("$VPY" -c "$PY_IMPORTS" 2>/dev/null)"
  while IFS='|' read -r state name extra; do
    [ -n "$state" ] || continue
    if [ "$state" = ok ]; then ok "$name $extra"; else missing="$missing $name"; fi
  done <<<"$out"
  if [ -n "$missing" ]; then
    fail "Libraries that do not import:$missing  →  ${VPY} -m pip install -r requirements.txt"
  fi
  if [ "$DEV" = 1 ] && [ "$CHECK" != 1 ]; then
    printf '    installing test tools (reportlab, playwright)...\n'
    if "$VPY" -m pip install --disable-pip-version-check reportlab playwright >"$LOG" 2>&1; then
      ok "reportlab and playwright installed"
      if confirm "Download Playwright's Chromium (~150 MB) for the review-screen tests?" y; then
        run_cmd "$VPY" -m playwright install chromium >"$LOG" 2>&1 && ok "Playwright Chromium installed" \
          || { tail -3 "$LOG" | sed 's/^/    /'; warn "I could not download Playwright's Chromium (the screen tests will be skipped)."; }
      fi
    else
      tail -5 "$LOG" | sed 's/^/    /'
      warn "I could not install the test tools."
    fi
  fi
}

# ---------------------------------------------------------------------------------------- 4. OCR
find_tesseract() {
  TESS="${TESSERACT_CMD:-}"
  [ -n "$TESS" ] && { has "$TESS" || [ -x "$TESS" ]; } && return 0
  TESS="$(command -v tesseract 2>/dev/null || true)"
  [ -n "$TESS" ] && return 0
  local c
  for c in /opt/homebrew/bin/tesseract /usr/local/bin/tesseract /usr/bin/tesseract; do
    [ -x "$c" ] && { TESS="$c"; return 0; }
  done
  TESS=""; return 1
}

tesseract_langs() {
  "$TESS" --list-langs 2>&1 | grep -v -i -e '^list of available' -e '^error' -e '^warning' | sed 's/[[:space:]]*$//' | grep -v '^$' || true
}

tessdata_dir() {
  local d c
  d="$("$TESS" --list-langs 2>&1 | sed -n 's/^List of available languages in "\(.*\)" (.*/\1/p' | head -1)"
  d="${d%/}"
  if [ -z "$d" ] || [ ! -d "$d" ]; then
    d=""
    for c in "${TESSDATA_PREFIX:-}" "${TESSDATA_PREFIX:-}/tessdata" /opt/homebrew/share/tessdata /usr/local/share/tessdata \
             /usr/share/tesseract-ocr/5/tessdata /usr/share/tesseract-ocr/4.00/tessdata /usr/share/tessdata; do
      [ -n "$c" ] && [ -d "$c" ] && { d="$c"; break; }
    done
  fi
  printf '%s' "$d"
}

download_traineddata() {  # download_traineddata LANGUAGE REPO(best|fast)
  local lang="$1" repo="$2" tmp ref url
  tmp="$(mktemp "${TMPDIR:-/tmp}/pharos-tess.XXXXXX")"
  for ref in main 4.1.0; do
    url="$TESSDATA_URL_BASE/tessdata_$repo/raw/$ref/$lang.traineddata"
    if curl -fsSL --retry 2 --max-time 600 -o "$tmp" "$url" 2>/dev/null && [ "$(wc -c <"$tmp")" -gt 10000 ]; then
      if [ -w "$TESSDATA_DIR" ]; then cp "$tmp" "$TESSDATA_DIR/$lang.traineddata" && chmod 644 "$TESSDATA_DIR/$lang.traineddata"
      else as_root cp "$tmp" "$TESSDATA_DIR/$lang.traineddata" && as_root chmod 644 "$TESSDATA_DIR/$lang.traineddata"
      fi
      local rc=$?
      rm -f "$tmp"
      return $rc
    fi
  done
  rm -f "$tmp"
  return 1
}

install_tesseract_pm() {  # $1 = list of desired languages
  local l packages
  case "$PM" in
    brew) install_with "Tesseract with all languages (brew install tesseract tesseract-lang)" brew install tesseract tesseract-lang ;;
    apt)
      packages="tesseract-ocr"
      for l in $1; do packages="$packages tesseract-ocr-$l"; done
      apt_install "Tesseract and the languages ($1) via apt" $packages ;;
    dnf)    install_with "Tesseract (dnf)" as_root dnf install -y tesseract ;;
    pacman) install_with "Tesseract (pacman)" as_root pacman -S --noconfirm tesseract ;;
    zypper) install_with "Tesseract (zypper)" as_root zypper install -y tesseract-ocr ;;
    *)      return 1 ;;
  esac
}

step_ocr() {
  step "4. OCR (Tesseract)"
  if [ "$NO_OCR" = 1 ]; then info "Skipped (--no-ocr). Scanned books cannot be read."; return 0; fi
  local desired="$BASE_LANGS $EXTRA_LANGS" missing l v
  if ! find_tesseract; then
    note "Tesseract not found."
    install_tesseract_pm "$desired" && { hash -r; find_tesseract; } || true
  fi
  if ! find_tesseract; then
    fail "Tesseract missing (needed for scanned books). macOS: brew install tesseract tesseract-lang · Debian/Ubuntu: sudo apt install tesseract-ocr tesseract-ocr-deu"
    return 1
  fi
  v="$("$TESS" --version 2>&1 | head -1 | sed 's/^[^0-9]*//; s/[^0-9.].*$//')"
  if [ -z "$v" ]; then
    fail "The Tesseract at $TESS does not run (broken install?). macOS: brew reinstall tesseract"
    return 1
  fi
  if [ "$(major_version "$v")" -lt 4 ]; then
    fail "Tesseract $v is too old (needs 4 or newer, with the LSTM engine)"
    return 1
  fi
  ok "Tesseract $v at $TESS"

  local inst; inst="$(tesseract_langs)"
  missing=""
  for l in $desired; do grep -Fxq "$l" <<<"$inst" || missing="$missing $l"; done
  if [ -n "$missing" ] && [ "$CHECK" != 1 ]; then
    note "Missing OCR languages:$missing"
    case "$PM" in
      apt) install_tesseract_pm "$desired" || true ;;
      brew) install_with "the Tesseract languages (brew install tesseract-lang)" brew install tesseract-lang || true ;;
    esac
    inst="$(tesseract_langs)"
  fi
  TESSDATA_DIR="$(tessdata_dir)"
  missing=""
  for l in $desired; do grep -Fxq "$l" <<<"$inst" || missing="$missing $l"; done
  if [ -n "$missing" ] && [ "$CHECK" != 1 ] && [ -n "$TESSDATA_DIR" ]; then
    if confirm "Download straight from the official Tesseract repository the languages:$missing (to $TESSDATA_DIR)?" y; then
      for l in $missing; do
        if download_traineddata "$l" fast; then ok "$l downloaded"; else warn "I could not download $l ($TESSDATA_URL_BASE/tessdata_fast)."; fi
      done
      inst="$(tesseract_langs)"
    fi
  fi
  for l in $desired; do
    if grep -Fxq "$l" <<<"$inst"; then ok "language $l"
    elif [ "$l" = eng ] || [ "$l" = osd ]; then fail "language $l missing (required). Install: tesseract-ocr-$l (apt) or tesseract-lang (brew)"
    else warn "language $l missing: books in '$l' cannot be read by OCR (install tesseract-ocr-$l or use brew install tesseract-lang)"
    fi
  done

  # gothic type (Fraktur): the program tests these models on its own and uses the one that reads best
  local gothic_ok="" gothic_missing=""
  for l in $GOTHIC_LANGS; do grep -Fxq "$l" <<<"$inst" && gothic_ok="$gothic_ok $l" || gothic_missing="$gothic_missing $l"; done
  if [ -n "$gothic_missing" ] && [ "$CHECK" != 1 ] && [ -n "$TESSDATA_DIR" ]; then
    info "Books printed in gothic type (Fraktur) need one of these models:$gothic_missing"
    if confirm "Download the Fraktur models ('best' version, more accurate) to $TESSDATA_DIR?" y; then
      for l in $gothic_missing; do
        if download_traineddata "$l" best; then ok "$l (Fraktur) downloaded"; else warn "I could not download $l ($TESSDATA_URL_BASE/tessdata_best)."; fi
      done
      inst="$(tesseract_langs)"
      gothic_ok=""
      for l in $GOTHIC_LANGS; do grep -Fxq "$l" <<<"$inst" && gothic_ok="$gothic_ok $l"; done
    fi
  fi
  if [ -n "$gothic_ok" ]; then ok "gothic type (Fraktur):$gothic_ok"
  else warn "No Fraktur model (frk, deu_latf): gothic books will come out with many errors. Run ./setup.sh again to download them."
  fi
}

# ---------------------------------------------------------------------------------------- 5. Ollama
normalize_url() {
  local u="$1" rest host
  case "$u" in http://*|https://*) ;; *) u="http://$u" ;; esac
  rest="${u#*://}"; rest="${rest%%/*}"
  host="${rest%%:*}"
  [ "$host" = "0.0.0.0" ] && u="${u/0.0.0.0/localhost}" && rest="${rest/0.0.0.0/localhost}"
  case "$rest" in *:*) ;; *) u="${u%/}:11434" ;; esac
  printf '%s' "${u%/}"
}

is_local() {
  local rest="${1#*://}"; rest="${rest%%/*}"
  case "${rest%%:*}" in localhost|127.*|"[::1]"|::1) return 0 ;; *) return 1 ;; esac
}

ollama_up() { curl -fsS --max-time 6 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; }

list_models() {
  curl -fsS --max-time 10 "$OLLAMA_URL/api/tags" 2>/dev/null | grep -o '"name": *"[^"]*"' | sed 's/^"name": *"//; s/"$//' || true
}

has_model() {
  local m="$1" base="$1"
  case "$m" in *:*) ;; *) base="$m:latest" ;; esac
  grep -Fxq -e "$m" -e "$base" <<<"$INSTALLED_MODELS"
}

find_ollama_cli() {
  has ollama && return 0
  [ -x /Applications/Ollama.app/Contents/Resources/ollama ] && return 0
  return 1
}

start_local_ollama() {
  if [ "$OS_" = Darwin ] && [ -d /Applications/Ollama.app ]; then
    run_cmd open -a Ollama
  elif [ "$PM" = brew ] && brew list --formula ollama >/dev/null 2>&1; then
    run_cmd brew services start ollama
  elif has systemctl && systemctl list-unit-files 2>/dev/null | grep -q '^ollama.service'; then
    run_cmd as_root systemctl start ollama
  elif has ollama; then
    printf '    %s$ ollama serve (in the background; log in .ollama-serve.log)%s\n' "$C_D" "$C_0"
    nohup ollama serve >"$ROOT/.ollama-serve.log" 2>&1 &
  else
    return 1
  fi
}

wait_ollama() {
  local i
  for i in $(seq 1 45); do ollama_up && return 0; sleep 1; done
  return 1
}

install_ollama() {
  if [ "$OS_" = Darwin ]; then
    if [ "$PM" = brew ]; then install_with "Ollama (brew install ollama)" brew install ollama; else return 1; fi
  else
    if [ "$CHECK" = 1 ]; then return 1; fi
    if [ "$INSTALL_OLLAMA" = 1 ] || confirm "Run Ollama's official installer (curl -fsSL https://ollama.com/install.sh | sh, asks for sudo)?" n; then
      printf '    %s$ curl -fsSL https://ollama.com/install.sh | sh%s\n' "$C_D" "$C_0"
      curl -fsSL https://ollama.com/install.sh | sh
    else
      info "To install by hand:  curl -fsSL https://ollama.com/install.sh | sh   (or use --install-ollama)"
      return 1
    fi
  fi
}

download_model() {  # downloads via Ollama's API (works for local and remote), with a progress bar and retries
  local model="$1" tries=0 rc
  while [ "$tries" -lt 3 ]; do
    tries=$((tries + 1))
    "$PYANY" -c "$PY_PULL" "$OLLAMA_URL" "$model"; rc=$?
    [ "$rc" -eq 0 ] && return 0
    [ "$rc" -ge 130 ] && exit 130
    printf '    retry (%d/3) in 3 s... Ollama resumes where it stopped\n' "$tries"
    sleep 3
  done
  return 1
}

ensure_model() {  # ensure_model NAME "what it is for" required(1|0)
  local m="$1" use="$2" required="$3" miss_fn=warn
  [ "$required" = 1 ] && miss_fn=fail
  if has_model "$m"; then ok "model $m ($use)"; return 0; fi
  if [ "$NO_MODELS" = 1 ] || [ "$CHECK" = 1 ]; then
    $miss_fn "model $m ($use) is not installed → ./setup.sh downloads it (12B models: about 8 GB)"
    return 1
  fi
  if [ "$OLLAMA_LOCAL" = 1 ]; then
    local free; free="$(free_gb "${OLLAMA_MODELS:-$HOME/.ollama}")"
    if [ -n "$free" ] && [ "$free" -lt "$GB_PER_MODEL" ]; then
      note "Only ${free} GB free on disk; a 12B model takes about 8 GB."
      confirm "Try to download anyway?" n || { $miss_fn "model $m ($use) not downloaded: low disk space"; return 1; }
    fi
  fi
  confirm "Download the model $m ($use; 12B models weigh about 8 GB)?" y || { $miss_fn "model $m ($use) not downloaded → ollama pull $m"; return 1; }
  printf '    downloading %s from %s ...\n' "$m" "$OLLAMA_URL"
  if download_model "$m"; then
    ok "model $m downloaded"
    INSTALLED_MODELS="$INSTALLED_MODELS"$'\n'"$m"
    return 0
  fi
  $miss_fn "I could not download $m (network? disk space?) → ollama pull $m"
  return 1
}

test_model() {  # short translation to see that the model loads and responds
  local m="$1"
  printf '    loading %s (may take a minute)...\n' "$m"
  if "$PYANY" -c "$PY_TEST" "$OLLAMA_URL" "$m"
  then ok "model $m responded"; else warn "The model $m did not respond (not enough memory? check the Ollama logs)."; fi
}

step_ollama() {
  step "5. Ollama and models"
  OLLAMA_URL="$(normalize_url "${OLLAMA_URL_ARG:-${OLLAMA_HOST:-http://localhost:11434}}")"
  if is_local "$OLLAMA_URL"; then OLLAMA_LOCAL=1; else OLLAMA_LOCAL=0; fi
  info "Server: $OLLAMA_URL ($([ "$OLLAMA_LOCAL" = 1 ] && echo 'this machine' || echo 'another machine'))"

  if [ "$OLLAMA_LOCAL" = 1 ]; then
    if find_ollama_cli; then ok "Ollama installed"
    else
      note "Ollama is not installed."
      if install_ollama; then hash -r; fi
      find_ollama_cli && ok "Ollama installed" || { fail "Ollama missing. macOS: brew install ollama (or https://ollama.com/download) · Linux: curl -fsSL https://ollama.com/install.sh | sh"; return 1; }
    fi
  fi

  if ollama_up; then
    ok "Ollama responding at $OLLAMA_URL"
  elif [ "$OLLAMA_LOCAL" = 1 ] && [ "$CHECK" != 1 ]; then
    note "Ollama is not responding; I'll start it."
    if start_local_ollama && wait_ollama; then ok "Ollama started at $OLLAMA_URL"
    else fail "I could not start Ollama. Open the Ollama app or run 'ollama serve' in another terminal and try again."; return 1; fi
  else
    if [ "$OLLAMA_LOCAL" = 1 ]; then fail "Ollama is not responding at $OLLAMA_URL (open the app or run 'ollama serve')"
    else fail "I could not reach Ollama at $OLLAMA_URL. Check the address, the network and whether the server listens outside localhost (OLLAMA_HOST=0.0.0.0)."
    fi
    return 1
  fi

  INSTALLED_MODELS="$(list_models)"
  ensure_model "$TRANS_MODEL" "translation" 1 || true
  if [ "$NO_GENERAL_MODEL" = 1 ]; then
    info "General model for OCR suggestions: skipped (--no-ocr-model)."
  elif [ "$NO_OCR" = 1 ] && [ "$GENERAL_MODEL_SET" != 1 ]; then
    info "General model for OCR suggestions: skipped (OCR disabled)."
  else
    ensure_model "$GENERAL_MODEL" "OCR correction suggestions" 0 || true
  fi
  if [ "$TEST" = 1 ] && [ "$CHECK" != 1 ]; then
    has_model "$TRANS_MODEL" && test_model "$TRANS_MODEL"
    if [ "$NO_GENERAL_MODEL" != 1 ] && has_model "$GENERAL_MODEL"; then test_model "$GENERAL_MODEL"; fi
  fi
}

# ---------------------------------------------------------------------------------------- 6. optional
step_optional() {
  step "6. Optional"
  local calibre=""
  if has ebook-convert; then calibre="$(command -v ebook-convert)"
  elif [ -x /Applications/calibre.app/Contents/MacOS/ebook-convert ]; then calibre="/Applications/calibre.app/Contents/MacOS/ebook-convert"; fi
  if [ -n "$calibre" ]; then ok "Calibre ($calibre): MOBI/AZW3/FB2/DOCX will be read"
  elif [ "$CALIBRE" = 1 ] && [ "$CHECK" != 1 ]; then
    case "$PM" in
      brew)   install_with "Calibre (brew install --cask calibre)" brew install --cask calibre ;;
      apt)    install_with "Calibre (apt)" as_root apt-get install -y calibre ;;
      dnf)    install_with "Calibre (dnf)" as_root dnf install -y calibre ;;
      pacman) install_with "Calibre (pacman)" as_root pacman -S --noconfirm calibre ;;
      *)      info "Install Calibre from https://calibre-ebook.com/download" ;;
    esac
    hash -r
    if has ebook-convert || [ -x /Applications/calibre.app/Contents/MacOS/ebook-convert ]; then ok "Calibre installed"; else warn "Calibre not installed."; fi
  else
    info "Calibre not installed (optional: only for MOBI/AZW3/FB2/DOCX; EPUB, PDF, TXT and scans work without it). Use --calibre to install it."
  fi
}

# ---------------------------------------------------------------------------------------- 7. check
step_check() {
  step "7. Final check"
  if [ -z "$VPY" ]; then info "Skipped: no usable Python."; return 0; fi
  local v
  if v="$("$VPY" "$ROOT/pharos.py" --version 2>&1)"; then ok "The program starts: $v"
  else printf '%s\n' "$v" | tail -4 | sed 's/^/    /'; fail "pharos.py does not start (see the message above)"; return 1; fi
  if [ "$NO_OCR" != 1 ] && [ -n "$TESS" ]; then
    local out
    out="$(TESSERACT_CMD="$TESS" "$VPY" -c "$PY_OCR" "$ROOT" 2>&1)"
    case "$out" in
      ok\|*) ok "The program sees Tesseract and the languages: ${out#ok|}" ;;
      *)     fail "The program could not use Tesseract: ${out#error|}" ;;
    esac
  fi
}

create_shortcut() {
  [ "$CHECK" = 1 ] && return 0
  [ -n "$VPY" ] || return 0
  {
    local u="" m="" mo=""
    [ -n "$OLLAMA_URL_ARG" ] && [ "$OLLAMA_LOCAL" != 1 ] && u="$OLLAMA_URL"
    [ "$TRANS_MODEL_SET" = 1 ] && m="$TRANS_MODEL"
    [ "$GENERAL_MODEL_SET" = 1 ] && mo="$GENERAL_MODEL"
    printf '# Generated by setup.sh (choices remembered between runs). Delete this file to go back to the defaults.\n'
    printf 'OLLAMA_URL=%q\nMODEL=%q\nOCR_MODEL=%q\n' "$u" "$m" "$mo"
  } >"$CONF"
  cat >"$ROOT/pharos.sh" <<'SHORTCUT'
#!/usr/bin/env bash
# Generated by setup.sh: runs Pharos with the environment's Python (.venv), without having to "activate" anything.
#   ./pharos.sh book.pdf          (equivalent to: python pharos.py book.pdf)
# The choices made in setup (Ollama on another machine, models) live in .setup.conf.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OLLAMA_URL=""; MODEL=""; OCR_MODEL=""
[ -f "$DIR/.setup.conf" ] && . "$DIR/.setup.conf"
PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || PY="${PYTHON:-python3}"
has_opt() { local o="$1" a; shift; for a in "$@"; do case "$a" in "$o"|"$o="*) return 0 ;; esac; done; return 1; }
if [ -n "$OLLAMA_URL" ] && ! has_opt --ollama "$@"; then set -- "$@" --ollama "$OLLAMA_URL"; fi
if [ -n "$MODEL" ] && ! has_opt --model "$@"; then set -- "$@" --model "$MODEL"; fi
if [ -n "$OCR_MODEL" ] && ! has_opt --ocr-model "$@"; then set -- "$@" --ocr-model "$OCR_MODEL"; fi
exec "$PY" "$DIR/pharos.py" "$@"
SHORTCUT
  chmod +x "$ROOT/pharos.sh"
  ok "Shortcut created: ./pharos.sh (uses the .venv; no need for 'source .venv/bin/activate')"
}

# ---------------------------------------------------------------------------------------- execution
printf '%sPharos — environment %s%s\n' "$C_B" "$([ "$CHECK" = 1 ] && echo check || echo setup)" "$C_0"
[ "$CHECK" = 1 ] && info "--check mode: nothing will be installed or changed."
[ "$CHECK" != 1 ] && [ "$YES" != 1 ] && [ ! -t 0 ] && info "No interactive terminal and no -y: I do not install system packages or download models (I only prepare the .venv and the Python libraries). Use -y to accept everything."

step_system
step_python
step_libs
step_ocr
step_ollama
step_optional
step_check
create_shortcut

step "Summary"
if [ -n "$FAIL_SUMMARY" ]; then printf '%s%sWhat is missing (required):%s\n%s' "$C_ER" "$C_B" "$C_0" "$FAIL_SUMMARY"; fi
if [ -n "$WARN_SUMMARY" ]; then printf '%s%sWarnings:%s\n%s' "$C_AV" "$C_B" "$C_0" "$WARN_SUMMARY"; fi
if [ "$FAILURES" -eq 0 ]; then
  if [ "$CHECK" = 1 ]; then printf '  %s✓ Environment ready.%s\n' "$C_OK" "$C_0"
  else
    printf '  %s✓ Environment ready.%s To translate:\n' "$C_OK" "$C_0"
    info "./pharos.sh my_book.pdf"
    info "./pharos.sh book_scan.pdf --source de      # German scan: OCR + on-screen review"
    info "./pharos.sh --help"
  fi
  exit 0
fi
if [ "$CHECK" = 1 ]; then printf '\n  Run ./setup.sh (without --check) to prepare what is missing.\n'
else printf '\n  Fix what is marked with ✗ and run ./setup.sh again: it resumes where it stopped.\n'
fi
exit 1
