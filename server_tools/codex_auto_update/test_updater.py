"""验证版本筛选、进程范围和原子切换失败回退。"""

from pathlib import Path
from contextlib import ExitStack, contextmanager
import io
import json
import os
import tarfile
import tempfile
import unittest
from unittest.mock import mock_open, patch

import updater


@contextmanager
def retention_environment():
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        root = Path(directory)
        for name in ("PACKAGES", "BACKUPS", "LEGACY_BACKUPS", "LEGACY_PACKAGE", "NPM_CACHE"):
            path = root / name.lower()
            path.mkdir()
            stack.enter_context(patch.object(updater, name, path))
        command = root / "codex"
        stack.enter_context(patch.object(updater, "COMMAND", command))
        stack.enter_context(patch.object(updater, "running_executables", return_value=[]))
        yield command


def fake_install(version, stamp):
    package = updater.PACKAGES / f"{version}-{stamp}"
    package.mkdir()
    (package / ".codex-managed.json").write_text(json.dumps({"version": version}))
    (package / "codex").touch()
    return package


def fake_archive(parent, version, stamp, *, legacy=False):
    path = parent / f"codex-{version}-{stamp}.tar.gz"
    content = json.dumps({"name": "@openai/codex", "version": version}).encode()
    with tarfile.open(path, "w:gz") as bundle:
        member = tarfile.TarInfo(("@openai/" if legacy else "") + "codex/package.json")
        member.size = len(content)
        bundle.addfile(member, io.BytesIO(content))
    return path


