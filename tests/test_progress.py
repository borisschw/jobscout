"""Tests for run progress logging."""

import io

from jobscout.progress import RunProgress


def test_progress_disabled_is_silent():
    buf = io.StringIO()
    progress = RunProgress(enabled=False, stream=buf)
    progress.log("hello")
    progress.detail("detail")
    assert buf.getvalue() == ""


def test_progress_logs_and_respects_verbose():
    buf = io.StringIO()
    progress = RunProgress(enabled=True, verbose=False, stream=buf)
    progress.log("step")
    progress.detail("hidden")
    assert buf.getvalue() == "step\n"

    buf = io.StringIO()
    progress = RunProgress(enabled=True, verbose=True, stream=buf)
    progress.detail("shown")
    assert buf.getvalue() == "shown\n"
