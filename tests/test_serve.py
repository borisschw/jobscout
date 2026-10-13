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
    server = ThreadingHTTPServer(("127.0.0.1", 0), ViewerHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


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
