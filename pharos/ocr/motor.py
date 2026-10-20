"""Executa o OCR páginas afora: em paralelo, com cache por página (retomável) e imagens para a revisão."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from . import preprocessar, tesseract
from .preprocessar import preparar as preparar_pagina
from .paginas import FontePaginas
from .tesseract import ErroOCR

VERSAO = 1  # sobe quando o pré-processamento mudar (invalida o cache)
LIMIAR_REORIENTAR = 55  # confiança média abaixo disso -> tenta detectar página girada


@dataclass
class ParamsOCR:
    lang: str
    psm: int = 3
    dpi: int = 300

    def hash(self) -> str:
        return hashlib.sha1(f"{VERSAO}|{self.lang}|{self.psm}|{self.dpi}".encode()).hexdigest()[:10]


def plano_de_paginas(fonte: FontePaginas, indices: list[int], duplas: bool) -> list[dict]:
    """Páginas lógicas a processar. Com duplas, uma imagem larga vira duas páginas (esquerda, direita)."""
    plano: list[dict] = []
    n = 0
    for i in indices:
        if duplas and preprocessar.e_dupla(*fonte.tamanho(i)):
            for metade in (0, 1):
                n += 1
                plano.append({"n": n, "fonte": i, "metade": metade})
        else:
            n = n + 1 if duplas else i + 1
            plano.append({"n": n, "fonte": i, "metade": None})
    return plano


def proporcao_de_duplas(fonte: FontePaginas, indices: list[int]) -> float:
    if not indices:
        return 0.0
    amostra = indices if len(indices) <= 40 else indices[:: max(1, len(indices) // 40)]
    return sum(1 for i in amostra if preprocessar.e_dupla(*fonte.tamanho(i))) / len(amostra)


def caminho_pagina(dir_ocr: Path, n: int) -> Path:
    return Path(dir_ocr) / f"p{n:04d}.json"


def caminho_imagem(dir_img: Path, n: int) -> Path:
    return Path(dir_img) / f"p{n:04d}.jpg"


def _gravar_json(caminho: Path, dados: dict) -> None:
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, caminho)


def pagina_pronta(dir_ocr: Path, dir_img: Path, n: int, params: ParamsOCR, item: dict | None = None) -> dict | None:
    """Resultado salvo da página n, se foi feito com os mesmos parâmetros (e, com `item`, da mesma imagem/metade)."""
    c = caminho_pagina(dir_ocr, n)
    if not c.exists() or not caminho_imagem(dir_img, n).exists():
        return None
    try:
        d = json.loads(c.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if d.get("hash") != params.hash():
        return None
    if item is not None and (d.get("fonte") != item["fonte"] or d.get("metade") != item["metade"]):
        return None
    return d


def preparar_amostra(fonte: FontePaginas, i: int, dpi: int, destino: Path, metade: int | None = None) -> Path:
    """Renderiza e prepara uma página para testes rápidos de idioma/modelo. Devolve o PNG gravado em `destino`."""
    bruta = fonte.renderizar(i, dpi)
    pre, _ = preparar_pagina(bruta, metade)
    arq = Path(destino) / f"amostra_{i}.png"
    pre.save(arq, dpi=(dpi, dpi), compress_level=1)
    return arq


def processar_pagina(item: dict, fonte: FontePaginas, params: ParamsOCR, exe: str,
                     dir_ocr: Path, dir_img: Path) -> dict:
    pronta = pagina_pronta(dir_ocr, dir_img, item["n"], params, item)
    if pronta is not None:
        return pronta
    t0 = time.monotonic()
    bruta = fonte.renderizar(item["fonte"], params.dpi)
    pre, info = preprocessar.preparar(bruta, item["metade"])
    del bruta

    girada = 0
    with tempfile.TemporaryDirectory(prefix="ocr_") as tmp:
        arq = Path(tmp) / "p.png"
        pre.save(arq, dpi=(params.dpi, params.dpi), compress_level=1)
        palavras = tesseract.rodar_tsv(exe, arq, params.lang, params.psm, params.dpi)
        conf = tesseract.confianca_media(palavras)
        if conf < LIMIAR_REORIENTAR:  # página de lado ou de cabeça para baixo?
            o = tesseract.orientacao(exe, arq)
            if o and o[0] in (90, 180, 270) and o[1] >= 1.5:
                pre2 = preprocessar.girar_horario(pre, o[0])
                pre2.save(arq, dpi=(params.dpi, params.dpi), compress_level=1)
                palavras2 = tesseract.rodar_tsv(exe, arq, params.lang, params.psm, params.dpi)
                if tesseract.confianca_media(palavras2) > conf + 5:
                    pre, palavras, conf, girada = pre2, palavras2, tesseract.confianca_media(palavras2), o[0]

    preprocessar.reduzir_para_revisao(pre).save(caminho_imagem(dir_img, item["n"]), quality=82)
    dados = {
        "n": item["n"], "fonte": item["fonte"], "metade": item["metade"], "hash": params.hash(),
        "lang": params.lang, "largura": pre.width, "altura": pre.height, "angulo": info["angulo"],
        "giro": girada, "tinta": info["tinta"], "conf": round(conf, 1), "palavras": palavras,
        "segundos": round(time.monotonic() - t0, 1),
    }
    _gravar_json(caminho_pagina(dir_ocr, item["n"]), dados)
    return dados


def executar(fonte: FontePaginas, plano: list[dict], params: ParamsOCR, exe: str, dir_ocr: Path, dir_img: Path,
             processos: int = 0, progresso=None) -> tuple[dict[int, dict], dict[int, str]]:
    """Roda o OCR nas páginas do plano que ainda não têm resultado. Devolve ({n: página}, {n: erro})."""
    dir_ocr, dir_img = Path(dir_ocr), Path(dir_img)
    dir_ocr.mkdir(parents=True, exist_ok=True)
    dir_img.mkdir(parents=True, exist_ok=True)
    trabalhadores = processos if processos > 0 else max(1, min(8, (os.cpu_count() or 2) - 1 or 1))
    resultados: dict[int, dict] = {}
    erros: dict[int, str] = {}
    total = len(plano)
    feitos = 0
    ex = ThreadPoolExecutor(max_workers=trabalhadores)
    try:
        futuros = {ex.submit(processar_pagina, it, fonte, params, exe, dir_ocr, dir_img): it for it in plano}
        for fut in as_completed(futuros):
            it = futuros[fut]
            feitos += 1
            try:
                resultados[it["n"]] = fut.result()
            except ErroOCR as e:
                erros[it["n"]] = str(e)
            except Exception as e:  # noqa: BLE001
                erros[it["n"]] = f"{type(e).__name__}: {e}"
            if progresso:
                progresso(feitos, total, it["n"], resultados.get(it["n"]))
    except BaseException:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown(wait=True)
    return resultados, erros


def carregar_paginas(dir_ocr: Path, numeros: list[int]) -> dict[int, dict]:
    saida = {}
    for n in numeros:
        c = caminho_pagina(dir_ocr, n)
        if c.exists():
            saida[n] = json.loads(c.read_text(encoding="utf-8"))
    return saida
