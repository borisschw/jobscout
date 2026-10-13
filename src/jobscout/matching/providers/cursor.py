"""Cursor CLI LLM provider."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from jobscout.config import LLMConfig
from jobscout.matching.providers.base import LLMError, LLMProvider

_WIN_PATHEXT = ".COM;.EXE;.BAT;.CMD;.PS1"
_VERSION_DIR_RE = re.compile(
    r"^\d{4}\.\d{1,2}\.\d{1,2}(?:-\d{2}-\d{2}-\d{2})?-[a-f0-9]+$"
)


def _install_roots() -> list[Path]:
    roots: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "cursor-agent")
    home = Path.home()
    roots.append(home / "AppData" / "Local" / "cursor-agent")
    roots.append(home / ".local" / "bin")
    seen: set[Path] = set()
    unique: list[Path] = []
    for root in roots:
        resolved = root.resolve() if root.exists() else root
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(root)
    return unique


def _powershell_exe() -> str:
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.is_file():
        return str(candidate)
    return shutil.which("powershell.exe") or "powershell.exe"


def _launch_via_ps1(ps1: Path) -> list[str]:
    return [
        _powershell_exe(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ps1),
    ]


def _version_sort_key(name: str) -> int:
    date_part = name.split("-")[0]
    year, month, day = date_part.split(".")
    return int(f"{year}{month.zfill(2)}{day.zfill(2)}")


def _node_plus_index(directory: Path) -> list[str] | None:
    node = directory / "node.exe"
    index = directory / "index.js"
    if node.is_file() and index.is_file():
        return [str(node), str(index)]
    return None


def windows_node_argv(root: Path) -> list[str] | None:
    """Resolve node.exe + index.js so argv is not rewritten by PowerShell.

    Windows PowerShell 5.1 turns ``--output-format json`` into ``-On`` when the
    official ``cursor-agent.ps1`` wrapper forwards ``$args`` to node.
    """
    direct = _node_plus_index(root)
    if direct:
        return direct
    versions = root / "versions"
    if not versions.is_dir():
        return None
    candidates = [
        path
        for path in versions.iterdir()
        if path.is_dir() and _VERSION_DIR_RE.match(path.name) and _node_plus_index(path)
    ]
    if candidates:
        latest = max(candidates, key=lambda path: _version_sort_key(path.name))
        return _node_plus_index(latest)
    return _node_plus_index(versions / "dist-package")


def agent_cli_args(model: str, prompt: str) -> list[str]:
    # Equals-form flags stay one argv token. Split ``--output-format json`` is
    # rewritten to ``-On`` by Windows PowerShell 5.1.
    return [
        "--print",
        "--output-format=json",
        f"--model={model}",
        "--mode=ask",
        "--trust",
        prompt,
    ]


def _which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    extra = [str(p) for p in _install_roots() if p.is_dir()]
    search = os.pathsep.join([*extra, os.environ.get("PATH", "")])
    if os.name == "nt":
        return shutil.which(name, path=search, pathext=_WIN_PATHEXT)
    return shutil.which(name, path=search)


def _ps1_for(path: Path) -> Path | None:
    if path.suffix.lower() == ".ps1" and path.is_file():
        return path
    sibling = path.with_name("cursor-agent.ps1")
    if sibling.is_file():
        return sibling
    alt = path.with_suffix(".ps1")
    if alt.is_file():
        return alt
    return None


def resolve_cursor_argv(command: str) -> list[str]:
    requested = command.split() or ["cursor-agent"]
    names = list(dict.fromkeys([requested[0], "cursor-agent", "agent"]))

    if os.name == "nt":
        for root in _install_roots():
            node_argv = windows_node_argv(root)
            if node_argv:
                extra = requested[1:]
                return [*node_argv, *extra]
        for root in _install_roots():
            ps1 = root / "cursor-agent.ps1"
            if ps1.is_file():
                return _launch_via_ps1(ps1)
        for name in names:
            found = _which(name)
            if not found:
                continue
            path = Path(found)
            if path.suffix.lower() in {".ps1", ".cmd", ".bat"}:
                ps1 = _ps1_for(path)
                if ps1 is not None:
                    return _launch_via_ps1(ps1)
            extra = requested[1:] if name == requested[0] else []
            return [found, *extra]

    for name in names:
        found = _which(name)
        if found:
            extra = requested[1:] if name == requested[0] else []
            return [found, *extra]
        local = Path.home() / ".local" / "bin" / name
        if local.is_file():
            extra = requested[1:] if name == requested[0] else []
            return [str(local), *extra]
    return requested


class _WinJob:
    """Kill the agent and any children when closed, including after the parent exits.

    Windows will not delete a directory that is still a process's current
    directory. The Cursor CLI is node, and node can leave a child behind with
    that cwd after the parent has already exited.
    """

    def __init__(self) -> None:
        self._handle: int | None = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32 = kernel32
        # Handles are pointer-sized. The default ctypes restype (c_int) truncates
        # them on 64-bit Windows and CloseHandle then hits the wrong object.
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            self._handle = None
            return

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            self._handle,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            self.close()

    def assign(self, pid: int) -> None:
        if not self._handle:
            return
        kernel32 = self._kernel32
        # PROCESS_TERMINATE | PROCESS_SET_QUOTA, required by AssignProcessToJobObject.
        process = kernel32.OpenProcess(0x0001 | 0x0100, False, pid)
        if not process:
            return
        try:
            kernel32.AssignProcessToJobObject(self._handle, process)
        finally:
            kernel32.CloseHandle(process)

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle:
            self._kernel32.CloseHandle(handle)


async def _cleanup_proc(proc: asyncio.subprocess.Process | None) -> None:
    """Ensure subprocess pipes and transports are closed (needed on Windows)."""
    if proc is None:
        return
    if proc.returncode is None:
        _kill_process_tree(proc.pid)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except TimeoutError:
            pass
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        transport.close()


def _kill_process_tree(pid: int) -> None:
    """Stop pid and any children. No-op when the process is already gone."""
    if os.name != "nt":
        return
    try:
        import subprocess

        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        pass


async def _remove_workdir(path: str) -> None:
    """Delete the agent cwd. A locked directory must not fail the score."""
    target = Path(path)
    for delay in (0, 0.05, 0.15, 0.4, 0.8):
        if delay:
            await asyncio.sleep(delay)
        try:
            shutil.rmtree(target)
            return
        except OSError:
            continue


class CursorProvider(LLMProvider):
    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        self._argv = resolve_cursor_argv(config.command)

    def ensure_ready(self) -> None:
        self._argv = resolve_cursor_argv(self.config.command)
        program = self._argv[0]
        if not Path(program).is_file() and shutil.which(program) is None:
            raise LLMError(
                f"Cursor Agent CLI {self.config.command!r} was not found. "
                "Looked on PATH and in %LOCALAPPDATA%\\cursor-agent. "
                "Install it from https://cursor.com/docs/cli or set llm.provider "
                "to openai, anthropic, or ollama."
            )

    async def complete(self, system: str, user: str) -> str:
        self.ensure_ready()
        prompt = f"{system}\n\n{user}"
        proc: asyncio.subprocess.Process | None = None
        # Isolate the agent from the repo. On Windows, deleting this directory
        # raises WinError 32 if the agent (or a child) still has it as cwd, and
        # that exception used to discard a successful score.
        workdir = tempfile.mkdtemp(prefix="jobscout-cursor-")
        job = _WinJob() if os.name == "nt" else None
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self._argv,
                    *agent_cli_args(self.config.model, prompt),
                    cwd=workdir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as e:
                raise LLMError(
                    f"Failed to launch Cursor Agent CLI ({self._argv[0]!r}): {e}"
                ) from e
            if job is not None:
                job.assign(proc.pid)

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.config.timeout_seconds,
                )
            except TimeoutError as e:
                raise LLMError("Cursor CLI timed out") from e

            if proc.returncode != 0:
                raise LLMError(
                    f"Cursor CLI failed (exit {proc.returncode}): "
                    f"{stderr.decode()[:300]}"
                )

            output = stdout.decode()
            try:
                envelope = json.loads(output)
            except json.JSONDecodeError as e:
                raise LLMError(
                    f"Cursor CLI returned non-JSON output: {output[:300]}"
                ) from e

            if envelope.get("is_error"):
                raise LLMError(envelope.get("result", "Cursor CLI error"))

            result = envelope.get("result", "")
            if isinstance(result, dict):
                return json.dumps(result)
            return str(result or "")
        finally:
            await _cleanup_proc(proc)
            if job is not None:
                job.close()
            await _remove_workdir(workdir)
