import unittest

from stackflowos.protocol import (
    Envelope, EventRecord, KIND_EVENT, KIND_REPLY, KIND_RPC,
    MAX_MESSAGE_BYTES, PROTOCOL_VERSION,
)


class EnvelopeTests(unittest.TestCase):
    def test_rpc_roundtrip(self):
        env = Envelope.rpc("lease.acquire", {"resource": "npu.0"},
                           src="studio.coder.1", dst="sys", trace="t1")
        wire = env.to_bytes()
        back = Envelope.from_bytes(wire)
        self.assertEqual(back.kind, KIND_RPC)
        self.assertEqual(back.action, "lease.acquire")
        self.assertEqual(back.src, "studio.coder.1")
        self.assertEqual(back.dst, "sys")
        self.assertEqual(back.data, {"resource": "npu.0"})
        self.assertEqual(back.trace, "t1")
        self.assertEqual(back.v, PROTOCOL_VERSION)
        self.assertTrue(back.ok)

    def test_reply_ok(self):
        req = Envelope.rpc("x.y", {"a": 1}, src="a", dst="b")
        rep = req.reply({"value": 42})
        self.assertEqual(rep.kind, KIND_REPLY)
        self.assertEqual(rep.id, req.id)
        self.assertEqual(rep.src, "b")
        self.assertEqual(rep.dst, "a")
        self.assertEqual(rep.data, {"value": 42})
        self.assertTrue(rep.ok)

    def test_reply_error(self):
        req = Envelope.rpc("x.y", {}, src="a", dst="b")
        rep = req.reply_error("DENIED", "no", detail={"cap": "fs.write"})
        self.assertFalse(rep.ok)
        self.assertEqual(rep.error["code"], "DENIED")
        self.assertEqual(rep.error["detail"], {"cap": "fs.write"})

    def test_event_roundtrip(self):
        env = Envelope.event("task.updated", {"task_id": "t-1", "status": "doing"},
                             source="worker.coder.1", project_id="p-1", seq=7)
        back = Envelope.from_bytes(env.to_bytes())
        self.assertEqual(back.kind, KIND_EVENT)
        self.assertEqual(back.action, "task.updated")
        self.assertEqual(back.seq, 7)
        self.assertEqual(back.data["type"], "task.updated")
        self.assertEqual(back.data["source"], "worker.coder.1")
        self.assertEqual(back.data["project_id"], "p-1")
        self.assertEqual(back.data["data"]["task_id"], "t-1")

    def test_event_record(self):
        env = Envelope.event("lease.expired", {"lease_id": "l-1"},
                             source="sys", project_id="p-2", seq=9)
        rec = EventRecord.from_envelope(env, seq=9)
        self.assertEqual(rec.type, "lease.expired")
        self.assertEqual(rec.project_id, "p-2")
        self.assertEqual(rec.seq, 9)
        again = EventRecord.from_dict(rec.to_dict())
        self.assertEqual(again.to_dict(), rec.to_dict())
        as_env = rec.to_envelope()
        self.assertEqual(as_env.kind, KIND_EVENT)
        self.assertEqual(as_env.data["type"], "lease.expired")

    def test_invalid_envelopes(self):
        with self.assertRaises(ValueError):
            Envelope.from_bytes(b"{not json")
        with self.assertRaises(ValueError):
            Envelope.from_dict({"kind": "bogus", "action": "a", "src": "s", "dst": "d"})
        with self.assertRaises(ValueError):
            Envelope.from_dict({"kind": "rpc", "action": "", "src": "s", "dst": "d"})

    def test_oversize_rejected(self):
        env = Envelope.rpc("x", {"blob": "a" * MAX_MESSAGE_BYTES}, src="a", dst="b")
        with self.assertRaises(ValueError):
            env.to_bytes()


if __name__ == "__main__":
    unittest.main()
