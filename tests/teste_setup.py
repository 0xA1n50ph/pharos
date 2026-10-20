#!/usr/bin/env python3
"""Testes do setup.sh — sem internet, sem Homebrew e sem baixar modelos de verdade.

Uso:  python tests/teste_setup.py            (leva 1–2 minutos)
      python tests/teste_setup.py --rapido   (pula os cenários que criam .venv)

Cada cenário monta um "mundo" descartável: uma cópia do projeto, um diretório de ferramentas falsas no PATH
(brew, apt-get, sudo, id, uname, tesseract, ollama, open...), um servidor que imita o Ollama (tests/ollama_falso.py,
agora com /api/pull) e um servidor HTTP local que faz o papel do GitHub do Tesseract. Para o script achar que o
Tesseract ou o Python "não existem", o teste os esconde só para aquele processo (unshare -m + mount --bind /dev/null);
o sistema de verdade não é tocado. Os pacotes do pip vêm de "rodas" vazias criadas na hora (sem PyPI).

NÃO cobre: macOS/Homebrew de verdade, o bash 3.2 do macOS, `ollama pull` real (baixar ~16 GB), instalação real pelo PyPI,
download real dos dados do Tesseract no GitHub. Para o bash 3.2 há só uma varredura estática por recursos do bash 4+.
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

RAIZ = Path(__file__).resolve().parent.parent
UNSHARE = shutil.which("unshare") or "unshare"
BASH = shutil.which("bash") or "bash"
FALSO = RAIZ / "tests" / "ollama_falso.py"
SETUP = RAIZ / "setup.sh"
falhas = 0


def ok(cond: bool, texto: str) -> None:
    global falhas
    print(("  ok    " if cond else "  FALHOU ") + texto)
    if not cond:
        falhas += 1


def titulo(t: str) -> None:
    print("\n" + t)


# ------------------------------------------------------------------------------------------ utilidades de rede
def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def esperar_porta(porta: int, host: str = "127.0.0.1", ate: float = 8) -> bool:
    fim = time.time() + ate
    while time.time() < fim:
        with contextlib.suppress(OSError), socket.create_connection((host, porta), timeout=0.5):
            return True
        time.sleep(0.1)
    return False


def ip_da_maquina() -> str | None:
    """Um endereço que NÃO seja de loopback (para fingir um Ollama em outra máquina)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))
        ip = s.getsockname()[0]
        s.close()
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


class OllamaFalso:
    def __init__(self, modelos: str | None = None, bind: str = "127.0.0.1", **env: str):
        self.porta = porta_livre()
        self.log = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
        e = dict(os.environ, FALSO_LOG=self.log, FALSO_BIND=bind, **env)
        if modelos is not None:
            e["FALSO_MODELOS"] = modelos
        self.proc = subprocess.Popen([sys.executable, str(FALSO), str(self.porta)], env=e,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert esperar_porta(self.porta, "127.0.0.1"), "o Ollama falso não subiu"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.porta}"

    def pulls(self) -> list[str]:
        out = []
        with contextlib.suppress(FileNotFoundError):
            for linha in Path(self.log).read_text().splitlines():
                d = json.loads(linha)
                if "pull" in d:
                    out.append(d["pull"])
        return out

    def parar(self) -> None:
        self.proc.terminate()
        with contextlib.suppress(Exception):
            self.proc.wait(5)
        with contextlib.suppress(OSError):
            os.unlink(self.log)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.parar()


