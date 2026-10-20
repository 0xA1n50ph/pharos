"""Etapa de OCR: do scan (PDF, imagens) ao Livro no formato interno, com avaliação de qualidade e revisão humana."""
from __future__ import annotations

import os
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Config, IDIOMAS, nome_idioma, normalizar_codigo
from .extratores import pdf_texto
from .extratores.comum import ErroExtracao
from .idioma import detectar_trecho
from .modelo import Livro, TITULO
from .ocr import layout, montagem, motor, preprocessar, qualidade, servidor, tesseract
from .ocr.motor import ParamsOCR
from .ocr.paginas import FontePaginas
from .ocr.revisao import Revisao, Sessao
from .ocr.sugestoes_ia import SugestoesIA
from .ocr.tesseract import ErroOCR
from .ollama import Ollama
from .projeto import Projeto
from .ui import Barra, SaidaDoUsuario, UI

_DIFERENCA_GOTICA = 8.0   # pontos de confiança média que o modelo gótico precisa ganhar para ser sugerido
_CONFIANCA_BAIXA = 65.0   # abaixo disso o OCR de amostra merece um aviso


def _intervalos(numeros: list[int]) -> str:
    """[1,2,3,7,9,10] -> '1–3, 7, 9–10'."""
    if not numeros:
        return ""
    partes, ini, ant = [], numeros[0], numeros[0]
    for n in numeros[1:] + [None]:
        if n is not None and n == ant + 1:
            ant = n
            continue
        partes.append(str(ini) if ini == ant else f"{ini}–{ant}")
        if n is not None:
            ini = ant = n
    return ", ".join(partes)


def _resumo_lista(numeros: list[int]) -> str:
    texto = _intervalos(sorted(numeros))
    return texto if len(texto) <= 120 else texto[:117].rsplit(",", 1)[0] + f" … (+{len(numeros)} no total)"


# ----------------------------------------------------------------------------- rota: OCR ou texto direto

def rotear_pdf(original: Path, cfg: Config, ui: UI, args) -> dict | None:
    """Decide como extrair um PDF. None = extração direta do texto; senão {'ocr': [índices], 'texto': {páginas}}.

    Índices do OCR começam em 0; páginas de texto começam em 1."""
    ui.info("Classificando cada página (texto, scan, camada de OCR embutida)...")
    classes = pdf_texto.classificar_paginas(original)
    cont = Counter(classes)
    n_texto, n_scan, n_camada = cont["texto"], cont["scan"], cont["camada"]
    uteis = n_texto + n_scan + n_camada
    com_texto = {i + 1 for i, c in enumerate(classes) if c == "texto"}
    com_camada = {i + 1 for i, c in enumerate(classes) if c == "camada"}
    ocr_scan = [i for i, c in enumerate(classes) if c == "scan"]
    ocr_scan_camada = [i for i, c in enumerate(classes) if c in ("scan", "camada")]

    if uteis == 0:
        ui.aviso("Nenhuma página tem texto selecionável nem imagem de página inteira.")
        if not ui.confirmar("Tentar o OCR em todas as páginas mesmo assim?", False):
            raise ErroExtracao("PDF sem conteúdo aproveitável.")
        return {"ocr": list(range(len(classes))), "texto": set()}

    if n_camada / uteis >= 0.5:
        ui.info(f"É um scan que já traz uma camada de texto (OCR de outra ferramenta) em {n_camada} página(s).")
        ui.info("Essa camada costuma ter erros, sobretudo em livros antigos, e não informa a confiança de cada palavra:\n"
                "sem isso não dá para destacar o que é duvidoso na revisão. Refazer o OCR com o Tesseract é mais lento,\n"
                "mas dá esse controle.")
        if cfg.ocr_idioma and not cfg.ocr_usar_camada:
            usar = False  # já começou a refazer o OCR numa execução anterior
        elif args.usar_camada_embutida:
            usar = True
        elif ui.auto:
            usar = False
        else:
            usar = not ui.confirmar("Refazer o OCR com o Tesseract? (Não = usar o texto embutido)", True)
        cfg.ocr_usar_camada = usar
        if usar:
            return None
        return {"ocr": ocr_scan_camada, "texto": com_texto}

    # páginas de texto e de imagem misturadas: com várias páginas de imagem (ou quase tudo imagem), é um livro escaneado
    if n_scan / uteis >= 0.2 and (n_scan >= 3 or n_scan / uteis >= 0.8):
        return {"ocr": ocr_scan, "texto": com_texto | com_camada}

    if n_scan:  # poucas páginas de imagem num livro de texto: capa, pranchas, ilustrações
        ui.info(f"{n_scan} página(s) são só imagem (capa, ilustrações, pranchas?): {_resumo_lista([i + 1 for i in ocr_scan])}")
        if args.ocr_imagens or (not ui.auto and ui.confirmar("Rodar o OCR nelas também?", False)):
            return {"ocr": ocr_scan, "texto": com_texto | com_camada}
        ui.info("Elas ficam de fora do texto (use --ocr-imagens para lê-las com OCR).")
    return None


