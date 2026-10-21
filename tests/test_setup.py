#!/usr/bin/env python3
"""Tests of setup.sh — no internet, no Homebrew and without downloading real models.

Usage:  python tests/test_setup.py          (takes 1–2 minutes)
        python tests/test_setup.py --fast   (skips the scenarios that create a .venv)

Each scenario builds a throwaway "world": a copy of the project, a directory of fake tools on the PATH
(brew, apt-get, sudo, id, uname, tesseract, ollama, open...), a server imitating Ollama (tests/fake_ollama.py,
now with /api/pull) and a local HTTP server standing in for Tesseract's GitHub. To make the script think
Tesseract or Python "do not exist", the test hides them for that process only (unshare -m + mount --bind /dev/null);
the real system is untouched. The pip packages come from empty "wheels" built on the fly (no PyPI).

NOT covered: real macOS/Homebrew, the macOS bash 3.2, a real `ollama pull` (downloading ~16 GB), a real PyPI install,
a real download of Tesseract data from GitHub. For bash 3.2 there is only a static scan for bash 4+ features.
"""
from __future__ import annotations

import contextlib
import hashlib
import http.server
import json
import os
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNSHARE = shutil.which("unshare") or "unshare"
BASH = shutil.which("bash") or "bash"
FAKE = ROOT / "tests" / "fake_ollama.py"
SETUP = ROOT / "setup.sh"
failures = 0


def ok(cond: bool, text: str) -> None:
    global failures
    print(("  ok    " if cond else "  FAIL  ") + text)
    if not cond:
        failures += 1


def heading(t: str) -> None:
    print("\n" + t)


# ------------------------------------------------------------------------------------------ network utilities
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, host: str = "127.0.0.1", until: float = 8) -> bool:
    end = time.time() + until
    while time.time() < end:
        with contextlib.suppress(OSError), socket.create_connection((host, port), timeout=0.5):
            return True
        time.sleep(0.1)
    return False


