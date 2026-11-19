"""Entry point for worker units launched via ``python -m stackflowos.worker``.

The actual implementation lives in :mod:`stackflowos.units.worker`; this shim
keeps the process command short and stable (AgentSupervisor depends on it).
"""

from .units.worker import main

if __name__ == "__main__":
    raise SystemExit(main())
