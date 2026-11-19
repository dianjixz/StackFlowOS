import asyncio
import unittest

from stackflowos.kernel import Context, EventBus, Service, is_bailed


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_provide_inherit_override_isolate(self):
        root = Context(name="root")
        child = root.child("child")
        root.provide("svc", "root-value")
        self.assertEqual(child.get("svc"), "root-value")
        child.provide("svc", "child-value")
        self.assertEqual(child.get("svc"), "child-value")
        self.assertEqual(root.get("svc"), "root-value")

        isolated = child.isolate("svc")
        with self.assertRaises(KeyError):
            isolated.get("svc")
        self.assertEqual(isolated.get("svc", "fallback"), "fallback")

    async def test_effect_runs_in_reverse_order(self):
        ctx = Context(name="root")
        order = []
        ctx.effect(lambda: order.append("a") or (lambda: order.append("a-down")))
        ctx.effect(lambda: order.append("b") or (lambda: order.append("b-down")))
        await ctx.dispose()
        self.assertEqual(order, ["a", "b", "b-down", "a-down"])

    async def test_effect_dispose_is_idempotent(self):
        ctx = Context(name="root")
        calls = []
        dispose = ctx.effect(lambda: (lambda: calls.append(1)))
        dispose()
        dispose()
        await ctx.dispose()
        self.assertEqual(calls, [1])

    async def test_effect_after_dispose_raises(self):
        ctx = Context(name="root")
        await ctx.dispose()
        with self.assertRaises(RuntimeError):
            ctx.effect(lambda: None)

    async def test_children_disposed_before_parent(self):
        root = Context(name="root")
        events = []
        child = root.child("child")
        child.effect(lambda: (lambda: events.append("child")))
        root.effect(lambda: (lambda: events.append("root")))
        await root.dispose()
        self.assertEqual(events, ["child", "root"])

    async def test_listener_removed_with_context(self):
        root = Context(name="root")
        child = root.child("sub")
        seen = []
        child.on("ping", lambda payload: seen.append(payload))
        root.emit("ping", 1)
        self.assertEqual(seen, [1])
        await child.dispose()
        root.emit("ping", 2)
        self.assertEqual(seen, [1])

    async def test_plugin_registers_and_disposes(self):
        root = Context(name="root")
        seen = []

        def plugin(ctx, config):
            ctx.provide("greeting", config["text"])
            ctx.on("hello", lambda p: seen.append(p))
            return lambda: seen.append("disposed")

        handle = root.plugin(plugin, {"text": "hi"})
        await handle.ready()
        self.assertEqual(root.get("greeting"), "hi")
        root.emit("hello", "x")
        await handle.dispose()
        self.assertEqual(seen, ["x", "disposed"])
        with self.assertRaises(KeyError):
            root.get("greeting")

    async def test_async_plugin(self):
        root = Context(name="root")
        flags = []

        async def plugin(ctx, config):
            await asyncio.sleep(0)
            flags.append("started")
            ctx.provide("async-svc", 42)
            return lambda: flags.append("stopped")

        handle = root.plugin(plugin)
        await handle.ready()
        self.assertEqual(root.get("async-svc"), 42)
        await handle.dispose()
        self.assertEqual(flags, ["started", "stopped"])

    async def test_service_base(self):
        class Foo(Service):
            provide = "foo"

            def __init__(self, ctx):
                super().__init__(ctx)

        root = Context(name="root")
        foo = Foo(root)
        self.assertIs(root.get("foo"), foo)
        await root.dispose()
        with self.assertRaises(KeyError):
            root.get("foo")


class EventTests(unittest.IsolatedAsyncioTestCase):
    def test_emit_order_and_prepend(self):
        bus = EventBus()
        seen = []
        bus.on("e", lambda p: seen.append(("a", p)))
        bus.on("e", lambda p: seen.append(("b", p)), prepend=True)
        bus.emit("e", 1)
        self.assertEqual(seen, [("b", 1), ("a", 1)])

    def test_listener_error_is_isolated(self):
        bus = EventBus()
        seen = []

        def bad(payload):
            raise RuntimeError("boom")

        bus.on("e", bad)
        bus.on("e", lambda p: seen.append(p))
        bus.emit("e", 3)  # must not raise
        self.assertEqual(seen, [3])

    def test_on_any_observes(self):
        bus = EventBus()
        calls = []
        bus.on_any(lambda mode, name, payload: calls.append((mode, name, payload)))
        bus.emit("a", 1)
        asyncio.run(bus.parallel("b", 2))
        self.assertEqual(calls[0], ("emit", "a", 1))
        self.assertEqual(calls[1][:2], ("parallel", "b"))

    async def test_parallel_collects_errors(self):
        bus = EventBus()

        async def bad(payload):
            raise ValueError("x")

        async def good(payload):
            return payload + 1

        bus.on("e", bad)
        bus.on("e", good)
        with self.assertRaises(ExceptionGroup):
            await bus.parallel("e", 1)

    async def test_serial_and_bail(self):
        bus = EventBus()
        order = []

        async def first(payload):
            order.append("first")
            return None

        async def second(payload):
            order.append("second")
            return "stop"

        async def third(payload):
            order.append("third")
            return None

        for cb in (first, second, third):
            bus.on("e", cb)
        result = await bus.serial("e", 0)
        self.assertEqual(result, "stop")
        self.assertEqual(order, ["first", "second"])

        bus2 = EventBus()
        bus2.on("e", lambda p: False)
        bus2.on("e", lambda p: "bailed")
        bus2.on("e", lambda p: "never")
        self.assertEqual(bus2.bail("e"), "bailed")
        self.assertFalse(is_bailed(None))
        self.assertFalse(is_bailed(False))

    async def test_waterfall_next_and_veto(self):
        bus = EventBus()
        order = []

        async def outer(payload, next_):
            order.append("outer-in")
            result = await next_()
            order.append("outer-out")
            return result

        async def inner(payload, next_):
            order.append("inner")
            return ("done", payload)

        bus.on("e", outer)
        bus.on("e", inner)
        result = await bus.waterfall("e", 7)
        self.assertEqual(result, ("done", 7))
        self.assertEqual(order, ["outer-in", "inner", "outer-out"])

        veto = EventBus()

        async def stop(payload, next_):
            return "vetoed"

        async def never(payload, next_):
            raise AssertionError("should not run")

        veto.on("e", stop)
        veto.on("e", never)
        self.assertEqual(await veto.waterfall("e"), "vetoed")

    async def test_waterfall_next_twice_raises(self):
        bus = EventBus()

        async def twice(payload, next_):
            await next_()
            await next_()

        bus.on("e", twice)
        with self.assertRaises(RuntimeError):
            await bus.waterfall("e")


if __name__ == "__main__":
    unittest.main()
