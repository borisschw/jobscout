"""Local report viewer HTTP server."""

from __future__ import annotations

import json
import socket
import sys
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

from jobscout.config import load_config
from jobscout.models import VerdictLabel
from jobscout.report.writer import write_reports
from jobscout.schedule import DailyScheduler, next_check_label
from jobscout.store import Store

if TYPE_CHECKING:
    from jobscout.config import AppConfig

MAX_BODY_SIZE = 64 * 1024


class ViewerHandler(BaseHTTPRequestHandler):
    config: AppConfig
    store: Store
    company_filter: list[str] | None = None
    scheduler: DailyScheduler | None = None

    def log_message(self, format: str, *args) -> None:
        pass

    def handle(self) -> None:
        """Ignore a browser that closes the socket before the response is done.

        A second PC often opens a connection and then drops it (a cancelled
        load, a favicon, or the tab reloading). That is a hang-up, not a
        failed report.
        """
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return

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

        if self.path == "/api/status":
            self._send_json(200, _status_payload(self.scheduler))
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
                schedule_enabled=self.scheduler is not None,
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


def _status_payload(scheduler: DailyScheduler | None) -> dict:
    if scheduler is None:
        return {
            "enabled": False,
            "running": False,
            "at": None,
            "last_started_at": None,
            "last_finished_at": None,
            "next_run_at": None,
            "next_label": "",
            "error": None,
        }
    return scheduler.status()


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    target = "127.0.0.1" if host in ("0.0.0.0", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((target, port)) == 0


def _lan_ips() -> list[str]:
    """IPv4 addresses of this machine other than loopback."""
    found: list[str] = []
    seen: set[str] = set()

    def add(ip: str) -> None:
        if not ip or ip.startswith("127.") or ip in seen:
            return
        seen.add(ip)
        found.append(ip)

    try:
        # Picks the address used for the default route. UDP connect sends nothing.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            add(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except socket.gaierror:
        pass
    return found


def viewer_urls(host: str, port: int) -> list[str]:
    """Browser addresses for this listener.

    ``0.0.0.0`` is not a page you can open. Callers get localhost, then each
    LAN address so another PC on the same network can use it.
    """
    if host in ("0.0.0.0", ""):
        return [f"http://127.0.0.1:{port}/", *(f"http://{ip}:{port}/" for ip in _lan_ips())]
    return [f"http://{host}:{port}/"]


def start_server(
    config: AppConfig,
    store: Store,
    port: int = 8765,
    *,
    host: str = "0.0.0.0",
    open_browser: bool = False,
    company_filter: list[str] | None = None,
    schedule: bool | None = None,
) -> None:
    if port_in_use(port, host):
        print(f"A viewer is already running on port {port}")
        sys.exit(0)

    enable_schedule = config.settings.schedule.enabled if schedule is None else schedule
    scheduler = DailyScheduler(config, store) if enable_schedule else None

    ViewerHandler.config = config
    ViewerHandler.store = store
    ViewerHandler.company_filter = company_filter
    ViewerHandler.scheduler = scheduler
    server = ThreadingHTTPServer((host, port), ViewerHandler)
    urls = viewer_urls(host, port)
    print(f"Serving report viewer at {urls[0]}", flush=True)
    for extra in urls[1:]:
        print(f"On this network: {extra}", flush=True)
    if scheduler is not None:
        at = config.settings.schedule.at
        if scheduler.due():
            print(f"The {at} check has not run yet. Starting it now.", flush=True)
        else:
            print(next_check_label(datetime.now().astimezone(), at), flush=True)
        scheduler.start()
    if open_browser:
        webbrowser.open(urls[0])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if scheduler is not None:
            scheduler.stop()
        server.server_close()


def main(
    project_root: Path | None = None,
    port: int = 8765,
    open_browser: bool = False,
    schedule: bool | None = None,
    host: str = "0.0.0.0",
) -> None:
    config = load_config(project_root)
    store = Store(config.db_path)
    try:
        start_server(
            config,
            store,
            port,
            host=host,
            open_browser=open_browser,
            schedule=schedule,
        )
    finally:
        store.close()
