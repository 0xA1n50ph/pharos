# Pharos — tradutor de livros com IA local (Ollama)

Traduz livros inteiros (PDF com texto **ou escaneado**, pasta de imagens de páginas, EPUB, TXT/Markdown e, com o
Calibre instalado, MOBI/AZW3/FB2/DOCX) usando um modelo local do Ollama, com **perfil de tradução** (gênero,
tom, regras, glossário), uma **prévia das 3 primeiras páginas para refinar o estilo** antes de gastar horas no
livro todo, validação automática de cada trecho e retomada de onde parou.

Livros escaneados passam por **OCR (Tesseract)** e por uma **tela de revisão** no navegador, onde você compara
a imagem de cada página com o texto reconhecido e corrige o que for preciso *antes* de traduzir.

## Instalação

```bash
./setup.sh              # verifica e prepara tudo, perguntando antes de instalar (macOS e Linux)
./pharos.sh livro.pdf    # atalho criado pelo setup: usa o ambiente .venv sem precisar "ativá-lo"
```

O `setup.sh` confere e, no que faltar, prepara (cada passo só mexe no que falta; pode rodar quantas vezes quiser):

1. **Sistema:** SO, memória (modelos 12B pedem uns 10 GB), `curl`, gerenciador de pacotes (Homebrew, apt, dnf, pacman, zypper).
2. **Python 3.10+** e o ambiente virtual `.venv` (instala o Python pelo gerenciador de pacotes, se você deixar).
3. **Bibliotecas** do `requirements.txt` dentro do `.venv`, conferidas por importação.
4. **OCR:** Tesseract e os idiomas `eng osd deu spa por` (via `brew`/`apt`; o que o pacote não trouxer é baixado do
   repositório oficial do Tesseract) e os modelos de **letra gótica** `frk` e `deu_latf`.
5. **Ollama** (local ou em outra máquina) e os modelos: `translategemma:12b` (obrigatório) e `gemma3:12b`
   (opcional, usado só nas sugestões de correção do OCR). Baixa pela API do Ollama, com barra de progresso e
   novas tentativas; se cair no meio, o download retoma de onde parou.
6. **Opcionais:** Calibre (MOBI/AZW3/FB2/DOCX).
7. **Conferência:** abre o próprio programa e confere se ele enxerga o Tesseract; cria o atalho `./pharos.sh`.

Nada que peça senha de administrador (`sudo`) roda sem a sua confirmação; sem terminal interativo e sem `-y`, o
script não instala pacotes do sistema nem baixa modelos (só prepara o `.venv`). O instalador oficial do Ollama no
Linux (`curl ... | sh`) só roda com `--instalar-ollama` ou se você responder "sim" à pergunta. Termina com código 0
quando o ambiente está pronto e 1 quando falta algo obrigatório (modelo de tradução, Python, bibliotecas, Ollama).

| Opção do `setup.sh` | Efeito |
|---|---|
| `--verificar` | só diagnostica: não instala nem altera nada (bom para conferir uma máquina) |
| `-y`, `--sim` | responde "sim" ao que é de rotina (perguntas de risco, como pouco espaço em disco, continuam "não") |
| `--ollama URL` | Ollama em outra máquina (ex.: `http://pi.local:11434`); fica em `.setup.conf` e o `./pharos.sh` passa a usá-lo |
| `--modelo NOME`, `--modelo-ocr NOME` | outro modelo de tradução / outro modelo geral para as sugestões de OCR (lembrados em `.setup.conf`) |
| `--sem-modelos`, `--sem-modelo-ocr` | não baixa modelos (só confere) / não baixa o modelo das sugestões de OCR |
| `--sem-ocr`, `--idiomas fra,ita` | pula o Tesseract / acrescenta idiomas de OCR |
| `--python CMD`, `--sem-venv` | escolhe o interpretador / usa o Python direto, sem `.venv` |
| `--dev` | também instala o necessário para rodar os testes (reportlab, playwright + Chromium) |
| `--calibre`, `--instalar-ollama` | instala o Calibre / aceita o instalador oficial do Ollama (Linux) |
| `--testar` | faz uma tradução curta com cada modelo para ver se carregam (leva alguns minutos) |

