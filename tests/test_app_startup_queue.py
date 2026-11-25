"""Hub boot hook: a queue persisted by the previous process is re-armed.

The supervisor-level behaviour (restored ``QueueEntry`` rows being drained) is
covered in ``tests/test_agent_queue.py``; these tests pin the App-side hook
that makes the hub call it once, after ``agents.json`` has been loaded.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from stackflowos.app import App


class AppResumeAgentQueueTests(unittest.TestCase):
    def test_hook_calls_the_supervisor_resume_queue(self) -> None:
        calls: list[dict] = []

        def resume_queue() -> dict:
            result = {"queued": 2, "available": 1, "scheduled": True}
            calls.append(result)
            return result

        app = App.__new__(App)
        app.supervisor = SimpleNamespace(resume_queue=resume_queue)

        self.assertEqual(app._resume_agent_queue(),
                         {"queued": 2, "available": 1, "scheduled": True})
        self.assertEqual(calls, [{"queued": 2, "available": 1,
                                  "scheduled": True}])

    def test_hook_is_a_noop_without_a_supervisor(self) -> None:
        app = App.__new__(App)
        app.supervisor = SimpleNamespace()
        self.assertEqual(app._resume_agent_queue(), {})

    def test_hook_never_blocks_boot_on_a_broken_snapshot(self) -> None:
        app = App.__new__(App)
        app.log = mock.Mock()
        app.supervisor = SimpleNamespace(
            resume_queue=mock.Mock(side_effect=RuntimeError("corrupt queue")))

        self.assertEqual(app._resume_agent_queue(), {})
        app.log.exception.assert_called_once()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