class UpdaterTests(unittest.TestCase):
    def test_accept_stable_and_compare_numerically(self):
        self.assertGreater(updater.version_tuple("0.165.0"), updater.version_tuple("0.99.0"))
        for version in ("0.166.0-alpha.1", "latest", "0.165", "01.2.3", "1.2.3\n"):
            with self.assertRaises(ValueError):
                updater.version_tuple(version)

    def test_cli_scope_excludes_independent_services(self):
        with patch.object(updater, "PACKAGES", Path("/opt/codex-auto-update/packages")):
            self.assertTrue(updater.managed_process("/usr/local/lib/node_modules/@openai/codex/vendor/codex (deleted)", "0::/user.slice"))
            self.assertTrue(updater.managed_process("/opt/codex-auto-update/packages/0.165.0/bin/codex", "0::/user.slice"))
            self.assertFalse(updater.managed_process("/opt/qqbot/codex-cache/bin/codex", "0::/system.slice/codex-mobile.service"))
            self.assertFalse(updater.managed_process("/usr/local/lib/node_modules/@openai/codex/vendor/codex", "0::/system.slice/qqbot-codex.service"))
            self.assertFalse(updater.managed_process("/usr/local/lib/node_modules/@openai/codex-other/bin/codex", "0::/user.slice"))

    def test_activation_rollback_preserves_original_relative_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = root / "codex"
            command.symlink_to("old/bin/codex")
            with patch.object(updater, "COMMAND", command), patch.object(updater, "verify", side_effect=RuntimeError("模拟验证失败")):
                with self.assertRaises(RuntimeError):
                    updater.activate(root / "new/bin/codex", "0.166.0")
            self.assertEqual(command.readlink(), Path("old/bin/codex"))
            self.assertFalse(list(root.glob(".codex-auto-update-*")))

    def test_activation_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = root / "codex"
            command.symlink_to("old/bin/codex")
            target = root / "new/bin/codex"
            with patch.object(updater, "COMMAND", command), patch.object(updater, "verify") as verify:
                updater.activate(target, "0.166.0")
                verify.assert_called_once_with(command, "0.166.0")
            self.assertEqual(command.readlink(), target)

    def test_install_failure_prevents_activation_and_session_closure(self):
        with patch.object(updater.sys, "argv", ["updater.py"]), \
             patch.object(updater.os, "geteuid", return_value=0), \
             patch.object(updater, "open", mock_open(), create=True), \
             patch.object(updater.fcntl, "flock"), \
             patch.object(updater, "installed_version", return_value="0.165.0"), \
             patch.object(updater, "run", return_value='"0.166.0"'), \
             patch.object(updater, "backup_current", return_value="backup.tar.gz"), \
             patch.object(updater, "stage", side_effect=RuntimeError("模拟安装失败")), \
             patch.object(updater, "activate") as activate, \
             patch.object(updater, "close_old_sessions") as close:
            with self.assertRaises(RuntimeError):
                updater.main()
            activate.assert_not_called()
            close.assert_not_called()

    def test_old_session_receives_termination(self):
        identity = ("10", 1, 2)
        checks = iter([True, False, False, False, False])
        with patch.object(updater, "old_sessions", return_value=[(123, identity, "0.163.0")]), \
             patch.object(updater, "same_process", side_effect=lambda *args: next(checks, False)), \
             patch.object(updater.os, "kill") as kill:
            updater.close_old_sessions("0.165.0")
            kill.assert_called_once_with(123, updater.signal.SIGTERM)

    def test_reused_pid_is_not_signaled(self):
        identity = ("10", 1, 2)
        with patch.object(updater, "old_sessions", return_value=[(123, identity, "0.163.0")]), patch.object(updater, "same_process", return_value=False), patch.object(updater.os, "kill") as kill:
            updater.close_old_sessions("0.165.0")
            kill.assert_not_called()

    def test_retention_keeps_only_previous_distinct_version(self):
        with retention_environment() as command:
            current = fake_install("0.165.0", "current")
            older = fake_install("0.163.0", "older")
            previous = fake_install("0.164.0", "previous")
            command.symlink_to(current / "codex")
            legacy = updater.LEGACY_PACKAGE
            (legacy / "package.json").write_text(json.dumps({"name": "@openai/codex", "version": "0.165.0"}))
            duplicate = fake_archive(updater.BACKUPS, "0.165.0", "duplicate")
            too_old = fake_archive(updater.BACKUPS, "0.163.0", "older")
            retained = fake_archive(updater.LEGACY_BACKUPS, "0.164.0", "previous", legacy=True)
            os.utime(too_old, (1000, 1000))
            os.utime(retained, (100, 100))
            unrelated = updater.BACKUPS / "unrelated.txt"
            unrelated.touch()
            cache = updater.NPM_CACHE / "_cacache"
            cache.mkdir()
            (cache / "old-package").touch()
            updater.prune("0.165.0")
            self.assertTrue(current.exists())
            self.assertFalse(older.exists())
            self.assertFalse(previous.exists())
            self.assertFalse(legacy.exists())
            self.assertFalse(duplicate.exists())
            self.assertFalse(too_old.exists())
            self.assertEqual(list(updater.BACKUPS.glob("*.tar.gz")), [updater.BACKUPS / retained.name])
            self.assertTrue(unrelated.exists())
            self.assertFalse(cache.exists())
            updater.prune("0.165.0")
            self.assertTrue(command.exists())

    def test_corrupt_previous_archive_prevents_cleanup(self):
        with retention_environment() as command:
            current = fake_install("0.165.0", "current")
            previous = fake_install("0.164.0", "previous")
            command.symlink_to(current / "codex")
            archive = updater.BACKUPS / "codex-0.164.0-corrupt.tar.gz"
            archive.write_bytes(b"invalid")
            duplicate = fake_archive(updater.BACKUPS, "0.165.0", "duplicate")
            with self.assertRaises(tarfile.ReadError):
                updater.prune("0.165.0")
            self.assertTrue(current.exists())
            self.assertTrue(previous.exists())
            self.assertTrue(duplicate.exists())

    def test_in_use_installation_waits_for_exit(self):
        with retention_environment() as command:
            current = fake_install("0.165.0", "current")
            previous = fake_install("0.164.0", "previous")
            command.symlink_to(current / "codex")
            fake_archive(updater.BACKUPS, "0.164.0", "previous")
            with patch.object(updater, "running_executables", return_value=[previous / "codex"]):
                updater.prune("0.165.0")
            self.assertTrue(previous.exists())
            updater.prune("0.165.0")
            self.assertFalse(previous.exists())

    def test_missing_previous_archive_preserves_installation(self):
        with retention_environment() as command:
            current = fake_install("0.165.0", "current")
            previous = fake_install("0.164.0", "previous")
            command.symlink_to(current / "codex")
            fake_archive(updater.BACKUPS, "0.163.0", "too-old")
            with self.assertRaises(RuntimeError):
                updater.prune("0.165.0")
            self.assertTrue(previous.exists())

    def test_previous_archive_metadata_must_match_version(self):
        with retention_environment():
            archive = fake_archive(updater.BACKUPS, "0.164.0", "previous")
            with self.assertRaises(RuntimeError):
                updater.validate_archive(archive, "0.163.0")


if __name__ == "__main__":
    unittest.main()