Se preferir instalar à mão: `pip install -r requirements.txt` (Python 3.10+), `ollama pull translategemma:12b` e,
para livros escaneados, `brew install tesseract tesseract-lang` (Linux: `apt install tesseract-ocr tesseract-ocr-deu ...`).
O Ollama precisa estar rodando (app aberto ou `ollama serve`). Confira os idiomas do OCR com `tesseract --list-langs`:
para alemão você precisa de `deu`; para livros impressos em letra gótica (Fraktur), de `frk` ou `deu_latf` também. Se
faltar algum, o programa diz qual e como instalar.

## Uso

```bash
python pharos.py livro.pdf                    # fluxo guiado, com perguntas
python pharos.py livro.epub --destino en
python pharos.py livro.epub --modelo gemma3:12b --glossario meu_glossario.tsv
python pharos.py livro/                       # retoma um projeto interrompido (ou rode o mesmo comando)
python pharos.py livro.pdf --sim              # aceita as sugestões, sem perguntas (lote)
python pharos.py scan.pdf --origem de         # scan: OCR em alemão (testa letra gótica sozinho) + revisão
python pharos.py fotos_das_paginas/ --ocr-lang frk   # pasta de imagens, OCR em Fraktur
python pharos.py livro/ --revisar-ocr         # reabre a revisão do OCR de um projeto já extraído
```

| Opção | Efeito |
|---|---|
| `--origem` / `--destino` | idiomas (`de`, `es`, `en`, `pt`...). Sem elas o programa detecta e pergunta |
| `--modelo`, `--modelo-analise` | modelo da tradução; modelo só para sugerir o perfil |
| `--ollama URL` | Ollama em outra máquina (ex.: o Pi 5) |
| `--glossario ARQ.tsv` | glossário inicial |
| `--genero` | `ocultismo`, `ficcao`, `filosofia`, `religioso`, `academico`, `outro` |
| `--formatos` | `epub,docx,md,html` |
| `--previa-paginas N` | tamanho da prévia (padrão 3) |
| `--sem-previa`, `--sem-contexto` | pula a prévia; não envia parágrafos anteriores como contexto |
| `--exportar-apenas` | reexporta o que já está traduzido, sem chamar o modelo |
| `--saida PASTA` | onde criar a pasta do projeto (padrão: pasta atual) |
| `--ocr-lang CÓDIGO` | idioma/modelo do Tesseract: `deu`, `frk`, `deu_latf`, `deu+eng`... Sem isso, deduzido de `--origem` ou detectado |
| `--revisar sinalizadas\|todas\|nenhuma` | revisão do OCR: só as páginas suspeitas, todas ou nenhuma (padrão: pergunta; com `--sim`: nenhuma) |
| `--revisar-ocr` | reabre a revisão do OCR de um projeto já extraído; só o que mudou é retraduzido |
| `--ocr-forcar` | refaz o OCR mesmo havendo resultado salvo, inclusive num projeto já extraído (as edições da revisão antiga ficam em `ocr_revisao.antigo.json`) |
| `--paginas-duplas auto\|sim\|nao` | imagens com duas páginas lado a lado: separar ou não (padrão: auto, com confirmação) |
| `--usar-camada-embutida` | scan que já traz texto embutido: usar esse texto em vez de refazer o OCR |
| `--ocr-imagens` | PDF de texto com algumas páginas só de imagem: ler essas páginas com OCR |
| `--ocr-dpi N`, `--ocr-processos N` | resolução de leitura (padrão 300) e páginas em paralelo (padrão: núcleos − 1, máx. 8) |
| `--modelo-ocr MODELO` | modelo do Ollama que **sugere** correções de OCR na tela de revisão (padrão: um modelo geral instalado, como `gemma3`; o de tradução não é ideal) |
| `--sugestoes-ia sinalizadas\|todas` | já pede ao modelo sugestões de correção em segundo plano quando a revisão abre (sem isso, use o botão da tela) |
| `--sem-navegador` | não abre o navegador na revisão; só mostra o endereço (útil por SSH) |

## O fluxo

