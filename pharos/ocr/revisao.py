"""Edições feitas na revisão do OCR: armazenamento, substituição global e sessão usada pela interface web."""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from .sugestoes_ia import ErroIA, SugestoesIA, padrao_da_palavra

TIPOS = {"paragrafo", "titulo", "verso", "nota", "legenda", "separador"}
_MAX_TEXTO = 200_000


def _limpar_bloco(b: dict) -> dict | None:
    if not isinstance(b, dict):
        return None
    tipo = b.get("tipo") if b.get("tipo") in TIPOS else "paragrafo"
    texto = str(b.get("texto", ""))[:_MAX_TEXTO].replace("\r\n", "\n").replace("\r", "\n")
    if tipo != "verso":
        texto = " ".join(texto.split())
    else:
        texto = "\n".join(l.strip() for l in texto.split("\n") if l.strip())
    if not texto.strip():
        return None
    try:
        nivel = max(1, min(4, int(b.get("nivel", 1))))
    except (TypeError, ValueError):
        nivel = 1
    return {"tipo": tipo, "texto": texto, "nivel": nivel, "recuo": True}


class Revisao:
    """Páginas editadas à mão (blocos) e páginas marcadas como revisadas. Gravação atômica a cada alteração."""

    def __init__(self, caminho: Path, hash_ocr: str):
        self.caminho = Path(caminho)
        self.trava = threading.RLock()
        self.dados: dict = {"hash": hash_ocr, "paginas": {}}
        if self.caminho.exists():
            try:
                lido = json.loads(self.caminho.read_text(encoding="utf-8"))
                if lido.get("hash") == hash_ocr:
                    self.dados = lido
                else:  # o OCR foi refeito com outros parâmetros: as edições antigas não valem mais
                    self.caminho.replace(self.caminho.with_suffix(".antigo.json"))
            except (json.JSONDecodeError, OSError):
                pass

    def _gravar(self) -> None:
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.caminho.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.dados, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.caminho)

    def editada(self, n: int) -> bool:
        with self.trava:
            return "blocos" in self.dados["paginas"].get(str(n), {})

    def revisada(self, n: int) -> bool:
        with self.trava:
            return bool(self.dados["paginas"].get(str(n), {}).get("revisada"))

    def blocos(self, n: int) -> list[dict] | None:
        with self.trava:
            p = self.dados["paginas"].get(str(n), {})
            return [dict(b) for b in p["blocos"]] if "blocos" in p else None

    def salvar(self, n: int, blocos: list[dict] | None, revisada: bool | None = None) -> None:
        with self.trava:
            p = self.dados["paginas"].setdefault(str(n), {})
            if blocos is not None:
                p["blocos"] = [b for b in (_limpar_bloco(x) for x in blocos) if b]
            if revisada is not None:
                p["revisada"] = bool(revisada)
            self._gravar()

    def revisadas(self) -> set[int]:
        with self.trava:
            return {int(n) for n, p in self.dados["paginas"].items() if p.get("revisada")}

    def editadas(self) -> set[int]:
        with self.trava:
            return {int(n) for n, p in self.dados["paginas"].items() if "blocos" in p}


