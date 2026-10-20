"""Perfil de tradução: contexto que o programa dá à IA (gênero, registro, regras, exemplos) e os prompts.

Os textos que vão para o modelo ficam em inglês (os modelos seguem melhor); a conversa com o usuário
é em português.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .config import Config, codigo_prompt, nome_idioma
from .glossario import Termo
from .modelo import Bloco, NOTA, TITULO, VERSO

GENEROS = {
    "ocultismo": (
        "Ocultismo / esoterismo",
        "Esoteric/occult text. Keep the specialised vocabulary of the tradition (kabbalistic, alchemical, "
        "astrological, ritual and magical terms) exact and consistent; never replace technical terms with "
        "everyday synonyms. Keep a solemn, precise, initiatory register; do not explain or modernise.",
    ),
    "ficcao": (
        "Ficção",
        "Fiction. Preserve the author's voice, diction, rhythm, sentence length, punctuation and period flavour, "
        "including archaisms and idiosyncrasies. Do not normalise, modernise, embellish or explain. Keep the "
        "narrator's and the characters' registers and the dialogue conventions.",
    ),
    "filosofia": (
        "Filosofia / ensaio",
        "Philosophical or essayistic text. Keep conceptual terminology literal and consistent; preserve the "
        "argument structure, long sentences and rhetorical devices; do not simplify.",
    ),
    "religioso": (
        "Religioso / espiritual",
        "Religious or spiritual text. Preserve sacred and liturgical terminology, the solemn tone and traditional "
        "phrasing; render names and technical terms consistently.",
    ),
    "academico": (
        "Acadêmico / técnico",
        "Academic or technical text. Neutral, precise register; keep terminology consistent and leave citations, "
        "numbers and references unchanged.",
    ),
    "outro": ("Outro / geral", "Preserve the author's tone and style faithfully."),
}

REGRAS_PADRAO = [
    "Translate faithfully and completely: do not omit, add, summarise, explain or modernise anything.",
    "Preserve the author's voice, sentence structure, rhythm, punctuation and rhetorical devices; "
    "do not simplify or smooth the text.",
    "Keep paragraph breaks and line breaks exactly as in the source.",
    "Keep proper names, quotations in other languages and technical terms without a good equivalent as in the "
    "source, unless the glossary says otherwise.",
    "Follow the glossary strictly: every glossary term must appear in the translation exactly as given.",
    "Output only the translation, with no comments, notes, quotation marks or preamble.",
]

_VARIANTES_EN = {
    "português do brasil": "Brazilian Portuguese",
    "portugues do brasil": "Brazilian Portuguese",
    "português de portugal": "European Portuguese",
    "portugues de portugal": "European Portuguese",
}


@dataclass
class Perfil:
    genero: str = "outro"
    obra: str = ""
    registro: str = GENEROS["outro"][1]
    regras: list = field(default_factory=lambda: list(REGRAS_PADRAO))
    exemplos: list = field(default_factory=list)  # [{"origem": ..., "traducao": ...}]
    versao: int = 1

    @classmethod
    def padrao(cls, genero: str = "outro", obra: str = "") -> "Perfil":
        genero = genero if genero in GENEROS else "outro"
        return cls(genero=genero, obra=obra, registro=GENEROS[genero][1])

    def hash(self) -> str:
        base = json.dumps(
            [self.genero, self.obra, self.registro, self.regras, self.exemplos], ensure_ascii=False, sort_keys=True
        )
        return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]

    def salvar(self, caminho: Path) -> None:
        Path(caminho).write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def carregar(cls, caminho: Path) -> "Perfil":
        d = json.loads(Path(caminho).read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ----------------------------------------------------------------------------- prompts

def _variante_en(variante: str) -> str:
    return _VARIANTES_EN.get(variante.strip().lower(), variante.strip())


def _cortar(t: str, limite: int, do_fim: bool = False) -> str:
    if len(t) <= limite:
        return t
    return ("…" + t[-limite:]) if do_fim else (t[:limite] + "…")


def _dica_do_tipo(bloco: Bloco) -> str:
    if bloco.tipo == VERSO:
        n = len([l for l in bloco.texto.split("\n") if l.strip()])
        return f"This passage is verse: keep exactly {n} lines, one for each source line."
    if bloco.tipo == TITULO:
        return "This passage is a heading: translate it as a heading, on a single line."
    if bloco.tipo == NOTA:
        return "This passage is a footnote: translate it as a footnote and keep its numbering."
    return ""


def _instrucoes(cfg: Config, perfil: Perfil, termos: list[Termo], bloco: Bloco, aviso: str) -> list[str]:
    linhas = []
    if cfg.variante_destino:
        linhas.append(f"Target variant: {_variante_en(cfg.variante_destino)}.")
    if perfil.obra:
        linhas.append(f"Work: {perfil.obra}.")
    if perfil.registro:
        linhas.append(f"Genre and register: {perfil.registro}")
    if perfil.regras:
        linhas.append("Rules:")
        linhas.extend(f"- {r}" for r in perfil.regras)
    if termos:
        linhas.append("Mandatory glossary for this passage (use these renderings exactly):")
        for t in termos:
            linhas.append(f"- {t.origem} -> {t.destino}" + (f" ({t.nota})" if t.nota else ""))
    dica = _dica_do_tipo(bloco)
    if dica:
        linhas.append(dica)
    if aviso:
        linhas.append(f"IMPORTANT: your previous answer was rejected: {aviso} Fix this in the new translation.")
    return linhas


def _bloco_contexto(contexto: list[tuple[str, str | None]]) -> list[str]:
    if not contexto:
        return []
    linhas = [
        "Context (previous passages of the same book, provided ONLY for consistency of terms and tone; "
        "do not translate, repeat or mention them):"
    ]
    for fonte, trad in contexto:
        linhas.append(f"- Source: {_cortar(fonte, 600, do_fim=True)}")
        if trad:
            linhas.append(f"  Translation: {_cortar(trad, 600, do_fim=True)}")
    return linhas


def _bloco_exemplos(perfil: Perfil) -> list[str]:
    exemplos = perfil.exemplos[-4:]
    if not exemplos:
        return []
    linhas = ["Examples of the desired translation style (approved by the translator):"]
    for e in exemplos:
        linhas.append(f"Source: {_cortar(e['origem'], 700)}")
        linhas.append(f"Translation: {_cortar(e['traducao'], 700)}")
    return linhas


def construir_mensagens(
    cfg: Config,
    perfil: Perfil,
    termos: list[Termo],
    bloco: Bloco,
    contexto: list[tuple[str, str | None]],
    usar_perfil: bool,
    estilo: str,
    aviso: str = "",
) -> list[dict]:
    """Monta as mensagens para o Ollama.

    estilo "translategemma": uma única mensagem de usuário no formato oficial do modelo, com as
    instruções extras entre a frase de papel e o pedido. estilo "chat": system + exemplos + user.
    Sem perfil (usar_perfil=False) devolve o prompt puro, usado como base de comparação.
    """
    src, tgt = nome_idioma(cfg.origem, "en"), nome_idioma(cfg.destino, "en")
    src_c, tgt_c = codigo_prompt(cfg.origem), codigo_prompt(cfg.destino)
    texto = bloco.texto

    if estilo == "translategemma":
        base = (
            f"You are a professional {src} ({src_c}) to {tgt} ({tgt_c}) translator. Your goal is to accurately "
            f"convey the meaning and nuances of the original {src} text while adhering to {tgt} grammar, "
            f"vocabulary, and cultural sensitivities.\n"
            f"Produce only the {tgt} translation, without any additional explanations or commentary."
        )
        pedido = f"Please translate the following {src} text into {tgt}:\n\n\n{texto}"
        if not usar_perfil:
            return [{"role": "user", "content": f"{base} {pedido}"}]
        extras = (
            _instrucoes(cfg, perfil, termos, bloco, aviso)
            + _bloco_exemplos(perfil)
            + _bloco_contexto(contexto)
        )
        corpo = base + ("\n\n" + "\n".join(extras) if extras else "") + "\n\n" + pedido
        return [{"role": "user", "content": corpo}]

    # estilo chat (Gemma 3, Qwen, Llama...)
    if not usar_perfil:
        sistema = f"You are a professional translator from {src} to {tgt}. Output only the translation."
        return [{"role": "system", "content": sistema}, {"role": "user", "content": texto}]
    sistema = "\n".join(
        [f"You are an expert literary translator from {src} to {tgt}."]
        + _instrucoes(cfg, perfil, termos, bloco, aviso)
        + ["Output only the translation."]
    )
    mensagens = [{"role": "system", "content": sistema}]
    for e in perfil.exemplos[-4:]:
        mensagens.append({"role": "user", "content": _cortar(e["origem"], 700)})
        mensagens.append({"role": "assistant", "content": _cortar(e["traducao"], 700)})
    contexto_txt = "\n".join(_bloco_contexto(contexto))
    usuario = (contexto_txt + "\n\nText to translate:\n\n" if contexto_txt else "") + texto
    mensagens.append({"role": "user", "content": usuario})
    return mensagens


def estilo_do_modelo(cfg: Config) -> str:
    if cfg.estilo_prompt in ("translategemma", "chat"):
        return cfg.estilo_prompt
    return "translategemma" if "translategemma" in cfg.modelo.lower() else "chat"


# ----------------------------------------------------------------------------- sugestão automática

def sugerir_perfil(ollama, cfg: Config, livro, max_trechos: int = 5) -> Perfil | None:
    """Pede ao modelo uma proposta de perfil a partir de trechos do livro. None se não vier JSON válido."""
    from .modelo import PARAGRAFO

    paragrafos = [b for b in livro.blocos() if b.tipo == PARAGRAFO and len(b.texto) >= 200]
    if not paragrafos:
        return None
    passo = max(1, len(paragrafos) // max_trechos)
    amostras = [_cortar(b.texto, 700) for b in paragrafos[::passo][:max_trechos]]
    generos = ", ".join(GENEROS)
    pedido = (
        f"Book: {livro.titulo or 'unknown'} by {livro.autor or 'unknown'}. Language: "
        f"{nome_idioma(cfg.origem, 'en')}.\nExcerpts:\n"
        + "\n---\n".join(amostras)
        + "\n\nAnalyse these excerpts to help a translator keep the author's style. Answer with ONLY a JSON "
        f'object with the keys "genero" (one of: {generos}), "obra" (one line: what the work is, its period and '
        'tradition), and "registro" (2-3 sentences, in English: tone, vocabulary and stylistic traits that the '
        "translation must preserve)."
    )
    try:
        resposta = ollama.chat(
            cfg.modelo_para_analise,
            [{"role": "user", "content": pedido}],
            temperatura=0.2,
            num_ctx=cfg.num_ctx,
        )
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"\{.*\}", resposta, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    genero = str(d.get("genero", "outro")).strip().lower()
    perfil = Perfil.padrao(genero if genero in GENEROS else "outro", str(d.get("obra", "")).strip())
    registro = str(d.get("registro", "")).strip()
    if registro:
        perfil.registro = registro
    return perfil
