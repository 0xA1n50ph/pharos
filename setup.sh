#!/usr/bin/env bash
# =====================================================================================================
#  setup.sh — verifica e prepara o ambiente do Pharos — tradutor de livros (macOS e Linux)
#
#  Faz, nesta ordem (cada passo só mexe no que falta, então pode rodar quantas vezes quiser):
#    1. Sistema      SO, memória, espaço em disco, gerenciador de pacotes
#    2. Python       3.10+ (instala pelo brew/apt/dnf/pacman, se você deixar) e o ambiente virtual .venv
#    3. Bibliotecas  requirements.txt dentro do .venv, conferidas por importação
#    4. OCR          Tesseract + idiomas (eng osd deu spa por) + letra gótica (frk, deu_latf)
#    5. Ollama       instalado, no ar (local ou em outra máquina) e com os modelos gemma baixados
#    6. Opcionais    Calibre (MOBI/AZW3/...), ferramentas de teste
#    7. Conferência  usa o próprio programa para checar que tudo se enxerga, e cria o atalho ./pharos.sh
#
#  Nada que exige senha de administrador (sudo) roda sem a sua confirmação. Sem terminal interativo e
#  sem -y, o script só verifica e mostra o comando para rodar à mão.
#
#  Uso:  ./setup.sh [opções]      (veja ./setup.sh --ajuda)
#  Compatível com o bash 3.2 que vem no macOS.
# =====================================================================================================
set -u -o pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$RAIZ/.venv"
CONF="$RAIZ/.setup.conf"
PY_MIN_MAJOR=3
PY_MIN_MINOR=10

MODELO_TRAD="translategemma:12b"
MODELO_GERAL="gemma3:12b"
IDIOMAS_BASE="eng osd deu spa por"
IDIOMAS_GOTICOS="frk deu_latf"
IDIOMAS_EXTRA=""
TESSDATA_URL_BASE="${TESSDATA_URL_BASE:-https://github.com/tesseract-ocr}"
GB_POR_MODELO=9

VERIFICAR=0; SIM=0; SEM_MODELOS=0; SEM_MODELO_GERAL=0; SEM_OCR=0; SEM_VENV=0; DEV=0; CALIBRE=0
TESTAR=0; INSTALAR_OLLAMA=0; PYTHON_ARG=""; MODELO_TRAD_EXPLICITO=0; MODELO_GERAL_EXPLICITO=0
OLLAMA_URL_ARG=""

FALHAS=0; AVISOS=0; RESUMO_FALHAS=""; RESUMO_AVISOS=""
PM=""; SO="$(uname -s)"; ARQ="$(uname -m)"
PY_ATALHO=0; PY=""; VPY=""; PYANY=""; TESS=""; TESSDATA_DIR=""; MODELOS_INST=""; OLLAMA_URL=""; OLLAMA_LOCAL=0
LOG="$(mktemp "${TMPDIR:-/tmp}/pharos-setup.XXXXXX")"
trap 'rm -f "$LOG"' EXIT
trap 'printf "\n  Interrompido. Rode ./setup.sh de novo para continuar de onde parou (downloads de modelos são retomados).\n"; exit 130' INT TERM

# ---------------------------------------------------------------------------------------- saída
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_OK=$'\033[32m'; C_AV=$'\033[33m'; C_ER=$'\033[31m'; C_B=$'\033[1m'; C_D=$'\033[2m'; C_0=$'\033[0m'
else
  C_OK=""; C_AV=""; C_ER=""; C_B=""; C_D=""; C_0=""
fi
passo()  { printf '\n%s%s%s\n' "$C_B" "$*" "$C_0"; }
ok()     { printf '  %s✓%s %s\n' "$C_OK" "$C_0" "$*"; }
info()   { printf '    %s\n' "$*"; }
aviso()  { printf '  %s!%s %s\n' "$C_AV" "$C_0" "$*"; AVISOS=$((AVISOS + 1)); RESUMO_AVISOS="$RESUMO_AVISOS  ! $*"$'\n'; }
nota()   { printf '  %s!%s %s\n' "$C_AV" "$C_0" "$*"; }   # como aviso, mas não entra no resumo (é só o aviso de que vou tentar resolver)
falha()  { printf '  %s✗%s %s\n' "$C_ER" "$C_0" "$*"; FALHAS=$((FALHAS + 1)); RESUMO_FALHAS="$RESUMO_FALHAS  ✗ $*"$'\n'; }
rodar()  { printf '    %s$ %s%s\n' "$C_D" "$*" "$C_0"; "$@"; }