def machine_ip() -> str | None:
    """An address that is NOT loopback (to pretend an Ollama on another machine)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))
        ip = s.getsockname()[0]
        s.close()
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


class FakeOllama:
    def __init__(self, models: str | None = None, bind: str = "127.0.0.1", **env: str):
        self.port = free_port()
        self.log = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
        e = dict(os.environ, FAKE_LOG=self.log, FAKE_BIND=bind, **env)
        if models is not None:
            e["FAKE_MODELS"] = models
        self.proc = subprocess.Popen([sys.executable, str(FAKE), str(self.port)], env=e,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert wait_port(self.port, "127.0.0.1"), "the fake Ollama did not come up"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def pulls(self) -> list[str]:
        out = []
        with contextlib.suppress(FileNotFoundError):
            for line in Path(self.log).read_text().splitlines():
                d = json.loads(line)
                if "pull" in d:
                    out.append(d["pull"])
        return out

    def stop(self) -> None:
        self.proc.terminate()
        with contextlib.suppress(Exception):
            self.proc.wait(5)
        with contextlib.suppress(OSError):
            os.unlink(self.log)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.stop()


class FakeGitHub:
    """Serves /tessdata_fast|best/raw/<ref>/<lang>.traineddata. routes: {path: bytes}."""

    def __init__(self, routes: dict[str, bytes]):
        self.routes, self.hits = routes, []
        owner = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                owner.hits.append(self.path)
                body = owner.routes.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.stop()


def data_(lang: str) -> bytes:
    return (b"FAKE-TRAINEDDATA-" + lang.encode() + b"\0") * 900   # > 10 kB, as the script requires


# ------------------------------------------------------------------------------------------ empty wheels (pip without PyPI)
def make_wheels(dest: Path) -> None:
    """One empty wheel per requirements.txt package; each carries a .pth pointing at the system packages."""
    dest.mkdir(parents=True, exist_ok=True)
    import importlib
    dirs = set()
    for mod in ("PIL", "numpy", "pypdfium2", "pdfplumber", "bs4", "docx"):
        m = importlib.import_module(mod)
        dirs.add(str(Path(m.__file__).resolve().parent.parent))
    pth = "\n".join(sorted(dirs)) + "\n"
    for dist, mod in (("pdfplumber", "pdfplumber"), ("beautifulsoup4", "bs4"), ("python-docx", "docx"), ("pillow", "PIL"),
                      ("numpy", "numpy"), ("pypdfium2", "pypdfium2"), ("reportlab", "reportlab"), ("playwright", "playwright")):
        norm = dist.replace("-", "_")
        di = f"{norm}-99.0.0.dist-info"
        files = {
            f"{di}/METADATA": f"Metadata-Version: 2.1\nName: {dist}\nVersion: 99.0.0\n",
            f"{di}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            f"zz_{norm}_system.pth": pth,
        }
        files[f"{di}/RECORD"] = "".join(f"{k},,\n" for k in files) + f"{di}/RECORD,,\n"
        if dist in ("reportlab", "playwright"):
            continue   # these two do NOT exist in the "store": --dev must fail gracefully
        with zipfile.ZipFile(dest / f"{norm}-99.0.0-py3-none-any.whl", "w") as z:
            for k, v in files.items():
                z.writestr(k, v)


# ------------------------------------------------------------------------------------------ fake tools
def write_tool(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


FAKE_TESSERACT = r'''#!/bin/bash
# fake tesseract: reads the "installed languages" from $WORLD/tessdata
D="$WORLD/tessdata"
case "$1" in
  --version) echo "tesseract 5.3.0"; echo " leptonica-1.82.0"; exit 0 ;;
  --list-langs)
    echo "List of available languages in \"$D/\" ($(ls "$D" 2>/dev/null | grep -c traineddata)):"
    ls "$D" 2>/dev/null | sed -n 's/\.traineddata$//p' | sort
    exit 0 ;;
esac
exit 0
'''

# installs what the package manager "would install": creates the fake tesseract and the .traineddata of the requested packages
INSTALL_TESSERACT = r'''
install_tess() {  # install_tess lang...
  mkdir -p "$WORLD/tessdata" "$WORLD/bin"
  cp "$WORLD/packages/tesseract.sh" "$WORLD/bin/tesseract"; chmod +x "$WORLD/bin/tesseract"
  for l in "$@"; do head -c 20000 /dev/zero | tr '\0' 'x' > "$WORLD/tessdata/$l.traineddata"; done
}
'''

APT = r'''#!/bin/bash
echo "apt-get $*" >> "$WORLD/calls.log"
''' + INSTALL_TESSERACT + r'''
[ "$1" = update ] && { touch "$WORLD/apt-updated"; exit 0; }
if [ "${APT_STALE_INDEX:-0}" = 1 ] && [ ! -e "$WORLD/apt-updated" ]; then
  echo "E: Unable to locate package (stale index)" >&2; exit 100
fi
[ "$1" = install ] || exit 0
shift
for a in "$@"; do
  case "$a" in
    -y) ;;
    tesseract-ocr) install_tess eng osd ;;
    tesseract-ocr-deu-latf) install_tess deu_latf ;;
    tesseract-ocr-*) install_tess "${a#tesseract-ocr-}" ;;
    python3) ln -sf "$WORLD/spare/python3.13" "$WORLD/bin/python3" ;;
    *) ;;
  esac
done
exit 0
'''

BREW = r'''#!/bin/bash
echo "brew $*" >> "$WORLD/calls.log"
''' + INSTALL_TESSERACT + r'''
case "$1" in
  shellenv) exit 0 ;;
  install)
    shift
    for a in "$@"; do
      case "$a" in
        tesseract) install_tess eng osd ;;
        tesseract-lang) install_tess deu spa por frk fra ita lat ;;   # no deu_latf: on purpose
        ollama) cp "$WORLD/packages/ollama.sh" "$WORLD/bin/ollama"; chmod +x "$WORLD/bin/ollama" ;;
      esac
    done ;;
  list) [ -x "$WORLD/bin/ollama" ] && exit 0 || exit 1 ;;
  services) nohup "$WORLD/bin/ollama" serve >/dev/null 2>&1 & exit 0 ;;
esac
exit 0
'''

OLLAMA = r'''#!/bin/bash
echo "ollama $*" >> "$WORLD/calls.log"
if [ "$1" = serve ]; then
  echo $$ > "$WORLD/ollama.pid"
  exec "$PYTHON_REAL" "$FAKE" "$FAKE_PORT"
fi
exit 0
'''

SUDO = r'''#!/bin/bash
echo "sudo $*" >> "$WORLD/calls.log"
exec "$@"
'''

ID_NON_ROOT = r'''#!/bin/bash
[ "$1" = "-u" ] && { echo 1000; exit 0; }
exec /usr/bin/id "$@"
'''

UNAME_MAC = r'''#!/bin/bash
case "$1" in -s) echo Darwin ;; -m) echo arm64 ;; *) exec /usr/bin/uname "$@" ;; esac
'''

SYSCTL = r'''#!/bin/bash
echo "${MEM_BYTES:-17179869184}"
'''

OPEN = r'''#!/bin/bash
echo "open $*" >> "$WORLD/calls.log"
nohup "$WORLD/bin/ollama" serve >/dev/null 2>&1 &
exit 0
'''

DF_FULL = r'''#!/bin/bash
echo "Filesystem 1024-blocks Used Available Capacity Mounted"
echo "/dev/x 100000000 98000000 2097152 98% /"
'''

HIDE = r'''#!/bin/bash
# hide.sh FILE1:FILE2 command...   (runs inside `unshare -m`: the mount only applies to this process and its children)
IFS=: read -ra files <<<"$1"; shift
for f in "${files[@]}"; do [ -e "$f" ] && mount --bind /dev/null "$f"; done
exec "$@"
'''


class World:
    def __init__(self, name: str):
        self.dir = Path(tempfile.mkdtemp(prefix=f"setup-{name}-"))
        self.bin = self.dir / "bin"
        self.proj = self.dir / "project"
        self.tmp = self.dir / "tmp"
        self.home = self.dir / "home"
        for d in (self.bin, self.tmp, self.home, self.dir / "packages", self.dir / "tessdata", self.dir / "spare"):
            d.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ROOT, self.proj, ignore=shutil.ignore_patterns(".venv", "__pycache__", ".git", "*.zip", "pharos.sh", ".setup.conf"))
        write_tool(self.dir / "packages" / "tesseract.sh", FAKE_TESSERACT)
        write_tool(self.dir / "packages" / "ollama.sh", OLLAMA)
        write_tool(self.dir / "hide.sh", HIDE)
        self.env: dict[str, str] = {}
        self.hidden: list[str] = []
        self.procs: list[subprocess.Popen] = []

    def tool(self, name: str, content: str) -> None:
        write_tool(self.bin / name, content)

    @property
    def calls(self) -> str:
        p = self.dir / "calls.log"
        return p.read_text() if p.exists() else ""

    def tessdata(self) -> list[str]:
        return sorted(p.name.removesuffix(".traineddata") for p in (self.dir / "tessdata").glob("*.traineddata"))

    def run_(self, *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        path = env.get("PATH", "/usr/bin:/bin")
        if self.hidden:
            names = tuple(sorted({Path(f).name.split(".")[0].rstrip("0123456789") for f in self.hidden}))
            path = str(sysbin(("python", "pip") if "python" in names else names))
        env.update({
            "PATH": f"{self.bin}:{path}", "WORLD": str(self.dir), "HOME": str(self.home),
            "TMPDIR": str(self.tmp), "NO_COLOR": "1", "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(self.dir / "wheels"),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHON_REAL": sys.executable, "FAKE": str(FAKE),
            "TESSERACT_CMD": "", "OLLAMA_HOST": "", "LC_ALL": "C.UTF-8",
        })
        env.update(self.env)
        cmd = ["bash", str(self.proj / "setup.sh"), *args]
        if self.hidden:
            cmd = [UNSHARE, "-m", BASH, str(self.dir / "hide.sh"), ":".join(self.hidden), BASH, *cmd[1:]]
        return subprocess.run(cmd, cwd=self.proj, env=env, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)

    def cleanup(self) -> None:
        pid = self.dir / "ollama.pid"
        if pid.exists():
            with contextlib.suppress(Exception):
                os.kill(int(pid.read_text().strip()), signal.SIGTERM)
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.cleanup()


_SYSBINS: dict[tuple, Path] = {}


def sysbin(exclude: tuple[str, ...]) -> Path:
    """A folder of links to everything the system has in /usr/bin and /usr/local/bin, except what starts with the given prefixes.
    Serves as a PATH when something needs to "not exist": the bash `command -v` finds even a non-executable file, so
    just hiding the file with mount is not enough."""
    if exclude not in _SYSBINS:
        d = Path(tempfile.mkdtemp(prefix="setup-sysbin-"))
        for folder in ("/usr/local/bin", "/usr/bin", "/bin"):
            for f in Path(folder).iterdir():
                if f.name.startswith(exclude) or (d / f.name).exists() or (d / f.name).is_symlink():
                    continue
                with contextlib.suppress(OSError):
                    (d / f.name).symlink_to(f)
        _SYSBINS[exclude] = d
    return _SYSBINS[exclude]


def reals_of(names: list[str]) -> list[str]:
    """Real paths (no symlinks) of the requested executables that exist in /usr/bin, /usr/local/bin, /bin."""
    found: set[str] = set()
    for folder in ("/usr/bin", "/usr/local/bin", "/bin"):
        for n in names:
            p = Path(folder) / n
            if p.exists():
                found.add(os.path.realpath(p))
    return sorted(found)


def lines_with(out: str, piece: str) -> list[str]:
    return [l for l in out.splitlines() if piece in l]


def contains(out: str, piece: str) -> bool:
    return piece in out


# ------------------------------------------------------------------------------------------ scenarios
def scenario_arguments() -> None:
    heading("A. Arguments and help")
    with World("args") as m:
        r = m.run_("--help")
        ok(r.returncode == 0 and "Usage: ./setup.sh" in r.stdout and "--check" in r.stdout, "--help shows the usage and exits with 0")
        r = m.run_("--nope")
        ok(r.returncode == 2 and "Unknown option" in r.stderr, "unknown option → error 2 with a message")
        r = m.run_("--ollama")
        ok(r.returncode == 2 and "needs a value" in r.stderr, "--ollama with no value → error 2")
        r = m.run_("--languages", "fra;rm -rf /")
        ok(r.returncode == 2 and "Tesseract codes" in r.stderr, "--languages with odd characters is refused")
        ok(not (m.proj / ".venv").exists() and not (m.proj / "pharos.sh").exists(), "nothing is created when arguments are refused")


def scenario_check() -> None:
    heading("B. --check: only diagnoses, changes nothing")
    with World("check") as m, FakeOllama() as oll, FakeGitHub({}) as gh:
        m.env.update(OLLAMA_HOST=oll.url, TESSDATA_URL_BASE=gh.url)
        # system tesseract (eng+osd) and some "ollama" on the PATH
        m.tool("ollama", "#!/bin/bash\nexit 0\n")
        before = sorted(str(p.relative_to(m.proj)) for p in m.proj.rglob("*") if "__pycache__" not in str(p))
        r = m.run_("--check")
        out = r.stdout
        after = sorted(str(p.relative_to(m.proj)) for p in m.proj.rglob("*") if "__pycache__" not in str(p))
        ok(r.returncode == 1, f"with no .venv the diagnosis reports a pending item (exit {r.returncode})")
        ok("--check mode" in out, "warns that it is in read-only mode")
        ok("The .venv virtual environment does not exist" in out, "reports that the .venv is missing")
        ok("Ollama responding" in out and "model translategemma:12b (translation)" in out, "finds Ollama and the translation model")
        ok("model gemma3:12b (OCR correction suggestions)" in out, "finds the general model")
        ok(re.search(r"language (deu|spa|por) missing", out) is not None, "warns about the missing OCR languages (the test machine only has eng+osd)")
        ok("No Fraktur model" in out, "warns that there is no Fraktur")
        ok(before == after, "did not create or delete any file in the project")
        ok(gh.hits == [] and oll.pulls() == [], "downloaded nothing (no tessdata, no model)")
        ok("Run ./setup.sh (without --check)" in out, "advises running without --check")
        ok(m.calls == "", "did not call brew/apt/sudo")


def scenario_linux_install(fast: bool) -> None:
    heading("C. Full install on Linux (non-root, apt, Ollama without models, Tesseract missing)")
    tess = reals_of(["tesseract"])
    ok(bool(tess), "the sandbox's real Tesseract will be hidden from the script")
    with World("linux") as m, FakeOllama(models="") as oll:
        routes = {}
        for l in ("frk",):
            routes[f"/tessdata_best/raw/main/{l}.traineddata"] = data_(l)
        routes["/tessdata_best/raw/4.1.0/deu_latf.traineddata"] = data_("deu_latf")   # only exists at the old ref
        with FakeGitHub(routes) as gh:
            make_wheels(m.dir / "wheels")
            m.tool("apt-get", APT)
            m.tool("sudo", SUDO)
            m.tool("id", ID_NON_ROOT)
            m.tool("ollama", "#!/bin/bash\nexit 0\n")
            m.hidden = tess
            m.env.update(OLLAMA_HOST=oll.url, TESSDATA_URL_BASE=gh.url, APT_STALE_INDEX="1")
            r = m.run_("-y")
            s = r.stdout
            if r.returncode != 0:
                print(s[-3000:], r.stderr[-1500:])
            ok(r.returncode == 0, f"finishes successfully (exit {r.returncode})")
            ok((m.proj / ".venv" / "bin" / "python").exists(), "created the .venv")
            ok("requirements.txt installed" in s and "pdfplumber" in s, "installed requirements.txt into the .venv and checked the libraries")
            ok("Tesseract not found" in s, "noticed that Tesseract was not installed")
            ch = m.calls
            ok("sudo apt-get install -y tesseract-ocr tesseract-ocr-eng tesseract-ocr-osd tesseract-ocr-deu tesseract-ocr-spa tesseract-ocr-por" in ch,
               "asked apt (via sudo, since not root) for Tesseract and the base languages")
            ok("apt-get update" in ch and ch.count("apt-get install") >= 2, "stale apt index: ran update and tried again")
            ok(m.tessdata() == ["deu", "deu_latf", "eng", "frk", "osd", "por", "spa"], f"final tessdata {m.tessdata()}")
            ok("/tessdata_best/raw/main/deu_latf.traineddata" in gh.hits and "/tessdata_best/raw/4.1.0/deu_latf.traineddata" in gh.hits,
               "deu_latf: tried 'main' and fell back to the 4.1.0 ref")
            ok("gothic type (Fraktur): frk deu_latf" in s, "confirms the gothic type")
            ok(sorted(oll.pulls()) == ["gemma3:12b", "translategemma:12b"], f"downloaded both models {oll.pulls()}")
            ok("pulling" in s and "100.0%" in s, "showed the download progress")
            ok("model translategemma:12b downloaded" in s, "confirms the downloaded model")
            ok("The program starts:" in s and "The program sees Tesseract and the languages" in s, "final check with the program itself")
            shortcut = m.proj / "pharos.sh"
            ok(shortcut.exists() and os.access(shortcut, os.X_OK), "created the ./pharos.sh shortcut, executable")
            ok("Environment ready" in s, "summary: environment ready")
            ok(not any("curl" in l and "ollama.com/install" in l for l in ch.splitlines()), "did not run the Ollama installer (it was already installed)")
            if not fast:
                v = subprocess.run([str(shortcut), "--version"], cwd=m.proj, capture_output=True, text=True)
                ok(v.returncode == 0 and "pharos" in v.stdout, f"./pharos.sh --version works ({v.stdout.strip()})")

            # 2nd run: nothing to do
            ch1, gh1, pl1 = m.calls, len(gh.hits), len(oll.pulls())
            r2 = m.run_("-y")
            ok(r2.returncode == 0, "the 2nd run also finishes successfully")
            ok(m.calls == ch1 and len(gh.hits) == gh1 and len(oll.pulls()) == pl1, "the 2nd run is idempotent: no apt, download or pull")
            ok("The .venv virtual environment already exists" in r2.stdout and "Tesseract 5.3.0" in r2.stdout, "recognises what is already ready")

            # --check after the install: all green, code 0
            r3 = m.run_("--check")
            ok(r3.returncode == 0 and "Environment ready" in r3.stdout and "✗" not in r3.stdout, f"--check after installing gives 0 (exit {r3.returncode})")

            # --test
            with FakeOllama(FAKE_STRICT="1") as oll2:
                m.env["OLLAMA_HOST"] = oll2.url
                r4 = m.run_("-y", "--test")
                ok("model translategemma:12b responded" in r4.stdout and "model gemma3:12b responded" in r4.stdout,
                   "--test translates a sentence with each model")
                ok(r4.returncode == 0, "--test finishes successfully")
            # --dev: with reportlab/playwright already visible (installed on the system) pip is satisfied...
            m.env["OLLAMA_HOST"] = oll.url
            r5 = m.run_("-y", "--dev")
            ok(r5.returncode == 0 and "reportlab and playwright installed" in r5.stdout, "--dev installs the test tools and proceeds")
            # ...and without them in the "store" (PIP_IGNORE_INSTALLED) pip fails: the script only warns, it does not break
            m.env["PIP_IGNORE_INSTALLED"] = "1"
            r6 = m.run_("-y", "--dev")
            ok(r6.returncode == 0 and "I could not install the test tools" in r6.stdout, "--dev with no access to the packages: only warns, does not break")
            m.env.pop("PIP_IGNORE_INSTALLED")


def scenario_models() -> None:
    heading("D. Models: failures, retries, low disk, no permission to install")
    base = ("--no-venv", "--no-ocr", "--ocr-model", "gemma3:12b")
    # D1 drop mid-download → retry
    with World("drop1") as m, FakeOllama(models="", FAKE_PULL_MODE="drop1") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.tool("ollama", "#!/bin/bash\nexit 0\n")
        r = m.run_("-y", *base)
        ok(r.returncode == 0 and "retry (1/3)" in r.stdout, "drop mid-download → retries and completes")
        ok(oll.pulls().count("translategemma:12b") == 2, f"the translation model was requested twice {oll.pulls()}")
    # D2 Ollama error: the translation model is required (fails), the general one is optional (warning)
    with World("error") as m, FakeOllama(models="", FAKE_PULL_MODE="error") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.tool("ollama", "#!/bin/bash\nexit 0\n")
        r = m.run_("-y", *base)
        ok(r.returncode == 1, "failure downloading the translation model → exit 1")
        ok("Ollama error: simulated download failure" in r.stdout, "shows the error Ollama returned")
        ok("✗ I could not download translategemma:12b" in r.stdout and "! I could not download gemma3:12b" in r.stdout,
           "required becomes ✗; optional becomes a warning")
        ok(oll.pulls().count("translategemma:12b") == 3, "gives up on the model after 3 attempts")
    # D3 model that does not exist
    with World("noexist") as m, FakeOllama(models="gemma3:12b") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.tool("ollama", "#!/bin/bash\nexit 0\n")
        r = m.run_("-y", "--model", "doesnotexist:1b", *base)
        ok(r.returncode == 1 and "file does not exist" in r.stdout, "nonexistent model: shows the Ollama error and fails")
    # D4 only 2 GB free → does not download under -y (a risky question defaults to "no")
    with World("disk") as m, FakeOllama(models="") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.tool("ollama", "#!/bin/bash\nexit 0\n")
        m.tool("df", DF_FULL)
        r = m.run_("-y", *base)
        ok("Only 2 GB free" in r.stdout and oll.pulls() == [], "low disk space: warns and does NOT download under -y")
        ok(r.returncode == 1, "and the missing required model counts as a pending item")
    # D5 --no-models / --no-ocr-model
    with World("no") as m, FakeOllama(models="translategemma:12b") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.tool("ollama", "#!/bin/bash\nexit 0\n")
        r = m.run_("-y", "--no-ocr-model", "--no-venv", "--no-ocr")
        ok(r.returncode == 0 and oll.pulls() == [] and "skipped (--no-ocr-model)" in r.stdout, "--no-ocr-model: does not download the general model")
        r = m.run_("-y", "--no-venv", "--no-ocr")
        ok(r.returncode == 0 and oll.pulls() == [] and "skipped (OCR disabled)" in r.stdout, "--no-ocr alone also skips the general model")
        r = m.run_("-y", "--no-models", *base)
        ok(r.returncode == 0 and oll.pulls() == [] and "! model gemma3:12b" in r.stdout, "--no-models: only checks (missing optional model = warning)")
    # D6 no terminal and no -y: never downloads
    with World("notty") as m, FakeOllama(models="") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.tool("ollama", "#!/bin/bash\nexit 0\n")
        r = m.run_(*base)
        ok(oll.pulls() == [] and r.returncode == 1, "no terminal and no -y: downloads no model (and reports the pending item)")
        ok("No interactive terminal and no -y" in r.stdout and "ollama pull translategemma:12b" in r.stdout, "explains and shows the manual command")
    # D7 the chosen model is remembered
    with World("remember") as m, FakeOllama(models="gemma3:4b") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.tool("ollama", "#!/bin/bash\nexit 0\n")
        r = m.run_("-y", "--model", "gemma3:4b", "--ocr-model", "gemma3:4b", "--no-venv", "--no-ocr")
        conf = (m.proj / ".setup.conf").read_text() if (m.proj / ".setup.conf").exists() else ""
        ok(r.returncode == 0 and "MODEL=gemma3:4b" in conf and "OCR_MODEL=gemma3:4b" in conf, "wrote the chosen models to .setup.conf")
        r2 = m.run_("--check", "--no-venv", "--no-ocr")
        ok("model gemma3:4b (translation)" in r2.stdout and r2.returncode == 0, "the next run reuses the choice without repeating the options")


def scenario_ollama_env() -> None:
    heading("E. Ollama: remote, unreachable, stopped (start) and not installed")
    ip = machine_ip()
    # E1 remote
    if ip:
        with World("remote") as m, FakeOllama(bind="0.0.0.0") as oll:
            url = f"http://{ip}:{oll.port}"
            r = m.run_("-y", "--ollama", url, "--no-venv", "--no-ocr")
            ok(r.returncode == 0 and "another machine" in r.stdout, f"Ollama on another machine ({ip}) works and does not need the app installed")
            conf = (m.proj / ".setup.conf").read_text()
            ok(url in conf, "the address is saved in .setup.conf")
            write_tool(m.proj / "pharos.py", "import sys\nprint('ARGS', sys.argv[1:])\n")
            a = subprocess.run([str(m.proj / "pharos.sh"), "book.pdf"], capture_output=True, text=True, cwd=m.dir).stdout
            ok(f"'--ollama', '{url}'" in a, "./pharos.sh passes --ollama on its own")
            b = subprocess.run([str(m.proj / "pharos.sh"), "book.pdf", "--ollama", "http://x:1"], capture_output=True, text=True, cwd=m.dir).stdout
            ok(b.count("--ollama") == 1 and "http://x:1" in b, "and does not duplicate it if the user already passed --ollama")
            r2 = m.run_("--check", "--no-venv", "--no-ocr")
            ok("another machine" in r2.stdout and url in r2.stdout, "the next check remembers the remote address")
        # no scheme or port
        with World("noport") as m:
            r = m.run_("--check", "--ollama", ip, "--no-venv", "--no-ocr")
            ok(f"Server: http://{ip}:11434" in r.stdout, "an address with no scheme/port is normalised (http://IP:11434)")
            ok("I could not reach Ollama" in r.stdout and r.returncode == 1, "unreachable server: a clear message and exit 1")
    else:
        print("  (no address outside loopback: the remote-Ollama scenario is skipped)")
    # E2 'localhost' and '0.0.0.0' count as this machine
    with World("local") as m, FakeOllama() as oll:
        m.tool("ollama", "#!/bin/bash\nexit 0\n")
        r = m.run_("--check", "--ollama", f"0.0.0.0:{oll.port}", "--no-venv", "--no-ocr")
        ok(f"Server: http://localhost:{oll.port} (this machine)" in r.stdout, "0.0.0.0 is swapped for localhost")
    # E3 local, installed, stopped → the script starts it (fake `ollama serve`)
    with World("stopped") as m:
        port = free_port()
        m.tool("ollama", OLLAMA)
        m.env.update(OLLAMA_HOST=f"http://127.0.0.1:{port}", FAKE_PORT=str(port))
        r = m.run_("-y", "--no-venv", "--no-ocr")
        ok("Ollama started at" in r.stdout and "ollama serve" in m.calls, "Ollama installed but stopped: the script starts it")
        ok(r.returncode == 0, f"and finishes well (the models already exist in the fake) (exit {r.returncode})")
        ok((m.proj / ".ollama-serve.log").exists(), "the server log is kept in .ollama-serve.log")
    # E4 local, not installed, Linux, no permission for the official installer
    with World("noollama") as m, FakeOllama() as oll:
        m.env["OLLAMA_HOST"] = oll.url
        r = m.run_("-y", "--no-venv", "--no-ocr")
        ok(r.returncode == 1 and "Ollama missing" in r.stdout, "Ollama not installed on Linux: fails with instructions")
        ok("curl -fsSL https://ollama.com/install.sh" in r.stdout and "ollama.com/install.sh | sh" not in m.calls, "does NOT run the remote installer without --install-ollama")
    # E5 no Ollama responding at all
    with World("dead") as m:
        port = free_port()
        m.tool("ollama", "#!/bin/bash\nexit 0\n")
        m.env["OLLAMA_HOST"] = f"http://127.0.0.1:{port}"
        r = m.run_("--check", "--no-venv", "--no-ocr")
        ok(r.returncode == 1 and "Ollama is not responding" in r.stdout, "--check with Ollama stopped: reports it without trying to start it")
        ok("ollama serve" not in m.calls, "and started nothing")


def scenario_macos(fast: bool) -> None:
    heading("F. Simulated macOS (uname=Darwin, brew, sysctl, open): the Homebrew path")
    tess = reals_of(["tesseract"])
    with World("mac") as m:
        port = free_port()
        routes = {"/tessdata_best/raw/main/deu_latf.traineddata": data_("deu_latf")}
        with FakeGitHub(routes) as gh:
            for name, content in (("uname", UNAME_MAC), ("brew", BREW), ("sysctl", SYSCTL),
                                  ("sw_vers", "#!/bin/bash\necho 15.0\n")):
                m.tool(name, content)
            m.hidden = tess
            m.env.update(OLLAMA_HOST=f"http://127.0.0.1:{port}", FAKE_PORT=str(port), TESSDATA_URL_BASE=gh.url, MEM_BYTES="8589934592")
            r = m.run_("-y", "--no-venv")
            s, ch = r.stdout, m.calls
            if r.returncode != 0:
                print(s[-2500:], r.stderr[-800:])
            ok("macOS 15.0 (arm64)" in s and "Package manager: brew" in s, "recognises macOS and Homebrew")
            ok("8 GB of RAM" in s and "12B models ask for" in s, "warns that 8 GB of RAM is little for 12B models")
            ok("brew install tesseract tesseract-lang" in ch, "installs Tesseract and all languages via brew")
            ok("brew install ollama" in ch, "installs Ollama via brew")
            ok("brew services start ollama" in ch, "starts Ollama via brew services")
            ok("Ollama started" in s, "and waits for it to respond")
            ok("deu_latf" in m.tessdata() and "/tessdata_best/raw/main/deu_latf.traineddata" in gh.hits, "deu_latf (which brew does not bring) comes from the official repository")
            ok(r.returncode == 0 and "Environment ready" in s, f"simulated macOS finishes successfully (exit {r.returncode})")
    # Ollama.app present → open -a Ollama
    app = Path("/Applications/Ollama.app")
    created = False
    if os.geteuid() == 0 and not Path("/Applications").exists():
        app.mkdir(parents=True)
        created = True
    try:
        if app.exists():
            with World("macapp") as m:
                port = free_port()
                for name, content in (("uname", UNAME_MAC), ("sysctl", SYSCTL), ("sw_vers", "#!/bin/bash\necho 15.0\n"),
                                      ("open", OPEN), ("ollama", OLLAMA)):
                    m.tool(name, content)
                m.env.update(OLLAMA_HOST=f"http://127.0.0.1:{port}", FAKE_PORT=str(port))
                r = m.run_("-y", "--no-venv", "--no-ocr")
                ok("open -a Ollama" in m.calls and "Ollama started" in r.stdout, "with the Ollama.app installed, it opens the app (open -a Ollama)")
        else:
            print("  (could not simulate /Applications/Ollama.app: scenario skipped)")
    finally:
        if created:
            shutil.rmtree("/Applications", ignore_errors=True)


def scenario_python() -> None:
    heading("G. Python: missing, old, installed via apt")
    with World("pybad") as m:
        r = m.run_("--check", "--python", "/does/not/exist/python")
        ok(r.returncode == 1 and "does not exist or is older" in r.stdout, "--python nonexistent → a clear failure")
        m.tool("pyold", "#!/bin/bash\nexit 1\n")
        r = m.run_("--check", "--python", "pyold")
        ok(r.returncode == 1 and "older than Python 3.10" in r.stdout, "an old Python is refused")
    with World("pyshim") as m:
        for name, content in (("uname", UNAME_MAC), ("sysctl", SYSCTL), ("sw_vers", "#!/bin/bash\necho 15.0\n"), ("xcode-select", "#!/bin/bash\nexit 2\n")):
            m.tool(name, content)
        r = m.run_("--check", "--python", "/usr/bin/python3")
        ok(r.returncode == 1 and "just a shim" in r.stdout, "macOS without the Command Line Tools: does not run /usr/bin/python3 (the Xcode shim)")
    py_reals = reals_of(["python3", "python", "python3.11", "python3.12", "python3.13", "python3.10", "python3.14"])
    ok(bool(py_reals), "the system Python will be hidden from the script")
    with World("nopython") as m, FakeOllama() as oll:
        shutil.copy(sys.executable, m.dir / "spare" / "python3.13")
        make_wheels(m.dir / "wheels")
        m.tool("apt-get", APT)
        m.tool("sudo", SUDO)
        m.tool("id", ID_NON_ROOT)
        m.tool("ollama", "#!/bin/bash\nexit 0\n")
        m.hidden = py_reals
        m.env["OLLAMA_HOST"] = oll.url
        r = m.run_("-y", "--no-ocr", "--no-models")
        s = r.stdout
        if r.returncode != 0:
            print(s[-2500:], r.stderr[-800:])
        ok("Python 3.10+ not found" in s, "notices there is no usable Python")
        ok("sudo apt-get install -y python3 python3-venv python3-pip" in m.calls, "installs python3 + venv + pip via apt")
        ok((m.proj / ".venv" / "bin" / "python").exists() and "requirements.txt installed" in s, "and proceeds: creates the .venv and installs the libraries")
        ok(r.returncode == 0, f"finishes successfully (exit {r.returncode})")
    with World("nopython2") as m:
        m.hidden = py_reals
        r = m.run_("--check")
        ok(r.returncode == 1 and "Python 3.10+ missing" in r.stdout, "--check with no Python: fails and shows how to install")
        ok(m.calls == "", "without installing anything")


def scenario_tesseract() -> None:
    heading("H. Tesseract: missing languages (apt), no network, no Tesseract at all, extra --languages, --no-ocr")
    tess = reals_of(["tesseract"])
    # H1 tesseract present, only eng+osd, languages via direct download (apt does not exist here)
    with World("languages") as m:
        write_tool(m.dir / "packages" / "x", "")
        m.tool("tesseract", FAKE_TESSERACT)
        for l in ("eng", "osd"):
            (m.dir / "tessdata" / f"{l}.traineddata").write_bytes(data_(l))
        routes = {f"/tessdata_fast/raw/main/{l}.traineddata": data_(l) for l in ("deu", "spa", "por", "fra")}
        routes["/tessdata_best/raw/main/frk.traineddata"] = data_("frk")
        with FakeGitHub(routes) as gh:
            m.env["TESSDATA_URL_BASE"] = gh.url
            m.tool("ollama", "#!/bin/bash\nexit 0\n")
            with FakeOllama() as oll:
                m.env["OLLAMA_HOST"] = oll.url
                r = m.run_("-y", "--no-venv", "--languages", "fra,ita")
                s = r.stdout
                ok(m.tessdata() == ["deu", "eng", "fra", "frk", "osd", "por", "spa"], f"downloaded deu spa por fra (fast) and frk (best): {m.tessdata()}")
                ok("language ita missing" in s and "I could not download ita" in s, "a language that does not exist at the source: only warns")
                ok("I could not download deu_latf" in s and "gothic type (Fraktur): frk" in s, "partial Fraktur (frk yes, deu_latf no) is reported")
                ok(r.returncode == 0, f"warnings do not block exit 0 (exit {r.returncode})")
                ok("/tessdata_fast/raw/main/deu.traineddata" in gh.hits and "/tessdata_best/raw/main/frk.traineddata" in gh.hits,
                   "common languages come from tessdata_fast; Fraktur from tessdata_best")
        # a corrupt download (tiny file / error HTML) is rejected
        with World("corrupt") as m2:
            m2.tool("tesseract", FAKE_TESSERACT)
            for l in ("eng", "osd", "deu", "spa", "por"):
                (m2.dir / "tessdata" / f"{l}.traineddata").write_bytes(data_(l))
            with FakeGitHub({"/tessdata_best/raw/main/frk.traineddata": b"<html>Not found</html>"}) as gh2, FakeOllama() as oll:
                m2.env.update(TESSDATA_URL_BASE=gh2.url, OLLAMA_HOST=oll.url)
                m2.tool("ollama", "#!/bin/bash\nexit 0\n")
                m2.run_("-y", "--no-venv")
                ok("frk" not in m2.tessdata(), "a too-small response (error page) is not written as a language")
    # H2 tessdata folder not writable + non-root user → uses sudo to copy
    with World("sudocp") as m:
        m.tool("tesseract", FAKE_TESSERACT)
        m.tool("sudo", SUDO)
        m.tool("id", ID_NON_ROOT)
        for l in ("eng", "osd", "deu", "spa", "por"):
            (m.dir / "tessdata" / f"{l}.traineddata").write_bytes(data_(l))
        os.chmod(m.dir / "tessdata", 0o555)
        with FakeGitHub({"/tessdata_best/raw/main/frk.traineddata": data_("frk")}) as gh, FakeOllama() as oll:
            m.env.update(TESSDATA_URL_BASE=gh.url, OLLAMA_HOST=oll.url)
            m.tool("ollama", "#!/bin/bash\nexit 0\n")
            r = m.run_("-y", "--no-venv")
            # root can write even in 555; what matters is that the script's path worked
            ok("frk" in m.tessdata(), "the gothic language written into the Tesseract folder")
        os.chmod(m.dir / "tessdata", 0o755)
    # H3 no Tesseract and no package manager → fails with instructions
    with World("none") as m, FakeOllama() as oll:
        m.hidden = tess
        m.env["OLLAMA_HOST"] = oll.url
        m.tool("ollama", "#!/bin/bash\nexit 0\n")
        m.tool("apt-get", "#!/bin/bash\nexit 1\n")   # present, but installs nothing
        r = m.run_("-y", "--no-venv")
        ok(r.returncode == 1 and "Tesseract missing" in r.stdout and "tesseract-ocr-deu" in r.stdout, "unable to install Tesseract: fails with the right command")
        ok("The program sees" not in r.stdout, "and does not pretend the final check passed")
    # H4 --no-ocr
    with World("noocr") as m, FakeOllama() as oll:
        m.hidden = tess
        m.env["OLLAMA_HOST"] = oll.url
        m.tool("ollama", "#!/bin/bash\nexit 0\n")
        r = m.run_("-y", "--no-venv", "--no-ocr")
        ok(r.returncode == 0 and "Skipped (--no-ocr)" in r.stdout and "General model for OCR suggestions: skipped (OCR disabled)" in r.stdout,
           "--no-ocr needs neither Tesseract nor the suggestions model")
    # H5 old Tesseract
    with World("old") as m, FakeOllama() as oll:
        m.tool("tesseract", FAKE_TESSERACT.replace("tesseract 5.3.0", "tesseract 3.04.01"))
        m.env["OLLAMA_HOST"] = oll.url
        m.tool("ollama", "#!/bin/bash\nexit 0\n")
        r = m.run_("--check", "--no-venv")
        ok("too old" in r.stdout, "Tesseract 3.x is refused (no LSTM)")


def scenario_interactive() -> None:
    heading("I. Interactive terminal (pty): questions, y/n answers and the progress bar")
    import pty
    with World("pty") as m, FakeOllama(models="", FAKE_PULL_MODE="slow") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.tool("ollama", "#!/bin/bash\nexit 0\n")
        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        env.update(PATH=f"{m.bin}:{env['PATH']}", WORLD=str(m.dir), HOME=str(m.home), TMPDIR=str(m.tmp), NO_COLOR="1",
                   OLLAMA_HOST=oll.url, TERM="xterm", TESSERACT_CMD="")
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(m.proj)
            os.execvpe("bash", ["bash", "setup.sh", "--no-venv", "--no-ocr", "--ocr-model", "gemma3:12b"], env)
        answers = ["n", "y"]          # does not download the translation model; downloads the general one
        buf = b""
        questions = 0
        end = time.time() + 90
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.5)
            if not r:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if (buf.rstrip().endswith(b"[Y/n]") or buf.rstrip().endswith(b"[y/N]")) and questions < len(answers) \
                    and buf.count(b"[Y/n]") + buf.count(b"[y/N]") > questions:
                os.write(fd, (answers[questions] + "\n").encode())
                questions += 1
        _, status = os.waitpid(pid, 0)
        text = buf.decode("utf-8", "replace")
        code = os.waitstatus_to_exitcode(status)
        ok(questions == 2, f"asked the 2 download questions (translation model and general model): {questions}")
        ok(oll.pulls() == ["gemma3:12b"], f"answering n/y downloaded only the second model {oll.pulls()}")
        ok("\r" in text and "100.0%" in text, "with a terminal, the progress bar updates on the same line (\\r)")
        ok(code == 1 and "not downloaded" in text, f"required model refused → pending item and exit 1 (exit {code})")


def scenario_ctrl_c() -> None:
    heading("J. Ctrl+C during the download")
    with World("ctrlc") as m, FakeOllama(models="", FAKE_PULL_MODE="slow") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.tool("ollama", "#!/bin/bash\nexit 0\n")
        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        env.update(PATH=f"{m.bin}:{env['PATH']}", WORLD=str(m.dir), HOME=str(m.home), TMPDIR=str(m.tmp), NO_COLOR="1", OLLAMA_HOST=oll.url, TESSERACT_CMD="")
        p = subprocess.Popen(["bash", "setup.sh", "-y", "--no-venv", "--no-ocr"], cwd=m.proj, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True)
        t0 = time.time()
        while time.time() - t0 < 20 and not oll.pulls():
            time.sleep(0.1)
        time.sleep(0.8)
        os.killpg(p.pid, signal.SIGINT)
        try:
            out, _ = p.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
            out, _ = p.communicate()
        ok(p.returncode == 130, f"Ctrl+C exits with 130 (got {p.returncode})")
        ok("Interrupted" in out and "Traceback" not in out, "shows a short message, no traceback")
        ok(len(oll.pulls()) == 1, "does not keep downloading the next model after being interrupted")


def scan_bash32() -> None:
    heading("K. Syntax and compatibility with the macOS bash 3.2 (static scan)")
    r = subprocess.run(["bash", "-n", str(SETUP)], capture_output=True, text=True)
    ok(r.returncode == 0, f"bash -n: {r.stderr.strip() or 'syntax ok'}")
    text = SETUP.read_text()
    # strip the Python snippets (heredocs <<'PY'), which are not bash
    no_py = re.sub(r"<<'PY' \|\| true\n.*?\nPY\n", "\n", text, flags=re.S)
    no_comments = "\n".join(l for l in no_py.splitlines() if not l.lstrip().startswith("#"))
    forbidden = {
        r"\bdeclare\s+-[a-zA-Z]*A": "associative arrays (bash 4)",
        r"\bmapfile\b|\breadarray\b": "mapfile/readarray (bash 4)",
        r"\$\{[A-Za-z_][A-Za-z_0-9]*(,,|\^\^|,|\^)\}": "case conversion ${v,,} ${v^^} (bash 4)",
        r"&>>": "&>> (bash 4)",
        r"\|&": "|& (bash 4)",
        r"\bcoproc\b": "coproc (bash 4)",
        r"\bwait\s+-n\b": "wait -n (bash 4.3)",
        r"\blocal\s+-n\b|\bdeclare\s+-n\b": "namerefs (bash 4.3)",
        r";;&|;&": "case with ;;& or ;& (bash 4)",
        r"read\s+(-[a-zA-Z]+\s+)*-t\s+0?\.\d": "fractional read -t (bash 4)",
        r"read\s+(-[a-zA-Z]+\s+)*-N": "read -N (bash 4)",
        r"\bsudo\s+-n": "silent sudo -n (different behaviour on macOS)",
        r"\bgrep\s+(-[a-zA-Z]*)?P": "grep -P (does not exist in the macOS grep)",
        r"\bsed\s+(-[a-zA-Z]*)?[ri]\b.*\\[+?|]": "sed -r / \\+ (different on BSD)",
        r"\breadlink\s+-f": "readlink -f (does not exist on old macOS)",
        r"\bdate\s+-d": "date -d (GNU)",
        r"\bstat\s+-c": "stat -c (GNU)",
        r"\bxargs\s+-r": "xargs -r (GNU)",
    }
    for pattern, name in forbidden.items():
        found = [l.strip() for l in no_comments.splitlines() if re.search(pattern, l)]
        ok(not found, f"no {name}" + (f" → {found[:2]}" if found else ""))
    # an empty "$@"/"${arr[@]}" with `set -u` breaks on bash < 4.4: the script must not use arrays
    ok(not re.search(r"\w+=\(", no_comments) and "${#" not in no_comments.replace("${#}", ""), "no arrays (avoids the bash 3.2 'unbound variable' with set -u)")
    ok("set -u -o pipefail" in text and "set -e" not in text.replace("set -u -o pipefail", ""), "set -u -o pipefail, no set -e (each step handles its own error)")
    ok(os.access(SETUP, os.X_OK), "setup.sh is executable")
    ok(text.startswith("#!/usr/bin/env bash"), "portable shebang (#!/usr/bin/env bash)")
    with_sudo = [l for l in no_comments.splitlines() if re.search(r"\bsudo\b", l)]
    execution = [l for l in with_sudo if re.search(r'\bsudo\s+"\$@"', l)]
    text_only = [l for l in with_sudo if l not in execution and not re.search(r"\b(info|fail|warn|confirm|echo|printf)\b", l)]
    ok(len(execution) == 1 and not text_only, f"the only executable sudo is the one in as_root (the rest are help text) {text_only[:2]}")


def real_tessdata() -> dict[str, int]:
    """State of THIS machine's Tesseract language folder: no scenario may touch it."""
    r = subprocess.run([shutil.which("tesseract") or "tesseract", "--list-langs"], capture_output=True, text=True)
    m = re.search(r'in "(.*?)/?"', r.stdout + r.stderr)
    folder = Path(m.group(1)) if m else None
    return {f.name: f.stat().st_size for f in folder.glob("*.traineddata")} if folder and folder.is_dir() else {}


def main() -> int:
    fast = "--fast" in sys.argv
    tessdata_before = real_tessdata()
    if not shutil.which("unshare"):
        print("(no `unshare`: scenarios that hide Tesseract/Python will be skipped)")
    t0 = time.time()
    scenario_arguments()
    scenario_check()
    scenario_models()
    scenario_ollama_env()
    scenario_tesseract()
    scenario_macos(fast)
    if not fast:
        scenario_linux_install(fast)
        scenario_python()
    scenario_interactive()
    scenario_ctrl_c()
    scan_bash32()
    heading("L. Safety of the test itself")
    ok(real_tessdata() == tessdata_before, "this machine's Tesseract language folder stayed intact")
    for d in _SYSBINS.values():
        shutil.rmtree(d, ignore_errors=True)
    print(f"\n{time.time() - t0:.0f} s")
    print("ALL GOOD" if failures == 0 else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