# ----------------------------------------------------------------------------- idioma e modelo do OCR

def _amostra(indices: list[int], n: int) -> list[int]:
    """n páginas espalhadas entre 15% e 85% do livro (evita capa, índice e contracapa)."""
    if len(indices) <= n:
        return list(indices)
    ini, fim = int(len(indices) * 0.15), max(int(len(indices) * 0.85), int(len(indices) * 0.15) + n)
    meio = indices[ini:fim] or indices
    passo = len(meio) / n
    return [meio[int(passo * k + passo / 2)] for k in range(n)]


def _ocr_de_amostras(exe: str, arquivos: list[Path], specs: list[str], cfg: Config) -> dict[str, list[list]]:
    """Roda cada idioma/modelo em cada página de amostra, em paralelo. {spec: palavras de todas as páginas}."""
    tarefas = [(spec, arq) for spec in specs for arq in arquivos]
    n = max(1, min(len(tarefas), (os.cpu_count() or 2) - 1 or 1, 6))

    def uma(t):
        return t[0], tesseract.rodar_tsv(exe, t[1], t[0], cfg.ocr_psm, cfg.ocr_dpi)

    saida: dict[str, list[list]] = {s: [] for s in specs}
    with ThreadPoolExecutor(max_workers=n) as ex:
        for spec, palavras in ex.map(uma, tarefas):
            saida[spec].extend(palavras)
    return saida


def _perguntar_idioma(ui: UI, padrao: str) -> str:
    while True:
        resp = ui.perguntar("Idioma do livro (Enter confirma; ou digite outro código/nome)", padrao)
        cod = normalizar_codigo(resp) if resp else None
        if cod:
            return cod
        if ui.auto:
            raise ErroExtracao("Idioma de origem indefinido; use --origem ou --ocr-lang.")
        ui.aviso(f"Não reconheci '{resp}'. Códigos conhecidos: {', '.join(sorted(IDIOMAS))}")


def _detectar_idioma(fonte: FontePaginas, indices: list[int], metade_de, exe: str, cfg: Config, ui: UI) -> str:
    instalados = tesseract.idiomas_instalados(exe)
    candidatos = [c for c in tesseract.CANDIDATOS_DETECCAO if c in instalados]
    if not candidatos:
        raise ErroOCR("Nenhum idioma de OCR instalado. " + tesseract.INSTRUCOES_INSTALACAO)
    spec = "+".join(candidatos)
    ui.info(f"Descobrindo o idioma com um OCR de amostra ({', '.join(candidatos)})...")
    with tempfile.TemporaryDirectory(prefix="amostra_") as tmp:
        arquivos = [motor.preparar_amostra(fonte, i, cfg.ocr_dpi, Path(tmp), metade_de(i)) for i in _amostra(indices, 2)]
        palavras = _ocr_de_amostras(exe, arquivos, [spec], cfg)[spec]
    r = detectar_trecho(" ".join(p[8] for p in palavras))
    padrao = ""
    if r:
        padrao = r[0]
        ui.info(f"Idioma detectado: {nome_idioma(r[0])} ({round(r[1] * 100)}% das palavras de função)")
    else:
        ui.aviso("Não consegui detectar o idioma automaticamente (em livros de letra gótica isso é comum).")
    return _perguntar_idioma(ui, padrao)


