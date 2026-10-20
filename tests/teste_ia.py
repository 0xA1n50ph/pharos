#!/usr/bin/env python3
"""Testes das sugestões de correção de OCR por IA (revisão do OCR).

Uso:  python tests/teste_ia.py
Partes A–D não precisam de Tesseract nem de Ollama (usam um modelo falso). A parte E usa o Tesseract de verdade (inglês)
e a F, o Playwright (pulada se faltar). O "modelo" é sempre tests/ollama_falso.py: um oráculo que corrige exatamente
os erros que o OCR cometeu (mais iscas que as validações precisam barrar).
"""
from __future__ import annotations

import contextlib
import difflib
import json
import re
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

import amostras  # noqa: E402
from pharos.ocr import sugestoes_ia as ia_mod  # noqa: E402
from pharos.ocr.sugestoes_ia import ErroIA, SugestoesIA  # noqa: E402
from pharos.ollama import Ollama, OllamaErro  # noqa: E402
from pharos.ocr import servidor  # noqa: E402

falhas = 0


def ok(cond: bool, texto: str) -> None:
    global falhas
    print(("  ok    " if cond else "  FALHOU ") + texto)
    if not cond:
        falhas += 1


def palavra(txt: str, conf: float, x: int = 10) -> list:
    return [1, 1, 1, x, 10, 40, 12, conf, txt]