uso() {
  cat <<'FIM'
Uso: ./setup.sh [opções]

Verifica e prepara o ambiente: Python + bibliotecas (.venv), Tesseract e idiomas, Ollama e modelos gemma.

  --verificar            só confere e mostra o que falta; não instala nem altera nada
  -y, --sim              responde "sim" a tudo (não pergunta)
  --ollama URL           Ollama em outra máquina (ex.: http://pi.local:11434). Padrão: $OLLAMA_HOST ou localhost.
                         Fica gravado em .setup.conf e o atalho ./pharos.sh passa a usá-lo
  --modelo NOME          modelo de tradução (padrão: translategemma:12b)
  --modelo-ocr NOME      modelo geral para as sugestões de correção do OCR (padrão: gemma3:12b)
  --sem-modelo-ocr       não baixa o modelo geral das sugestões de OCR
  --sem-modelos          não baixa nenhum modelo (só confere quais existem)
  --sem-ocr              não confere nem instala o Tesseract
  --idiomas LISTA        idiomas extras do OCR, separados por vírgula (ex.: fra,ita,lat). Já vêm: eng osd deu spa por
  --python CMD           interpretador Python a usar (padrão: o primeiro 3.10+ que achar)
  --sem-venv             usa o Python escolhido direto, sem criar o .venv (não recomendado)
  --dev                  também instala o necessário para os testes (reportlab, playwright + chromium)
  --calibre              também instala o Calibre (lê MOBI/AZW3/FB2/DOCX)
  --instalar-ollama      (Linux) aceita rodar o instalador oficial do Ollama (https://ollama.com/install.sh)
  --testar               faz uma tradução curta com cada modelo para ver se carregam (leva alguns minutos)
  -h, --ajuda            mostra esta ajuda

Exemplos:
  ./setup.sh                                  # prepara tudo, perguntando antes de instalar
  ./setup.sh --verificar                      # só diagnostica
  ./setup.sh -y --ollama http://pi.local:11434 --sem-modelo-ocr
Código de saída: 0 = ambiente pronto; 1 = falta algo obrigatório.
FIM
}

# ---------------------------------------------------------------------------------------- argumentos
precisa_valor() { [ $# -ge 2 ] && [ -n "$2" ] || { echo "A opção $1 precisa de um valor. Veja: ./setup.sh --ajuda" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
  case "$1" in
    --verificar|--check) VERIFICAR=1 ;;
    -y|--sim|--yes) SIM=1 ;;
    --ollama) precisa_valor "$@"; OLLAMA_URL_ARG="$2"; shift ;;
    --modelo) precisa_valor "$@"; MODELO_TRAD="$2"; MODELO_TRAD_EXPLICITO=1; shift ;;
    --modelo-ocr) precisa_valor "$@"; MODELO_GERAL="$2"; MODELO_GERAL_EXPLICITO=1; shift ;;
    --sem-modelo-ocr) SEM_MODELO_GERAL=1 ;;
    --sem-modelos) SEM_MODELOS=1 ;;
    --sem-ocr) SEM_OCR=1 ;;
    --idiomas) precisa_valor "$@"; IDIOMAS_EXTRA="$(printf '%s' "$2" | tr ',' ' ')"
               case "$IDIOMAS_EXTRA" in *[!a-zA-Z_\ ]*) echo "--idiomas: use códigos do Tesseract separados por vírgula (ex.: fra,ita,lat)" >&2; exit 2 ;; esac
               shift ;;
    --python) precisa_valor "$@"; PYTHON_ARG="$2"; shift ;;
    --sem-venv) SEM_VENV=1 ;;
    --dev) DEV=1 ;;
    --calibre) CALIBRE=1 ;;
    --instalar-ollama) INSTALAR_OLLAMA=1 ;;
    --testar) TESTAR=1 ;;
    -h|--ajuda|--help) uso; exit 0 ;;
    *) echo "Opção desconhecida: $1" >&2; echo "Veja: ./setup.sh --ajuda" >&2; exit 2 ;;
  esac
  shift
done


# ---------------------------------------------------------------------------------------- trechos em Python
# (guardados em variáveis para não ficarem dentro de $(...): o bash 3.2 do macOS tropeça em heredoc ali dentro)
IFS= read -r -d '' PY_IMPORTS <<'PY' || true
import importlib, importlib.metadata as md
for modulo, dist in (("PIL", "pillow"), ("numpy", "numpy"), ("pypdfium2", "pypdfium2"), ("pdfplumber", "pdfplumber"),
                     ("bs4", "beautifulsoup4"), ("docx", "python-docx")):
    try:
        importlib.import_module(modulo)
        try:
            v = md.version(dist)
        except Exception:
            v = "?"
        print("ok|%s|%s" % (dist, v))
    except Exception as e:
        print("falta|%s|%s" % (dist, type(e).__name__))
PY

IFS= read -r -d '' PY_PULL <<'PY' || true
import json, sys, urllib.request
url, modelo = sys.argv[1], sys.argv[2]
req = urllib.request.Request(url + "/api/pull", data=json.dumps({"model": modelo, "name": modelo, "stream": True}).encode(),
                             headers={"Content-Type": "application/json"})
tty = sys.stdout.isatty()
ultimo, proximo, sucesso = "", {}, False
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        for linha in r:
            linha = linha.strip()
            if not linha:
                continue
            d = json.loads(linha)
            if "error" in d:
                print("\n    erro do Ollama: %s" % d["error"])
                sys.exit(1)
            st = d.get("status", "")
            tot, feito = d.get("total"), d.get("completed")
            if tot and feito is not None:
                pct = 100.0 * feito / tot
                msg = "    %-26s %5.1f%%  %.1f/%.1f GB" % (st[:26], pct, feito / 1e9, tot / 1e9)
                if not tty:
                    if pct < proximo.get(st, 0):
                        continue
                    proximo[st] = (int(pct) // 10 + 1) * 10
            else:
                msg = "    " + st[:70]
            if msg != ultimo:
                if tty:
                    sys.stdout.write("\r" + msg.ljust(80))
                else:
                    sys.stdout.write(msg + "\n")
                sys.stdout.flush()
                ultimo = msg
            if st == "success":
                sucesso = True
except KeyboardInterrupt:
    print("\n    interrompido")
    sys.exit(130)
except Exception as e:
    print("\n    falha na conexão: %s" % e)
    sys.exit(1)
if tty:
    print()
sys.exit(0 if sucesso else 1)
PY

IFS= read -r -d '' PY_TESTE <<'PY' || true
import json, sys, time, urllib.request
url, modelo = sys.argv[1], sys.argv[2]
def post(caminho, corpo, timeout=900):
    req = urllib.request.Request(url + caminho, data=json.dumps(corpo).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)
t0 = time.time()
try:
    r = post("/api/chat", {"model": modelo, "stream": False, "options": {"num_predict": 60, "temperature": 0},
                           "messages": [{"role": "user", "content": "Translate from German to Portuguese. Reply with the translation only: Der Leuchtturm stand allein am Rand der Klippe."}]})
    txt = (r.get("message", {}).get("content") or "").strip().replace("\n", " ")
    if not txt:
        print("    resposta vazia")
        sys.exit(1)
    print("    %.0f s · «%s»" % (time.time() - t0, txt[:90]))
    post("/api/generate", {"model": modelo, "keep_alive": 0}, 30)
except Exception as e:
    print("    falhou: %s" % e)
    sys.exit(1)
PY

IFS= read -r -d '' PY_OCR <<'PY' || true
import sys
sys.path.insert(0, sys.argv[1])
try:
    from pharos.ocr import tesseract
    exe = tesseract.localizar()
    inst = sorted(tesseract.idiomas_instalados(exe))
    print("ok|" + ", ".join(i for i in inst if i != "osd"))
except Exception as e:
    print("erro|" + (str(e).splitlines() or ["?"])[0])
PY

# ---------------------------------------------------------------------------------------- configuração salva
conf_valor() { [ -f "$CONF" ] || return 0; ( . "$CONF" 2>/dev/null; eval "printf '%s' \"\${$1:-}\"" ); }
SALVO_OLLAMA="$(conf_valor OLLAMA_URL)"; SALVO_MODELO="$(conf_valor MODELO)"; SALVO_MODELO_OCR="$(conf_valor MODELO_OCR)"
[ -z "$OLLAMA_URL_ARG" ] && [ -n "$SALVO_OLLAMA" ] && OLLAMA_URL_ARG="$SALVO_OLLAMA"
if [ "$MODELO_TRAD_EXPLICITO" != 1 ] && [ -n "$SALVO_MODELO" ]; then MODELO_TRAD="$SALVO_MODELO"; MODELO_TRAD_EXPLICITO=1; fi
if [ "$MODELO_GERAL_EXPLICITO" != 1 ] && [ -n "$SALVO_MODELO_OCR" ]; then MODELO_GERAL="$SALVO_MODELO_OCR"; MODELO_GERAL_EXPLICITO=1; fi

# ---------------------------------------------------------------------------------------- utilitários
tem() { command -v "$1" >/dev/null 2>&1; }

confirmar() {  # confirmar "pergunta" [s|n = resposta padrão]
  local pergunta="$1" padrao="${2:-s}" r
  if [ "$SIM" = 1 ]; then [ "$padrao" = s ]; return; fi   # -y aceita o que é de rotina; o que é de risco (padrão "n") fica de fora
  [ -t 0 ] || return 1                       # sem terminal e sem -y: nunca instala sozinho
  if [ "$padrao" = s ]; then printf '  ? %s [S/n] ' "$pergunta"; else printf '  ? %s [s/N] ' "$pergunta"; fi
  read -r r || return 1
  case "$r" in
    "") [ "$padrao" = s ] ;;
    [sSyY]*) return 0 ;;
    *) return 1 ;;
  esac
}