1. **Arquivo.** Identifica o formato pelo conteúdo. Em PDF, classifica cada página (texto, scan ou texto
   embutido por outra ferramenta de OCR), propõe o método de extração e pede confirmação. Scan ou pasta de
   imagens seguem para o OCR (próxima seção); PDF misto lê o texto direto onde há e usa OCR no resto.
2. **Idioma.** Detecta o idioma amostrando parágrafos de várias partes do livro (inclui latim) e pede
   confirmação.
3. **Configuração.** Título, autor, idioma de destino, glossário e formatos de saída. É tudo que o programa
   pergunta antes da prévia.
4. **Perfil e prévia.** O programa propõe um perfil (a IA analisa trechos do livro, se você quiser), traduz
   ~3 páginas e mostra **lado a lado** em `previa/vN/previa.html`. Menu:

   | Tecla | Ação |
   |---|---|
   | `a` | aprovar e traduzir o livro inteiro |
   | `t` | ajustar o tom/registro |
   | `r` | adicionar regra de fidelidade |
   | `g` | adicionar termo ao glossário |
   | `e` | corrigir um parágrafo: vira exemplo para o modelo **e** vale na versão final |
   | `c` | comparar com a tradução sem perfil (mostra o que o perfil muda) |
   | `o` | testar outro trecho do livro |
   | `m` | trocar o modelo |
   | `s` | sair (o projeto fica salvo) |

   Cada versão da prévia guarda seu perfil e glossário em `previa/vN/`. As traduções da prévia aprovada são
   reaproveitadas na tradução do livro.
5. **Tradução.** Roda sozinha, parágrafo a parágrafo, com os parágrafos anteriores como contexto. `Ctrl+C`
   interrompe; o mesmo comando retoma de onde parou (cada parágrafo é gravado na hora).
6. **Exportação.** EPUB, DOCX, Markdown e edição bilíngue em HTML, mais `revisao/relatorio.md`.

## Livros escaneados (OCR)

Entram no OCR: PDF escaneado, PDF misto, pasta de imagens (`.jpg`, `.png`, `.tif`...; a ordem vem do nome do
arquivo, `pagina_2` antes de `pagina_10`) e TIFF de várias páginas.

1. **Idioma e modelo.** Com `--origem de` o programa usa `deu`. Sem isso, faz um OCR de amostra em duas páginas
   (ou lê o texto selecionável, em PDF misto) e pede confirmação. **Para alemão, testa o modelo normal contra os
   de letra gótica** (`frk`, `deu_latf`) em páginas de amostra e sugere o que leu com mais confiança; se o livro
   parece gótico e você não tem o modelo, avisa como instalar.
2. **Páginas duplas.** Se a maioria das imagens é mais larga que alta, pergunta se deve separar esquerda e
   direita (a sombra da lombada é descartada).
3. **Preparo e leitura.** Para cada página: ilumina por igual (sombras, foto com luz irregular), endireita a
   inclinação (até ±5°), gira páginas de lado ou de cabeça para baixo (quando a leitura vem ruim e o modelo
   `osd` está instalado) e roda o Tesseract, várias páginas em paralelo. Cada página é salva em `ocr/` assim que
   termina: `Ctrl+C` e o mesmo comando retomam do ponto em que parou.
4. **Estrutura.** A partir das palavras e posições reconhecidas, o programa remove cabeçalhos e números de
   página, separa notas de rodapé, reconhece títulos (por tamanho e centralização), versos/citações (linhas
   curtas ou recuadas), parágrafos (recuo, espaçamento, fim de linha), junta parágrafos que atravessam a página
   e desfaz hifenização.
5. **Qualidade.** Cada página recebe uma pontuação (confiança do Tesseract, palavras duvidosas, aparência de
   texto sem sentido). Abaixo de 85, ou com muitas palavras duvidosas, é **sinalizada**. Se mais da metade
   ficar ruim, o programa avisa (idioma errado? letra gótica? scan fraco?) e oferece refazer o OCR com outro
   modelo na hora.
