"""Local server for the OCR review screen.

It listens only on 127.0.0.1, requires a random token (in the URL and in the X-Token header) and checks the Host
header, so that another site open in the browser cannot read or change the project.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..ollama import OllamaError
from .engine import image_path
from .review import Session
from .ai_suggestions import AIError

_HTML = Path(__file__).with_name("review.html")
_MAX_BODY = 8 * 1024 * 1024


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, session: Session, token: str | None = None, port: int = 0):
        super().__init__(("127.0.0.1", port), _Handler)
        self.session = session
        self.token = token or secrets.token_urlsafe(16)
        self.finished = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/?t={self.token}"


class _Handler(BaseHTTPRequestHandler):
    server_version = "OCRReview/1"

    def log_message(self, *args) -> None:  # silent
        pass

    # -- utilities -------------------------------------------------------------------------------
    def _authorized(self, qs: dict) -> bool:
        host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost"):
            return False
        token = self.headers.get("X-Token") or (qs.get("t") or [""])[0]
        return secrets.compare_digest(token, self.server.token)  # type: ignore[attr-defined]

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    # -- GET -------------------------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/favicon.ico":  # the browser asks without a token; there is nothing to protect here
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if not self._authorized(qs):
            return self._json({"error": "not authorized"}, 403)
        session: Session = self.server.session  # type: ignore[attr-defined]
        route = u.path
        if route == "/":
            try:
                return self._send(200, _HTML.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                return self._json({"error": "review.html not found"}, 500)
        if route == "/api/index":
            return self._json(session.index())
        if route == "/api/suggestions":
            return self._json(session.suggestions)
        if route == "/api/ai":
            return self._json(session.ai_state())
        m = re.fullmatch(r"/api/ai/page/(\d+)", route)
        if m:
            n = int(m.group(1))
            if n not in session.pages:
                return self._json({"error": "page does not exist"}, 404)
            return self._json({"suggestions": session.ai_for_page(n)})
        m = re.fullmatch(r"/api/page/(\d+)", route)
        if m:
            data = session.page(int(m.group(1)))
            return self._json(data) if data else self._json({"error": "page does not exist"}, 404)
        m = re.fullmatch(r"/img/(\d+)", route)
        if m:
            file = image_path(session.dir_img, int(m.group(1)))
            if file.exists():
                return self._send(200, file.read_bytes(), "image/jpeg", "private, max-age=600")
        return self._json({"error": "not found"}, 404)

    # -- POST ------------------------------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._authorized(qs):
            return self._json({"error": "not authorized"}, 403)
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY:
            return self._json({"error": "body too large"}, 413)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json({"error": "invalid JSON"}, 400)
        session: Session = self.server.session  # type: ignore[attr-defined]
        route = u.path

        if route.startswith("/api/ai/"):
            return self._post_ai(session, route, body)

        m = re.fullmatch(r"/api/page/(\d+)", route)
        if m:
            n = int(m.group(1))
            if n not in session.pages:
                return self._json({"error": "page does not exist"}, 404)
            blocks = body.get("blocks")
            if not isinstance(blocks, list):
                return self._json({"error": "blocks missing"}, 400)
            rev = body.get("reviewed")
            session.save(n, blocks, None if rev is None else bool(rev))
            item = next((p for p in session.index()["pages"] if p["n"] == n), None)
            return self._json({"ok": True, "index": item})
        m = re.fullmatch(r"/api/mark/(\d+)", route)
        if m:
            session.mark(int(m.group(1)), bool(body.get("reviewed", True)))
            return self._json({"ok": True})
        if route == "/api/replace":
            r = session.replace(str(body.get("from", "")), str(body.get("to", "")),
                                bool(body.get("whole_word", True)), bool(body.get("ignore_case", False)),
                                bool(body.get("apply", False)))
            return self._json(r)
        if route == "/api/finish":
            self.server.finished = True  # type: ignore[attr-defined]
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return None
        return self._json({"error": "not found"}, 404)

    def _post_ai(self, session: Session, route: str, body: dict) -> None:
        """AI suggestions. Errors become messages for the person (502 = Ollama could not cope)."""
        try:
            m = re.fullmatch(r"/api/ai/page/(\d+)", route)
            if m:
                return self._json(session.ai_generate_page(int(m.group(1))))
            if route == "/api/ai/batch":
                return self._json(session.ai_start_batch(str(body.get("scope", "flagged"))))
            if route == "/api/ai/stop":
                session.ai_stop()
                return self._json({"ok": True})
            if route in ("/api/ai/accept", "/api/ai/reject"):
                ids = body.get("ids")
                if not isinstance(ids, list):
                    return self._json({"error": "ids missing"}, 400)
                session.ai_mark(ids, route.endswith("accept"))
                return self._json({"ok": True})
        except OllamaError as e:
            return self._json({"error": str(e)}, 502)
        except AIError as e:
            return self._json({"error": str(e)}, 409)
        return self._json({"error": "not found"}, 404)


def start_in_thread(session: Session, port: int = 0) -> ReviewServer:
    """Used in the tests: brings the server up in the background."""
    srv = ReviewServer(session, port=port)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    return srv


def run(session: Session, open_browser: bool = True, output=print, ai_batch: str | None = None) -> bool:
    """Blocks until the person clicks 'Finish review'. Returns False if interrupted with Ctrl+C.

    `ai_batch` ('flagged' | 'all') already sets the model suggesting fixes in the background."""
    srv = ReviewServer(session)
    output(f"Review screen: {srv.url}")
    if ai_batch:
        try:
            r = session.ai_start_batch(ai_batch)
            output(f"AI: suggesting fixes on {r.get('total', 0)} page(s) in the background "
                   f"(model {session.ai.effective_model()[0]}); they appear on the screen as they are ready."
                   if r.get("total") else "AI: no pending page to suggest for.")
        except (AIError, OllamaError) as e:
            output(f"AI: could not start ({e}). You can try again with the button on the screen.")
    if open_browser:
        try:
            webbrowser.open(srv.url)
        except Exception:  # noqa: BLE001
            pass
    output("Fix what you need and click 'Finish review' in the browser (Ctrl+C here pauses; what was saved stays).")
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return srv.finished
