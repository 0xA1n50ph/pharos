"""Extrator de PDF com camada de texto (pdfplumber) e análise do tipo de PDF.

Heurísticas, em ordem: leitura de linhas com tamanho/negrito/posição -> remoção de cabeçalhos,
rodapés e números de página -> classificação (título, nota, corpo) -> agrupamento em parágrafos
(recuo e espaçamento) -> verso -> junção de parágrafos entre páginas -> capítulos.
Limitações conhecidas: livros de duas colunas e tabelas complexas.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ..limpeza import juntar_linhas, limpar_texto
from ..modelo import Bloco, NOTA, PARAGRAFO, SEPARADOR, TITULO, VERSO
from .comum import ErroExtracao, dividir_em_capitulos, e_separador, montar_livro, parece_verso

_ZONA_MARGEM = 0.07
_RE_NUMERO_PAGINA = re.compile(r"^[\-–—\s\(\[]*([0-9]{1,4}|[ivxlcdm]{1,7})[\-–—\s\)\]]*$", re.IGNORECASE)
_RE_INICIO_NOTA = re.compile(r"^(\d{1,3}|[*†‡¹²³⁴⁵⁶⁷⁸⁹⁰])[\.\)]?\s")
_PONTUACAO_FINAL = ".!?…:;»\"”’)]*"
_VOGAIS = set("aeiouyáéíóúàèìòùâêîôûãõäöüëïœæ")


@dataclass
class _Linha:
    texto: str
    tam: float
    negrito: bool
    x0: float
    x1: float
    top: float
    bottom: float
    pagina: int
    cat: str = "corpo"


# ----------------------------------------------------------------------------- análise do tipo de PDF

def qualidade_texto(texto: str) -> float:
    """0..1: proporção de 'palavras' plausíveis. Camadas de OCR ruins ficam abaixo de ~0.7."""
    tokens = texto.split()
    if not tokens:
        return 0.0
    bons = 0
    for t in tokens:
        p = re.sub(r"^\W+|\W+$", "", t)
        if not p or p.isdigit():
            bons += 1
            continue
        letras = [c for c in p if c.isalpha()]
        if len(letras) < 0.7 * len(p):
            continue
        if len(letras) > 3 and not any(c.lower() in _VOGAIS for c in letras):
            continue
        if re.search(r"(.)\1{3,}", p):
            continue
        bons += 1
    return bons / len(tokens)


def _cobertura_imagens(page) -> float:
    area = float(page.width * page.height) or 1.0
    total = 0.0
    for im in page.images:
        w = max(0.0, min(im["x1"], page.width) - max(im["x0"], 0))
        h = max(0.0, min(im["bottom"], page.height) - max(im["top"], 0))
        total += w * h / area
    return min(total, 1.0)


def analisar(caminho: Path, amostra_max: int = 40) -> dict:
    """Classifica o PDF por página (amostrando livros grandes) e recomenda o método de extração."""
    import pdfplumber

    with pdfplumber.open(caminho) as pdf:
        total = len(pdf.pages)
        if total == 0:
            raise ErroExtracao("PDF sem páginas.")
        passo = max(1, total // amostra_max)
        indices = list(range(0, total, passo))[:amostra_max]
        paginas = []
        for i in indices:
            page = pdf.pages[i]
            chars = len(page.chars)
            cob = _cobertura_imagens(page)
            qualidade = None
            if chars < 50 and cob < 0.5:
                tipo = "vazia"
            elif chars < 50:
                tipo = "scan_sem_texto"
            elif cob >= 0.6:
                qualidade = qualidade_texto(page.extract_text() or "")
                tipo = "scan_ocr_bom" if qualidade >= 0.7 else "scan_ocr_ruim"
            else:
                tipo = "texto"
            paginas.append({"pagina": i + 1, "chars": chars, "cobertura": round(cob, 2),
                            "qualidade": None if qualidade is None else round(qualidade, 2), "tipo": tipo})

    cont = Counter(p["tipo"] for p in paginas)
    uteis = sum(v for k, v in cont.items() if k != "vazia")
    n_texto = cont["texto"] + cont["scan_ocr_bom"]
    n_scan = cont["scan_sem_texto"] + cont["scan_ocr_ruim"]
    if uteis == 0:
        tipo_doc, recomendacao = "vazio", "nenhum"
    elif n_texto / uteis >= 0.8:
        tipo_doc, recomendacao = "texto", "extracao_direta"
    elif n_scan / uteis >= 0.8:
        tipo_doc, recomendacao = "scan", "ocr"
    else:
        tipo_doc, recomendacao = "misto", "extracao_direta_e_ocr"

    amostra = f"{len(paginas)} de {total} páginas amostradas" if len(paginas) < total else f"{total} páginas"
    partes = [f"{cont[k]} {nome}" for k, nome in (
        ("texto", "com texto"), ("scan_ocr_bom", "escaneadas com OCR bom"),
        ("scan_ocr_ruim", "escaneadas com OCR ruim"), ("scan_sem_texto", "escaneadas sem texto"),
        ("vazia", "com pouco texto ou só ilustração")) if cont[k]]
    return {
        "total_paginas": total, "amostradas": len(paginas), "paginas": paginas,
        "contagem": dict(cont), "tipo": tipo_doc, "recomendacao": recomendacao,
        "resumo": f"{amostra}: " + ", ".join(partes),
    }


def classificar_paginas(caminho: Path) -> list[str]:
    """Tipo de TODAS as páginas, rápido (pdfium): 'texto' | 'scan' | 'camada' | 'vazia'.

    scan   = imagem cobrindo a página e sem texto (precisa de OCR)
    camada = imagem cobrindo a página com texto embutido (OCR de outra ferramenta)
    vazia  = sem nenhum texto e sem imagem grande (página em branco)
    Páginas com pouco texto (folha de rosto, dedicatória) contam como 'texto'.
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    pdf = pdfium.PdfDocument(str(caminho))
    saida: list[str] = []
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            try:
                w, h = page.get_size()
                tp = page.get_textpage()
                try:
                    chars = tp.count_chars()
                finally:
                    tp.close()
                cob = 0.0
                for obj in page.get_objects(filter=(raw.FPDF_PAGEOBJ_IMAGE,)):
                    l, b, r, t = obj.get_bounds()
                    cob += max(0.0, min(r, w) - max(l, 0.0)) * max(0.0, min(t, h) - max(b, 0.0)) / ((w * h) or 1.0)
                cob = min(cob, 1.0)
            finally:
                page.close()
            if chars == 0 and cob < 0.5:
                saida.append("vazia")
            elif chars < 50 and cob >= 0.5:
                saida.append("scan")
            elif cob >= 0.6:
                saida.append("camada")
            else:
                saida.append("texto")
    finally:
        pdf.close()
    return saida


