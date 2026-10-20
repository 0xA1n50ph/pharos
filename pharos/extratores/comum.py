"""Funções compartilhadas pelos extratores."""
from __future__ import annotations

import re

from ..modelo import Bloco, Capitulo, Livro, TITULO


class ErroExtracao(RuntimeError):
    pass


_RE_SEPARADOR = re.compile(r"^[\s\*\-_=~•·\.#]{3,}$")
RE_CAPITULO = re.compile(
    r"^(cap[ií]tulo|chapter|kapitel|chapitre|capitolo|livro|libro|buch|book|teil|parte|part|partie|tomo|abschnitt)\b",
    re.IGNORECASE,
)


def e_separador(texto: str) -> bool:
    t = texto.strip()
    return bool(_RE_SEPARADOR.match(t)) and not any(c.isalnum() for c in t)


def dividir_em_capitulos(blocos: list[Bloco]) -> list[Capitulo]:
    """Abre capítulo a cada título do nível mais alto. Títulos seguidos ficam juntos."""
    if not blocos:
        return []
    niveis = [b.nivel for b in blocos if b.tipo == TITULO]
    if not niveis:
        return [Capitulo(blocos=list(blocos))]
    # capítulo = nível mais alto que aparece 2+ vezes (o título do livro, que aparece uma vez, não conta)
    contagem = {n: niveis.count(n) for n in set(niveis)}
    repetidos = [n for n, c in contagem.items() if c >= 2]
    nivel_cap = min(repetidos) if repetidos else min(niveis)
    capitulos: list[Capitulo] = []
    atual = Capitulo()
    for b in blocos:
        if b.tipo == TITULO and b.nivel == nivel_cap and atual.blocos:
            so_titulos = all(x.tipo == TITULO for x in atual.blocos)
            # só títulos acumulados ficam junto do próximo ("Parte I" + "Capítulo 1"), exceto uma página de
            # rosto, que traz subtítulo/autor em nível mais baixo que o dos capítulos
            rosto = so_titulos and any(x.nivel > nivel_cap for x in atual.blocos)
            if not so_titulos or rosto:
                capitulos.append(atual)
                atual = Capitulo()
        atual.blocos.append(b)
    if atual.blocos:
        capitulos.append(atual)
    return capitulos


def montar_livro(titulo: str, autor: str, idioma: str, capitulos: list[Capitulo]) -> Livro:
    livro = Livro(
        titulo=titulo.strip(),
        autor=autor.strip(),
        idioma=idioma,
        capitulos=[c for c in capitulos if c.blocos],
    )
    livro.renumerar()
    return livro


def parece_verso(linhas: list[str], mediana: float) -> bool:
    """Bloco de 3+ linhas, a maioria bem mais curta que a linha típica do corpo do texto."""
    if len(linhas) < 3 or mediana <= 0:
        return False
    miolo = linhas[:-1]  # a última linha de um parágrafo normal costuma ser curta
    curtas = sum(1 for l in miolo if len(l) < 0.6 * mediana)
    return curtas / len(miolo) >= 0.6
