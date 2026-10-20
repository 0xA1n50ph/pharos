"""Cliente mínimo do Ollama (só biblioteca padrão)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class OllamaErro(RuntimeError):
    pass


class Ollama:
    def __init__(self, url: str = "http://localhost:11434", timeout: int = 900):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _post(self, caminho: str, corpo: dict, timeout: int | None = None) -> dict:
        req = urllib.request.Request(
            self.url + caminho,
            data=json.dumps(corpo).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detalhe = e.read().decode("utf-8", errors="replace")[:300]
            raise OllamaErro(f"HTTP {e.code}: {detalhe}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise OllamaErro(f"Falha ao falar com o Ollama em {self.url}: {e}") from e

    def modelos(self) -> list[str]:
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=10) as r:
                return [m["name"] for m in json.load(r).get("models", [])]
        except Exception as e:  # noqa: BLE001
            raise OllamaErro(f"Não consegui falar com o Ollama em {self.url}: {e}") from e

    def tem_modelo(self, nome: str) -> bool:
        base = nome if ":" in nome else nome + ":latest"
        instalados = self.modelos()
        return nome in instalados or base in instalados

    def chat(
        self,
        modelo: str,
        mensagens: list[dict],
        temperatura: float = 0.2,
        num_ctx: int = 8192,
        keep_alive: str | int = "30m",
        tentativas: int = 2,
    ) -> str:
        corpo = {
            "model": modelo,
            "messages": mensagens,
            "stream": False,
            "keep_alive": keep_alive,
            "options": {"temperature": temperatura, "num_ctx": num_ctx},
        }
        ultimo: OllamaErro | None = None
        for t in range(tentativas):
            try:
                resposta = self._post("/api/chat", corpo)
                return resposta["message"]["content"]
            except OllamaErro as e:
                ultimo = e
                time.sleep(2 * (t + 1))
        assert ultimo is not None
        raise ultimo

    def descarregar(self, modelo: str) -> None:
        """Libera o modelo da memória (útil antes de OCR/Whisper no mesmo Mac)."""
        try:
            self._post("/api/generate", {"model": modelo, "keep_alive": 0}, timeout=30)
        except OllamaErro:
            pass
