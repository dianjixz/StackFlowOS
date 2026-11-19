"""Unified permission system: capability → risk → policy → approval → audit.

Public surface:

* :data:`CAPABILITIES` / :func:`get_capability` — the closed capability table;
* :func:`risk_of` — dynamic risk escalation from the concrete scope;
* :class:`PolicyEngine` / :class:`Rule` / :class:`Decision` — deny-first rules;
* :class:`ApprovalBroker` / :class:`ApprovalRequest` — human decisions and
  one-shot/session/permanent grants, fail-closed on expiry.
"""

from .approvals import ApprovalBroker, ApprovalRequest
from .capabilities import (
    CAPABILITIES,
    DANGEROUS_PATTERNS,
    RISK_ORDER,
    CapabilityDef,
    get_capability,
    is_within,
    normalize_path,
    risk_of,
)
from .policy import Decision, PolicyEngine, Rule

__all__ = [
    "CAPABILITIES",
    "RISK_ORDER",
    "CapabilityDef",
    "DANGEROUS_PATTERNS",
    "get_capability",
    "risk_of",
    "is_within",
    "normalize_path",
    "Rule",
    "Decision",
    "PolicyEngine",
    "ApprovalRequest",
    "ApprovalBroker",
]
