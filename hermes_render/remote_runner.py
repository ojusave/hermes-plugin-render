"""Runs INSIDE the sandbox: bound a command and stop its process group on cancellation.

The host sends only the command, input, timeout and a random control directory.
Render account credentials never appear in this payload.
"""

import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main():
    data = json.loads(base64.b64decode(sys.argv[1]))
    control = Path(data["control"])
    control.mkdir(parents=True, exist_ok=True, mode=0o700)
    cancelled = False
    process = None

    def request_stop(signum, frame):
        nonlocal cancelled
        cancelled = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        if (control / "cancel").exists():
            return 130
        with tempfile.TemporaryFile() as input_file:
            input_file.write(data.get("stdin", "").encode("utf-8", "surrogateescape"))
            input_file.seek(0)
            process = subprocess.Popen(
                ["bash", "-lc" if data["login"] else "-c", data["command"]],
                stdin=input_file,
                start_new_session=True,
            )
            (control / "pid").write_text(str(process.pid))
            deadline = time.monotonic() + data["timeout"]
            while process.poll() is None:
                expired = time.monotonic() >= deadline
                if cancelled or expired or (control / "cancel").exists():
                    # A process group includes ordinary children such as test workers.
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    time.sleep(0.2)
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    return 124 if expired else 130
                time.sleep(0.05)
            return process.returncode if process.returncode >= 0 else 128 - process.returncode
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        shutil.rmtree(control, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