def _escolher_goticas(fonte, indices, metade_de, exe: str, base: str, iso: str, cfg: Config, ui: UI) -> str:
    """Compara o modelo normal com os de letra gótica (Fraktur) numa amostra e devolve o spec escolhido."""
    goticas = tesseract.goticas_instaladas(exe, iso)
    amostra = _amostra(indices, 4)
    if not goticas:
        # sem modelo gótico: só testa o normal para poder avisar
        specs = [base]
    else:
        specs = [base] + goticas
    ui.info(f"Testando o OCR em {len(amostra)} página(s) de amostra ({', '.join(specs)})...")
    with tempfile.TemporaryDirectory(prefix="amostra_") as tmp:
        arquivos = [motor.preparar_amostra(fonte, i, cfg.ocr_dpi, Path(tmp), metade_de(i)) for i in amostra]
        palavras = _ocr_de_amostras(exe, arquivos, specs, cfg)
    medias = {s: tesseract.confianca_media(w) for s, w in palavras.items()}
    ui.info("Confiança média na amostra: " + "; ".join(f"{s} {m:.0f}%" for s, m in medias.items()))
    if not goticas:
        if medias[base] < _CONFIANCA_BAIXA and iso in tesseract.GOTICAS:
            ui.aviso(
                f"Confiança baixa ({medias[base]:.0f}%). Se o livro for impresso em letra gótica (Fraktur), instale um modelo "
                f"({' ou '.join(tesseract.GOTICAS[iso])}): brew install tesseract-lang  — e rode de novo com --ocr-forcar."
            )
        return base
    melhor = max(medias, key=lambda s: medias[s])
    if melhor != base and medias[melhor] >= medias[base] + _DIFERENCA_GOTICA:
        ui.info(f"O modelo '{melhor}' leu bem melhor: o livro parece impresso em letra gótica (Fraktur).")
        if ui.auto or ui.confirmar(f"Usar '{melhor}' no OCR?", True):
            return melhor
    return base


def definir_idioma(fonte, indices, metade_de, exe: str, proj: Projeto, cfg: Config, ui: UI, args,
                   texto_amostra: str = "") -> tuple[str, str]:
    """(idioma/modelo do Tesseract, código ISO do livro ou ''). texto_amostra: texto selecionável de um PDF misto."""
    if cfg.ocr_idioma and not args.ocr_lang:  # decidido numa execução anterior
        tesseract.verificar(exe, cfg.ocr_idioma)
        return cfg.ocr_idioma, cfg.origem or tesseract.iso_do_codigo(cfg.ocr_idioma) or ""
    if args.ocr_lang:
        tesseract.verificar(exe, args.ocr_lang)
        iso = ""
        if args.origem:
            iso = normalizar_codigo(args.origem) or ""
        iso = iso or tesseract.iso_do_codigo(args.ocr_lang) or ""
        ui.info(f"OCR no idioma/modelo: {args.ocr_lang}")
        spec = args.ocr_lang
    else:
        if args.origem:
            iso = normalizar_codigo(args.origem)
            if not iso:
                raise ErroExtracao(f"Idioma de origem não reconhecido: {args.origem}")
        else:
            r = detectar_trecho(texto_amostra) if texto_amostra else None
            if r:
                ui.info(f"Idioma detectado pelo texto selecionável do PDF: {nome_idioma(r[0])} ({round(r[1] * 100)}% das palavras de função)")
                iso = _perguntar_idioma(ui, r[0])
            else:
                iso = _detectar_idioma(fonte, indices, metade_de, exe, cfg, ui)
        base = tesseract.codigo_para(iso)
        if not base:
            raise ErroOCR(f"Não conheço o modelo do Tesseract para {nome_idioma(iso)}. Use --ocr-lang com o código dele.")
        tesseract.verificar(exe, base)
        spec = _escolher_goticas(fonte, indices, metade_de, exe, base, iso, cfg, ui) if iso in tesseract.GOTICAS else base
    cfg.ocr_idioma = spec
    if iso:
        cfg.origem = iso
    cfg.salvar(proj.config_path)
    return spec, iso


# ----------------------------------------------------------------------------- páginas duplas

def definir_duplas(fonte: FontePaginas, indices: list[int], proj: Projeto, cfg: Config, ui: UI, args, misto: bool) -> bool:
    if misto:
        return False  # numeração das páginas de texto e de OCR não combinaria
    modo = args.paginas_duplas or cfg.ocr_duplas
    prop = motor.proporcao_de_duplas(fonte, indices)
    if modo == "sim":
        resultado = prop > 0
    elif modo == "nao":
        resultado = False
    elif cfg.ocr_duplas_aplicado in ("sim", "nao"):
        resultado = cfg.ocr_duplas_aplicado == "sim"  # já perguntado numa execução anterior
    elif prop >= 0.5:
        ui.info(f"{round(prop * 100)}% das imagens são bem mais largas que altas: parece haver duas páginas em cada imagem.")
        resultado = ui.auto or ui.confirmar("Separar cada imagem em duas páginas (esquerda e direita)?", True)
    else:
        resultado = False
    anterior = cfg.ocr_duplas_aplicado
    cfg.ocr_duplas_aplicado = "sim" if resultado else "nao"
    if anterior and anterior != cfg.ocr_duplas_aplicado and any(proj.ocr_dir.glob("p*.json")):
        ui.info("O modo de páginas duplas mudou: a numeração das páginas muda, então o OCR será refeito.")
        args.ocr_forcar = True
    cfg.salvar(proj.config_path)
    return resultado


