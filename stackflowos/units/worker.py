"""Worker unit: one agent per process.

The process is the isolation boundary. It connects to the hub over the same
pynng wire protocol used for IPC and TCP, registers itself as a unit, then
runs one agent loop. When the loop finishes the process exits; when the hub
dies the heartbeat fails and the process gives up instead of lingering.

CLI (used by AgentSupervisor):

    python -m stackflowos.worker --sys <rpc> --work-id coder.3 --role coder \
        --project p-1 [--task t-2] --state-dir <dir> --root <dir>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import sys
import time
from pathlib import Path

from ..agents import Agent, AgentRunner, MockBrain, ToolExecutor
from ..agents.api import SysAPI
from ..ai import make_provider
from ..config import Config
from ..protocol import EventRecord
from ..transport import RpcClient, UnitAddresses, UnitTransport
from ..util.logging import get_logger, setup_logging


class RemoteSys(SysAPI):
    """SysAPI implementation backed by the unit transport."""

    def __init__(self, transport: UnitTransport) -> None:
        self.transport = transport

    SLOW_ACTIONS = {
        "agent.spawn": 45.0,
        "project.create": 30.0,
        "unit.kill": 15.0,
        "agent.kill": 15.0,
    }

    async def call(self, action: str, data: dict | None = None, *,
                   timeout: float | None = None) -> dict:
        if timeout is None:
            timeout = self.SLOW_ACTIONS.get(action)
        return await self.transport.call(action, data or {}, timeout=timeout)


def worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stackflowos-worker")
    parser.add_argument("--sys", required=True, help="hub sys rpc address")
    parser.add_argument("--work-id", required=True, help="allocated work id")
    parser.add_argument("--role", required=True)
    parser.add_argument("--project")
    parser.add_argument("--task")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--label", default="")
    return parser


async def run_worker(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    state_dir = Path(args.state_dir).resolve()
    os.environ["SFO_ROOT"] = str(root)
    os.environ["SFO_STATE_DIR"] = str(state_dir)
    cfg = Config.load(root=root, state_dir=state_dir)
    setup_logging("INFO", cfg.paths.log_file)
    log = get_logger(f"worker.{args.role}")
    log.info("worker boot: work_id=%s role=%s project=%s task=%s",
             args.work_id, args.role, args.project, args.task)

    work_id = args.work_id

    # Bootstrap: ask the hub where this unit should bind its sockets.
    boot = RpcClient(args.sys, src=work_id, dst="sys",
                     timeout=cfg.transport.rpc_timeout_s)
    await boot.start()
    try:
        info = await boot.call("unit.addresses", {"work_id": work_id})
    finally:
        await boot.stop()
    addresses = UnitAddresses(work_id=work_id,
                              rpc=info["rpc_address"],
                              events=info["events_address"])
    unit = UnitTransport(sys_rpc=args.sys, work_id=work_id, addresses=addresses,
                         events_target=info["sys_events_address"],
                         logger=get_logger(f"unit.{work_id}"))
    sys_api = RemoteSys(unit)

    stop_event = asyncio.Event()
    state = {"paused": False, "killed": False, "status": "booting", "step": 0}

    async def on_pause(data: dict | None = None) -> dict:
        state["paused"] = True
        runner_ref = state.get("runner")
        if runner_ref is not None:
            runner_ref.pause()
        unit.publish(EventRecord(seq=0, type="unit.paused", source=work_id,
                                 ts=int(time.time() * 1000), data={"reason": (data or {}).get("reason", "")}))
        return {"paused": True}

    async def on_resume(data: dict | None = None) -> dict:
        state["paused"] = False
        runner_ref = state.get("runner")
        if runner_ref is not None:
            runner_ref.resume()
        return {"paused": False}

    async def on_kill(data: dict | None = None) -> dict:
        state["killed"] = True
        runner_ref = state.get("runner")
        if runner_ref is not None:
            runner_ref.kill()
        stop_event.set()
        return {"killed": True}

    async def on_status(data: dict | None = None) -> dict:
        return {
            "work_id": work_id,
            "role": args.role,
            "agent_id": state.get("agent_id"),
            "task_id": state.get("task_id"),
            "status": state["status"],
            "step": state["step"],
            "paused": state["paused"],
        }

    def _wrap(fn):
        async def _handler(env):
            data = env.data if isinstance(env.data, dict) else {}
            return await fn(data)
        return _handler

    unit.register_action("unit.pause", _wrap(on_pause))
    unit.register_action("unit.resume", _wrap(on_resume))
    unit.register_action("unit.kill", _wrap(on_kill))
    unit.register_action("unit.status", _wrap(on_status))

    await unit.start()
    agent: Agent | None = None
    runner: AgentRunner | None = None
    heartbeat_task: asyncio.Task | None = None
    try:
        project: dict | None = None
        if args.project:
            try:
                project = await sys_api.call("project.get", {"project_id": args.project})
            except Exception as exc:
                log.warning("could not load project %s: %s", args.project, exc)
                project = None

        workspace_dir = (Path(project["workspace"]) if project and project.get("workspace")
                         else state_dir / "agents" / work_id)
        workspace_dir.mkdir(parents=True, exist_ok=True)

        abilities = _abilities_for(args.role)
        await sys_api.call("unit.register", {
            "work_id": work_id,
            "kind": args.role,
            "label": args.label or args.role,
            "project_id": args.project,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "transport": cfg.transport.mode,
            "rpc_address": addresses.rpc,
            "events_address": addresses.events,
            "abilities": abilities,
            "hwinfo": {"python": sys.version.split()[0], "platform": sys.platform},
            "started_at": int(time.time() * 1000),
            "metadata": {"role": args.role, "task_id": args.task},
        })

        agent_data = await sys_api.call("agent.register", {
            "role": args.role,
            "project_id": args.project,
            "unit_id": work_id,
            "task_id": args.task,
            "model": cfg.llm.model if cfg.llm.provider != "mock" else "mock",
        })
        agent = Agent.from_dict(agent_data)
        state["agent_id"] = agent.id
        state["task_id"] = agent.task_id
        state["status"] = "idle"
        unit.publish(EventRecord(seq=0, type="agent.registered", source=work_id,
                                 ts=int(time.time() * 1000),
                                 data={"agent_id": agent.id, "role": agent.role},
                                 project_id=args.project))

        executor = ToolExecutor(sys_api, agent=agent, workspace=workspace_dir,
                                security_config=cfg.security,
                                logger=get_logger(f"tools.{work_id}"))
        brain = _make_brain(args.role, cfg)
        runner = AgentRunner(sys_api, agent, brain, executor, config=cfg.agents,
                             logger=get_logger(f"agent.{work_id}"),
                             workspace=workspace_dir)
        state["runner"] = runner
        state["status"] = "working"

        async def heartbeat_loop() -> None:
            failures = 0
            while not stop_event.is_set():
                await asyncio.sleep(cfg.agents.heartbeat_s)
                try:
                    await sys_api.call("unit.heartbeat", {
                        "work_id": work_id,
                        "stats": {"step": runner.steps if runner else 0,
                                  "status": state["status"]},
                    })
                    failures = 0
                except Exception as exc:
                    failures += 1
                    log.debug("heartbeat failed (%d): %s", failures, exc)
                    if failures >= 3:
                        log.warning("hub unreachable; worker exiting")
                        stop_event.set()
                        runner.kill()
                        return

        heartbeat_task = asyncio.create_task(heartbeat_loop())

        run_task = asyncio.create_task(runner.run())
        stop_waiter = asyncio.create_task(stop_event.wait())
        done, _pending = await asyncio.wait({run_task, stop_waiter},
                                            return_when=asyncio.FIRST_COMPLETED)
        if run_task in done:
            result = run_task.result()
            state["status"] = result.status
            log.info("agent finished: %s (%s steps)", result.status, result.steps)
        else:
            runner.kill()
            try:
                result = await asyncio.wait_for(run_task, timeout=5.0)
                state["status"] = result.status
            except Exception:
                state["status"] = "killed"
        stop_waiter.cancel()
        try:
            await sys_api.call("unit.release", {"work_id": work_id,
                                                "reason": "agent_finished"})
        except Exception:
            log.debug("unit.release failed", exc_info=True)
        return 0
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await unit.stop()
        except Exception:
            log.debug("unit transport stop failed", exc_info=True)
        log.info("worker exit: %s", work_id)


def _make_brain(role: str, cfg: Config):
    if cfg.llm.provider != "mock":
        try:
            from ..ai.llm import LLMBrain
            provider = make_provider(cfg.llm)
            return LLMBrain(provider, role, max_steps=cfg.agents.max_steps)
        except Exception:
            get_logger("worker").exception("LLM brain unavailable; falling back to mock")
    return MockBrain(role, max_steps=cfg.agents.max_steps)


def _abilities_for(role: str) -> list[dict]:
    try:
        from ..agents.roles import get_role
        from ..agents.tools import TOOLS
        role_def = get_role(role)
    except Exception:
        return []
    abilities = []
    for tool_name in role_def.tools:
        spec = TOOLS.get(tool_name)
        if spec is not None:
            abilities.append({"name": spec.capability, "tool": tool_name,
                              "scope": "project"})
    unique = {(a["name"], a["tool"]): a for a in abilities}
    return list(unique.values())


def main(argv: list[str] | None = None) -> int:
    args = worker_parser().parse_args(argv)

    async def _run() -> int:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()

        def _signal(*_args):
            if task is not None:
                task.cancel()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal)
            except NotImplementedError:  # pragma: no cover
                pass
        try:
            return await run_worker(args)
        except asyncio.CancelledError:
            return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
