"""Servidor local da tela de revisão do OCR.

Escuta só em 127.0.0.1, exige um token aleatório (na URL e no cabeçalho X-Token) e confere o cabeçalho Host,
para que outro site aberto no navegador não consiga ler nem alterar o projeto.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..ollama import OllamaErro
from .motor import caminho_imagem
from .revisao import Sessao
from .sugestoes_ia import ErroIA

_HTML = Path(__file__).with_name("revisao.html")
_MAX_CORPO = 8 * 1024 * 1024


class ServidorRevisao(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, sessao: Sessao, token: str | None = None, porta: int = 0):
        super().__init__(("127.0.0.1", porta), _Manipulador)
        self.sessao = sessao
        self.token = token or secrets.token_urlsafe(16)
        self.concluido = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/?t={self.token}"


class _Manipulador(BaseHTTPRequestHandler):
    server_version = "RevisaoOCR/1"

    def log_message(self, *args) -> None:  # silencioso
        pass

    # -- utilidades ------------------------------------------------------------------------------
    def _autorizado(self, qs: dict) -> bool:
        host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost"):
            return False
        token = self.headers.get("X-Token") or (qs.get("t") or [""])[0]
        return secrets.compare_digest(token, self.server.token)  # type: ignore[attr-defined]

    def _enviar(self, codigo: int, corpo: bytes, tipo: str, cache: str = "no-store") -> None:
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(corpo)

    def _json(self, dados, codigo: int = 200) -> None:
        self._enviar(codigo, json.dumps(dados, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    # -- GET -------------------------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/favicon.ico":  # o navegador pede sem token; não há nada a proteger aqui
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if not self._autorizado(qs):
            return self._json({"erro": "não autorizado"}, 403)
        sessao: Sessao = self.server.sessao  # type: ignore[attr-defined]
        rota = u.path
        if rota == "/":
            try:
                return self._enviar(200, _HTML.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                return self._json({"erro": "revisao.html não encontrado"}, 500)
        if rota == "/api/indice":
            return self._json(sessao.indice())
        if rota == "/api/sugestoes":
            return self._json(sessao.sugestoes)
        if rota == "/api/ia":
            return self._json(sessao.ia_estado())
        m = re.fullmatch(r"/api/ia/pagina/(\d+)", rota)
        if m:
            n = int(m.group(1))
            if n not in sessao.paginas:
                return self._json({"erro": "página inexistente"}, 404)
            return self._json({"sugestoes": sessao.ia_da_pagina(n)})
        m = re.fullmatch(r"/api/pagina/(\d+)", rota)
        if m:
            dados = sessao.pagina(int(m.group(1)))
            return self._json(dados) if dados else self._json({"erro": "página inexistente"}, 404)
        m = re.fullmatch(r"/img/(\d+)", rota)
        if m:
            arq = caminho_imagem(sessao.dir_img, int(m.group(1)))
            if arq.exists():
                return self._enviar(200, arq.read_bytes(), "image/jpeg", "private, max-age=600")
        return self._json({"erro": "não encontrado"}, 404)

    # -- POST ------------------------------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._autorizado(qs):
            return self._json({"erro": "não autorizado"}, 403)
        tamanho = int(self.headers.get("Content-Length") or 0)
        if tamanho > _MAX_CORPO:
            return self._json({"erro": "corpo grande demais"}, 413)
        try:
            corpo = json.loads(self.rfile.read(tamanho) or b"{}")
            if not isinstance(corpo, dict):
                raise ValueError
        except ValueError:
            return self._json({"erro": "JSON inválido"}, 400)
        sessao: Sessao = self.server.sessao  # type: ignore[attr-defined]
        rota = u.path

        if rota.startswith("/api/ia/"):
            return self._post_ia(sessao, rota, corpo)

        m = re.fullmatch(r"/api/pagina/(\d+)", rota)
        if m:
            n = int(m.group(1))
            if n not in sessao.paginas:
                return self._json({"erro": "página inexistente"}, 404)
            blocos = corpo.get("blocos")
            if not isinstance(blocos, list):
                return self._json({"erro": "blocos ausentes"}, 400)
            rev = corpo.get("revisada")
            sessao.salvar(n, blocos, None if rev is None else bool(rev))
            item = next((p for p in sessao.indice()["paginas"] if p["n"] == n), None)
            return self._json({"ok": True, "indice": item})
        m = re.fullmatch(r"/api/marcar/(\d+)", rota)
        if m:
            sessao.marcar(int(m.group(1)), bool(corpo.get("revisada", True)))
            return self._json({"ok": True})
        if rota == "/api/substituir":
            r = sessao.substituir(str(corpo.get("de", "")), str(corpo.get("para", "")),
                                  bool(corpo.get("palavra_inteira", True)), bool(corpo.get("ignorar_caixa", False)),
                                  bool(corpo.get("aplicar", False)))
            return self._json(r)
        if rota == "/api/concluir":
            self.server.concluido = True  # type: ignore[attr-defined]
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return None
        return self._json({"erro": "não encontrado"}, 404)

    def _post_ia(self, sessao: Sessao, rota: str, corpo: dict) -> None:
        """Sugestões por IA. Os erros viram mensagens para a pessoa (502 = o Ollama não deu conta)."""
        try:
            m = re.fullmatch(r"/api/ia/pagina/(\d+)", rota)
            if m:
                return self._json(sessao.ia_gerar_pagina(int(m.group(1))))
            if rota == "/api/ia/lote":
                return self._json(sessao.ia_iniciar_lote(str(corpo.get("escopo", "sinalizadas"))))
            if rota == "/api/ia/parar":
                sessao.ia_parar()
                return self._json({"ok": True})
            if rota in ("/api/ia/aceitar", "/api/ia/rejeitar"):
                ids = corpo.get("ids")
                if not isinstance(ids, list):
                    return self._json({"erro": "ids ausentes"}, 400)
                sessao.ia_marcar(ids, rota.endswith("aceitar"))
                return self._json({"ok": True})
        except OllamaErro as e:
            return self._json({"erro": str(e)}, 502)
        except ErroIA as e:
            return self._json({"erro": str(e)}, 409)
        return self._json({"erro": "não encontrado"}, 404)


def iniciar_em_thread(sessao: Sessao, porta: int = 0) -> ServidorRevisao:
    """Usado nos testes: sobe o servidor em segundo plano."""
    srv = ServidorRevisao(sessao, porta=porta)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    return srv


def rodar(sessao: Sessao, abrir_navegador: bool = True, saida=print, ia_lote: str | None = None) -> bool:
    """Bloqueia até a pessoa clicar em 'Concluir revisão'. Devolve False se foi interrompido com Ctrl+C.

    `ia_lote` ('sinalizadas' | 'todas') já põe o modelo para sugerir correções em segundo plano."""
    srv = ServidorRevisao(sessao)
    saida(f"Tela de revisão: {srv.url}")
    if ia_lote:
        try:
            r = sessao.ia_iniciar_lote(ia_lote)
            saida(f"IA: sugerindo correções em {r.get('total', 0)} página(s) em segundo plano "
                  f"(modelo {sessao.ia.modelo_efetivo()[0]}); elas aparecem na tela conforme ficam prontas."
                  if r.get("total") else "IA: nenhuma página pendente para sugerir.")
        except (ErroIA, OllamaErro) as e:
            saida(f"IA: não foi possível começar ({e}). Você pode tentar de novo pelo botão na tela.")
    if abrir_navegador:
        try:
            webbrowser.open(srv.url)
        except Exception:  # noqa: BLE001
            pass
    saida("Corrija o que precisar e clique em 'Concluir revisão' no navegador (Ctrl+C aqui pausa; o que foi salvo fica).")
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return srv.concluido
