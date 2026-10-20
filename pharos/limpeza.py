"""Limpeza de texto extraído: ligaduras, hifenização de fim de linha, espaços."""
from __future__ import annotations

import re
import unicodedata

LIGATURAS = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
}

# Depois de "Ein-" + "und Ausgang" não se deve colar: a palavra seguinte é conjunção.
_CONJUNCOES = {"und", "oder", "bzw", "sowie", "y", "e", "o", "u", "and", "or", "et", "ou", "ni"}
_RE_ESPACOS = re.compile(r"[ \t   ]+")
_RE_PRIMEIRA_PALAVRA = re.compile(r"[^\W\d_]+")


def limpar_texto(t: str) -> str:
    t = unicodedata.normalize("NFC", t)
    for k, v in LIGATURAS.items():
        t = t.replace(k, v)
    t = t.replace("­", "")  # hífen suave
    t = _RE_ESPACOS.sub(" ", t)
    return t.strip()


def _hifenizada(anterior: str, proxima: str) -> bool:
    if not anterior.endswith(("-", "‐", "‑")):
        return False
    if len(anterior) < 2 or not anterior[-2].isalpha():
        return False
    m = _RE_PRIMEIRA_PALAVRA.match(proxima)
    if not m:
        return False
    palavra = m.group(0)
    return palavra[0].islower() and palavra.lower() not in _CONJUNCOES


def juntar_linhas(linhas: list[str]) -> str:
    """Une as linhas de um parágrafo e desfaz a hifenização de fim de linha."""
    saida = ""
    for linha in linhas:
        linha = linha.strip()
        if not linha:
            continue
        if not saida:
            saida = linha
        elif _hifenizada(saida, linha):
            saida = saida[:-1] + linha
        else:
            saida += " " + linha
    return limpar_texto(saida)
