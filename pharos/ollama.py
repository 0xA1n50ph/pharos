"""Minimal Ollama client (standard library only)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class OllamaError(RuntimeError):
    pass


class Ollama:
    def __init__(self, url: str = "http://localhost:11434", timeout: int = 900):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, body: dict, timeout: int | None = None) -> dict:
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise OllamaError(f"HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise OllamaError(f"Failed to reach Ollama at {self.url}: {e}") from e

    def models(self) -> list[str]:
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=10) as r:
                return [m["name"] for m in json.load(r).get("models", [])]
        except Exception as e:  # noqa: BLE001
            raise OllamaError(f"Could not reach Ollama at {self.url}: {e}") from e

    def has_model(self, name: str) -> bool:
        base = name if ":" in name else name + ":latest"
        installed = self.models()
        return name in installed or base in installed

    def chat(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.2,
        num_ctx: int = 8192,
        keep_alive: str | int = "30m",
        attempts: int = 2,
    ) -> str:
        body = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": keep_alive,
            "options": {"temperature": temperature, "num_ctx": num_ctx},
        }
        last: OllamaError | None = None
        for t in range(attempts):
            try:
                response = self._post("/api/chat", body)
                return response["message"]["content"]
            except OllamaError as e:
                last = e
                time.sleep(2 * (t + 1))
        assert last is not None
        raise last

    def unload(self, model: str) -> None:
        """Frees the model from memory (useful before OCR/Whisper on the same Mac)."""
        try:
            self._post("/api/generate", {"model": model, "keep_alive": 0}, timeout=30)
        except OllamaError:
            pass
