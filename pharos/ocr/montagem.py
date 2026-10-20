"""Monta o Livro (formato interno) a partir dos blocos de OCR, já revisados, e de páginas lidas direto do texto."""
from __future__ import annotations

from ..extratores.comum import dividir_em_capitulos, montar_livro
from ..extratores.pdf_texto import _unir_continuacoes
from ..modelo import Bloco, Livro


def montar(blocos_ocr: dict[int, list[dict]], extras: list[Bloco] | None = None, recuados_extras: set[int] | None = None,
           titulo: str = "", autor: str = "", idioma: str = "") -> Livro:
    """blocos_ocr: {página: [dict]} (já com as edições). extras: blocos de páginas com texto selecionável
    (PDF misto), com os `id()` dos que começam recuados em recuados_extras."""
    todos: list[Bloco] = []
    recuados: set[int] = set(recuados_extras or ())
    for n in sorted(blocos_ocr):
        for d in blocos_ocr[n]:
            b = Bloco("", d["tipo"], d["texto"], n, int(d.get("nivel", 1)))
            if d.get("recuo"):
                recuados.add(id(b))
            todos.append(b)
    if extras:
        todos.extend(extras)
        todos.sort(key=lambda b: b.pagina or 0)  # estável: a ordem dentro de cada página é mantida
    todos = _unir_continuacoes(todos, recuados)
    return montar_livro(titulo, autor, idioma, dividir_em_capitulos(todos))
