"""RPC principals and their tokens (H3).

The wire protocol is v2: every external envelope carries an ``auth`` mapping::

    {"kind": "unit", "id": "coder.3", "token": "<32 random bytes, url-safe>"}
    {"kind": "operator", "id": "human:operator", "token": "<...>"}

``Envelope.src`` is self-reported and display-only; the hub derives a
:class:`Principal` from the credential in :attr:`Envelope.auth` and that
principal -- never ``src`` -- is the basis for authorization.

Tokens are generated with 32 bytes of entropy and only their SHA-256 digest is
kept, in memory.  Comparison is constant time (``hmac.compare_digest``).  Unit
tokens die with the unit (release, offline, spawn failure, restart); operator
tokens live as long as the hub process and are never written to disk.

Lifecycle::

    supervisor.spawn/restart -> issue_unit(work_id, project_id, role, agent_id)
    worker process           <- SFO_UNIT_TOKEN (env, not argv/logs)
    unit.release/offline     -> revoke(work_id)
    CLI/tests                -> issue_operator()
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
from dataclasses import dataclass

from ..errors import Unauthorized

#: Longest auth token accepted from the wire (anything longer is an attack,
#: not a token; hashing it would still be cheap, but rejecting is cleaner).
MAX_TOKEN_CHARS = 512


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller.

    ``kind`` is ``"unit"`` (worker), ``"operator"`` (CLI/console) or ``"sys"``
    (in-process only, never created from the wire).  ``id`` is the unit work id
    for units; ``agent_id`` is filled once the worker registers its agent, so
    approval principals match the agent roster.
    """

    kind: str
    id: str
    project_id: str | None = None
    role: str | None = None
    agent_id: str | None = None

    @property
    def is_unit(self) -> bool:
        return self.kind == "unit"

    @property
    def is_operator(self) -> bool:
        return self.kind == "operator"

    @property
    def work_id(self) -> str | None:
        """The unit id for unit principals, else ``None``."""
        return self.id if self.is_unit else None

    @property
    def identity(self) -> str:
        """The principal string approvals/rules are keyed by.

        Units act as their agent (once registered) or their work id; operators
        are the single local human principal ``human:operator``.
        """
        if self.is_unit:
            return self.agent_id or self.id
        return self.id


@dataclass(slots=True)
class _UnitCredential:
    token_hash: bytes
    project_id: str | None = None
    role: str | None = None
    agent_id: str | None = None


class RpcAuthRegistry:
    """In-memory unit/operator token registry (hashes only)."""

    OPERATOR_ID = "human:operator"

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._lock = threading.RLock()
        self._units: dict[str, _UnitCredential] = {}
        self._operator_hashes: list[bytes] = []
        self.log = logger or logging.getLogger("sfo.security.rpc_auth")

    # -- issuance ---------------------------------------------------------
    def issue_unit(self, work_id: str, project_id: str | None = None,
                   role: str | None = None,
                   agent_id: str | None = None) -> str:
        """Issue a fresh token for ``work_id`` and return it once.

        Re-issuing for the same work id replaces (and therefore revokes) the
        previous token: a unit has exactly one live credential.
        """
        work_id = str(work_id or "")
        if not work_id:
            raise ValueError("work_id is required to issue a unit token")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._units[work_id] = _UnitCredential(
                token_hash=self._hash(token),
                project_id=_opt_str(project_id),
                role=_opt_str(role),
                agent_id=_opt_str(agent_id),
            )
        return token

    def issue_operator(self) -> str:
        """Issue an operator token (memory only; never persisted)."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._operator_hashes.append(self._hash(token))
        return token

    def bind_agent(self, work_id: str, agent_id: str | None) -> None:
        """Record the agent behind a unit once ``agent.register`` ran.

        Attach flows allocate a unit before any agent exists, so the agent id
        is only known later; the next ``verify`` then returns it.
        """
        if not agent_id:
            return
        with self._lock:
            credential = self._units.get(str(work_id or ""))
            if credential is not None:
                credential.agent_id = str(agent_id)

    # -- verification -----------------------------------------------------
    def verify(self, auth: dict | None) -> Principal:
        """Validate one wire ``auth`` mapping and return its principal.

        Raises :class:`~stackflowos.errors.Unauthorized` for a missing/malformed
        credential, an unknown/revoked token or an unsupported kind.
        """
        if not isinstance(auth, dict):
            raise Unauthorized("rpc auth required (protocol v2)")
        kind = auth.get("kind")
        token = auth.get("token")
        if not isinstance(token, str) or not token:
            raise Unauthorized("rpc auth token missing")
        if len(token) > MAX_TOKEN_CHARS:
            raise Unauthorized("rpc auth token too long")
        digest = self._hash(token)
        if kind == "unit":
            work_id = auth.get("id")
            if not isinstance(work_id, str) or not work_id:
                raise Unauthorized("unit auth id missing")
            with self._lock:
                credential = self._units.get(work_id)
            if credential is None or not hmac.compare_digest(
                    credential.token_hash, digest):
                raise Unauthorized("unknown or revoked unit token")
            return Principal(kind="unit", id=work_id,
                             project_id=credential.project_id,
                             role=credential.role,
                             agent_id=credential.agent_id)
        if kind == "operator":
            if not self.verify_operator(token):
                raise Unauthorized("unknown operator token")
            return Principal(kind="operator", id=self.OPERATOR_ID)
        raise Unauthorized(f"unsupported rpc auth kind: {kind!r}")

    def verify_operator(self, token: str) -> bool:
        """True when ``token`` is a live operator token (constant time)."""
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
            return False
        digest = self._hash(token)
        with self._lock:
            hashes = list(self._operator_hashes)
        valid = False
        for known in hashes:  # scan all: no early exit on index
            if hmac.compare_digest(known, digest):
                valid = True
        return valid

    # -- revocation -------------------------------------------------------
    def revoke(self, work_id: str) -> bool:
        """Invalidate a unit's token immediately. Returns whether one existed."""
        with self._lock:
            return self._units.pop(str(work_id or ""), None) is not None

    def has_unit(self, work_id: str) -> bool:
        with self._lock:
            return str(work_id or "") in self._units

    def revoke_all(self) -> None:
        """Drop every credential (hub shutdown / tests)."""
        with self._lock:
            self._units.clear()
            self._operator_hashes.clear()

    # -- internals --------------------------------------------------------
    @staticmethod
    def _hash(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8", "surrogatepass")).digest()


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


__all__ = ["Principal", "RpcAuthRegistry"]