# ----------------------------------------------------------------------------- relatório

def escrever_relatorio(proj: Projeto, spec: str, pontos: dict, resumo: dict, sessao: Sessao, erros: dict, revisao_feita: bool,
                      ia: SugestoesIA | None = None) -> None:
    revisadas = sessao.revisao.revisadas()
    editadas = sessao.revisao.editadas()
    linhas = [
        "# Qualidade do OCR", "",
        f"- Idioma/modelo do Tesseract: `{spec}`",
        f"- Páginas: {resumo['paginas']} · confiança média {resumo['media']:.0f}%",
        f"- Sinalizadas: {resumo['sinalizadas']} · revisadas: {len(revisadas)} · editadas à mão: {len(editadas)}",
        f"- Revisão feita na tela: {'sim' if revisao_feita else 'não'}",
    ]
    if ia is not None and ia.resumo()["geradas"]:
        r = ia.resumo()
        linhas.append(f"- Sugestões de correção por IA ({r['modelo']}): {r['geradas']} geradas, {r['aceitas']} aceitas, "
                      f"{r['rejeitadas']} rejeitadas (nada é aplicado sem a sua decisão)")
    if erros:
        linhas.append(f"- Páginas com erro no OCR (ficaram de fora): {_intervalos(sorted(erros))}")
    linhas += ["", "## Páginas sinalizadas ou editadas", "", "| Página | Pontuação | Confiança | Duvidosas | Motivos | Revisada | Editada |",
               "|---|---|---|---|---|---|---|"]
    for n, p in pontos.items():
        if p["sinalizada"] or n in editadas:
            linhas.append(f"| {n} | {p['score']:.0f} | {p['conf']:.0f}% | {p['duvidosas']} | {'; '.join(p['motivos']) or '—'} | "
                          f"{'sim' if n in revisadas else 'não'} | {'sim' if n in editadas else 'não'} |")
    proj.ocr_relatorio_path.parent.mkdir(parents=True, exist_ok=True)
    proj.ocr_relatorio_path.write_text("\n".join(linhas) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------- etapa completa

def _metadados_pdf(original: Path) -> tuple[str, str]:
    if original.is_dir() or original.suffix.lower() != ".pdf":
        return "", ""
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(original))
        try:
            m = pdf.get_metadata_dict()
        finally:
            pdf.close()
        return (m.get("Title") or "").strip(), (m.get("Author") or "").strip()
    except Exception:  # noqa: BLE001
        return "", ""


def _decidir_revisao(args, ui: UI, resumo: dict) -> str:
    """sinalizadas | todas | nenhuma"""
    if args.revisar:
        return args.revisar
    if ui.auto:
        return "nenhuma"
    n_sin, n = resumo["sinalizadas"], resumo["paginas"]
    ui.info("\nRevisão do OCR: você compara a imagem de cada página com o texto reconhecido e corrige o que for preciso.")
    if n_sin:
        ui.info(f"  1) Revisar só as {n_sin} página(s) sinalizadas (recomendado)")
        ui.info(f"  2) Revisar todas as {n} páginas")
        ui.info("  3) Pular a revisão e traduzir o texto como está")
        r = ui.perguntar("Escolha", "1")
        return {"1": "sinalizadas", "2": "todas", "3": "nenhuma"}.get(r.strip(), "sinalizadas")
    ui.info("  Nenhuma página ficou abaixo do limiar de qualidade.")
    ui.info(f"  1) Conferir mesmo assim (todas as {n} páginas, na tela de revisão)")
    ui.info("  2) Pular a revisão (recomendado)")
    r = ui.perguntar("Escolha", "2")
    return "todas" if r.strip() == "1" else "nenhuma"