como_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"
  elif tem sudo; then sudo "$@"
  else echo "    (precisa de administrador e o sudo não existe)" >&2; return 1
  fi
}

# Pergunta e instala com o gerenciador de pacotes. $1 = descrição; demais = comando (já pronto para rodar).
instalar_com() {
  local descricao="$1"; shift
  if [ "$VERIFICAR" = 1 ]; then return 1; fi
  confirmar "Instalar $descricao agora?" s || { info "Para instalar à mão:  $*"; return 1; }
  rodar "$@"
}

apt_instalar() {  # apt_instalar "descrição" pacote...   (com 'apt-get update' e nova tentativa se o índice estiver velho)
  local d="$1"; shift
  [ "$VERIFICAR" = 1 ] && return 1
  confirmar "Instalar $d agora?" s || { info "Para instalar à mão:  sudo apt-get update && sudo apt-get install -y $*"; return 1; }
  rodar como_root apt-get install -y "$@" || { rodar como_root apt-get update -qq && rodar como_root apt-get install -y "$@"; }
}

livre_gb() {  # espaço livre (GB) no volume do diretório $1 (ou do primeiro pai que existir)
  local d="$1"
  while [ ! -d "$d" ] && [ "$d" != "/" ]; do d="$(dirname "$d")"; done
  df -Pk "$d" 2>/dev/null | awk 'NR==2 {printf "%d", $4/1048576}'
}

ram_gb() {
  if [ "$SO" = Darwin ]; then echo $(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1073741824 ))
  else awk '/MemTotal/ {printf "%d", $2/1048576}' /proc/meminfo 2>/dev/null || echo 0
  fi
}

versao_inteira() { local v="${1%%.*}"; case "$v" in ''|*[!0-9]*) echo 0 ;; *) echo "$v" ;; esac; }

# ---------------------------------------------------------------------------------------- 1. sistema
detectar_pm() {
  if tem brew; then PM=brew
  elif [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; PM=brew
  elif [ -x /usr/local/bin/brew ]; then eval "$(/usr/local/bin/brew shellenv)"; PM=brew
  elif tem apt-get; then PM=apt
  elif tem dnf; then PM=dnf
  elif tem pacman; then PM=pacman
  elif tem zypper; then PM=zypper
  else PM=""
  fi
}

passo_sistema() {
  passo "1. Sistema"
  case "$SO" in
    Darwin) ok "macOS $(sw_vers -productVersion 2>/dev/null || true) ($ARQ)" ;;
    Linux)  ok "Linux ($ARQ)" ;;
    *)      aviso "Sistema '$SO' não testado; o script segue como se fosse Linux." ;;
  esac
  detectar_pm
  if [ -n "$PM" ]; then ok "Gerenciador de pacotes: $PM"
  elif [ "$SO" = Darwin ]; then
    aviso "Homebrew não encontrado. Instale em https://brew.sh e rode este script de novo (ele facilita tudo no macOS)."
  else
    aviso "Nenhum gerenciador de pacotes conhecido (apt, dnf, pacman, zypper): instale as dependências à mão."
  fi
  tem curl && ok "curl presente" || falha "curl não encontrado (necessário para baixar modelos e dados do OCR)"
  local ram; ram="$(ram_gb)"
  if [ "$ram" -gt 0 ]; then
    if [ "$ram" -lt 12 ]; then aviso "${ram} GB de RAM: os modelos de 12B pedem uns 10 GB. Considere um modelo menor (ex.: --modelo gemma3:4b) ou o Ollama em outra máquina (--ollama)."
    else ok "${ram} GB de RAM (modelos 12B rodam um de cada vez; feche apps pesados durante a tradução)"; fi
  fi
}