6. **Revisão.** O programa pergunta se você quer revisar só as sinalizadas (recomendado), todas ou nenhuma, e
   abre uma tela no navegador (servidor local, só nesta máquina, protegido por token):
   - esquerda: lista de páginas, com filtro (sinalizadas / não revisadas / todas);
   - centro: a imagem da página, com **caixas nas palavras duvidosas** (clique numa caixa e o cursor vai para a
     palavra no texto) e zoom;
   - direita: o texto em blocos editáveis. Dá para mudar o tipo do bloco (parágrafo, título e nível, verso,
     nota), **juntar** com o anterior, **dividir** no cursor, apagar ou acrescentar;
   - **sugestões** de correção pelo próprio livro (uma palavra rara parecida com uma palavra frequente do mesmo
     livro: `lighthonse` → `lighthouse`), aplicáveis na página ou no livro todo;
   - **sugestões por IA** (opcional, veja abaixo): um modelo de linguagem aponta erros de OCR; você aceita ou
     rejeita cada um;
   - **localizar e substituir no livro todo**, com contagem antes de aplicar;
   - `Ctrl+Enter` salva e vai para a próxima; `Alt+←/→` navega; `Concluir revisão` fecha a etapa.

   Tudo é gravado a cada ação em `revisao/ocr_revisao.json`. `Ctrl+C` no terminal pausa; ao rodar de novo, a
   revisão reabre do ponto em que estava. Com `--revisar-ocr` você volta a ela mesmo depois de extrair o livro
   (por exemplo, ao notar um erro de OCR na prévia da tradução); o cache de tradução é por conteúdo, então só os
   trechos alterados são retraduzidos.
   **Sugestões por IA.** No topo da tela, "Sugestões por IA" pede a um modelo do Ollama que aponte erros de OCR
   na página atual, nas páginas sinalizadas ou em todas (a passada roda em segundo plano, mostra o andamento,
   tem botão **Parar** e as sugestões vão aparecendo enquanto você revisa; `--sugestoes-ia` já a inicia ao abrir
   a revisão). Cada sugestão aparece como `trecho atual → trecho proposto`, com o contexto e a confiança que o
   OCR tinha na palavra; **aceitar** aplica na caixa de texto (você ainda salva a página), **rejeitar** a
   esconde de vez. A regra é: **o modelo sugere, você decide**. Nada muda no livro sem um clique seu, e ao
   concluir a revisão a tela avisa quantas sugestões pendentes não serão aplicadas. Para um livro com
   linguagem ocultista ou literária isso importa, então o programa também barra, **antes** de mostrar:
   - trocas em palavras que o OCR leu com confiança alta (90% ou mais);
   - qualquer troca que mude **números**, pontuação, ou que acrescente/apague palavras (só se apaga símbolo
     que o OCR inventou, como `|` ou `~`);
   - a "modernização" de grafia antiga (`Thür` → `Tür`, `seyn` → `sein`, `Muth` → `Mut`): grafia do autor não é erro;
   - trocas por palavra muito diferente da lida, e respostas que parecem tradução ou reescrita do bloco;
   - e só vão ao modelo os blocos que têm alguma palavra de confiança baixa (o resto não precisa).

   O texto do livro é enviado ao modelo como dado a corrigir, nunca como instrução, e o resultado só vale depois
   dessas validações e do seu clique. Ainda assim o modelo não conhece o livro e pode errar: confira com a imagem.
   O modelo é escolhido por `--modelo-ocr`; sem ele, usa um modelo geral instalado (`gemma3`, `qwen`, `llama3`,
   `mistral`, o maior que couber em 14B) porque o TranslateGemma tende a traduzir em vez de corrigir (se for o
   único instalado, o programa usa e avisa). As sugestões ficam em `revisao/ocr_sugestoes_ia.json` e o modelo é
   descarregado da memória ao fechar a revisão. Num Pi 5 isso é lento: rode a passada no Mac (`--ollama`).
7. **Texto final.** O livro montado (com as suas correções) vira `extraido/livro.md` e `livro.json`, e a
   tradução segue como nos outros formatos. `revisao/ocr_qualidade.md` lista as páginas sinalizadas, revisadas e
   editadas.

**Dicas para o scan:** 300 dpi, preto e branco ou cinza, páginas retas. Foto de celular funciona, mas com
luz uniforme e sem dedos na página. O programa corrige inclinação leve e sombras, não dobras nem páginas
curvas perto da lombada.

## Perfil de tradução

