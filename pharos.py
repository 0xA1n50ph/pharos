#!/usr/bin/env python3
"""Pharos — tradutor de livros com IA local (Ollama).

Entrada: PDF (com texto ou escaneado), pasta de imagens de páginas, EPUB, TXT/MD e, com o Calibre instalado,
         MOBI/AZW3/FB2/DOCX... Livros escaneados passam por OCR (Tesseract) e por uma tela de revisão.
Saída:   pasta do projeto com texto extraído, traduções, relatório de revisão e EPUB/DOCX/MD/HTML.

Exemplos:
    python pharos.py meu_livro.pdf
    python pharos.py scan_do_livro.pdf --origem de          # OCR em alemão (testa letra gótica sozinho)
    python pharos.py fotos_das_paginas/ --ocr-lang frk       # pasta de imagens, OCR em Fraktur
    python pharos.py meu_livro.epub --destino en --modelo gemma3:12b
    python pharos.py meu-livro/            # retoma um projeto interrompido
    python pharos.py meu-livro/ --revisar-ocr   # reabre a revisão do OCR de um projeto já extraído
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from pharos import __version__
from pharos.config import Config, IDIOMAS, nome_idioma, normalizar_codigo
from pharos.deteccao import tipo_arquivo
from pharos.exportar import FORMATOS, exportar_tudo
from pharos.extratores import extrair
from pharos.extratores.comum import ErroExtracao
from pharos.glossario import carregar as carregar_glossario, garantir_arquivo
from pharos.idioma import detectar_livro
from pharos.modelo import Livro, livro_para_markdown
from pharos.ollama import Ollama, OllamaErro
from pharos.perfil import GENEROS, Perfil, sugerir_perfil
from pharos.previa import ciclo_previa
from pharos.projeto import Projeto
from pharos.traducao import CacheTraducao, Correcoes, Tradutor, buscar_traducoes, relatorio_alertas
from pharos.ui import Progresso, SaidaDoUsuario, UI

NOMES_TIPO = {
    "pdf": "PDF", "epub": "EPUB", "txt": "texto puro/Markdown", "imagens": "imagens de páginas (scan)",
    "convertivel": "formato convertível (via Calibre)", "desconhecido": "desconhecido",
}
NOMES_TIPO_PDF = {
    "texto": "livro com texto selecionável", "scan": "scan (páginas em imagem)",
    "misto": "misto (parte texto, parte scan)", "vazio": "sem conteúdo aproveitável",
}


def ler_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="pharos",
        description="Traduz livros (PDF, EPUB, TXT...) com um modelo local do Ollama, com prévia e ajuste de estilo.",
    )
    p.add_argument("alvo", help="arquivo do livro, ou pasta de um projeto para retomar")
    p.add_argument("--origem", help="idioma do livro (ex.: de, es, en); sem isso o programa detecta e confirma")
    p.add_argument("--destino", help="idioma da tradução (ex.: pt, en); sem isso o programa pergunta")
    p.add_argument("--modelo", help="modelo do Ollama (padrão: translategemma:12b)")
    p.add_argument("--modelo-analise", help="modelo usado só para sugerir o perfil (padrão: o mesmo da tradução)")
    p.add_argument("--ollama", help="URL do Ollama (padrão: http://localhost:11434)")
    p.add_argument("--saida", help="pasta onde criar o projeto (padrão: pasta atual)")
    p.add_argument("--formatos", help="formatos de saída separados por vírgula: epub,docx,md,html")
    p.add_argument("--glossario", help="arquivo TSV: termo<TAB>tradução<TAB>nota")
    p.add_argument("--genero", help=f"gênero da obra: {', '.join(GENEROS)}")
    p.add_argument("--variante", help='variante do idioma de destino (padrão para pt: "português do Brasil")')
    p.add_argument("--previa-paginas", type=int, help="páginas da prévia (padrão: 3)")
    p.add_argument("--sem-contexto", action="store_true", help="não envia parágrafos anteriores como contexto")
    p.add_argument("--sem-previa", action="store_true", help="pula a prévia e traduz direto")
    p.add_argument("--sim", action="store_true", help="aceita as sugestões sem perguntar (útil em lote)")
    p.add_argument("--exportar-apenas", action="store_true", help="não traduz mais nada; só reexporta o que há no cache")
    p.add_argument("--extrair-apenas", action="store_true",
                   help="só extrai o texto (inclui OCR e revisão) e para; não traduz nem precisa do Ollama")
    ocr = p.add_argument_group("OCR (livros escaneados)")
    ocr.add_argument("--ocr-lang", help="idioma(s)/modelo do Tesseract, ex.: deu, frk, deu_latf, deu+eng "
                                        "(padrão: deduzido de --origem ou detectado; em alemão testa letra gótica)")
    ocr.add_argument("--revisar", choices=["sinalizadas", "todas", "nenhuma"],
                     help="revisão do OCR na tela: só páginas sinalizadas, todas ou nenhuma (padrão: pergunta)")
    ocr.add_argument("--revisar-ocr", action="store_true",
                     help="reabre a revisão do OCR de um projeto já extraído (retraduz só o que mudou)")
    ocr.add_argument("--ocr-forcar", action="store_true", help="refaz o OCR mesmo havendo resultado salvo")
    ocr.add_argument("--paginas-duplas", choices=["auto", "sim", "nao"],
                     help="imagens com duas páginas lado a lado: separar? (padrão: auto)")
    ocr.add_argument("--usar-camada-embutida", action="store_true",
                     help="PDF escaneado que já tem texto embutido: usar esse texto em vez de refazer o OCR")
    ocr.add_argument("--ocr-imagens", action="store_true",
                     help="PDF de texto com algumas páginas só de imagem: ler essas páginas com OCR (padrão: ficam de fora)")
    ocr.add_argument("--ocr-dpi", type=int, help="resolução de renderização do PDF para o OCR (padrão: 300)")
    ocr.add_argument("--ocr-processos", type=int, help="páginas lidas em paralelo (padrão: automático)")
    ocr.add_argument("--modelo-ocr", help="modelo do Ollama que SUGERE correções de OCR na tela de revisão "
                                          "(padrão: um modelo geral instalado, como gemma3; o de tradução não é ideal)")
    ocr.add_argument("--sugestoes-ia", choices=["sinalizadas", "todas"],
                     help="já pede ao modelo sugestões de correção do OCR em segundo plano quando a revisão abre "
                          "(sem isso, use o botão da tela; nada é aplicado sem você aceitar)")
    ocr.add_argument("--sem-navegador", action="store_true",
                     help="não abre o navegador na revisão; só mostra o endereço (útil por SSH)")
    p.add_argument("--versao", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


# ----------------------------------------------------------------------------- etapas

def etapa_extracao(proj: Projeto, cfg: Config, ui: UI, args) -> Livro:
    if proj.estado()["etapas"]["extracao"] and proj.livro_path.exists():
        return Livro.carregar(proj.livro_path)

    original = proj.original
    if original is None:
        raise ErroExtracao("Não encontrei o arquivo original na pasta do projeto.")
    ui.titulo("1. Arquivo")
    tipo = tipo_arquivo(original)
    nome = original.name + ("/" if original.is_dir() else "")
    ui.info(f"{nome} — formato detectado: {NOMES_TIPO[tipo]}")
    if tipo == "desconhecido":
        raise ErroExtracao("Formato não reconhecido. Use PDF, EPUB, TXT ou imagens de páginas "
                           "(ou instale o Calibre para MOBI/AZW3/DOCX).")

    rota = None  # None = extração direta do texto; senão {"ocr": [índices] | None (todas), "texto": {páginas}}
    if tipo == "imagens":
        ui.info("Imagens de páginas: o texto sai por OCR, com uma etapa de revisão antes da tradução.")
        rota = {"ocr": None, "texto": set()}
    elif tipo == "pdf":
        from pharos.extratores import pdf_texto

        ui.info("Analisando as páginas (texto selecionável ou scan)...")
        a = pdf_texto.analisar(original)
        ui.info(f"{a['resumo']}\nClassificação: {NOMES_TIPO_PDF[a['tipo']]}")
        etapa_ocr = _modulo_ocr()
        rota = etapa_ocr.rotear_pdf(original, cfg, ui, args)
        if rota is None:
            if a["tipo"] == "misto":
                ui.aviso("Parte das páginas é só imagem; elas ficarão de fora (a lista vai para revisao/paginas_sem_texto.txt).")
            if not ui.confirmar("Extrair o texto diretamente do PDF?", True):
                if ui.confirmar("Usar o OCR (Tesseract) em vez disso?", False):
                    rota = {"ocr": None, "texto": set()}
                else:
                    raise SaidaDoUsuario("extração recusada")

    if rota is not None:
        etapa_ocr = _modulo_ocr()
        n_ocr = len(rota["ocr"]) if rota["ocr"] is not None else "todas as"
        n_txt = len(rota["texto"])
        ui.info(f"Plano: OCR em {n_ocr} página(s)" + (f" e leitura direta do texto em {n_txt}" if n_txt else "") + ".")
        if not ui.confirmar("Prosseguir com o OCR?", True):
            raise SaidaDoUsuario("extração recusada")
        livro, rel = etapa_ocr.extrair_scan(proj, cfg, ui, args, original, rota["ocr"], rota["texto"])
    else:
        def progresso(i: int, total: int) -> None:
            if total >= 20 and (i % max(1, total // 10) == 0 or i == total):
                ui.info(f"  lendo... {i}/{total}")

        ui.info("Extraindo o conteúdo...")
        livro, rel = extrair(original, tipo, progresso)
    if not livro.titulo:
        livro.titulo = original.stem
    palavras = livro.total_palavras()
    if palavras < 200:
        raise ErroExtracao(f"Só {palavras} palavras extraídas; o arquivo parece vazio ou protegido.")
    livro.salvar(proj.livro_path)
    proj.livro_md_path.write_text(livro_para_markdown(livro), encoding="utf-8")
    if rel.get("paginas_sem_texto"):
        (proj.pasta("revisao") / "paginas_sem_texto.txt").write_text(
            "Páginas sem texto selecionável (scan), ignoradas na extração:\n"
            + ", ".join(map(str, rel["paginas_sem_texto"])) + "\n", encoding="utf-8")
    n_blocos = sum(len(c.blocos) for c in livro.capitulos)
    ui.info(f"Extraído: {len(livro.capitulos)} capítulo(s), {n_blocos} trechos, {palavras} palavras.")
    if rel.get("cabecalhos_removidos"):
        ui.info("Cabeçalhos/rodapés removidos: " + "; ".join(rel["cabecalhos_removidos"][:4]))
    if rel.get("paginas_sem_texto"):
        ui.aviso(f"{len(rel['paginas_sem_texto'])} página(s) sem texto foram ignoradas.")
    ui.info(f"Texto no idioma original: {proj.livro_md_path}")
    if original.is_file():  # para um conjunto de imagens, vale o nome da pasta indicada na criação do projeto
        cfg.arquivo_original = original.name
    cfg.salvar(proj.config_path)
    proj.marcar(extracao=True)
    return livro


def _modulo_ocr():
    try:
        from pharos import etapa_ocr
    except ImportError as e:  # Pillow, numpy ou pypdfium2 ausentes
        raise ErroExtracao(f"O OCR precisa de bibliotecas que não estão instaladas ({e}).\n"
                           "Rode: pip install pillow numpy pypdfium2") from e
    return etapa_ocr


def etapa_idioma(proj: Projeto, cfg: Config, livro: Livro, ui: UI, args) -> None:
    if proj.estado()["etapas"]["idioma"] and cfg.origem:
        return
    ui.titulo("2. Idioma do livro")
    if args.origem:
        cod = normalizar_codigo(args.origem)
        if not cod:
            raise ErroExtracao(f"Idioma de origem não reconhecido: {args.origem}")
    else:
        dist = detectar_livro(livro)
        padrao = livro.idioma if livro.idioma in IDIOMAS else ""
        if dist:
            principal, parcela = dist[0]
            outros = [f"{nome_idioma(l)} {round(p * 100)}%" for l, p in dist[1:] if p >= 0.08]
            ui.info(f"Idioma detectado: {nome_idioma(principal)} ({round(parcela * 100)}% dos trechos amostrados)"
                    + (f"; também aparecem: {', '.join(outros)}" if outros else ""))
            if livro.idioma and livro.idioma != principal:
                ui.aviso(f"Os metadados do arquivo indicam '{livro.idioma}'.")
            padrao = principal
        else:
            ui.aviso("Não consegui detectar o idioma automaticamente.")
        while True:
            resp = ui.perguntar("Idioma de origem (Enter confirma; ou digite outro código/nome)", padrao)
            cod = normalizar_codigo(resp) if resp else None
            if cod:
                break
            if ui.auto:
                raise ErroExtracao("Idioma de origem indefinido; use --origem.")
            ui.aviso(f"Não reconheci '{resp}'. Códigos conhecidos: {', '.join(sorted(IDIOMAS))}")
    cfg.origem = cod
    livro.idioma = cod
    livro.salvar(proj.livro_path)
    cfg.salvar(proj.config_path)
    proj.marcar(idioma=True)


def etapa_configuracao(proj: Projeto, cfg: Config, livro: Livro, ui: UI, args) -> None:
    if proj.estado()["etapas"]["configurado"]:
        return
    ui.titulo("3. Configuração")
    cfg.titulo = ui.perguntar("Título do livro", livro.titulo)
    cfg.autor = ui.perguntar("Autor (opcional)", livro.autor)
    livro.titulo, livro.autor = cfg.titulo, cfg.autor
    livro.salvar(proj.livro_path)

    if args.destino:
        cod = normalizar_codigo(args.destino)
        if not cod:
            raise ErroExtracao(f"Idioma de destino não reconhecido: {args.destino}")
    else:
        while True:
            resp = ui.perguntar("Traduzir para qual idioma? (pt = português, en = inglês, ou outro código)", "pt")
            cod = normalizar_codigo(resp)
            if cod and cod != cfg.origem:
                break
            if ui.auto:
                raise ErroExtracao("Idioma de destino inválido ou igual ao de origem.")
            ui.aviso("Idioma não reconhecido ou igual ao do livro.")
    cfg.destino = cod
    cfg.variante_destino = args.variante if args.variante is not None else ("português do Brasil" if cod == "pt" else "")

    if args.glossario:
        origem = Path(args.glossario).expanduser()
        if not origem.exists():
            raise ErroExtracao(f"Glossário não encontrado: {origem}")
        shutil.copy2(origem, proj.glossario_path)
    else:
        caminho = "" if ui.auto else ui.perguntar("Glossário (.tsv: termo<TAB>tradução<TAB>nota) — caminho, ou Enter para começar vazio")
        if caminho and Path(caminho).expanduser().exists():
            shutil.copy2(Path(caminho).expanduser(), proj.glossario_path)
        else:
            garantir_arquivo(proj.glossario_path)
    ui.info(f"Glossário: {len(carregar_glossario(proj.glossario_path))} termo(s) em {proj.glossario_path.name}")

    if args.formatos:
        brutos = args.formatos
    else:
        brutos = ui.perguntar(f"Formatos de saída ({', '.join(FORMATOS)})", ",".join(cfg.formatos))
    formatos = [f.strip().lower() for f in brutos.split(",") if f.strip()]
    invalidos = [f for f in formatos if f not in FORMATOS]
    if invalidos or not formatos:
        raise ErroExtracao(f"Formato(s) de saída inválido(s): {', '.join(invalidos) or '(vazio)'}")
    cfg.formatos = formatos
    cfg.salvar(proj.config_path)
    proj.marcar(configurado=True)


def verificar_ollama(cfg: Config, ollama: Ollama) -> None:
    try:
        ollama.modelos()
    except OllamaErro as e:
        raise OllamaErro(f"{e}\nAbra o app do Ollama (ou rode `ollama serve`) e tente de novo.") from e
    for nome in {cfg.modelo, cfg.modelo_para_analise}:
        if not ollama.tem_modelo(nome):
            raise OllamaErro(f"O modelo '{nome}' não está instalado no Ollama. Rode: ollama pull {nome}  (ou ./setup.sh)")


def etapa_perfil(proj: Projeto, cfg: Config, livro: Livro, ui: UI, ollama: Ollama, args) -> Perfil:
    if proj.estado()["etapas"]["perfil_aprovado"] and proj.perfil_path.exists():
        return Perfil.carregar(proj.perfil_path)

    ui.titulo("4. Perfil de tradução e prévia")
    verificar_ollama(cfg, ollama)

    if proj.perfil_path.exists():
        perfil = Perfil.carregar(proj.perfil_path)
        ui.info("Usando o perfil já salvo neste projeto.")
    else:
        perfil = None
        if args.genero:
            g = args.genero.strip().lower()
            if g not in GENEROS:
                raise ErroExtracao(f"Gênero desconhecido: {g}. Opções: {', '.join(GENEROS)}")
            perfil = Perfil.padrao(g)
        elif ui.auto or ui.confirmar("Quer que a IA sugira o perfil (gênero e tom) a partir de trechos do livro?", True):
            ui.info("Analisando trechos do livro...")
            perfil = sugerir_perfil(ollama, cfg, livro)
            if perfil is None:
                ui.aviso("A IA não devolveu uma sugestão utilizável; o perfil parte do gênero escolhido.")
        if perfil is None:
            perfil = Perfil.padrao("outro")

        ui.info(f"\nGênero: {perfil.genero} ({GENEROS[perfil.genero][0]})")
        genero = ui.perguntar(f"Gênero ({', '.join(GENEROS)})", perfil.genero).lower()
        if genero in GENEROS and genero != perfil.genero:
            perfil = Perfil.padrao(genero, perfil.obra)
        elif genero not in GENEROS:
            ui.aviso(f"Gênero '{genero}' desconhecido; mantendo '{perfil.genero}'.")
        padrao_obra = perfil.obra or f"{cfg.titulo}" + (f", de {cfg.autor}" if cfg.autor else "")
        perfil.obra = ui.perguntar("Descreva a obra em uma linha (título, autor, época, tradição)", padrao_obra)
        ui.info(f"Tom/registro pedido ao modelo: {perfil.registro}")

    perfil.salvar(proj.perfil_path)
    if args.sem_previa:
        ui.info("Prévia pulada (--sem-previa).")
    else:
        ciclo_previa(proj, cfg, perfil, ollama, livro, ui)
    perfil.salvar(proj.perfil_path)
    cfg.salvar(proj.config_path)
    proj.marcar(perfil_aprovado=True)
    return perfil


def criar_tradutor(proj: Projeto, cfg: Config, perfil: Perfil, ollama: Ollama) -> Tradutor:
    return Tradutor(cfg, perfil, proj.glossario_path, ollama, CacheTraducao(proj.cache_path), Correcoes(proj.correcoes_path))


def etapa_traducao(tradutor: Tradutor, proj: Projeto, cfg: Config, livro: Livro, ui: UI, ollama: Ollama) -> None:
    ui.titulo("5. Tradução do livro")
    todos = list(livro.blocos())
    prontos = sum(1 for b in todos if tradutor.resolver(b) is not None)
    ui.info(f"{len(todos)} trechos: {prontos} já prontos, {len(todos) - prontos} para traduzir com {cfg.modelo}.")
    if prontos < len(todos):
        ui.info("A partir daqui roda sozinho. Ctrl+C interrompe; o mesmo comando retoma de onde parou.")
    tradutor.traduzir_lista(todos, progresso=Progresso(ui))
    ollama.descarregar(cfg.modelo)  # libera a memória (útil no Mac, onde GPU e RAM são compartilhadas)
    proj.marcar(traducao_completa=True)


def etapa_exportar(tradutor: Tradutor, proj: Projeto, cfg: Config, livro: Livro, ui: UI) -> None:
    ui.titulo("6. Exportação")
    textos, resultados = buscar_traducoes(livro, tradutor)
    pendentes = sum(1 for r in resultados.values() if "pendente" in r.alertas)
    gerados = exportar_tudo(proj, cfg, livro, textos, resultados, cfg.formatos)
    proj.relatorio_path.write_text(relatorio_alertas(livro, resultados), encoding="utf-8")
    revisar = sum(1 for r in resultados.values() if "revisar" in r.alertas)
    ui.info("Arquivos gerados:")
    for g in gerados:
        ui.info(f"  {g}")
    ui.info(f"Relatório de revisão: {proj.relatorio_path}")
    if pendentes:
        ui.aviso(f"{pendentes} trecho(s) ainda sem tradução (mantidos no original). Rode de novo para continuar.")
    if revisar:
        ui.aviso(f"{revisar} trecho(s) esgotaram as tentativas e merecem revisão humana (veja o relatório).")
    proj.marcar(exportado=not pendentes)


# ----------------------------------------------------------------------------- principal

def executar(args, ui: UI) -> int:
    alvo = Path(args.alvo).expanduser()
    if not alvo.exists():
        ui.erro(f"Não encontrei: {alvo}")
        return 1
    base = Path(args.saida).expanduser() if args.saida else Path.cwd()

    if alvo.is_dir() and Projeto.e_projeto(alvo):
        proj = Projeto.abrir(alvo.resolve())
        ui.info(f"Retomando o projeto em {proj.raiz}")
    elif alvo.is_dir() and tipo_arquivo(alvo) != "imagens":
        ui.erro(f"{alvo} não é uma pasta de projeto (falta config.json) nem uma pasta de imagens de páginas.")
        return 1
    else:
        proj = Projeto.localizar(alvo, base)
        if proj and (ui.auto or ui.confirmar("Já existe um projeto para este livro. Retomar de onde parou?", True)):
            ui.info(f"Retomando o projeto em {proj.raiz}")
        else:
            proj = Projeto.criar(alvo, base)
            Config(arquivo_original=alvo.name).salvar(proj.config_path)
            ui.info(f"Projeto criado em {proj.raiz}")

    cfg = Config.carregar(proj.config_path)
    if args.modelo:
        cfg.modelo = args.modelo
    if args.modelo_analise:
        cfg.modelo_analise = args.modelo_analise
    if args.ollama:
        cfg.ollama_url = args.ollama
    if args.modelo_ocr:
        cfg.modelo_ocr = args.modelo_ocr
    if args.previa_paginas:
        cfg.paginas_previa = args.previa_paginas
    if args.sem_contexto:
        cfg.contexto_blocos = 0
    if args.paginas_duplas:
        cfg.ocr_duplas = args.paginas_duplas
    tem_ocr = any(proj.ocr_dir.glob("p*.json"))
    if args.revisar_ocr and not tem_ocr:
        ui.erro("Este projeto não tem páginas lidas por OCR para revisar.")
        return 1
    if (args.revisar_ocr or args.ocr_forcar) and tem_ocr:
        # refaz a montagem do livro a partir do OCR salvo (ou do OCR refeito); o cache de tradução é por conteúdo,
        # então só o que mudou é retraduzido
        proj.marcar(extracao=False, ocr_revisada=False, traducao_completa=False, exportado=False)
        if args.revisar_ocr:
            args.revisar = args.revisar or "todas"
    cfg.salvar(proj.config_path)
    ollama = Ollama(cfg.ollama_url)

    livro = etapa_extracao(proj, cfg, ui, args)
    etapa_idioma(proj, cfg, livro, ui, args)

    if args.extrair_apenas:
        ui.info(f"\nTexto extraído (idioma: {nome_idioma(cfg.origem)}). Nada foi traduzido (--extrair-apenas).")
        ui.info(f"  {proj.livro_md_path}")
        ui.info(f"  {proj.livro_path}")
        ui.info("Para traduzir, rode o mesmo comando sem --extrair-apenas (o OCR não é refeito).")
        return 0

    etapa_configuracao(proj, cfg, livro, ui, args)

    if args.exportar_apenas:
        perfil = Perfil.carregar(proj.perfil_path) if proj.perfil_path.exists() else Perfil.padrao("outro")
        tradutor = criar_tradutor(proj, cfg, perfil, ollama)
    else:
        perfil = etapa_perfil(proj, cfg, livro, ui, ollama, args)
        tradutor = criar_tradutor(proj, cfg, perfil, ollama)
        etapa_traducao(tradutor, proj, cfg, livro, ui, ollama)
    etapa_exportar(tradutor, proj, cfg, livro, ui)
    ui.info(f"\nPronto. Tudo está em {proj.raiz}")
    return 0


def main(argv=None) -> int:
    args = ler_args(argv)
    ui = UI(auto=args.sim)
    try:
        return executar(args, ui)
    except SaidaDoUsuario:
        ui.info("\nSaindo. O projeto está salvo; rode o mesmo comando para retomar.")
        return 0
    except KeyboardInterrupt:
        ui.info("\n\nInterrompido. O que já foi traduzido está salvo; rode o mesmo comando para continuar.")
        return 130
    except (ErroExtracao, OllamaErro) as e:
        ui.erro(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
