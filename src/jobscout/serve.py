"""Local report viewer HTTP server."""

from __future__ import annotations

import json
import socket
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

from jobscout.config import load_config
from jobscout.matching.prompts import prompt_fingerprint
from jobscout.models import VerdictLabel
from jobscout.report.writer import write_reports
from jobscout.store import Store

if TYPE_CHECKING:
    from jobscout.config import AppConfig

MAX_BODY_SIZE = 64 * 1024


class ViewerHandler(BaseHTTPRequestHandler):
    config: AppConfig
    store: Store
    company_filter: list[str] | None = None

    def log_message(self, format: str, *args) -> None:
        pass

    def _send_json(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(200, {"ok": True})
            return

        if self.path in ("/", "/index.html"):
            threshold = self.config.preferences.score_threshold
            allowed = self.config.visible_company_names(self.company_filter)
            matches = self.store.scored_jobs(min_score=threshold)
            matches = [
                m
                for m in matches
                if m.job.company.lower() in allowed
                and m.verdict.verdict in (VerdictLabel.STRONG, VerdictLabel.POSSIBLE)
            ]
            failures: list[str] = []
            counts = self.store.status_counts()
            html_path, _ = write_reports(
                self.config,
                matches,
                failures,
                interactive=True,
                pending_scoring=counts.get("pending_scoring", 0),
                run=self.store.latest_run(),
            )
            content = html_path.read_text(encoding="utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content.encode())))
            self.end_headers()
            self.wfile.write(content.encode())
            return

        self.send_error(404)

    def _posted_keys(self) -> list[str] | None:
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY_SIZE:
            self.send_error(413)
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "Invalid JSON"})
            return None
        keys = data.get("keys", [])
        if not isinstance(keys, list):
            self._send_json(400, {"ok": False, "error": "Invalid JSON"})
            return None
        return keys

    def _mark_keys(self, keys: list[str], action) -> None:
        found, unmatched = self.store.resolve_job_keys(keys)
        if unmatched:
            self._send_json(422, {"ok": False, "changed": 0, "unmatched": unmatched})
            return
        changed = action(found)
        self.store._conn.commit()
        self._send_json(200, {"ok": True, "changed": changed, "unmatched": []})

    def do_POST(self) -> None:
        actions = {
            "/api/ignore": self.store.ignore,
            "/api/unignore": self.store.unignore,
            "/api/apply": self.store.mark_applied,
            "/api/unapply": self.store.unapply,
        }
        action = actions.get(self.path)
        if action is None:
            self.send_error(404)
            return
        keys = self._posted_keys()
        if keys is None:
            return
        self._mark_keys(keys, action)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_server(
    config: AppConfig,
    store: Store,
    port: int = 8765,
    *,
    open_browser: bool = False,
    company_filter: list[str] | None = None,
) -> None:
    if port_in_use(port):
        print(f"A viewer is already running on port {port}")
        sys.exit(0)

    ViewerHandler.config = config
    ViewerHandler.store = store
    ViewerHandler.company_filter = company_filter
    server = ThreadingHTTPServer(("127.0.0.1", port), ViewerHandler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Serving report viewer at {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(project_root: Path | None = None, port: int = 8765, open_browser: bool = False) -> None:
    config = load_config(project_root)
    store = Store(config.db_path)
    try:
        start_server(config, store, port, open_browser=open_browser)
    finally:
        store.close()
