"""Exercise repeated use and failures on paid Render Sandboxes from this host.

Run with --render-cli-auth to explicitly reuse the local Render CLI login.
Each worker gets its own disposable Hermes profile. Private receipts retain the
resource IDs needed for cleanup; the public report removes those identifiers.
This is a bounded engineering test, not a long-duration or independent user study.
"""

from __future__ import annotations

# Host-side process control, timestamps, file receipts, and parallel test workers.
import argparse
import hashlib
import json
import logging
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

# Use the existing isolated-profile setup and real plugin loader.
from test_support import ROOT, load_provider, prepare_home


def save(path, value):
    """Replace a host-side receipt atomically, so a killed worker leaves readable evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".writing")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n")
    temporary.replace(path)


def authentication():
    """Read the already-authorized CLI login on the host; never print its contents."""
    from ruamel.yaml import YAML

    cfg = YAML(typ="safe").load((Path.home() / ".render/cli.yaml").read_text())
    return cfg["api"]["key"], cfg["workspace"]


def client():
    """Give the supervisor its own Render connection, separate from the worker being killed."""
    from render import Render

    token, workspace = authentication()
    return Render(token=token, owner_id=workspace).experimental.sandboxes


def state_record(state):
    """Keep actual service status, including errored resources with a termination timestamp."""
    return {
        k: getattr(state, k)
        for k in (
            "status",
            "terminated_at",
            "created_at",
            "plan",
            "region",
            "timeout_seconds",
            "network_policy",
        )
    }


def direct(api, sandbox_id, command):
    """Run a bounded observation in the cloud without involving the plugin under test."""
    from render.experimental.sandbox import SandboxExecExit, SandboxExecOutput

    output, code = "", None
    for event in api.exec(sandbox_id, "timeout 35 bash -c " + shlex.quote(command)):
        if isinstance(event, SandboxExecOutput):
            output += event.data
        elif isinstance(event, SandboxExecExit):
            code = event.exit_code
    return {"output": output, "returncode": code}


def active_ids(api):
    """Traverse every active page; checking only the first page could miss a leaked sandbox."""
    ids, cursor = set(), None
    while True:
        page = api.list(status=["creating", "running", "suspended", "resuming"], cursor=cursor)
        ids.update(item.id for item in page.sandboxes)
        if not page.next_cursor:
            return ids
        cursor = page.next_cursor


def worker(args):
    """Load the actual plugin in a separate process and exercise one owned environment."""
    from ruamel.yaml import YAML

    receipt = args.receipt
    report = {"mode": args.worker, "status": "running", "sandbox_ids": [], "checks": []}
    home, _ = prepare_home("reliability/" + args.run_id + "/" + receipt.stem, True)
    yaml = YAML(typ="safe")
    config = yaml.load((home / "config.yaml").read_text())
    lifetime = 60 if args.worker == "crash" else 300
    config["plugins"]["entries"]["render"]["settings"]["lifetime_seconds"] = lifetime
    with (home / "config.yaml").open("w") as stream:
        yaml.dump(config, stream)
    # Same remote filename, different contents in each profile. Mixing profiles
    # would reveal itself as the wrong marker after upload or synchronization back.
    marker = "profile-" + receipt.stem
    skill = home / "skills/reliability/SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("# Synthetic test marker\n" + marker + "\n")

    class CreationReceipt(logging.Handler):
        """Record ownership immediately after create, even if later initialization fails."""

        def emit(self, record):
            if record.msg == "Render sandbox created: %s":
                report["sandbox_ids"].append(record.args[0])
                save(receipt, report)

    logger = logging.getLogger("")
    handler = CreationReceipt()
    logger.addHandler(handler)
    # Only this isolated worker changes its logging level; the normal user profile is untouched.
    logger.setLevel(logging.INFO)
    provider = load_provider()
    env = None
    try:
        env = provider.create_environment(
            cwd="/workspace",
            timeout=30,
            task_id=receipt.stem,
            container_config={"container_persistent": False},
        )
        report["state_at_start"] = state_record(env._api.from_id(env.sandbox_id))
        assert report["state_at_start"]["plan"] == "starter"
        assert report["state_at_start"]["network_policy"] == "deny-all"
        assert report["state_at_start"]["timeout_seconds"] == lifetime
        report["ready_at"] = datetime.now(UTC).isoformat()
        report["sandbox_id"] = env.sandbox_id
        save(receipt, report)
        if args.worker == "crash":
            # The parent will kill this host process after it sees started on Render.
            # The remote command's own 10-second deadline must still stop its children.
            env.execute(
                "(sleep 25; touch /workspace/late) & touch /workspace/started; sleep 90", timeout=10
            )
            raise AssertionError("Crash worker was not killed before its command finished")
        if args.barrier:
            deadline = time.monotonic() + 80
            while not args.barrier.exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("Parallel test barrier did not open")
                time.sleep(0.2)
        report["work_started_at"] = datetime.now(UTC).isoformat()
        for index in range(args.commands):
            # Test a changing value, not a repeated cached answer. A nonzero exit
            # every tenth call also checks that one failed command does not spoil the session.
            result = env.execute(
                f"printf '%s' '{marker}-{index}' > /workspace/value; "
                f"cat /workspace/value; exit {7 if index % 10 == 9 else 0}"
            )
            assert result["output"] == f"{marker}-{index}"
            assert result["returncode"] == (7 if index % 10 == 9 else 0)
            time.sleep(1)
        report["checks"].append(f"{args.commands} changing outputs and expected exit codes")
        result = env.execute("export RELIABILITY_MARKER=retained; mkdir -p subdir; cd subdir")
        assert result["returncode"] == 0
        result = env.execute('printf "%s:%s" "$RELIABILITY_MARKER" "$PWD"')
        assert result["output"] == "retained:/workspace/subdir"
        report["checks"].append("shell variable and working directory retained")
        result = env.execute(
            "cat " + shlex.quote(env._remote_state + "/skills/reliability/SKILL.md")
        )
        assert result["returncode"] == 0 and marker in result["output"]
        report["checks"].append("own profile marker uploaded without cross-profile substitution")
        result = env.execute(
            "printf '%s' '"
            + marker
            + "-remote' > "
            + shlex.quote(env._remote_state + "/skills/reliability/SKILL.md")
        )
        assert result["returncode"] == 0
        exported = receipt.with_suffix(".artifact.txt")
        env._api.copy_from(env.sandbox_id, "/workspace/value", str(exported))
        assert exported.read_text() == f"{marker}-{args.commands - 1}"
        report["checks"].append("downloaded file independently matches expected value")
        assert env.execute('test -z "${RENDER_API_KEY+x}"')["returncode"] == 0
        report["checks"].append("Render API key absent from cloud environment")
        report["work_finished_at"] = datetime.now(UTC).isoformat()
        env.cleanup()
        env.cleanup()
        assert skill.read_text() == marker + "-remote"
        report["checks"].append("own profile synchronized back; repeated cleanup succeeds")
        report["status"] = "passed"
    except BaseException as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        raise
    finally:
        if env:
            try:
                env.cleanup()
            except Exception as exc:
                report["cleanup_error_type"] = type(exc).__name__
        report["finished_at"] = datetime.now(UTC).isoformat()
        save(receipt, report)
        logger.removeHandler(handler)


def start_worker(run_dir, mode, name, commands=3, barrier=None):
    """Launch only this test's child process; logs stay private and contain no printed keys."""
    receipt = run_dir / (name + ".json")
    command = [
        sys.executable,
        __file__,
        "--render-cli-auth",
        "--worker",
        mode,
        "--receipt",
        str(receipt),
        "--run-id",
        run_dir.name,
        "--commands",
        str(commands),
    ]
    if barrier:
        command += ["--barrier", str(barrier)]
    log = receipt.with_suffix(".log").open("w")
    process = subprocess.Popen(command, stdout=log, stderr=log)
    log.close()
    return process, receipt


