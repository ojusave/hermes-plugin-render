"""Failure, artifact, isolation and execution checks using the installed plugin."""

import json
import sys
import threading
import time

from test_support import ROOT, load_provider, prepare_home


def main():
    prepare_home("runtime", "--render-cli-auth" in sys.argv)
    provider = load_provider()
    report = {"checks": [], "sandboxes": []}
    started = time.monotonic()
    environments = []
    try:

        def create(name):
            env = provider.create_environment(
                cwd="/workspace",
                timeout=30,
                task_id=name,
                container_config={"container_persistent": False},
            )
            environments.append(env)
            report["sandboxes"].append(env.sandbox_id)
            return env

        env = create("runtime")
        result = env.execute("(sleep 3; touch /workspace/late-timeout) & wait", timeout=1)
        print("timeout", result, flush=True)
        assert result["returncode"] == 124
        time.sleep(3)
        assert env.execute("test ! -e /workspace/late-timeout")["returncode"] == 0
        report["checks"].append("timeout kills remote command and ordinary children")
        from tools.interrupt import set_interrupt

        tid = threading.get_ident()
        timer = threading.Timer(2, lambda: set_interrupt(True, thread_id=tid))
        timer.start()
        try:
            result = env.execute("(sleep 5; touch /workspace/late-interrupt) & wait", timeout=20)
        finally:
            timer.cancel()
            set_interrupt(False, thread_id=tid)
        print("interrupt", result, flush=True)
        assert result["returncode"] in (130, -2)
        time.sleep(5)
        assert env.execute("test ! -e /workspace/late-interrupt")["returncode"] == 0
        report["checks"].append("Hermes interrupt stops remote child without losing workspace")
        assert env.execute("cat", stdin_data="hello λ\n")["output"] == "hello λ\n"
        report["checks"].append("stdin and unicode")
        result = env.execute("python3 -c 'print(\"z\"*100000)'", bounded_capture=False)
        assert result["returncode"] == 0 and len(result["output"].strip()) == 100000
        report["checks"].append("large output stream")
        artifacts = ROOT / "validation" / "private"
        artifacts.mkdir(exist_ok=True)
        source, dest = artifacts / "binary-input.bin", artifacts / "binary-output.bin"
        source.write_bytes(bytes(range(256)) * 8)
        env._api.copy_to(env.sandbox_id, str(source), "/workspace/binary.bin")
        env._api.copy_from(env.sandbox_id, "/workspace/binary.bin", str(dest))
        assert dest.read_bytes() == source.read_bytes()
        report["checks"].append("binary upload/download preserves bytes")
        other = create("isolated")
        assert other.sandbox_id != env.sandbox_id
        assert other.execute("test ! -e /workspace/binary.bin")["returncode"] == 0
        report["checks"].append("separate sessions have separate filesystems")
    finally:
        failures = []
        for env in environments:
            try:
                env.cleanup()
            except Exception as exc:
                failures.append({"id": env.sandbox_id, "error_type": type(exc).__name__})
        report["cleanup_failures"] = failures
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        (ROOT / "validation" / "live-runtime.json").write_text(json.dumps(report, indent=2) + "\n")
        if failures:
            raise RuntimeError(f"Cleanup needs attention: {failures}")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