# ----------------------------------------------------------------------------- extração

def _linhas_da_pagina(page, numero: int) -> list[_Linha]:
    try:
        brutas = page.extract_text_lines(return_chars=True, strip=True)
    except Exception:  # noqa: BLE001
        return []
    linhas = []
    for l in brutas:
        texto = limpar_texto(l["text"])
        chars = [c for c in l.get("chars", []) if c["text"].strip()]
        if not texto or not chars:
            continue
        tam = statistics.fmean(c["size"] for c in chars)
        em_negrito = sum(
            1 for c in chars if re.search(r"bold|black|heavy|demi", c.get("fontname", ""), re.I)
        ) / len(chars) >= 0.6
        linhas.append(_Linha(texto, round(tam, 1), em_negrito, l["x0"], l["x1"], l["top"], l["bottom"], numero))
    return linhas


def _norm(t: str) -> str:
    return re.sub(r"\d+", "#", t.lower()).strip()


def _remover_cabecalhos(paginas: list[tuple[int, float, float, list[_Linha]]]) -> list[str]:
    """Remove linhas repetidas nas margens e números de página; devolve o que foi removido."""
    contagem: Counter = Counter()
    for _, _, h, linhas in paginas:
        vistos = {_norm(l.texto) for l in linhas if l.top < _ZONA_MARGEM * h or l.bottom > (1 - _ZONA_MARGEM) * h}
        contagem.update(vistos)
    limite = max(3, int(0.3 * len(paginas)))
    repetidos = {t for t, c in contagem.items() if c >= limite}
    removidos: set[str] = set()
    for i, (n, w, h, linhas) in enumerate(paginas):
        mantidas = []
        for l in linhas:
            na_margem = l.top < _ZONA_MARGEM * h or l.bottom > (1 - _ZONA_MARGEM) * h
            if na_margem and (_norm(l.texto) in repetidos or _RE_NUMERO_PAGINA.match(l.texto)):
                removidos.add(l.texto if not _RE_NUMERO_PAGINA.match(l.texto) else "(números de página)")
                continue
            mantidas.append(l)
        paginas[i] = (n, w, h, mantidas)
    return sorted(removidos)


