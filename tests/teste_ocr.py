#!/usr/bin/env python3
"""Testes do OCR e da tela de revisão.

Uso:  python tests/teste_ocr.py
Precisa do Tesseract (com 'eng' e, de preferência, 'osd') e de: pillow, numpy, pypdfium2, reportlab.
Os testes com Tesseract de verdade usam inglês; a lógica de alemão/letra gótica é testada com Tesseract simulado.
O teste da tela no navegador usa Playwright (pulado se não estiver instalado).
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import io
import json
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "tests"))
PY = sys.executable
CLI = str(RAIZ / "pharos.py")

import amostras  # noqa: E402

falhas = 0


def ok(cond: bool, texto: str) -> None:
    global falhas
    print(("  ok    " if cond else "  FALHOU ") + texto)
    if not cond:
        falhas += 1


def similaridade(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def rodar(args: list[str], cwd: Path, entrada: str | None = None, timeout: int = 300):
    r = subprocess.run([PY, CLI, *args], input=entrada, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def api(url: str, caminho: str, corpo: dict | None = None):
    """Chama a API da tela de revisão a partir da URL impressa pelo programa (http://127.0.0.1:PORTA/?t=TOKEN)."""
    m = re.match(r"(http://127\.0\.0\.1:\d+)/\?t=(\S+)", url)
    req = urllib.request.Request(m.group(1) + caminho, headers={"X-Token": m.group(2)},
                                 data=None if corpo is None else json.dumps(corpo).encode())
    if corpo is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def rodar_interativo(args: list[str], cwd: Path, acao, entrada: str = "\n" * 40, timeout: int = 300):
    """Roda o programa com respostas padrão (Enter) e, quando a tela de revisão abre, chama acao(url, processo)."""
    # SIGINT volta ao padrão: shells não interativos (CI, segundo plano) o herdam como "ignorado" e o Ctrl+C simulado não chegaria
    p = subprocess.Popen([PY, CLI, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, cwd=cwd, preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    linhas: list[str] = []
    threading.Thread(target=lambda: [linhas.append(l) for l in p.stdout], daemon=True).start()
    p.stdin.write(entrada)
    p.stdin.close()
    url, fim = None, time.time() + timeout
    while time.time() < fim and p.poll() is None and not url:
        for l in list(linhas):
            m = re.search(r"Tela de revisão: (http://\S+)", l)
            if m:
                url = m.group(1)
        time.sleep(0.2)
    if url and acao:
        acao(url, p)
    try:
        p.wait(timeout=60 if url else timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        linhas.append("\n[teste] o programa não terminou a tempo e foi encerrado\n")
    time.sleep(0.3)
    return p.returncode, "".join(linhas), url


def paragrafos(livro_json: Path) -> list[str]:
    d = json.loads(livro_json.read_text(encoding="utf-8"))
    return [b["texto"] for c in d["capitulos"] for b in c["blocos"] if b["tipo"] == "paragrafo"]


def blocos_tipo(livro_json: Path, tipo: str) -> list[str]:
    d = json.loads(livro_json.read_text(encoding="utf-8"))
    return [b["texto"] for c in d["capitulos"] for b in c["blocos"] if b["tipo"] == tipo]


def mesclar_pdfs(destino: Path, partes: list[tuple[Path, list[int]]]) -> None:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    fontes = []
    for pdf, idx in partes:
        src = pdfium.PdfDocument(str(pdf))
        fontes.append(src)
        doc.import_pages(src, idx)
    doc.save(str(destino))
    doc.close()
    for f in fontes:
        f.close()


def main() -> int:
    from pharos.ocr import tesseract

    try:
        exe = tesseract.localizar()
        instalados = tesseract.idiomas_instalados(exe)
    except Exception as e:  # noqa: BLE001
        print(f"Tesseract não encontrado ({e}); testes de OCR pulados.")
        return 0
    if "eng" not in instalados:
        print("O Tesseract não tem o idioma 'eng'; testes de OCR pulados.")
        return 0
    tem_osd = "osd" in instalados

    tmp = Path(tempfile.mkdtemp(prefix="tradutor_ocr_"))
    am = tmp / "amostras"
    am.mkdir()
    amostras.gerar_pdf(am / "lighthouse.pdf", "recuo", "en")
    amostras.gerar_scan(am / "lighthouse.pdf", am / "scan.pdf")
    esperado = amostras.texto_esperado_en()
    esperado_junto = " ".join(esperado)
    print(f"Pasta de teste: {tmp}\n")

    # ------------------------------------------------------------------ A. pré-processamento
    print("A. pré-processamento: inclinação, página em branco, páginas duplas")
    from PIL import Image

    from pharos.ocr import preprocessar as pp

    base = amostras.renderizar_pdf(am / "lighthouse.pdf", 200, [1])[0]
    for ang in (-2.5, -1.0, 0.6, 1.4, 3.2):
        d = amostras.degradar(base, ang, 6.0, seed=1)
        est = pp.estimar_inclinacao(pp.uniformizar_iluminacao(pp.cinza(d)))
        ok(abs(est + ang) <= 0.3, f"inclinação de {ang:+.1f}° estimada como {-est:+.2f}°")
    ok(pp.estimar_inclinacao(Image.new("L", (1200, 1600), 255)) == 0.0, "página em branco não é girada")
    larga = Image.new("L", (2 * 1000 + 60, 1400), 120)
    larga.paste(base.resize((1000, 1400)), (0, 0))
    larga.paste(base.resize((1000, 1400)), (1060, 0))
    ok(pp.e_dupla(*larga.size) and not pp.e_dupla(1000, 1400), "imagem larga reconhecida como dupla; página normal não")
    esq, dire = pp.dividir_dupla(pp.cinza(larga), 0), pp.dividir_dupla(pp.cinza(larga), 1)
    ok(960 <= esq.width <= 1005 and 960 <= dire.width <= 1005,
       f"dupla com sombra na lombada: cada metade fica só com a sua página ({esq.width} e {dire.width} px)")
    sem_sombra = Image.new("L", (2 * 1000 + 120, 1400), 255)
    sem_sombra.paste(base.resize((1000, 1400)), (0, 0))
    sem_sombra.paste(base.resize((1000, 1400)), (1120, 0))
    e2, d2 = pp.dividir_dupla(pp.cinza(sem_sombra), 0), pp.dividir_dupla(pp.cinza(sem_sombra), 1)
    ok(1000 <= e2.width <= 1120 and 1000 <= d2.width <= 1120 and abs(e2.width - 1060) <= 40,
       f"dupla sem sombra: corta no meio do vão ({e2.width} e {d2.width} px)")

    # ------------------------------------------------------------------ B. qualidade, sugestões, edições
    print("\nB. qualidade, sugestões, edições e substituição global")
    from pharos.ocr import qualidade
    from pharos.ocr.revisao import Revisao, Sessao, _limpar_bloco

    def palavra(txt, conf, x=10):
        return [1, 1, 1, x, 10, 40, 12, conf, txt]

    boas = [palavra(w, 95) for w in "the keeper climbed the old stair every night".split()] * 3
    pag_boa = {"n": 1, "palavras": boas, "tinta": 0.1, "largura": 1000, "altura": 1400}
    pag_ruim = {"n": 2, "palavras": [palavra(w, 40) for w in "tbe kecper clinbed tbe o1d stalr evry n1ght".split()] * 3,
                "tinta": 0.1, "largura": 1000, "altura": 1400}
    pag_muda = {"n": 3, "palavras": [], "tinta": 0.001, "largura": 1000, "altura": 1400}
    pag_tinta = {"n": 4, "palavras": [], "tinta": 0.15, "largura": 1000, "altura": 1400}
    pts = qualidade.pontuar_todas({1: pag_boa, 2: pag_ruim, 3: pag_muda, 4: pag_tinta})
    ok(not pts[1]["sinalizada"] and pts[2]["sinalizada"], "página boa passa; página de confiança baixa é sinalizada")
    ok(not pts[3]["sinalizada"], "página em branco não é sinalizada")
    ok(pts[4]["sinalizada"], "página com imagem mas sem texto reconhecido é sinalizada")
    ok(qualidade.resumir(pts)["sinalizadas"] == 2, "resumo conta as sinalizadas")
    paginas_s = {
        1: {"n": 1, "palavras": [palavra("lighthouse", 96)] * 6 + [palavra("lighthonse", 41)], "tinta": .1, "largura": 1000, "altura": 1400},
        2: {"n": 2, "palavras": [palavra("down", 95)] * 5 + [palavra("dawn", 82)], "tinta": .1, "largura": 1000, "altura": 1400},
    }
    sug = qualidade.gerar_sugestoes(paginas_s)
    ok(any(s["de"] == "lighthonse" and s["para"] == "lighthouse" for s in sug), "sugere lighthonse → lighthouse")
    ok(not any(s["de"] == "dawn" for s in sug), "não 'corrige' palavra legítima lida com boa confiança (dawn)")
    ok(_limpar_bloco({"tipo": "xyz", "texto": "  a   b \n c ", "nivel": 99}) == {"tipo": "paragrafo", "texto": "a b c", "nivel": 4, "recuo": True},
       "bloco editado é saneado (tipo, espaços, nível)")
    ok(_limpar_bloco({"tipo": "paragrafo", "texto": "   "}) is None, "bloco vazio é descartado")
    auto = {1: [{"tipo": "paragrafo", "texto": "The man and the woman saw the lighthouse.", "nivel": 1, "recuo": True}],
            2: [{"tipo": "paragrafo", "texto": "Lighthouse keepers; a Lighthouse.", "nivel": 1, "recuo": True}]}
    rev_json = tmp / "rev_teste.json"
    ses = Sessao("T", paginas_s, auto, qualidade.pontuar_todas(paginas_s), sug, Revisao(rev_json, "h1"), tmp)
    ok(ses.substituir("man", "men")["ocorrencias"] == 1, "'man' só como palavra inteira (não troca 'woman')")
    ok(ses.substituir("lighthouse", "farol", True, True)["ocorrencias"] == 3, "ignorar maiúsculas conta as 3 ocorrências")
    ok(ses.substituir("lighthouse", "farol", True, False)["ocorrencias"] == 1, "com maiúsculas conta só 1")
    ses.substituir("lighthouse", "farol", True, True, aplicar=True)
    ok("farol" in ses.blocos(1)[0]["texto"] and ses.revisao.editada(1) and ses.revisao.editada(2), "aplicar grava as páginas alteradas")
    ses.marcar(2, True)
    ok(Revisao(rev_json, "h1").revisada(2) and Revisao(rev_json, "h1").editada(1), "edições e revisão persistem em disco")
    Revisao(rev_json, "h2")
    ok(rev_json.with_suffix(".antigo.json").exists() and not rev_json.exists(), "edições de um OCR com outros parâmetros são postas de lado")

    # ------------------------------------------------------------------ C. idioma, letra gótica, rota do PDF (Tesseract simulado)
    print("\nC. escolha de idioma, letra gótica e rota do PDF (Tesseract simulado)")
    from pharos import ler_args
    from pharos import etapa_ocr
    from pharos.config import Config
    from pharos.extratores import pdf_texto
    from pharos.extratores.comum import ErroExtracao
    from pharos.projeto import Projeto
    from pharos.ui import UI

    class FonteFalsa:
        total = 10

        def tamanho(self, i):
            return (600, 800)

    def conf_por_idioma(tabela):
        def falso(exe, imagem, lang, psm=3, dpi=300, timeout=600):
            texto = ("der die und nicht ist mit von zu für auf das ein eine wird nach auch aber wie oder dass "
                     "sich noch nur sind war haben")
            return [[1, 1, 1, i * 10, 0, 9, 9, tabela.get(lang, 50), w] for i, w in enumerate(texto.split())]
        return falso

    salvo = (tesseract.idiomas_instalados, tesseract.rodar_tsv, etapa_ocr.motor.preparar_amostra)
    etapa_ocr.motor.preparar_amostra = lambda fonte, i, dpi, dest, metade=None: Path(dest) / f"a{i}.png"

    def cenario(instalados, confs, args_extra=(), cfg=None):
        tesseract.idiomas_instalados = lambda exe: frozenset(instalados)
        tesseract.rodar_tsv = conf_por_idioma(confs)
        args = ler_args(["x", *args_extra])
        cfg = cfg or Config()
        pasta = Path(tempfile.mkdtemp(dir=tmp))
        cfg.salvar(pasta / "config.json")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = etapa_ocr.definir_idioma(FonteFalsa(), list(range(10)), lambda i: None, "tess", Projeto(pasta), cfg,
                                           UI(auto=True), args)
        return res, buf.getvalue(), cfg

    try:
        (spec, iso), saida, cfg = cenario({"deu", "frk", "eng", "osd"}, {"deu": 45, "frk": 82}, ["--origem", "de"])
        ok(spec == "frk" and iso == "de", f"livro em Fraktur: o modelo gótico ganha (escolhido: {spec})")
        ok(cfg.ocr_idioma == "frk" and cfg.origem == "de", "escolha guardada na configuração do projeto")
        (spec, iso), saida, _ = cenario({"deu", "frk", "eng"}, {"deu": 90, "frk": 80}, ["--origem", "de"])
        ok(spec == "deu", "letra normal: continua com o modelo comum")
        (spec, iso), saida, _ = cenario({"deu", "frk", "eng"}, {"deu": 55, "frk": 58}, ["--origem", "de"])
        ok(spec == "deu", "diferença pequena não troca de modelo")
        (spec, iso), saida, _ = cenario({"deu", "deu_latf", "eng"}, {"deu": 40, "deu_latf": 85}, ["--origem", "de"])
        ok(spec == "deu_latf", "também considera o modelo deu_latf")
        (spec, iso), saida, _ = cenario({"deu", "eng"}, {"deu": 40}, ["--origem", "de"])
        ok(spec == "deu" and "gótica" in saida and "frk" in saida, "sem modelo gótico instalado e confiança baixa: avisa como instalar")
        (spec, iso), saida, cfg = cenario({"deu", "frk", "eng"}, {}, ["--ocr-lang", "frk"])
        ok(spec == "frk" and iso == "de", "--ocr-lang frk é respeitado e implica alemão")
        (spec, iso), saida, _ = cenario({"deu", "eng"}, {}, ["--ocr-lang", "deu+eng", "--origem", "de"])
        ok(spec == "deu+eng", "vários idiomas juntos (deu+eng)")
        cfg_prev = Config(ocr_idioma="frk", origem="de")
        chamadas = []
        tesseract.rodar_tsv = lambda *a, **k: chamadas.append(1) or []
        tesseract.idiomas_instalados = lambda exe: frozenset({"deu", "frk"})
        with contextlib.redirect_stdout(io.StringIO()):
            r = etapa_ocr.definir_idioma(FonteFalsa(), list(range(10)), lambda i: None, "tess", Projeto(tmp), cfg_prev,
                                         UI(auto=True), ler_args(["x"]))
        ok(r == ("frk", "de") and not chamadas, "execução retomada usa a escolha anterior sem refazer testes")
        try:
            cenario({"eng"}, {}, ["--origem", "ru"])
            ok(False, "idioma sem modelo instalado deveria dar erro")
        except ErroExtracao as e:
            ok("rus" in str(e) and "não tem o idioma" in str(e), "idioma sem modelo instalado: erro claro")
        # detecção do idioma por OCR de amostra (texto em alemão)
        (spec, iso), saida, _ = cenario({"deu", "eng", "fra", "osd"}, {"deu": 90, "eng": 90, "fra": 90}, [])
        ok(iso == "de" and "alemão" in saida, f"detecta o idioma pelo texto reconhecido (detectou: {iso})")
    finally:
        tesseract.idiomas_instalados, tesseract.rodar_tsv, etapa_ocr.motor.preparar_amostra = salvo

    original_classificar = pdf_texto.classificar_paginas

    def rota(classes, extra=(), auto=True, cfg=None):
        pdf_texto.classificar_paginas = lambda caminho: classes
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                return etapa_ocr.rotear_pdf(Path("x.pdf"), cfg or Config(), UI(auto=auto), ler_args(["x", *extra]))
        finally:
            pdf_texto.classificar_paginas = original_classificar

    ok(rota(["texto"] * 10) is None, "PDF só de texto: extração direta")
    r = rota(["scan"] * 10)
    ok(r and r["ocr"] == list(range(10)) and not r["texto"], "PDF só de scan: OCR em todas")
    r = rota(["texto"] * 5 + ["scan"] * 5)
    ok(r and r["ocr"] == [5, 6, 7, 8, 9] and r["texto"] == {1, 2, 3, 4, 5}, "PDF misto: OCR nas de imagem, texto direto nas outras")
    ok(rota(["scan"] + ["texto"] * 19) is None, "livro de texto com capa em imagem: não dispara OCR (modo automático)")
    ok(rota(["scan", "scan", "texto", "texto", "texto"]) is None, "duas páginas de imagem num livro de texto: ficam de fora")
    r = rota(["scan"] + ["texto"] * 19, ["--ocr-imagens"])
    ok(r and r["ocr"] == [0] and len(r["texto"]) == 19, "--ocr-imagens lê com OCR as páginas só de imagem")
    r = rota(["scan"] * 3 + ["texto"] * 7)
    ok(r and r["ocr"] == [0, 1, 2], "três ou mais páginas de imagem (30%): OCR nelas")
    r = rota(["scan"] * 2)
    ok(r and r["ocr"] == [0, 1], "PDF pequeno só de imagens: OCR")
    r = rota(["camada"] * 10)
    ok(r and r["ocr"] == list(range(10)), "scan com camada de OCR embutida: refaz o OCR por padrão")
    ok(rota(["camada"] * 10, ["--usar-camada-embutida"]) is None, "--usar-camada-embutida usa o texto embutido")
    try:
        rota(["vazia"] * 4)
        ok(False, "PDF sem conteúdo deveria dar erro")
    except ErroExtracao:
        ok(True, "PDF sem texto nem imagens: recusa, a menos que o usuário peça OCR")

    # ------------------------------------------------------------------ D. de ponta a ponta com Tesseract de verdade
    print("\nD. de ponta a ponta com OCR de verdade (inglês)")
    comuns = ["--sim", "--origem", "en", "--destino", "pt", "--revisar", "nenhuma", "--sem-previa", "--exportar-apenas"]

    t0 = time.time()
    cod, out = rodar([str(am / "scan.pdf"), "--saida", str(tmp / "d1"), *comuns], tmp)
    proj = tmp / "d1" / "scan"
    livro = proj / "extraido" / "livro.json"
    ok(cod == 0, f"scan em PDF: termina com código 0 (veio {cod}; {time.time() - t0:.0f}s)")
    if cod != 0:
        print(out[-1500:])
    ps = paragrafos(livro) if livro.exists() else []
    ok(len(ps) == len(esperado), f"{len(esperado)} parágrafos reconhecidos (vieram {len(ps)})")
    sim = similaridade(" ".join(ps), esperado_junto)
    ok(sim >= 0.985, f"texto fiel ao original: {sim:.2%} de similaridade (mínimo 98,5%)")
    ok(not any("The Lighthouse" in p for p in ps), "cabeçalhos repetidos removidos do corpo")
    ok(not any(re.fullmatch(r"\d{1,3}", p.strip()) for p in ps), "números de página removidos")
    ok(len(blocos_tipo(livro, "verso")) == 1 and "keeper rests unseen" in blocos_tipo(livro, "verso")[0], "poema reconhecido como verso, com as quebras")
    ok(len(blocos_tipo(livro, "nota")) == 1 and "coastal administration" in blocos_tipo(livro, "nota")[0], "nota de rodapé reconhecida")
    caps = json.loads(livro.read_text(encoding="utf-8"))["capitulos"]
    ok(len(caps) == 4 and caps[0]["blocos"][0]["texto"] == "The Lighthouse", f"folha de rosto + 3 capítulos (vieram {len(caps)})")
    estado = json.loads((proj / "estado.json").read_text(encoding="utf-8"))["etapas"]
    cfg = json.loads((proj / "config.json").read_text(encoding="utf-8"))
    ok(estado["extracao"] and estado["ocr_revisada"] and estado["idioma"], "etapas de extração, OCR e idioma marcadas")
    ok(cfg["origem"] == "en" and cfg["ocr_idioma"] == "eng", "idioma e modelo do OCR guardados no projeto")
    ok((proj / "revisao" / "ocr_qualidade.md").exists() and len(list((proj / "ocr").glob("p*.json"))) == 6, "relatório de qualidade e OCR por página salvos")
    ok(len(list((proj / "paginas").glob("p*.jpg"))) == 6, "imagens das páginas guardadas para a revisão")
    ok(any((proj / "saida").glob("*.epub")), "exportou o EPUB do livro escaneado")
    t0 = time.time()
    cod, out = rodar([str(am / "scan.pdf"), "--saida", str(tmp / "d1"), *comuns], tmp)
    ok(cod == 0 and "Retomando" in out and "OCR pronto" not in out, "rodar de novo retoma sem refazer o OCR")

    # imagens soltas em uma pasta, uma delas de cabeça para baixo
    pasta_img = tmp / "paginas_do_scan"
    amostras.gerar_imagens(am / "lighthouse.pdf", pasta_img)
    if tem_osd:
        p3 = pasta_img / "pagina_003.jpg"
        Image.open(p3).rotate(180).save(p3, quality=75)
    cod, out = rodar([str(pasta_img), "--saida", str(tmp / "d2"), *comuns], tmp)
    proj2 = tmp / "d2" / "paginas-do-scan-livro"
    ok(cod == 0 and proj2.exists(), f"pasta de imagens: termina com código 0 e cria o projeto (código {cod})")
    if cod != 0:
        print(out[-1500:])
    ps2 = paragrafos(proj2 / "extraido" / "livro.json") if proj2.exists() else []
    sim2 = similaridade(" ".join(ps2), esperado_junto)
    ok(len(ps2) == len(esperado) and sim2 >= 0.97,
       f"pasta de imagens{' (uma de cabeça para baixo)' if tem_osd else ''}: {len(ps2)} parágrafos, {sim2:.2%} de similaridade")
    ok(len(list((proj2 / "original").glob("*.jpg"))) == 6, "as imagens foram copiadas para o projeto")
    cod, out = rodar([str(pasta_img), "--saida", str(tmp / "d2"), *comuns], tmp)
    ok(cod == 0 and "Retomando" in out and not (tmp / "d2" / "paginas-do-scan-livro-2").exists(), "a mesma pasta de imagens reabre o projeto (não duplica)")

    # duas páginas por imagem
    amostras.gerar_scan(am / "lighthouse.pdf", am / "duplas.pdf", duplas=True)
    cod, out = rodar([str(am / "duplas.pdf"), "--saida", str(tmp / "d3"), *comuns], tmp)
    proj3 = tmp / "d3" / "duplas"
    ps3 = paragrafos(proj3 / "extraido" / "livro.json") if (proj3 / "extraido" / "livro.json").exists() else []
    ok(cod == 0 and "duas páginas em cada imagem" in out, "detecta imagens com duas páginas e propõe separar")
    ok(len(list((proj3 / "ocr").glob("p*.json"))) == 6, "3 imagens viraram 6 páginas")
    ok(similaridade(" ".join(ps3), esperado_junto) >= 0.97 and len(ps3) == len(esperado),
       f"texto das páginas duplas na ordem certa ({similaridade(' '.join(ps3), esperado_junto):.2%})")
    cod, out = rodar([str(am / "duplas.pdf"), "--saida", str(tmp / "d3"), *comuns, "--ocr-forcar", "--paginas-duplas", "nao"], tmp)
    ok(cod == 0 and "modo de páginas duplas mudou" in out and len(list((proj3 / "ocr").glob("p*.json"))) == 3,
       "mudar o modo de páginas duplas refaz o OCR com a nova numeração (sem restos do anterior)")

    # PDF misto: 6 páginas com texto seguidas de 6 escaneadas (o mesmo livro duas vezes)
    mesclar_pdfs(am / "misto.pdf", [(am / "lighthouse.pdf", list(range(6))), (am / "scan.pdf", list(range(6)))])
    sem_origem = [a for a in comuns if a not in ("--origem", "en")]
    cod, out = rodar([str(am / "misto.pdf"), "--saida", str(tmp / "d4"), *sem_origem], tmp)
    proj4 = tmp / "d4" / "misto"
    ps4 = paragrafos(proj4 / "extraido" / "livro.json") if (proj4 / "extraido" / "livro.json").exists() else []
    ps4 = [p for p in ps4 if len(p) > 40]  # ignora linhas curtas da folha de rosto
    ok(cod == 0 and "OCR em 6 página(s) e leitura direta do texto em 6" in out, "PDF misto: plano com OCR e texto direto")
    ok("Idioma detectado pelo texto selecionável" in out and "inglês" in out, "PDF misto: o idioma vem do texto selecionável, sem OCR de amostra")
    ok(len(list((proj4 / "ocr").glob("p*.json"))) == 6, "OCR só nas 6 páginas escaneadas")
    sim4 = similaridade(" ".join(ps4), " ".join(esperado + esperado))
    ok(len(ps4) == 2 * len(esperado) and sim4 >= 0.985, f"misto: texto das duas origens, cada uma no seu lugar ({len(ps4)} parágrafos, {sim4:.2%})")
    ok(bool(ps4) and ps4[0].startswith("The wind came") and ps4[len(esperado)].startswith("The wind came"), "misto: primeiro o trecho em texto, depois o escaneado")

    # PDF escaneado com camada de texto embutida (gerado pelo próprio Tesseract)
    camada_ok = False
    try:
        partes = []
        imgs = amostras.renderizar_pdf(am / "scan.pdf", 200, [1, 2, 3])
        for k, im in enumerate(imgs):
            png = am / f"c{k}.png"
            im.save(png)
            r = subprocess.run([exe, str(png), str(am / f"c{k}"), "-l", "eng", "pdf"], capture_output=True, text=True)
            if r.returncode == 0 and (am / f"c{k}.pdf").exists():
                partes.append((am / f"c{k}.pdf", [0]))
        if len(partes) == 3:
            mesclar_pdfs(am / "camada.pdf", partes)
            camada_ok = pdf_texto.classificar_paginas(am / "camada.pdf") == ["camada"] * 3
    except Exception:  # noqa: BLE001
        camada_ok = False
    if camada_ok:
        cod, out = rodar([str(am / "camada.pdf"), "--saida", str(tmp / "d5"), *comuns], tmp)
        ok(cod == 0 and "camada de texto" in out and "OCR pronto" in out, "scan com texto embutido: refaz o OCR por padrão e avisa")
        cod, out = rodar([str(am / "camada.pdf"), "--saida", str(tmp / "d6"), *comuns, "--usar-camada-embutida"], tmp)
        ok(cod == 0 and "OCR pronto" not in out and "Extraindo o conteúdo" in out, "--usar-camada-embutida extrai o texto embutido sem OCR")
    else:
        print("  (pulado: o Tesseract desta máquina não gerou PDF com camada de texto)")

    # ------------------------------------------------------------------ E. revisão: página ruim, tela, edição, pausa, retomada
    print("\nE. revisão do OCR: página ruim sinalizada, tela de revisão, pausa e retomada")
    pasta_ruim = tmp / "scan_ruim"
    pasta_ruim.mkdir()
    for i, im in enumerate(amostras.renderizar_pdf(am / "lighthouse.pdf", 200), 1):
        d = (amostras.degradar(im, 1.0, 60.0, desfoque=2.2, seed=3) if i == 3 else amostras.degradar(im, 1.0, 5.0, seed=i))
        d.save(pasta_ruim / f"pagina_{i:03d}.jpg", quality=75)
    args_e = [str(pasta_ruim), "--saida", str(tmp / "e"), "--origem", "en", "--destino", "pt", "--sem-navegador",
              "--sem-previa", "--exportar-apenas"]
    proj_e = tmp / "e" / "scan-ruim-livro"
    estado_tela: dict = {}

    def pausar(url, p):
        idx = api(url, "/api/indice")
        estado_tela["sinalizadas"] = [x["n"] for x in idx["paginas"] if x["sinalizada"]]
        estado_tela["filtro"] = idx["filtro"]
        pg = api(url, "/api/pagina/3")
        estado_tela["caixas3"] = len(pg["caixas"])
        p.send_signal(signal.SIGINT)

    cod, out, url = rodar_interativo(args_e, tmp, pausar)
    ok(url is not None and "127.0.0.1" in url, "a tela de revisão abre (endereço local com token)")
    ok(3 in estado_tela.get("sinalizadas", []), f"a página degradada (3) está entre as sinalizadas: {estado_tela.get('sinalizadas')}")
    ok(len(estado_tela.get("sinalizadas", [])) < 6 and estado_tela.get("filtro") == "sinalizadas", "a lista inicial mostra só as sinalizadas")
    ok(estado_tela.get("caixas3", 0) > 0, f"a página ruim traz caixas nas palavras duvidosas ({estado_tela.get('caixas3')})")
    ok(cod == 0 and "Saindo" in out, "Ctrl+C na revisão pausa sem erro")
    est = json.loads((proj_e / "estado.json").read_text(encoding="utf-8"))["etapas"]
    ok(not est["extracao"] and len(list((proj_e / "ocr").glob("p*.json"))) == 6, "pausa: extração não marcada, OCR salvo")

    marca = "TRECHO-REVISADO-NO-TESTE"

    def editar(url, p):
        idx = api(url, "/api/indice")
        pg = api(url, "/api/pagina/3")
        blocos = pg["blocos"]
        paragrafo = next(b for b in blocos if b["tipo"] == "paragrafo")
        paragrafo["texto"] = marca + " " + paragrafo["texto"]
        r = api(url, "/api/pagina/3", {"blocos": blocos, "revisada": True})
        estado_tela["salvou"] = r["indice"]["revisada"] and r["indice"]["editada"]
        cont = api(url, "/api/substituir", {"de": "lighthouse", "para": "farol", "palavra_inteira": True,
                                           "ignorar_caixa": True, "aplicar": False})
        estado_tela["conta"] = cont["ocorrencias"]
        api(url, "/api/substituir", {"de": "lighthouse", "para": "farol", "palavra_inteira": True,
                                     "ignorar_caixa": True, "aplicar": True})
        for x in idx["paginas"]:
            if x["sinalizada"]:
                api(url, f"/api/marcar/{x['n']}", {"revisada": True})
        api(url, "/api/concluir", {})

    cod, out, url = rodar_interativo(args_e, tmp, editar)
    ok(cod == 0 and "Retomando" in out and "já feitas antes" in out, "retomada: reabre a revisão sem refazer o OCR")
    ok(estado_tela.get("salvou") is True and estado_tela.get("conta", 0) >= 2, f"edição salva e substituição global contou {estado_tela.get('conta')}")
    livro_e = proj_e / "extraido" / "livro.json"
    ps_e = paragrafos(livro_e)
    ok(any(marca in p for p in ps_e), "a correção feita na tela chegou ao livro final")
    ok(not any(re.search(r"\blighthouse\b", p, re.I) for p in ps_e) and any("farol" in p for p in ps_e),
       "a substituição global valeu para o livro inteiro")
    ok(abs(len(ps_e) - len(esperado)) <= 3, f"a estrutura do livro continua coerente apesar da página ruim ({len(ps_e)} parágrafos para {len(esperado)})")
    rel = (proj_e / "revisao" / "ocr_qualidade.md").read_text(encoding="utf-8")
    ok("Revisão feita na tela: sim" in rel and "| 3 |" in rel, "relatório registra a revisão e a página 3")
    est = json.loads((proj_e / "estado.json").read_text(encoding="utf-8"))["etapas"]
    ok(est["extracao"] and est["ocr_revisada"], "após concluir a revisão, a extração é marcada como pronta")

    # reabrir a revisão de um projeto já extraído (--revisar-ocr)
    marca2 = "SEGUNDA-REVISAO"

    def editar2(url, p):
        pg = api(url, "/api/pagina/2")
        b = next(x for x in pg["blocos"] if x["tipo"] == "paragrafo")
        b["texto"] = marca2 + " " + b["texto"]
        api(url, "/api/pagina/2", {"blocos": pg["blocos"], "revisada": True})
        api(url, "/api/concluir", {})

    cod, out, url = rodar_interativo([str(proj_e), "--sem-navegador", "--sem-previa", "--exportar-apenas", "--revisar-ocr"], tmp, editar2)
    ps_e2 = paragrafos(livro_e)
    ok(cod == 0 and any(marca2 in p for p in ps_e2) and any(marca in p for p in ps_e2),
       "--revisar-ocr reabre a revisão e mantém as edições anteriores")

    # ------------------------------------------------------------------ F. a tela no navegador (Playwright)
    print("\nF. tela de revisão no navegador")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sync_playwright = None
        print("  (pulado: Playwright não está instalado)")
    if sync_playwright:
        from pharos.ocr import layout, motor, servidor

        ns = sorted(int(p.stem[1:]) for p in (proj_e / "ocr").glob("p*.json"))
        paginas = motor.carregar_paginas(proj_e / "ocr", ns)
        auto_b, _ = layout.analisar_documento(paginas)
        pontos = qualidade.pontuar_todas(paginas)
        sug_ui = [{"de": "Anna", "para": "Anne", "n": 1, "paginas": [3], "conf": 40, "freq_para": 5}]
        rev_ui = Revisao(tmp / "rev_ui.json", paginas[ns[0]]["hash"])
        ses_ui = Sessao("Teste da tela", paginas, auto_b, pontos, sug_ui, rev_ui, proj_e / "paginas", filtro_inicial="sinalizadas")
        srv = servidor.iniciar_em_thread(ses_ui)
        erros_js: list[str] = []
        exe_chromium = Path("/opt/pw-browsers/chromium")
        try:
            with sync_playwright() as pw:
                nav = pw.chromium.launch(executable_path=str(exe_chromium)) if exe_chromium.exists() else pw.chromium.launch()
                pg = nav.new_context(viewport={"width": 1400, "height": 900}).new_page()
                pg.on("pageerror", lambda e: erros_js.append(str(e)))
                pg.on("console", lambda m: erros_js.append(m.text) if m.type == "error" else None)
                pg.on("dialog", lambda d: d.accept())
                pg.goto(srv.url)
                pg.wait_for_selector("#blocos textarea")
                pg.wait_for_function("document.querySelector('#img').naturalWidth > 0")
                ok(pg.locator("#lista .item").count() >= 1, "lista de páginas desenhada")
                ok("Teste da tela" in pg.title(), "título da página mostra o livro")
                pg.locator('#lista .item[data-n="3"]').click()
                pg.wait_for_function("document.querySelector('#pagtit').textContent.includes('Página 3')")
                pg.wait_for_timeout(300)
                ok(pg.locator(".caixa").count() > 0, f"caixas nas palavras duvidosas da página 3 ({pg.locator('.caixa').count()})")
                ok(pg.locator("#sug .chip").count() == 1, "sugestão de correção exibida")
                n0 = pg.locator("#blocos textarea").count()
                # o bloco 0 da página degradada é um cabeçalho curto e mal lido; o bloco 1 é um parágrafo
                ta = pg.locator("#blocos textarea").nth(1)
                ta.click()
                pg.keyboard.press("Home")
                for _ in range(25):
                    pg.keyboard.press("ArrowRight")
                pg.get_by_text("✂ dividir").nth(1).click()
                ok(pg.locator("#blocos textarea").count() == n0 + 1, "dividir bloco no cursor")
                pg.get_by_text("↑ juntar").nth(1).click()
                ok(pg.locator("#blocos textarea").count() == n0, "juntar com o bloco anterior")
                pg.locator("#blocos textarea").nth(1).fill("Texto corrigido na tela.")
                pg.keyboard.press("Control+Enter")
                pg.wait_for_timeout(600)
                ok(ses_ui.revisao.revisada(3) and ses_ui.revisao.blocos(3)[1]["texto"] == "Texto corrigido na tela.",
                   "Ctrl+Enter salva a página e marca como revisada")
                ok("1 de" in pg.locator("#progtxt").inner_text(), "progresso da revisão atualizado")
                pg.select_option("#filtro", "todas")
                pg.locator("#global summary").click()
                pg.fill("#de", "lighthouse")
                pg.fill("#para", "farol")
                pg.check("#caixa")
                pg.click("#contar")
                pg.wait_for_timeout(300)
                ok("ocorrência" in pg.locator("#subres").inner_text(), "contagem da substituição global")
                pg.click("#substituir")
                pg.wait_for_timeout(600)
                ok(all("lighthouse" not in b["texto"].lower() for n in ns for b in ses_ui.blocos(n)), "substituição global aplicada")
                pg.click("#concluir")
                pg.wait_for_timeout(800)
                ok(srv.concluido and pg.locator(".fim h1").count() == 1, "concluir encerra a revisão")
                nav.close()
        finally:
            with contextlib.suppress(Exception):
                srv.server_close()
        ok(not erros_js, f"sem erros no console do navegador {erros_js[:2] if erros_js else ''}")

    # ------------------------------------------------------------------ G. tradução completa de um scan (Ollama simulado)
    print("\nG. scan → OCR → tradução (Ollama simulado) → exportação")
    from teste_fluxo import Falso

    with Falso(tmp) as srv_f:
        cod, out = rodar([str(am / "scan.pdf"), "--saida", str(tmp / "g"), "--ollama", srv_f.url, "--sim", "--origem", "en",
                          "--destino", "pt", "--revisar", "nenhuma", "--sem-previa", "--genero", "ficcao"], tmp, timeout=400)
        reqs = srv_f.requisicoes
    proj_g = tmp / "g" / "scan"
    ok(cod == 0, f"scan traduzido de ponta a ponta (código {cod})")
    if cod != 0:
        print(out[-1500:])
    est = json.loads((proj_g / "estado.json").read_text(encoding="utf-8"))["etapas"] if (proj_g / "estado.json").exists() else {}
    ok(est and all(est.values()), f"todas as etapas concluídas: {est}")
    ok(len(reqs) > 0 and any("lighthouse" in json.dumps(r, ensure_ascii=False).lower() for r in reqs), f"o texto do OCR foi enviado ao modelo ({len(reqs)} requisições)")
    ok(any((proj_g / "saida").glob("*.docx")) and any((proj_g / "saida").glob("*.epub")), "DOCX e EPUB gerados")

    print(f"\n{'TUDO CERTO' if not falhas else str(falhas) + ' verificação(ões) falharam'} — arquivos em {tmp}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
