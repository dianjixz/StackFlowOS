"""L2 orchestration: units, leases, failure domain, scheduling, event log.

Public surface:

* :class:`EventLog` — append-only sequenced event store + live broadcast;
* :class:`UnitRegistry` / :class:`UnitInfo` — the ``ps`` of the system;
* :class:`LeaseManager` / :class:`Lease` — time-bounded resource claims;
* :class:`FailureManager` / :class:`FailurePolicy` — restart/quarantine domain;
* :class:`Scheduler` — advisory placement scoring.
"""

from .eventlog import EventLog, emit_both
from .failure import FailureManager, FailurePolicy
from .leases import Lease, LeaseManager
from .registry import STATUSES, UnitInfo, UnitRegistry
from .scheduler import Scheduler

__all__ = [
    "EventLog",
    "emit_both",
    "UnitInfo",
    "UnitRegistry",
    "STATUSES",
    "Lease",
    "LeaseManager",
    "FailurePolicy",
    "FailureManager",
    "Scheduler",
]
