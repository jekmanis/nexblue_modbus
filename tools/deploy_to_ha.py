#!/usr/bin/env python3
r"""Deploy the nexblue_modbus integration to the Home Assistant machine.

Modelled on the growatt_modbus fork's deploy tool. The websocket steps need the
``websockets`` package, which is tooling only (not an integration requirement)::

    uv run --with websockets python tools/deploy_to_ha.py --host 192.0.2.20 \
        --token-file ~/ha_token.txt --dry-run --deploy --restart --setup-entry 192.0.2.10
    uv run --with websockets python tools/deploy_to_ha.py --host 192.0.2.20 \
        --token-file ~/ha_token.txt --deploy --restart --setup-entry 192.0.2.10

``--host`` (or the ``HA_HOST`` environment variable) is required. The share defaults
to ``\\<host>\config``; override it with ``--share-root``.

The HA long-lived token comes from ``HA_TOKEN`` or ``--token-file`` (first non-empty
line). It is never printed, never written to the repository, never put on a command line.

Order of operations, regardless of flag order:

    1. backup        integration dir + .storage/core.config_entries, newest 5 kept
    2. deploy        mirror custom_components/nexblue_modbus onto the share, SHA256-verified
       (or --restore <backup-name>)
    3. restart       a new custom component needs a full restart; waits for RUNNING
    4. setup-entry   create the config entry through the config-flow REST API (skipped
                     when one already exists)
    5. verify        entry state 'loaded', key entities not unknown/unavailable,
                     no system_log entries from custom_components.nexblue_modbus

The SMB share must already be mounted/authenticated in Windows. Use ``net use`` without
an explicit user; an explicit user fails with error 1219.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.client
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_PORT = 8123

DOMAIN = "nexblue_modbus"
BACKUP_DIR_NAME = "nexblue_modbus_backups"
BACKUP_PREFIX = "backup-"
BACKUPS_TO_KEEP = 5
CONFIG_ENTRIES_FILE = Path(".storage") / "core.config_entries"

# unique_id suffixes (after "<serial>_") of the entities that must have a value.
VERIFY_KEYS = ("state", "voltage_l1", "power", "lifetime_energy")

EXCLUDED_DIR_NAMES = {"__pycache__"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}

RESTART_POLL_TIMEOUT = 300.0
RUNNING_WAIT_TIMEOUT = 180.0
ENTITY_CHECK_TIMEOUT = 120.0
POLL_INTERVAL = 5.0

_DRY_RUN = False


def log(msg: str = "") -> None:
    print("[{0:%H:%M:%S}] {1}".format(datetime.now(), msg) if msg else "", flush=True)


def step(msg: str) -> None:
    log()
    log("=== {0} ===".format(msg))


def plan(msg: str) -> None:
    log("{0}{1}".format("DRY-RUN would: " if _DRY_RUN else "", msg))


class DeployError(RuntimeError):
    """Fatal, already-explained failure."""


def load_token(token_file):
    if token_file:
        path = Path(token_file).expanduser()
        if not path.is_file():
            raise DeployError("--token-file not found: {0}".format(path))
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip().strip('"').strip("'")
            if line:
                return line
        raise DeployError("--token-file is empty: {0}".format(path))
    token = (os.environ.get("HA_TOKEN") or "").strip()
    if not token:
        raise DeployError("No HA token. Set HA_TOKEN or pass --token-file <path>.")
    return token


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #

def relative_files(root: Path) -> dict[str, Path]:
    found = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIR_NAMES for part in rel.parts):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        found[rel.as_posix()] = path
    return found


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_version(root: Path) -> str:
    try:
        return str(json.loads((root / "manifest.json").read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError) as exc:
        return "<unreadable: {0}>".format(exc)


def mirror(source: Path, target: Path, label: str) -> None:
    """Mirror ``source`` onto ``target`` (stale files deleted), then SHA256-verify."""
    src_files = relative_files(source)
    if not src_files:
        raise DeployError("Source has no deployable files: {0}".format(source))
    dst_files = relative_files(target) if target.is_dir() else {}
    stale = sorted(set(dst_files) - set(src_files))
    caches = sorted(p for p in target.rglob("*") if p.is_dir() and p.name in EXCLUDED_DIR_NAMES) \
        if target.is_dir() else []

    log("{0}: {1}".format(label, source))
    log("        -> {0}".format(target))
    log("  version {0}; {1} file(s) to copy, {2} stale, {3} __pycache__ dir(s)".format(
        manifest_version(source), len(src_files), len(stale), len(caches)))
    for rel in stale:
        log("    stale (will be deleted): {0}".format(rel))
    if _DRY_RUN:
        plan("copy {0} file(s) and SHA256-verify each one".format(len(src_files)))
        return

    for rel, src in sorted(src_files.items()):
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for rel in stale:
        (target / rel).unlink(missing_ok=True)
    for cache in caches:
        shutil.rmtree(cache, ignore_errors=True)

    problems = [rel for rel, src in src_files.items()
                if not (target / rel).is_file() or sha256(src) != sha256(target / rel)]
    problems += sorted(set(relative_files(target)) - set(src_files))
    if problems:
        for rel in problems:
            log("  FAIL {0}".format(rel))
        raise DeployError("{0} verification failed for {1} file(s); roll back with --restore"
                          .format(label, len(problems)))
    log("  OK: {0} file(s) verified byte-identical, deployed version {1}".format(
        len(src_files), manifest_version(target)))


def do_backup(share_root: Path, integration_dir: Path, backup_root: Path) -> None:
    step("BACKUP")
    dest = backup_root / (BACKUP_PREFIX + datetime.now().strftime("%Y%m%d-%H%M%S"))
    files = relative_files(integration_dir) if integration_dir.is_dir() else {}
    entries_file = share_root / CONFIG_ENTRIES_FILE
    log("  integration: {0} file(s){1}".format(
        len(files), "" if files else " (not installed yet)"))
    log("  config entries: {0}".format("present" if entries_file.is_file() else "missing"))
    plan("write backup to {0}".format(dest))
    if not _DRY_RUN:
        for rel, src in files.items():
            (dest / DOMAIN / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / DOMAIN / rel)
        if entries_file.is_file():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entries_file, dest / "core.config_entries")
        log("  backup written: {0}".format(dest))
    existing = sorted((p for p in backup_root.glob(BACKUP_PREFIX + "*") if p.is_dir()),
                      key=lambda p: p.name, reverse=True) if backup_root.is_dir() else []
    for old in existing[BACKUPS_TO_KEEP:]:
        plan("prune old backup {0}".format(old.name))
        if not _DRY_RUN:
            shutil.rmtree(old, ignore_errors=True)


# --------------------------------------------------------------------------- #
# HA API
# --------------------------------------------------------------------------- #

class HA:
    def __init__(self, host: str, port: int, token: str) -> None:
        self.base = "http://{0}:{1}".format(host, port)
        self.ws_url = "ws://{0}:{1}/api/websocket".format(host, port)
        self._token = token

    def request(self, path, method="GET", body=None, timeout=30.0):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, method=method, data=data, headers={
            "Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, raw.decode("utf-8", "replace")

    def ws(self, messages, timeout=60.0):
        """Authenticate, send each message, return each result payload."""
        try:
            import websockets
        except ImportError as exc:
            raise DeployError("websockets is needed: uv run --with websockets python "
                              "tools/deploy_to_ha.py ...") from exc

        async def run():
            async with websockets.connect(self.ws_url, max_size=64 * 1024 * 1024) as conn:
                json.loads(await conn.recv())
                await conn.send(json.dumps({"type": "auth", "access_token": self._token}))
                auth = json.loads(await conn.recv())
                if auth.get("type") != "auth_ok":
                    raise DeployError("websocket auth failed: {0}".format(auth.get("type")))
                results = []
                for index, message in enumerate(messages, start=1):
                    await conn.send(json.dumps(dict(message, id=index)))
                    while True:
                        reply = json.loads(await asyncio.wait_for(conn.recv(), timeout))
                        if reply.get("id") == index and reply.get("type") == "result":
                            break
                    if not reply.get("success"):
                        raise DeployError("{0} failed: {1}".format(message["type"], reply.get("error")))
                    results.append(reply.get("result"))
                return results

        return asyncio.run(run())

    def entries(self):
        status, body = self.request("/api/config/config_entries/entry?domain=" + DOMAIN)
        if status != 200:
            raise DeployError("listing config entries failed: HTTP {0}: {1}".format(status, body))
        return body


def do_restart(ha: HA) -> None:
    step("RESTART HOME ASSISTANT")
    plan("POST /api/services/homeassistant/restart, wait for /api/config state RUNNING")
    if _DRY_RUN:
        return
    try:
        status, body = ha.request("/api/services/homeassistant/restart", "POST", {})
        log("  restart request returned HTTP {0}".format(status))
        if status not in (200, 201):
            raise DeployError("restart refused: HTTP {0}: {1}".format(status, body))
    except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, OSError) as exc:
        log("  connection dropped while restarting ({0!r}) - expected".format(exc))

    started = time.monotonic()
    time.sleep(POLL_INTERVAL)
    deadline = started + RESTART_POLL_TIMEOUT + RUNNING_WAIT_TIMEOUT
    state = None
    while time.monotonic() < deadline:
        try:
            status, body = ha.request("/api/config", timeout=10.0)
            state = body.get("state") if status == 200 and isinstance(body, dict) else None
            if state == "RUNNING":
                log("  Home Assistant RUNNING after ~{0:.0f}s".format(time.monotonic() - started))
                return
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
            pass
        time.sleep(POLL_INTERVAL)
    raise DeployError("Home Assistant not RUNNING after restart (last state {0!r})".format(state))


def do_setup_entry(ha: HA, args) -> None:
    step("CONFIG ENTRY")
    existing = [] if _DRY_RUN else ha.entries()
    if existing:
        for entry in existing:
            log("  already configured: {0} ({1}) state={2}".format(
                entry.get("title"), entry.get("entry_id"), entry.get("state")))
        log("  skipping the config flow")
        return
    data = {"host": args.setup_entry, "port": args.charger_port, "unit_id": args.unit_id,
            "scan_interval": args.scan_interval}
    plan("run the {0} config flow with {1}".format(DOMAIN, data))
    if _DRY_RUN:
        return
    status, flow = ha.request("/api/config/config_entries/flow", "POST",
                              {"handler": DOMAIN, "show_advanced_options": False})
    if status != 200 or flow.get("type") != "form":
        raise DeployError("starting the config flow failed: HTTP {0}: {1}".format(status, flow))
    status, result = ha.request("/api/config/config_entries/flow/" + flow["flow_id"], "POST", data)
    if status != 200 or result.get("type") != "create_entry":
        raise DeployError("config flow did not create an entry: HTTP {0}: {1}".format(status, result))
    log("  created entry {0} ({1})".format(result.get("title"),
                                            (result.get("result") or {}).get("entry_id")))


def do_verify(ha: HA) -> bool:
    step("VERIFY")
    if _DRY_RUN:
        plan("check entry state, entities {0} and system_log".format(", ".join(VERIFY_KEYS)))
        return True
    ok = True
    deadline = time.monotonic() + ENTITY_CHECK_TIMEOUT
    while True:
        entries = ha.entries()
        states = [e.get("state") for e in entries]
        if states and all(s == "loaded" for s in states):
            break
        if time.monotonic() >= deadline:
            log("  FAIL config entry state(s): {0}".format(states or "no entry"))
            return False
        time.sleep(POLL_INTERVAL)
    log("  OK   config entry state: {0}".format(", ".join(states)))

    registry = [e for e in ha.ws([{"type": "config/entity_registry/list"}])[0]
                if e.get("platform") == DOMAIN]
    log("  {0} entities registered for {1}".format(len(registry), DOMAIN))
    by_key = {e["unique_id"].split("_", 1)[1]: e["entity_id"] for e in registry}
    for key in VERIFY_KEYS:
        entity_id = by_key.get(key)
        if entity_id is None:
            log("  FAIL no entity with unique_id *_{0}".format(key))
            ok = False
            continue
        status, state = ha.request("/api/states/" + entity_id)
        value = state.get("state") if status == 200 else "HTTP {0}".format(status)
        good = value not in ("unknown", "unavailable", None) and status == 200
        unit = (state.get("attributes") or {}).get("unit_of_measurement", "") if status == 200 else ""
        log("  {0} {1} = {2} {3}".format("OK  " if good else "FAIL", entity_id, value, unit))
        ok &= good

    entries = ha.ws([{"type": "system_log/list"}])[0] or []
    hits = [e for e in entries if "custom_components." + DOMAIN in json.dumps(e, default=str)]
    if hits:
        ok = False
        for entry in hits:
            log("  FAIL system_log [{0}] {1}: {2} (count={3})".format(
                entry.get("level"), entry.get("name"), entry.get("message"), entry.get("count")))
    else:
        log("  OK   no system_log entries from custom_components.{0} ({1} entries total)".format(
            DOMAIN, len(entries)))
    return ok


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def build_parser():
    parser = argparse.ArgumentParser(description="Deploy nexblue_modbus to Home Assistant.")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--deploy", action="store_true", help="mirror the integration onto the share")
    parser.add_argument("--restore", metavar="BACKUP_NAME", help="mirror a backup back instead")
    parser.add_argument("--restart", action="store_true", help="restart Home Assistant")
    parser.add_argument("--setup-entry", metavar="CHARGER_HOST",
                        help="create the config entry for this charger if none exists")
    parser.add_argument("--verify", action="store_true",
                        help="check entry, entities and log (implied by --restart/--setup-entry)")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--charger-port", type=int, default=502)
    parser.add_argument("--unit-id", type=int, default=200)
    parser.add_argument("--scan-interval", type=int, default=10)
    parser.add_argument("--host", default=os.environ.get("HA_HOST"),
                        help="Home Assistant host (or env HA_HOST); required")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--share-root", help=r"HA config share (default \\<host>\config)")
    parser.add_argument("--token-file")
    return parser


def main(argv=None) -> int:
    global _DRY_RUN
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.host:
        parser.error("--host is required (or set HA_HOST)")
    _DRY_RUN = args.dry_run
    if args.deploy and args.restore:
        log("--deploy and --restore are exclusive")
        return 2
    verify = args.verify or args.restart or bool(args.setup_entry)
    if not (args.deploy or args.restore or args.restart or args.setup_entry or verify):
        log("Nothing to do. Pass --deploy, --restore, --restart, --setup-entry or --verify.")
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    source = repo_root / "custom_components" / DOMAIN
    share_root = Path(args.share_root or r"\\{0}\config".format(args.host))
    integration_dir = share_root / "custom_components" / DOMAIN
    backup_root = share_root / BACKUP_DIR_NAME

    step("PLAN")
    log("  mode:   {0}".format("DRY RUN" if _DRY_RUN else "LIVE"))
    log("  source: {0}".format(source))
    log("  target: {0}".format(integration_dir))
    log("  HA:     http://{0}:{1}".format(args.host, args.port))

    ha = None
    if args.restart or args.setup_entry or verify:
        token = load_token(args.token_file)
        log("  token:  loaded ({0} chars, value never printed)".format(len(token)))
        ha = HA(args.host, args.port, token)

    if (args.deploy or args.restore) and not share_root.is_dir():
        raise DeployError("Share not reachable: {0} (mount it first, `net use` without a user)"
                          .format(share_root))
    if (args.deploy or args.restore) and not args.no_backup:
        do_backup(share_root, integration_dir, backup_root)
    if args.deploy:
        step("DEPLOY")
        mirror(source, integration_dir, "deploy")
    if args.restore:
        step("RESTORE")
        mirror(backup_root / args.restore / DOMAIN, integration_dir, "restore")
    if args.restart:
        do_restart(ha)
    if args.setup_entry:
        do_setup_entry(ha, args)
    healthy = do_verify(ha) if verify else True

    step("DONE")
    if not healthy:
        log("  FAILED - roll back with --restore {0}<timestamp> --restart".format(BACKUP_PREFIX))
        return 1
    log("  completed successfully")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DeployError as exc:
        log("ERROR: {0}".format(exc))
        sys.exit(1)
    except KeyboardInterrupt:
        log("Interrupted.")
        sys.exit(130)