def _estatisticas(paginas) -> tuple[float, float, float]:
    """(tamanho do corpo, margem esquerda típica, comprimento mediano de linha do corpo)."""
    tamanhos: Counter = Counter()
    for _, _, _, linhas in paginas:
        for l in linhas:
            tamanhos[round(l.tam * 2) / 2] += len(l.texto)
    if not tamanhos:
        return 11.0, 0.0, 0.0
    corpo = tamanhos.most_common(1)[0][0]
    margens: Counter = Counter()
    comprimentos = []
    for _, _, _, linhas in paginas:
        for l in linhas:
            if abs(l.tam - corpo) <= 0.6:
                margens[round(l.x0 / 2) * 2] += 1
                comprimentos.append(len(l.texto))
    margem = margens.most_common(1)[0][0] if margens else 0.0
    mediana = statistics.median(comprimentos) if comprimentos else 0.0
    return corpo, float(margem), float(mediana)


def _classificar(paginas, corpo: float) -> dict[float, int]:
    """Preenche l.cat e devolve {tamanho de título: nível}."""
    tamanhos_titulo = sorted(
        {round(l.tam * 2) / 2 for _, _, _, ls in paginas for l in ls if l.tam >= corpo * 1.15}, reverse=True
    )
    niveis = {t: i + 1 for i, t in enumerate(tamanhos_titulo)}
    for _, _, h, linhas in paginas:
        for l in linhas:
            if l.tam >= corpo * 1.15:
                l.cat = "titulo"
            elif corpo * 0.5 <= l.tam <= corpo * 0.88 and l.top > 0.5 * h:
                l.cat = "nota"
            elif (l.negrito and l.tam >= corpo * 0.98 and len(l.texto) <= 80
                  and not l.texto.rstrip().endswith((".", ",", ";"))):
                l.cat = "titulo_menor"
            else:
                l.cat = "corpo"
    return niveis


def _termina_frase(t: str) -> bool:
    return t.rstrip()[-1:] in _PONTUACAO_FINAL


def _blocos_da_pagina(linhas: list[_Linha], corpo: float, margem: float, mediana: float,
                      niveis: dict[float, int]) -> tuple[list[Bloco], set[int]]:
    """Devolve os blocos da página e o conjunto de id() dos que começam com recuo de parágrafo."""
    grupos: list[list[_Linha]] = []
    for l in linhas:
        if not grupos:
            grupos.append([l])
            continue
        ant = grupos[-1][-1]
        gap = l.top - ant.bottom
        nova = False
        if l.cat != ant.cat:
            nova = True
        elif l.cat in ("titulo", "titulo_menor"):
            nova = abs(l.tam - ant.tam) > 0.6 or gap > 0.8 * l.tam
        elif l.cat == "nota":
            nova = bool(_RE_INICIO_NOTA.match(l.texto)) and _termina_frase(ant.texto)
        else:
            recuo = l.x0 - margem > 0.8 * corpo
            nova = gap > 0.45 * corpo or (recuo and _termina_frase(ant.texto))
        if nova:
            grupos.append([l])
        else:
            grupos[-1].append(l)

    nivel_menor = len(niveis) + 1
    blocos: list[Bloco] = []
    recuados: set[int] = set()
    for g in grupos:
        cat = g[0].cat
        pagina = g[0].pagina
        textos = [l.texto for l in g]
        if cat == "titulo":
            b = Bloco("", TITULO, juntar_linhas(textos), pagina, niveis.get(round(g[0].tam * 2) / 2, 1))
        elif cat == "titulo_menor":
            b = Bloco("", TITULO, juntar_linhas(textos), pagina, min(nivel_menor, 4))
        elif cat == "nota":
            b = Bloco("", NOTA, juntar_linhas(textos), pagina)
        elif len(g) == 1 and e_separador(textos[0]):
            b = Bloco("", SEPARADOR, "* * *", pagina)
        elif parece_verso(textos, mediana):
            b = Bloco("", VERSO, "\n".join(textos), pagina)
        else:
            b = Bloco("", PARAGRAFO, juntar_linhas(textos), pagina)
        if g[0].x0 - margem > 0.8 * corpo:
            recuados.add(id(b))
        blocos.append(b)
    return blocos, recuados


