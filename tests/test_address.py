"""Tests for :mod:`stackflowos.transport.address`.

Temporary state lives in a short system temp directory (``SFO_TEST_TMPDIR``,
default ``<tempdir>/sfo-tests``), never under the checkout: unix socket paths
must fit in ``sockaddr_un``, so a deep checkout path would make IPC binds fail.
The allocator is exercised without starting any sockets except for the TCP
probe binds.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import tempfile
import unittest
from pathlib import Path

from stackflowos.errors import Unavailable, ValidationError
from stackflowos.transport.address import MAX_IPC_PATH_BYTES, AddressAllocator

TMP_ROOT = Path(os.environ.get("SFO_TEST_TMPDIR")
                or Path(tempfile.gettempdir()) / "sfo-tests")
WORK_ID_RE = re.compile(r"^[A-Za-z0-9_-]+\.[0-9]+$")


def _ipc_path_too_long(run_dir: Path) -> bool:
    """Whether even a hashed IPC socket path would exceed the length limit.

    Mirrors :meth:`AddressAllocator._ipc_path`: when ``run_dir`` is deep it
    builds ``<run_dir>/<stem>-<12-char digest><suffix>`` with a one-char stem.
    """
    shortest_name = 1 + 1 + 12 + len(".events.sock")  # x-<digest><suffix>
    return (len(str(run_dir).encode("utf-8")) + 1 + shortest_name
            > MAX_IPC_PATH_BYTES)


def _bindable(host: str, port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _free_port_pair(host: str = "127.0.0.1",
                    start: int = 17900, end: int = 17999) -> tuple[int, int]:
    """Find two adjacent free ports >= 17900 for the exhaustive-pool test."""
    for base in range(start, end):
        if _bindable(host, base) and _bindable(host, base + 1):
            return base, base + 1
    raise AssertionError(f"no free port pair in {start}-{end}")


def _port_of(address: str) -> int:
    return int(address.rsplit(":", 1)[1])


class AddressAllocatorTests(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(TMP_ROOT),
                                                 prefix="sfo-")
        self.tmp = Path(self._tmp.name)
        self.run_dir = (self.tmp / "run").resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------
    def skip_if_ipc_path_too_long(self, run_dir: Path) -> None:
        if _ipc_path_too_long(run_dir):
            self.skipTest(
                "checkout path too long for unix sockets: "
                f"{run_dir}")

    def ipc(self, **kwargs) -> AddressAllocator:
        self.skip_if_ipc_path_too_long(self.run_dir)
        return AddressAllocator(mode="ipc", host="127.0.0.1",
                                run_dir=self.run_dir, **kwargs)

    def tcp(self, pool: tuple[int, int], **kwargs) -> AddressAllocator:
        return AddressAllocator(mode="tcp", host="127.0.0.1",
                                run_dir=self.run_dir, port_pool=pool, **kwargs)

    # -- ipc --------------------------------------------------------------
    def test_ipc_allocates_unique_rpc_and_events(self) -> None:
        alloc = self.ipc()
        self.assertEqual(alloc.mode, "ipc")
        self.assertEqual(alloc.run_dir, self.run_dir)

        sys = alloc.ensure_sys()
        self.assertEqual(sys.work_id, "sys.0")
        self.assertEqual(sys.rpc, f"ipc://{self.run_dir}/sys.0.rpc.sock")
        self.assertEqual(sys.events, f"ipc://{self.run_dir}/sys.0.events.sock")
        self.assertNotEqual(sys.rpc, sys.events)

        work_id = alloc.new_work_id("coder")
        work = alloc.allocate(work_id)
        self.assertEqual(work.work_id, "coder.1")
        self.assertEqual(work.rpc, f"ipc://{self.run_dir}/coder.1.rpc.sock")
        self.assertEqual(work.events, f"ipc://{self.run_dir}/coder.1.events.sock")
        self.assertNotIn(work.rpc, (sys.rpc, sys.events))
        self.assertNotIn(work.events, (sys.rpc, sys.events))
        self.assertEqual(alloc.work_ids, ("sys.0", "coder.1"))
        # The run directory is created by the allocator so listeners can bind.
        self.assertTrue(self.run_dir.is_dir())

    def test_ensure_sys_is_idempotent_and_reserved_first(self) -> None:
        alloc = self.ipc()
        first = alloc.ensure_sys()
        second = alloc.ensure_sys()
        self.assertEqual(first, second)
        self.assertEqual(alloc.work_ids, ("sys.0",))
        # sys.0 owns the reserved n=0 slot; the first worker is .1.
        self.assertEqual(alloc.new_work_id("coder"), "coder.1")
        self.assertEqual(alloc.new_work_id("lead"), "lead.2")

    def test_new_work_id_sequence_and_format(self) -> None:
        alloc = self.ipc()
        alloc.ensure_sys()
        ids = [alloc.new_work_id("coder"), alloc.new_work_id("lead"),
               alloc.new_work_id("coder")]
        self.assertEqual(ids, ["coder.1", "lead.2", "coder.3"])
        for work_id in ids:
            self.assertRegex(work_id, WORK_ID_RE)
        self.assertEqual(len(set(ids)), len(ids))
        with self.assertRaises(ValidationError):
            alloc.new_work_id("")
        with self.assertRaises(ValidationError):
            alloc.new_work_id("bad kind")
        with self.assertRaises(ValidationError):
            alloc.new_work_id("a.b")

    def test_release_frees_work_id_and_allows_reallocation(self) -> None:
        alloc = self.ipc()
        alloc.ensure_sys()
        work_id = alloc.new_work_id("coder")
        first = alloc.allocate(work_id)
        self.assertIsNotNone(alloc.addresses(work_id))
        alloc.release(work_id)
        self.assertIsNone(alloc.addresses(work_id))
        self.assertEqual(alloc.work_ids, ("sys.0",))
        # Releasing an unknown id is a no-op.
        alloc.release(work_id)

        again = alloc.allocate(work_id)
        self.assertEqual(again, first)
        self.assertEqual(alloc.work_ids, ("sys.0", "coder.1"))
        # The counter is monotonic, so a released name is never reissued.
        self.assertEqual(alloc.new_work_id("coder"), "coder.2")

    def test_allocate_is_idempotent(self) -> None:
        alloc = self.ipc()
        first = alloc.allocate("coder.7")
        second = alloc.allocate("coder.7")
        self.assertEqual(first, second)
        self.assertEqual(alloc.work_ids, ("coder.7",))

    def test_stale_socket_files_removed_on_allocate_and_cleanup(self) -> None:
        alloc = self.ipc()
        stale_rpc = self.run_dir / "sys.0.rpc.sock"
        stale_events = self.run_dir / "sys.0.events.sock"
        stale_rpc.write_text("stale")
        stale_events.write_text("stale")

        sys = alloc.ensure_sys()
        self.assertFalse(stale_rpc.exists())
        self.assertFalse(stale_events.exists())

        unit = alloc.allocate(alloc.new_work_id("coder"))
        rpc_file = Path(unit.rpc[len("ipc://"):])
        events_file = Path(unit.events[len("ipc://"):])
        rpc_file.write_text("stale")
        events_file.write_text("stale")
        alloc.cleanup()
        self.assertFalse(rpc_file.exists())
        self.assertFalse(events_file.exists())
        self.assertEqual(alloc.work_ids, ())
        self.assertIsNone(alloc.addresses(sys.work_id))

    def test_hostile_work_id_is_sanitised_and_unique(self) -> None:
        alloc = self.ipc()
        first = alloc.allocate("lead/1")
        second = alloc.allocate("lead_1")
        self.assertNotEqual(first.rpc, second.rpc)
        for addresses in (first, second):
            rpc_path = Path(addresses.rpc[len("ipc://"):])
            events_path = Path(addresses.events[len("ipc://"):])
            self.assertEqual(rpc_path.parent, self.run_dir)
            self.assertEqual(events_path.parent, self.run_dir)
            self.assertNotIn("/", rpc_path.name)
            self.assertTrue(rpc_path.name.endswith(".rpc.sock"))

    def test_long_ipc_path_is_shortened(self) -> None:
        self.skip_if_ipc_path_too_long(self.run_dir)
        alloc = AddressAllocator(mode="ipc", host="127.0.0.1",
                                 run_dir=self.run_dir)
        sys = alloc.ensure_sys()
        long_id = "coder." + "x" * 60
        unit = alloc.allocate(long_id)
        sys_path = Path(sys.rpc[len("ipc://"):])
        self.assertLessEqual(len(str(sys_path).encode("utf-8")),
                             MAX_IPC_PATH_BYTES)
        for url in (unit.rpc, unit.events):
            path = Path(url[len("ipc://"):])
            self.assertEqual(path.parent, self.run_dir)
            self.assertLessEqual(
                len(str(path).encode("utf-8")), MAX_IPC_PATH_BYTES,
                f"ipc path too long: {path}")
            self.assertTrue(path.name.endswith(".sock"))
            digest = hashlib.sha1(long_id.encode("utf-8")).hexdigest()[:12]
            self.assertIn(digest, path.name)
        self.assertNotEqual(unit.rpc, unit.events)
        # The shortening is deterministic: the same id yields the same path.
        alloc.release(unit.work_id)
        self.assertEqual(alloc.allocate(unit.work_id), unit)

    # -- tcp --------------------------------------------------------------
    def test_tcp_allocates_two_distinct_probed_ports_per_unit(self) -> None:
        base, _ = _free_port_pair()
        alloc = self.tcp((base, min(base + 9, 17999)))
        sys = alloc.ensure_sys()
        unit = alloc.allocate(alloc.new_work_id("coder"))

        ports = []
        for addresses in (sys, unit):
            self.assertTrue(addresses.rpc.startswith("tcp://127.0.0.1:"))
            self.assertTrue(addresses.events.startswith("tcp://127.0.0.1:"))
            rpc_port = _port_of(addresses.rpc)
            events_port = _port_of(addresses.events)
            self.assertNotEqual(rpc_port, events_port)
            self.assertIn(rpc_port, range(base, min(base + 10, 18000)))
            self.assertIn(events_port, range(base, min(base + 10, 18000)))
            ports.extend((rpc_port, events_port))
        self.assertEqual(len(set(ports)), 4)

    def test_tcp_advertise_host_is_used_in_addresses(self) -> None:
        base, _ = _free_port_pair()
        alloc = self.tcp((base, min(base + 9, 17999)),
                         advertise_host="192.0.2.10")
        sys = alloc.ensure_sys()
        self.assertTrue(sys.rpc.startswith("tcp://192.0.2.10:"))
        self.assertTrue(sys.events.startswith("tcp://192.0.2.10:"))
        self.assertNotEqual(sys.rpc, sys.events)

    def test_tcp_pool_exhaustion_and_port_reuse(self) -> None:
        base, other = _free_port_pair()
        alloc = self.tcp((base, other))
        sys = alloc.ensure_sys()
        self.assertEqual({_port_of(sys.rpc), _port_of(sys.events)},
                         {base, other})

        work_id = alloc.new_work_id("coder")
        with self.assertRaises(Unavailable):
            alloc.allocate(work_id)
        self.assertEqual(alloc.work_ids, ("sys.0",))

        alloc.release(sys.work_id)
        self.assertEqual(alloc.work_ids, ())
        unit = alloc.allocate(work_id)
        self.assertEqual({_port_of(unit.rpc), _port_of(unit.events)},
                         {base, other})

    def test_release_frees_tcp_ports(self) -> None:
        base, other = _free_port_pair()
        alloc = self.tcp((base, other))
        sys = alloc.ensure_sys()
        alloc.release(sys.work_id)
        again = alloc.ensure_sys()
        self.assertEqual(again.work_id, "sys.0")
        self.assertEqual({_port_of(again.rpc), _port_of(again.events)},
                         {base, other})

    # -- validation -------------------------------------------------------
    def test_invalid_configuration_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            AddressAllocator(mode="carrier-pigeon", host="127.0.0.1",
                             run_dir=self.run_dir)
        with self.assertRaises(ValidationError):
            AddressAllocator(mode="ipc", host="", run_dir=self.run_dir)
        with self.assertRaises(ValidationError):
            AddressAllocator(mode="tcp", host="127.0.0.1",
                             run_dir=self.run_dir, port_pool=(18000, 17999))
        with self.assertRaises(ValidationError):
            AddressAllocator(mode="tcp", host="127.0.0.1",
                             run_dir=self.run_dir, port_pool=(0, 10))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
