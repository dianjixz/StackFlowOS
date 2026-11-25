"""Self-tests for the shared test infrastructure in ``tests/conftest.py``.

These pin the two properties the rest of the suite relies on:

* scratch directories are short, writable and outside the checkout (git tests
  need an isolated repository root);
* an over-long UNIX-socket path is skipped with an actionable reason instead
  of failing inside ``bind()``.

They also guard the packaging metadata: the dev extra must declare the test
runner the suite actually uses.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from tests.conftest import (
    IPC_PATH_LIMIT,
    REPO,
    TMP_ROOT,
    git_scratch_dir,
    ipc_dir_skip_reason,
    ipc_path_bytes,
    ipc_path_skip_reason,
    make_temp_dir,
    require_ipc_dir,
    require_ipc_path,
    tmp_root,
)


class _SkipRecorder:
    """Minimal ``unittest.TestCase`` stand-in for the skip helpers."""

    def __init__(self) -> None:
        self.reason: str | None = None

    def skipTest(self, reason: str) -> None:  # noqa: N802 - unittest spelling
        self.reason = reason
        raise unittest.SkipTest(reason)


class ScratchDirTests(unittest.TestCase):
    def test_shared_root_is_created_and_short(self) -> None:
        root = tmp_root()
        self.assertTrue(root.is_dir())
        self.assertEqual(root, TMP_ROOT)
        self.assertEqual(root, Path(os.environ.get("SFO_TEST_TMPDIR")
                                    or Path(tempfile.gettempdir()) / "sfo-tests"))

    def test_make_temp_dir_uses_explicit_root_and_prefix(self) -> None:
        base = make_temp_dir(prefix="w6-base-")
        self.addCleanup(shutil.rmtree, base, True)
        child = make_temp_dir(prefix="w6-child-", root=base)
        self.addCleanup(shutil.rmtree, child, True)

        self.assertTrue(base.is_dir())
        self.assertEqual(child.parent, base)
        self.assertTrue(child.name.startswith("w6-child-"))
        self.assertNotEqual(base, child)

    def test_git_scratch_dir_is_a_standalone_repo_root(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git binary is not available")
        scratch = git_scratch_dir(prefix="w6-git-")
        self.addCleanup(shutil.rmtree, scratch, True)
        self.assertTrue(scratch.is_dir())

        subprocess.run(["git", "init", "-q", str(scratch)], check=True)
        toplevel = subprocess.check_output(
            ["git", "-C", str(scratch), "rev-parse", "--show-toplevel"],
            text=True).strip()
        self.assertEqual(Path(toplevel).resolve(), scratch.resolve())
        # The helper is for throw-away repos.  It must not be nested inside the
        # checkout when the default (system temp) root is used.
        if "SFO_TEST_TMPDIR" not in os.environ:
            repo = REPO.resolve()
            self.assertFalse(scratch.resolve().is_relative_to(repo),
                             f"scratch dir must live outside {repo}")


class IpcPathHelperTests(unittest.TestCase):
    def test_short_socket_path_is_accepted(self) -> None:
        scratch = make_temp_dir(prefix="w6-sock-")
        self.addCleanup(shutil.rmtree, scratch, True)
        path = scratch / "probe.sock"
        self.assertLessEqual(ipc_path_bytes(path), IPC_PATH_LIMIT)
        self.assertIsNone(ipc_path_skip_reason(path))
        self.assertIsNone(ipc_dir_skip_reason(scratch))

        recorder = _SkipRecorder()
        require_ipc_path(recorder, path)
        require_ipc_dir(recorder, scratch)
        self.assertIsNone(recorder.reason)

    def test_path_at_the_limit_is_accepted_and_one_byte_over_is_skipped(self) -> None:
        # Build paths of exactly IPC_PATH_LIMIT and IPC_PATH_LIMIT + 1 bytes.
        prefix = "/tmp/"
        suffix = ".sock"
        pad = IPC_PATH_LIMIT - len(prefix) - len(suffix)
        at_limit = Path(prefix + "a" * pad + suffix)
        over_limit = Path(prefix + "a" * (pad + 1) + suffix)
        self.assertEqual(ipc_path_bytes(at_limit), IPC_PATH_LIMIT)
        self.assertEqual(ipc_path_bytes(over_limit), IPC_PATH_LIMIT + 1)

        self.assertIsNone(ipc_path_skip_reason(at_limit))
        reason = ipc_path_skip_reason(over_limit)
        self.assertIsNotNone(reason)
        self.assertIn(str(IPC_PATH_LIMIT), reason)
        self.assertIn("SFO_TEST_TMPDIR", reason)

        recorder = _SkipRecorder()
        with self.assertRaises(unittest.SkipTest):
            require_ipc_path(recorder, over_limit)
        self.assertIn(str(IPC_PATH_LIMIT), recorder.reason or "")

    def test_deep_run_dir_is_skipped_with_a_clear_reason(self) -> None:
        deep = Path("/tmp") / ("d" * (IPC_PATH_LIMIT + 20))
        reason = ipc_dir_skip_reason(deep)
        self.assertIsNotNone(reason)
        self.assertIn(str(IPC_PATH_LIMIT), reason)
        self.assertIn("shortest hashed socket name", reason)
        self.assertIn("--basetemp", reason)

        recorder = _SkipRecorder()
        with self.assertRaises(unittest.SkipTest):
            require_ipc_dir(recorder, deep)
        self.assertIn(str(IPC_PATH_LIMIT), recorder.reason or "")

    def test_helper_path_can_actually_bind_a_unix_socket(self) -> None:
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("platform has no AF_UNIX sockets")
        scratch = make_temp_dir(prefix="w6-bind-")
        self.addCleanup(shutil.rmtree, scratch, True)
        path = scratch / "probe.sock"
        require_ipc_path(self, path)  # skips instead of OSError when too deep

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
            listener.listen(1)
            self.assertTrue(path.exists())
        finally:
            listener.close()
            path.unlink(missing_ok=True)

    def test_limit_matches_the_transport_constant(self) -> None:
        try:
            from stackflowos.transport.address import MAX_IPC_PATH_BYTES
        except ModuleNotFoundError as exc:  # pynng not installed for this run
            self.skipTest(f"transport dependencies unavailable: {exc}")
        self.assertEqual(IPC_PATH_LIMIT, MAX_IPC_PATH_BYTES)


class DevExtrasTests(unittest.TestCase):
    def _pyproject(self) -> dict:
        return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))

    def test_dev_extra_declares_the_test_runner_and_subtests_plugin(self) -> None:
        dev = self._pyproject()["project"]["optional-dependencies"]["dev"]
        names = {re.split(r"[<>=!~;\[]", spec.strip(), maxsplit=1)[0].lower()
                 for spec in dev}
        self.assertIn("pytest", names)
        self.assertIn("pytest-subtests", names)

    def test_pytest_config_keeps_skip_reasons_visible(self) -> None:
        options = self._pyproject()["tool"]["pytest"]["ini_options"]
        self.assertEqual(options["testpaths"], ["tests"])
        self.assertIn("-rs", options["addopts"])

    def test_declared_dev_deps_are_importable_when_installed(self) -> None:
        import pytest  # noqa: PLC0415 - assert the installed runner, not metadata

        self.assertTrue(pytest.__version__)
        try:
            import pytest_subtests  # noqa: F401,PLC0415
        except ModuleNotFoundError:
            # Declared in the dev extra; the machine venv is pytest-only, so
            # report that honestly instead of failing or pretending it exists.
            self.skipTest("pytest-subtests is not installed in this interpreter "
                          "(declared in [project.optional-dependencies].dev)")
