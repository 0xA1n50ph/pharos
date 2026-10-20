#!/usr/bin/env python3
"""Teste de ponta a ponta, sem Ollama de verdade (usa tests/ollama_falso.py).

Uso:  python tests/teste_fluxo.py
Precisa, só para os testes, de: reportlab e pillow (geram o PDF de amostra).
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "tests"))
PY = sys.executable

import amostras  # noqa: E402

falhas = 0


def ok(cond: bool, texto: str) -> None:
    global falhas
    print(("  ok    " if cond else "  FALHOU ") + texto)
    if not cond:
        falhas += 1


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Falso:
    def __init__(self, tmp: Path, **env):
        self.log = tmp / f"falso_{time.time_ns()}.jsonl"
        self.env = {**os.environ, "FALSO_LOG": str(self.log), **env}

    def __enter__(self):
        self.porta = porta_livre()
        self.proc = subprocess.Popen([PY, str(RAIZ / "tests" / "ollama_falso.py"), str(self.porta)], env=self.env)
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.porta}/api/tags", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        return self

    def __exit__(self, *a):
        self.proc.terminate()
        self.proc.wait()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.porta}"

    @property
    def requisicoes(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines() if l.strip()]


def rodar(args: list[str], cwd: Path, entrada: str | None = None, timeout: int = 300):
    r = subprocess.run([PY, str(RAIZ / "pharos.py"), *args], input=entrada, capture_output=True,
                       text=True, cwd=cwd, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def ultimo_conteudo(req: dict) -> str:
    return req["mensagens"][-1]["content"]


def todo_conteudo(req: dict) -> str:
    return "\n".join(m["content"] for m in req["mensagens"])


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tradutor_teste_"))
    am = tmp / "amostras"
    am.mkdir()
    amostras.gerar_pdf(am / "leuchtturm_recuo.pdf", "recuo")
    amostras.gerar_txt(am / "leuchtturm.txt")
    amostras.gerar_epub(am / "leuchtturm.epub")
    print(f"Pasta de teste: {tmp}\n")

    # ------------------------------------------------------------------ A. unidades
    print("A. limpeza e detecção de idioma")
    from pharos.idioma import detectar_trecho
    from pharos.limpeza import juntar_linhas

    ok(juntar_linhas(["Der Leucht-", "turm steht am Meer"]) == "Der Leuchtturm steht am Meer", "hifenização desfeita")
    ok(juntar_linhas(["die Ein-", "und Ausgänge"]) == "die Ein- und Ausgänge", "hífen de enumeração preservado")
    amostras_idioma = {
        "de": "Der Wind kam am frühen Morgen von Westen und brachte den Geruch von Salz und feuchtem Holz mit sich.",
        "en": "The wind came from the west early in the morning and brought with it the smell of salt and wet wood.",
        "es": "El viento llegó del oeste temprano por la mañana y trajo consigo el olor de la sal y de la madera húmeda.",
        "pt": "O vento chegou do oeste de manhã cedo e trouxe consigo o cheiro do sal e da madeira úmida.",
        "fr": "Le vent est venu de l'ouest tôt le matin et a apporté avec lui l'odeur du sel et du bois humide.",
        "it": "Il vento arrivò da ovest di primo mattino e portò con sé l'odore del sale e del legno umido.",
        "la": "Et in principio erat verbum, et verbum erat apud deum, et deus erat verbum, quod non est in hoc mundo.",
    }
    for cod, texto in amostras_idioma.items():
        r = detectar_trecho(texto)
        ok(bool(r) and r[0] == cod, f"idioma {cod} reconhecido ({r[0] if r else None})")

    # ------------------------------------------------------------------ B. EPUB, modo automático
    print("\nB. EPUB, modo automático (--sim), com falhas injetadas")
    with Falso(tmp, FALSO_FALHA_UMA_VEZ="Postbote", FALSO_FALHA_SEMPRE="Kompass") as srv:
        cod, out = rodar([str(am / "leuchtturm.epub"), "--saida", str(tmp / "b"), "--ollama", srv.url,
                          "--sim", "--destino", "pt"], tmp)
        reqs = srv.requisicoes
    proj = tmp / "b" / "leuchtturm"
    ok(cod == 0, f"termina com código 0 (veio {cod})")
    for ext in ("epub", "docx", "md", "bilingue.html"):
        ok(any(proj.glob(f"saida/*.{ext}")), f"gerou .{ext}")
    rel = (proj / "revisao" / "relatorio.md").read_text(encoding="utf-8")
    ok("Kompass" in rel and "revisar" in rel, "relatório lista o trecho que esgotou as tentativas")
    ok(sum("IMPORTANT: your previous answer was rejected" in ultimo_conteudo(r) for r in reqs) == 3,
       "3 retentativas com o motivo no prompt (1 do 'Postbote' + 2 do 'Kompass')")
    ok(any("Context (previous passages" in ultimo_conteudo(r) for r in reqs), "parágrafos anteriores enviados como contexto")
    from pharos.extratores import epub as ex_epub
    liv_orig, _ = ex_epub.extrair(am / "leuchtturm.epub")
    liv_trad, _ = ex_epub.extrair(next(proj.glob("saida/*.epub")))
    ok(sum(len(c.blocos) for c in liv_orig.capitulos) == sum(len(c.blocos) for c in liv_trad.capitulos),
       "EPUB traduzido tem a mesma estrutura do original (mesmo nº de blocos)")
    ok([b.tipo for b in liv_orig.blocos() if b.tipo != "nota"] == [b.tipo for b in liv_trad.blocos() if b.tipo != "nota"]
       or [b.tipo for b in liv_orig.blocos()] == [b.tipo for b in liv_trad.blocos()], "tipos de bloco (título, verso, separador) preservados")
    from docx import Document
    ok(len(Document(str(next(proj.glob("saida/*.docx")))).paragraphs) > 10, "DOCX abre e tem conteúdo")

    # ------------------------------------------------------------------ C. PDF com página escaneada
    print("\nC. PDF com uma página escaneada")
    with Falso(tmp) as srv:
        cod, out = rodar([str(am / "leuchtturm_recuo.pdf"), "--saida", str(tmp / "c"), "--ollama", srv.url,
                          "--sim", "--destino", "pt"], tmp)
    proj = tmp / "c" / "leuchtturm-recuo"
    ok(cod == 0, f"termina com código 0 (veio {cod})")
    ok("misto" in out and "--ocr-imagens" in out, "PDF de texto com uma página de imagem: avisa e segue só com o texto")
    ok((proj / "revisao" / "paginas_sem_texto.txt").exists(), "lista as páginas sem texto em revisao/")
    livro = json.loads((proj / "extraido" / "livro.json").read_text(encoding="utf-8"))
    ok(len(livro["capitulos"]) == 3, f"capítulos detectados no PDF: {len(livro['capitulos'])} (esperado 3: título + 2)")
    textos = " ".join(b["texto"] for c in livro["capitulos"] for b in c["blocos"])
    ok("Leuchtturm" in textos and "Leucht-" not in textos and "¹" in textos, "hifenização desfeita e nota preservada")
    ok("Küstenverwaltung" in textos, "nota de rodapé extraída")

    # ------------------------------------------------------------------ D. PDF só de imagens
    print("\nD. PDF só de imagens (scan puro) e Ollama desligado")
    from reportlab.pdfgen import canvas
    from PIL import Image
    scan = am / "scan.pdf"
    c = canvas.Canvas(str(scan), pagesize=(420, 595))
    Image.new("L", (840, 1190), 230).save(am / "p.png")
    for _ in range(3):
        c.drawImage(str(am / "p.png"), 0, 0, width=420, height=595)
        c.showPage()
    c.save()
    cod, out = rodar([str(scan), "--saida", str(tmp / "d"), "--sim", "--destino", "pt"], tmp)
    ok(cod == 1 and ("OCR" in out or "Tesseract" in out), "scan puro sem idioma conhecido: pede --origem/--ocr-lang (código 1)")
    cod, out = rodar([str(am / "leuchtturm.txt"), "--saida", str(tmp / "d2"), "--ollama", "http://127.0.0.1:1",
                      "--sim", "--destino", "pt"], tmp)
    ok(cod == 1 and "Ollama" in out, "Ollama indisponível: mensagem clara, sem traceback")

    # ------------------------------------------------------------------ E. interativo: prévia e refinamento
    print("\nE. TXT, interativo: prévia de 3 páginas e menu de ajustes")
    entrada = "\n".join([
        "",                         # idioma de origem (aceita a detecção)
        "", "",                     # título, autor
        "",                         # idioma de destino (pt)
        "",                         # glossário (vazio)
        "",                         # formatos
        "n",                        # sem sugestão automática de perfil
        "ocultismo",                # gênero
        "Novela de teste",          # obra
        "t", "Novo tom: texto solene de teste",
        "r", "Nunca traduzir nomes próprios",
        "g", "Leuchtturm", "Farol", "",
        "c",
        "e", "2", "Tradução corrigida manualmente pelo usuário de teste.", "",
        "o",
        "m", "gemma3:12b",
        "a",
    ]) + "\n"
    with Falso(tmp) as srv:
        cod, out = rodar([str(am / "leuchtturm.txt"), "--saida", str(tmp / "e"), "--ollama", srv.url], tmp, entrada)
        reqs = srv.requisicoes
    proj = tmp / "e" / "leuchtturm"
    ok(cod == 0, f"termina com código 0 (veio {cod})\n{out[-600:] if cod else ''}")
    versoes = sorted(p.name for p in (proj / "previa").iterdir())
    ok(len(versoes) == 8, f"uma pasta por versão da prévia: {versoes}")
    todos = [todo_conteudo(r) for r in reqs]
    ok(any("Novo tom: texto solene de teste" in t for t in todos), "[t] novo tom chegou ao prompt")
    ok(any("Nunca traduzir nomes próprios" in t for t in todos), "[r] nova regra chegou ao prompt")
    ok(any("Leuchtturm -> Farol" in t for t in todos), "[g] termo do glossário chegou ao prompt")
    ok(any("Examples of the desired translation style" in t and "corrigida manualmente" in t for t in todos),
       "[e] correção virou exemplo no prompt")
    ok(any("commentary. Please translate the following" in t for t in todos), "[c] prompt base (oficial) usado na comparação")
    ok("Sem perfil (base)" in (proj / "previa" / "v5" / "previa.html").read_text(encoding="utf-8"),
       "[c] HTML da prévia tem a coluna 'sem perfil'")
    ok(any(r["modelo"] == "gemma3:12b" and r["mensagens"][0]["role"] == "system" for r in reqs),
       "[m] modelo trocado e estilo de prompt 'chat' (mensagem de sistema)")
    perfil = json.loads((proj / "perfil.json").read_text(encoding="utf-8"))
    ok(perfil["registro"].startswith("Novo tom") and len(perfil["exemplos"]) == 1 and "Nunca traduzir nomes próprios" in perfil["regras"],
       "perfil.json guarda tom, regra e exemplo")
    md = next(proj.glob("saida/*.md")).read_text(encoding="utf-8")
    ok("Tradução corrigida manualmente pelo usuário de teste." in md, "correção manual aparece na tradução final")
    ok("Farol" in md, "glossário aplicado e validado na tradução final")
    estado = json.loads((proj / "estado.json").read_text(encoding="utf-8"))["etapas"]
    ok(all(estado.values()), f"todas as etapas concluídas: {estado}")

    # ------------------------------------------------------------------ F. interrupção e retomada
    print("\nF. Ctrl+C no meio da tradução e retomada")
    with Falso(tmp, FALSO_ATRASO="0.7") as srv:
        p = subprocess.Popen([PY, str(RAIZ / "pharos.py"), str(am / "leuchtturm.epub"), "--saida", str(tmp / "f"),
                              "--ollama", srv.url, "--sim", "--destino", "pt", "--sem-previa", "--genero", "ficcao"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=tmp)
        time.sleep(5)
        p.send_signal(signal.SIGINT)
        saida_p = p.communicate(timeout=30)[0]
        feitas = len(srv.requisicoes)
    proj = tmp / "f" / "leuchtturm"
    linhas_cache = len((proj / "traduzido" / "cache.jsonl").read_text(encoding="utf-8").splitlines())
    ok(p.returncode == 130 and "Interrompido" in saida_p, f"Ctrl+C: código 130 e mensagem de retomada (código {p.returncode})")
    ok(0 < linhas_cache < 13, f"parte da tradução ficou salva no cache ({linhas_cache} de 13)")
    with Falso(tmp) as srv:
        cod, out = rodar([str(am / "leuchtturm.epub"), "--saida", str(tmp / "f"), "--ollama", srv.url, "--sim",
                          "--destino", "pt", "--sem-previa", "--genero", "ficcao"], tmp)
        novas = len(srv.requisicoes)
    ok(cod == 0 and "Retomando" in out, "retoma o projeto existente sem perguntar de novo")
    ok(novas == 13 - linhas_cache, f"só traduziu o que faltava ({novas} novas requisições; faltavam {13 - linhas_cache})")
    ok(any((proj / "saida").glob("*.epub")), "exportou ao final")

    print(f"\n{'TUDO CERTO' if not falhas else str(falhas) + ' verificação(ões) falharam'} — arquivos em {tmp}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