`perfil.json` guarda: gênero, descrição da obra, **registro/tom**, **regras de fidelidade** (não omitir, não
acrescentar, não modernizar...) e **exemplos** corrigidos por você. Os textos que vão para o modelo ficam em
inglês (modelos seguem melhor); você pode escrever o tom em português. Para o TranslateGemma, o prompt segue o
formato oficial do modelo, com as instruções extras entre a frase de papel e o pedido. Para modelos de chat
(Gemma 3, Qwen, Llama), usa mensagem de sistema + exemplos.

## Glossário

`glossario.tsv`, uma linha por termo, separado por TAB: `termo no original`, `tradução obrigatória`, `nota`.

```
Sephiroth	Sefirot	nome hebraico, transliteração usual em português
```

Só os termos presentes em cada parágrafo vão no prompt (aceita flexões no fim da palavra). Depois da
tradução, o programa confere se cada termo foi aplicado; se não, refaz o parágrafo.

## Validação e relatório

Cada trecho traduzido passa por: resposta vazia, ainda no idioma original, idêntica ao original, tamanho
muito diferente (omissão/acréscimo), quebras de linha a mais, contagem de linhas em versos e aplicação do
glossário. Se falhar, refaz até 3 vezes dizendo ao modelo o que estava errado. O que esgotar as tentativas é
mantido (a melhor versão) e listado em `revisao/relatorio.md` para revisão humana.

## Pasta do projeto

```
nome-do-livro/
  original/        arquivo enviado
  extraido/        livro.md (texto no idioma original) e livro.json (formato interno)
  ocr/             (scans) resultado do OCR de cada página: palavras, posições e confiança
  paginas/         (scans) imagem preparada de cada página, usada na tela de revisão
  previa/vN/       cada rodada da prévia: previa.html, perfil.json, glossario.tsv
  traduzido/       cache.jsonl (uma linha por parágrafo) e correcoes.json (suas correções à mão)
  revisao/         relatorio.md, paginas_sem_texto.txt, ocr_qualidade.md, ocr_revisao.json (suas correções do OCR)
                   e ocr_sugestoes_ia.json (sugestões da IA, com o que você aceitou e rejeitou)
  saida/           EPUB, DOCX, Markdown e edição bilíngue
  config.json  estado.json  perfil.json  glossario.tsv
```

`config.json` tem ajustes finos: `temperatura`, `num_ctx`, `contexto_blocos`, `max_tentativas`,
`max_caracteres_bloco`, `estilo_prompt` (`auto`, `translategemma` ou `chat`) e, para o OCR, `ocr_psm`,
`ocr_dpi`, `ocr_limiar_palavra` (confiança abaixo da qual uma palavra é "duvidosa", padrão 70) e
`ocr_limiar_pagina` (pontuação abaixo da qual a página é sinalizada, padrão 85) e `modelo_ocr` (modelo das
sugestões por IA).

## Limitações desta versão

- **OCR:** só foi testado com Tesseract em **inglês**. A escolha de modelo (inclusive Fraktur), a detecção de
  idioma e o aviso de instalação são testados com um Tesseract simulado; na prática, a qualidade em alemão e
  em letra gótica depende dos modelos instalados e do estado do scan. Revise as primeiras páginas antes de
  confiar no resto.
- **`setup.sh`:** foi testado no Linux contra ferramentas e servidores simulados (macOS, Homebrew, Ollama e GitHub
  de mentira; veja `tests/teste_setup.py`). Não foi rodado num Mac de verdade, no bash 3.2 do macOS, com
  `ollama pull` real (≈ 16 GB), com o PyPI nem com o download real dos dados do Tesseract. Se algum passo falhar na sua
  máquina, `./setup.sh --verificar` mostra o estado e o script pode ser rodado de novo: ele retoma de onde parou.
- **Sugestões por IA:** só foram testadas com um Ollama simulado (um "modelo" que corrige exatamente os erros do
  OCR, mais iscas que as validações têm de barrar). Quanto o `gemma3:12b` real acerta em alemão ou em Fraktur
  ainda não foi medido: comece por uma página e veja se as sugestões ajudam. Elas só pegam erros em palavras de
  confiança baixa; erro "confiante" do OCR (palavra real errada, lida com 90%+) só aparece na sua leitura.
