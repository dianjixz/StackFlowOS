"""Tests for :mod:`stackflowos.transport.nng`.

The suite starts real pynng sockets on IPC sockets and on TCP ports >= 17900,
always under ``<repo>/.stackflowos/tmp``.  Every test tears its transports down
so no socket files or background tasks outlive it.
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
import time
import unittest
from pathlib import Path

import pynng

from stackflowos.errors import BadRequest, Denied, SfoError, Timeout, Unavailable
from stackflowos.protocol import EventRecord, now_ms
from stackflowos.transport import (
    AddressAllocator,
    EventPublisher,
    EventSubscriber,
    HubTransport,
    RpcClient,
    RpcServer,
    UnitAddresses,
    UnitTransport,
    cleanup_ipc_address,
    is_ipc,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TMP_ROOT = REPO_ROOT / ".stackflowos" / "tmp"


def _now() -> int:
    return now_ms()


class IpAddressHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(TMP_ROOT))
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_is_ipc(self) -> None:
        self.assertTrue(is_ipc("ipc:///tmp/x.rpc.sock"))
        self.assertFalse(is_ipc("tcp://127.0.0.1:17900"))
        self.assertFalse(is_ipc(""))
        self.assertFalse(is_ipc("inproc://x"))

    def test_cleanup_ipc_address(self) -> None:
        target = self.tmp / "stale.sock"
        target.write_text("stale")
        cleanup_ipc_address(f"ipc://{target}")
        self.assertFalse(target.exists())
        # Missing files are fine, non-ipc addresses are a no-op.
        cleanup_ipc_address(f"ipc://{target}")
        non_ipc = self.tmp / "other.sock"
        non_ipc.write_text("keep")
        cleanup_ipc_address(f"tcp://127.0.0.1:17900")
        self.assertTrue(non_ipc.exists())


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(TMP_ROOT))
        self.tmp = Path(self._tmp.name)
        self.run_dir = (self.tmp / "run").resolve()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------
    def ipc_allocator(self, name: str = "run") -> AddressAllocator:
        return AddressAllocator(mode="ipc", host="127.0.0.1",
                                run_dir=(self.tmp / name).resolve())

    def tcp_allocator(self, name: str = "tcp",
                      pool: tuple[int, int] = (17900, 17960)) -> AddressAllocator:
        return AddressAllocator(mode="tcp", host="127.0.0.1",
                                run_dir=(self.tmp / name).resolve(),
                                port_pool=pool)

    async def _start_rpc_pair(
        self, alloc: AddressAllocator, handlers: dict, *,
        contexts: int = 4, timeout: float = 2.0,
    ) -> tuple[RpcServer, RpcClient, UnitAddresses]:
        sys = alloc.ensure_sys()
        server = RpcServer(sys.rpc, contexts=contexts)
        for action, handler in handlers.items():
            server.register(action, handler)
        await server.start()
        client = RpcClient(sys.rpc, src="sys", dst="sys",
                           timeout=timeout, contexts=contexts)
        await client.start()
        return server, client, sys

    async def _publish_until(self, publishers_and_records, predicate,
                             *, timeout: float = 2.0) -> None:
        """Retry publishing until ``predicate()`` or the deadline expires.

        PUB/SUB has no delivery guarantee before the subscription handshake
        completes, so tests retry instead of sleeping for a fixed time.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return
            for publisher, record in publishers_and_records:
                publisher.publish(record)
            await asyncio.sleep(0.05)
        self.fail("event was not delivered before the timeout")

    # -- RPC --------------------------------------------------------------
    async def test_rpc_echo_over_ipc(self) -> None:
        alloc = self.ipc_allocator()
        handlers = {
            "echo": lambda env: {"data": env.data, "src": env.src,
                                 "action": env.action},
            "nothing": lambda env: None,
        }
        server, client, sys = await self._start_rpc_pair(alloc, handlers)
        try:
            payload = {"nested": {"list": [1, 2, {"ok": True}]}, "none": None}
            result = await client.call("echo", payload)
            self.assertEqual(result["data"], payload)
            self.assertEqual(result["src"], "sys")
            self.assertEqual(result["action"], "echo")
            self.assertEqual(await client.call("nothing"), {})
            self.assertTrue(is_ipc(sys.rpc))
        finally:
            await client.stop()
            await server.stop()
        # No stray socket files after an orderly shutdown.
        self.assertEqual(list(self.run_dir.iterdir()), [])

    async def test_rpc_error_mapping(self) -> None:
        def denied(env):
            raise Denied("permission refused", detail={"cap": "fs.write"})

        def boom(env):
            raise RuntimeError("kaboom")

        alloc = self.ipc_allocator()
        server, client, _sys = await self._start_rpc_pair(
            alloc, {"denied": denied, "boom": boom})
        try:
            with self.assertRaises(Denied) as caught:
                await client.call("denied", {"capability": "fs.write"})
            self.assertEqual(caught.exception.code, "DENIED")
            self.assertEqual(caught.exception.detail, {"cap": "fs.write"})

            with self.assertRaises(BadRequest) as caught:
                await client.call("does.not.exist")
            self.assertEqual(caught.exception.code, "BAD_REQUEST")

            with self.assertRaises(SfoError) as caught:
                await client.call("boom")
            self.assertEqual(caught.exception.code, "INTERNAL")
            self.assertIn("kaboom", caught.exception.message)
            self.assertNotIn("Traceback", caught.exception.message)
        finally:
            await client.stop()
            await server.stop()

    async def test_rpc_concurrent_calls_are_parallel(self) -> None:
        active = 0
        peak = 0

        async def slow(env):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.05)
            finally:
                active -= 1
            return {"i": env.data["i"]}

        alloc = self.ipc_allocator()
        server, client, _sys = await self._start_rpc_pair(
            alloc, {"slow": slow}, contexts=4)
        try:
            started = time.perf_counter()
            results = await asyncio.gather(
                *(client.call("slow", {"i": i}) for i in range(8)))
            elapsed = time.perf_counter() - started
        finally:
            await client.stop()
            await server.stop()

        self.assertEqual(sorted(result["i"] for result in results),
                         list(range(8)))
        # 8 calls to a 50ms handler finish in well under 2s, and more than one
        # handler ran at the same time: the REP contexts really multiplex.
        self.assertLess(elapsed, 2.0)
        self.assertGreaterEqual(peak, 2)

    async def test_rpc_echo_over_tcp(self) -> None:
        alloc = self.tcp_allocator()
        server, client, sys = await self._start_rpc_pair(
            alloc, {"echo": lambda env: {"data": env.data}})
        try:
            self.assertFalse(is_ipc(sys.rpc))
            result = await client.call("echo", {"transport": "tcp"})
            self.assertEqual(result, {"data": {"transport": "tcp"}})
        finally:
            await client.stop()
            await server.stop()

    async def test_rpc_call_after_server_stop_raises(self) -> None:
        alloc = self.ipc_allocator()
        server, client, _sys = await self._start_rpc_pair(
            alloc, {"echo": lambda env: {"ok": True}}, timeout=0.3)
        try:
            self.assertEqual(await client.call("echo"), {"ok": True})
            await server.stop()
            with self.assertRaises((Timeout, Unavailable)) as caught:
                await client.call("echo")
            self.assertIn(caught.exception.code, ("TIMEOUT", "UNAVAILABLE"))
        finally:
            await client.stop()

    async def test_rpc_server_bind_conflict_raises_unavailable(self) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        server = RpcServer(f"tcp://127.0.0.1:{port}")
        try:
            with self.assertRaises(Unavailable):
                await server.start()
        finally:
            holder.close()
            await server.stop()

    # -- events -----------------------------------------------------------
    async def test_events_delivered_over_ipc(self) -> None:
        alloc = self.ipc_allocator()
        sys = alloc.ensure_sys()
        records: list[EventRecord] = []
        subscriber = EventSubscriber(sys.events)
        subscriber.on_record(records.append)
        await subscriber.start()
        publisher = EventPublisher(sys.events, src="coder.1")
        await publisher.start()
        record = EventRecord(seq=1, type="task.updated", source="coder.1",
                             ts=_now(), data={"task_id": "t-1"},
                             project_id="p-1")
        try:
            await self._publish_until([(publisher, record)],
                                      lambda: bool(records))
        finally:
            await publisher.stop()
            await subscriber.stop()
        self.assertEqual(records[0].type, "task.updated")
        self.assertEqual(records[0].source, "coder.1")
        self.assertEqual(records[0].data, {"task_id": "t-1"})
        self.assertEqual(records[0].project_id, "p-1")

    async def test_events_delivered_over_tcp(self) -> None:
        alloc = self.tcp_allocator()
        sys = alloc.ensure_sys()
        records: list[EventRecord] = []
        subscriber = EventSubscriber(sys.events)
        subscriber.on_record(records.append)
        await subscriber.start()
        publisher = EventPublisher(sys.events, src="lead.2")
        await publisher.start()
        record = EventRecord(seq=2, type="agent.working", source="lead.2",
                             ts=_now(), data={"agent_id": "a-1"})
        try:
            await self._publish_until([(publisher, record)],
                                      lambda: bool(records))
        finally:
            await publisher.stop()
            await subscriber.stop()
        self.assertEqual(records[0].type, "agent.working")
        self.assertEqual(records[0].source, "lead.2")

    async def test_events_multiple_publishers_to_one_subscriber(self) -> None:
        alloc = self.ipc_allocator()
        sys = alloc.ensure_sys()
        records: list[EventRecord] = []
        subscriber = EventSubscriber(sys.events)
        subscriber.on_record(records.append)
        await subscriber.start()
        pub_a = EventPublisher(sys.events, src="coder.1")
        pub_b = EventPublisher(sys.events, src="coder.2")
        await pub_a.start()
        await pub_b.start()
        rec_a = EventRecord(seq=1, type="task.created", source="coder.1",
                            ts=_now(), data={"task_id": "t-1"})
        rec_b = EventRecord(seq=2, type="task.created", source="coder.2",
                            ts=_now(), data={"task_id": "t-2"})

        def both_seen() -> bool:
            return {record.source for record in records} >= {"coder.1", "coder.2"}

        try:
            await self._publish_until([(pub_a, rec_a), (pub_b, rec_b)],
                                      both_seen)
        finally:
            await pub_a.stop()
            await pub_b.stop()
            await subscriber.stop()
        by_source = {record.source: record for record in records}
        self.assertEqual(by_source["coder.1"].data, {"task_id": "t-1"})
        self.assertEqual(by_source["coder.2"].data, {"task_id": "t-2"})

    async def test_events_malformed_frame_is_skipped(self) -> None:
        alloc = self.ipc_allocator()
        sys = alloc.ensure_sys()
        records: list[EventRecord] = []
        subscriber = EventSubscriber(sys.events)
        subscriber.on_record(records.append)
        await subscriber.start()
        publisher = EventPublisher(sys.events, src="coder.1")
        await publisher.start()
        garbage = pynng.Pub0(dial=sys.events, block_on_dial=False)
        record = EventRecord(seq=3, type="unit.online", source="coder.1",
                             ts=_now(), data={"work_id": "coder.1"})
        try:
            await asyncio.sleep(0.1)  # let the raw publisher's pipe attach
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2.0
            while not records and loop.time() < deadline:
                garbage.send(b"\xff\xfe this is not an envelope")
                publisher.publish(record)
                await asyncio.sleep(0.05)
        finally:
            garbage.close()
            await publisher.stop()
            await subscriber.stop()
        self.assertTrue(records, "subscriber stopped after a malformed frame")
        self.assertEqual(records[0].type, "unit.online")
        self.assertFalse(any(record.type == "malformed" for record in records))

    # -- hub <-> unit -----------------------------------------------------
    async def test_hub_unit_handshake_over_ipc(self) -> None:
        alloc = self.ipc_allocator("handshake")
        sys = alloc.ensure_sys()
        unit_id = alloc.new_work_id("coder")
        unit_addresses = alloc.allocate(unit_id)

        hub = HubTransport(sys_addresses=sys, timeout=3.0)
        hub.register_action("sys.ping", lambda env: {
            "ok": True, "action": env.action, "from": env.src})
        hub_events: asyncio.Queue[EventRecord] = asyncio.Queue()

        async def on_event(record: EventRecord) -> None:
            await hub_events.put(record)

        hub.on_event(on_event)
        await hub.start()

        unit = UnitTransport(sys_rpc=sys.rpc, work_id=unit_id,
                             addresses=unit_addresses,
                             events_target=sys.events)
        unit.register_action("unit.status", lambda env: {
            "work_id": unit_id, "step": env.data.get("step", 0)})
        await unit.start()
        hub.add_unit(unit_id, unit_addresses.rpc)
        try:
            # hub -> worker RPC
            result = await hub.call(unit_id, "unit.status", {"step": 3})
            self.assertEqual(result, {"work_id": unit_id, "step": 3})

            # worker -> hub RPC
            pong = await unit.call("sys.ping", {})
            self.assertTrue(pong["ok"])
            self.assertEqual(pong["action"], "sys.ping")
            self.assertEqual(pong["from"], unit_id)

            # worker -> hub events (the unit dials the hub's listener)
            record = EventRecord(seq=1, type="task.updated", source=unit_id,
                                 ts=_now(), data={"task_id": "t-7"},
                                 project_id="p-1")
            deadline = asyncio.get_running_loop().time() + 2.0
            while hub_events.empty() and asyncio.get_running_loop().time() < deadline:
                unit.publish(record)
                await asyncio.sleep(0.05)
            received = await asyncio.wait_for(hub_events.get(), 1.0)
            self.assertEqual(received.type, "task.updated")
            self.assertEqual(received.source, unit_id)
            self.assertEqual(received.data, {"task_id": "t-7"})
            self.assertEqual(received.project_id, "p-1")

            # The unit's own events endpoint stays unbound in this version.
            self.assertFalse(
                Path(unit_addresses.events[len("ipc://"):]).exists())

            # Dropping a unit makes subsequent calls fail fast.
            hub.remove_unit(unit_id)
            with self.assertRaises(Unavailable):
                await hub.call(unit_id, "unit.status")
            # ... and re-adding it brings the link back without a restart.
            hub.add_unit(unit_id, unit_addresses.rpc)
            again = await hub.call(unit_id, "unit.status", {"step": 9})
            self.assertEqual(again["step"], 9)
        finally:
            await unit.stop()
            await hub.stop()

        self.assertEqual(list((self.tmp / "handshake").iterdir()), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
