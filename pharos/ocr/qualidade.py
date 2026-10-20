"""Qualidade do OCR por página e sugestões de correção.

Pontuação: confiança média do Tesseract, descontada pela fração de palavras duvidosas e por texto de aparência
implausível. As sugestões usam só o próprio livro (sem dicionário): uma palavra rara que difere em uma letra de uma
palavra frequente do mesmo livro, ou que vira uma palavra frequente trocando confusões típicas de OCR (rn/m, cl/d...).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from ..extratores.pdf_texto import qualidade_texto
from .tesseract import confianca_media

LIMIAR_PALAVRA = 70   # confiança abaixo disso = palavra duvidosa
LIMIAR_PAGINA = 85    # pontuação abaixo disso = página sinalizada

_RE_TOKEN = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)
_CONFUSOES = [("rn", "m"), ("m", "rn"), ("ii", "n"), ("n", "ii"), ("li", "h"), ("h", "li"), ("cl", "d"), ("d", "cl"),
              ("vv", "w"), ("w", "vv"), ("l", "i"), ("i", "l"), ("c", "e"), ("e", "c"), ("ſ", "f"), ("f", "s"),
              ("s", "f"), ("u", "n"), ("n", "u")]
_CONF_MAXIMA_CONFUSAO = 90  # palavra que o Tesseract leu com mais confiança que isso não entra nas sugestões


def pontuar_pagina(pag: dict, limiar_palavra: int = LIMIAR_PALAVRA, limiar_pagina: int = LIMIAR_PAGINA) -> dict:
    palavras = pag["palavras"]
    n = len(palavras)
    base = {"n": pag["n"], "palavras": n, "tinta": pag.get("tinta", 0.0)}
    if n < 8:
        mudo = pag.get("tinta", 0.0) < 0.012
        return {**base, "conf": confianca_media(palavras), "duvidosas": 0, "fracao_duvidosas": 0.0,
                "plausivel": 1.0, "score": 100.0 if mudo else 0.0, "sinalizada": not mudo,
                "motivos": [] if mudo else ["quase sem texto reconhecido, mas a página tem imagem ou tinta"]}
    letras = sum(len(p[8]) for p in palavras) or 1
    duvidosas = [p for p in palavras if p[7] < limiar_palavra and any(c.isalnum() for c in p[8])]
    frac = sum(len(p[8]) for p in duvidosas) / letras
    media = confianca_media(palavras)
    plausivel = qualidade_texto(" ".join(p[8] for p in palavras))
    score = round(max(0.0, media - 30 * (1 - plausivel) - 30 * frac), 1)
    motivos = []
    if media < limiar_pagina:
        motivos.append(f"confiança média {media:.0f}%")
    if frac >= 0.08:
        motivos.append(f"{len(duvidosas)} palavras duvidosas ({frac:.0%} do texto)")
    if plausivel < 0.9:
        motivos.append("muitos trechos com aparência de ruído")
    return {**base, "conf": round(media, 1), "duvidosas": len(duvidosas), "fracao_duvidosas": round(frac, 3),
            "plausivel": round(plausivel, 3), "score": score, "sinalizada": score < limiar_pagina or bool(motivos),
            "motivos": motivos}


def pontuar_todas(paginas: dict[int, dict], limiar_palavra: int = LIMIAR_PALAVRA,
                  limiar_pagina: int = LIMIAR_PAGINA) -> dict[int, dict]:
    return {n: pontuar_pagina(p, limiar_palavra, limiar_pagina) for n, p in sorted(paginas.items())}


def resumir(pontos: dict[int, dict]) -> dict:
    if not pontos:
        return {"paginas": 0, "sinalizadas": 0, "media": 0.0, "pior": [], "geral_ruim": False}
    sinal = [n for n, p in pontos.items() if p["sinalizada"]]
    com_texto = [p for p in pontos.values() if p["palavras"] >= 8]
    media = sum(p["conf"] for p in com_texto) / len(com_texto) if com_texto else 0.0
    pior = sorted(pontos.values(), key=lambda p: p["score"])[:5]
    return {
        "paginas": len(pontos), "sinalizadas": len(sinal), "lista_sinalizadas": sinal, "media": round(media, 1),
        "pior": [(p["n"], p["score"]) for p in pior],
        "geral_ruim": len(sinal) > 0.5 * len(pontos) and len(pontos) >= 4,
    }


# ----------------------------------------------------------------------------- sugestões

def _caixa_como(original: str, candidata: str) -> str:
    if original.isupper() and len(original) > 1:
        return candidata.upper()
    if original[:1].isupper() and not candidata[:1].isupper():
        return candidata[:1].upper() + candidata[1:]
    if original[:1].islower() and candidata[:1].isupper():
        return candidata[:1].lower() + candidata[1:]
    return candidata


def gerar_sugestoes(paginas: dict[int, dict], limiar_palavra: int = 80, maximo: int = 300) -> list[dict]:
    freq: Counter = Counter()
    formas: dict[str, Counter] = defaultdict(Counter)
    onde: dict[str, list[int]] = defaultdict(list)
    confs: dict[str, list[float]] = defaultdict(list)
    for n, pag in paginas.items():
        for p in pag["palavras"]:
            for tok in _RE_TOKEN.findall(p[8]):
                if len(tok) < 4:
                    continue
                k = tok.lower()
                freq[k] += 1
                formas[k][tok] += 1
                onde[k].append(n)
                confs[k].append(p[7])
    frequentes = {k for k, c in freq.items() if c >= 3}
    indice: dict[str, set[str]] = defaultdict(set)
    for k in frequentes:
        indice[k].add(k)
        for i in range(len(k)):
            indice[k[:i] + k[i + 1:]].add(k)

    sugestoes = []
    for k, c in freq.items():
        if c > 2 or k in frequentes:
            continue
        conf_min = min(confs[k])
        if conf_min >= _CONF_MAXIMA_CONFUSAO:
            continue
        duvidosa = conf_min < limiar_palavra
        vizinhas = set(indice.get(k, ())) | {t for i in range(len(k)) for t in indice.get(k[:i] + k[i + 1:], ())}
        vizinhas.discard(k)
        por_confusao: set[str] = set()
        for a, b in _CONFUSOES:
            start = 0
            while (i := k.find(a, start)) != -1:
                t = k[:i] + b + k[i + len(a):]
                if t in frequentes and t != k:
                    por_confusao.add(t)
                start = i + 1
        candidatas = (vizinhas if duvidosa else set()) | por_confusao
        if not candidatas:
            continue
        melhor = max(candidatas, key=lambda t: freq[t])
        if freq[melhor] < max(3, 3 * c):
            continue
        original = formas[k].most_common(1)[0][0]
        para = _caixa_como(original, formas[melhor].most_common(1)[0][0])
        sugestoes.append({"de": original, "para": para, "n": c, "paginas": sorted(set(onde[k]))[:12],
                          "conf": round(min(confs[k]), 1), "freq_para": freq[melhor]})
    sugestoes.sort(key=lambda s: (-s["n"], s["conf"]))
    return sugestoes[:maximo]
