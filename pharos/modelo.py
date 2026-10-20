"""Formato interno do livro: todo extrator gera isto; tradução e exportação só conhecem isto."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterator

# tipos de bloco
TITULO = "titulo"
PARAGRAFO = "paragrafo"
VERSO = "verso"          # linhas curtas cuja quebra importa (poemas, invocações)
NOTA = "nota"            # nota de rodapé ou de fim
LEGENDA = "legenda"
SEPARADOR = "separador"  # "* * *", linha horizontal

TIPOS_TRADUZIVEIS = {TITULO, PARAGRAFO, VERSO, NOTA, LEGENDA}


@dataclass
class Bloco:
    id: str
    tipo: str
    texto: str
    pagina: int | None = None
    nivel: int = 1  # só para título (1 = mais alto)


@dataclass
class Capitulo:
    blocos: list[Bloco] = field(default_factory=list)

    @property
    def titulo(self) -> str:
        for b in self.blocos:
            if b.tipo == TITULO:
                return b.texto
        return ""


@dataclass
class Livro:
    titulo: str = ""
    autor: str = ""
    idioma: str = ""
    capitulos: list[Capitulo] = field(default_factory=list)

    def blocos(self) -> Iterator[Bloco]:
        for cap in self.capitulos:
            yield from cap.blocos

    def total_palavras(self) -> int:
        return sum(len(b.texto.split()) for b in self.blocos() if b.tipo != SEPARADOR)

    def renumerar(self) -> None:
        """Dá ids estáveis (c003p00045) depois que a estrutura está definitiva."""
        for ci, cap in enumerate(self.capitulos, 1):
            for bi, b in enumerate(cap.blocos, 1):
                b.id = f"c{ci:03d}p{bi:05d}"

    def salvar(self, caminho: Path) -> None:
        Path(caminho).write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8"
        )

    @classmethod
    def carregar(cls, caminho: Path) -> "Livro":
        d = json.loads(Path(caminho).read_text(encoding="utf-8"))
        return cls(
            titulo=d.get("titulo", ""),
            autor=d.get("autor", ""),
            idioma=d.get("idioma", ""),
            capitulos=[
                Capitulo(blocos=[Bloco(**b) for b in c["blocos"]]) for c in d["capitulos"]
            ],
        )


def bloco_para_markdown(b: Bloco, texto: str | None = None) -> str:
    t = b.texto if texto is None else texto
    if b.tipo == TITULO:
        return "#" * max(1, min(b.nivel, 6)) + " " + t
    if b.tipo == VERSO:
        return "  \n".join(t.split("\n"))
    if b.tipo == NOTA:
        return "> " + t.replace("\n", "\n> ")
    if b.tipo == LEGENDA:
        return "*" + t + "*"
    if b.tipo == SEPARADOR:
        return "* * *"
    return t


def livro_para_markdown(livro: Livro, traducoes: dict[str, str] | None = None) -> str:
    partes = []
    if livro.titulo:
        partes.append(f"% {livro.titulo}\n% {livro.autor}".rstrip())
    for b in livro.blocos():
        texto = None
        if traducoes is not None:
            texto = traducoes.get(b.id, b.texto)
        partes.append(bloco_para_markdown(b, texto))
    return "\n\n".join(partes) + "\n"
