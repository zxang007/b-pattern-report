"""验证版本筛选、进程范围和原子切换失败回退。"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import mock_open, patch

import updater


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


if __name__ == "__main__":
    unittest.main()