# ---------------------------------------------------------------------------------------- 2. Python
python_ok() { "$1" -c "import sys; sys.exit(0 if sys.version_info >= ($PY_MIN_MAJOR, $PY_MIN_MINOR) else 1)" >/dev/null 2>&1; }

# Como python_ok, mas sem executar o "python3" de /usr/bin do macOS quando faltam as Ferramentas de Linha de Comando:
# nesse caso ele é só um atalho que abre uma janela pedindo para instalá-las.
python_usavel() {
  if [ "$SO" = Darwin ] && [ "$(command -v "$1" 2>/dev/null)" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then
    PY_ATALHO=1
    return 1
  fi
  python_ok "$1"
}

achar_python() {
  local c
  if [ -n "$PYTHON_ARG" ]; then
    if { tem "$PYTHON_ARG" || [ -x "$PYTHON_ARG" ]; } && python_usavel "$PYTHON_ARG"; then PY="$(command -v "$PYTHON_ARG" 2>/dev/null || echo "$PYTHON_ARG")"; return 0; fi
    return 1
  fi
  for c in python3.13 python3.12 python3.11 python3.10 python3 python /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if { tem "$c" || [ -x "$c" ]; } && python_usavel "$c"; then PY="$(command -v "$c" 2>/dev/null || echo "$c")"; return 0; fi
  done
  return 1
}

instalar_python() {
  case "$PM" in
    brew)   instalar_com "o Python 3.12 (brew)" brew install python@3.12 ;;
    apt)    apt_instalar "o Python 3 com venv e pip (apt)" python3 python3-venv python3-pip ;;
    dnf)    instalar_com "o Python 3 (dnf)" como_root dnf install -y python3 python3-pip ;;
    pacman) instalar_com "o Python 3 (pacman)" como_root pacman -S --noconfirm python python-pip ;;
    zypper) instalar_com "o Python 3 (zypper)" como_root zypper install -y python3 python3-pip ;;
    *)      return 1 ;;
  esac
}

passo_python() {
  passo "2. Python e ambiente virtual"
  if ! achar_python; then
    if [ -n "$PYTHON_ARG" ]; then
      [ "$PY_ATALHO" = 1 ] && info "Esse python3 do macOS é só um atalho (pede as Ferramentas de Linha de Comando); o script não o executa."
      falha "'$PYTHON_ARG' não existe ou é anterior ao Python $PY_MIN_MAJOR.$PY_MIN_MINOR"; return 1
    fi
    nota "Python $PY_MIN_MAJOR.$PY_MIN_MINOR+ não encontrado ($(command -v python3 >/dev/null 2>&1 && python3 --version 2>&1 || echo 'nenhum python3'))."
    instalar_python && { hash -r; achar_python; } || true
  fi
  if [ -z "$PY" ]; then
    [ "$PY_ATALHO" = 1 ] && info "O python3 de /usr/bin do macOS é só um atalho (pede as Ferramentas de Linha de Comando); o script não o executa."
    falha "Python $PY_MIN_MAJOR.$PY_MIN_MINOR+ ausente. macOS: brew install python@3.12 · Debian/Ubuntu: sudo apt install python3 python3-venv"
    return 1
  fi
  ok "Python $("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])') em $PY"
  PYANY="$PY"

  if [ "$SEM_VENV" = 1 ]; then
    VPY="$PY"
    aviso "Sem ambiente virtual (--sem-venv): as bibliotecas vão para o Python escolhido."
    return 0
  fi

  if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c 'pass' >/dev/null 2>&1 && python_ok "$VENV/bin/python"; then
    ok "Ambiente virtual .venv já existe"
    VPY="$VENV/bin/python"
    return 0
  fi
  if [ -e "$VENV" ] && [ "$VERIFICAR" != 1 ]; then
    nota ".venv existe, mas está quebrado ou é de um Python antigo: vou recriá-lo."
    rm -rf "$VENV"
  fi
  if [ "$VERIFICAR" = 1 ]; then
    falha "Ambiente virtual .venv não existe ou está quebrado (rode ./setup.sh)"
    return 1
  fi
  printf '    %s$ %s -m venv .venv%s\n' "$C_D" "$PY" "$C_0"
  if ! "$PY" -m venv "$VENV" >"$LOG" 2>&1; then
    if [ "$PM" = apt ] && grep -qi "ensurepip\|python3-venv" "$LOG"; then
      apt_instalar "o módulo venv do Python (apt)" python3-venv python3-pip && "$PY" -m venv "$VENV" >"$LOG" 2>&1
    fi
  fi
  if [ -x "$VENV/bin/python" ]; then
    ok "Ambiente virtual criado em .venv"
    VPY="$VENV/bin/python"
  else
    tail -5 "$LOG" | sed 's/^/    /'
    falha "Não consegui criar o ambiente virtual .venv"
    return 1
  fi
}

# ---------------------------------------------------------------------------------------- 3. bibliotecas
passo_bibliotecas() {
  passo "3. Bibliotecas Python"
  [ -n "$VPY" ] || { info "Pulado: ainda não há ambiente Python (veja o passo 2)."; return 0; }
  if [ "$VERIFICAR" != 1 ]; then
    "$VPY" -m pip --version >/dev/null 2>&1 || "$VPY" -m ensurepip --upgrade >/dev/null 2>&1
    "$VPY" -m pip install --disable-pip-version-check --quiet --upgrade pip >"$LOG" 2>&1 || true
    printf '    instalando requirements.txt (pode levar um minuto)...\n'
    if "$VPY" -m pip install --disable-pip-version-check -r "$RAIZ/requirements.txt" >"$LOG" 2>&1; then
      ok "requirements.txt instalado"
    else
      tail -8 "$LOG" | sed 's/^/    /'
      falha "pip não conseguiu instalar requirements.txt (sem internet? versão do Python sem pacotes prontos?)"
    fi
  fi
  local saida linha faltam=""
  saida="$("$VPY" -c "$PY_IMPORTS" 2>/dev/null)"
  while IFS='|' read -r estado nome extra; do
    [ -n "$estado" ] || continue
    if [ "$estado" = ok ]; then ok "$nome $extra"; else faltam="$faltam $nome"; fi
  done <<<"$saida"
  if [ -n "$faltam" ]; then
    falha "Bibliotecas que não importam:$faltam  →  ${VPY} -m pip install -r requirements.txt"
  fi
  if [ "$DEV" = 1 ] && [ "$VERIFICAR" != 1 ]; then
    printf '    instalando ferramentas de teste (reportlab, playwright)...\n'
    if "$VPY" -m pip install --disable-pip-version-check reportlab playwright >"$LOG" 2>&1; then
      ok "reportlab e playwright instalados"
      if confirmar "Baixar o Chromium do Playwright (~150 MB) para os testes da tela de revisão?" s; then
        rodar "$VPY" -m playwright install chromium >"$LOG" 2>&1 && ok "Chromium do Playwright instalado" \
          || { tail -3 "$LOG" | sed 's/^/    /'; aviso "Não consegui baixar o Chromium do Playwright (os testes da tela serão pulados)."; }
      fi
    else
      tail -5 "$LOG" | sed 's/^/    /'
      aviso "Não consegui instalar as ferramentas de teste."
    fi
  fi
}

