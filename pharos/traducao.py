"""Motor de tradução: contexto, validação, retentativas e cache (para retomar de onde parou)."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import glossario as gl
from .config import Config
from .idioma import detectar_trecho
from .modelo import Bloco, Livro, NOTA, PARAGRAFO, TIPOS_TRADUZIVEIS, TITULO, VERSO
from .ollama import Ollama, OllamaErro
from .perfil import Perfil, construir_mensagens, estilo_do_modelo

_CJK = {"ja", "ko", "zh-cn", "zh-tw"}
_RAZAO = (0.5, 2.2)

_RE_PREAMBULO = re.compile(
    r"^(here(?:'s| is)[^\n]{0,80}:|aqui est[aá][^\n]{0,80}:|claro[,!][^\n]{0,80}:|translation:|"
    r"tradu[cç][aã]o:|traducci[oó]n:|[uü]bersetzung:)\s*\n?",
    re.IGNORECASE,
)


@dataclass
class Resultado:
    texto: str
    alertas: list = field(default_factory=list)
    tentativas: int = 0
    do_cache: bool = False


class CacheTraducao:
    """Um JSON por linha; cada tradução é gravada na hora, então Ctrl+C não perde nada."""

    def __init__(self, caminho: Path):
        self.caminho = Path(caminho)
        self.dados: dict[str, dict] = {}
        if self.caminho.exists():
            for linha in self.caminho.read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(linha)
                except json.JSONDecodeError:
                    continue  # última linha cortada por uma interrupção
                self.dados[d["chave"]] = d

    def get(self, chave: str) -> dict | None:
        return self.dados.get(chave)

    def put(self, chave: str, bloco_id: str, resultado: Resultado) -> None:
        d = {"chave": chave, "id": bloco_id, "traducao": resultado.texto,
             "alertas": resultado.alertas, "tentativas": resultado.tentativas}
        self.dados[chave] = d
        with open(self.caminho, "a", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


class Correcoes:
    """Traduções corrigidas à mão durante a prévia: valem sempre, em qualquer versão do perfil."""

    def __init__(self, caminho: Path):
        self.caminho = Path(caminho)
        self.dados: dict[str, str] = {}
        if self.caminho.exists():
            try:
                self.dados = json.loads(self.caminho.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.dados = {}

    @staticmethod
    def _chave(bloco: Bloco) -> str:
        return bloco.id + ":" + hashlib.sha1(bloco.texto.encode("utf-8")).hexdigest()[:10]

    def get(self, bloco: Bloco) -> str | None:
        return self.dados.get(self._chave(bloco))

    def put(self, bloco: Bloco, texto: str) -> None:
        self.dados[self._chave(bloco)] = texto
        self.caminho.write_text(json.dumps(self.dados, ensure_ascii=False, indent=1), encoding="utf-8")


ALERTAS_INFORMATIVOS = {"ja_no_idioma_de_destino", "corrigido_manualmente"}


def dividir_em_frases(texto: str, limite: int) -> list[str]:
    """Parte um texto longo em pedaços de até `limite` caracteres, preferindo fronteiras de frase."""
    partes: list[str] = []
    atual = ""
    for frase in re.split(r"(?<=[.!?…»”\"])\s+", texto):
        while len(frase) > limite:  # frase gigante, sem pontuação: corta no espaço mais próximo
            corte = frase.rfind(" ", 0, limite)
            corte = corte if corte > 0 else limite
            if atual:
                partes.append(atual)
                atual = ""
            partes.append(frase[:corte])
            frase = frase[corte:].lstrip()
        if atual and len(atual) + 1 + len(frase) > limite:
            partes.append(atual)
            atual = frase
        else:
            atual = (atual + " " + frase).strip()
    if atual:
        partes.append(atual)
    return partes


def _limpar_saida(bruto: str, bloco: Bloco) -> str:
    t = bruto.strip()
    t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
    t = re.sub(r"\n?```$", "", t).strip()
    t = _RE_PREAMBULO.sub("", t).strip()
    if len(t) > 2 and t[0] in "\"“«" and t[-1] in "\"”»" and bloco.texto[:1] not in "\"“«„»'‘":
        t = t[1:-1].strip()
    return t


class Tradutor:
    def __init__(self, cfg: Config, perfil: Perfil, glossario_path: Path, ollama: Ollama, cache: CacheTraducao,
                 correcoes: Correcoes | None = None):
        self.cfg = cfg
        self.perfil = perfil
        self.correcoes = correcoes
        self.glossario = gl.carregar(glossario_path)
        self.hash_glossario = hashlib.sha1(gl.texto_do_arquivo(glossario_path).encode()).hexdigest()[:12]
        self.ollama = ollama
        self.cache = cache
        self.estilo = estilo_do_modelo(cfg)

    # -- chaves de cache -----------------------------------------------------------------------
    def chave(self, bloco: Bloco, usar_perfil: bool = True) -> str:
        partes = [bloco.texto, bloco.tipo, self.cfg.origem, self.cfg.destino, self.cfg.modelo, self.estilo]
        if usar_perfil:
            partes += ["perfil", self.perfil.hash(), self.hash_glossario, self.cfg.variante_destino,
                       str(self.cfg.contexto_blocos)]
        else:
            partes += ["base"]
        return hashlib.sha1("\x1f".join(partes).encode("utf-8")).hexdigest()

    # -- o que dá para resolver sem chamar o modelo -------------------------------------------
    def resolver(self, bloco: Bloco, usar_perfil: bool = True) -> Resultado | None:
        if bloco.tipo not in TIPOS_TRADUZIVEIS or not any(c.isalpha() for c in bloco.texto):
            return Resultado(bloco.texto, [], 0, True)
        if usar_perfil and self.correcoes:
            corrigido = self.correcoes.get(bloco)
            if corrigido:
                return Resultado(corrigido, ["corrigido_manualmente"], 0, True)
        hit = self.cache.get(self.chave(bloco, usar_perfil))
        if hit:
            return Resultado(hit["traducao"], hit["alertas"], hit["tentativas"], True)
        if self.cfg.validar_idioma and self.cfg.origem != self.cfg.destino and len(bloco.texto) >= 80:
            r = detectar_trecho(bloco.texto)
            if r and r[0] == self.cfg.destino and r[1] >= 0.8:
                return Resultado(bloco.texto, ["ja_no_idioma_de_destino"], 0, True)
        return None

    # -- validação -----------------------------------------------------------------------------
    def _validar(self, bloco: Bloco, texto: str, termos: list[gl.Termo]) -> list[tuple[str, str]]:
        p: list[tuple[str, str]] = []
        cfg = self.cfg
        if not texto.strip():
            return [("vazio", "the answer was empty.")]
        if bloco.tipo != VERSO and "\n" in texto.strip():
            p.append(("quebras", "the answer has extra line breaks or extra paragraphs; output a single paragraph."))
        if bloco.tipo == VERSO:
            n_src = len([l for l in bloco.texto.split("\n") if l.strip()])
            n_dst = len([l for l in texto.split("\n") if l.strip()])
            if n_src != n_dst:
                p.append(("linhas", f"the source has {n_src} lines but the answer has {n_dst}."))
        if (len(bloco.texto) >= 80 and cfg.origem not in _CJK and cfg.destino not in _CJK
                and bloco.tipo != TITULO):
            razao = len(texto) / max(1, len(bloco.texto))
            if razao < _RAZAO[0]:
                p.append(("tamanho", "the answer is much shorter than the source; something was omitted."))
            elif razao > _RAZAO[1]:
                p.append(("tamanho", "the answer is much longer than the source; something was added."))
        if cfg.origem != cfg.destino:
            if len(bloco.texto) >= 40 and texto.strip() == bloco.texto.strip():
                p.append(("nao_traduzido", "the answer is identical to the source text."))
            elif cfg.validar_idioma and len(texto) >= 80:
                r = detectar_trecho(texto)
                if r and r[0] == cfg.origem and r[0] != cfg.destino and r[1] >= 0.7:
                    p.append(("idioma", f"the answer is still in the source language; write it in {cfg.destino}."))
        faltam = gl.faltando(termos, texto)
        if faltam:
            lista = "; ".join(f"{t.origem} -> {t.destino}" for t in faltam)
            p.append(("glossario", f"glossary terms not applied: {lista}."))
        return p

    # -- chamada ao modelo ---------------------------------------------------------------------
    def traduzir(self, bloco: Bloco, contexto: list[tuple[str, str | None]] | None = None,
                 usar_perfil: bool = True) -> Resultado:
        pronto = self.resolver(bloco, usar_perfil)
        if pronto is not None:
            return pronto
        if bloco.tipo in (PARAGRAFO, NOTA) and len(bloco.texto) > self.cfg.max_caracteres_bloco:
            # parágrafo gigante (OCR ruim, TXT sem quebras): traduz por partes, em fronteiras de frase
            partes = dividir_em_frases(bloco.texto, self.cfg.max_caracteres_bloco)
            rs = [
                self._traduzir_um(Bloco(f"{bloco.id}.{i}", bloco.tipo, p, bloco.pagina), contexto, usar_perfil, False)
                for i, p in enumerate(partes, 1)
            ]
            resultado = Resultado(
                " ".join(r.texto for r in rs), sorted({a for r in rs for a in r.alertas}), max(r.tentativas for r in rs)
            )
            self.cache.put(self.chave(bloco, usar_perfil), bloco.id, resultado)
            return resultado
        return self._traduzir_um(bloco, contexto, usar_perfil, True)

    def _traduzir_um(self, bloco: Bloco, contexto, usar_perfil: bool, cachear: bool) -> Resultado:
        cfg = self.cfg
        termos = gl.termos_no_texto(self.glossario, bloco.texto) if usar_perfil else []
        contexto = contexto if (usar_perfil and cfg.contexto_blocos) else []
        aviso = ""
        melhor: tuple[int, str, list[str]] | None = None
        for tentativa in range(1, cfg.max_tentativas + 1):
            mensagens = construir_mensagens(
                cfg, self.perfil, termos, bloco, contexto or [], usar_perfil, self.estilo, aviso
            )
            bruto = self.ollama.chat(cfg.modelo, mensagens, cfg.temperatura, cfg.num_ctx)
            texto = _limpar_saida(bruto, bloco)
            problemas = self._validar(bloco, texto, termos)
            if not problemas:
                resultado = Resultado(texto, [], tentativa)
                if cachear:
                    self.cache.put(self.chave(bloco, usar_perfil), bloco.id, resultado)
                return resultado
            if melhor is None or len(problemas) < melhor[0]:
                melhor = (len(problemas), texto, [c for c, _ in problemas])
            aviso = " ".join(d for _, d in problemas)

        assert melhor is not None
        _, texto, codigos = melhor
        if bloco.tipo != VERSO:
            texto = " ".join(texto.split())
        if not texto.strip():  # nada aproveitável: mantém o original, marcado para revisão
            texto, codigos = bloco.texto, codigos + ["falha"]
        resultado = Resultado(texto, sorted(set(codigos + ["revisar"])), cfg.max_tentativas)
        if cachear:
            self.cache.put(self.chave(bloco, usar_perfil), bloco.id, resultado)
        return resultado

    def traduzir_lista(self, blocos: list[Bloco], usar_perfil: bool = True, progresso=None) -> dict[str, Resultado]:
        """Traduz em ordem, passando os parágrafos anteriores como contexto."""
        resultados: dict[str, Resultado] = {}
        historico: list[tuple[str, str | None]] = []
        for i, b in enumerate(blocos, 1):
            n = self.cfg.contexto_blocos
            contexto = historico[-n:] if (n and usar_perfil and b.tipo in (PARAGRAFO, NOTA, VERSO)) else []
            r = self._com_reentrada(b, contexto, usar_perfil)
            resultados[b.id] = r
            if b.tipo in (PARAGRAFO, VERSO):
                historico.append((b.texto, r.texto))
            elif b.tipo == TITULO and b.nivel <= 1:
                historico.clear()
            if progresso:
                progresso(i, len(blocos), b, r)
        return resultados

    def _com_reentrada(self, bloco, contexto, usar_perfil) -> Resultado:
        """Se o Ollama cair no meio, espera e tenta de novo antes de desistir (o que já foi feito está salvo)."""
        for espera in (5, 15, 45, None):
            try:
                return self.traduzir(bloco, contexto, usar_perfil)
            except OllamaErro:
                if espera is None:
                    raise
                time.sleep(espera)
        raise AssertionError("inalcançável")


def buscar_traducoes(livro: Livro, tradutor: Tradutor) -> tuple[dict[str, str], dict[str, Resultado]]:
    """Lê do cache o que já foi traduzido (sem chamar o modelo). Falta -> mantém o original."""
    textos: dict[str, str] = {}
    resultados: dict[str, Resultado] = {}
    for b in livro.blocos():
        r = tradutor.resolver(b)
        if r is None:
            r = Resultado(b.texto, ["pendente"], 0)
        textos[b.id] = r.texto
        resultados[b.id] = r
    return textos, resultados


def relatorio_alertas(livro: Livro, resultados: dict[str, Resultado]) -> str:
    """Markdown com os trechos que merecem revisão humana."""
    linhas = ["# Relatório de revisão", ""]
    contagem: dict[str, int] = {}
    itens = []
    for cap in livro.capitulos:
        for b in cap.blocos:
            r = resultados.get(b.id)
            if r and set(r.alertas) - ALERTAS_INFORMATIVOS:
                itens.append((cap.titulo or "(início)", b, r))
                for a in r.alertas:
                    contagem[a] = contagem.get(a, 0) + 1
    if not itens:
        linhas.append("Nenhum alerta: todos os trechos passaram na validação automática.")
        return "\n".join(linhas) + "\n"
    linhas.append("Resumo dos alertas: " + ", ".join(f"{k}: {v}" for k, v in sorted(contagem.items())))
    linhas.append("")
    linhas.append("`revisar` = esgotou as tentativas e a melhor versão foi mantida; `pendente` = ainda não traduzido.")
    linhas.append("")
    for cap_titulo, b, r in itens:
        linhas.append(f"## {b.id} — {cap_titulo}" + (f" (p. {b.pagina})" if b.pagina else ""))
        linhas.append(f"Alertas: {', '.join(r.alertas)}")
        linhas.append("")
        linhas.append("> **Original:** " + b.texto.replace("\n", " ")[:600])
        linhas.append("")
        linhas.append("> **Tradução:** " + r.texto.replace("\n", " ")[:600])
        linhas.append("")
    return "\n".join(linhas) + "\n"
