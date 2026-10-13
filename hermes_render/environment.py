"""One owned Render sandbox per Hermes execution environment.

Hermes owns the shell/session behavior; this adapter owns transport and lifetime.
No local command execution or automatic replay of ambiguous remote commands.
"""

from __future__ import annotations

import base64
import json
import logging
import shlex

# Build one temporary archive on the host so large skill libraries need one upload.
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

from hermes_constants import get_hermes_home
from render import Render
from render.experimental.sandbox import SandboxExecExit, SandboxExecOutput, SandboxNotFoundError
from tools.environments.base import BaseEnvironment, EnvironmentConnectionError
from tools.environments.file_sync import FileSyncManager, iter_sync_files, quoted_rm_command

from .process import RenderProcess
from .provider import Settings

logger = logging.getLogger(__name__)


class RenderEnvironment(BaseEnvironment):
    _stdin_mode = "payload"

    def __init__(
        self,
        *,
        settings: Settings,
        token: str,
        cwd="/workspace",
        timeout=60,
        task_id="default",
        client=None,
    ):
        if cwd == "~":
            cwd = "/workspace"
        if not cwd.startswith("/") or "\x00" in cwd:
            raise ValueError(
                "Render terminal.cwd must be an absolute Linux path, such as /workspace."
            )
        super().__init__(cwd=cwd, timeout=timeout)
        self.settings = settings
        self.task_id = task_id
        self._home = get_hermes_home()
        self._sdk = client or Render(token=token, owner_id=settings.workspace_id)
        self._api = self._sdk.experimental.sandboxes
        self._lock = threading.RLock()
        self._closed = False
        self._broken = False
        self._sync_manager = None
        self._sync_error = None
        self._active = []
        self.sandbox_id = None
        self._runner = f"/tmp/hermes-render-{uuid.uuid4().hex}.py"
        # Never retry creation after an ambiguous response: that can create a duplicate.
        try:
            sandbox = self._api.create(
                plan=settings.plan,
                timeout_seconds=settings.lifetime_seconds,
                network_policy=settings.network_policy,
                env={},
            )
        except Exception:
            raise EnvironmentConnectionError(
                "Render sandbox creation failed or its response was lost. Check the Render Dashboard before retrying."
            ) from None
        self.sandbox_id = sandbox.id
        logger.info("Render sandbox created: %s", self.sandbox_id)
        try:
            deadline = time.monotonic() + 120
            while sandbox.status != "running":
                if sandbox.status in {"errored", "terminated"} or time.monotonic() >= deadline:
                    raise EnvironmentConnectionError(
                        f"Render sandbox {self.sandbox_id} did not become ready."
                    )
                time.sleep(1)
                sandbox = self._api.from_id(self.sandbox_id)
            self._control(f"mkdir -p {shlex.quote(cwd)}")
            self._api.copy_to(
                self.sandbox_id, str(Path(__file__).with_name("remote_runner.py")), self._runner
            )
            self._remote_home = self._control("printf '%s' \"$HOME\"").strip()
            if not self._remote_home.startswith("/"):
                raise EnvironmentConnectionError("Could not determine the sandbox home directory.")
            self._remote_state = f"{self._remote_home}/.hermes"
            self._sync_manager = FileSyncManager(
                get_files_fn=lambda: iter_sync_files(self._remote_state),
                upload_fn=self._upload,
                # Hermes supplies only changed, staged files to this batch callback.
                bulk_upload_fn=self._bulk_upload,
                delete_fn=self._delete,
                bulk_download_fn=self._download_state,
            )
            self._sync()
            self.init_session()
        except BaseException as exc:
            self.cleanup()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise EnvironmentConnectionError(
                f"Render sandbox {self.sandbox_id} initialization failed; it was terminated."
            ) from None

    def _control(self, command):
        output = []
        exit_code = None
        try:
            bounded_command = f"timeout 120 bash -c {shlex.quote(command)}"
            for event in self._api.exec(self.sandbox_id, bounded_command):
                if isinstance(event, SandboxExecOutput):
                    output.append(event.data)
                elif isinstance(event, SandboxExecExit):
                    exit_code = event.exit_code
        except Exception:
            raise EnvironmentConnectionError(
                f"Render control transport failed in {self.sandbox_id}."
            ) from None
        if exit_code != 0:
            raise EnvironmentConnectionError(
                f"Render control operation failed in {self.sandbox_id}."
            )
        return "".join(output)

    def _upload(self, host_path, remote_path):
        try:
            parent = str(PurePosixPath(remote_path).parent)
            self._control(f"mkdir -p {shlex.quote(parent)}")
            self._api.copy_to(self.sandbox_id, host_path, remote_path)
        except Exception:
            self._sync_error = EnvironmentConnectionError("Render file transfer failed.")
            raise self._sync_error from None

    def _bulk_upload(self, files):
        """Send Hermes's selected files together, preserving each remote destination."""
        # The archive is private on this host and removed when the context exits.
        # For example, /root/.hermes/skills/a.md becomes root/.hermes/skills/a.md.
        # Only Hermes's staged file list is included, never the whole host folder.
        archive = f"/tmp/hermes-push-{uuid.uuid4().hex}.tar"
        try:
            with tempfile.NamedTemporaryFile(prefix="hermes-render-push-", suffix=".tar") as local:
                with tarfile.open(fileobj=local, mode="w", dereference=True) as bundle:
                    for host_path, remote_path in files:
                        # Reject destinations that could escape their declared absolute path.
                        # Staged inputs must contain file bytes, not directories or devices.
                        destination = PurePosixPath(remote_path)
                        if (
                            not destination.is_absolute()
                            or ".." in destination.parts
                            or "\x00" in remote_path
                            or len(destination.parts) < 2
                            or not Path(host_path).is_file()
                        ):
                            raise ValueError("Invalid synchronized file mapping")
                        bundle.add(host_path, arcname=str(destination).lstrip("/"), recursive=False)
                # Flush the host file before the Render client reads and uploads it.
                local.flush()
                self._api.copy_to(self.sandbox_id, local.name, archive)
            # Extraction runs on Render. tar creates missing parent folders and preserves
            # file modes, such as an executable skill script, without changing ownership.
            # Remove the remote archive even when extraction returns an error.
            quoted = shlex.quote(archive)
            self._control(
                f'tar --no-same-owner -xf {quoted} -C /; result=$?; rm -f {quoted}; exit "$result"'
            )
        except Exception:
            # Hermes catches upload errors internally. Keep a separate failure marker
            # so _sync() can stop the agent command instead of using incomplete files.
            self._sync_error = EnvironmentConnectionError("Render batch file transfer failed.")
            raise self._sync_error from None

    def _delete(self, paths):
        try:
            self._control(quoted_rm_command(paths))
        except Exception:
            self._sync_error = EnvironmentConnectionError("Render file transfer failed.")
            raise self._sync_error from None

    def _download_state(self, destination):
        archive = f"/tmp/hermes-state-{uuid.uuid4().hex}.tar"
        try:
            self._control(
                f"tar cf {shlex.quote(archive)} --exclude='*.sock' -C / {shlex.quote(self._remote_state.lstrip('/'))}"
            )
            self._api.copy_from(self.sandbox_id, archive, str(destination))
        except Exception:
            self._sync_error = EnvironmentConnectionError("Render file transfer failed.")
            raise self._sync_error from None
        finally:
            self._control(f"rm -f {shlex.quote(archive)}")

    def _sync(self):
        if self._sync_manager:
            self._sync_error = None
            self._sync_manager.sync()
            if self._sync_error:
                raise EnvironmentConnectionError(
                    "Render file synchronization failed; command was not started."
                ) from None

    def _before_execute(self):
        if self._closed or self._broken:
            raise EnvironmentConnectionError(
                "This Render environment is closed or has an unresolved transport failure."
            )
        self._sync()

    def execute(self, command, cwd="", **kwargs):
        # Preserve one shell's cwd/snapshot semantics when callers share the environment.
        with self._lock:
            try:
                return super().execute(command, cwd, **kwargs)
            except EnvironmentConnectionError:
                self._broken = True
                raise
            finally:
                self._active = [p for p in self._active if not p._done.is_set()]

    def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
        if self._closed:
            raise EnvironmentConnectionError("Render environment has been closed.")
        control = f"/tmp/hermes-render-control-{uuid.uuid4().hex}"
        payload = base64.b64encode(
            json.dumps(
                {
                    "command": cmd_string,
                    "login": login,
                    "timeout": timeout,
                    "stdin": stdin_data or "",
                    "control": control,
                }
            ).encode()
        ).decode()
        if len(payload) > 1_000_000:
            raise ValueError(
                "This preview limits one command plus stdin to 750 KB. Upload large inputs as files."
            )
        command = f"python3 {shlex.quote(self._runner)} {shlex.quote(payload)}"

        def cancel():
            try:
                self._control(
                    f"mkdir -p {shlex.quote(control)} && touch {shlex.quote(control + '/cancel')}"
                )
            except Exception:
                # Losing the cancellation channel must not leave uncontrolled remote work.
                self._broken = True
                self._terminate()

        process = RenderProcess(self._api.exec(self.sandbox_id, command), cancel)
        self._active.append(process)
        return process

    def _terminate(self):
        try:
            self._api.terminate(self.sandbox_id)
        except SandboxNotFoundError:
            return
        except Exception:
            raise EnvironmentConnectionError(
                f"Termination failed for Render sandbox {self.sandbox_id}; retry cleanup."
            ) from None
        deadline = time.monotonic() + 30
        while True:
            try:
                state = self._api.from_id(self.sandbox_id)
            except SandboxNotFoundError:
                return
            except Exception:
                raise EnvironmentConnectionError(
                    f"Could not verify cleanup of Render sandbox {self.sandbox_id}; retry cleanup."
                ) from None
            if state.status == "terminated" or state.terminated_at is not None:
                return
            if time.monotonic() >= deadline:
                raise EnvironmentConnectionError(
                    f"Could not verify cleanup of Render sandbox {self.sandbox_id}."
                )
            time.sleep(1)

    def cleanup(self):
        for process in list(self._active):
            process.kill()
        with self._lock:
            if self._closed:
                return
            sync_failed = False
            try:
                if self._sync_manager and not self._broken:
                    self._sync_error = None
                    self._sync_manager.sync_back(hermes_home=self._home)
                    sync_failed = self._sync_error is not None
            finally:
                if self.sandbox_id:
                    self._terminate()
                self._closed = True
            if sync_failed:
                raise EnvironmentConnectionError(
                    "Sandbox terminated, but Hermes state synchronization failed."
                )
