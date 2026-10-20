"""Extrator de texto puro / Markdown."""
from __future__ import annotations

import re
import statistics
from pathlib import Path

from ..limpeza import juntar_linhas, limpar_texto
from ..modelo import Bloco, PARAGRAFO, SEPARADOR, TITULO, VERSO
from .comum import RE_CAPITULO, dividir_em_capitulos, e_separador, montar_livro, parece_verso


def _ler(caminho: Path) -> str:
    bruto = caminho.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return bruto.decode(enc)
        except UnicodeError:
            continue
    return bruto.decode("utf-8", errors="replace")


def extrair(caminho: Path) -> tuple:
    texto = _ler(caminho).replace("\r\n", "\n").replace("\r", "\n")
    paragrafos = [p for p in re.split(r"\n\s*\n", texto) if p.strip()]

    # linha típica do corpo (para distinguir texto com quebra rígida de versos)
    tamanhos = [len(l.strip()) for p in paragrafos for l in p.split("\n") if len(p.split("\n")) >= 3 and l.strip()]
    mediana = statistics.median(tamanhos) if tamanhos else 0

    blocos: list[Bloco] = []
    for p in paragrafos:
        linhas = [l.rstrip() for l in p.split("\n") if l.strip()]
        if not linhas:
            continue
        if len(linhas) == 1:
            unica = linhas[0].strip()
            if e_separador(unica):
                blocos.append(Bloco("", SEPARADOR, "* * *"))
                continue
            m = re.match(r"^(#{1,6})\s+(.*)$", unica)
            if m:
                blocos.append(Bloco("", TITULO, limpar_texto(m.group(2)), nivel=len(m.group(1))))
                continue
            if len(unica) < 80 and RE_CAPITULO.match(unica):
                blocos.append(Bloco("", TITULO, limpar_texto(unica), nivel=1))
                continue
            if (
                len(unica) < 60
                and unica.upper() == unica
                and any(c.isalpha() for c in unica)
                and not unica.endswith((".", ","))
            ):
                blocos.append(Bloco("", TITULO, limpar_texto(unica), nivel=2))
                continue
        if parece_verso(linhas, mediana):
            blocos.append(Bloco("", VERSO, "\n".join(limpar_texto(l) for l in linhas)))
        else:
            blocos.append(Bloco("", PARAGRAFO, juntar_linhas(linhas)))

    titulo = caminho.stem.replace("_", " ").replace("-", " ").strip()
    livro = montar_livro(titulo, "", "", dividir_em_capitulos(blocos))
    return livro, {}