# ---------------------------------------------------------------------------------------- 4. OCR
achar_tesseract() {
  TESS="${TESSERACT_CMD:-}"
  [ -n "$TESS" ] && { tem "$TESS" || [ -x "$TESS" ]; } && return 0
  TESS="$(command -v tesseract 2>/dev/null || true)"
  [ -n "$TESS" ] && return 0
  local c
  for c in /opt/homebrew/bin/tesseract /usr/local/bin/tesseract /usr/bin/tesseract; do
    [ -x "$c" ] && { TESS="$c"; return 0; }
  done
  TESS=""; return 1
}

idiomas_tesseract() {
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

baixar_traineddata() {  # baixar_traineddata IDIOMA REPOSITÓRIO(best|fast)
  local lang="$1" repo="$2" tmp ref url
  tmp="$(mktemp "${TMPDIR:-/tmp}/pharos-tess.XXXXXX")"
  for ref in main 4.1.0; do
    url="$TESSDATA_URL_BASE/tessdata_$repo/raw/$ref/$lang.traineddata"
    if curl -fsSL --retry 2 --max-time 600 -o "$tmp" "$url" 2>/dev/null && [ "$(wc -c <"$tmp")" -gt 10000 ]; then
      if [ -w "$TESSDATA_DIR" ]; then cp "$tmp" "$TESSDATA_DIR/$lang.traineddata" && chmod 644 "$TESSDATA_DIR/$lang.traineddata"
      else como_root cp "$tmp" "$TESSDATA_DIR/$lang.traineddata" && como_root chmod 644 "$TESSDATA_DIR/$lang.traineddata"
      fi
      local rc=$?
      rm -f "$tmp"
      return $rc
    fi
  done
  rm -f "$tmp"
  return 1
}

instalar_tesseract_pm() {  # $1 = lista de idiomas desejados
  local l pacotes
  case "$PM" in
    brew) instalar_com "o Tesseract com todos os idiomas (brew install tesseract tesseract-lang)" brew install tesseract tesseract-lang ;;
    apt)
      pacotes="tesseract-ocr"
      for l in $1; do pacotes="$pacotes tesseract-ocr-$l"; done
      apt_instalar "o Tesseract e os idiomas ($1) pelo apt" $pacotes ;;
    dnf)    instalar_com "o Tesseract (dnf)" como_root dnf install -y tesseract ;;
    pacman) instalar_com "o Tesseract (pacman)" como_root pacman -S --noconfirm tesseract ;;
    zypper) instalar_com "o Tesseract (zypper)" como_root zypper install -y tesseract-ocr ;;
    *)      return 1 ;;
  esac
}

passo_ocr() {
  passo "4. OCR (Tesseract)"
  if [ "$SEM_OCR" = 1 ]; then info "Pulado (--sem-ocr). Livros escaneados não poderão ser lidos."; return 0; fi
  local desejados="$IDIOMAS_BASE $IDIOMAS_EXTRA" faltam l v
  if ! achar_tesseract; then
    nota "Tesseract não encontrado."
    instalar_tesseract_pm "$desejados" && { hash -r; achar_tesseract; } || true
  fi
  if ! achar_tesseract; then
    falha "Tesseract ausente (necessário para livros escaneados). macOS: brew install tesseract tesseract-lang · Debian/Ubuntu: sudo apt install tesseract-ocr tesseract-ocr-deu"
    return 1
  fi
  v="$("$TESS" --version 2>&1 | head -1 | sed 's/^[^0-9]*//; s/[^0-9.].*$//')"
  if [ -z "$v" ]; then
    falha "O Tesseract em $TESS não executa (instalação quebrada?). macOS: brew reinstall tesseract"
    return 1
  fi
  if [ "$(versao_inteira "$v")" -lt 4 ]; then
    falha "Tesseract $v é antigo demais (precisa do 4 ou mais, com o motor LSTM)"
    return 1
  fi
  ok "Tesseract $v em $TESS"

  local inst; inst="$(idiomas_tesseract)"
  faltam=""
  for l in $desejados; do grep -Fxq "$l" <<<"$inst" || faltam="$faltam $l"; done
  if [ -n "$faltam" ] && [ "$VERIFICAR" != 1 ]; then
    nota "Idiomas do OCR que faltam:$faltam"
    case "$PM" in
      apt) instalar_tesseract_pm "$desejados" || true ;;
      brew) instalar_com "os idiomas do Tesseract (brew install tesseract-lang)" brew install tesseract-lang || true ;;
    esac
    inst="$(idiomas_tesseract)"
  fi
  TESSDATA_DIR="$(tessdata_dir)"
  faltam=""
  for l in $desejados; do grep -Fxq "$l" <<<"$inst" || faltam="$faltam $l"; done
  if [ -n "$faltam" ] && [ "$VERIFICAR" != 1 ] && [ -n "$TESSDATA_DIR" ]; then
    if confirmar "Baixar direto do repositório oficial do Tesseract os idiomas:$faltam (para $TESSDATA_DIR)?" s; then
      for l in $faltam; do
        if baixar_traineddata "$l" fast; then ok "$l baixado"; else aviso "Não consegui baixar $l ($TESSDATA_URL_BASE/tessdata_fast)."; fi
      done
      inst="$(idiomas_tesseract)"
    fi
  fi
  for l in $desejados; do
    if grep -Fxq "$l" <<<"$inst"; then ok "idioma $l"
    elif [ "$l" = eng ] || [ "$l" = osd ]; then falha "idioma $l ausente (necessário). Instale: tesseract-ocr-$l (apt) ou tesseract-lang (brew)"
    else aviso "idioma $l ausente: livros em '$l' não poderão ser lidos por OCR (instale tesseract-ocr-$l ou use brew install tesseract-lang)"
    fi
  done

  # letra gótica (Fraktur): o programa testa estes modelos sozinho e usa o que ler melhor
  local gotica_ok="" gotica_falta=""
  for l in $IDIOMAS_GOTICOS; do grep -Fxq "$l" <<<"$inst" && gotica_ok="$gotica_ok $l" || gotica_falta="$gotica_falta $l"; done
  if [ -n "$gotica_falta" ] && [ "$VERIFICAR" != 1 ] && [ -n "$TESSDATA_DIR" ]; then
    info "Livros impressos em letra gótica (Fraktur) precisam de um destes modelos:$gotica_falta"
    if confirmar "Baixar os modelos de Fraktur (versão 'best', mais precisa) para $TESSDATA_DIR?" s; then
      for l in $gotica_falta; do
        if baixar_traineddata "$l" best; then ok "$l (Fraktur) baixado"; else aviso "Não consegui baixar $l ($TESSDATA_URL_BASE/tessdata_best)."; fi
      done
      inst="$(idiomas_tesseract)"
      gotica_ok=""
      for l in $IDIOMAS_GOTICOS; do grep -Fxq "$l" <<<"$inst" && gotica_ok="$gotica_ok $l"; done
    fi
  fi
  if [ -n "$gotica_ok" ]; then ok "letra gótica (Fraktur):$gotica_ok"
  else aviso "Sem modelo de Fraktur (frk, deu_latf): livros góticos sairão com muitos erros. Rode ./setup.sh de novo para baixar."
  fi
}

