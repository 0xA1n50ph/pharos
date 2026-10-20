"""Servidor que imita a API do Ollama (/api/tags, /api/chat, /api/generate) para testar sem modelo.

Gera "traduções" em português com o tamanho do original, aplica os termos do glossário que aparecem no
prompt e permite injetar falhas para testar validação, retentativa e relatório.

Variáveis de ambiente:
  FALSO_LOG              arquivo .jsonl onde cada requisição é registrada
  FALSO_ATRASO           segundos de espera por requisição (para testar Ctrl+C e retomada)
  FALSO_FALHA_UMA_VEZ    se o texto contém isto, a 1ª tentativa devolve o original (e a 2ª acerta)
  FALSO_FALHA_SEMPRE     se o texto contém isto, todas as tentativas devolvem algo curto demais
  FALSO_OCR_TROCAS       "errado=certo;errado2=certo2": o "revisor de OCR" aplica estas trocas ao texto recebido
  FALSO_OCR_TROCAS_ARQ   arquivo JSON {"errado": "certo"} lido a cada pedido (troca palavras inteiras)
  FALSO_OCR_MODO         "" (aplica as trocas) | traduz | preambulo | cerca | erro | lento
  FALSO_OCR_ATRASO       segundos de espera por pedido no modo "lento" (padrão 1.5)
  FALSO_MODELOS          modelos "instalados", separados por vírgula (padrão: translategemma:12b,gemma3:12b; vazio = nenhum)
  FALSO_ESTRITO          se "1", /api/chat e /api/generate respondem 404 para modelo não instalado (como o Ollama de verdade)
  FALSO_PULL_MODO        "" (baixa e instala) | erro (devolve {"error": ...}) | cai1 (a 1ª tentativa de cada modelo corta a
                         conexão no meio; a 2ª funciona) | cai (sempre corta) | lento (0,3 s por pedaço)
  FALSO_BIND             endereço onde escutar (padrão 127.0.0.1)

/api/pull imita o Ollama: devolve linhas JSON ("pulling manifest", "pulling <digest>" com total/completed, ... "success").
Modelos cujo nome contém "naoexiste" respondem com o erro de manifesto que o Ollama devolve.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

FRASES = [
    "O vento chegou de manhã cedo e trouxe consigo o cheiro do sal e da madeira úmida, e ele não disse nada.",
    "Ela sabia que a luz era para os outros, mas não podia deixar de pensar nas coisas que não via.",
    "Depois de uma hora de silêncio, o velho voltou para a casa e fechou a porta com cuidado.",
    "Havia no mar uma calma que não era natural, como se ele esperasse por alguma coisa que só ele conhecia.",
]
vistos: dict[str, int] = {}
_env_modelos = os.environ.get("FALSO_MODELOS")
MODELOS: list[str] = [m for m in (_env_modelos if _env_modelos is not None else "translategemma:12b,gemma3:12b").split(",") if m]
PULLS: dict[str, int] = {}


def _registrar(obj: dict) -> None:
    log = os.environ.get("FALSO_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _instalado(nome: str) -> bool:
    return nome in MODELOS or (":" not in nome and nome + ":latest" in MODELOS)


def _extrair_texto(mensagens: list[dict]) -> str:
    ultimo = mensagens[-1]["content"]
    if "Please translate the following" in ultimo:
        return ultimo.split(":\n\n\n", 1)[-1]
    if "Text to translate:\n\n" in ultimo:
        return ultimo.split("Text to translate:\n\n", 1)[-1]
    return ultimo


def _termos_do_glossario(mensagens: list[dict]) -> list[str]:
    tudo = "\n".join(m["content"] for m in mensagens)
    termos = []
    if "Mandatory glossary" in tudo:
        bloco = tudo.split("Mandatory glossary", 1)[1]
        for linha in bloco.splitlines()[1:]:
            m = re.match(r"- (.+?) -> (.+?)( \(.*\))?$", linha)
            if not m:
                break
            termos.append(m.group(2))
    return termos


def _revisar_ocr(mensagens: list[dict]) -> str:
    """Imita um modelo que corrige OCR: devolve o texto entre <text>...</text> com as trocas de FALSO_OCR_TROCAS."""
    usuario = mensagens[-1]["content"]
    m = re.search(r"<text>\n(.*)\n</text>", usuario, re.S)
    texto = m.group(1) if m else usuario
    modo = os.environ.get("FALSO_OCR_MODO", "")
    if modo == "traduz":
        return " ".join(["O vento chegou de manhã cedo e trouxe consigo o cheiro do sal."] * max(1, len(texto) // 70))
    for par in filter(None, os.environ.get("FALSO_OCR_TROCAS", "").split(";")):
        errado, _, certo = par.partition("=")
        texto = texto.replace(errado, certo)
    arq = os.environ.get("FALSO_OCR_TROCAS_ARQ")
    if arq and os.path.exists(arq):
        with open(arq, encoding="utf-8") as f:
            for errado, certo in json.load(f).items():
                texto = re.sub(r"(?<!\w)" + re.escape(errado) + r"(?!\w)", certo, texto)
    if modo == "preambulo":
        return "Here is the corrected text:\n" + texto
    if modo == "cerca":
        return "```\n" + texto + "\n```"
    return texto


def _traduzir(mensagens: list[dict]) -> str:
    texto = _extrair_texto(mensagens)
    tudo = "\n".join(m["content"] for m in mensagens)
    chave = texto.strip()
    vistos[chave] = vistos.get(chave, 0) + 1

    sempre = os.environ.get("FALSO_FALHA_SEMPRE")
    uma_vez = os.environ.get("FALSO_FALHA_UMA_VEZ")
    if sempre and sempre in texto:
        return "Curto."
    if uma_vez and uma_vez in texto and vistos[chave] == 1:
        return texto  # devolve sem traduzir

    linhas = [l for l in texto.split("\n") if l.strip()]
    if "keep exactly" in tudo and len(linhas) > 1:  # verso
        return "\n".join(f"Verso traduzido número {i + 1} do poema." for i in range(len(linhas)))
    if len(texto) < 60:
        return "Título traduzido" if texto.strip() else texto
    alvo = int(len(texto) * 1.05)
    saida, i = "", 0
    while len(saida) < alvo:
        saida += FRASES[i % len(FRASES)] + " "
        i += 1
    saida = saida.strip()
    extras = _termos_do_glossario(mensagens)
    if extras:
        saida += " (" + ", ".join(extras) + ")"
    return saida


class Manipulador(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silencioso
        pass

    def _json(self, dados, codigo=200):
        corpo = json.dumps(dados).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):
        if self.path == "/api/tags":
            return self._json({"models": [{"name": m, "model": m, "size": 8100000000} for m in MODELOS]})
        self._json({}, 404)

    def _pull(self, modelo: str) -> None:
        PULLS[modelo] = PULLS.get(modelo, 0) + 1
        _registrar({"pull": modelo, "tentativa": PULLS[modelo]})
        modo = os.environ.get("FALSO_PULL_MODO", "")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()

        def linha(d):
            self.wfile.write((json.dumps(d) + "\n").encode())
            self.wfile.flush()
        linha({"status": "pulling manifest"})
        if "naoexiste" in modelo:
            return linha({"error": "pull model manifest: file does not exist"})
        if modo == "erro":
            return linha({"error": "falha simulada no download"})
        total = 8100000000
        for camada, tam in (("sha256:aaaaaaaaaaaa", total), ("sha256:bbbbbbbbbbbb", 1200)):
            for i in range(0, 11):
                linha({"status": "pulling " + camada[:19], "digest": camada, "total": tam, "completed": tam * i // 10})
                if modo == "lento":
                    time.sleep(0.3)
                if (modo == "cai" or (modo == "cai1" and PULLS[modelo] == 1)) and camada.startswith("sha256:a") and i == 4:
                    self.wfile.flush()
                    self.connection.close()   # corta no meio, sem "success"
                    return
        linha({"status": "verifying sha256 digest"})
        linha({"status": "writing manifest"})
        linha({"status": "success"})
        if modelo not in MODELOS:
            MODELOS.append(modelo)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        corpo = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/pull":
            return self._pull(corpo.get("model") or corpo.get("name") or "")
        estrito = os.environ.get("FALSO_ESTRITO") == "1"
        if estrito and self.path in ("/api/chat", "/api/generate") and not _instalado(corpo.get("model", "")):
            return self._json({"error": "model '%s' not found" % corpo.get("model", "")}, 404)
        if self.path == "/api/generate":
            return self._json({"done": True})
        if self.path == "/api/chat":
            atraso = float(os.environ.get("FALSO_ATRASO", "0"))
            if atraso:
                time.sleep(atraso)
            msgs = corpo["messages"]
            log = os.environ.get("FALSO_LOG")
            if log:
                with open(log, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"modelo": corpo.get("model"), "mensagens": msgs}, ensure_ascii=False) + "\n")
            ultimo = msgs[-1]["content"]
            if "meticulous proofreader of OCR" in msgs[0]["content"]:
                if os.environ.get("FALSO_OCR_MODO") == "erro":
                    return self._json({"error": "falha simulada"}, 500)
                if os.environ.get("FALSO_OCR_MODO") == "lento":
                    time.sleep(float(os.environ.get("FALSO_OCR_ATRASO", "1.5")))
                resposta = _revisar_ocr(msgs)
            elif "Answer with ONLY a JSON object" in ultimo:  # sugestão de perfil
                resposta = json.dumps({"genero": "ficcao", "obra": "Novela de teste sobre um farol",
                                       "registro": "Sober literary prose with short, rhythmic sentences."})
            else:
                resposta = _traduzir(msgs)
            return self._json({"message": {"role": "assistant", "content": resposta}, "done": True})
        self._json({}, 404)


if __name__ == "__main__":
    porta = int(sys.argv[1]) if len(sys.argv) > 1 else 11999
    from http.server import ThreadingHTTPServer
    ThreadingHTTPServer((os.environ.get("FALSO_BIND", "127.0.0.1"), porta), Manipulador).serve_forever()