def wait_ready(process, receipt, api=None):
    """Wait for a durable ownership receipt, then optionally confirm remote work has begun."""
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Worker exited before readiness; inspect its private log")
        if receipt.exists():
            value = json.loads(receipt.read_text())
            if value.get("ready_at"):
                if (
                    api is None
                    or direct(api, value["sandbox_id"], "test -e /workspace/started")["returncode"]
                    == 0
                ):
                    return value
        time.sleep(2)
    raise TimeoutError("Worker did not become ready")


def failure_checks(run_dir, run_id):
    """Interrupt a real cloud session, then fail and retry cleanup against the real API."""
    from render import Render
    from tools.environments.base import EnvironmentConnectionError

    _, workspace = prepare_home("reliability/" + run_id + "/failure", True)
    provider = load_provider()
    api = client()
    report = {"checks": [], "sandbox_ids": [], "status": "running"}
    receipt = run_dir / "failures.json"
    environments = []
    try:
        env = provider.create_environment(
            cwd="/workspace",
            timeout=30,
            task_id="remote-termination",
            container_config={"container_persistent": False},
        )
        environments.append(env)
        report["sandbox_ids"].append(env.sandbox_id)
        save(receipt, report)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                env.execute, "touch /workspace/started; sleep 60; echo FALSE_SUCCESS", timeout=45
            )
            deadline = time.monotonic() + 15
            while direct(api, env.sandbox_id, "test -e /workspace/started")["returncode"] != 0:
                if time.monotonic() > deadline:
                    raise TimeoutError("Remote task did not start")
                time.sleep(1)
            api.terminate(env.sandbox_id)
            try:
                answer = future.result(timeout=60)
            except EnvironmentConnectionError:
                report["checks"].append("remote termination raises a connection failure")
            else:
                assert answer.get("returncode") != 0, "Dead sandbox reported success"
                report["checks"].append("remote termination reports nonzero failure")
            try:
                answer = env.execute("echo MUST_NOT_RUN")
            except EnvironmentConnectionError:
                report["checks"].append("broken environment refuses further execution")
            else:
                raise AssertionError("Broken environment accepted another command")
        env.cleanup()
        other = provider.create_environment(
            cwd="/workspace",
            timeout=30,
            task_id="cleanup-auth-failure",
            container_config={"container_persistent": False},
        )
        environments.append(other)
        report["sandbox_ids"].append(other.sandbox_id)
        save(receipt, report)
        assert other.execute("echo recovered")["output"].strip() == "recovered"
        report["checks"].append("new environment works after remote termination")
        real_api = other._api
        # A deliberately invalid test key produces a real authorization failure.
        # It does not edit or revoke the user's key. Restore the working connection
        # immediately so this exact task-owned resource can always be cleaned up.
        other._api = Render(token="invalid", owner_id=workspace).experimental.sandboxes
        try:
            try:
                other.cleanup()
            except EnvironmentConnectionError as exc:
                assert "invalid" not in str(exc)
                assert not other._closed
                report["checks"].append(
                    "real unauthorized cleanup fails clearly and stays retryable"
                )
            else:
                raise AssertionError("Invalid credential unexpectedly cleaned up the sandbox")
        finally:
            other._api = real_api
        other.cleanup()
        assert other._closed
        report["checks"].append("cleanup retry succeeds after restoring authorized connection")
        report["status"] = "passed"
    finally:
        errors = []
        for env in environments:
            try:
                env.cleanup()
            except Exception as exc:
                errors.append(type(exc).__name__)
        report["cleanup_errors"] = errors
        save(receipt, report)
    return report