# ---------------------------------------------------------------------------------------- 5. Ollama
normalizar_url() {
  local u="$1" resto host
  case "$u" in http://*|https://*) ;; *) u="http://$u" ;; esac
  resto="${u#*://}"; resto="${resto%%/*}"
  host="${resto%%:*}"
  [ "$host" = "0.0.0.0" ] && u="${u/0.0.0.0/localhost}" && resto="${resto/0.0.0.0/localhost}"
  case "$resto" in *:*) ;; *) u="${u%/}:11434" ;; esac
  printf '%s' "${u%/}"
}

e_local() {
  local resto="${1#*://}"; resto="${resto%%/*}"
  case "${resto%%:*}" in localhost|127.*|"[::1]"|::1) return 0 ;; *) return 1 ;; esac
}

ollama_no_ar() { curl -fsS --max-time 6 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; }

listar_modelos() {
  curl -fsS --max-time 10 "$OLLAMA_URL/api/tags" 2>/dev/null | grep -o '"name": *"[^"]*"' | sed 's/^"name": *"//; s/"$//' || true
}

tem_modelo() {
  local m="$1" base="$1"
  case "$m" in *:*) ;; *) base="$m:latest" ;; esac
  grep -Fxq -e "$m" -e "$base" <<<"$MODELOS_INST"
}

achar_ollama_cli() {
  tem ollama && return 0
  [ -x /Applications/Ollama.app/Contents/Resources/ollama ] && return 0
  return 1
}

iniciar_ollama_local() {
  if [ "$SO" = Darwin ] && [ -d /Applications/Ollama.app ]; then
    rodar open -a Ollama
  elif [ "$PM" = brew ] && brew list --formula ollama >/dev/null 2>&1; then
    rodar brew services start ollama
  elif tem systemctl && systemctl list-unit-files 2>/dev/null | grep -q '^ollama.service'; then
    rodar como_root systemctl start ollama
  elif tem ollama; then
    printf '    %s$ ollama serve (em segundo plano; log em .ollama-serve.log)%s\n' "$C_D" "$C_0"
    nohup ollama serve >"$RAIZ/.ollama-serve.log" 2>&1 &
  else
    return 1
  fi
}

esperar_ollama() {
  local i
  for i in $(seq 1 45); do ollama_no_ar && return 0; sleep 1; done
  return 1
}

instalar_ollama() {
  if [ "$SO" = Darwin ]; then
    if [ "$PM" = brew ]; then instalar_com "o Ollama (brew install ollama)" brew install ollama; else return 1; fi
  else
    if [ "$VERIFICAR" = 1 ]; then return 1; fi
    if [ "$INSTALAR_OLLAMA" = 1 ] || confirmar "Rodar o instalador oficial do Ollama (curl -fsSL https://ollama.com/install.sh | sh, pede sudo)?" n; then
      printf '    %s$ curl -fsSL https://ollama.com/install.sh | sh%s\n' "$C_D" "$C_0"
      curl -fsSL https://ollama.com/install.sh | sh
    else
      info "Para instalar à mão:  curl -fsSL https://ollama.com/install.sh | sh   (ou use --instalar-ollama)"
      return 1
    fi
  fi
}

baixar_modelo() {  # baixa pela API do Ollama (serve para local e remoto), com barra de progresso e novas tentativas
  local modelo="$1" tent=0 rc
  while [ "$tent" -lt 3 ]; do
    tent=$((tent + 1))
    "$PYANY" -c "$PY_PULL" "$OLLAMA_URL" "$modelo"; rc=$?
    [ "$rc" -eq 0 ] && return 0
    [ "$rc" -ge 130 ] && exit 130
    printf '    nova tentativa (%d/3) em 3 s... o Ollama retoma de onde parou\n' "$tent"
    sleep 3
  done
  return 1
}

