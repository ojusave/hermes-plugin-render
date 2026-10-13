"""Behavior tests against real Hermes classes and our remote process supervisor."""

import base64
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault(
    "HERMES_HOME", str(Path(__file__).resolve().parents[1] / ".test-home" / "unit")
)

import pytest
from render.experimental.sandbox import SandboxExecExit, SandboxExecOutput
from tools.environments.base import EnvironmentConnectionError

from hermes_render.environment import RenderEnvironment
from hermes_render.process import RenderProcess
from hermes_render.provider import RenderProvider, Settings

RUNNER = Path(__file__).resolve().parents[1] / "hermes_render" / "remote_runner.py"


def supervise(tmp_path, command, timeout=3, stdin="", cancelled=False):
    control = tmp_path / "control"
    if cancelled:
        control.mkdir()
        (control / "cancel").touch()
    payload = base64.b64encode(
        json.dumps(
            {
                "control": str(control),
                "command": command,
                "login": False,
                "timeout": timeout,
                "stdin": stdin,
            }
        ).encode()
    ).decode()
    return subprocess.run(
        [sys.executable, str(RUNNER), payload], capture_output=True, text=True, timeout=10
    )


@pytest.mark.parametrize("code", [0, 7])
def test_remote_output_and_exit(tmp_path, code):
    result = supervise(tmp_path, f"printf hello; printf problem >&2; exit {code}")
    assert (result.stdout, result.stderr, result.returncode) == ("hello", "problem", code)


def test_remote_stdin_is_data(tmp_path):
    result = supervise(tmp_path, "cat", stdin="λ\n$(touch should-not-exist)\n")
    assert result.returncode == 0
    assert result.stdout == "λ\n$(touch should-not-exist)\n"


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux process-group behavior is verified on live Render sandboxes; macOS runner denies killpg",
)
def test_timeout_stops_children(tmp_path):
    marker = tmp_path / "late"
    result = supervise(tmp_path, f"(sleep 1; touch '{marker}') & wait", timeout=0.1)
    assert result.returncode == 124
    time.sleep(1.1)
    assert not marker.exists()


def test_cancel_before_dispatch_does_not_run(tmp_path):
    marker = tmp_path / "should-not-run"
    result = supervise(tmp_path, f"touch '{marker}'", cancelled=True)
    assert result.returncode == 130
    assert not marker.exists()


def test_stream_preserves_large_output_and_failure_code():
    text = "λ" * 100_000
    p = RenderProcess(iter([SandboxExecOutput("stdout", text), SandboxExecExit(7)]), lambda: None)
    assert p.stdout.read() == text
    assert p.wait(5) == 7
    p.stdout.close()


@pytest.mark.parametrize("events", [[], [SandboxExecOutput("stdout", "partial")]])
def test_missing_exit_never_becomes_success(events):
    p = RenderProcess(iter(events), lambda: None)
    p.stdout.read()
    with pytest.raises(EnvironmentConnectionError, match="completion is unknown"):
        p.wait(5)
    p.stdout.close()


def test_transport_error_does_not_disclose_exception():
    def events():
        yield SandboxExecOutput("stdout", "public-output")
        raise RuntimeError("credential=secret-value")

    p = RenderProcess(events(), lambda: None)
    assert p.stdout.read() == "public-output"
    with pytest.raises(EnvironmentConnectionError) as exc:
        p.wait(5)
    assert "secret-value" not in str(exc.value)
    p.stdout.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(workspace_id="bad"),
        dict(workspace_id="tea-test", lifetime_seconds=True),
        dict(workspace_id="tea-test", lifetime_seconds=0),
        dict(workspace_id="tea-test", plan="free"),
        dict(workspace_id="tea-test", network_policy="allow-list"),
    ],
)
def test_invalid_settings_rejected(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_persistent_mode_rejected_before_create():
    p = RenderProvider(SimpleNamespace(get_config=lambda k, d: d))
    with pytest.raises(ValueError, match="disposable"):
        p.create_environment(
            cwd="/workspace", timeout=30, container_config={"container_persistent": True}
        )


def test_provider_keeps_guard_and_strips_render_key():
    p = RenderProvider(SimpleNamespace(get_config=lambda k, d: d))
    assert p.skip_container_guards is False
    assert "RENDER_API_KEY" in p.strip_env_keys
    assert p.session_isolated_when_nonpersistent is True


def test_cleanup_failure_remains_retryable():
    env = RenderEnvironment.__new__(RenderEnvironment)
    env._active, env._closed, env._broken, env._sync_manager = [], False, False, None
    env._lock, env.sandbox_id = threading.RLock(), "sbx-test"
    calls = []

    def terminate(sandbox_id):
        calls.append(sandbox_id)
        if len(calls) == 1:
            raise RuntimeError("temporary service failure")

    env._api = SimpleNamespace(
        terminate=terminate,
        from_id=lambda sid: SimpleNamespace(status="terminated", terminated_at=None),
    )
    with pytest.raises(EnvironmentConnectionError, match="retry cleanup"):
        env.cleanup()
    assert not env._closed
    env.cleanup()
    env.cleanup()
    assert env._closed and len(calls) == 2


def test_control_error_is_sanitized():
    env = RenderEnvironment.__new__(RenderEnvironment)
    env.sandbox_id = "sbx-test"

    def fail(*args):
        raise RuntimeError("token=private-secret")

    env._api = SimpleNamespace(exec=fail)
    with pytest.raises(EnvironmentConnectionError) as exc:
        env._control("true")
    assert "private-secret" not in str(exc.value)
