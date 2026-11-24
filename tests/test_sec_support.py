"""Shared helpers for the SEC workflow tests (``tests/test_sec_*.py``).

Kept deliberately small: a fake app with just enough surface for the web
routes the session/control tests use, a raw HTTP/1.1 client (with optional
TLS), and a self-signed certificate generator for the TLS tests.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import ssl
import subprocess
import types
from pathlib import Path

from stackflowos.l2 import EventLog


class FakeSecurity:
    def __init__(self, **overrides):
        self.audit_redact = True
        self.audit_max_field_chars = 4000
        self.sandbox = "off"
        self.net_pin_dns = True
        self.allow_private_network = False
        for key, value in overrides.items():
            setattr(self, key, value)


class FakeWeb:
    def __init__(self, **overrides):
        self.port = 0
        self.host = "127.0.0.1"
        self.session_max = 32
        self.cookie_ttl_days = 7
        self.users = []
        self.launch_token_single_use = True
        for key, value in overrides.items():
            setattr(self, key, value)


class FakeConfig:
    def __init__(self, **overrides):
        self.web = FakeWeb()
        self.security = FakeSecurity()
        for key, value in overrides.items():
            setattr(self, key, value)

    def public_dict(self):
        return {"system": {"name": "TestOS"}, "web": {"port": 0}}


class FakeApp:
    """Just enough ``App`` for admission, sessions and control-socket tests."""

    version = "0.1.0-test"

    def __init__(self, tmp: Path, *, config: FakeConfig | None = None):
        self.tmp = Path(tmp)
        self.eventlog = EventLog(self.tmp / "events")
        self.config = config or FakeConfig()
        self.extensions: list = []
        self.created: list = []
        self.paths = types.SimpleNamespace(run_dir=self.tmp / "run")
        (self.tmp / "run").mkdir(parents=True, exist_ok=True)

    def health(self):
        return {"status": "ok", "http": "ok", "transport": "ok",
                "version": self.version, "uptime_s": 5,
                "units": {"online": 1}}

    def snapshot(self):
        return {"sys": {"name": "TestOS", "version": self.version},
                "units": [], "projects": [], "agents": [], "leases": [],
                "approvals": [], "rules": [], "events": [], "chat": [],
                "config": self.config.public_dict(), "stats": {}}

    async def create_project(self, name, goal, created_by="human:operator"):
        self.created.append((name, goal))
        return {"id": "p-1", "name": name, "goal": goal, "status": "active"}


def make_assets(tmp: Path) -> Path:
    assets = Path(tmp) / "assets"
    (assets / "css").mkdir(parents=True, exist_ok=True)
    (assets / "js").mkdir(parents=True, exist_ok=True)
    (assets / "index.html").write_text("<!doctype html><title>SFO</title>",
                                       "utf-8")
    (assets / "css" / "os.css").write_text("body{background:#000}", "utf-8")
    (assets / "js" / "main.js").write_text("export {};", "utf-8")
    return assets


async def raw_request(port: int, method: str, path: str, *,
                      host: str | None = None, headers: dict | None = None,
                      body: bytes = b"", timeout: float = 5.0,
                      tls: bool = False) -> tuple[int, dict, bytes]:
    """Send one raw HTTP/1.1 request and parse the response (read to EOF)."""
    if host is None:
        host = f"127.0.0.1:{port}"
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}"]
    for key, value in (headers or {}).items():
        lines.append(f"{key}: {value}")
    if body:
        lines.append(f"Content-Length: {len(body)}")
    wire = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
    ssl_context = None
    if tls:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", port, ssl=ssl_context)
    try:
        writer.write(wire)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    head, _, payload = data.partition(b"\r\n\r\n")
    head_lines = head.decode("latin-1").split("\r\n")
    status = int(head_lines[0].split(" ")[1]) if head_lines else 0
    parsed: dict[str, str] = {}
    for line in head_lines[1:]:
        key, separator, value = line.partition(":")
        if separator:
            parsed[key.strip().lower()] = value.strip()
    return status, parsed, payload


def json_body(**kwargs) -> bytes:
    return json.dumps(kwargs).encode("utf-8")


def write_headers(**kwargs) -> dict:
    headers = {"Content-Type": "application/json", "X-SFO-Request": "1"}
    headers.update(kwargs)
    return headers


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def make_self_signed_cert(directory: Path) -> tuple[Path, Path] | None:
    """Generate a short-lived localhost certificate with the ``openssl`` CLI.

    Returns ``None`` when the CLI is unavailable so tests can skip instead of
    depending on a Python crypto library (the project is stdlib-only).
    """
    openssl = shutil.which("openssl")
    if not openssl:
        return None
    cert = Path(directory) / "cert.pem"
    key = Path(directory) / "key.pem"
    command = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1",
        "-subj", "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ]
    completed = subprocess.run(command, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=30, check=False)
    if completed.returncode != 0 or not cert.is_file() or not key.is_file():
        return None
    return cert, key
