"""Server that imitates the Ollama API (/api/tags, /api/chat, /api/generate) to test without a model.

It generates Portuguese "translations" matching the size of the original, applies the glossary terms that appear in
the prompt, and lets you inject failures to test validation, retries and the report.

Environment variables:
  FAKE_LOG              .jsonl file where each request is recorded
  FAKE_DELAY            seconds to wait per request (to test Ctrl+C and resume)
  FAKE_FAIL_ONCE        if the text contains this, the 1st attempt returns the original (and the 2nd gets it right)
  FAKE_FAIL_ALWAYS      if the text contains this, every attempt returns something too short
  FAKE_OCR_SWAPS        "wrong=right;wrong2=right2": the "OCR proofreader" applies these swaps to the received text
  FAKE_OCR_SWAPS_FILE   a JSON file {"wrong": "right"} read on each request (swaps whole words)
  FAKE_OCR_MODE         "" (applies the swaps) | translate | preamble | fence | error | slow
  FAKE_OCR_DELAY        seconds to wait per request in "slow" mode (default 1.5)
  FAKE_MODELS           "installed" models, comma-separated (default: translategemma:12b,gemma3:12b; empty = none)
  FAKE_STRICT           if "1", /api/chat and /api/generate answer 404 for a model that is not installed (like the real Ollama)
  FAKE_PULL_MODE        "" (downloads and installs) | error (returns {"error": ...}) | drop1 (the 1st attempt for each model cuts the
                        connection mid-way; the 2nd works) | drop (always cuts) | slow (0.3 s per chunk)
  FAKE_BIND             address to listen on (default 127.0.0.1)

/api/pull imitates Ollama: returns JSON lines ("pulling manifest", "pulling <digest>" with total/completed, ... "success").
Models whose name contains "doesnotexist" respond with the manifest error that Ollama returns.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

SENTENCES = [
    "O vento chegou de manhã cedo e trouxe consigo o cheiro do sal e da madeira úmida, e ele não disse nada.",
    "Ela sabia que a luz era para os outros, mas não podia deixar de pensar nas coisas que não via.",
    "Depois de uma hora de silêncio, o velho voltou para a casa e fechou a porta com cuidado.",
    "Havia no mar uma calma que não era natural, como se ele esperasse por alguma coisa que só ele conhecia.",
]
seen: dict[str, int] = {}
_env_models = os.environ.get("FAKE_MODELS")
MODELS: list[str] = [m for m in (_env_models if _env_models is not None else "translategemma:12b,gemma3:12b").split(",") if m]
PULLS: dict[str, int] = {}


def _record(obj: dict) -> None:
    log = os.environ.get("FAKE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _installed(name: str) -> bool:
    return name in MODELS or (":" not in name and name + ":latest" in MODELS)


def _extract_text(messages: list[dict]) -> str:
    last = messages[-1]["content"]
    if "Please translate the following" in last:
        return last.split(":\n\n\n", 1)[-1]
    if "Text to translate:\n\n" in last:
        return last.split("Text to translate:\n\n", 1)[-1]
    return last


def _glossary_terms(messages: list[dict]) -> list[str]:
    everything = "\n".join(m["content"] for m in messages)
    terms = []
    if "Mandatory glossary" in everything:
        block = everything.split("Mandatory glossary", 1)[1]
        for line in block.splitlines()[1:]:
            m = re.match(r"- (.+?) -> (.+?)( \(.*\))?$", line)
            if not m:
                break
            terms.append(m.group(2))
    return terms


def _review_ocr(messages: list[dict]) -> str:
    """Imitates a model that fixes OCR: returns the text between <text>...</text> with the FAKE_OCR_SWAPS swaps."""
    user = messages[-1]["content"]
    m = re.search(r"<text>\n(.*)\n</text>", user, re.S)
    text = m.group(1) if m else user
    mode = os.environ.get("FAKE_OCR_MODE", "")
    if mode == "translate":
        return " ".join(["O vento chegou de manhã cedo e trouxe consigo o cheiro do sal."] * max(1, len(text) // 70))
    for pair in filter(None, os.environ.get("FAKE_OCR_SWAPS", "").split(";")):
        wrong, _, right = pair.partition("=")
        text = text.replace(wrong, right)
    file = os.environ.get("FAKE_OCR_SWAPS_FILE")
    if file and os.path.exists(file):
        with open(file, encoding="utf-8") as f:
            for wrong, right in json.load(f).items():
                text = re.sub(r"(?<!\w)" + re.escape(wrong) + r"(?!\w)", right, text)
    if mode == "preamble":
        return "Here is the corrected text:\n" + text
    if mode == "fence":
        return "```\n" + text + "\n```"
    return text


def _translate(messages: list[dict]) -> str:
    text = _extract_text(messages)
    everything = "\n".join(m["content"] for m in messages)
    key = text.strip()
    seen[key] = seen.get(key, 0) + 1

    always = os.environ.get("FAKE_FAIL_ALWAYS")
    once = os.environ.get("FAKE_FAIL_ONCE")
    if always and always in text:
        return "Curto."
    if once and once in text and seen[key] == 1:
        return text  # returns it untranslated

    lines = [l for l in text.split("\n") if l.strip()]
    if "keep exactly" in everything and len(lines) > 1:  # verse
        return "\n".join(f"Verso traduzido número {i + 1} do poema." for i in range(len(lines)))
    if len(text) < 60:
        return "Título traduzido" if text.strip() else text
    target = int(len(text) * 1.05)
    out, i = "", 0
    while len(out) < target:
        out += SENTENCES[i % len(SENTENCES)] + " "
        i += 1
    out = out.strip()
    extras = _glossary_terms(messages)
    if extras:
        out += " (" + ", ".join(extras) + ")"
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silent
        pass

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            return self._json({"models": [{"name": m, "model": m, "size": 8100000000} for m in MODELS]})
        self._json({}, 404)

    def _pull(self, model: str) -> None:
        PULLS[model] = PULLS.get(model, 0) + 1
        _record({"pull": model, "attempt": PULLS[model]})
        mode = os.environ.get("FAKE_PULL_MODE", "")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()

        def line(d):
            self.wfile.write((json.dumps(d) + "\n").encode())
            self.wfile.flush()
        line({"status": "pulling manifest"})
        if "doesnotexist" in model:
            return line({"error": "pull model manifest: file does not exist"})
        if mode == "error":
            return line({"error": "simulated download failure"})
        total = 8100000000
        for layer, size in (("sha256:aaaaaaaaaaaa", total), ("sha256:bbbbbbbbbbbb", 1200)):
            for i in range(0, 11):
                line({"status": "pulling " + layer[:19], "digest": layer, "total": size, "completed": size * i // 10})
                if mode == "slow":
                    time.sleep(0.3)
                if (mode == "drop" or (mode == "drop1" and PULLS[model] == 1)) and layer.startswith("sha256:a") and i == 4:
                    self.wfile.flush()
                    self.connection.close()   # cuts mid-way, with no "success"
                    return
        line({"status": "verifying sha256 digest"})
        line({"status": "writing manifest"})
        line({"status": "success"})
        if model not in MODELS:
            MODELS.append(model)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/pull":
            return self._pull(body.get("model") or body.get("name") or "")
        strict = os.environ.get("FAKE_STRICT") == "1"
        if strict and self.path in ("/api/chat", "/api/generate") and not _installed(body.get("model", "")):
            return self._json({"error": "model '%s' not found" % body.get("model", "")}, 404)
        if self.path == "/api/generate":
            return self._json({"done": True})
        if self.path == "/api/chat":
            delay = float(os.environ.get("FAKE_DELAY", "0"))
            if delay:
                time.sleep(delay)
            msgs = body["messages"]
            log = os.environ.get("FAKE_LOG")
            if log:
                with open(log, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"model": body.get("model"), "messages": msgs}, ensure_ascii=False) + "\n")
            last = msgs[-1]["content"]
            if "meticulous proofreader of OCR" in msgs[0]["content"]:
                if os.environ.get("FAKE_OCR_MODE") == "error":
                    return self._json({"error": "simulated failure"}, 500)
                if os.environ.get("FAKE_OCR_MODE") == "slow":
                    time.sleep(float(os.environ.get("FAKE_OCR_DELAY", "1.5")))
                response = _review_ocr(msgs)
            elif "Answer with ONLY a JSON object" in last:  # profile suggestion
                response = json.dumps({"genre": "fiction", "work": "Test novella about a lighthouse",
                                       "register": "Sober literary prose with short, rhythmic sentences."})
            else:
                response = _translate(msgs)
            return self._json({"message": {"role": "assistant", "content": response}, "done": True})
        self._json({}, 404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 11999
    from http.server import ThreadingHTTPServer
    ThreadingHTTPServer((os.environ.get("FAKE_BIND", "127.0.0.1"), port), Handler).serve_forever()
