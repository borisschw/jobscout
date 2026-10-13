"""Tests for the Cursor CLI provider."""

import asyncio
import os
import sys
from pathlib import Path

from jobscout.matching.providers.cursor import (
    _WinJob,
    _cleanup_proc,
    _remove_workdir,
    agent_cli_args,
    resolve_cursor_argv,
    windows_node_argv,
)


def test_agent_cli_args_keeps_output_format_as_one_token():
    args = agent_cli_args("composer-2.5", "score this job")
    assert "--output-format=json" in args
    assert "json" not in args
    assert "--output-format" not in args
    assert args[-1] == "score this job"


def test_windows_node_argv_picks_latest_version(tmp_path: Path):
    versions = tmp_path / "versions"
    old = versions / "2026.09.15-d2fe57e"
    new = versions / "2026.09.28-64d2043"
    for folder in (old, new):
        folder.mkdir(parents=True)
        (folder / "node.exe").write_bytes(b"")
        (folder / "index.js").write_text("/* fake */", encoding="utf-8")
    argv = windows_node_argv(tmp_path)
    assert argv == [str(new / "node.exe"), str(new / "index.js")]


def test_resolve_cursor_argv_uses_windows_node_install(tmp_path, monkeypatch):
    monkeypatch.setattr("jobscout.matching.providers.cursor.os.name", "nt")
    agent_dir = tmp_path / "cursor-agent"
    monkeypatch.setattr(
        "jobscout.matching.providers.cursor._install_roots",
        lambda: [agent_dir],
    )
    monkeypatch.setattr("jobscout.matching.providers.cursor.shutil.which", lambda *a, **k: None)

    version = agent_dir / "versions" / "2026.09.28-64d2043"
    version.mkdir(parents=True)
    node = version / "node.exe"
    index = version / "index.js"
    node.write_bytes(b"")
    index.write_text("/* fake */", encoding="utf-8")
    (agent_dir / "cursor-agent.ps1").write_text("# wrapper", encoding="utf-8")

    argv = resolve_cursor_argv("cursor-agent")
    assert argv == [str(node), str(index)]
    assert "powershell.exe" not in argv[0]


def test_resolve_cursor_argv_uses_windows_ps1_install(tmp_path, monkeypatch):
    monkeypatch.setattr("jobscout.matching.providers.cursor.os.name", "nt")
    agent_dir = tmp_path / "cursor-agent"
    monkeypatch.setattr(
        "jobscout.matching.providers.cursor._install_roots",
        lambda: [agent_dir],
    )
    monkeypatch.setattr(
        "jobscout.matching.providers.cursor._powershell_exe",
        lambda: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    )
    monkeypatch.setattr("jobscout.matching.providers.cursor.shutil.which", lambda *a, **k: None)

    agent_dir.mkdir()
    ps1 = agent_dir / "cursor-agent.ps1"
    ps1.write_text("# fake", encoding="utf-8")
    (agent_dir / "cursor-agent.cmd").write_text("@echo off", encoding="utf-8")

    argv = resolve_cursor_argv("cursor-agent")
    assert argv[0].endswith("powershell.exe")
    assert argv[-1] == str(ps1)
    assert "--print" not in argv


async def test_remove_workdir_deletes_directory(tmp_path: Path):
    target = tmp_path / "agent-cwd"
    target.mkdir()
    (target / "note.txt").write_text("x", encoding="utf-8")
    await _remove_workdir(str(target))
    assert not target.exists()


async def test_remove_workdir_ignores_locked_directory(tmp_path: Path, monkeypatch):
    target = tmp_path / "locked"
    target.mkdir()

    def _fail(path):
        raise PermissionError(32, "The process cannot access the file", str(path))

    monkeypatch.setattr("jobscout.matching.providers.cursor.shutil.rmtree", _fail)
    await _remove_workdir(str(target))
    assert target.exists()


async def test_win_job_kills_assigned_process():
    if os.name != "nt":
        return
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    job = _WinJob()
    try:
        job.assign(proc.pid)
        job.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise AssertionError("job object did not kill the process")
    finally:
        job.close()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_cleanup_proc_waits_for_running_process():
    proc = await asyncio.create_subprocess_exec(
        "python",
        "-c",
        "import time; time.sleep(30)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await _cleanup_proc(proc)
    assert proc.returncode is not None


def test_resolve_cursor_argv_prefers_which_on_posix(monkeypatch, tmp_path):
    monkeypatch.setattr("jobscout.matching.providers.cursor.os.name", "posix")
    fake = tmp_path / "cursor-agent"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "jobscout.matching.providers.cursor.shutil.which",
        lambda name, **k: str(fake) if name == "cursor-agent" else None,
    )
    argv = resolve_cursor_argv("cursor-agent")
    assert argv == [str(fake)]
