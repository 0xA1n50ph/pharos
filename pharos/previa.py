"""Prévia das primeiras páginas: traduz um trecho, mostra lado a lado e deixa refinar tom, regras,
glossário e exemplos antes de gastar horas no livro inteiro."""
from __future__ import annotations

import shutil
import webbrowser
from datetime import datetime

from . import glossario as gl
from .config import Config, nome_idioma
from .exportar import _esc, pagina_html
from .modelo import Bloco, Livro, PARAGRAFO, TITULO
from .ollama import Ollama
from .perfil import Perfil, estilo_do_modelo
from .projeto import Projeto
from .traducao import CacheTraducao, Correcoes, Tradutor
from .ui import Progresso, SaidaDoUsuario, UI

MENU = """
O que deseja fazer?
  [a] Aprovar e traduzir o livro inteiro
  [t] Ajustar o tom / registro (descrição do estilo)
  [r] Adicionar uma regra de fidelidade
  [g] Adicionar um termo ao glossário
  [e] Corrigir um parágrafo (vira exemplo para o modelo e vale na versão final)
  [c] Comparar com a tradução sem perfil (mostra o que o perfil muda)
  [o] Testar outro trecho do livro
  [m] Trocar o modelo
  [s] Sair (o projeto fica salvo)"""


def selecionar_trecho(livro: Livro, cfg: Config, deslocamento: int = 0) -> tuple[list[Bloco], int]:
    """Trecho de ~N páginas do corpo do livro. Devolve (blocos, parágrafos longos usados)."""
    todos = list(livro.blocos())
    longos = [i for i, b in enumerate(todos) if b.tipo == PARAGRAFO and len(b.texto) >= 200]
    if not longos:
        return todos[:12], 0
    inicio = longos[min(deslocamento, len(longos) - 1)]
    if inicio > 0 and todos[inicio - 1].tipo == TITULO:
        inicio -= 1
    pagina0 = todos[inicio].pagina
    meta_palavras = cfg.paginas_previa * cfg.palavras_por_pagina
    blocos, palavras = [], 0
    for b in todos[inicio:inicio + 60]:
        if blocos and pagina0 is not None and b.pagina is not None and b.pagina >= pagina0 + cfg.paginas_previa:
            break
        if blocos and pagina0 is None and palavras >= meta_palavras:
            break
        blocos.append(b)
        palavras += len(b.texto.split())
    usados = sum(1 for b in blocos if b.tipo == PARAGRAFO and len(b.texto) >= 200)
    return blocos, usados


def _linhas_html(blocos, resultados, base):
    linhas = []
    for n, b in enumerate(blocos, 1):
        r = resultados[b.id]
        linhas.append({
            "n": n, "tipo": b.tipo, "orig": b.texto, "trad": r.texto, "alertas": r.alertas,
            "base": base[b.id].texto if base else None,
        })
    return linhas


def _mostrar_no_terminal(ui: UI, blocos, resultados, max_pares: int = 3) -> None:
    ui.info()
    mostrados = 0
    for n, b in enumerate(blocos, 1):
        if b.tipo != PARAGRAFO or mostrados >= max_pares:
            continue
        mostrados += 1
        ui.info(f"[{n}] ORIGINAL:  {b.texto[:320]}{'…' if len(b.texto) > 320 else ''}")
        t = resultados[b.id].texto
        ui.info(f"    TRADUÇÃO:  {t[:320]}{'…' if len(t) > 320 else ''}\n")
    alertas = sum(1 for b in blocos if set(resultados[b.id].alertas) - {"corrigido_manualmente", "ja_no_idioma_de_destino"})
    if alertas:
        ui.aviso(f"{alertas} trecho(s) da prévia com alerta (veja os selos no HTML).")


