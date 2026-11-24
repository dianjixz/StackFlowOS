"""DIST: mDNS codec, peer state machine and discovery extension (no multicast)."""

from __future__ import annotations

import os
import socket
import struct
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from stackflowos.transport import discovery as d

SERVICE = d.DEFAULT_SERVICE_TYPE
INSTANCE = f"node-a.{SERVICE}"
HOST = "node-a.local"


def _response(records, *, questions=()) -> bytes:
    return d.build_response(answers=records, questions=questions)


def _full_response() -> bytes:
    return _response([
        d.ptr_record(SERVICE, INSTANCE, ttl=120),
        d.srv_record(INSTANCE, HOST, 17820, ttl=120),
        d.txt_record(INSTANCE, {
            "node_id": "node-a",
            "transport_rpc": "tcp://127.0.0.1:17820",
            "http_port": "17800",
            "version": "0.1.0",
            "projects": "3",
        }),
        d.a_record(HOST, "192.168.1.5"),
        d.aaaa_record(HOST, "fe80::1"),
    ])


class CodecTests(unittest.TestCase):
    def test_name_roundtrip(self):
        for name in (SERVICE, INSTANCE, HOST, "a.b.c", "x"):
            encoded = d.encode_name(name)
            decoded, offset = d.decode_name(encoded, 0)
            self.assertEqual(decoded, d.normalize_name(name))
            self.assertEqual(offset, len(encoded))
        self.assertEqual(d.encode_name(""), b"\x00")

    def test_long_label_is_rejected(self):
        with self.assertRaises(d.DnsFormatError):
            d.encode_name("x" * 64 + ".local")

    def test_compressed_name_is_decoded(self):
        question = d.encode_name(SERVICE) + struct.pack("!HH", d.TYPE_PTR, 1)
        target = d.encode_name(INSTANCE)
        rdata_offset = 12 + len(question)
        self.assertTrue(rdata_offset > 12)
        record = (b"\xc0\x0c" + struct.pack("!HHIH", d.TYPE_PTR, 1, 120,
                                           len(target)) + target)
        packet = struct.pack("!HHHHHH", 0, 0x8400, 1, 1, 0, 0) + question + record
        message = d.parse_message(packet)
        self.assertEqual(message.questions[0].name, d.normalize_name(SERVICE))
        self.assertEqual(message.answers[0].name, d.normalize_name(SERVICE))
        self.assertEqual(message.answers[0].data, d.normalize_name(INSTANCE))

    def test_pointer_loop_is_rejected(self):
        packet = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0)
        packet += b"\xc0\x0c" + struct.pack("!HHIH", d.TYPE_PTR, 1, 120, 0)
        with self.assertRaises(d.DnsFormatError):
            d.parse_message(packet)

    def test_all_record_types_roundtrip(self):
        records = [
            d.ptr_record(SERVICE, INSTANCE),
            d.srv_record(INSTANCE, HOST, 17820, priority=1, weight=2),
            d.txt_record(INSTANCE, ["a=1", "b=two"]),
            d.a_record(HOST, "10.0.0.9"),
            d.aaaa_record(HOST, "2001:db8::1"),
        ]
        message = d.parse_message(_response(records))
        by_type = {record.rtype: record for record in message.answers}
        self.assertEqual(by_type[d.TYPE_PTR].data, d.normalize_name(INSTANCE))
        srv = by_type[d.TYPE_SRV].data
        self.assertEqual((srv["port"], srv["priority"], srv["weight"]),
                         (17820, 1, 2))
        self.assertEqual(srv["target"], d.normalize_name(HOST))
        self.assertEqual(by_type[d.TYPE_TXT].data, ["a=1", "b=two"])
        self.assertEqual(by_type[d.TYPE_A].data, "10.0.0.9")
        self.assertEqual(by_type[d.TYPE_AAAA].data, "2001:db8::1")

    def test_malformed_packets_are_rejected(self):
        for packet in (b"", b"\x00" * 4,
                       struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0),
                       struct.pack("!HHHHHH", 0, 0, 0, 1, 0, 0) + b"\x00" * 4):
            with self.subTest(packet=packet):
                with self.assertRaises(d.DnsFormatError):
                    d.parse_message(packet)

    def test_query_roundtrip(self):
        message = d.parse_message(d.build_query(SERVICE, d.TYPE_PTR))
        self.assertEqual(message.questions[0].qtype, d.TYPE_PTR)
        self.assertEqual(message.questions[0].name, d.normalize_name(SERVICE))