garantir_modelo() {  # garantir_modelo NOME "para que serve" obrigatorio(1|0)
  local m="$1" uso="$2" obrig="$3" falta_fn=aviso
  [ "$obrig" = 1 ] && falta_fn=falha
  if tem_modelo "$m"; then ok "modelo $m ($uso)"; return 0; fi
  if [ "$SEM_MODELOS" = 1 ] || [ "$VERIFICAR" = 1 ]; then
    $falta_fn "modelo $m ($uso) não está instalado → ./setup.sh baixa (modelos de 12B: uns 8 GB)"
    return 1
  fi
  if [ "$OLLAMA_LOCAL" = 1 ]; then
    local livre; livre="$(livre_gb "${OLLAMA_MODELS:-$HOME/.ollama}")"
    if [ -n "$livre" ] && [ "$livre" -lt "$GB_POR_MODELO" ]; then
      nota "Só ${livre} GB livres em disco; um modelo de 12B ocupa uns 8 GB."
      confirmar "Tentar baixar mesmo assim?" n || { $falta_fn "modelo $m ($uso) não baixado: pouco espaço em disco"; return 1; }
    fi
  fi
  confirmar "Baixar o modelo $m ($uso; modelos de 12B pesam uns 8 GB)?" s || { $falta_fn "modelo $m ($uso) não baixado → ollama pull $m"; return 1; }
  printf '    baixando %s de %s ...\n' "$m" "$OLLAMA_URL"
  if baixar_modelo "$m"; then
    ok "modelo $m baixado"
    MODELOS_INST="$MODELOS_INST"$'\n'"$m"
    return 0
  fi
  $falta_fn "não consegui baixar $m (rede? espaço em disco?) → ollama pull $m"
  return 1
}

testar_modelo() {  # tradução curta para ver se o modelo carrega e responde
  local m="$1"
  printf '    carregando %s (pode levar um minuto)...\n' "$m"
  if "$PYANY" -c "$PY_TESTE" "$OLLAMA_URL" "$m"
  then ok "modelo $m respondeu"; else aviso "O modelo $m não respondeu (memória insuficiente? veja os logs do Ollama)."; fi
}

passo_ollama() {
  passo "5. Ollama e modelos"
  OLLAMA_URL="$(normalizar_url "${OLLAMA_URL_ARG:-${OLLAMA_HOST:-http://localhost:11434}}")"
  if e_local "$OLLAMA_URL"; then OLLAMA_LOCAL=1; else OLLAMA_LOCAL=0; fi
  info "Servidor: $OLLAMA_URL ($([ "$OLLAMA_LOCAL" = 1 ] && echo 'esta máquina' || echo 'outra máquina'))"

  if [ "$OLLAMA_LOCAL" = 1 ]; then
    if achar_ollama_cli; then ok "Ollama instalado"
    else
      nota "Ollama não está instalado."
      if instalar_ollama; then hash -r; fi
      achar_ollama_cli && ok "Ollama instalado" || { falha "Ollama ausente. macOS: brew install ollama (ou https://ollama.com/download) · Linux: curl -fsSL https://ollama.com/install.sh | sh"; return 1; }
    fi
  fi

  if ollama_no_ar; then
    ok "Ollama respondendo em $OLLAMA_URL"
  elif [ "$OLLAMA_LOCAL" = 1 ] && [ "$VERIFICAR" != 1 ]; then
    nota "Ollama não está respondendo; vou iniciá-lo."
    if iniciar_ollama_local && esperar_ollama; then ok "Ollama iniciado em $OLLAMA_URL"
    else falha "Não consegui iniciar o Ollama. Abra o app Ollama ou rode 'ollama serve' em outro terminal e tente de novo."; return 1; fi
  else
    if [ "$OLLAMA_LOCAL" = 1 ]; then falha "Ollama não está respondendo em $OLLAMA_URL (abra o app ou rode 'ollama serve')"
    else falha "Não alcancei o Ollama em $OLLAMA_URL. Confira o endereço, a rede e se o servidor escuta fora de localhost (OLLAMA_HOST=0.0.0.0)."
    fi
    return 1
  fi

  MODELOS_INST="$(listar_modelos)"
  garantir_modelo "$MODELO_TRAD" "tradução" 1 || true
  if [ "$SEM_MODELO_GERAL" = 1 ]; then
    info "Modelo geral para sugestões de OCR: pulado (--sem-modelo-ocr)."
  elif [ "$SEM_OCR" = 1 ] && [ "$MODELO_GERAL_EXPLICITO" != 1 ]; then
    info "Modelo geral para sugestões de OCR: pulado (OCR desligado)."
  else
    garantir_modelo "$MODELO_GERAL" "sugestões de correção do OCR" 0 || true
  fi
  if [ "$TESTAR" = 1 ] && [ "$VERIFICAR" != 1 ]; then
    tem_modelo "$MODELO_TRAD" && testar_modelo "$MODELO_TRAD"
    if [ "$SEM_MODELO_GERAL" != 1 ] && tem_modelo "$MODELO_GERAL"; then testar_modelo "$MODELO_GERAL"; fi
  fi
}

# ---------------------------------------------------------------------------------------- 6. opcionais
passo_opcionais() {
  passo "6. Opcionais"
  local calibre=""
  if tem ebook-convert; then calibre="$(command -v ebook-convert)"
  elif [ -x /Applications/calibre.app/Contents/MacOS/ebook-convert ]; then calibre="/Applications/calibre.app/Contents/MacOS/ebook-convert"; fi
  if [ -n "$calibre" ]; then ok "Calibre ($calibre): MOBI/AZW3/FB2/DOCX serão lidos"
  elif [ "$CALIBRE" = 1 ] && [ "$VERIFICAR" != 1 ]; then
    case "$PM" in
      brew)   instalar_com "o Calibre (brew install --cask calibre)" brew install --cask calibre ;;
      apt)    instalar_com "o Calibre (apt)" como_root apt-get install -y calibre ;;
      dnf)    instalar_com "o Calibre (dnf)" como_root dnf install -y calibre ;;
      pacman) instalar_com "o Calibre (pacman)" como_root pacman -S --noconfirm calibre ;;
      *)      info "Instale o Calibre em https://calibre-ebook.com/download" ;;
    esac
    hash -r
    if tem ebook-convert || [ -x /Applications/calibre.app/Contents/MacOS/ebook-convert ]; then ok "Calibre instalado"; else aviso "Calibre não instalado."; fi
  else
    info "Calibre não instalado (opcional: só para MOBI/AZW3/FB2/DOCX; EPUB, PDF, TXT e scans funcionam sem ele). Use --calibre para instalar."
  fi
}