def main(args):
    """Supervise bounded tests and verify all owned IDs using an independent API connection."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = ROOT / "validation/private/reliability" / stamp
    run_dir.mkdir(parents=True)
    api = client()
    processes = []
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "repeat_sessions": [],
        "parallel_sessions": [],
        "sandbox_count": 0,
        "source_sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                ROOT / "plugin.yaml",
                ROOT / "__init__.py",
                *sorted((ROOT / "hermes_render").glob("*.py")),
            ]
        },
    }
    save(run_dir / "summary.json", report)
    try:
        print("START abrupt host termination with 60-second sandbox lifetime", flush=True)
        crash, receipt = start_worker(run_dir, "crash", "crash")
        processes.append(crash)
        started = wait_ready(crash, receipt, api)
        crash.kill()
        assert crash.wait(timeout=10) == -9
        report["crash"] = {
            "host_exit_code": -9,
            "sandbox_id": started["sandbox_id"],
            "requested_lifetime_seconds": 60,
            "killed_at": datetime.now(UTC).isoformat(),
        }
        # Run independent repeated-session checks while the crashed owner's sandbox expires.
        for index in range(10):
            print(f"REPEAT {index + 1}/10", flush=True)
            proc, path = start_worker(run_dir, "regular", f"repeat-{index:02}", commands=10)
            processes.append(proc)
            assert proc.wait(timeout=160) == 0, f"Repeat worker failed: {path.name}"
            report["repeat_sessions"].append(json.loads(path.read_text()))
            if index == 0:
                # Delay beyond the ordinary child's planned 25-second write. If
                # the sandbox already expired, its terminal state is stronger evidence.
                state = api.from_id(started["sandbox_id"])
                if not state.terminated_at:
                    time.sleep(12)
                    state = api.from_id(started["sandbox_id"])
                    if not state.terminated_at:
                        check = direct(api, started["sandbox_id"], "test ! -e /workspace/late")
                        report["crash"]["late_child_write_absent"] = check["returncode"] == 0
                        assert report["crash"]["late_child_write_absent"]
            save(run_dir / "summary.json", report)
        state = api.from_id(started["sandbox_id"])
        report["crash"]["observed_after_expiry"] = state_record(state)
        assert state.terminated_at, "Killed host left a sandbox past its lifetime"
        assert started["sandbox_id"] not in active_ids(api)
        report["crash"]["absent_from_complete_active_listing"] = True
        report["crash"]["status"] = "passed"
        print("PARALLEL 3 distinct profiles and simultaneous environments", flush=True)
        barrier = run_dir / "start-parallel"
        parallel = [
            start_worker(run_dir, "regular", f"parallel-{i}", commands=12, barrier=barrier)
            for i in range(3)
        ]
        processes += [p for p, _ in parallel]
        for proc, path in parallel:
            wait_ready(proc, path)
        barrier.touch()
        for proc, path in parallel:
            assert proc.wait(timeout=160) == 0, f"Parallel worker failed: {path.name}"
            report["parallel_sessions"].append(json.loads(path.read_text()))
        assert max(x["work_started_at"] for x in report["parallel_sessions"]) < min(
            x["work_finished_at"] for x in report["parallel_sessions"]
        )
        report["parallel_work_intervals_overlap"] = True
        print("FAILURES remote termination and rejected cleanup authorization", flush=True)
        report["failures"] = failure_checks(run_dir, stamp)
        report["status"] = "passed"
    except BaseException as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        raise
    finally:
        # Stop only children created above. Then reconcile all recorded IDs,
        # including those saved before a worker failed or was forcibly killed.
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        owned = set()
        for path in run_dir.glob("*.json"):
            data = json.loads(path.read_text())
            owned.update(data.get("sandbox_ids", []))
        cleanup = []
        for sid in sorted(owned):
            item = {"sandbox_id": sid}
            try:
                for attempt in range(3):
                    try:
                        api.terminate(sid)
                        state = api.from_id(sid)
                        if state.terminated_at or state.status == "terminated":
                            break
                    except Exception:
                        if attempt == 2:
                            raise
                    time.sleep(2)
                item.update(state_record(state))
            except Exception as exc:
                item["error_type"] = type(exc).__name__
            cleanup.append(item)
        remaining = active_ids(api) & owned
        report["cleanup"] = cleanup
        report["sandbox_count"] = len(owned)
        report["all_cleanup_verified"] = not remaining and all(
            x.get("terminated_at") or x.get("status") == "terminated" for x in cleanup
        )
        if not report["all_cleanup_verified"]:
            report["status"] = "failed"
            report["error_type"] = "CleanupVerificationError"
        report["finished_at"] = datetime.now(UTC).isoformat()
        save(run_dir / "summary.json", report)

        # Remove identity and local layout from the public report, retaining
        # measured outcomes and the source fingerprints needed to reproduce it.
        def sanitize(value):
            if isinstance(value, dict):
                return {
                    k: sanitize(v)
                    for k, v in value.items()
                    if k not in {"sandbox_id", "sandbox_ids"}
                }
            return [sanitize(x) for x in value] if isinstance(value, list) else value

        public = sanitize(report)
        public["scope"] = (
            "10 sequential sessions, 3 simultaneous profiles in one workspace, host kill, remote termination, real rejected cleanup. No independent human tester or long-duration load claim."
        )
        save(ROOT / "validation/public/live-reliability.json", public)
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "sandbox_count": len(owned),
                    "all_cleanup_verified": report["all_cleanup_verified"],
                    "private_run": str(run_dir),
                },
                indent=2,
            ),
            flush=True,
        )
        if not report["all_cleanup_verified"]:
            raise RuntimeError("An owned sandbox still requires cleanup; inspect private receipts")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-cli-auth", action="store_true", required=True)
    parser.add_argument("--worker", choices=["regular", "crash"])
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--barrier", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--commands", type=int, default=3)
    arguments = parser.parse_args()
    worker(arguments) if arguments.worker else main(arguments)
