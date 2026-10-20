"""Tesseract pela linha de comando: localizar, listar idiomas, rodar (saída TSV) e detectar orientação.

Cada palavra reconhecida vira uma lista: [bloco, par, linha, esq, topo, larg, alt, conf, texto]
(coordenadas em pixels da imagem enviada ao Tesseract; conf de 0 a 100).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from ..extratores.comum import ErroExtracao

INSTRUCOES_INSTALACAO = (
    "O OCR precisa do Tesseract instalado.\n"
    "  macOS:  brew install tesseract tesseract-lang\n"
    "  Linux:  sudo apt install tesseract-ocr tesseract-ocr-deu   (e os idiomas que precisar)\n"
    "  Ou rode ./setup.sh, que instala o Tesseract e os idiomas do OCR.\n"
    "Confira os idiomas disponíveis com: tesseract --list-langs"
)

_CAMINHOS_COMUNS = ["/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract", "/usr/bin/tesseract"]

# código ISO do programa -> código do Tesseract
CODIGOS = {
    "de": "deu", "en": "eng", "es": "spa", "fr": "fra", "it": "ita", "pt": "por", "nl": "nld",
    "ca": "cat", "ru": "rus", "pl": "pol", "cs": "ces", "sv": "swe", "da": "dan", "no": "nor",
    "fi": "fin", "ro": "ron", "hu": "hun", "tr": "tur", "el": "ell", "he": "heb", "ar": "ara",
    "ja": "jpn", "ko": "kor", "zh-cn": "chi_sim", "zh-tw": "chi_tra", "la": "lat",
}
_INVERSO = {v: k for k, v in CODIGOS.items()}

# modelos de letra gótica (Fraktur) que vale testar, por idioma, em ordem de preferência
GOTICAS = {"de": ("frk", "deu_latf", "script/Fraktur")}

# idiomas candidatos quando o programa precisa descobrir o idioma de um scan
CANDIDATOS_DETECCAO = ("deu", "eng", "fra", "spa", "ita", "por", "nld", "lat")


class ErroOCR(ErroExtracao):
    pass


def localizar(exe: str | None = None) -> str:
    achado = exe or os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
    if not achado:
        achado = next((c for c in _CAMINHOS_COMUNS if Path(c).exists()), None)
    if not achado:
        raise ErroOCR(INSTRUCOES_INSTALACAO)
    return achado


@lru_cache(maxsize=8)
def idiomas_instalados(exe: str) -> frozenset:
    try:
        r = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ErroOCR(f"Não consegui executar o Tesseract ({exe}): {e}") from e
    linhas = (r.stdout + "\n" + r.stderr).splitlines()
    return frozenset(
        l.strip() for l in linhas
        if l.strip() and not l.lower().startswith(("list of available", "error", "warning"))
    )


def codigo_para(iso: str) -> str | None:
    return CODIGOS.get(iso.lower())


def iso_do_codigo(tess: str) -> str | None:
    primeiro = tess.split("+")[0]
    if primeiro in _INVERSO:
        return _INVERSO[primeiro]
    for iso, modelos in GOTICAS.items():
        if primeiro in modelos:
            return iso
    return None


def faltando(exe: str, spec: str) -> list[str]:
    inst = idiomas_instalados(exe)
    return [p for p in spec.split("+") if p and p not in inst]


def verificar(exe: str, spec: str) -> None:
    if not spec:
        raise ErroOCR("Idioma do OCR não definido.")
    falta = faltando(exe, spec)
    if falta:
        inst = sorted(i for i in idiomas_instalados(exe) if i != "osd")
        raise ErroOCR(
            f"O Tesseract não tem o idioma: {', '.join(falta)}.\n"
            f"Instalados: {', '.join(inst) or '(nenhum)'}.\n"
            "No macOS: brew install tesseract-lang   |   Linux: sudo apt install tesseract-ocr-<idioma>"
        )


def goticas_instaladas(exe: str, iso: str) -> list[str]:
    inst = idiomas_instalados(exe)
    return [g for g in GOTICAS.get(iso, ()) if g in inst]


def _executar(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}  # vários processos em paralelo, 1 thread cada
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        raise ErroOCR(f"O Tesseract demorou mais de {timeout}s numa página.") from e
    except OSError as e:
        raise ErroOCR(f"Falha ao executar o Tesseract: {e}") from e


def rodar_tsv(exe: str, imagem: Path, lang: str, psm: int = 3, dpi: int = 300, timeout: int = 600) -> list[list]:
    r = _executar([exe, str(imagem), "stdout", "-l", lang, "--psm", str(psm), "--dpi", str(dpi), "tsv"], timeout)
    if r.returncode != 0:
        raise ErroOCR(f"Tesseract falhou (código {r.returncode}): {(r.stderr or r.stdout)[-300:].strip()}")
    palavras: list[list] = []
    for linha in r.stdout.splitlines()[1:]:
        c = linha.split("\t", 11)
        if len(c) < 12 or c[0] != "5":
            continue
        texto = c[11].strip()
        if not texto:
            continue
        try:
            conf = float(c[10])
            if conf < 0:
                continue
            palavras.append([int(c[2]), int(c[3]), int(c[4]), int(c[6]), int(c[7]), int(c[8]), int(c[9]),
                             round(conf, 1), texto])
        except ValueError:
            continue
    return palavras


def orientacao(exe: str, imagem: Path, timeout: int = 120) -> tuple[int, float] | None:
    """(graus a girar no sentido horário, confiança) ou None. Precisa do modelo 'osd'."""
    if "osd" not in idiomas_instalados(exe):
        return None
    try:
        r = _executar([exe, str(imagem), "stdout", "--psm", "0"], timeout)
    except ErroOCR:
        return None
    saida = r.stdout + "\n" + r.stderr
    rot = re.search(r"Rotate:\s*(\d+)", saida)
    conf = re.search(r"Orientation confidence:\s*([\d.]+)", saida)
    if not rot or not conf:
        return None
    return int(rot.group(1)), float(conf.group(1))


def confianca_media(palavras: list[list]) -> float:
    """Confiança média ponderada pelo tamanho da palavra (0 se não houver palavras)."""
    total = sum(len(p[8]) for p in palavras)
    if not total:
        return 0.0
    return sum(p[7] * len(p[8]) for p in palavras) / total
