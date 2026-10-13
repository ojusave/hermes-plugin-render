"""Drop one real encrypted execution connection without changing system networking.

Run on the host with --render-cli-auth. A local forwarder passes encrypted bytes
unchanged, then closes the sockets after a command has started on Render. TLS
certificate checking stays enabled. Only this test process uses the forwarder.
"""

from __future__ import annotations

# These standard-library modules supply receipts, a loopback network listener,
# and a separate observer thread. No certificate or traffic decryption library is used.
import argparse
import hashlib
import json
import os
import select
import socket
import socketserver
import threading
import time
from datetime import UTC, datetime

# Reuse the existing task-owned credential reader, result writer, and plugin loader.
from live_reliability import active_ids, client, direct, save, state_record
from test_support import ROOT, load_provider, prepare_home


class Tunnel(socketserver.BaseRequestHandler):
    """Pass an HTTPS CONNECT tunnel through unchanged until the test asks it to close."""

    def handle(self):
        # Read only the proxy connection request, such as CONNECT host:443.
        # The subsequent application headers and credentials remain encrypted.
        upstream = None
        try:
            self.request.settimeout(10)
            header = b""
            while b"\r\n\r\n" not in header and len(header) < 8192:
                part = self.request.recv(1)
                if not part:
                    return
                header += part
            method, address, _ = header.split(b"\r\n", 1)[0].decode().split()
            if method != "CONNECT" or self.server.cut.is_set():
                return
            hostname, port = address.rsplit(":", 1)
            # The destination comes from Render's normal SDK, and certificate
            # validation remains with that SDK. The test never substitutes a certificate.
            upstream = socket.create_connection((hostname, int(port)), timeout=10)
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.server.connections += 1
            peers = (self.request, upstream)
            # Forward both directions. Closing either socket interrupts the real
            # in-flight network stream instead of manufacturing an exception.
            while not self.server.cut.is_set():
                readable, _, _ = select.select(peers, [], [], 0.1)
                for source in readable:
                    payload = source.recv(65536)
                    if not payload:
                        return
                    destination = upstream if source is self.request else self.request
                    destination.sendall(payload)
        except OSError, ValueError:
            # A socket error is expected when the deliberate cut happens.
            # The application-level assertion below decides whether Hermes handled it correctly.
            pass
        finally:
            if upstream is not None:
                upstream.close()
            self.request.close()


class Forwarder(socketserver.ThreadingTCPServer):
    """Serve a few test connections on this computer's loopback address only."""

    daemon_threads = True
    allow_reuse_address = True