class OllamaStub:
    """Imita o cliente do Ollama sem rede. `resposta(parte) -> str`."""

    def __init__(self, resposta=lambda parte: parte, modelos=("gemma3:12b", "translategemma:12b"), atraso: float = 0.0):
        self.resposta, self._modelos, self.atraso = resposta, list(modelos), atraso
        self.chamadas: list[dict] = []
        self.descarregados: list[str] = []

    def modelos(self):
        return list(self._modelos)

    def tem_modelo(self, nome):
        return nome in self._modelos or (nome + ":latest") in self._modelos

    def chat(self, modelo, mensagens, **kw):
        if self.atraso:
            time.sleep(self.atraso)
        usuario = mensagens[-1]["content"]
        m = re.search(r"<text>\n(.*)\n</text>", usuario, re.S)
        parte = m.group(1) if m else usuario
        self.chamadas.append({"modelo": modelo, "sistema": mensagens[0]["content"], "usuario": usuario, "parte": parte})
        r = self.resposta(parte)
        if isinstance(r, Exception):
            raise r
        return r

    def descarregar(self, modelo):
        self.descarregados.append(modelo)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tradutor_ia_"))
    print(f"Pasta de teste: {tmp}\n")

    # ------------------------------------------------------------------ A. validação das trocas
    print("A. o que o modelo muda: comparação palavra a palavra e validações")
    cmp = ia_mod.comparar

    r, d = cmp("The lighthonse stood by the sea.", "The lighthouse stood by the sea.", {"lighthonse": 40.0})
    ok(len(r) == 1 and r[0]["de"] == "lighthonse" and r[0]["para"] == "lighthouse", "correção de OCR numa palavra de confiança baixa passa")
    ok("The lighthonse stood by the sea."[r[0]["ini"]:r[0]["fim"]] == "lighthonse", "as posições apontam para o texto original")
    r, d = cmp("The house stood by the sea.", "The horse stood by the sea.", {"house": 96.0})
    ok(r == [] and d.get("ocr_confiante") == 1, "palavra que o OCR leu com confiança alta não pode ser trocada")
    r, d = cmp("Das Hans stand am Meer.", "Das Haus stand am Meer.", {})
    ok(len(r) == 1, "palavra que não consta nas leituras do OCR (ex.: digitada por você) não é bloqueada por confiança")
    r, d = cmp("Das Hans stand 1888 da.", "Das Haus stand 1889 da.", {"Hans": 55.0})
    ok([x["de"] for x in r] == ["Hans"] and d.get("numero") == 1, "troca de número é descartada; a de palavra segue")
    r, d = cmp("Die Thür und seyn Haus.", "Die Tür und sein Haus.", {"Thür": 50.0, "seyn": 50.0})
    ok(r == [] and d.get("grafia_antiga") == 2, "modernização de grafia antiga (Thür→Tür, seyn→sein) é descartada")
    r, d = cmp("Die Thur und das Haus.", "Die Thür und das Haus.", {"Thur": 50.0})
    ok(len(r) == 1 and r[0]["para"] == "Thür", "diacrítico perdido pelo OCR (Thur→Thür) é correção, não modernização")
    r, d = cmp("Das Haus stand am Meer und sah hinaus.", "Das alte Haus stand am Meer und sah hinaus.", {})
    ok(r == [] and d.get("acrescimo") == 1, "acréscimo de palavra é descartado")
    r, d = cmp("Das Haus. Dann ging er.", "Das Haus, Dann ging er.", {"Haus": 40.0})
    ok(r == [] and d.get("pontuacao", 0) >= 1, "troca de pontuação é escolha do autor e é descartada")
    r, d = cmp("Das | Haus stand da.", "Das Haus stand da.", {})
    ok(len(r) == 1 and r[0]["de"] == "|" and r[0]["para"] == "", "símbolo inventado pelo OCR pode ser apagado")
    r, d = cmp("Das alte Haus stand am Meer und sah hinaus.", "Das Haus stand am Meer und sah hinaus.", {"alte": 40.0})
    ok(r == [] and d.get("remocao") == 1, "apagar palavra de verdade é descartado")
    r, d = cmp("The house stood by the sea.", "The dog stood by the sea.", {"house": 40.0})
    ok(r == [] and d.get("muito_diferente") == 1, "trocar por palavra muito diferente é descartado")
    trad = "O vento chegou de manhã cedo e trouxe consigo o cheiro do sal e da madeira úmida, e ele não disse nada."
    r, d = cmp("The wind came early in the morning and brought with it the smell of salt and damp wood, and he said nothing.", trad, {})
    ok(r == [] and d.get("reescrita") == 1, "resposta traduzida/reescrita é descartada por inteiro")
    r, d = cmp("Zeile eins hier\nZeile zwei dort\nZeile drei da", "Zeile eins hier\nZeile zwei dort\nZeile drei da", {})
    ok(r == [], "texto igual não gera sugestão")
    verso = "Die Wellen schlagen\nan den Strnd hinauf\nund fliehen wieder"
    r, d = cmp(verso, verso.replace("Strnd", "Strand"), {"Strnd": 35.0})
    ok(len(r) == 1 and verso[r[0]["ini"]:r[0]["fim"]] == "Strnd", "em verso, as quebras de linha ficam e a posição está certa")
    r, d = cmp("Er sah rn eine Tür im Dunkeln stehen.", "Er sah meine Tür im Dunkeln stehen.", {"rn": 30.0, "eine": 90.0})
    ok(len(r) == 1 and r[0]["de"] == "rn eine" and r[0]["para"] == "meine", "palavra partida pelo OCR é reunida (rn eine → meine)")
    r, d = cmp("Der Lenchtturm der Küste stand da.", "Der Leuchtturm den Küste stand da.", {"Lenchtturm": 40.0, "der": 97.0})
    ok([x["de"] for x in r] == ["Lenchtturm"], "numa troca emparelhada, só vale a palavra que o OCR duvidava")
    r, d = cmp("Eins zwei drei vier fünf sechs sieben acht neun zehn elf zwölf", "Un deux trois quatre cinq six sept huit neuf dix onze douze", {})
    ok(r == [] and d, "troca em quase todas as palavras: descartada")
    r, d = cmp("Der Lenchtturm\nstand am Meer", "Der Leuchtturm stand am Meer", {"Lenchtturm": 40.0})
    ok(all("\n" not in x["de"] for x in r), "uma troca nunca atravessa quebra de linha")

    ok(ia_mod.limpar_resposta("```\nDas Haus.\n```", "Das Haus.") == "Das Haus.", "tira cerca de código da resposta")
    ok(ia_mod.limpar_resposta("\"Das Haus stand da.\"", "Das Haus stand da.") == "Das Haus stand da.", "tira aspas postas pelo modelo")
    ok(ia_mod.limpar_resposta("Here is the corrected text:\nDas Haus stand da.", "Das Haus stand da.") == "Das Haus stand da.",
       "tira a frase de apresentação do modelo")
    ok(ia_mod.limpar_resposta("<text>\nDas Haus.\n</text>", "Das Haus.") == "Das Haus.", "tira as marcas <text> devolvidas")
    ok(ia_mod.limpar_resposta("\"Ja\", sagte er.", "\"Ja\", sagte er.") == "\"Ja\", sagte er.", "aspas do próprio texto ficam")

    longo = " ".join(f"Satz Nummer {i} steht hier und ist lang genug." for i in range(120))
    partes = ia_mod.dividir_em_partes(longo)
    ok(len(partes) >= 3 and "".join(p for _, p in partes) == longo and all(len(p) <= ia_mod.MAX_PARTE for _, p in partes),
       f"bloco longo é dividido em {len(partes)} partes que reconstroem o texto")
    ok(all(longo[o:o + len(p)] == p for o, p in partes) and all(p.rstrip().endswith(".") for _, p in partes[:-1]),
       "as partes terminam em fim de frase e os deslocamentos estão certos")

    ok(ia_mod.escolher_modelo([], "meu:7b", "translategemma:12b") == ("meu:7b", ""), "modelo escolhido pela pessoa é respeitado")
    ok(ia_mod.escolher_modelo([], "", "gemma3:12b") == ("gemma3:12b", ""), "modelo de uso geral da configuração é usado como está")
    m, av = ia_mod.escolher_modelo(["translategemma:12b", "gemma3:1b", "gemma3:12b", "qwen3:8b"], "", "translategemma:12b")
    ok(m == "gemma3:12b" and not av, f"com modelo de tradução, escolhe um geral instalado: o maior que cabe ({m})")
    m, av = ia_mod.escolher_modelo(["translategemma:12b"], "", "translategemma:12b")
    ok(m == "translategemma:12b" and "ollama pull" in av, "só com modelo de tradução: usa, mas avisa como instalar um geral")
    try:
        ia_mod.escolher_modelo([], "", "")
        ok(False, "sem nenhum modelo: erro claro")
    except ErroIA as e:
        ok("--modelo-ocr" in str(e), "sem nenhum modelo: erro claro")

    confs = ia_mod.mapa_confianca([palavra("Haus,", 40), palavra("Haus", 95), palavra("stand", 99), palavra("-", -1)])
    ok(confs == {"Haus": 40, "stand": 99}, "mapa de confiança guarda a menor leitura de cada palavra")
    ok(ia_mod.suspeitas_do_bloco("Das Haus stand da", confs, 85) == ["Haus"], "só palavras de confiança baixa vão como suspeitas")
    ok(ia_mod.suspeitas_do_bloco("Das Haus stand da", {"Haus": 99.0}, 85) == [], "bloco sem palavra duvidosa não vai ao modelo")

    # ------------------------------------------------------------------ B. geração, cache e estado
    print("\nB. geração por página, cache, rejeitar/aceitar e passada em lote")
    blocos = [
        {"tipo": "paragrafo", "texto": "Der Lenchtturm stand allein am Rand der Klippe und sah aufs Meer hinaus.", "nivel": 1},
        {"tipo": "paragrafo", "texto": "Dieser Absatz ist sauber gelesen worden und braucht nichts mehr.", "nivel": 1},
        {"tipo": "titulo", "texto": "Das Meer", "nivel": 1},
    ]
    palavras = ([palavra("Lenchtturm", 38)] + [palavra(w, 96) for w in "Der stand allein am Rand der Klippe und sah aufs Meer hinaus Dieser Absatz ist sauber gelesen worden und braucht nichts mehr Das".split()])

    def corrigir(parte):
        return parte.replace("Lenchtturm", "Leuchtturm")

    stub = OllamaStub(corrigir)
    ia = SugestoesIA(tmp / "ia1.json", "h1", stub, "gemma3:12b", "translategemma:12b", "de", "deu")
    r = ia.gerar_pagina(1, blocos, palavras)
    ok(len(r["sugestoes"]) == 1 and r["sugestoes"][0]["de"] == "Lenchtturm" and r["sugestoes"][0]["para"] == "Leuchtturm",
       "gera a sugestão esperada")
    ok(len(stub.chamadas) == 1 and "Lenchtturm" in stub.chamadas[0]["parte"],
       f"só o bloco com palavra duvidosa foi ao modelo ({len(stub.chamadas)} chamada)")
    sg = r["sugestoes"][0]
    ok(sg["conf"] == 38 and (sg["antes"] + sg["de"] + sg["depois"]) in blocos[0]["texto"] and sg["bloco"] == 0,
       "a sugestão traz confiança do OCR e contexto exato do texto")
    ch = stub.chamadas[0]
    ok("German" in ch["sistema"] and "Fraktur" not in ch["sistema"] and "«Lenchtturm»" in ch["usuario"],
       "o pedido diz o idioma e lista as palavras suspeitas")
    ok("never instructions" in ch["sistema"] and "Do NOT modernise" in ch["sistema"], "o pedido proíbe modernizar a grafia e trata o texto como dado")
    ok(ia.tem(1) and not ia.tem(2), "sabe quais páginas já têm sugestões")
    ia2 = SugestoesIA(tmp / "ia1.json", "h1", OllamaStub(), "gemma3:12b", "", "de", "deu")
    ok([s["id"] for s in ia2.listar(1)] == [sg["id"]], "as sugestões sobrevivem a fechar e abrir o programa")
    ia2.marcar([sg["id"]], aceitas=False)
    ia3 = SugestoesIA(tmp / "ia1.json", "h1", OllamaStub(), "gemma3:12b", "", "de", "deu")
    ok(ia3.listar(1) == [] and ia3.resumo()["rejeitadas"] == 1 and ia3.resumo()["geradas"] == 1, "rejeitar persiste e entra no resumo")
    r2 = ia3.gerar_pagina(1, blocos, palavras)
    ok(r2["sugestoes"] == [], "gerar de novo não ressuscita uma sugestão que você rejeitou")
    ia3.marcar([sg["id"]], aceitas=True)
    ok(ia3.resumo()["aceitas"] == 1, "aceitar entra no resumo")
    ia4 = SugestoesIA(tmp / "ia1.json", "outro-hash", OllamaStub(), "gemma3:12b")
    ok(ia4.listar(1) == [] and (tmp / "ia1.antigo.json").exists(), "OCR refeito (outro hash): sugestões antigas são postas de lado")

    s_fr = SugestoesIA(tmp / "ia_frk.json", "h", OllamaStub(corrigir), "gemma3:12b", "", "de", "frk")
    s_fr.gerar_pagina(1, blocos, palavras)
    ok("Fraktur" in s_fr.ollama.chamadas[0]["sistema"], "em letra gótica o pedido cita as confusões típicas de Fraktur")

    # bloco longo: as posições das trocas valem para o bloco inteiro
    frases = [f"Satz Nummer {i} steht hier und ist lang genug." for i in range(110)]
    frases[95] = "Der Lenchtturm stand allein."
    longo = " ".join(frases)
    pal_l = [palavra("Lenchtturm", 30)] + [palavra("Satz", 95)]
    s_l = SugestoesIA(tmp / "ia_longo.json", "h", OllamaStub(corrigir), "gemma3:12b")
    r = s_l.gerar_pagina(1, [{"tipo": "paragrafo", "texto": longo, "nivel": 1}], pal_l)
    ok(len(s_l.ollama.chamadas) >= 3 and len(r["sugestoes"]) == 1 and
       (r["sugestoes"][0]["antes"] + "Lenchtturm" + r["sugestoes"][0]["depois"]) in longo,
       f"bloco longo vai em partes ({len(s_l.ollama.chamadas)}) e a sugestão cai no lugar certo")

    # respostas problemáticas do modelo
    for nome, resp in (("traduz", lambda p: "O vento chegou de manhã cedo e trouxe consigo o cheiro do sal. " * 2),
                       ("com apresentação", lambda p: "Here is the corrected text:\n" + corrigir(p)),
                       ("em cerca de código", lambda p: "```\n" + corrigir(p) + "\n```")):
        s_x = SugestoesIA(tmp / f"ia_{abs(hash(nome))}.json", "h", OllamaStub(resp), "gemma3:12b")
        rx = s_x.gerar_pagina(1, blocos, palavras)
        esperado = 0 if nome == "traduz" else 1
        ok(len(rx["sugestoes"]) == esperado, f"resposta {nome}: {'nada é sugerido' if not esperado else 'a correção real continua valendo'}")

    # falhas
    s_e = SugestoesIA(tmp / "ia_err.json", "h", OllamaStub(lambda p: OllamaErro("caiu")), "gemma3:12b")
    try:
        s_e.gerar_pagina(1, blocos, palavras)
        ok(False, "erro do Ollama é levantado")
    except OllamaErro:
        ok(not (tmp / "ia_err.json").exists() and not s_e.tem(1), "erro do Ollama: nada é guardado e o erro chega a quem chamou")
    s_m = SugestoesIA(tmp / "ia_sem.json", "h", OllamaStub(modelos=("outro:7b",)), "gemma3:12b")
    try:
        s_m.gerar_pagina(1, blocos, palavras)
        ok(False, "modelo ausente dá erro")
    except ErroIA as e:
        ok("ollama pull gemma3:12b" in str(e), "modelo não instalado: a mensagem diz o comando para instalar")
    est = s_m.estado()
    ok(est["disponivel"] is False and "gemma3:12b" in est["motivo"], "estado informa que não está disponível e por quê")

    # passada em lote
    pal_p = {n: palavras for n in range(1, 6)}
    bl_p = {n: blocos for n in range(1, 6)}
    fonte = lambda n: (bl_p[n], pal_p[n])  # noqa: E731
    s_b = SugestoesIA(tmp / "ia_lote.json", "h", OllamaStub(corrigir, atraso=0.05), "gemma3:12b")
    s_b.iniciar_lote([1, 2, 3, 4, 5], fonte, "todas")
    try:
        s_b.iniciar_lote([1], fonte)
        ok(False, "duas passadas ao mesmo tempo são recusadas")
    except ErroIA:
        ok(True, "duas passadas ao mesmo tempo são recusadas")
    s_b.aguardar(20)
    t = s_b.estado_trabalho()
    ok(not t["ativo"] and t["feitas"] == 5 and t["sugestoes"] == 5 and not t["erro"], f"lote termina: {t['feitas']} páginas, {t['sugestoes']} sugestões")
    ok(all(s_b.tem(n) for n in range(1, 6)), "cada página fica guardada")

    s_p = SugestoesIA(tmp / "ia_parar.json", "h", OllamaStub(corrigir, atraso=0.3), "gemma3:12b")
    s_p.iniciar_lote([1, 2, 3, 4, 5], fonte, "todas")
    time.sleep(0.45)
    s_p.parar()
    s_p.aguardar(20)
    t = s_p.estado_trabalho()
    ok(not t["ativo"] and 0 < t["feitas"] < 5, f"parar interrompe depois da página atual ({t['feitas']} de 5 feitas)")

    s_f = SugestoesIA(tmp / "ia_falha.json", "h", OllamaStub(lambda p: OllamaErro("caiu")), "gemma3:12b")
    s_f.iniciar_lote([1, 2, 3, 4], fonte)
    s_f.aguardar(30)
    t = s_f.estado_trabalho()
    ok(not t["ativo"] and t["feitas"] == 2 and "caiu" in t["erro"], f"duas falhas seguidas encerram o lote e registram o erro ({t['feitas']} tentadas)")

    s_d = SugestoesIA(tmp / "ia_desc.json", "h", stub, "gemma3:12b")
    s_d.descarregar()
    ok(stub.descarregados == [], "não descarrega o modelo se nunca o usou")
    stub.descarregados.clear()
    s_u = SugestoesIA(tmp / "ia_desc2.json", "h", stub, "gemma3:12b")
    s_u.gerar_pagina(1, blocos, palavras)
    s_u.descarregar()
    ok(stub.descarregados == ["gemma3:12b"], "depois de usar, descarrega o modelo para liberar memória")

    # ------------------------------------------------------------------ C. a sessão: nunca aplica sozinha
    print("\nC. sessão de revisão: as sugestões não mexem no texto")
    from pharos.ocr.revisao import Revisao, Sessao

    def sessao_de(stub_ia, arq):
        paginas = {n: {"n": n, "largura": 1000, "altura": 1400, "palavras": palavras, "hash": "h"} for n in (1, 2, 3, 4)}
        auto = {n: [dict(b) for b in blocos] for n in paginas}
        pontos = {1: {"score": 60, "sinalizada": True, "palavras": 40}, 2: {"score": 95, "sinalizada": False, "palavras": 40},
                  3: {"score": 70, "sinalizada": True, "palavras": 40}, 4: {"score": 99, "sinalizada": False, "palavras": 2}}
        rev = Revisao(tmp / f"rev_{arq}.json", "h")
        ia_ = SugestoesIA(tmp / f"{arq}.json", "h", stub_ia, "gemma3:12b", "", "de", "deu")
        return Sessao("Teste", paginas, auto, pontos, [], rev, tmp, ia=ia_), ia_

    ses, ia_s = sessao_de(OllamaStub(corrigir), "ses1")
    antes_blocos = ses.blocos_finais()
    ses.ia_gerar_pagina(1)
    ses.ia_iniciar_lote("todas")
    ia_s.aguardar(20)
    ok(ses.blocos_finais() == antes_blocos and not ses.revisao.editadas(),
       "depois de gerar sugestões (página e lote), o texto e as edições continuam exatamente iguais")
    ok(len(ses.ia_da_pagina(1)) == 1 and ses.pagina(1)["ia"][0]["para"] == "Leuchtturm", "a página traz as sugestões à parte, junto do texto")
    idx = {p["n"]: p for p in ses.indice()["paginas"]}
    ok(idx[1]["ia"] == 1 and idx[3]["ia"] == 1 and idx[4]["ia"] == 0 and ses.indice()["ia"] is True, "o índice mostra quantas sugestões cada página tem")
    ses.salvar(1, [{"tipo": "paragrafo", "texto": "Der Leuchtturm stand allein.", "nivel": 1}], True)
    ok(ses.ia_da_pagina(1) == [], "se você já corrigiu o texto à mão, a sugestão deixa de aparecer")
    ses2, ia_s2 = sessao_de(OllamaStub(corrigir), "ses2")
    ses2.marcar(2, True)
    ia_s2.gerar_pagina(3, *ses2._fonte_ia(3))
    ok(ses2.paginas_para_ia("sinalizadas") == [1], "lote 'sinalizadas' pula revisadas, já geradas e sem texto")
    ok(ses2.paginas_para_ia("todas") == [1], "lote 'todas' pula revisadas, já geradas e páginas quase sem texto")
    sem_ia = Sessao("x", {1: {"n": 1, "largura": 1, "altura": 1, "palavras": []}}, {}, {}, [], Revisao(tmp / "rev_x.json", "h"), tmp)
    ok(sem_ia.ia_da_pagina(1) == [] and sem_ia.ia_estado()["disponivel"] is False and sem_ia.indice()["ia"] is False,
       "sem IA configurada, nada muda na sessão")

    # ------------------------------------------------------------------ D. servidor HTTP com um Ollama (falso) de verdade
    print("\nD. servidor da revisão + Ollama falso por HTTP")
    from teste_fluxo import Falso

    from pharos.ocr import servidor

    def chamar(srv, caminho, corpo=None, token=True):
        base = srv.url.split("/?")[0]
        req = urllib.request.Request(base + caminho, data=None if corpo is None else json.dumps(corpo).encode(),
                                     headers={"X-Token": srv.token} if token else {})
        if corpo is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def sessao_http(url_ollama, arq, modelo="gemma3:12b"):
        ses_, ia_ = sessao_de(Ollama(url_ollama, timeout=30), arq)
        ia_.preferido = modelo
        return ses_, ia_

    with Falso(tmp, FALSO_OCR_TROCAS="Lenchtturm=Leuchtturm") as f:
        ses_h, ia_h = sessao_http(f.url, "http1")
        srv = servidor.iniciar_em_thread(ses_h)
        try:
            ok(chamar(srv, "/api/ia", token=False)[0] == 403, "sem token, a API de IA recusa")
            c, est = chamar(srv, "/api/ia")
            ok(c == 200 and est["disponivel"] and est["modelo"] == "gemma3:12b" and not est["trabalho"]["ativo"], "estado: disponível, modelo certo")
            c, r = chamar(srv, "/api/ia/pagina/1", {})
            ok(c == 200 and len(r["sugestoes"]) == 1 and r["sugestoes"][0]["para"] == "Leuchtturm", "POST gera a sugestão da página")
            ok(chamar(srv, "/api/pagina/1")[1]["blocos"][0]["texto"].count("Lenchtturm") == 1, "o texto da página segue intacto")
            ident = r["sugestoes"][0]["id"]
            c, _ = chamar(srv, "/api/ia/rejeitar", {"ids": [ident]})
            ok(c == 200 and chamar(srv, "/api/ia/pagina/1")[1]["sugestoes"] == [], "rejeitar esconde a sugestão")
            ok(chamar(srv, "/api/ia/aceitar", {"ids": "x"})[0] == 400, "ids inválidos: 400")
            ok(chamar(srv, "/api/ia/pagina/99", {})[0] == 409 and chamar(srv, "/api/ia/pagina/99")[0] == 404, "página inexistente é recusada")
            c, r = chamar(srv, "/api/ia/lote", {"escopo": "sinalizadas"})
            ok(c == 200 and r.get("total") == 1, f"lote 'sinalizadas' começa (resposta: {r.get('total')} página pendente)")
            fim = time.time() + 30
            while time.time() < fim and chamar(srv, "/api/ia")[1]["trabalho"]["ativo"]:
                time.sleep(0.2)
            t = chamar(srv, "/api/ia")[1]["trabalho"]
            ok(not t["ativo"] and t["feitas"] == 1 and chamar(srv, "/api/ia/pagina/3")[1]["sugestoes"], "lote termina e a página 3 recebe sugestão")
            ok(chamar(srv, "/api/ia/lote", {"escopo": "x"})[0] == 409, "escopo inválido: erro claro")
            c, r = chamar(srv, "/api/ia/lote", {"escopo": "sinalizadas"})
            ok(c == 200 and r.get("nada") is True, "lote sem páginas pendentes diz que não há nada a fazer")
        finally:
            srv.server_close()
        reqs = [x for x in f.requisicoes if "proofreader" in x["mensagens"][0]["content"]]
        ok(reqs and reqs[0]["modelo"] == "gemma3:12b", f"as chamadas ao Ollama usaram o modelo escolhido ({len(reqs)} chamadas)")

    with Falso(tmp, FALSO_OCR_MODO="erro") as f:
        ses_h, ia_h = sessao_http(f.url, "http2")
        srv = servidor.iniciar_em_thread(ses_h)
        try:
            c, r = chamar(srv, "/api/ia/pagina/1", {})
            ok(c == 502 and "erro" in r, f"Ollama com erro: a tela recebe 502 e a mensagem ({c})")
        finally:
            srv.server_close()
    with Falso(tmp, FALSO_OCR_MODO="traduz") as f:
        ses_h, ia_h = sessao_http(f.url, "http3")
        srv = servidor.iniciar_em_thread(ses_h)
        try:
            c, r = chamar(srv, "/api/ia/pagina/1", {})
            ok(c == 200 and r["sugestoes"] == [] and r["info"]["descartes"].get("reescrita"), "modelo que traduz: nenhuma sugestão, e o descarte é registrado")
        finally:
            srv.server_close()
    ses_h, ia_h = sessao_http("http://127.0.0.1:9", "http4")
    srv = servidor.iniciar_em_thread(ses_h)
    try:
        c, est = chamar(srv, "/api/ia")
        ok(c == 200 and est["disponivel"] is False and "Ollama" in est["motivo"], "Ollama fora do ar: a tela é avisada sem travar")
        c, r = chamar(srv, "/api/ia/pagina/1", {})
        ok(c == 409 and "Ollama" in r["erro"], "e pedir sugestão dá mensagem clara (409)")
    finally:
        srv.server_close()

    if "--so-unidade" in sys.argv:
        print(f"\n{'TUDO CERTO' if not falhas else str(falhas) + ' verificação(ões) falharam'} (só A–D) — {tmp}")
        return 1 if falhas else 0

    return parte_ocr(tmp, ok_final=lambda: falhas)