class GitHubFalso:
    """Serve /tessdata_fast|best/raw/<ref>/<idioma>.traineddata. rotas: {caminho: bytes}."""

    def __init__(self, rotas: dict[str, bytes]):
        self.rotas, self.acessos = rotas, []
        dono = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                dono.acessos.append(self.path)
                corpo = dono.rotas.get(self.path)
                if corpo is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def parar(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.parar()


def dados(idioma: str) -> bytes:
    return (b"TRAINEDDATA-FALSO-" + idioma.encode() + b"\0") * 900   # > 10 kB, como o script exige


# ------------------------------------------------------------------------------------------ rodas vazias (pip sem PyPI)
def criar_rodas(destino: Path) -> None:
    """Uma roda vazia por pacote do requirements.txt; cada uma traz um .pth que aponta para os pacotes do sistema."""
    destino.mkdir(parents=True, exist_ok=True)
    import importlib
    pastas = set()
    for mod in ("PIL", "numpy", "pypdfium2", "pdfplumber", "bs4", "docx"):
        m = importlib.import_module(mod)
        pastas.add(str(Path(m.__file__).resolve().parent.parent))
    pth = "\n".join(sorted(pastas)) + "\n"
    for dist, mod in (("pdfplumber", "pdfplumber"), ("beautifulsoup4", "bs4"), ("python-docx", "docx"), ("pillow", "PIL"),
                      ("numpy", "numpy"), ("pypdfium2", "pypdfium2"), ("reportlab", "reportlab"), ("playwright", "playwright")):
        norm = dist.replace("-", "_")
        di = f"{norm}-99.0.0.dist-info"
        arqs = {
            f"{di}/METADATA": f"Metadata-Version: 2.1\nName: {dist}\nVersion: 99.0.0\n",
            f"{di}/WHEEL": "Wheel-Version: 1.0\nGenerator: teste\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            f"zz_{norm}_sistema.pth": pth,
        }
        arqs[f"{di}/RECORD"] = "".join(f"{k},,\n" for k in arqs) + f"{di}/RECORD,,\n"
        if dist in ("reportlab", "playwright"):
            continue   # essas duas NÃO existem na "loja": o --dev precisa falhar com elegância
        with zipfile.ZipFile(destino / f"{norm}-99.0.0-py3-none-any.whl", "w") as z:
            for k, v in arqs.items():
                z.writestr(k, v)


# ------------------------------------------------------------------------------------------ ferramentas falsas
def escrever(caminho: Path, texto: str) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(texto)
    caminho.chmod(caminho.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


TESSERACT_FALSO = r'''#!/bin/bash
# tesseract de mentira: lê os "idiomas instalados" de $MUNDO/tessdata
D="$MUNDO/tessdata"
case "$1" in
  --version) echo "tesseract 5.3.0"; echo " leptonica-1.82.0"; exit 0 ;;
  --list-langs)
    echo "List of available languages in \"$D/\" ($(ls "$D" 2>/dev/null | grep -c traineddata)):"
    ls "$D" 2>/dev/null | sed -n 's/\.traineddata$//p' | sort
    exit 0 ;;
esac
exit 0
'''

# instala o que o gerenciador de pacotes "instalaria": cria o tesseract falso e os .traineddata dos pacotes pedidos
INSTALA_TESSERACT = r'''
instala_tess() {  # instala_tess idioma...
  mkdir -p "$MUNDO/tessdata" "$MUNDO/bin"
  cp "$MUNDO/pacotes/tesseract.sh" "$MUNDO/bin/tesseract"; chmod +x "$MUNDO/bin/tesseract"
  for l in "$@"; do head -c 20000 /dev/zero | tr '\0' 'x' > "$MUNDO/tessdata/$l.traineddata"; done
}
'''

APT = r'''#!/bin/bash
echo "apt-get $*" >> "$MUNDO/chamadas.log"
''' + INSTALA_TESSERACT + r'''
[ "$1" = update ] && { touch "$MUNDO/apt-atualizado"; exit 0; }
if [ "${APT_INDICE_VELHO:-0}" = 1 ] && [ ! -e "$MUNDO/apt-atualizado" ]; then
  echo "E: Unable to locate package (índice velho)" >&2; exit 100
fi
[ "$1" = install ] || exit 0
shift
for a in "$@"; do
  case "$a" in
    -y) ;;
    tesseract-ocr) instala_tess eng osd ;;
    tesseract-ocr-deu-latf) instala_tess deu_latf ;;
    tesseract-ocr-*) instala_tess "${a#tesseract-ocr-}" ;;
    python3) ln -sf "$MUNDO/reserva/python3.13" "$MUNDO/bin/python3" ;;
    *) ;;
  esac
done
exit 0
'''

BREW = r'''#!/bin/bash
echo "brew $*" >> "$MUNDO/chamadas.log"
''' + INSTALA_TESSERACT + r'''
case "$1" in
  shellenv) exit 0 ;;
  install)
    shift
    for a in "$@"; do
      case "$a" in
        tesseract) instala_tess eng osd ;;
        tesseract-lang) instala_tess deu spa por frk fra ita lat ;;   # sem deu_latf: de propósito
        ollama) cp "$MUNDO/pacotes/ollama.sh" "$MUNDO/bin/ollama"; chmod +x "$MUNDO/bin/ollama" ;;
      esac
    done ;;
  list) [ -x "$MUNDO/bin/ollama" ] && exit 0 || exit 1 ;;
  services) nohup "$MUNDO/bin/ollama" serve >/dev/null 2>&1 & exit 0 ;;
esac
exit 0
'''

OLLAMA = r'''#!/bin/bash
echo "ollama $*" >> "$MUNDO/chamadas.log"
if [ "$1" = serve ]; then
  echo $$ > "$MUNDO/ollama.pid"
  exec "$PYTHON_REAL" "$FALSO" "$FALSO_PORTA"
fi
exit 0
'''

SUDO = r'''#!/bin/bash
echo "sudo $*" >> "$MUNDO/chamadas.log"
exec "$@"
'''

ID_NAO_ROOT = r'''#!/bin/bash
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
echo "open $*" >> "$MUNDO/chamadas.log"
nohup "$MUNDO/bin/ollama" serve >/dev/null 2>&1 &
exit 0
'''

DF_CHEIO = r'''#!/bin/bash
echo "Filesystem 1024-blocks Used Available Capacity Mounted"
echo "/dev/x 100000000 98000000 2097152 98% /"
'''

ESCONDER = r'''#!/bin/bash
# esconder.sh ARQ1:ARQ2 comando...   (roda dentro de `unshare -m`: o mount só vale para este processo e seus filhos)
IFS=: read -ra arqs <<<"$1"; shift
for f in "${arqs[@]}"; do [ -e "$f" ] && mount --bind /dev/null "$f"; done
exec "$@"
'''


class Mundo:
    def __init__(self, nome: str):
        self.dir = Path(tempfile.mkdtemp(prefix=f"setup-{nome}-"))
        self.bin = self.dir / "bin"
        self.proj = self.dir / "projeto"
        self.tmp = self.dir / "tmp"
        self.home = self.dir / "home"
        for d in (self.bin, self.tmp, self.home, self.dir / "pacotes", self.dir / "tessdata", self.dir / "reserva"):
            d.mkdir(parents=True, exist_ok=True)
        shutil.copytree(RAIZ, self.proj, ignore=shutil.ignore_patterns(".venv", "__pycache__", ".git", "*.zip", "pharos.sh", ".setup.conf"))
        escrever(self.dir / "pacotes" / "tesseract.sh", TESSERACT_FALSO)
        escrever(self.dir / "pacotes" / "ollama.sh", OLLAMA)
        escrever(self.dir / "esconder.sh", ESCONDER)
        self.env: dict[str, str] = {}
        self.escondidos: list[str] = []
        self.processos: list[subprocess.Popen] = []

    def ferramenta(self, nome: str, conteudo: str) -> None:
        escrever(self.bin / nome, conteudo)

    @property
    def chamadas(self) -> str:
        p = self.dir / "chamadas.log"
        return p.read_text() if p.exists() else ""

    def tessdata(self) -> list[str]:
        return sorted(p.name.removesuffix(".traineddata") for p in (self.dir / "tessdata").glob("*.traineddata"))

    def rodar(self, *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        caminho = env.get("PATH", "/usr/bin:/bin")
        if self.escondidos:
            nomes = tuple(sorted({Path(f).name.split(".")[0].rstrip("0123456789") for f in self.escondidos}))
            caminho = str(sysbin(("python", "pip") if "python" in nomes else nomes))
        env.update({
            "PATH": f"{self.bin}:{caminho}", "MUNDO": str(self.dir), "HOME": str(self.home),
            "TMPDIR": str(self.tmp), "NO_COLOR": "1", "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(self.dir / "rodas"),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHON_REAL": sys.executable, "FALSO": str(FALSO),
            "TESSERACT_CMD": "", "OLLAMA_HOST": "", "LC_ALL": "C.UTF-8",
        })
        env.update(self.env)
        cmd = ["bash", str(self.proj / "setup.sh"), *args]
        if self.escondidos:
            cmd = [UNSHARE, "-m", BASH, str(self.dir / "esconder.sh"), ":".join(self.escondidos), BASH, *cmd[1:]]
        return subprocess.run(cmd, cwd=self.proj, env=env, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)

    def limpar(self) -> None:
        pid = self.dir / "ollama.pid"
        if pid.exists():
            with contextlib.suppress(Exception):
                os.kill(int(pid.read_text().strip()), signal.SIGTERM)
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.limpar()


_SYSBINS: dict[tuple, Path] = {}


def sysbin(excluir: tuple[str, ...]) -> Path:
    """Uma pasta de links para tudo que o sistema tem em /usr/bin e /usr/local/bin, menos o que começa com os prefixos dados.
    Serve de PATH quando algo precisa "não existir": o `command -v` do bash acha até arquivo não executável, então só
    esconder o arquivo com mount não basta."""
    if excluir not in _SYSBINS:
        d = Path(tempfile.mkdtemp(prefix="setup-sysbin-"))
        for pasta in ("/usr/local/bin", "/usr/bin", "/bin"):
            for f in Path(pasta).iterdir():
                if f.name.startswith(excluir) or (d / f.name).exists() or (d / f.name).is_symlink():
                    continue
                with contextlib.suppress(OSError):
                    (d / f.name).symlink_to(f)
        _SYSBINS[excluir] = d
    return _SYSBINS[excluir]


def reais_de(nomes: list[str]) -> list[str]:
    """Caminhos reais (sem symlinks) dos executáveis pedidos que existem em /usr/bin, /usr/local/bin, /bin."""
    achados: set[str] = set()
    for pasta in ("/usr/bin", "/usr/local/bin", "/bin"):
        for n in nomes:
            p = Path(pasta) / n
            if p.exists():
                achados.add(os.path.realpath(p))
    return sorted(achados)


def linhas_com(saida: str, trecho: str) -> list[str]:
    return [l for l in saida.splitlines() if trecho in l]


def tem(saida: str, trecho: str) -> bool:
    return trecho in saida


# ------------------------------------------------------------------------------------------ cenários
def cenario_argumentos() -> None:
    titulo("A. Argumentos e ajuda")
    with Mundo("args") as m:
        r = m.rodar("--ajuda")
        ok(r.returncode == 0 and "Uso: ./setup.sh" in r.stdout and "--verificar" in r.stdout, "--ajuda mostra o uso e sai com 0")
        r = m.rodar("--nada")
        ok(r.returncode == 2 and "Opção desconhecida" in r.stderr, "opção desconhecida → erro 2 com mensagem")
        r = m.rodar("--ollama")
        ok(r.returncode == 2 and "precisa de um valor" in r.stderr, "--ollama sem valor → erro 2")
        r = m.rodar("--idiomas", "fra;rm -rf /")
        ok(r.returncode == 2 and "códigos do Tesseract" in r.stderr, "--idiomas com caracteres estranhos é recusado")
        ok(not (m.proj / ".venv").exists() and not (m.proj / "pharos.sh").exists(), "nada é criado ao recusar argumentos")


def cenario_verificar() -> None:
    titulo("B. --verificar: só diagnostica, não altera nada")
    with Mundo("verificar") as m, OllamaFalso() as oll, GitHubFalso({}) as gh:
        m.env.update(OLLAMA_HOST=oll.url, TESSDATA_URL_BASE=gh.url)
        # tesseract do sistema (eng+osd) e um "ollama" qualquer no PATH
        m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
        antes = sorted(str(p.relative_to(m.proj)) for p in m.proj.rglob("*") if "__pycache__" not in str(p))
        r = m.rodar("--verificar")
        saida = r.stdout
        depois = sorted(str(p.relative_to(m.proj)) for p in m.proj.rglob("*") if "__pycache__" not in str(p))
        ok(r.returncode == 1, f"sem .venv o diagnóstico acusa pendência (saída {r.returncode})")
        ok("Modo --verificar" in saida, "avisa que está no modo só-leitura")
        ok("Ambiente virtual .venv não existe" in saida, "acusa que falta o .venv")
        ok("Ollama respondendo" in saida and "modelo translategemma:12b (tradução)" in saida, "encontra o Ollama e o modelo de tradução")
        ok("modelo gemma3:12b (sugestões de correção do OCR)" in saida, "encontra o modelo geral")
        ok(re.search(r"idioma (deu|spa|por) ausente", saida) is not None, "avisa os idiomas do OCR que faltam (a máquina de teste só tem eng+osd)")
        ok("Sem modelo de Fraktur" in saida, "avisa que não há Fraktur")
        ok(antes == depois, "não criou nem apagou nenhum arquivo no projeto")
        ok(gh.acessos == [] and oll.pulls() == [], "não baixou nada (nem tessdata, nem modelo)")
        ok("Rode ./setup.sh (sem --verificar)" in saida, "orienta a rodar sem --verificar")
        ok(m.chamadas == "", "não chamou brew/apt/sudo")


def cenario_instalacao_linux(rapido: bool) -> None:
    titulo("C. Instalação completa no Linux (não root, apt, Ollama sem modelos, Tesseract ausente)")
    tess = reais_de(["tesseract"])
    ok(bool(tess), "o Tesseract real do sandbox será escondido do script")
    with Mundo("linux") as m, OllamaFalso(modelos="") as oll:
        rotas = {}
        for l in ("frk",):
            rotas[f"/tessdata_best/raw/main/{l}.traineddata"] = dados(l)
        rotas["/tessdata_best/raw/4.1.0/deu_latf.traineddata"] = dados("deu_latf")   # só existe na referência antiga
        with GitHubFalso(rotas) as gh:
            criar_rodas(m.dir / "rodas")
            m.ferramenta("apt-get", APT)
            m.ferramenta("sudo", SUDO)
            m.ferramenta("id", ID_NAO_ROOT)
            m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
            m.escondidos = tess
            m.env.update(OLLAMA_HOST=oll.url, TESSDATA_URL_BASE=gh.url, APT_INDICE_VELHO="1")
            r = m.rodar("-y")
            s = r.stdout
            if r.returncode != 0:
                print(s[-3000:], r.stderr[-1500:])
            ok(r.returncode == 0, f"termina com sucesso (saída {r.returncode})")
            ok((m.proj / ".venv" / "bin" / "python").exists(), "criou o .venv")
            ok("requirements.txt instalado" in s and "pdfplumber" in s, "instalou o requirements.txt no .venv e conferiu as bibliotecas")
            ok("Tesseract não encontrado" in s, "percebeu que o Tesseract não estava instalado")
            ch = m.chamadas
            ok("sudo apt-get install -y tesseract-ocr tesseract-ocr-eng tesseract-ocr-osd tesseract-ocr-deu tesseract-ocr-spa tesseract-ocr-por" in ch,
               "pediu ao apt (via sudo, pois não é root) o Tesseract e os idiomas base")
            ok("apt-get update" in ch and ch.count("apt-get install") >= 2, "índice velho do apt: rodou update e tentou de novo")
            ok(m.tessdata() == ["deu", "deu_latf", "eng", "frk", "osd", "por", "spa"], f"tessdata final {m.tessdata()}")
            ok("/tessdata_best/raw/main/deu_latf.traineddata" in gh.acessos and "/tessdata_best/raw/4.1.0/deu_latf.traineddata" in gh.acessos,
               "deu_latf: tentou 'main' e caiu para a referência 4.1.0")
            ok("letra gótica (Fraktur): frk deu_latf" in s, "confirma a letra gótica")
            ok(sorted(oll.pulls()) == ["gemma3:12b", "translategemma:12b"], f"baixou os dois modelos {oll.pulls()}")
            ok("pulling" in s and "100.0%" in s, "mostrou o progresso do download")
            ok("modelo translategemma:12b baixado" in s, "confirma o modelo baixado")
            ok("O programa abre:" in s and "O programa enxerga o Tesseract e os idiomas" in s, "conferência final com o próprio programa")
            traduzir = m.proj / "pharos.sh"
            ok(traduzir.exists() and os.access(traduzir, os.X_OK), "criou o atalho ./pharos.sh executável")
            ok("Ambiente pronto" in s, "resumo: ambiente pronto")
            ok(not any("curl" in l and "ollama.com/install" in l for l in ch.splitlines()), "não rodou o instalador do Ollama (já estava instalado)")
            if not rapido:
                v = subprocess.run([str(traduzir), "--versao"], cwd=m.proj, capture_output=True, text=True)
                ok(v.returncode == 0 and "pharos" in v.stdout, f"./pharos.sh --versao funciona ({v.stdout.strip()})")

            # 2ª execução: nada a fazer
            ch1, gh1, pl1 = m.chamadas, len(gh.acessos), len(oll.pulls())
            r2 = m.rodar("-y")
            ok(r2.returncode == 0, "2ª execução também termina com sucesso")
            ok(m.chamadas == ch1 and len(gh.acessos) == gh1 and len(oll.pulls()) == pl1, "2ª execução é idempotente: nenhum apt, download ou pull")
            ok("Ambiente virtual .venv já existe" in r2.stdout and "Tesseract 5.3.0" in r2.stdout, "reconhece o que já está pronto")

            # --verificar depois da instalação: tudo verde, código 0
            r3 = m.rodar("--verificar")
            ok(r3.returncode == 0 and "Ambiente pronto" in r3.stdout and "✗" not in r3.stdout, f"--verificar depois de instalar dá 0 (saída {r3.returncode})")

            # --testar
            with OllamaFalso(FALSO_ESTRITO="1") as oll2:
                m.env["OLLAMA_HOST"] = oll2.url
                r4 = m.rodar("-y", "--testar")
                ok("modelo translategemma:12b respondeu" in r4.stdout and "modelo gemma3:12b respondeu" in r4.stdout,
                   "--testar traduz uma frase com cada modelo")
                ok(r4.returncode == 0, "--testar termina com sucesso")
            # --dev: com reportlab/playwright já visíveis (instalados no sistema) o pip fica satisfeito...
            m.env["OLLAMA_HOST"] = oll.url
            r5 = m.rodar("-y", "--dev")
            ok(r5.returncode == 0 and "reportlab e playwright instalados" in r5.stdout, "--dev instala as ferramentas de teste e segue")
            # ...e sem eles na "loja" (PIP_IGNORE_INSTALLED) o pip falha: o script só avisa, não quebra
            m.env["PIP_IGNORE_INSTALLED"] = "1"
            r6 = m.rodar("-y", "--dev")
            ok(r6.returncode == 0 and "Não consegui instalar as ferramentas de teste" in r6.stdout, "--dev sem acesso aos pacotes: só avisa, não quebra")
            m.env.pop("PIP_IGNORE_INSTALLED")


def cenario_modelos() -> None:
    titulo("D. Modelos: falhas, novas tentativas, pouco disco, sem permissão para instalar")
    base = ("--sem-venv", "--sem-ocr", "--modelo-ocr", "gemma3:12b")
    # D1 queda no meio do download → nova tentativa
    with Mundo("cai1") as m, OllamaFalso(modelos="", FALSO_PULL_MODO="cai1") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
        r = m.rodar("-y", *base)
        ok(r.returncode == 0 and "nova tentativa (1/3)" in r.stdout, "queda no meio do download → tenta de novo e conclui")
        ok(oll.pulls().count("translategemma:12b") == 2, f"o modelo de tradução foi pedido 2 vezes {oll.pulls()}")
    # D2 erro do Ollama: modelo de tradução é obrigatório (falha), o geral é opcional (aviso)
    with Mundo("erro") as m, OllamaFalso(modelos="", FALSO_PULL_MODO="erro") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
        r = m.rodar("-y", *base)
        ok(r.returncode == 1, "falha no download do modelo de tradução → saída 1")
        ok("erro do Ollama: falha simulada" in r.stdout, "mostra o erro devolvido pelo Ollama")
        ok("✗ não consegui baixar translategemma:12b" in r.stdout and "! não consegui baixar gemma3:12b" in r.stdout,
           "obrigatório vira ✗; opcional vira aviso")
        ok(oll.pulls().count("translategemma:12b") == 3, "desiste do modelo depois de 3 tentativas")
    # D3 modelo que não existe
    with Mundo("naoexiste") as m, OllamaFalso(modelos="gemma3:12b") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
        r = m.rodar("-y", "--modelo", "naoexiste:1b", *base)
        ok(r.returncode == 1 and "file does not exist" in r.stdout, "modelo inexistente: mostra o erro do Ollama e falha")
    # D4 só 2 GB livres → não baixa sob -y (pergunta de risco tem resposta padrão "não")
    with Mundo("disco") as m, OllamaFalso(modelos="") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
        m.ferramenta("df", DF_CHEIO)
        r = m.rodar("-y", *base)
        ok("Só 2 GB livres" in r.stdout and oll.pulls() == [], "pouco espaço em disco: avisa e NÃO baixa sob -y")
        ok(r.returncode == 1, "e o modelo obrigatório ausente conta como pendência")
    # D5 --sem-modelos / --sem-modelo-ocr
    with Mundo("sem") as m, OllamaFalso(modelos="translategemma:12b") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
        r = m.rodar("-y", "--sem-modelo-ocr", "--sem-venv", "--sem-ocr")
        ok(r.returncode == 0 and oll.pulls() == [] and "pulado (--sem-modelo-ocr)" in r.stdout, "--sem-modelo-ocr: não baixa o modelo geral")
        r = m.rodar("-y", "--sem-venv", "--sem-ocr")
        ok(r.returncode == 0 and oll.pulls() == [] and "pulado (OCR desligado)" in r.stdout, "--sem-ocr sozinho também dispensa o modelo geral")
        r = m.rodar("-y", "--sem-modelos", *base)
        ok(r.returncode == 0 and oll.pulls() == [] and "! modelo gemma3:12b" in r.stdout, "--sem-modelos: só confere (modelo opcional ausente = aviso)")
    # D6 sem terminal e sem -y: nunca baixa
    with Mundo("semtty") as m, OllamaFalso(modelos="") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
        r = m.rodar(*base)
        ok(oll.pulls() == [] and r.returncode == 1, "sem terminal e sem -y: não baixa modelo nenhum (e acusa a pendência)")
        ok("Sem terminal interativo e sem -y" in r.stdout and "ollama pull translategemma:12b" in r.stdout, "explica e mostra o comando manual")
    # D7 modelo escolhido é lembrado
    with Mundo("lembra") as m, OllamaFalso(modelos="gemma3:4b") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
        r = m.rodar("-y", "--modelo", "gemma3:4b", "--modelo-ocr", "gemma3:4b", "--sem-venv", "--sem-ocr")
        conf = (m.proj / ".setup.conf").read_text() if (m.proj / ".setup.conf").exists() else ""
        ok(r.returncode == 0 and "MODELO=gemma3:4b" in conf and "MODELO_OCR=gemma3:4b" in conf, "gravou os modelos escolhidos em .setup.conf")
        r2 = m.rodar("--verificar", "--sem-venv", "--sem-ocr")
        ok("modelo gemma3:4b (tradução)" in r2.stdout and r2.returncode == 0, "a execução seguinte reutiliza a escolha sem repetir as opções")


def cenario_ollama_ambiente() -> None:
    titulo("E. Ollama: remoto, inalcançável, parado (iniciar) e não instalado")
    ip = ip_da_maquina()
    # E1 remoto
    if ip:
        with Mundo("remoto") as m, OllamaFalso(bind="0.0.0.0") as oll:
            url = f"http://{ip}:{oll.porta}"
            r = m.rodar("-y", "--ollama", url, "--sem-venv", "--sem-ocr")
            ok(r.returncode == 0 and "outra máquina" in r.stdout, f"Ollama em outra máquina ({ip}) funciona e dispensa o app instalado")
            conf = (m.proj / ".setup.conf").read_text()
            ok(url in conf, "o endereço fica gravado em .setup.conf")
            escrever(m.proj / "pharos.py", "import sys\nprint('ARGS', sys.argv[1:])\n")
            a = subprocess.run([str(m.proj / "pharos.sh"), "livro.pdf"], capture_output=True, text=True, cwd=m.dir).stdout
            ok(f"'--ollama', '{url}'" in a, "./pharos.sh passa --ollama sozinho")
            b = subprocess.run([str(m.proj / "pharos.sh"), "livro.pdf", "--ollama", "http://x:1"], capture_output=True, text=True, cwd=m.dir).stdout
            ok(b.count("--ollama") == 1 and "http://x:1" in b, "e não duplica se o usuário já passou --ollama")
            r2 = m.rodar("--verificar", "--sem-venv", "--sem-ocr")
            ok("outra máquina" in r2.stdout and url in r2.stdout, "a verificação seguinte lembra o endereço remoto")
        # sem esquema nem porta
        with Mundo("semporta") as m:
            r = m.rodar("--verificar", "--ollama", ip, "--sem-venv", "--sem-ocr")
            ok(f"Servidor: http://{ip}:11434" in r.stdout, "endereço sem esquema/porta é normalizado (http://IP:11434)")
            ok("Não alcancei o Ollama" in r.stdout and r.returncode == 1, "servidor inalcançável: mensagem clara e saída 1")
    else:
        print("  (sem endereço fora do loopback: cenário do Ollama remoto pulado)")
    # E2 'localhost' e '0.0.0.0' contam como esta máquina
    with Mundo("local") as m, OllamaFalso() as oll:
        m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
        r = m.rodar("--verificar", "--ollama", f"0.0.0.0:{oll.porta}", "--sem-venv", "--sem-ocr")
        ok(f"Servidor: http://localhost:{oll.porta} (esta máquina)" in r.stdout, "0.0.0.0 é trocado por localhost")
    # E3 local, instalado, parado → o script inicia (fake `ollama serve`)
    with Mundo("parado") as m:
        porta = porta_livre()
        m.ferramenta("ollama", OLLAMA)
        m.env.update(OLLAMA_HOST=f"http://127.0.0.1:{porta}", FALSO_PORTA=str(porta))
        r = m.rodar("-y", "--sem-venv", "--sem-ocr")
        ok("Ollama iniciado em" in r.stdout and "ollama serve" in m.chamadas, "Ollama instalado mas parado: o script o inicia")
        ok(r.returncode == 0, f"e termina bem (os modelos já existem no falso) (saída {r.returncode})")
        ok((m.proj / ".ollama-serve.log").exists(), "log do servidor guardado em .ollama-serve.log")
    # E4 local, não instalado, Linux, sem permissão para o instalador oficial
    with Mundo("semollama") as m, OllamaFalso() as oll:
        m.env["OLLAMA_HOST"] = oll.url
        r = m.rodar("-y", "--sem-venv", "--sem-ocr")
        ok(r.returncode == 1 and "Ollama ausente" in r.stdout, "Ollama não instalado no Linux: falha com instruções")
        ok("curl -fsSL https://ollama.com/install.sh" in r.stdout and "ollama.com/install.sh | sh" not in m.chamadas, "NÃO roda o instalador remoto sem --instalar-ollama")
    # E5 sem Ollama nenhum respondendo
    with Mundo("morto") as m:
        porta = porta_livre()
        m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
        m.env["OLLAMA_HOST"] = f"http://127.0.0.1:{porta}"
        r = m.rodar("--verificar", "--sem-venv", "--sem-ocr")
        ok(r.returncode == 1 and "Ollama não está respondendo" in r.stdout, "--verificar com Ollama parado: acusa sem tentar iniciar")
        ok("ollama serve" not in m.chamadas, "e não iniciou nada")


def cenario_macos(rapido: bool) -> None:
    titulo("F. macOS simulado (uname=Darwin, brew, sysctl, open): caminho do Homebrew")
    tess = reais_de(["tesseract"])
    with Mundo("mac") as m:
        porta = porta_livre()
        rotas = {"/tessdata_best/raw/main/deu_latf.traineddata": dados("deu_latf")}
        with GitHubFalso(rotas) as gh:
            for nome, conteudo in (("uname", UNAME_MAC), ("brew", BREW), ("sysctl", SYSCTL),
                                   ("sw_vers", "#!/bin/bash\necho 15.0\n")):
                m.ferramenta(nome, conteudo)
            m.escondidos = tess
            m.env.update(OLLAMA_HOST=f"http://127.0.0.1:{porta}", FALSO_PORTA=str(porta), TESSDATA_URL_BASE=gh.url, MEM_BYTES="8589934592")
            r = m.rodar("-y", "--sem-venv")
            s, ch = r.stdout, m.chamadas
            if r.returncode != 0:
                print(s[-2500:], r.stderr[-800:])
            ok("macOS 15.0 (arm64)" in s and "Gerenciador de pacotes: brew" in s, "reconhece macOS e Homebrew")
            ok("8 GB de RAM" in s and "modelos de 12B pedem" in s, "avisa que 8 GB de RAM é pouco para modelos 12B")
            ok("brew install tesseract tesseract-lang" in ch, "instala o Tesseract e todos os idiomas pelo brew")
            ok("brew install ollama" in ch, "instala o Ollama pelo brew")
            ok("brew services start ollama" in ch, "inicia o Ollama pelo brew services")
            ok("Ollama iniciado" in s, "e espera ele responder")
            ok("deu_latf" in m.tessdata() and "/tessdata_best/raw/main/deu_latf.traineddata" in gh.acessos, "deu_latf (que o brew não traz) vem do repositório oficial")
            ok(r.returncode == 0 and "Ambiente pronto" in s, f"macOS simulado termina com sucesso (saída {r.returncode})")
            for n in ("brew services start ollama",):
                pass
    # app Ollama.app presente → open -a Ollama
    app = Path("/Applications/Ollama.app")
    criou = False
    if os.geteuid() == 0 and not Path("/Applications").exists():
        app.mkdir(parents=True)
        criou = True
    try:
        if app.exists():
            with Mundo("macapp") as m:
                porta = porta_livre()
                for nome, conteudo in (("uname", UNAME_MAC), ("sysctl", SYSCTL), ("sw_vers", "#!/bin/bash\necho 15.0\n"),
                                       ("open", OPEN), ("ollama", OLLAMA)):
                    m.ferramenta(nome, conteudo)
                m.env.update(OLLAMA_HOST=f"http://127.0.0.1:{porta}", FALSO_PORTA=str(porta))
                r = m.rodar("-y", "--sem-venv", "--sem-ocr")
                ok("open -a Ollama" in m.chamadas and "Ollama iniciado" in r.stdout, "com o app Ollama.app instalado, abre o app (open -a Ollama)")
        else:
            print("  (não foi possível simular /Applications/Ollama.app: cenário pulado)")
    finally:
        if criou:
            shutil.rmtree("/Applications", ignore_errors=True)


def cenario_python() -> None:
    titulo("G. Python: ausente, antigo, instalado pelo apt")
    with Mundo("pyruim") as m:
        r = m.rodar("--verificar", "--python", "/nao/existe/python")
        ok(r.returncode == 1 and "não existe ou é anterior" in r.stdout, "--python inexistente → falha clara")
        m.ferramenta("pyvelho", "#!/bin/bash\nexit 1\n")
        r = m.rodar("--verificar", "--python", "pyvelho")
        ok(r.returncode == 1 and "anterior ao Python 3.10" in r.stdout, "Python antigo é recusado")
    with Mundo("pyatalho") as m:
        for nome, conteudo in (("uname", UNAME_MAC), ("sysctl", SYSCTL), ("sw_vers", "#!/bin/bash\necho 15.0\n"), ("xcode-select", "#!/bin/bash\nexit 2\n")):
            m.ferramenta(nome, conteudo)
        r = m.rodar("--verificar", "--python", "/usr/bin/python3")
        ok(r.returncode == 1 and "só um atalho" in r.stdout, "macOS sem Ferramentas de Linha de Comando: não executa o /usr/bin/python3 (atalho do Xcode)")
    py_reais = reais_de(["python3", "python", "python3.11", "python3.12", "python3.13", "python3.10", "python3.14"])
    ok(bool(py_reais), "o Python do sistema será escondido do script")
    with Mundo("semypython") as m, OllamaFalso() as oll:
        shutil.copy(sys.executable, m.dir / "reserva" / "python3.13")
        criar_rodas(m.dir / "rodas")
        m.ferramenta("apt-get", APT)
        m.ferramenta("sudo", SUDO)
        m.ferramenta("id", ID_NAO_ROOT)
        m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
        m.escondidos = py_reais
        m.env["OLLAMA_HOST"] = oll.url
        r = m.rodar("-y", "--sem-ocr", "--sem-modelos")
        s = r.stdout
        if r.returncode != 0:
            print(s[-2500:], r.stderr[-800:])
        ok("Python 3.10+ não encontrado" in s, "percebe que não há Python utilizável")
        ok("sudo apt-get install -y python3 python3-venv python3-pip" in m.chamadas, "instala python3 + venv + pip pelo apt")
        ok((m.proj / ".venv" / "bin" / "python").exists() and "requirements.txt instalado" in s, "e prossegue: cria o .venv e instala as bibliotecas")
        ok(r.returncode == 0, f"termina com sucesso (saída {r.returncode})")
    with Mundo("semypython2") as m:
        m.escondidos = py_reais
        r = m.rodar("--verificar")
        ok(r.returncode == 1 and "Python 3.10+ ausente" in r.stdout, "--verificar sem Python: falha e mostra como instalar")
        ok(m.chamadas == "", "sem instalar nada")


def cenario_tesseract() -> None:
    titulo("H. Tesseract: idiomas faltando (apt), sem rede, sem Tesseract nenhum, --idiomas extra, --sem-ocr")
    tess = reais_de(["tesseract"])
    # H1 tesseract presente, só eng+osd, idiomas via download direto (apt não existe aqui)
    with Mundo("idiomas") as m:
        escrever(m.dir / "pacotes" / "x", "")
        m.ferramenta("tesseract", TESSERACT_FALSO)
        for l in ("eng", "osd"):
            (m.dir / "tessdata" / f"{l}.traineddata").write_bytes(dados(l))
        rotas = {f"/tessdata_fast/raw/main/{l}.traineddata": dados(l) for l in ("deu", "spa", "por", "fra")}
        rotas["/tessdata_best/raw/main/frk.traineddata"] = dados("frk")
        with GitHubFalso(rotas) as gh:
            m.env["TESSDATA_URL_BASE"] = gh.url
            m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
            with OllamaFalso() as oll:
                m.env["OLLAMA_HOST"] = oll.url
                r = m.rodar("-y", "--sem-venv", "--idiomas", "fra,ita")
                s = r.stdout
                ok(m.tessdata() == ["deu", "eng", "fra", "frk", "osd", "por", "spa"], f"baixou deu spa por fra (fast) e frk (best): {m.tessdata()}")
                ok("idioma ita ausente" in s and "Não consegui baixar ita" in s, "idioma que não existe na fonte: só avisa")
                ok("Não consegui baixar deu_latf" in s and "letra gótica (Fraktur): frk" in s, "Fraktur parcial (frk sim, deu_latf não) é reportado")
                ok(r.returncode == 0, f"avisos não impedem a saída 0 (saída {r.returncode})")
                ok("/tessdata_fast/raw/main/deu.traineddata" in gh.acessos and "/tessdata_best/raw/main/frk.traineddata" in gh.acessos,
                   "idiomas comuns vêm do tessdata_fast; Fraktur do tessdata_best")
        # download corrompido (arquivo minúsculo / HTML de erro) é rejeitado
        with Mundo("corrompido") as m2:
            m2.ferramenta("tesseract", TESSERACT_FALSO)
            for l in ("eng", "osd", "deu", "spa", "por"):
                (m2.dir / "tessdata" / f"{l}.traineddata").write_bytes(dados(l))
            with GitHubFalso({"/tessdata_best/raw/main/frk.traineddata": b"<html>Not found</html>"}) as gh2, OllamaFalso() as oll:
                m2.env.update(TESSDATA_URL_BASE=gh2.url, OLLAMA_HOST=oll.url)
                m2.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
                m2.rodar("-y", "--sem-venv")
                ok("frk" not in m2.tessdata(), "resposta pequena demais (página de erro) não é gravada como idioma")
    # H2 pasta do tessdata sem permissão de escrita + usuário não root → usa sudo para copiar
    with Mundo("sudocp") as m:
        m.ferramenta("tesseract", TESSERACT_FALSO)
        m.ferramenta("sudo", SUDO)
        m.ferramenta("id", ID_NAO_ROOT)
        for l in ("eng", "osd", "deu", "spa", "por"):
            (m.dir / "tessdata" / f"{l}.traineddata").write_bytes(dados(l))
        os.chmod(m.dir / "tessdata", 0o555)
        with GitHubFalso({"/tessdata_best/raw/main/frk.traineddata": dados("frk")}) as gh, OllamaFalso() as oll:
            m.env.update(TESSDATA_URL_BASE=gh.url, OLLAMA_HOST=oll.url)
            m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
            r = m.rodar("-y", "--sem-venv")
            # root consegue escrever mesmo em 555; o que vale é o caminho do script ter funcionado
            ok("frk" in m.tessdata(), "idioma gótico gravado na pasta do Tesseract")
        os.chmod(m.dir / "tessdata", 0o755)
    # H3 sem Tesseract e sem gerenciador de pacotes → falha com instruções
    with Mundo("nenhum") as m, OllamaFalso() as oll:
        m.escondidos = tess
        m.env["OLLAMA_HOST"] = oll.url
        m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
        m.ferramenta("apt-get", "#!/bin/bash\nexit 1\n")   # presente, mas nada instala
        r = m.rodar("-y", "--sem-venv")
        ok(r.returncode == 1 and "Tesseract ausente" in r.stdout and "tesseract-ocr-deu" in r.stdout, "sem conseguir instalar o Tesseract: falha com o comando certo")
        ok("O programa enxerga" not in r.stdout, "e não finge que a conferência final passou")
    # H4 --sem-ocr
    with Mundo("semocr") as m, OllamaFalso() as oll:
        m.escondidos = tess
        m.env["OLLAMA_HOST"] = oll.url
        m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
        r = m.rodar("-y", "--sem-venv", "--sem-ocr")
        ok(r.returncode == 0 and "Pulado (--sem-ocr)" in r.stdout and "Modelo geral para sugestões de OCR: pulado (OCR desligado)" in r.stdout,
           "--sem-ocr não exige Tesseract nem o modelo das sugestões")
    # H5 Tesseract velho
    with Mundo("velho") as m, OllamaFalso() as oll:
        m.ferramenta("tesseract", TESSERACT_FALSO.replace("tesseract 5.3.0", "tesseract 3.04.01"))
        m.env["OLLAMA_HOST"] = oll.url
        m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
        r = m.rodar("--verificar", "--sem-venv")
        ok("antigo demais" in r.stdout, "Tesseract 3.x é recusado (sem LSTM)")


def cenario_interativo() -> None:
    titulo("I. Terminal interativo (pty): perguntas, respostas s/n e barra de progresso")
    import pty
    with Mundo("pty") as m, OllamaFalso(modelos="", FALSO_PULL_MODO="lento") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        env.update(PATH=f"{m.bin}:{env['PATH']}", MUNDO=str(m.dir), HOME=str(m.home), TMPDIR=str(m.tmp), NO_COLOR="1",
                   OLLAMA_HOST=oll.url, TERM="xterm", TESSERACT_CMD="")
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(m.proj)
            os.execvpe("bash", ["bash", "setup.sh", "--sem-venv", "--sem-ocr", "--modelo-ocr", "gemma3:12b"], env)
        respostas = ["n", "s"]          # não baixa o modelo de tradução; baixa o geral
        buf = b""
        perguntas = 0
        fim = time.time() + 90
        while time.time() < fim:
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
            if (buf.rstrip().endswith(b"[S/n]") or buf.rstrip().endswith(b"[s/N]")) and perguntas < len(respostas) \
                    and buf.count(b"[S/n]") + buf.count(b"[s/N]") > perguntas:
                os.write(fd, (respostas[perguntas] + "\n").encode())
                perguntas += 1
        _, status = os.waitpid(pid, 0)
        texto = buf.decode("utf-8", "replace")
        codigo = os.waitstatus_to_exitcode(status)
        ok(perguntas == 2, f"fez as 2 perguntas de download (modelo de tradução e modelo geral): {perguntas}")
        ok(oll.pulls() == ["gemma3:12b"], f"respondendo n/s baixou só o segundo modelo {oll.pulls()}")
        ok("\r" in texto and "100.0%" in texto, "com terminal, a barra de progresso se atualiza na mesma linha (\\r)")
        ok(codigo == 1 and "não baixado" in texto, f"modelo obrigatório recusado → pendência e saída 1 (saída {codigo})")


def cenario_ctrl_c() -> None:
    titulo("J. Ctrl+C durante o download")
    with Mundo("ctrlc") as m, OllamaFalso(modelos="", FALSO_PULL_MODO="lento") as oll:
        m.env["OLLAMA_HOST"] = oll.url
        m.ferramenta("ollama", "#!/bin/bash\nexit 0\n")
        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        env.update(PATH=f"{m.bin}:{env['PATH']}", MUNDO=str(m.dir), HOME=str(m.home), TMPDIR=str(m.tmp), NO_COLOR="1", OLLAMA_HOST=oll.url, TESSERACT_CMD="")
        p = subprocess.Popen(["bash", "setup.sh", "-y", "--sem-venv", "--sem-ocr"], cwd=m.proj, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True)
        t0 = time.time()
        while time.time() - t0 < 20 and not oll.pulls():
            time.sleep(0.1)
        time.sleep(0.8)
        os.killpg(p.pid, signal.SIGINT)
        try:
            saida, _ = p.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
            saida, _ = p.communicate()
        ok(p.returncode == 130, f"Ctrl+C encerra com 130 (saiu {p.returncode})")
        ok("Interrompido" in saida and "Traceback" not in saida, "mostra uma mensagem curta, sem traceback")
        ok(len(oll.pulls()) == 1, "não segue baixando o próximo modelo depois de interrompido")


def varredura_bash32() -> None:
    titulo("K. Sintaxe e compatibilidade com o bash 3.2 do macOS (varredura estática)")
    r = subprocess.run(["bash", "-n", str(SETUP)], capture_output=True, text=True)
    ok(r.returncode == 0, f"bash -n: {r.stderr.strip() or 'sintaxe ok'}")
    texto = SETUP.read_text()
    # tira os trechos de Python (heredocs <<'PY'), que não são bash
    sem_py = re.sub(r"<<'PY' \|\| true\n.*?\nPY\n", "\n", texto, flags=re.S)
    sem_coment = "\n".join(l for l in sem_py.splitlines() if not l.lstrip().startswith("#"))
    proibidos = {
        r"\bdeclare\s+-[a-zA-Z]*A": "arrays associativos (bash 4)",
        r"\bmapfile\b|\breadarray\b": "mapfile/readarray (bash 4)",
        r"\$\{[A-Za-z_][A-Za-z_0-9]*(,,|\^\^|,|\^)\}": "conversão de caixa ${v,,} ${v^^} (bash 4)",
        r"&>>": "&>> (bash 4)",
        r"\|&": "|& (bash 4)",
        r"\bcoproc\b": "coproc (bash 4)",
        r"\bwait\s+-n\b": "wait -n (bash 4.3)",
        r"\blocal\s+-n\b|\bdeclare\s+-n\b": "namerefs (bash 4.3)",
        r";;&|;&": "case com ;;& ou ;& (bash 4)",
        r"read\s+(-[a-zA-Z]+\s+)*-t\s+0?\.\d": "read -t fracionário (bash 4)",
        r"read\s+(-[a-zA-Z]+\s+)*-N": "read -N (bash 4)",
        r"\bsudo\s+-n": "sudo -n silencioso (comportamento diferente no macOS)",
        r"\bgrep\s+(-[a-zA-Z]*)?P": "grep -P (não existe no grep do macOS)",
        r"\bsed\s+(-[a-zA-Z]*)?[ri]\b.*\\[+?|]": "sed -r / \\+ (diferente no BSD)",
        r"\breadlink\s+-f": "readlink -f (não existe no macOS antigo)",
        r"\bdate\s+-d": "date -d (GNU)",
        r"\bstat\s+-c": "stat -c (GNU)",
        r"\bxargs\s+-r": "xargs -r (GNU)",
    }
    for padrao, nome in proibidos.items():
        achou = [l.strip() for l in sem_coment.splitlines() if re.search(padrao, l)]
        ok(not achou, f"sem {nome}" + (f" → {achou[:2]}" if achou else ""))
    # "$@"/"${arr[@]}" vazio com `set -u` quebra no bash < 4.4: o script não deve usar arrays
    ok(not re.search(r"\w+=\(", sem_coment) and "${#" not in sem_coment.replace("${#}", ""), "sem arrays (evita 'unbound variable' do bash 3.2 com set -u)")
    ok("set -u -o pipefail" in texto and "set -e" not in texto.replace("set -u -o pipefail", ""), "set -u -o pipefail, sem set -e (cada passo trata o seu erro)")
    ok(os.access(SETUP, os.X_OK), "setup.sh é executável")
    ok(texto.startswith("#!/usr/bin/env bash"), "shebang portátil (#!/usr/bin/env bash)")
    com_sudo = [l for l in sem_coment.splitlines() if re.search(r"\bsudo\b", l)]
    execucao = [l for l in com_sudo if re.search(r'\bsudo\s+"\$@"', l)]
    so_texto = [l for l in com_sudo if l not in execucao and not re.search(r"\b(info|falha|aviso|confirmar|echo|printf)\b", l)]
    ok(len(execucao) == 1 and not so_texto, f"o único sudo executável é o de como_root (as demais citações são texto de ajuda) {so_texto[:2]}")


def tessdata_real() -> dict[str, int]:
    """Estado da pasta de idiomas do Tesseract DESTA máquina: nenhum cenário pode mexer nela."""
    r = subprocess.run([shutil.which("tesseract") or "tesseract", "--list-langs"], capture_output=True, text=True)
    m = re.search(r'in "(.*?)/?"', r.stdout + r.stderr)
    pasta = Path(m.group(1)) if m else None
    return {f.name: f.stat().st_size for f in pasta.glob("*.traineddata")} if pasta and pasta.is_dir() else {}


def main() -> int:
    rapido = "--rapido" in sys.argv
    tessdata_antes = tessdata_real()
    if not shutil.which("unshare"):
        print("(sem `unshare`: cenários que escondem o Tesseract/Python serão pulados)")
    t0 = time.time()
    cenario_argumentos()
    cenario_verificar()
    cenario_modelos()
    cenario_ollama_ambiente()
    cenario_tesseract()
    cenario_macos(rapido)
    if not rapido:
        cenario_instalacao_linux(rapido)
        cenario_python()
    cenario_interativo()
    cenario_ctrl_c()
    varredura_bash32()
    titulo("L. Segurança do próprio teste")
    ok(tessdata_real() == tessdata_antes, "a pasta de idiomas do Tesseract desta máquina ficou intacta")
    for d in _SYSBINS.values():
        shutil.rmtree(d, ignore_errors=True)
    print(f"\n{time.time() - t0:.0f} s")
    print("TUDO CERTO" if falhas == 0 else f"{falhas} FALHA(S)")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