def ciclo_previa(proj: Projeto, cfg: Config, perfil: Perfil, ollama: Ollama, livro: Livro, ui: UI,
                 abrir_navegador: bool = True) -> bool:
    """Devolve True se o perfil foi aprovado. SaidaDoUsuario se o usuário sair."""
    versao = 0
    deslocamento = 0
    comparar = False
    correcoes = Correcoes(proj.correcoes_path)
    primeira = True

    while True:
        versao += 1
        perfil.versao = versao
        pasta = proj.pasta("previa") / f"v{versao}"
        pasta.mkdir(parents=True, exist_ok=True)
        perfil.salvar(pasta / "perfil.json")
        perfil.salvar(proj.perfil_path)
        cfg.salvar(proj.config_path)
        if proj.glossario_path.exists():
            shutil.copy2(proj.glossario_path, pasta / "glossario.tsv")

        cache = CacheTraducao(proj.cache_path)
        tradutor = Tradutor(cfg, perfil, proj.glossario_path, ollama, cache, correcoes)
        blocos, usados = selecionar_trecho(livro, cfg, deslocamento)
        palavras = sum(len(b.texto.split()) for b in blocos)
        ui.titulo(f"Prévia v{versao}")
        ui.info(f"Traduzindo {len(blocos)} trechos (~{palavras} palavras) com {cfg.modelo}...")
        resultados = tradutor.traduzir_lista(blocos, progresso=Progresso(ui, "Prévia"))
        base = None
        if comparar:
            ui.info("Gerando também a tradução sem perfil, para comparação...")
            base = tradutor.traduzir_lista(blocos, usar_perfil=False, progresso=Progresso(ui, "Base"))

        subtitulo = (
            f"{_esc(nome_idioma(cfg.origem))} → {_esc(nome_idioma(cfg.destino))} · modelo {_esc(cfg.modelo)} · "
            f"perfil v{versao} ({_esc(perfil.genero)}) · {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        )
        rotulos = (nome_idioma(cfg.origem).capitalize(), "Tradução com perfil", "Sem perfil (base)")
        html_path = pasta / "previa.html"
        html_path.write_text(
            pagina_html(f"Prévia v{versao} — {livro.titulo}", subtitulo, _linhas_html(blocos, resultados, base), rotulos),
            encoding="utf-8",
        )
        _mostrar_no_terminal(ui, blocos, resultados)
        ui.info(f"Prévia completa, lado a lado: {html_path}")
        if abrir_navegador and primeira and ui.tty and not ui.auto:
            try:
                webbrowser.open(html_path.as_uri())
            except Exception:  # noqa: BLE001
                pass
        primeira = False

        if ui.auto:
            ui.info("Modo automático: prévia aprovada.")
            return True

        while True:  # menu; só sai deste laço quando algo exige refazer a prévia
            ui.info(MENU)
            escolha = ui.perguntar("Escolha", "a").lower()[:1]
            if escolha == "a":
                return True
            if escolha == "s":
                raise SaidaDoUsuario("saída pedida na prévia")
            if escolha == "t":
                ui.info(f"\nTom atual: {perfil.registro}")
                novo = ui.perguntar("Novo texto de tom/registro (Enter mantém; pode ser em português)")
                if not novo:
                    continue
                perfil.registro = novo
                break
            if escolha == "r":
                regra = ui.perguntar("Nova regra (ex.: não traduzir nomes de lojas e ordens)")
                if not regra:
                    continue
                perfil.regras.append(regra)
                break
            if escolha == "g":
                origem = ui.perguntar("Termo no original")
                destino = ui.perguntar("Tradução obrigatória") if origem else ""
                if not (origem and destino):
                    continue
                gl.adicionar(proj.glossario_path, origem, destino, ui.perguntar("Nota (opcional)"))
                break
            if escolha == "e":
                try:
                    n = int(ui.perguntar("Número do trecho (coluna #)"))
                    bloco = blocos[n - 1]
                except (ValueError, IndexError):
                    ui.aviso("Número inválido.")
                    continue
                ui.info(f"\nORIGINAL:  {bloco.texto}\nATUAL:     {resultados[bloco.id].texto}")
                novo = ui.multilinha("Escreva a tradução corrigida (termine com uma linha vazia; só Enter cancela):")
                if not novo:
                    continue
                correcoes.put(bloco, novo)
                perfil.exemplos.append({"origem": bloco.texto, "traducao": novo})
                break
            if escolha == "c":
                comparar = True
                break
            if escolha == "o":
                deslocamento += max(1, usados)
                break
            if escolha == "m":
                modelo = ui.perguntar("Nome do modelo no Ollama", cfg.modelo)
                if modelo == cfg.modelo:
                    continue
                if not ollama.tem_modelo(modelo):
                    ui.aviso(f"O modelo {modelo} não está instalado (ollama pull {modelo}).")
                    continue
                cfg.modelo = modelo
                ui.info(f"Estilo de prompt para este modelo: {estilo_do_modelo(cfg)}")
                break
            ui.aviso("Opção não reconhecida.")
