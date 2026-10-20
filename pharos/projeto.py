"""Pasta do projeto: tudo de um livro (original, texto extraído, traduções, saídas) fica junto."""
from __future__ import annotations

import json
import re
import shutil
import unicodedata
from pathlib import Path

from .deteccao import IMAGENS_EXT

SUBPASTAS = ["original", "extraido", "ocr", "paginas", "revisao", "traduzido", "previa", "saida"]
ETAPAS_INICIAIS = {
    "extracao": False, "idioma": False, "configurado": False,
    "perfil_aprovado": False, "traducao_completa": False, "exportado": False,
}


def slug(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()
    return t or "livro"


class Projeto:
    def __init__(self, raiz: Path):
        self.raiz = Path(raiz)

    # caminhos
    def pasta(self, nome: str) -> Path:
        p = self.raiz / nome
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def config_path(self) -> Path:
        return self.raiz / "config.json"

    @property
    def estado_path(self) -> Path:
        return self.raiz / "estado.json"

    @property
    def perfil_path(self) -> Path:
        return self.raiz / "perfil.json"

    @property
    def glossario_path(self) -> Path:
        return self.raiz / "glossario.tsv"

    @property
    def livro_path(self) -> Path:
        return self.raiz / "extraido" / "livro.json"

    @property
    def livro_md_path(self) -> Path:
        return self.raiz / "extraido" / "livro.md"

    @property
    def cache_path(self) -> Path:
        return self.raiz / "traduzido" / "cache.jsonl"

    @property
    def correcoes_path(self) -> Path:
        return self.raiz / "traduzido" / "correcoes.json"

    @property
    def relatorio_path(self) -> Path:
        return self.raiz / "revisao" / "relatorio.md"

    @property
    def ocr_dir(self) -> Path:
        return self.raiz / "ocr"          # resultado do OCR por página (p0001.json...)

    @property
    def paginas_dir(self) -> Path:
        return self.raiz / "paginas"      # imagens das páginas usadas na tela de revisão

    @property
    def ocr_revisao_path(self) -> Path:
        return self.raiz / "revisao" / "ocr_revisao.json"

    @property
    def ocr_sugestoes_ia_path(self) -> Path:
        return self.raiz / "revisao" / "ocr_sugestoes_ia.json"

    @property
    def ocr_relatorio_path(self) -> Path:
        return self.raiz / "revisao" / "ocr_qualidade.md"

    @property
    def original(self) -> Path | None:
        """O arquivo do livro; para um conjunto de imagens, a própria pasta 'original'."""
        pasta = self.raiz / "original"
        arquivos = sorted(p for p in pasta.glob("*") if p.is_file() and not p.name.startswith("."))
        if len(arquivos) > 1 and all(p.suffix.lower() in IMAGENS_EXT for p in arquivos):
            return pasta
        return arquivos[0] if arquivos else None

    # ciclo de vida
    @classmethod
    def e_projeto(cls, caminho: Path) -> bool:
        return caminho.is_dir() and (caminho / "config.json").exists()

    @classmethod
    def abrir(cls, raiz: Path) -> "Projeto":
        return cls(raiz)

    @staticmethod
    def _nome_base(arquivo: Path) -> str:
        return slug(arquivo.name) + "-livro" if arquivo.is_dir() else slug(arquivo.stem)

    @classmethod
    def localizar(cls, arquivo: Path, base: Path) -> "Projeto | None":
        """Projeto já criado para este arquivo (mesmo nome de pasta e mesmo arquivo original)."""
        nome = cls._nome_base(arquivo)
        for raiz in [base / nome] + [base / f"{nome}-{n}" for n in range(2, 10)]:
            if not cls.e_projeto(raiz):
                continue
            try:
                cfg = json.loads((raiz / "config.json").read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if cfg.get("arquivo_original") == arquivo.name:
                return cls(raiz)
        return None

    @classmethod
    def criar(cls, arquivo: Path, base: Path) -> "Projeto":
        nome = cls._nome_base(arquivo)
        raiz = base / nome
        n = 2
        while raiz.exists():
            raiz = base / f"{nome}-{n}"
            n += 1
        for sub in SUBPASTAS:
            (raiz / sub).mkdir(parents=True, exist_ok=True)
        if arquivo.is_dir():  # conjunto de imagens: copia só as imagens, mantendo os nomes (a ordem vem deles)
            for img in sorted(arquivo.iterdir()):
                if img.is_file() and img.suffix.lower() in IMAGENS_EXT:
                    shutil.copy2(img, raiz / "original" / img.name)
        else:
            shutil.copy2(arquivo, raiz / "original" / arquivo.name)
        p = cls(raiz)
        p.estado_path.write_text(
            json.dumps({"etapas": dict(ETAPAS_INICIAIS)}, indent=2), encoding="utf-8"
        )
        return p

    # estado
    def estado(self) -> dict:
        try:
            d = json.loads(self.estado_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            d = {}
        etapas = dict(ETAPAS_INICIAIS)
        etapas.update(d.get("etapas", {}))
        d["etapas"] = etapas
        return d

    def marcar(self, **etapas: bool) -> None:
        d = self.estado()
        d["etapas"].update(etapas)
        self.estado_path.write_text(json.dumps(d, indent=2), encoding="utf-8")
