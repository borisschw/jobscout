"""Run progress logging for long-running pipeline steps."""

from __future__ import annotations

import sys
from typing import TextIO


class RunProgress:
    """Line-based progress output (stderr by default, flushed immediately)."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        verbose: bool = False,
        stream: TextIO | None = None,
    ) -> None:
        self.enabled = enabled
        self.verbose = verbose
        self.stream = stream or sys.stderr

    def log(self, message: str) -> None:
        if self.enabled:
            print(message, file=self.stream, flush=True)

    def detail(self, message: str) -> None:
        if self.enabled and self.verbose:
            print(message, file=self.stream, flush=True)