def main():
    """Create one sandbox, interrupt its live output stream, and independently check cleanup."""
    # Import after choosing this runner so importing helpers alone never creates a resource.
    from tools.environments.base import EnvironmentConnectionError

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = ROOT / "validation/private/transport" / stamp
    run_dir.mkdir(parents=True)
    prepare_home("transport/" + stamp, True)
    provider, observer = load_provider(), client()
    env = None
    report = {
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "method": "Real encrypted TCP stream closed by a test-only local CONNECT forwarder",
        "source_sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT / "hermes_render").glob("*.py")
        },
    }
    # Save and restore only this process's proxy variables. Other apps and users
    # keep their existing network settings throughout the test.
    proxy_names = ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy")
    previous = {name: os.environ.get(name) for name in proxy_names}
    server = Forwarder(("127.0.0.1", 0), Tunnel)
    server.cut, server.connections = threading.Event(), 0
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    watcher = None
    try:
        env = provider.create_environment(
            cwd="/workspace",
            timeout=30,
            task_id="transport-drop",
            container_config={"container_persistent": False},
        )
        report["sandbox_id"] = env.sandbox_id
        save(run_dir / "report.json", report)
        # Prime the observer's account API connection before applying the proxy.
        observer.from_id(env.sandbox_id)
        server_thread.start()
        address = "http://127.0.0.1:" + str(server.server_address[1])
        os.environ.update(HTTPS_PROXY=address, https_proxy=address, NO_PROXY="", no_proxy="")
        observed = {}

        def disconnect_after_start():
            """Wait for a cloud-created marker, then sever the execution connection."""
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    result = direct(observer, env.sandbox_id, "test -e /workspace/started")
                    if result["returncode"] == 0:
                        observed["remote_start_confirmed"] = True
                        assert server.connections > 0
                        server.cut.set()
                        observed["cut_at"] = datetime.now(UTC).isoformat()
                        return
                    time.sleep(1)
                observed["error"] = "Remote command did not start within 20 seconds"
            except Exception as exc:
                observed["error"] = type(exc).__name__
                server.cut.set()

        watcher = threading.Thread(target=disconnect_after_start, daemon=True)
        watcher.start()
        print("DISCONNECT real execution connection after remote start", flush=True)
        try:
            answer = env.execute(
                "echo one >> /workspace/dispatches; "
                "touch /workspace/started; sleep 50; echo FALSE_SUCCESS",
                timeout=60,
            )
        except EnvironmentConnectionError as exc:
            report["reported_error"] = str(exc)
            report["connection_failure_reported"] = True
        else:
            raise AssertionError(
                f"Disconnected execution unexpectedly returned {answer.get('returncode')}"
            )
        watcher.join(timeout=25)
        assert (
            not watcher.is_alive()
            and observed.get("remote_start_confirmed")
            and "error" not in observed
        )
        report["observation"] = observed
        report["forwarded_connection_count"] = server.connections
        # Stop routing before checking the broken environment and cleaning up.
        # The plugin must still refuse further work, even though networking is healthy again.
        for name, value in previous.items():
            os.environ.pop(name, None) if value is None else os.environ.__setitem__(name, value)
        try:
            env.execute("echo MUST_NOT_RUN")
        except EnvironmentConnectionError:
            report["broken_environment_refuses_new_command"] = True
        else:
            raise AssertionError("Connection failure was silently ignored")
        state = observer.from_id(env.sandbox_id)
        # If the plugin already terminated after losing its cancellation channel,
        # no further file read is possible. Otherwise verify only one dispatch occurred.
        if not state.terminated_at:
            check = direct(observer, env.sandbox_id, "wc -l < /workspace/dispatches")
            assert check["returncode"] == 0 and check["output"].strip() == "1"
            report["dispatch_count"] = 1
        report["status"] = "passed"
    except BaseException as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        raise
    finally:
        # Restore the host process before cleanup so the deliberate outage cannot
        # prevent termination. The independent client retries only this owned ID.
        for name, value in previous.items():
            os.environ.pop(name, None) if value is None else os.environ.__setitem__(name, value)
        server.cut.set()
        if server_thread.is_alive():
            server.shutdown()
        server.server_close()
        if watcher:
            watcher.join(timeout=25)
        if env:
            try:
                env.cleanup()
            finally:
                observer.terminate(env.sandbox_id)
                state = observer.from_id(env.sandbox_id)
                report["cleanup"] = state_record(state)
                report["all_cleanup_verified"] = bool(
                    state.terminated_at
                ) and env.sandbox_id not in active_ids(observer)
        # Cleanup belongs to the acceptance result, even if execution checks passed.
        if not report.get("all_cleanup_verified"):
            report["status"] = "failed"
            report["error_type"] = "CleanupVerificationError"
        report["finished_at"] = datetime.now(UTC).isoformat()
        save(run_dir / "report.json", report)
        public = {k: v for k, v in report.items() if k != "sandbox_id"}
        save(ROOT / "validation/public/live-transport.json", public)
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "all_cleanup_verified": report.get("all_cleanup_verified"),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    # Require an explicit opt-in to reading the existing CLI login; do not start
    # paid cloud work merely because another program imports this module.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-cli-auth", action="store_true", required=True)
    parser.parse_args()
    main()
