"""L0 transport: pynng REP/REQ (control plane) and PUB/SUB (event plane).

Public surface (everything the rest of StackFlowOS imports):

* :class:`~stackflowos.transport.address.AddressAllocator`,
  :class:`~stackflowos.transport.address.UnitAddresses`
* :class:`~stackflowos.transport.nng.RpcServer`,
  :class:`~stackflowos.transport.nng.RpcClient`,
  :class:`~stackflowos.transport.nng.EventPublisher`,
  :class:`~stackflowos.transport.nng.EventSubscriber`,
  :class:`~stackflowos.transport.nng.HubTransport`,
  :class:`~stackflowos.transport.nng.UnitTransport`
* :func:`~stackflowos.transport.nng.is_ipc`,
  :func:`~stackflowos.transport.nng.cleanup_ipc_address`
"""

from __future__ import annotations

from .address import AddressAllocator, UnitAddresses
from .nng import (
    EventPublisher,
    EventSubscriber,
    HubTransport,
    RpcClient,
    RpcServer,
    UnitTransport,
    cleanup_ipc_address,
    is_ipc,
)

__all__ = [
    "AddressAllocator",
    "UnitAddresses",
    "RpcServer",
    "RpcClient",
    "EventPublisher",
    "EventSubscriber",
    "HubTransport",
    "UnitTransport",
    "is_ipc",
    "cleanup_ipc_address",
]
