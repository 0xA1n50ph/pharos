"""Glossário em TSV: termo_original <TAB> tradução_obrigatória <TAB> nota (opcional).

Linhas começando com # são comentários. O programa injeta no prompt só os termos presentes em cada
trecho e depois confere se a tradução os aplicou.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CABECALHO = (
    "# Glossário: termo no original <TAB> tradução obrigatória <TAB> nota (opcional)\n"
    "# Exemplo:\tSephiroth\tSefirot\tnome hebraico, transliteração usual em português\n"
)


@dataclass(frozen=True)
class Termo:
    origem: str
    destino: str
    nota: str = ""


def garantir_arquivo(caminho: Path) -> None:
    if not caminho.exists():
        caminho.write_text(CABECALHO, encoding="utf-8")


def carregar(caminho: Path) -> list[Termo]:
    if not caminho.exists():
        return []
    termos = []
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        if not linha.strip() or linha.lstrip().startswith("#"):
            continue
        partes = [p.strip() for p in linha.split("\t")]
        if len(partes) >= 2 and partes[0] and partes[1]:
            termos.append(Termo(partes[0], partes[1], partes[2] if len(partes) > 2 else ""))
    return termos


def adicionar(caminho: Path, origem: str, destino: str, nota: str = "") -> None:
    garantir_arquivo(caminho)
    with open(caminho, "a", encoding="utf-8") as f:
        f.write(f"{origem.strip()}\t{destino.strip()}\t{nota.strip()}\n")


def texto_do_arquivo(caminho: Path) -> str:
    return caminho.read_text(encoding="utf-8") if caminho.exists() else ""


@lru_cache(maxsize=2048)
def _regex(origem: str) -> re.Pattern:
    # aceita flexões no fim da palavra (Engel -> Engels, Engeln)
    return re.compile(r"(?<!\w)" + re.escape(origem) + r"\w*", re.IGNORECASE | re.UNICODE)


def termos_no_texto(glossario: list[Termo], texto: str) -> list[Termo]:
    return [t for t in glossario if _regex(t.origem).search(texto)]


def faltando(termos: list[Termo], traducao: str) -> list[Termo]:
    baixa = traducao.lower()
    return [t for t in termos if t.destino.lower() not in baixa]