def extrair_scan(proj: Projeto, cfg: Config, ui: UI, args, original: Path, indices: list[int] | None = None,
                 paginas_texto: set[int] | None = None) -> tuple[Livro, dict]:
    """OCR + layout + qualidade + revisão + montagem. Devolve (Livro, relatório para a etapa de extração)."""
    exe = tesseract.localizar()
    fonte = FontePaginas(original)
    try:
        return _extrair(proj, cfg, ui, args, original, fonte, exe, indices, paginas_texto or set())
    finally:
        fonte.fechar()


def _extrair(proj, cfg, ui, args, original, fonte, exe, indices, paginas_texto):
    if args.ocr_dpi:
        cfg.ocr_dpi = args.ocr_dpi
    if args.ocr_processos is not None:
        cfg.ocr_processos = args.ocr_processos
    todas = indices if indices is not None else list(range(fonte.total))
    if not todas:
        raise ErroExtracao("Nenhuma página para o OCR.")

    ui.titulo("1b. OCR")
    ui.info(f"Tesseract: {exe}")
    duplas = definir_duplas(fonte, todas, proj, cfg, ui, args, misto=bool(paginas_texto))

    def metade_de(i: int) -> int | None:
        return 0 if duplas and preprocessar.e_dupla(*fonte.tamanho(i)) else None

    extras, recuados = [], set()
    amostra_texto = ""
    if paginas_texto:
        extras, recuados, _meta, _rel = pdf_texto.extrair_blocos(original, set(paginas_texto), exigir_texto=False)
        paragrafos_longos = [b.texto for b in extras if b.tipo == "paragrafo" and len(b.texto) >= 120]
        amostra_texto = " ".join(paragrafos_longos[:: max(1, len(paragrafos_longos) // 40)][:40])
    spec, iso = definir_idioma(fonte, todas, metade_de, exe, proj, cfg, ui, args, amostra_texto)
    plano = motor.plano_de_paginas(fonte, todas, duplas)

    if args.ocr_forcar:
        for pasta, padrao in ((proj.ocr_dir, "p*.json"), (proj.paginas_dir, "p*.jpg")):
            for arq in pasta.glob(padrao):
                arq.unlink()
        if proj.ocr_revisao_path.exists():  # as edições eram sobre o OCR antigo
            proj.ocr_revisao_path.replace(proj.ocr_revisao_path.with_suffix(".antigo.json"))
        args.ocr_forcar = False

    while True:
        params = ParamsOCR(spec, cfg.ocr_psm, cfg.ocr_dpi)
        prontas = sum(1 for it in plano if motor.pagina_pronta(proj.ocr_dir, proj.paginas_dir, it["n"], params, it))
        ui.info(f"OCR de {len(plano)} página(s) com '{spec}' a {cfg.ocr_dpi} dpi"
                + (f" ({prontas} já feitas antes)." if prontas else "."))
        if prontas < len(plano):
            ui.info("Isso leva um tempo; pode interromper com Ctrl+C e retomar depois (o que já foi lido fica salvo).")
        barra = Barra(ui, "OCR")
        resultados, erros = motor.executar(
            fonte, plano, params, exe, proj.ocr_dir, proj.paginas_dir, cfg.ocr_processos,
            lambda feitos, total, n, d: barra(feitos, total, f"p. {n}"))
        if erros:
            ui.aviso(f"{len(erros)} página(s) falharam no OCR: {_resumo_lista(sorted(erros))}")
            for n, e in list(sorted(erros.items()))[:3]:
                ui.info(f"  p. {n}: {e[:160]}")
        if not resultados:
            raise ErroOCR("O OCR não conseguiu ler nenhuma página.")
        paginas = dict(sorted(resultados.items()))
        pontos = qualidade.pontuar_todas(paginas, cfg.ocr_limiar_palavra, cfg.ocr_limiar_pagina)
        resumo = qualidade.resumir(pontos)
        if resumo["geral_ruim"] and not ui.auto:
            ui.aviso(f"Mais da metade das páginas ficou com qualidade baixa (confiança média {resumo['media']:.0f}%). "
                     "Costuma ser idioma errado, letra gótica (Fraktur) ou scan de baixa resolução.")
            novo = ui.perguntar("Refazer o OCR com outro idioma/modelo? Digite o código do Tesseract (ex.: frk, deu_latf, deu+eng) "
                                "ou Enter para continuar assim", "").strip()
            if novo:
                try:
                    tesseract.verificar(exe, novo)
                except ErroOCR as e:
                    ui.aviso(str(e))
                    continue
                spec = novo
                cfg.ocr_idioma = spec
                cfg.salvar(proj.config_path)
                continue
        break

    if erros:
        if ui.auto:
            raise ErroExtracao("Falhas no OCR em algumas páginas; rode de novo para tentar outra vez.")
        if not ui.confirmar("Continuar sem essas páginas? (Não = parar agora e tentar de novo depois)", False):
            raise SaidaDoUsuario("OCR incompleto")

    ui.info(f"\nOCR pronto: {resumo['paginas']} página(s), confiança média {resumo['media']:.0f}%.")
    if resumo["sinalizadas"]:
        ui.info(f"{resumo['sinalizadas']} página(s) sinalizada(s) para revisão: {_resumo_lista(resumo['lista_sinalizadas'])}")
    auto_blocos, rel_layout = layout.analisar_documento(paginas)
    sugestoes = qualidade.gerar_sugestoes(paginas)
    if sugestoes:
        ui.info(f"{len(sugestoes)} sugestão(ões) de correção por consistência do próprio livro (aparecem na tela de revisão).")

    modo = _decidir_revisao(args, ui, resumo)
    titulo_livro = cfg.titulo or _metadados_pdf(original)[0] or original.stem.replace("_", " ")
    filtro = "sinalizadas" if (modo == "sinalizadas" and resumo["sinalizadas"]) else "todas"
    rev = Revisao(proj.ocr_revisao_path, params.hash())
    ia = SugestoesIA(proj.ocr_sugestoes_ia_path, params.hash(), Ollama(cfg.ollama_url, timeout=300), cfg.modelo_ocr,
                     cfg.modelo_analise or cfg.modelo, iso or cfg.origem, spec)
    sessao = Sessao(titulo_livro, paginas, auto_blocos, pontos, sugestoes, rev, proj.paginas_dir, cfg.ocr_limiar_palavra, filtro, ia)
    revisao_feita = False
    if modo != "nenhuma":
        ui.titulo("1c. Revisão do OCR")
        try:
            ok = servidor.rodar(sessao, abrir_navegador=not args.sem_navegador, saida=ui.info, ia_lote=args.sugestoes_ia)
        finally:
            if ia.estado_trabalho()["ativo"]:
                ui.info("Esperando a IA terminar a página atual...")
                ia.parar()
                ia.aguardar(180)
            ia.descarregar()  # libera a memória antes da tradução
        if not ok:
            raise SaidaDoUsuario("revisão do OCR pausada")
        revisao_feita = True
        ui.info(f"Revisão concluída: {len(rev.editadas())} página(s) editada(s), {len(rev.revisadas())} marcada(s) como revisada(s).")
        r = ia.resumo()
        if r["geradas"]:
            ui.info(f"Sugestões da IA ({r['modelo']}): {r['geradas']} geradas, {r['aceitas']} aceitas, {r['rejeitadas']} rejeitadas por você.")
    elif resumo["sinalizadas"]:
        ui.aviso("A revisão foi pulada; as páginas sinalizadas seguem com o texto do OCR.")
    elif rev.editadas():
        ui.info(f"Aplicando {len(rev.editadas())} página(s) editada(s) numa revisão anterior.")
    escrever_relatorio(proj, spec, pontos, resumo, sessao, erros, revisao_feita, ia)
    ui.info(f"Relatório de qualidade do OCR: {proj.ocr_relatorio_path}")

    livro = montagem.montar(sessao.blocos_finais(), extras, recuados, titulo_livro, cfg.autor or _metadados_pdf(original)[1], iso)
    if not cfg.titulo and not _metadados_pdf(original)[0] and livro.capitulos:
        rosto = livro.capitulos[0].blocos  # folha de rosto (só títulos): o primeiro é o título do livro
        if rosto and all(b.tipo == TITULO for b in rosto):
            livro.titulo = rosto[0].texto.strip() or livro.titulo
    proj.marcar(ocr_revisada=True)
    if iso:
        cfg.origem = iso
        proj.marcar(idioma=True)
    relatorio = {
        "cabecalhos_removidos": rel_layout.get("cabecalhos_removidos", []),
        "paginas_sem_texto": [],
        "ocr": {"paginas": resumo["paginas"], "media": resumo["media"], "sinalizadas": resumo["sinalizadas"], "idioma": spec},
    }
    return livro, relatorio
