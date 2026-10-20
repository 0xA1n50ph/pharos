"""Interação com o usuário no terminal (perguntas, confirmações, progresso)."""
from __future__ import annotations

import sys
import time


class SaidaDoUsuario(Exception):
    """O usuário escolheu sair (ou a entrada acabou). O projeto fica salvo para retomar."""


def _hms(segundos: float) -> str:
    s = int(max(0, segundos))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


class UI:
    def __init__(self, auto: bool = False):
        self.auto = auto
        self.tty = sys.stdout.isatty()

    def info(self, msg: str = "") -> None:
        print(msg, flush=True)

    def titulo(self, msg: str) -> None:
        print("\n" + msg + "\n" + "─" * len(msg), flush=True)

    def aviso(self, msg: str) -> None:
        print("Aviso: " + msg, flush=True)

    def erro(self, msg: str) -> None:
        print("Erro: " + msg, file=sys.stderr, flush=True)

    def perguntar(self, texto: str, padrao: str = "") -> str:
        sufixo = f" [{padrao}]" if padrao else ""
        if self.auto:
            print(f"{texto}{sufixo}: {padrao}  (automático)", flush=True)
            return padrao
        try:
            resposta = input(f"{texto}{sufixo}: ").strip()
        except EOFError:
            raise SaidaDoUsuario("a entrada terminou") from None
        return resposta or padrao

    def confirmar(self, texto: str, padrao: bool = True) -> bool:
        r = self.perguntar(texto + (" (S/n)" if padrao else " (s/N)"), "").lower()
        if not r:
            return padrao
        return r in ("s", "sim", "y", "yes")

    def multilinha(self, texto: str) -> str:
        """Lê linhas até uma linha vazia."""
        if self.auto:
            return ""
        print(texto, flush=True)
        linhas = []
        while True:
            try:
                l = input()
            except EOFError:
                break
            if not l.strip():
                break
            linhas.append(l)
        return "\n".join(linhas).strip()


class Progresso:
    """Callback de progresso da tradução, com ETA."""

    def __init__(self, ui: UI, rotulo: str = "Traduzindo"):
        self.ui = ui
        self.rotulo = rotulo
        self.inicio = time.monotonic()
        self.ultimo = self.inicio
        self.duracoes: list[float] = []
        self.alertas = 0

    def __call__(self, i: int, total: int, bloco, resultado) -> None:
        agora = time.monotonic()
        if not resultado.do_cache:
            self.duracoes.append(agora - self.ultimo)
        self.ultimo = agora
        novos = [a for a in resultado.alertas if a not in ("ja_no_idioma_de_destino", "corrigido_manualmente")]
        if novos and not resultado.do_cache:
            self.alertas += 1
            print(f"\n  ! {bloco.id}: {', '.join(novos)}", flush=True)
        media = sum(self.duracoes[-30:]) / max(1, len(self.duracoes[-30:]))
        eta = media * (total - i)
        linha = f"{self.rotulo}: {i}/{total} ({100 * i // max(1, total)}%)  restante ~{_hms(eta)}  {bloco.id}"
        if self.ui.tty:
            sys.stdout.write("\r" + linha.ljust(78))
            if i == total:
                sys.stdout.write("\n")
            sys.stdout.flush()
        elif i % 10 == 0 or i == total:
            print(linha, flush=True)


class Barra:
    """Progresso simples com tempo restante, para etapas longas (OCR, por exemplo)."""

    def __init__(self, ui: UI, rotulo: str):
        self.ui = ui
        self.rotulo = rotulo
        self.inicio = time.monotonic()
        self._passo_log = 0

    def __call__(self, feitos: int, total: int, detalhe: str = "") -> None:
        dec = time.monotonic() - self.inicio
        eta = dec / feitos * (total - feitos) if feitos else 0
        linha = f"{self.rotulo}: {feitos}/{total} ({100 * feitos // max(1, total)}%)  restante ~{_hms(eta)}"
        if detalhe:
            linha += f"  {detalhe}"
        if self.ui.tty:
            sys.stdout.write("\r" + linha.ljust(78))
            if feitos == total:
                sys.stdout.write("\n")
            sys.stdout.flush()
        else:
            passo = max(1, total // 10)
            if feitos % passo == 0 or feitos == total:
                print(linha, flush=True)
