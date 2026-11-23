"""S6: plugin disposer race during async startup.

A plugin's ``run`` task awaits the plugin factory; if the context is disposed
while that await is in flight, the disposer returned afterwards used to be
appended to an already-drained list and never called. ``dispose`` also used to
finish while a startup task was still running.
"""

import asyncio
import unittest
from unittest import mock

from stackflowos.kernel import Context
from stackflowos.kernel import context as kernel_context


class DisposeDuringStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_mount_dispose_waits_for_startup_and_cleans_late_target(self):
        root = Context(name="root")
        calls = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_plugin(ctx, config):
            started.set()
            await release.wait()
            return lambda: calls.append("disposed")

        handle = root.plugin(slow_plugin)
        await started.wait()

        disposing = asyncio.create_task(root.dispose())
        await asyncio.sleep(0)
        # dispose() must not finish while the plugin is still starting.
        self.assertTrue(root.disposed)
        self.assertFalse(disposing.done())

        release.set()
        await asyncio.wait_for(disposing, 2)

        self.assertEqual(calls, ["disposed"])
        self.assertEqual(handle.state, "disposed")
        self.assertEqual(handle.ctx._disposers, [])
        self.assertTrue(handle.ctx.disposed)
        self.assertFalse(root._start_tasks)

    async def test_handle_dispose_during_startup_disposes_late_target(self):
        root = Context(name="root")
        calls = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_plugin(ctx, config):
            started.set()
            await release.wait()
            return lambda: calls.append("disposed")

        handle = root.plugin(slow_plugin)
        await started.wait()

        disposing = asyncio.create_task(handle.dispose())
        await asyncio.sleep(0)
        self.assertTrue(handle.ctx.disposed)
        self.assertFalse(disposing.done())

        release.set()
        await asyncio.wait_for(disposing, 2)

        # The late target must be disposed immediately, not appended to the
        # already-cleared disposer list.
        self.assertEqual(calls, ["disposed"])
        self.assertEqual(handle.state, "disposed")
        self.assertEqual(handle.ctx._disposers, [])
        self.assertFalse(handle.ctx._start_tasks)

    async def test_dispose_after_startup_calls_disposer_once(self):
        root = Context(name="root")
        calls = []

        async def plugin(ctx, config):
            await asyncio.sleep(0)
            return lambda: calls.append("disposed")

        handle = root.plugin(plugin)
        await handle.ready()
        self.assertEqual(handle.state, "active")

        await root.dispose()
        self.assertEqual(calls, ["disposed"])
        self.assertTrue(handle.ctx.disposed)

        # Both paths are idempotent.
        await root.dispose()
        await handle.dispose()
        self.assertEqual(calls, ["disposed"])

    async def test_concurrent_dispose_during_startup_is_idempotent(self):
        root = Context(name="root")
        calls = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_plugin(ctx, config):
            started.set()
            await release.wait()
            return lambda: calls.append("disposed")

        handle = root.plugin(slow_plugin)
        await started.wait()

        first = asyncio.create_task(root.dispose())
        second = asyncio.create_task(root.dispose())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)

        self.assertEqual(calls, ["disposed"])
        self.assertEqual(handle.state, "disposed")
        self.assertEqual(root._disposers, [])

    async def test_dispose_after_startup_failure_does_not_raise(self):
        root = Context(name="root")

        async def broken(ctx, config):
            await asyncio.sleep(0)
            raise ValueError("boom")

        handle = root.plugin(broken)
        with self.assertRaises(ValueError):
            await handle.ready()
        self.assertEqual(handle.state, "failed")
        self.assertIsInstance(handle.error, ValueError)

        await root.dispose()  # must not raise
        self.assertTrue(root.disposed)
        self.assertEqual(handle.state, "failed")

    async def test_stuck_startup_is_cancelled_after_timeout(self):
        root = Context(name="root")
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def stuck(ctx, config):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return lambda: None

        handle = root.plugin(stuck)
        await started.wait()

        with mock.patch.object(kernel_context, "_STARTUP_TIMEOUT_S", 0.05):
            with self.assertLogs("sfo.root", level="WARNING") as captured:
                await asyncio.wait_for(root.dispose(), 2)

        self.assertTrue(cancelled.is_set())
        self.assertTrue(any("cancelling" in line for line in captured.output))
        self.assertTrue(root.disposed)
        self.assertEqual(handle.state, "disposed")


class SynchronousPluginTests(unittest.TestCase):
    def test_sync_plugin_path_is_unchanged(self):
        root = Context(name="root")
        calls = []

        def plugin(ctx, config):
            return lambda: calls.append("disposed")

        handle = root.plugin(plugin)
        self.assertIsNone(handle._task)
        self.assertEqual(handle.state, "active")

        asyncio.run(root.dispose())
        self.assertEqual(calls, ["disposed"])
        self.assertEqual(handle.state, "disposed")


if __name__ == "__main__":
    unittest.main()