class StateMachineTests(unittest.TestCase):
    def test_full_announcement_builds_peer(self):
        clock = [1000.0]
        state = d.DiscoveryState(SERVICE, peer_ttl_s=120,
                                 now=lambda: clock[0])
        peers = state.handle_packet(_full_response())
        self.assertEqual(len(peers), 1)
        peer = peers[0]
        self.assertEqual(peer["name"], d.normalize_name(INSTANCE))
        self.assertEqual(peer["host"], d.normalize_name(HOST))
        self.assertEqual(peer["port"], 17820)
        self.assertEqual(peer["addresses"], ["192.168.1.5", "fe80::1"])
        self.assertEqual(peer["node_id"], "node-a")
        self.assertEqual(peer["transport_rpc"], "tcp://127.0.0.1:17820")
        self.assertEqual(peer["http_port"], 17800)
        self.assertEqual(peer["version"], "0.1.0")
        self.assertEqual(peer["projects"], 3)
        self.assertEqual(clock[0], 1000.0)

    def test_merge_across_messages(self):
        state = d.DiscoveryState(SERVICE, now=lambda: 5.0)
        state.handle_packet(_response([d.ptr_record(SERVICE, INSTANCE)]))
        state.handle_packet(_response([
            d.srv_record(INSTANCE, HOST, 9000),
            d.a_record(HOST, "10.1.2.3"),
        ]))
        peers = state.snapshot()
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]["port"], 9000)
        self.assertEqual(peers[0]["addresses"], ["10.1.2.3"])

    def test_ttl_expiry_and_goodbye(self):
        clock = [100.0]
        state = d.DiscoveryState(SERVICE, peer_ttl_s=120,
                                 now=lambda: clock[0])
        state.handle_packet(_response([d.ptr_record(SERVICE, INSTANCE, ttl=30)]))
        self.assertEqual(len(state.snapshot()), 1)
        clock[0] += 31
        self.assertEqual(state.snapshot(), [])
        state.handle_packet(_response([d.ptr_record(SERVICE, INSTANCE, ttl=30)]))
        self.assertEqual(len(state.snapshot()), 1)
        state.handle_packet(_response([d.ptr_record(SERVICE, INSTANCE, ttl=0)]))
        self.assertEqual(state.snapshot(), [])

    def test_other_services_are_ignored(self):
        state = d.DiscoveryState(SERVICE, now=lambda: 1.0)
        state.handle_packet(_response([
            d.ptr_record("_other._tcp.local.", "x._other._tcp.local."),
        ]))
        self.assertEqual(state.snapshot(), [])

    def test_malformed_packet_counts_error_and_returns_snapshot(self):
        state = d.DiscoveryState(SERVICE, now=lambda: 1.0)
        self.assertEqual(state.handle_packet(b"not dns"), [])
        self.assertGreaterEqual(state.errors, 1)


