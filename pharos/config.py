"""Configuração do projeto e tabela de idiomas."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path

# código ISO -> (nome em português, nome em inglês)
IDIOMAS: dict[str, tuple[str, str]] = {
    "de": ("alemão", "German"),
    "en": ("inglês", "English"),
    "es": ("espanhol", "Spanish"),
    "fr": ("francês", "French"),
    "it": ("italiano", "Italian"),
    "pt": ("português", "Portuguese"),
    "nl": ("holandês", "Dutch"),
    "ca": ("catalão", "Catalan"),
    "ru": ("russo", "Russian"),
    "pl": ("polonês", "Polish"),
    "cs": ("tcheco", "Czech"),
    "sv": ("sueco", "Swedish"),
    "da": ("dinamarquês", "Danish"),
    "no": ("norueguês", "Norwegian"),
    "fi": ("finlandês", "Finnish"),
    "ro": ("romeno", "Romanian"),
    "hu": ("húngaro", "Hungarian"),
    "tr": ("turco", "Turkish"),
    "el": ("grego", "Greek"),
    "he": ("hebraico", "Hebrew"),
    "ar": ("árabe", "Arabic"),
    "ja": ("japonês", "Japanese"),
    "ko": ("coreano", "Korean"),
    "zh-cn": ("chinês (simplificado)", "Chinese"),
    "zh-tw": ("chinês (tradicional)", "Chinese"),
    "la": ("latim", "Latin"),
}

# códigos usados no prompt do TranslateGemma quando diferem do ISO simples
_CODIGO_PROMPT = {"zh-cn": "zh-Hans", "zh-tw": "zh-Hant"}


def nome_idioma(codigo: str, lingua: str = "pt") -> str:
    par = IDIOMAS.get(codigo.lower())
    if not par:
        return codigo
    return par[0] if lingua == "pt" else par[1]


def codigo_prompt(codigo: str) -> str:
    return _CODIGO_PROMPT.get(codigo.lower(), codigo)


def normalizar_codigo(texto: str) -> str | None:
    """Aceita 'pt', 'PT', 'português', 'portugues', 'ingles'... e devolve o código."""
    t = texto.strip().lower()
    if t in IDIOMAS:
        return t
    sem_acento = (
        t.replace("ê", "e").replace("é", "e").replace("ã", "a").replace("á", "a")
        .replace("ç", "c").replace("ô", "o").replace("í", "i")
    )
    for cod, (pt, en) in IDIOMAS.items():
        pt_sem = (
            pt.replace("ê", "e").replace("é", "e").replace("ã", "a").replace("á", "a")
            .replace("ç", "c").replace("ô", "o").replace("í", "i")
        )
        if sem_acento in (pt_sem, en.lower()):
            return cod
    return None


@dataclass
class Config:
    arquivo_original: str = ""
    titulo: str = ""
    autor: str = ""
    origem: str = ""
    destino: str = "pt"
    variante_destino: str = "português do Brasil"
    modelo: str = "translategemma:12b"
    modelo_analise: str = ""          # vazio = usa o mesmo modelo da tradução
    ollama_url: str = "http://localhost:11434"
    estilo_prompt: str = "auto"       # auto | translategemma | chat
    palavras_por_pagina: int = 300
    paginas_previa: int = 3
    contexto_blocos: int = 2          # parágrafos anteriores enviados como contexto (0 = desliga)
    max_caracteres_bloco: int = 3000  # parágrafos maiores são traduzidos por partes, em fronteiras de frase
    max_tentativas: int = 3
    temperatura: float = 0.2
    num_ctx: int = 8192
    validar_idioma: bool = True
    # OCR (livros escaneados)
    ocr_idioma: str = ""              # idioma(s) do Tesseract, ex.: deu, frk, deu+eng (vazio = decidir na extração)
    ocr_psm: int = 3                  # segmentação de página do Tesseract (3 = automática)
    ocr_dpi: int = 300
    ocr_processos: int = 0            # 0 = automático (núcleos - 1, no máximo 8)
    ocr_duplas: str = "auto"          # auto | sim | nao: imagens com duas páginas lado a lado
    ocr_duplas_aplicado: str = ""     # o que foi decidido na última leitura (sim | nao); vazio = ainda não decidido
    ocr_usar_camada: bool = False     # PDF com texto embutido: usar essa camada em vez de refazer o OCR
    ocr_limiar_palavra: int = 70      # confiança abaixo disso = palavra duvidosa
    ocr_limiar_pagina: int = 85       # pontuação abaixo disso = página sinalizada para revisão
    modelo_ocr: str = ""              # modelo que sugere correções de OCR na revisão (vazio = um modelo geral instalado)
    formatos: list = field(default_factory=lambda: ["epub", "docx", "md", "html"])

    @property
    def modelo_para_analise(self) -> str:
        return self.modelo_analise or self.modelo

    @classmethod
    def carregar(cls, caminho: Path) -> "Config":
        dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
        validos = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in dados.items() if k in validos})

    def salvar(self, caminho: Path) -> None:
        Path(caminho).write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )
