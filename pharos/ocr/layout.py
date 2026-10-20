"""Do OCR (palavras com caixa) para blocos do formato interno.

Etapas: palavras -> linhas -> remoção de cabeçalhos/rodapés/números de página (comparando páginas) ->
tamanho do corpo e espaçamento por página -> classificação (título, nota, corpo) -> agrupamento em parágrafos
(recuo, espaçamento, fim de linha) -> versos. A junção de parágrafos entre páginas fica para a montagem.

Os limites usam o tamanho do texto da própria página (não valores fixos), então servem para qualquer resolução.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher

from ..extratores.comum import RE_CAPITULO, e_separador, parece_verso
from ..limpeza import juntar_linhas, limpar_texto

_PONTUACAO_FINAL = ".!?…:;»\"”’)]*"
_RE_INICIO_NOTA = re.compile(r"^(\d{1,3}|[*†‡¹²³⁴⁵⁶⁷⁸⁹⁰])[\.\)]?\s")
_ZONA_MARGEM = 0.09
_TROCAS = str.maketrans({"ſ": "s"})

# A altura de uma linha oscila 15-20% conforme tenha ou não letras que sobem/descem; por isso o "corpo" é um
# percentil alto das alturas e só diferenças grandes contam como título (maior) ou nota (menor).
_RAZAO_TITULO = 1.3
_RAZAO_NOTA = 0.8
_RAZAO_PASSO_PADRAO = 1.75  # distância entre linhas / altura do texto, quando a página não tem linhas para medir


def limpar_ocr(t: str) -> str:
    return limpar_texto(t.translate(_TROCAS))


def termina_frase(t: str) -> bool:
    return t.rstrip()[-1:] in _PONTUACAO_FINAL


def parece_numero_de_pagina(t: str) -> bool:
    s = re.sub(r"[\s\-–—\(\)\[\]\.\|•·*]", "", t)
    if not s or len(s) > 7:
        return False
    if s.isdigit():
        return len(s) <= 4
    if re.fullmatch(r"[ivxlcdm]+", s, re.IGNORECASE):
        return True
    return bool(re.fullmatch(r"[0-9OolI]{1,4}", s)) and any(c.isdigit() for c in s)


@dataclass
class Linha:
    texto: str
    x0: float
    x1: float
    top: float
    bottom: float
    tam: float
    conf: float
    bloco: int
    par: int
    cat: str = "corpo"


def _percentil_ponderado(pares: list[tuple[float, float]], p: float, padrao: float) -> float:
    if not pares:
        return padrao
    pares = sorted(pares)
    total = sum(w for _, w in pares)
    acum = 0.0
    for v, w in pares:
        acum += w
        if acum >= p * total:
            return v
    return pares[-1][0]


def _percentil(valores: list[float], p: float) -> float:
    v = sorted(valores)
    if not v:
        return 0.0
    return float(v[min(len(v) - 1, max(0, round(p * (len(v) - 1))))])


# ----------------------------------------------------------------------------- palavras -> linhas

def _tamanho_da_linha(palavras: list) -> float:
    """Tamanho do tipo: altura da palavra mais alta da linha (a que tem ascendente e descendente).

    A altura de cada palavra depende das letras que ela tem (x-height, ascendente, descendente), então percentis
    baixos oscilam muito de linha para linha; o máximo é estável. Palavras gigantes por defeito de OCR ficam de fora."""
    alturas = sorted(w[6] for w in palavras if sum(c.isalpha() for c in w[8]) >= 3) or sorted(w[6] for w in palavras)
    mediana = statistics.median(alturas)
    validas = [h for h in alturas if h <= 1.6 * mediana] or alturas
    return float(validas[-1])


def linhas_da_pagina(pag: dict) -> list[Linha]:
    grupos: dict[tuple, list] = {}
    for p in pag["palavras"]:
        grupos.setdefault((p[0], p[1], p[2]), []).append(p)
    linhas = []
    for (b, par, _), ws in grupos.items():
        ws.sort(key=lambda w: w[3])
        texto = limpar_ocr(" ".join(w[8] for w in ws))
        if not texto or (not any(c.isalnum() for c in texto) and not e_separador(texto)):
            continue
        # posição vertical pelo centro das palavras: não muda com letras que sobem/descem nem com inclinação residual
        tam = _tamanho_da_linha(ws)
        cy = statistics.median(w[4] + w[6] / 2 for w in ws)
        linhas.append(Linha(
            texto=texto,
            x0=min(w[3] for w in ws), x1=max(w[3] + w[5] for w in ws),
            top=cy - tam / 2, bottom=cy + tam / 2, tam=tam,
            conf=sum(w[7] for w in ws) / len(ws),
            bloco=b, par=par,
        ))
    return linhas


# ----------------------------------------------------------------------------- cabeçalhos e rodapés

def _norm(t: str) -> str:
    return re.sub(r"[^a-z#]", "", re.sub(r"\d+", "#", t.lower()))


def remover_cabecalhos(paginas: dict[int, tuple[int, int, list[Linha]]]) -> tuple[dict, list[str]]:
    """Tira das margens os números de página e as linhas que se repetem em várias páginas (tolera erros de OCR)."""
    reps: list[list] = []  # [norma, {páginas}, exemplo]
    achados: dict[tuple[int, int], int] = {}  # (página, id da linha) -> índice do grupo
    for n, (_, h, linhas) in paginas.items():
        for l in linhas:
            if not (l.top < _ZONA_MARGEM * h or l.bottom > (1 - _ZONA_MARGEM) * h):
                continue
            norma = _norm(l.texto)
            if len(norma) < 3:
                continue
            for i, r in enumerate(reps):
                if r[0] == norma or SequenceMatcher(None, r[0], norma).ratio() >= 0.82:
                    r[1].add(n)
                    achados[(n, id(l))] = i
                    break
            else:
                reps.append([norma, {n}, l.texto])
                achados[(n, id(l))] = len(reps) - 1
    limite = max(3, int(0.25 * len(paginas)))
    repetidos = {i for i, r in enumerate(reps) if len(r[1]) >= limite}

    removidos: list[str] = []
    saida = {}
    for n, (w, h, linhas) in paginas.items():
        mantidas = []
        for l in linhas:
            na_margem = l.top < _ZONA_MARGEM * h or l.bottom > (1 - _ZONA_MARGEM) * h
            if na_margem and achados.get((n, id(l))) in repetidos:
                removidos.append(reps[achados[(n, id(l))]][2])
                continue
            if na_margem and parece_numero_de_pagina(l.texto):
                removidos.append("(números de página)")
                continue
            if na_margem and l.conf < 35 and len(l.texto) <= 3:  # sujeira da borda do scan
                continue
            mantidas.append(l)
        saida[n] = (w, h, mantidas)
    exemplos: list[str] = []
    for t in removidos:
        if t not in exemplos:
            exemplos.append(t)
    return saida, exemplos


# ----------------------------------------------------------------------------- contexto da página

class _Contexto:
    """Tamanho do corpo, espaçamento e margens de uma página."""

    def __init__(self, linhas: list[Linha], largura: int, altura: int, corpo_padrao: float):
        self.largura, self.altura = largura, altura
        elegiveis = [(l.tam, len(l.texto)) for l in linhas if len(l.texto) >= 8]
        self.corpo = _percentil_ponderado(elegiveis, 0.7, corpo_padrao) if len(elegiveis) >= 6 else corpo_padrao
        deltas = [b.top - a.top for a, b in zip(linhas, linhas[1:])
                  if a.bloco == b.bloco and 0.8 * self.corpo < b.top - a.top < 3.2 * self.corpo]
        self.passo_medido = statistics.median(deltas) if len(deltas) >= 4 else None
        self.passo = self.passo_medido or _RAZAO_PASSO_PADRAO * self.corpo
        self.folga = self._medir_folga(linhas)
        por_bloco: dict[int, list[Linha]] = defaultdict(list)
        for l in linhas:
            por_bloco[l.bloco].append(l)
        self.ref_bloco: dict[int, tuple[float, float, float, float]] = {}
        for b, ls in por_bloco.items():
            if len(ls) >= 3:
                self.ref_bloco[b] = (
                    min(l.x0 for l in ls), max(l.x1 for l in ls),
                    statistics.median(l.x0 for l in ls), _percentil([l.x1 for l in ls], 0.85),
                )

    def _medir_folga(self, linhas: list[Linha]) -> float:
        """Quanto uma linha pode ficar aquém da margem direita e ainda ser considerada 'cheia'.

        Texto justificado: pouco (1,4 × corpo). Texto com margem direita irregular: a variação normal do fim das
        linhas no meio das frases, senão toda frase que termina perto do fim da linha pareceria fim de parágrafo."""
        minimo = 1.4 * self.corpo
        base = [l for l in linhas if len(l.texto) >= 8]
        if len(base) < 6:
            return minimo
        largo = _percentil([l.x1 for l in base], 0.85) - _percentil([l.x0 for l in base], 0.10)
        meio = [l.x1 for l in linhas
                if len(l.texto.split()) >= 4 and not termina_frase(l.texto) and (l.x1 - l.x0) >= 0.55 * largo]
        if len(meio) < 6:
            return minimo
        return max(minimo, _percentil(meio, 0.85) - _percentil(meio, 0.10))

    def _ref_ou_nada(self, l: Linha) -> tuple[float, float] | None:
        if l.bloco in self.ref_bloco:
            _, _, esq, dir_ = self.ref_bloco[l.bloco]
            return esq, dir_
        melhor, sobra = None, 0.0
        for a, b, esq, dir_ in self.ref_bloco.values():
            o = min(b, l.x1) - max(a, l.x0)
            if o > sobra:
                sobra, melhor = o, (esq, dir_)
        if melhor is None or sobra < 0.5 * (l.x1 - l.x0):
            return None
        return melhor

    def ref(self, l: Linha) -> tuple[float, float]:
        """(margem esquerda, margem direita) típicas da coluna em que a linha está."""
        return self._ref_ou_nada(l) or (l.x0, l.x1)

    def coluna(self, l: Linha) -> tuple[float, float]:
        """Como ref, mas sem referência usa a página (para testar centralização de títulos)."""
        return self._ref_ou_nada(l) or (0.12 * self.largura, 0.88 * self.largura)

    def recuo(self, l: Linha) -> float:
        return l.x0 - self.ref(l)[0]


# ----------------------------------------------------------------------------- classificação

def _isolada(linhas: list[Linha], i: int, corpo: float) -> bool:
    antes = linhas[i].top - linhas[i - 1].bottom if i > 0 else 9 * corpo
    depois = linhas[i + 1].top - linhas[i].bottom if i + 1 < len(linhas) else 9 * corpo
    return antes > 1.0 * corpo and depois > 1.0 * corpo


def _classificar_pagina(linhas: list[Linha], ctx: _Contexto) -> None:
    corpo = ctx.corpo
    for l in linhas:
        if len(l.texto) < 120 and l.tam >= _RAZAO_TITULO * corpo:
            l.cat = "titulo"
    por_bloco: dict[int, list[Linha]] = defaultdict(list)
    for l in linhas:
        por_bloco[l.bloco].append(l)
    for ls in por_bloco.values():  # notas de rodapé: bloco inteiro menor que o corpo, na parte de baixo
        c = [l for l in ls if l.cat == "corpo"]
        if (c and statistics.median(l.tam for l in c) <= _RAZAO_NOTA * corpo
                and statistics.median(len(l.texto.split()) for l in c) >= 4
                and min(l.top for l in c) > 0.45 * ctx.altura):
            for l in c:
                l.cat = "nota"
    for i, l in enumerate(linhas):
        if l.cat != "corpo" or len(l.texto) > 60:
            continue
        if RE_CAPITULO.match(l.texto) and len(l.texto.split()) <= 8 and _isolada(linhas, i, corpo):
            l.cat = "titulo"  # "Capítulo 3", "KAPITEL II"...
            continue
        if l.texto[-1:] in ",;:":
            continue
        esq, dir_ = ctx.coluna(l)  # título menor: curto, centrado na coluna e isolado por espaço em branco
        if (abs((esq + dir_) / 2 - (l.x0 + l.x1) / 2) <= 0.04 * ctx.largura
                and (l.x1 - l.x0) <= 0.75 * (dir_ - esq)
                and sum(c.isalpha() for c in l.texto) >= 3 and _isolada(linhas, i, corpo)):
            l.cat = "titulo_menor"


def _nova_paragrafo(ant: Linha, l: Linha, ctx: _Contexto) -> bool:
    corpo = ctx.corpo
    _, dir_a = ctx.ref(ant)
    recuo = ctx.recuo(l)
    cheia = ant.x1 >= dir_a - ctx.folga
    fim = termina_frase(ant.texto)
    dy = l.top - ant.top
    if dy > 1.35 * ctx.passo:
        return True
    if recuo > 0.9 * corpo:
        # linhas seguidas com o mesmo recuo (verso, citação) formam um bloco só
        if ctx.recuo(ant) > 0.9 * corpo and abs(l.x0 - ant.x0) < 0.6 * corpo and 0 < dy:
            return False
        return True
    if l.bloco != ant.bloco:
        return not (cheia and not fim)
    return (not cheia) and fim


def _agrupar(linhas: list[Linha], ctx: _Contexto) -> list[list[Linha]]:
    grupos: list[list[Linha]] = []
    for l in linhas:
        if not grupos:
            grupos.append([l])
            continue
        ant = grupos[-1][-1]
        if l.cat != ant.cat or l.cat == "titulo_menor":
            nova = True
        elif l.cat == "titulo":
            nova = abs(l.tam - ant.tam) > 0.12 * ant.tam or (l.top - ant.bottom) > 0.8 * max(l.tam, ant.tam)
        elif l.cat == "nota":
            nova = bool(_RE_INICIO_NOTA.match(l.texto)) and termina_frase(ant.texto)
        else:
            nova = _nova_paragrafo(ant, l, ctx)
        if nova:
            grupos.append([l])
        else:
            grupos[-1].append(l)
    return grupos


# ----------------------------------------------------------------------------- documento

def _niveis_de_titulo(razoes: list[float]) -> list[float]:
    """Centros dos grupos de razão tamanho/corpo (tolerância de 20%), do maior para o menor."""
    centros: list[float] = []
    for r in sorted(razoes, reverse=True):
        if centros and r >= centros[-1] * 0.8:
            continue
        centros.append(r)
    return centros


def analisar_documento(paginas: dict[int, dict]) -> tuple[dict[int, list[dict]], dict]:
    """paginas: {n: JSON do OCR}. Devolve ({n: [blocos]}, relatório)."""
    linhas_por_pagina = {n: (p["largura"], p["altura"], linhas_da_pagina(p)) for n, p in sorted(paginas.items())}
    todas = [(l.tam, len(l.texto)) for _, _, ls in linhas_por_pagina.values() for l in ls if len(l.texto) >= 8]
    corpo_padrao = _percentil_ponderado(todas, 0.7, 30.0)

    linhas_por_pagina, removidos = remover_cabecalhos(linhas_por_pagina)

    ctxs = {n: _Contexto(ls, w, h, corpo_padrao) for n, (w, h, ls) in linhas_por_pagina.items()}
    razoes_passo = [c.passo_medido / c.corpo for c in ctxs.values() if c.passo_medido and c.corpo]
    if razoes_passo:  # páginas sem linhas suficientes herdam a proporção típica do livro
        razao = statistics.median(razoes_passo)
        for c in ctxs.values():
            if not c.passo_medido:
                c.passo = razao * c.corpo
    for n, (_, _, ls) in linhas_por_pagina.items():
        _classificar_pagina(ls, ctxs[n])

    razoes = [l.tam / ctxs[n].corpo for n, (_, _, ls) in linhas_por_pagina.items() for l in ls
              if l.cat == "titulo" and ctxs[n].corpo > 0 and l.tam >= _RAZAO_TITULO * ctxs[n].corpo]
    centros = _niveis_de_titulo(razoes)

    blocos_por_pagina: dict[int, list[dict]] = {}
    for n, (_, _, linhas) in linhas_por_pagina.items():
        ctx = ctxs[n]
        comprimentos = [len(l.texto) for l in linhas if l.cat == "corpo"]
        mediana_chars = statistics.median(comprimentos) if comprimentos else 0
        blocos: list[dict] = []
        for g in _agrupar(linhas, ctx):
            cat, textos = g[0].cat, [l.texto for l in g]
            recuo = ctx.recuo(g[0]) > 0.9 * ctx.corpo
            if cat == "titulo":
                r = g[0].tam / ctx.corpo if ctx.corpo else 1.0
                if r < _RAZAO_TITULO:  # veio pela regra "Capítulo N"
                    nivel = 1
                else:
                    nivel = 1 + next((i for i, c in enumerate(centros) if r >= c * 0.8), max(0, len(centros) - 1))
                b = {"tipo": "titulo", "texto": juntar_linhas(textos), "nivel": min(nivel, 4)}
            elif cat == "titulo_menor":
                b = {"tipo": "titulo", "texto": juntar_linhas(textos), "nivel": min(len(centros) + 1, 4)}
            elif cat == "nota":
                b = {"tipo": "nota", "texto": juntar_linhas(textos), "nivel": 1}
            elif len(g) == 1 and e_separador(textos[0]):
                b = {"tipo": "separador", "texto": "* * *", "nivel": 1}
            elif parece_verso(textos, mediana_chars):
                b = {"tipo": "verso", "texto": "\n".join(textos), "nivel": 1}
            else:
                b = {"tipo": "paragrafo", "texto": juntar_linhas(textos), "nivel": 1}
            b["recuo"] = recuo
            if b["texto"].strip():
                blocos.append(b)
        blocos_por_pagina[n] = blocos

    return blocos_por_pagina, {"cabecalhos_removidos": removidos[:6], "niveis_de_titulo": len(centros)}
