"""Operator control socket: 0600, SO_PEERCRED same-uid, fresh URL minting."""

from __future__ import annotations

import asyncio
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stackflowos.config import default_root
from stackflowos.security import control
from stackflowos.web.server import WebServer

from tests.test_sec_support import FakeApp, make_assets, raw_request

REPO = default_root()


class ControlSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.app = FakeApp(self.tmp)
        self.extension = control.build(self.app)
        self.socket_path = control.control_socket_path(self.app.paths.run_dir)
        self.assets = make_assets(self.tmp)
        await self.extension.start()
        self.server = WebServer(self.app, host="127.0.0.1", port=0,
                                assets_dir=self.assets)
        await self.server.start()
        self.port = self.server.port

    async def asyncTearDown(self):
        await self.server.stop()
        await self.extension.stop()
        await self.app.eventlog.close()
        self._tmp.cleanup()

    async def query(self, action: str = "url", **kwargs) -> dict:
        return await asyncio.to_thread(
            control.query_control_socket, self.socket_path, action, **kwargs)

    async def test_socket_is_private_unix_socket(self):
        info = self.socket_path.lstat()
        self.assertTrue(stat.S_ISSOCK(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(info.st_uid, os.getuid())
        self.assertEqual(self.socket_path,
                         Path(self.app.paths.run_dir) / "control.sock")

    async def test_ping_and_status(self):
        self.assertTrue((await self.query("ping"))["ok"])
        reply = await self.query("status")
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["data"]["running"])
        self.assertTrue(reply["data"]["tls"] is False)

    async def test_url_returns_fresh_single_use_link(self):
        first = await self.query("url")
        second = await self.query("url")
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertNotEqual(first["url"], second["url"])
        token = first["url"].split("token=", 1)[1]
        status, _, _ = await raw_request(self.port, "GET", f"/?token={token}")
        self.assertEqual(status, 303)
        # The minted token is single-use, like every launch link.
        status, _, _ = await raw_request(self.port, "GET", f"/?token={token}")
        self.assertEqual(status, 401)

    async def test_unknown_action_is_refused(self):
        reply = await self.query("shutdown")
        self.assertFalse(reply["ok"])

    async def test_different_uid_is_refused(self):
        real = control.peer_credentials
        expected = os.getuid()
        with mock.patch.object(control, "peer_credentials",
                               lambda sock: (4242, expected + 1, expected)):
            reply = await self.query("url")
        self.assertFalse(reply["ok"])
        self.assertIn("permission", reply["error"])
        self.assertIs(control.peer_credentials, real)

    async def test_peer_credentials_returns_same_uid(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            pid, uid, gid = control.peer_credentials(left)
            self.assertGreater(pid, 0)
            self.assertEqual(uid, os.getuid())
            self.assertEqual(gid, os.getgid())
        finally:
            left.close()
            right.close()

    async def test_request_login_url_helper(self):
        url = await asyncio.to_thread(
            control.request_login_url, self.app.paths.run_dir)
        self.assertIsNotNone(url)
        self.assertIn("/?token=", url)


class ControlFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_url_before_web_attachment_is_not_ready(self):
        app = FakeApp(self.tmp)
        extension = control.build(app)
        await extension.start()
        try:
            path = control.control_socket_path(app.paths.run_dir)
            reply = await asyncio.to_thread(
                control.query_control_socket, path, "url")
            self.assertFalse(reply["ok"])
            self.assertIn("not ready", reply["error"])
        finally:
            await extension.stop()
            await app.eventlog.close()

    async def test_stale_socket_is_replaced(self):
        app = FakeApp(self.tmp)
        path = control.control_socket_path(app.paths.run_dir)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(path))
        stale.close()  # leaves a dead socket file behind
        self.assertTrue(path.exists())
        server = control.ControlServer(path)
        await server.start()
        try:
            reply = await asyncio.to_thread(
                control.query_control_socket, path, "ping")
            self.assertTrue(reply["ok"], reply)
        finally:
            await server.stop()
            await app.eventlog.close()

    async def test_non_socket_path_is_never_replaced(self):
        app = FakeApp(self.tmp)
        path = control.control_socket_path(app.paths.run_dir)
        path.write_text("operator file", "utf-8")
        with self.assertRaises(RuntimeError):
            await control.ControlServer(path).start()
        self.assertEqual(path.read_text("utf-8"), "operator file")

    async def test_missing_socket_returns_error_not_exception(self):
        reply = await asyncio.to_thread(
            control.query_control_socket, self.tmp / "nope.sock", "url")
        self.assertFalse(reply["ok"])
        self.assertIn("not available", reply["error"])


if __name__ == "__main__":
    unittest.main()
