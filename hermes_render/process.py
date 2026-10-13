"""Translate Render's output stream into the process interface Hermes consumes."""

from __future__ import annotations

import contextvars
import os
import subprocess
import threading

from render.experimental.sandbox import SandboxExecExit, SandboxExecOutput
from tools.environments.base import EnvironmentConnectionError


class RenderProcess:
    def __init__(self, events, cancel):
        self._cancel = cancel
        self._done = threading.Event()
        self._error = None
        self._returncode = None
        read_fd, self._write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "r", encoding="utf-8", errors="replace")
        self._thread = threading.Thread(
            target=contextvars.copy_context().run,
            args=(self._consume, events),
            daemon=True,
        )
        self._thread.start()

    def _consume(self, events):
        try:
            for event in events:
                if isinstance(event, SandboxExecOutput):
                    remaining = memoryview(event.data.encode("utf-8", errors="replace"))
                    while remaining:
                        remaining = remaining[os.write(self._write_fd, remaining) :]
                elif isinstance(event, SandboxExecExit):
                    self._returncode = event.exit_code
            if self._returncode is None:
                raise RuntimeError("Execution stream ended without an exit event")
        except Exception:
            # SDK exceptions can contain URLs and headers. Only expose a fixed diagnostic.
            self._error = EnvironmentConnectionError(
                "Render command transport failed; completion is unknown.",
                retry_hint="End this session and inspect its sandbox before retrying the task.",
            )
        finally:
            os.close(self._write_fd)
            self._done.set()

    @property
    def returncode(self):
        return self._returncode

    def poll(self):
        if not self._done.is_set():
            return None
        if self._error:
            raise self._error
        return self._returncode

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("Render command", timeout)
        return self.poll()

    def kill(self):
        if not self._done.is_set():
            self._cancel()
