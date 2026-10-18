"""Tests for serve.py."""

import json
import re
import threading
import urllib.request

from jobscout.config import load_config
from jobscout.serve import ViewerHandler
from jobscout.store import Store
from http.server import ThreadingHTTPServer


def _start_server(config, store):
    ViewerHandler.config = config
    ViewerHandler.store = store
    ViewerHandler.company_filter = None
    ViewerHandler.scheduler = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), ViewerHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def test_dropped_client_does_not_print_a_traceback(app_config, project, capsys):
    import socket
    import struct
    import time

    config = load_config(project)
    store = Store(config.db_path)
    server, port = _start_server(config, store)
    try:
        sock = socket.create_connection(("127.0.0.1", port))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()
        time.sleep(0.3)
        health = urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz")
        assert json.loads(health.read())["ok"] is True
        captured = capsys.readouterr()
        assert "Exception occurred" not in captured.err
        assert "Connection reset by peer" not in captured.err
    finally:
        server.shutdown()
        store.close()


def test_viewer_urls_for_lan_and_localhost(monkeypatch):
    from jobscout.serve import viewer_urls

    monkeypatch.setattr("jobscout.serve._lan_ips", lambda: ["192.168.1.20", "10.0.0.5"])
    assert viewer_urls("0.0.0.0", 8765) == [
        "http://127.0.0.1:8765/",
        "http://192.168.1.20:8765/",
        "http://10.0.0.5:8765/",
    ]
    assert viewer_urls("127.0.0.1", 8765) == ["http://127.0.0.1:8765/"]


def test_healthz_endpoint(app_config, project):
    config = load_config(project)
    store = Store(config.db_path)
    server, port = _start_server(config, store)
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz")
        data = json.loads(resp.read())
        assert data["ok"] is True
    finally:
        server.shutdown()
        store.close()


def test_ignore_api(app_config, project, make_job):
    config = load_config(project)
    store = Store(config.db_path)
    job = make_job()
    store.upsert_job(job)
    store._conn.commit()
    server, port = _start_server(config, store)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/ignore",
            data=json.dumps({"keys": [job.job_key]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req)
        data = json.loads(resp.read())
        assert data["ok"] is True
        assert data["changed"] == 1
        assert store.is_ignored(job.job_key)
    finally:
        server.shutdown()
        store.close()


def test_apply_api(app_config, project, make_job):
    config = load_config(project)
    store = Store(config.db_path)
    job = make_job()
    store.upsert_job(job)
    store.ignore([job.job_key])
    store._conn.commit()
    server, port = _start_server(config, store)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/apply",
            data=json.dumps({"keys": [job.job_key]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req)
        data = json.loads(resp.read())
        assert data["ok"] is True
        assert data["changed"] == 1
        assert store.is_applied(job.job_key)
        assert not store.is_ignored(job.job_key)
    finally:
        server.shutdown()
        store.close()


def test_page_shows_run_summary_and_new_jobs(app_config, project, make_job):
    from jobscout.models import Verdict, VerdictLabel

    config = load_config(project)
    store = Store(config.db_path)
    new_job = make_job(title="New Role")
    old_job = make_job(external_id="old", title="Old Role", url="https://example.com/jobs/old")
    store.upsert_job(new_job)
    store.upsert_job(old_job)
    seen = store._conn.execute(
        "SELECT first_seen FROM jobs WHERE job_key = ?",
        (new_job.job_key,),
    ).fetchone()["first_seen"]
    store._conn.execute(
        "UPDATE jobs SET first_seen = ? WHERE job_key = ?",
        ("2020-01-01T00:00:00Z", old_job.job_key),
    )
    verdict = Verdict(score=85, verdict=VerdictLabel.STRONG, why="Fit", model="m", prompt_version="1")
    store.save_verdict(new_job, verdict)
    store.save_verdict(old_job, verdict)
    store.save_run(
        started_at=seen,
        finished_at=seen,
        fetched=4,
        new=1,
        changed=0,
        closed=0,
        scored=1,
        llm_calls=1,
        removed=0,
        llm_errors=0,
    )
    store._conn.commit()
    server, port = _start_server(config, store)
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/")
        html = resp.read().decode()
        assert "Run summary" in html.split("<main>", 1)[0]
        assert "<dt>Fetched</dt><dd>4</dd>" in html
        assert 'class="card new"' in html
        old_cards = re.findall(
            r'<div class="(card[^"]*)" data-key=',
            html.split("Old Role", 1)[0],
        )
        assert old_cards[-1] == "card"
    finally:
        server.shutdown()
        store.close()


def test_schedule_banner_and_status(app_config, project):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from jobscout.schedule import DailyScheduler

    config = load_config(project)
    config.settings.schedule.enabled = True
    config.settings.schedule.at = "23:00"
    store = Store(config.db_path)
    morning = datetime(2026, 1, 15, 9, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
    scheduler = DailyScheduler(
        config,
        store,
        clock=lambda: morning,
        runner=lambda _config, _store: None,
    )
    server, port = _start_server(config, store)
    ViewerHandler.scheduler = scheduler
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert 'id="schedule-status"' in html
        assert "23:00" in html
        assert "pollSchedule" in html
        status = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status").read())
        assert status["enabled"] is True
        assert status["running"] is False
        assert status["at"] == "23:00"
        assert status["next_label"] == "Next check today at 23:00"
    finally:
        server.shutdown()
        store.close()


def test_status_is_off_when_the_viewer_has_no_scheduler(app_config, project):
    config = load_config(project)
    store = Store(config.db_path)
    server, port = _start_server(config, store)
    try:
        status = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status").read())
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert status["enabled"] is False
        assert 'id="schedule-status"' not in html
    finally:
        server.shutdown()
        store.close()


def test_ignore_api_returns_422_for_unknown_key(app_config, project):
    config = load_config(project)
    store = Store(config.db_path)
    server, port = _start_server(config, store)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/ignore",
            data=json.dumps({"keys": ["nonexistent"]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
            assert False, "Should have raised"
        except urllib.error.HTTPError as e:
            assert e.code == 422
    finally:
        server.shutdown()
        store.close()