def trocas_reais(textos: list[str], lexico: dict[str, str]) -> dict[str, str]:
    """O "oráculo" do modelo falso: para cada palavra lida que não existe no livro verdadeiro, a palavra verdadeira mais parecida."""
    tabela = {}
    chaves = list(lexico)
    for t in set(re.findall(r"\w+", " ".join(textos))):
        if len(t) < 4 or t.lower() in lexico:
            continue
        achou = difflib.get_close_matches(t.lower(), chaves, n=1, cutoff=0.75)
        if achou:
            certo = lexico[achou[0]]
            tabela[t] = certo[0].upper() + certo[1:] if t[0].isupper() else certo
    return tabela


def parte_ocr(tmp: Path, ok_final) -> int:
    from pharos.ocr import layout, motor, qualidade, tesseract
    from pharos.ocr.revisao import Revisao, Sessao
    from teste_fluxo import Falso
    from teste_ocr import api, paragrafos, rodar_interativo

    def encerrar(msg: str = "") -> int:
        print(f"\n{'TUDO CERTO' if not falhas else str(falhas) + ' verificação(ões) falharam'}{msg} — arquivos em {tmp}")
        return 1 if falhas else 0

    try:
        exe = tesseract.localizar()
        instalados = tesseract.idiomas_instalados(exe)
    except Exception as e:  # noqa: BLE001
        print(f"\nTesseract não encontrado ({e}); partes E e F puladas.")
        return encerrar(" (A–D)")
    if "eng" not in instalados:
        print("\nO Tesseract não tem o idioma 'eng'; partes E e F puladas.")
        return encerrar(" (A–D)")

    # ------------------------------------------------------------------ E. de ponta a ponta: scan ruim + OCR de verdade + CLI
    print("\nE. de ponta a ponta: scan degradado, OCR real, revisão com sugestões da IA")
    am = tmp / "amostras"
    am.mkdir()
    amostras.gerar_pdf(am / "lighthouse.pdf", "recuo", "en")
    esperado = amostras.texto_esperado_en()
    lexico = {w.lower(): w.lower() for w in re.findall(r"\w+", " ".join(esperado))}
    pasta = tmp / "scan_ruim"
    pasta.mkdir()
    for i, im in enumerate(amostras.renderizar_pdf(am / "lighthouse.pdf", 200), 1):
        d = amostras.degradar(im, 1.0, 60.0, desfoque=2.2, seed=i) if i in (3, 4) else amostras.degradar(im, 1.0, 5.0, seed=i)
        d.save(pasta / f"pagina_{i:03d}.jpg", quality=75)
    arq_trocas = tmp / "trocas.json"
    proj = tmp / "e" / "scan-ruim-livro"
    vistos: dict = {}

    def sessao_com(srv_url, caminho):
        ns = sorted(int(p.stem[1:]) for p in (proj / "ocr").glob("p*.json"))
        paginas = motor.carregar_paginas(proj / "ocr", ns)
        auto_b, _ = layout.analisar_documento(paginas)
        pontos = qualidade.pontuar_todas(paginas)
        ia_ = SugestoesIA(caminho, "hash-teste", Ollama(srv_url, timeout=120), "gemma3:12b", "", "en", "eng")
        ses_ = Sessao("Teste da IA", paginas, auto_b, pontos, [], Revisao(tmp / f"rev_{caminho.stem}.json", "hash-teste"),
                      proj / "paginas", filtro_inicial="todas", ia=ia_)
        return ses_, ia_, paginas, auto_b

    with Falso(tmp, FALSO_OCR_TROCAS_ARQ=str(arq_trocas)) as falso:
        args1 = [str(pasta), "--saida", str(tmp / "e"), "--origem", "en", "--destino", "pt", "--sem-navegador", "--sem-previa",
                 "--exportar-apenas", "--ollama", falso.url, "--modelo-ocr", "gemma3:12b", "--revisar", "sinalizadas"]

        def revisar(url, p):
            vistos["estado"] = api(url, "/api/ia")
            antes = api(url, "/api/pagina/3")["blocos"]
            textos = [b["texto"] for b in antes]
            pal3 = motor.carregar_paginas(proj / "ocr", [3])[3]["palavras"]
            confs = ia_mod.mapa_confianca(pal3)
            tabela = trocas_reais(textos, lexico)
            elegivel = " ".join(t for t in textos if ia_mod.suspeitas_do_bloco(t, confs))
            isca = next((w for w in re.findall(r"\w+", elegivel) if len(w) >= 6 and confs.get(w, 0) >= 96 and w.lower() in lexico), None)
            if isca:
                tabela[isca] = isca[:-1] + ("x" if isca[-1] != "x" else "z")
            vistos["tabela"], vistos["isca"] = tabela, isca
            arq_trocas.write_text(json.dumps(tabela), encoding="utf-8")
            r = api(url, "/api/ia/pagina/3", {})
            vistos["sug"], vistos["info"] = r["sugestoes"], r["info"]
            depois = api(url, "/api/pagina/3")["blocos"]
            vistos["intacto"] = depois == antes
            blocos = depois
            sugs = r["sugestoes"]
            vistos["aceita"] = sugs[-1] if sugs else None
            vistos["rejeitada"] = sugs[0] if len(sugs) >= 2 else None
            if vistos["rejeitada"]:
                api(url, "/api/ia/rejeitar", {"ids": [vistos["rejeitada"]["id"]]})
            if vistos["aceita"]:
                sg = vistos["aceita"]
                t = blocos[sg["bloco"]]["texto"]
                i = t.find(sg["antes"] + sg["de"] + sg["depois"]) + len(sg["antes"])
                blocos[sg["bloco"]]["texto"] = t[:i] + sg["para"] + t[i + len(sg["de"]):]
                api(url, "/api/ia/aceitar", {"ids": [sg["id"]]})
            api(url, "/api/pagina/3", {"blocos": blocos, "revisada": True})
            for x in api(url, "/api/indice")["paginas"]:
                if x["sinalizada"]:
                    api(url, f"/api/marcar/{x['n']}", {"revisada": True})
            api(url, "/api/concluir", {})

        cod, out, url = rodar_interativo(args1, tmp, revisar, timeout=400)
        est = vistos.get("estado", {})
        ok(cod == 0, f"programa terminou (código {cod})" + ("" if cod == 0 else "\n" + out[-1500:]))
        ok(est.get("disponivel") and est.get("modelo") == "gemma3:12b", f"a tela vê a IA disponível com o modelo escolhido ({est.get('modelo')})")
        sug = vistos.get("sug", [])
        tabela = vistos.get("tabela", {})
        ok(len(sug) >= 1, f"a página degradada recebeu {len(sug)} sugestão(ões) a partir de erros reais do OCR")
        ok(all(tabela.get(s["de"]) == s["para"] for s in sug), "toda sugestão é uma das correções que o modelo propôs (nada a mais entra)")
        ok(vistos.get("intacto") is True, "gerar as sugestões não alterou o texto da página")
        if vistos.get("isca"):
            ok(vistos["isca"] not in [s["de"] for s in sug] and vistos["info"]["descartes"].get("ocr_confiante", 0) >= 1,
               f"a isca (palavra '{vistos['isca']}' lida com confiança alta) foi barrada")
        else:
            print("  (sem palavra-isca de confiança alta nos blocos elegíveis; checagem pulada)")
        ok("Sugestões da IA (gemma3:12b)" in out, "o programa informa o resumo das sugestões ao fim da revisão")
        rel = (proj / "revisao" / "ocr_qualidade.md").read_text(encoding="utf-8")
        ok("Sugestões de correção por IA" in rel and "nada é aplicado sem a sua decisão" in rel, "o relatório de qualidade registra as sugestões da IA")
        arq_ia = proj / "revisao" / "ocr_sugestoes_ia.json"
        ok(arq_ia.exists() and json.loads(arq_ia.read_text(encoding="utf-8"))["modelo"] == "gemma3:12b", "as sugestões ficam salvas no projeto")
        texto_livro = " ".join(paragrafos(proj / "extraido" / "livro.json"))
        if vistos.get("aceita"):
            ok(vistos["aceita"]["de"] not in texto_livro and vistos["aceita"]["para"] in texto_livro,
               f"a sugestão aceita ('{vistos['aceita']['de']}' → '{vistos['aceita']['para']}') chegou ao livro")
        if vistos.get("rejeitada"):
            ok(vistos["rejeitada"]["de"] in texto_livro, f"a sugestão rejeitada ('{vistos['rejeitada']['de']}') NÃO foi aplicada ao livro")

        # segunda execução: --sugestoes-ia começa sozinha, sem aplicar nada
        vistos2: dict = {}

        def lote(url, p):
            fim = time.time() + 90
            while time.time() < fim:
                t = api(url, "/api/ia")["trabalho"]
                if not t["ativo"] and (t["total"] or time.time() > fim - 80):
                    break
                time.sleep(0.4)
            vistos2["trabalho"] = api(url, "/api/ia")["trabalho"]
            vistos2["de_novo"] = api(url, "/api/ia/pagina/3", {})["sugestoes"]
            api(url, "/api/concluir", {})

        args2 = [str(proj), "--sem-navegador", "--sem-previa", "--exportar-apenas", "--ollama", falso.url,
                 "--revisar-ocr", "--sugestoes-ia", "todas"]
        cod, out, url = rodar_interativo(args2, tmp, lote, timeout=400)
        t = vistos2.get("trabalho", {})
        ok(cod == 0 and "IA: sugerindo correções em" in out, "--sugestoes-ia começa a passada sozinha quando a revisão abre")
        ok(t and not t["ativo"] and t["feitas"] == t["total"] and t["total"] >= 3, f"a passada cobre as páginas ainda sem sugestão ({t.get('feitas')}/{t.get('total')})")
        voltaram = [x["de"] for x in vistos2.get("de_novo", [])]
        ok(vistos["aceita"]["de"] not in voltaram and vistos["rejeitada"]["de"] not in voltaram,
           f"pedir de novo na página 3 não traz de volta a sugestão aceita nem a rejeitada ({len(voltaram)} outras seguem pendentes)")
        texto_livro2 = " ".join(paragrafos(proj / "extraido" / "livro.json"))
        ok(texto_livro2 == texto_livro, "depois da passada em lote, o texto do livro continua exatamente o mesmo (sugestão não é aplicação)")
        ok(any(r for r in falso.requisicoes if "proofreader" in r["mensagens"][0]["content"]), "o modelo falso recebeu os pedidos de revisão")

    # ------------------------------------------------------------------ F. a tela no navegador
    print("\nF. tela de revisão com sugestões da IA no navegador")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  (pulado: Playwright não está instalado)")
        return encerrar(" (A–E)")

    ns = sorted(int(p.stem[1:]) for p in (proj / "ocr").glob("p*.json"))
    paginas0 = motor.carregar_paginas(proj / "ocr", ns)
    auto0, _ = layout.analisar_documento(paginas0)
    tabela_f = trocas_reais([b["texto"] for n in ns for b in auto0.get(n, [])], lexico)
    arq_f = tmp / "trocas_f.json"
    arq_f.write_text(json.dumps(tabela_f), encoding="utf-8")
    ok(len(tabela_f) >= 2, f"(preparo) o oráculo conhece {len(tabela_f)} leituras erradas do livro")

    erros_js: list[str] = []
    dialogos: list[str] = []
    exe_chromium = Path("/opt/pw-browsers/chromium")
    with Falso(tmp, FALSO_OCR_TROCAS_ARQ=str(arq_f), FALSO_OCR_MODO="lento", FALSO_OCR_ATRASO="0.5") as falso_f:
        ses, ia_f, _, _ = sessao_com(falso_f.url, tmp / "ia_ui.json")
        srv = servidor.iniciar_em_thread(ses)
        try:
            with sync_playwright() as pw:
                nav = pw.chromium.launch(executable_path=str(exe_chromium)) if exe_chromium.exists() else pw.chromium.launch()
                ctx = nav.new_context(viewport={"width": 1500, "height": 950})
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: erros_js.append(str(e)))
                pg.on("console", lambda m: erros_js.append(m.text) if m.type == "error" else None)

                def dialogo(d):
                    dialogos.append(d.message)
                    d.accept()

                pg.on("dialog", dialogo)
                pg.goto(srv.url)
                pg.wait_for_selector("#blocos textarea")
                pg.wait_for_function("!document.querySelector('#ia-rodar').disabled", timeout=20000)
                ok(pg.locator("#ia-ctl").is_visible(), "o controle de IA aparece quando há IA na sessão")
                ok("gemma3:12b" in (pg.locator("#ia-rodar").get_attribute("title") or ""), "o botão informa o modelo")
                pg.locator('#lista .item[data-n="3"]').click()
                pg.wait_for_function("document.querySelector('#pagtit').textContent.includes('Página 3')")
                ok(pg.locator("#ia-sug .item").count() == 0, "antes de pedir, não há sugestões na tela")
                texto_antes = [t.input_value() for t in pg.locator("#blocos textarea").all()]
                pg.click("#ia-rodar")
                pg.wait_for_selector("#ia-sug .item", timeout=60000)
                n_sug = pg.locator("#ia-sug .item").count()
                ok(n_sug >= 1, f"a IA sugere {n_sug} troca(s) na página 3 (mostradas com o antes e o depois)")
                ok(pg.locator("#ia-sug del").count() == n_sug and pg.locator("#ia-sug ins").count() == n_sug,
                   "cada sugestão mostra o trecho atual riscado e o proposto")
                ok([t.input_value() for t in pg.locator("#blocos textarea").all()] == texto_antes, "as sugestões aparecem sem mexer no texto")
                pg.screenshot(path=str(tmp / "ia_claro.png"))
                de0 = pg.locator("#ia-sug del").first.inner_text()
                para0 = pg.locator("#ia-sug ins").first.inner_text()
                pg.locator("#ia-sug .item").first.locator("text=aceitar").first.click()
                pg.wait_for_timeout(300)
                tas = " ".join(t.input_value() for t in pg.locator("#blocos textarea").all())
                ok(para0 in tas and pg.locator("#ia-sug .item").count() == n_sug - 1, f"aceitar aplica '{de0}' → '{para0}' no texto e some da lista")
                ok(ia_f.resumo()["aceitas"] == 1, "o servidor registra a aceitação")
                if n_sug >= 3:
                    pg.locator("#ia-sug .item").first.locator("text=rejeitar").first.click()
                    pg.wait_for_timeout(300)
                    ok(pg.locator("#ia-sug .item").count() == n_sug - 2 and ia_f.resumo()["rejeitadas"] == 1, "rejeitar tira a sugestão sem mexer no texto")
                restantes = pg.locator("#ia-sug .item").count()
                if restantes:
                    pg.locator("#ia-sug .item .tx").first.click()
                    ok(pg.evaluate("document.activeElement.tagName") == "TEXTAREA", "clicar na sugestão leva ao trecho no texto")
                    pg.get_by_text("aceitar todas").click()
                    pg.wait_for_timeout(300)
                    ok(pg.locator("#ia-sug .item").count() == 0, "'aceitar todas' aplica as que sobraram nesta página")
                pg.keyboard.press("Control+Enter")
                pg.wait_for_timeout(700)
                salvo = " ".join(b["texto"] for b in ses.revisao.blocos(3) or [])
                ok(para0 in salvo and ses.revisao.revisada(3), "salvar a página grava o texto já corrigido")

                # passada em lote pela tela: andamento, Parar e continuar
                pg.select_option("#ia-escopo", "todas")
                pg.click("#ia-rodar")
                pg.wait_for_function("document.querySelector('#ia-status').textContent.startsWith('IA ')", timeout=15000)
                ok(pg.locator("#ia-parar").is_visible(), "a passada em lote mostra o andamento e o botão Parar")
                pg.click("#ia-parar")
                fim = time.time() + 60
                while time.time() < fim and ia_f.estado_trabalho()["ativo"]:
                    time.sleep(0.2)
                t1 = ia_f.estado_trabalho()
                ok(not t1["ativo"] and t1["feitas"] < t1["total"], f"Parar interrompe a passada ({t1['feitas']} de {t1['total']} páginas)")
                pg.wait_for_timeout(2600)
                pg.click("#ia-rodar")
                fim = time.time() + 120
                while time.time() < fim and (ia_f.estado_trabalho()["total"] == t1["total"] or ia_f.estado_trabalho()["ativo"]):
                    time.sleep(0.3)
                t2 = ia_f.estado_trabalho()
                ok(t2["total"] == t1["total"] - t1["feitas"] and t2["feitas"] == t2["total"],
                   f"rodar de novo continua só nas páginas que faltavam ({t2['feitas']}/{t2['total']})")
                pg.wait_for_timeout(2600)
                com_selo = pg.locator("#lista .selo.ia").count()
                ok(com_selo >= 1, f"ao terminar, as páginas com sugestões ganham o selo 'IA n' ({com_selo})")
                ok(pg.locator("#ia-parar").is_hidden(), "o botão Parar some quando a passada termina")

                # visual: modo escuro e celular numa página com sugestões
                alvo = next((p["n"] for p in ses.indice()["paginas"] if p["ia"] and p["n"] != 3), None)
                if alvo:
                    for nome, kw in (("ia_escuro.png", {"viewport": {"width": 1500, "height": 950}, "color_scheme": "dark"}),
                                     ("ia_celular.png", {"viewport": {"width": 390, "height": 844}})):
                        c2 = nav.new_context(**kw)
                        p2 = c2.new_page()
                        p2.goto(srv.url)
                        p2.wait_for_selector("#blocos textarea")
                        p2.locator(f'#lista .item[data-n="{alvo}"]').click()
                        p2.wait_for_selector("#ia-sug .item", timeout=15000)
                        p2.screenshot(path=str(tmp / nome), full_page=nome == "ia_celular.png")
                        if nome == "ia_celular.png":
                            ok(p2.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "no celular a tela não ganha rolagem horizontal")
                        c2.close()
                # concluir avisa que sugestões pendentes NÃO serão aplicadas
                pg.click("#concluir")
                pg.wait_for_timeout(900)
                ok(any("NÃO serão aplicadas" in d for d in dialogos), "ao concluir, a tela avisa que sugestões pendentes não serão aplicadas")
                ok(srv.concluido, "a revisão conclui normalmente")
                nav.close()
        finally:
            with contextlib.suppress(Exception):
                srv.server_close()
    ok(not erros_js, f"sem erros no console do navegador {erros_js[:2] if erros_js else ''}")
    return encerrar()


if __name__ == "__main__":
    sys.exit(main())