class Sessao:
    """Tudo que a interface de revisão precisa, atrás de uma trava (o servidor atende em várias threads)."""

    def __init__(self, titulo: str, paginas: dict[int, dict], auto: dict[int, list[dict]], pontos: dict[int, dict],
                 sugestoes: list[dict], revisao: Revisao, dir_img: Path, limiar_palavra: int = 70,
                 filtro_inicial: str = "sinalizadas", ia: SugestoesIA | None = None):
        self.titulo, self.paginas, self.auto, self.pontos = titulo, paginas, auto, pontos
        self.sugestoes, self.revisao, self.dir_img = sugestoes, revisao, Path(dir_img)
        self.ia = ia
        self.limiar_palavra, self.filtro_inicial = limiar_palavra, filtro_inicial
        self.trava = threading.RLock()

    def blocos(self, n: int) -> list[dict]:
        with self.trava:
            editados = self.revisao.blocos(n)
            return editados if editados is not None else [dict(b) for b in self.auto.get(n, [])]

    def indice(self) -> dict:
        with self.trava:
            itens = []
            for n in sorted(self.paginas):
                p = self.pontos.get(n, {})
                itens.append({"n": n, "score": p.get("score", 0), "sinalizada": bool(p.get("sinalizada")),
                              "duvidosas": p.get("duvidosas", 0), "revisada": self.revisao.revisada(n),
                              "editada": self.revisao.editada(n), "motivos": p.get("motivos", []),
                              "ia": len(self.ia_da_pagina(n)) if self.ia else 0})
            return {"titulo": self.titulo, "paginas": itens, "filtro": self.filtro_inicial,
                    "limiar_palavra": self.limiar_palavra, "sugestoes_total": len(self.sugestoes),
                    "ia": self.ia is not None}

    def pagina(self, n: int) -> dict | None:
        with self.trava:
            pag = self.paginas.get(n)
            if pag is None:
                return None
            W, H = pag["largura"], pag["altura"]
            caixas = [[round(p[3] / W, 5), round(p[4] / H, 5), round(p[5] / W, 5), round(p[6] / H, 5), p[7], p[8]]
                      for p in pag["palavras"] if p[7] < self.limiar_palavra and any(c.isalnum() for c in p[8])]
            return {"n": n, "blocos": self.blocos(n), "caixas": caixas, "ponto": self.pontos.get(n, {}),
                    "editada": self.revisao.editada(n), "revisada": self.revisao.revisada(n),
                    "sugestoes": [s for s in self.sugestoes if n in s["paginas"]],
                    "ia": self.ia_da_pagina(n)}

    def salvar(self, n: int, blocos: list[dict], revisada: bool | None) -> None:
        with self.trava:
            if n in self.paginas:
                self.revisao.salvar(n, blocos, revisada)

    def marcar(self, n: int, revisada: bool) -> None:
        with self.trava:
            if n in self.paginas:
                self.revisao.salvar(n, None, revisada)

    def substituir(self, de: str, para: str, palavra_inteira: bool = True, ignorar_caixa: bool = False,
                   aplicar: bool = False) -> dict:
        """Troca texto em todas as páginas. Sem aplicar, só conta."""
        if not de:
            return {"ocorrencias": 0, "paginas": 0, "aplicado": False}
        padrao = re.escape(de)
        if palavra_inteira:
            padrao = r"(?<![^\W_])" + padrao + r"(?![^\W_])"
        rx = re.compile(padrao, re.IGNORECASE if ignorar_caixa else 0)
        total, afetadas = 0, []
        with self.trava:
            for n in sorted(self.paginas):
                blocos = self.blocos(n)
                mudou = False
                for b in blocos:
                    novo, k = rx.subn(lambda _m: para, b["texto"])
                    if k:
                        total += k
                        mudou = True
                        b["texto"] = novo
                if mudou:
                    afetadas.append(n)
                    if aplicar:
                        self.revisao.salvar(n, blocos, None)
        return {"ocorrencias": total, "paginas": len(afetadas), "lista": afetadas[:30], "aplicado": bool(aplicar)}

    def blocos_finais(self) -> dict[int, list[dict]]:
        with self.trava:
            return {n: self.blocos(n) for n in sorted(self.paginas)}

    # -- sugestões por IA (só sugerem; quem aplica é a pessoa, na tela) -------------------------
    def _exigir_ia(self) -> SugestoesIA:
        if self.ia is None:
            raise ErroIA("As sugestões por IA não estão ativas nesta sessão.")
        return self.ia

    def ia_da_pagina(self, n: int) -> list[dict]:
        """Sugestões ainda não aceitas nem rejeitadas e que ainda se aplicam ao texto atual da página."""
        if self.ia is None:
            return []
        with self.trava:
            textos = [b["texto"] for b in self.blocos(n)]
        saida = []
        for sg in self.ia.listar(n):
            rx = padrao_da_palavra(sg["de"])
            if any(rx.search(t) for t in textos):
                saida.append(sg)
        return saida

    def ia_estado(self) -> dict:
        if self.ia is None:
            return {"disponivel": False, "motivo": "As sugestões por IA não estão ativas nesta sessão.",
                    "modelo": "", "aviso": "", "trabalho": {"ativo": False}, "totais": {}}
        return self.ia.estado()

    def _fonte_ia(self, n: int) -> tuple[list[dict], list]:
        with self.trava:
            return self.blocos(n), self.paginas[n]["palavras"]

    def ia_gerar_pagina(self, n: int) -> dict:
        """Pede ao modelo as sugestões de uma página (bloqueia até terminar)."""
        ia = self._exigir_ia()
        if n not in self.paginas:
            raise ErroIA("Página inexistente.")
        blocos, palavras = self._fonte_ia(n)
        ia.gerar_pagina(n, blocos, palavras)
        return {"sugestoes": self.ia_da_pagina(n), "info": ia.info_pagina(n)}

    def paginas_para_ia(self, escopo: str) -> list[int]:
        with self.trava:
            alvo = []
            for n in sorted(self.paginas):
                if self.revisao.revisada(n) or (self.ia and self.ia.tem(n)):
                    continue
                if escopo == "sinalizadas" and not self.pontos.get(n, {}).get("sinalizada"):
                    continue
                if self.pontos.get(n, {}).get("palavras", 1) < 8:
                    continue
                alvo.append(n)
            return alvo

    def ia_iniciar_lote(self, escopo: str) -> dict:
        ia = self._exigir_ia()
        if escopo not in ("sinalizadas", "todas"):
            raise ErroIA("Escopo inválido.")
        ok, motivo = ia.disponibilidade()
        if not ok:
            raise ErroIA(motivo)
        paginas = self.paginas_para_ia(escopo)
        if not paginas:
            return {"ativo": False, "total": 0, "feitas": 0, "sugestoes": 0, "erro": "", "escopo": escopo, "atual": None,
                    "parar": False, "nada": True}
        return ia.iniciar_lote(paginas, self._fonte_ia, escopo)

    def ia_parar(self) -> None:
        if self.ia:
            self.ia.parar()

    def ia_marcar(self, ids: list, aceitas: bool) -> None:
        self._exigir_ia().marcar([i for i in ids if isinstance(i, str)][:500], aceitas)
