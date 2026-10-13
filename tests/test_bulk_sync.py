"""Check real archive contents and Hermes rollback without creating paid resources."""

# These standard-library helpers read archives, set executable permission, and
# supply a small recording client in place of the network connection.
import tarfile
from types import SimpleNamespace

# pytest checks expected failures. The real Hermes manager controls which files
# are sent and whether a failed upload is remembered as successful.
import pytest
from tools.environments.base import EnvironmentConnectionError
from tools.environments.file_sync import FileSyncManager

from hermes_render.environment import RenderEnvironment


def recorder():
    """Create an unconnected provider and record exactly what it would upload."""
    # Skip the paid-resource constructor. Only the archive callback is exercised.
    env = RenderEnvironment.__new__(RenderEnvironment)
    env.sandbox_id = "sbx-test"
    env._sync_error = None
    archives, commands = [], []

    def copy_to(sandbox_id, local_path, remote_path):
        # Read while the temporary archive exists. Record bytes and modes so the
        # checks can catch a wrong mapping, missing file, or lost execute bit.
        assert sandbox_id == "sbx-test"
        with tarfile.open(local_path) as archive:
            archives.append(
                {
                    member.name: (archive.extractfile(member).read(), member.mode)
                    for member in archive.getmembers()
                }
            )

    env._api = SimpleNamespace(copy_to=copy_to)
    env._control = commands.append
    return env, archives, commands


def test_large_profile_uses_one_upload_and_preserves_destinations(tmp_path):
    # Model a normal skill library with hundreds of files, including Unicode and
    # an explicitly mapped file outside .hermes. Every file has different bytes.
    env, archives, commands = recorder()
    files = []
    expected = {}
    for index in range(328):
        path = tmp_path / str(index)
        value = f"skill {index}: λ".encode()
        path.write_bytes(value)
        path.chmod(0o700 if index == 0 else 0o600)
        remote = f"/root/.hermes/skills/group {index}/λ.md"
        files.append((str(path), remote))
        expected[remote.lstrip("/")] = (value, 0o700 if index == 0 else 0o600)
    files.append((str(tmp_path / "0"), "/opt/service/config.json"))
    expected["opt/service/config.json"] = (b"skill 0: \xce\xbb", 0o700)
    env._bulk_upload(files)
    # One archive and one extraction replaces hundreds of separate transfers.
    assert archives == [expected]
    assert len(commands) == 1 and "--no-same-owner" in commands[0]
    assert "rm -f" in commands[0]


@pytest.mark.parametrize("destination", ["relative.txt", "/root/../escape", "/", "/bad\x00name"])
def test_invalid_destination_never_uploads(tmp_path, destination):
    # A malformed mapping must fail before any network operation happens.
    env, archives, commands = recorder()
    path = tmp_path / "source"
    path.write_text("example")
    with pytest.raises(EnvironmentConnectionError, match="batch file transfer failed"):
        env._bulk_upload([(str(path), destination)])
    assert archives == [] and commands == []


def test_failed_batch_is_sanitized_and_retried_by_real_manager(tmp_path):
    # A failed extraction must not mark files as synchronized. Use Hermes's real
    # bookkeeping to prove a subsequent synchronization sends the same data again.
    env, archives, commands = recorder()
    path = tmp_path / "source"
    path.write_text("expected")
    files = [(str(path), "/root/.hermes/skills/a.md")]

    def fail(command):
        raise RuntimeError("secret-example-value")

    env._control = fail
    env._sync_manager = FileSyncManager(
        get_files_fn=lambda: files,
        upload_fn=lambda *args: pytest.fail("Individual upload should not run"),
        delete_fn=lambda paths: None,
        bulk_upload_fn=env._bulk_upload,
        sync_interval=0,
    )
    with pytest.raises(EnvironmentConnectionError) as error:
        env._sync()
    assert "secret-example-value" not in str(error.value)
    assert env._sync_manager._synced_files == {}
    # Restoring transport allows a retry. A later unchanged cycle sends nothing.
    env._control = commands.append
    env._sync()
    env._sync()
    assert len(archives) == 2 and archives[0] == archives[1]
    assert len(commands) == 1