def _unir_continuacoes(blocos: list[Bloco], recuados: set[int]) -> list[Bloco]:
    """Junta o parágrafo que termina no pé de uma página e continua no topo da seguinte.

    Continuação = o anterior não termina em pontuação de fim de frase e o seguinte começa em
    minúscula ou sem recuo (em alemão, substantivos começam com maiúscula, então só a caixa não basta).
    """
    saida: list[Bloco] = []
    ultimo: Bloco | None = None
    paginas_vistas: set[int] = set()
    for b in blocos:
        primeiro = False
        if b.tipo != NOTA and b.pagina not in paginas_vistas:
            paginas_vistas.add(b.pagina)
            primeiro = True
        if (primeiro and b.tipo == PARAGRAFO and ultimo is not None and ultimo.pagina != b.pagina
                and not _termina_frase(ultimo.texto)
                and (b.texto[:1].islower() or id(b) not in recuados)):
            ultimo.texto = juntar_linhas([ultimo.texto, b.texto])
            continue
        saida.append(b)
        if b.tipo != NOTA:
            ultimo = b if b.tipo == PARAGRAFO else None
    return saida


def extrair_blocos(caminho: Path, paginas: set[int] | None = None, progresso=None, exigir_texto: bool = True):
    """Blocos das páginas com texto selecionável. `paginas` (números a partir de 1) limita a extração.

    Devolve (blocos, recuados, metadados do PDF, relatório); `recuados` = id() dos blocos que começam com recuo."""
    import pdfplumber

    if paginas is not None and not paginas:
        return [], set(), {}, {"total_paginas": 0, "paginas_sem_texto": [], "cabecalhos_removidos": [],
                               "tamanho_corpo": 0.0}
    paginas_lidas: list[tuple[int, float, float, list[_Linha]]] = []
    sem_texto: list[int] = []
    with pdfplumber.open(caminho) as pdf:
        total = len(pdf.pages)
        meta = pdf.metadata or {}
        for i, page in enumerate(pdf.pages, 1):
            if progresso:
                progresso(i, total)
            if paginas is not None and i not in paginas:
                continue
            n = len(page.chars)
            cob = _cobertura_imagens(page) if n < 50 else 0.0
            if n == 0 or (n < 50 and cob >= 0.5):
                if cob >= 0.5:  # imagem cobrindo a página, sem texto: é scan (precisa de OCR)
                    sem_texto.append(i)
                continue  # página em branco
            paginas_lidas.append((i, float(page.width), float(page.height), _linhas_da_pagina(page, i)))

    palavras = sum(len(l.texto.split()) for _, _, _, ls in paginas_lidas for l in ls)
    if exigir_texto and palavras < 100:
        raise ErroExtracao(
            "O PDF quase não tem texto selecionável; parece escaneado. Rode de novo e escolha o OCR."
        )

    cabecalhos = _remover_cabecalhos(paginas_lidas)
    corpo, margem, mediana = _estatisticas(paginas_lidas)
    niveis = _classificar(paginas_lidas, corpo)

    blocos: list[Bloco] = []
    recuados: set[int] = set()
    for _, _, _, linhas in paginas_lidas:
        bl, rec = _blocos_da_pagina(linhas, corpo, margem, mediana, niveis)
        blocos.extend(bl)
        recuados |= rec
    relatorio = {
        "total_paginas": total,
        "paginas_sem_texto": sem_texto,
        "cabecalhos_removidos": cabecalhos,
        "tamanho_corpo": corpo,
    }
    return blocos, recuados, meta, relatorio


def extrair(caminho: Path, progresso=None) -> tuple:
    blocos, recuados, meta, relatorio = extrair_blocos(caminho, None, progresso)
    blocos = _unir_continuacoes(blocos, recuados)
    titulo = (meta.get("Title") or "").strip() or Path(caminho).stem.replace("_", " ")
    autor = (meta.get("Author") or "").strip()
    livro = montar_livro(titulo, autor, "", dividir_em_capitulos(blocos))
    return livro, relatorio