- O OCR não lê escrita à mão, nem livros de duas colunas ou tabelas complexas com a ordem certa; marcadores de
  nota de rodapé (¹) às vezes são lidos como `!` ou `1`. Livros com muitas ilustrações geram páginas
  "sem texto" que aparecem como sinalizadas; marque-as como revisadas.
- PDF de texto com poucas páginas só de imagem (capa, pranchas): essas páginas ficam de fora e são listadas em
  `revisao/paginas_sem_texto.txt` (use `--ocr-imagens` para lê-las).
- Em PDF misto, páginas duplas não são separadas e cabeçalhos repetidos só são removidos se aparecerem em
  3 páginas ou mais de cada parte.
- PDFs de duas colunas e tabelas complexas podem sair com a ordem de leitura trocada.
- Imagens e capa não são levadas para o EPUB/DOCX traduzido.
- Arquivos com DRM não são lidos.
- A detecção de idioma cobre alemão, inglês, espanhol, francês, italiano, português, holandês, latim e
  idiomas de outros alfabetos (russo, grego, hebraico, árabe, coreano, japonês, chinês). Para outros
  idiomas, digite o código na confirmação.
- O TranslateGemma é um modelo de tradução e pode seguir instruções de estilo pior do que um modelo de chat.
  Use a opção `c` da prévia para ver se o perfil muda o resultado; se não mudar, teste `--modelo gemma3:12b`.

## Testes

```bash
./setup.sh --dev                                 # (ou: pip install reportlab pillow numpy pypdfium2 playwright)
python tests/teste_fluxo.py                      # tradução, prévia, glossário, EPUB/PDF/TXT
python tests/teste_ocr.py                        # OCR e revisão (precisa do Tesseract com 'eng')
python tests/teste_ia.py                         # sugestões de correção por IA (partes A–D rodam sem Tesseract)
python tests/teste_setup.py                      # o setup.sh, com ferramentas e servidores de mentira (1–3 min)
```

`teste_fluxo.py` roda o fluxo inteiro contra `tests/ollama_falso.py`, um servidor que imita a API do Ollama
(com falhas injetadas), sem precisar de modelo.

`teste_ocr.py` gera livros escaneados de amostra (inclinados, com ruído e sombra) e confere, com o Tesseract de
verdade: inclinação e páginas duplas, scan em PDF, pasta de imagens (uma de cabeça para baixo), PDF misto,
texto embutido, similaridade do texto reconhecido com o original (≥ 98,5%), página ruim sinalizada, a tela de
revisão pela API e no navegador (Playwright, se instalado), pausa e retomada, `--revisar-ocr` e o caminho
scan → OCR → tradução → exportação. A lógica de idioma/Fraktur e a rota do PDF usam um Tesseract simulado.

`teste_ia.py` cobre as sugestões por IA: as validações (confiança alta, números, grafia antiga, acréscimos,
reescrita, quebras de linha), o cache e a persistência, a passada em lote (parar, falhas do Ollama, modelo ausente),
a garantia de que **gerar sugestões não altera o texto**, a API protegida por token, e, com o Tesseract de verdade,
um scan degradado do início ao fim (aceitar uma sugestão a leva ao livro; rejeitar não; `--sugestoes-ia`) e a tela
no navegador (aceitar, rejeitar, aceitar todas, Parar, selo por página, aviso ao concluir, modo escuro e celular).

`teste_setup.py` exercita o `setup.sh` sem internet e sem baixar nada: monta um "mundo" descartável com `brew`, `apt-get`,
`sudo`, `uname` (macOS), `tesseract` e `ollama` falsos no PATH, um Ollama de mentira com `/api/pull` (progresso, quedas no
meio, erros, modelo inexistente) e um servidor local no lugar do GitHub do Tesseract. Confere: `--verificar` sem efeitos
colaterais, instalação completa e segunda execução idempotente, Python/Tesseract/Ollama ausentes, Ollama local parado e
remoto, `.setup.conf` e `./pharos.sh`, pouco espaço em disco, execução sem terminal (nunca instala nem baixa sozinho),
perguntas s/n num terminal de verdade (pty), Ctrl+C, e uma varredura por recursos do bash 4+ (o macOS traz o bash 3.2).
