# Deploying StackFlowOS

Two supported deployment shapes:

1. **Container** (`deploy/Dockerfile`) -- one hub process, state in `/data`.
2. **systemd user unit** (`deploy/stackflowos.service`) -- bare-metal / VM
   install for one operator.

The hub is a single process: web console + L2 orchestration + pynng transport.
Workers are short-lived child processes it spawns. There is **no cluster
consensus** in this version; HA means *cold standby on a shared state
directory* (see [Cold standby](#cold-standby)).

---

## Container

```bash
docker build -f deploy/Dockerfile -t stackflowos:hub .
docker volume create sfo-data
docker run -d --name sfo \
  -p 127.0.0.1:17800:17800 \
  -v sfo-data:/data \
  -e SFO_WEB_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" \
  stackflowos:hub
docker logs -f sfo            # hub log
docker inspect sfo --format '{{.State.Health.Status}}'
```

* `/data` holds `config.json`, `state/` (projects, counters, approvals,
  memory), `events/` (JSONL + archives), `logs/`, `projects/` workspaces.
  Back it up as a unit; the container itself is stateless.
* The image runs as UID/GID `10001` (`sfo`); if you bind-mount a host directory
  instead of a named volume, `chown -R 10001:10001` it first.
* `SFO_WEB_TOKEN` pins the browser login token. Without it the hub prints a
  fresh token to the TTY, which containers usually lack -- set the variable or
  the console is not reachable.
* The image exposes `17800/tcp` (console) and the transport pool
  `17820-17899/tcp`. The default transport is `ipc://` (worker sockets inside
  the same container/namespace). To let workers on other hosts connect, switch
  to TCP **and** explicitly allow it:

  ```jsonc
  // /data/config.json
  {"transport": {"mode": "tcp", "host": "0.0.0.0", "allow_remote": true,
                 "advertise_host": "10.0.0.5"}}
  ```

  TCP without TLS is plaintext; keep it on a private network or tunnel it.

### Healthcheck

`GET /healthz` is anonymous and returns only liveness (`status`, `transport`);
`GET /api/health` (authenticated) additionally reports units, extensions,
eventlog/lease sub-reports (when wired) and is what the app dashboard polls.
The Docker `HEALTHCHECK` uses `/healthz`.

---

## systemd user unit

```bash
mkdir -p ~/.config/systemd/user ~/.config/stackflowos
cp deploy/stackflowos.service ~/.config/systemd/user/
install -m 0600 /dev/null ~/.config/stackflowos/hub.env
printf 'SFO_WEB_TOKEN=%s\n' "$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" \
  > ~/.config/stackflowos/hub.env
systemctl --user daemon-reload
systemctl --user enable --now stackflowos.service
journalctl --user -u stackflowos.service -f
```

* `StateDirectory=stackflowos` + `SFO_STATE_DIR=%S/stackflowos` keep every
  runtime byte under `~/.local/state/stackflowos`.
* Edit `ExecStart` if StackFlowOS lives in a virtualenv
  (`/opt/sfo/venv/bin/stackflowos`).
* `Restart=on-failure` restarts a crashed hub; a clean `systemctl stop` is not
  restarted.
* Use `loginctl enable-linger <user>` for a headless host so the user unit
  starts at boot without a login session.
* `stackflowos status --token "$SFO_WEB_TOKEN"` prints the state from outside
  the unit.

---

## TLS and remote access

The console binds loopback by default and **refuses** `0.0.0.0` / other
non-loopback hosts unless explicitly allowed (`web.allow_remote=true` or
`stackflowos hub --allow-remote`). Allowing a remote bind does not add
authentication by itself -- every `/api/*` call still requires a browser
session or bearer token, but the transport is plaintext unless TLS is on.

Preferred options, in order:

1. **SSH tunnel** -- keep the bind on `127.0.0.1`, `ssh -L 17800:127.0.0.1:17800 host`.
2. **Built-in TLS** -- set a certificate/key pair and let the hub serve HTTPS:

   ```jsonc
   {"web": {"host": "0.0.0.0", "allow_remote": true,
            "tls_cert": "/data/tls/fullchain.pem",
            "tls_key": "/data/tls/privkey.pem",
            "tls_min_version": "TLSv1.2"}}
   ```

   Both files are required together; a half-configured pair fails loudly at
   config load. Mount `/data/tls` read-only.
3. **Reverse proxy** (nginx/Caddy) terminating TLS, proxying to
   `127.0.0.1:17800`, and passing `Host`/`Origin` unchanged. If the proxy
   rewrites the authority, add the public name to `web.trusted_hosts` or the
   Host admission check will reject it.

Remote **workers** need the TCP transport, not just the web console: set
`transport.mode=tcp`, `transport.host=0.0.0.0`, `transport.allow_remote=true`
and `transport.advertise_host` to the address workers should dial. Worker RPC
uses protocol-v2 tokens issued by the hub at spawn time; the token travels in
the child environment, never argv or logs.

mDNS discovery is off by default. Set `cluster.discovery=mdns` (or
`"discovery": "mdns"` in `config.json`) to announce the hub as
`_stackflowos._tcp.local`; `GET /api/discovery/peers` lists what the hub sees.
Workers can find a hub with
`python -c "from stackflowos.transport.discovery import discover_hubs; print(discover_hubs(2))"`.
Multicast is link-local: it does not cross routers/VPNs without extra setup, so
keep `transport.advertise_host` for routed deployments.

---

## Cold standby (not Raft)

`cluster.standby=true` (CLI `--standby`) starts a **second process on the same
state directory** that does not serve traffic while another hub holds the
state-dir lock (`<state>/run/hub.lock`, `flock`). It polls the lock every
`cluster.standby_poll_s` and takes over when the active process exits.

Semantics and limits:

* **Shared state directory is mandatory** (same host or a single-writer
  filesystem such as a network volume that honours `flock`). Two independent
  copies of the state are *not* a cluster.
* **Not quorum-based**: there is no Raft/Paxos, no leader election, no log
  replication. If the active hub is merely paused (VM freeze, SIGSTOP, network
  partition of a remote filesystem), the standby cannot distinguish "dead"
  from "stuck" -- only process exit or lock release triggers takeover.
* **Brief unavailability during failover**: the standby must notice the lock
  release (`standby_poll_s`, default 2s), boot its services and load the state.
  SSE clients reconnect with `?since=<last_seq>`; the event log/archives make
  replay lossless. Expect seconds of downtime, not zero.
* **Single writer**: never run two hubs against the same state without the
  standby lock. The lock is the only guard; concurrent writers corrupt JSON
  state and duplicate `seq` numbers.
* **Fencing helps with leases, not storage**: lease fencing tokens
  (`cluster.lease_fencing`, persisted in
  `state/state/lease_fencing.json`) stop a paused worker from acting on a
  stale lease after failover. They do not make the JSON stores multi-writer.
* The web console/port must be free for the standby; configure the standby to
  bind only after takeover (the shipped extension does this via the hub lock),
  or use a load-balancer/virtual IP that follows the active process.

Cold standby is a pragmatic "restart on failure" story for a single operator,
not a multi-node database. If you need real HA, run one hub and replicate
backups, or put an external supervisor (Kubernetes/systemd) in front of a
single instance.

---

## Backup and restore

All durable state is under the state directory. Two consistent options:

1. **Online SQLite + file copy** (only when `storage.backend=sqlite`):
   `sqlite3 /data/state/stackflowos.db ".backup '/backup/sfo.db'"` plus a
   `tar` of the remaining files (projects.json, memory files, workspaces).
2. **Stopped copy** (always consistent): stop the hub, then archive the
   directory:

   ```bash
   systemctl --user stop stackflowos.service
   tar --xz -C ~/.local/state -cf backup/sfo-$(date +%F).tar.xz stackflowos
   systemctl --user start stackflowos.service
   ```

   For containers: `docker run --rm -v sfo-data:/data -v "$PWD":/backup busybox tar -czf /backup/sfo.tgz -C /data .`

Retention: the ops backup extension keeps `observability.backup_keep`
(default 7) copies when enabled. Restore by starting the hub with an empty/new
state dir, stopping it, replacing the directory contents, and starting again;
run `stackflowos doctor` afterwards to validate config/state.

Event log archives (`events/events-*.jsonl.gz`) are kept
`storage.eventlog_rotate_keep` deep (default 5). The JSONL files can be
replayed into SQLite with:

```bash
python -m stackflowos.storage.migrate --state-dir /data --sqlite-path /data/state/stackflowos.db --dry-run
python -m stackflowos.storage.migrate --state-dir /data --sqlite-path /data/state/stackflowos.db
```

Run the migration **before** setting `SFO_STORAGE_BACKEND=sqlite` /
`storage.backend=sqlite`; an empty database ignores pre-existing JSONL history.

---

## Upgrade

1. Back up (above).
2. Read `docs/architecture.md` §14 and `docs/team/*.md` for interface changes.
3. Build/pull the new image or `pip install --upgrade .` into the venv.
4. Stop, start, check `GET /healthz` and `stackflowos doctor`.
5. Rollback = restore the previous image/venv and the backup directory. The
   JSON stores are version-tagged (`{"version": 1}`); SQLite uses
   `PRAGMA user_version` migrations that only move forward -- keep the backup
   taken before the upgrade.
6. Workers are spawned fresh by the supervisor, so a hub restart also
   replaces workers; interrupted tasks are reset/reclaimed, not silently
   marked done.

---

## Troubleshooting

| Symptom | Where to look |
|---|---|
| console returns 401 | `SFO_WEB_TOKEN`, `stackflowos status --token`, token single-use setting |
| `config error: web.host ... allow_remote` | deliberate: pass `--allow-remote` or set `web.allow_remote=true` |
| health `status: degraded` | transport not running; check port/lock conflicts, `journalctl --user -u stackflowos` |
| `/api/discovery/peers` empty | `cluster.discovery=mdns`, multicast blocked, port 5353 already used by avahi (both can share, check logs) |
| worker cannot connect | `transport.mode=tcp`, `advertise_host`, firewall, token in child env |
| second hub exits `UNAVAILABLE` | expected: one hub per state directory; use `--standby` for cold standby |