# ---------------------------------------------------------------------------------------- 7. conferência
passo_conferencia() {
  passo "7. Conferência final"
  if [ -z "$VPY" ]; then info "Pulada: sem Python utilizável."; return 0; fi
  local v
  if v="$("$VPY" "$RAIZ/pharos.py" --versao 2>&1)"; then ok "O programa abre: $v"
  else printf '%s\n' "$v" | tail -4 | sed 's/^/    /'; falha "pharos.py não inicia (veja a mensagem acima)"; return 1; fi
  if [ "$SEM_OCR" != 1 ] && [ -n "$TESS" ]; then
    local saida
    saida="$(TESSERACT_CMD="$TESS" "$VPY" -c "$PY_OCR" "$RAIZ" 2>&1)"
    case "$saida" in
      ok\|*) ok "O programa enxerga o Tesseract e os idiomas: ${saida#ok|}" ;;
      *)     falha "O programa não conseguiu usar o Tesseract: ${saida#erro|}" ;;
    esac
  fi
}

criar_atalho() {
  [ "$VERIFICAR" = 1 ] && return 0
  [ -n "$VPY" ] || return 0
  {
    local u="" m="" mo=""
    [ -n "$OLLAMA_URL_ARG" ] && [ "$OLLAMA_LOCAL" != 1 ] && u="$OLLAMA_URL"
    [ "$MODELO_TRAD_EXPLICITO" = 1 ] && m="$MODELO_TRAD"
    [ "$MODELO_GERAL_EXPLICITO" = 1 ] && mo="$MODELO_GERAL"
    printf '# Gerado pelo setup.sh (ajustes lembrados entre execuções). Apague este arquivo para voltar ao padrão.\n'
    printf 'OLLAMA_URL=%q\nMODELO=%q\nMODELO_OCR=%q\n' "$u" "$m" "$mo"
  } >"$CONF"
  cat >"$RAIZ/pharos.sh" <<'ATALHO'
#!/usr/bin/env bash
# Gerado pelo setup.sh: roda o Pharos com o Python do ambiente (.venv), sem precisar "ativar" nada.
#   ./pharos.sh livro.pdf          (equivale a: python pharos.py livro.pdf)
# Os ajustes escolhidos no setup (Ollama em outra máquina, modelos) ficam em .setup.conf.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OLLAMA_URL=""; MODELO=""; MODELO_OCR=""
[ -f "$DIR/.setup.conf" ] && . "$DIR/.setup.conf"
PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || PY="${PYTHON:-python3}"
tem_opcao() { local o="$1" a; shift; for a in "$@"; do case "$a" in "$o"|"$o="*) return 0 ;; esac; done; return 1; }
if [ -n "$OLLAMA_URL" ] && ! tem_opcao --ollama "$@"; then set -- "$@" --ollama "$OLLAMA_URL"; fi
if [ -n "$MODELO" ] && ! tem_opcao --modelo "$@"; then set -- "$@" --modelo "$MODELO"; fi
if [ -n "$MODELO_OCR" ] && ! tem_opcao --modelo-ocr "$@"; then set -- "$@" --modelo-ocr "$MODELO_OCR"; fi
exec "$PY" "$DIR/pharos.py" "$@"
ATALHO
  chmod +x "$RAIZ/pharos.sh"
  ok "Atalho criado: ./pharos.sh (usa o .venv; dispensa 'source .venv/bin/activate')"
}

# ---------------------------------------------------------------------------------------- execução
printf '%sPharos — %s do ambiente%s\n' "$C_B" "$([ "$VERIFICAR" = 1 ] && echo verificação || echo preparação)" "$C_0"
[ "$VERIFICAR" = 1 ] && info "Modo --verificar: nada será instalado nem alterado."
[ "$VERIFICAR" != 1 ] && [ "$SIM" != 1 ] && [ ! -t 0 ] && info "Sem terminal interativo e sem -y: não instalo pacotes do sistema nem baixo modelos (só preparo o .venv e as bibliotecas Python). Use -y para aceitar tudo."

passo_sistema
passo_python
passo_bibliotecas
passo_ocr
passo_ollama
passo_opcionais
passo_conferencia
criar_atalho

passo "Resumo"
if [ -n "$RESUMO_FALHAS" ]; then printf '%s%sO que falta (obrigatório):%s\n%s' "$C_ER" "$C_B" "$C_0" "$RESUMO_FALHAS"; fi
if [ -n "$RESUMO_AVISOS" ]; then printf '%s%sAvisos:%s\n%s' "$C_AV" "$C_B" "$C_0" "$RESUMO_AVISOS"; fi
if [ "$FALHAS" -eq 0 ]; then
  if [ "$VERIFICAR" = 1 ]; then printf '  %s✓ Ambiente pronto.%s\n' "$C_OK" "$C_0"
  else
    printf '  %s✓ Ambiente pronto.%s Para traduzir:\n' "$C_OK" "$C_0"
    info "./pharos.sh meu_livro.pdf"
    info "./pharos.sh scan_do_livro.pdf --origem de      # scan em alemão: OCR + revisão na tela"
    info "./pharos.sh --ajuda"
  fi
  exit 0
fi
if [ "$VERIFICAR" = 1 ]; then printf '\n  Rode ./setup.sh (sem --verificar) para preparar o que falta.\n'
else printf '\n  Resolva o que está marcado com ✗ e rode ./setup.sh de novo: ele retoma de onde parou.\n'
fi
exit 1