class DiscoverHubsTests(unittest.TestCase):
    def test_discover_hubs_with_injected_socket(self):
        class FakeSocket:
            def __init__(self):
                self.sent = []
                self.replies = [_full_response()]
                self.timeout = 48
                self.closed = False

            def sendto(self, data, addr):
                self.sent.append((data, addr))

            def settimeout(self, value):
                self.timeout = value

            def recvfrom(self, size):
                if self.replies:
                    return self.replies.pop(0), ("127.0.0.1", 5353)
                raise socket.timeout()

            def close(self):
                self.closed = True

        fake = FakeSocket()
        peers = d.discover_hubs(0.3, socket_factory=lambda: fake)
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]["node_id"], "node-a")
        self.assertTrue(fake.sent, "a PTR query must be sent")
        self.assertTrue(fake.closed)

    def test_discover_hubs_degrades_when_socket_fails(self):
        def broken():
            raise d.MdnsError("no multicast route")

        self.assertEqual(d.discover_hubs(0.1, socket_factory=broken), [])

    @unittest.skipUnless(os.environ.get("SFO_TEST_MDNS") == "1",
                         "real multicast I/O is environment dependent")
    def test_real_multicast_smoke(self):  # pragma: no cover - opt in
        # This only asserts the call does not raise; CI leaves it skipped.
        peers = d.discover_hubs(0.5)
        self.assertIsInstance(peers, list)


class _FakeApp:
    version = "0.1.0"

    def __init__(self, discovery: str = "off", advertise: bool = True):
        self.config = SimpleNamespace(
            cluster=SimpleNamespace(discovery=discovery,
                                    service_type=SERVICE,
                                    advertise=advertise),
            web=SimpleNamespace(port=17800),
            transport=SimpleNamespace(advertise_host=""),
            system=SimpleNamespace(host_label="test-host"),
        )
        self.sys_addresses = SimpleNamespace(rpc="tcp://127.0.0.1:17820")
        self.projects = SimpleNamespace(list=lambda: [1, 2, 3])


class DiscoveryExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_is_inert(self):
        extension = d.build(_FakeApp("off"))
        await extension.start()
        try:
            health = extension.health()
            self.assertEqual(health["peers"], 0)
            self.assertEqual(health["mode"], "off")
            self.assertFalse(health["advertising"])
            route = [item for item in extension.routes()
                     if item[0] == "GET" and item[1] == r"/api/discovery/peers"]
            self.assertEqual(len(route), 1)
            status, payload = await extension.peers_route(None, {}, {})
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["peers"], [])
        finally:
            await extension.stop()

    async def test_socket_failures_are_isolated(self):
        extension = d.build(_FakeApp("mdns"))
        with mock.patch.object(d, "_open_mdns_socket",
                               side_effect=d.MdnsError("no route")):
            await extension.start()
        try:
            health = extension.health()
            self.assertEqual(health["mode"], "mdns")
            self.assertFalse(health["advertising"])
            self.assertFalse(health["browsing"])
            self.assertGreaterEqual(health["errors"], 1)
        finally:
            await extension.stop()

    async def test_on_config_restarts_only_on_change(self):
        extension = d.build(_FakeApp("off"))
        await extension.start()
        try:
            await extension.on_config(extension.app.config)  # unchanged: no-op
            self.assertEqual(extension.mode, "off")
            new_config = _FakeApp("off").config
            new_config.cluster.discovery = "mdns"
            with mock.patch.object(d, "_open_mdns_socket",
                                   side_effect=d.MdnsError("no route")):
                await extension.on_config(new_config)
            self.assertEqual(extension.mode, "mdns")
        finally:
            await extension.stop()

    async def test_peer_filtering_excludes_self(self):
        extension = d.build(_FakeApp("mdns", advertise=True))
        # Pretend we advertised and a browser collected our own instance.
        extension._instance = d.normalize_name(INSTANCE)
        extension._state.handle_packet(_response([
            d.ptr_record(SERVICE, INSTANCE),
            d.txt_record(INSTANCE, {"node_id": "node-a"}),
        ]))
        self.assertEqual(extension.peers(), [])
        extension._state.handle_packet(_response([
            d.ptr_record(SERVICE, "node-b." + SERVICE),
            d.txt_record("node-b." + SERVICE, {"node_id": "node-b"}),
        ]))
        self.assertEqual([peer["node_id"] for peer in extension.peers()],
                         ["node-b"])


if __name__ == "__main__":
    unittest.main()
