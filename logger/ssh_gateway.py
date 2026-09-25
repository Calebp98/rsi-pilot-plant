"""Command-only SSH gateway. The agent never gets credentials for the GPU server.

  agent --ssh <target>@logger:2222 'cmd'-->  gateway  --ssh (gateway's own key)-->  target

Each command is logged (kind "ssh_exec") before it runs, then its result ("ssh_result":
exit status, stdin, stdout, stderr, duration) once it finishes. Both are signed by the gateway key
and chained like every other entry. Shells, PTYs, SFTP/scp, agent and port forwarding are refused.

Env / Fly secrets:
  SSH_CLIENT_KEYS  "name:ssh-ed25519 AAAA...,name2:..."   agent keys allowed to connect
  SSH_TARGETS      "gpu=user@host:port,..."                 SSH username selects the target
  SSH_TARGET_HOSTKEYS "gpu=ssh-ed25519 AAAA...,..."         pinned target host keys (required)
  SSH_TARGET_LABELS   "gpu=Runpod RTX 3080 pod ...,..."       operator's description (declared, not checked)

Every PROBE_INTERVAL seconds the gateway connects to each target with the pinned host key and its
upstream key, runs nothing, and disconnects. The result is published in info() so the dashboard
shows whether the GPU is reachable now, not whether the last logged command worked.
"""

import asyncio
import base64
import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncssh

PORT = int(os.environ.get("SSH_PORT", "2222"))
MAX_CAPTURE = 1024 * 1024
TIMEOUT = int(os.environ.get("SSH_COMMAND_TIMEOUT", "3600"))
PROBE_INTERVAL = int(os.environ.get("SSH_PROBE_INTERVAL", "15"))


def fingerprint(openssh_pub):
    """SHA256:... as printed by ssh-keygen -l."""
    raw = base64.b64decode(openssh_pub.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def parse_map(env, sep="="):
    out = {}
    for item in os.environ.get(env, "").split(","):
        if sep in item:
            k, v = item.strip().split(sep, 1)
            out[k.strip()] = v.strip()
    return out


def load_or_create(path, comment):
    if not path.exists():
        key = asyncssh.generate_private_key("ssh-ed25519", comment=comment)
        key.write_private_key(str(path))
        path.chmod(0o600)
    return asyncssh.read_private_key(str(path))


class Gateway:
    def __init__(self, data_dir, log_entry):
        """log_entry(kind, data) appends a gateway-signed entry and returns {"seq", "hash"}."""
        self.log = log_entry
        self.host_key = load_or_create(Path(data_dir) / "ssh_host_ed25519", "lucid-logger host")
        self.upstream_key = load_or_create(Path(data_dir) / "upstream_ed25519", "lucid-logger upstream")
        self.clients = {}
        for name, key in parse_map("SSH_CLIENT_KEYS", ":").items():
            self.clients[name] = asyncssh.import_public_key(key)
        self.targets = {}
        hostkeys = parse_map("SSH_TARGET_HOSTKEYS")
        labels = parse_map("SSH_TARGET_LABELS")
        self.probes = {}
        for name, spec in parse_map("SSH_TARGETS").items():
            user, hostport = spec.split("@", 1)
            host, port = hostport.rsplit(":", 1) if ":" in hostport else (hostport, "22")
            self.targets[name] = {"user": user, "host": host, "port": int(port), "hostkey": hostkeys.get(name),
                                  "label": labels.get(name)}

    def info(self):
        return {
            "port": PORT,
            "host_pubkey": self.host_key.export_public_key().decode().strip(),
            "upstream_pubkey": self.upstream_key.export_public_key().decode().strip(),
            "clients": {n: k.export_public_key().decode().strip() for n, k in self.clients.items()},
            "targets": {n: {"user": t["user"], "host": t["host"], "port": t["port"], "hostkey_pinned": bool(t["hostkey"]),
                            "hostkey_sha256": fingerprint(t["hostkey"]) if t["hostkey"] else None,
                            "network": "fly-private" if t["host"].endswith(".internal") else "public",
                            "label": t["label"], "probe": self.probes.get(n)}
                        for n, t in self.targets.items()},
            "probe_interval_s": PROBE_INTERVAL,
        }

    def known_hosts(self, target):
        return asyncssh.import_known_hosts(f"{target['host']} {target['hostkey']}\n[{target['host']}]:{target['port']} {target['hostkey']}\n")

    async def probe(self, name, target):
        """Connect and authenticate with the pinned host key and upstream key; run nothing."""
        t0 = time.time()
        result = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ok": False}
        try:
            if not target["hostkey"]:
                raise ValueError("no pinned host key")
            async with asyncssh.connect(target["host"], port=target["port"], username=target["user"],
                                        client_keys=[self.upstream_key], known_hosts=self.known_hosts(target),
                                        agent_path=None, connect_timeout=10) as conn:
                result.update(ok=True, server_version=conn.get_extra_info("server_version"))
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
        result["latency_ms"] = int((time.time() - t0) * 1000)
        prev = self.probes.get(name) or {}
        # keep when it was last up and when the current run of failures began, so "down since" survives page reloads
        result["last_ok_at"] = result["checked_at"] if result["ok"] else prev.get("last_ok_at")
        result["failing_since"] = None if result["ok"] else (prev.get("failing_since") or result["checked_at"])
        self.probes[name] = result

    async def probe_loop(self):
        while True:
            await asyncio.gather(*(self.probe(n, t) for n, t in self.targets.items()))
            await asyncio.sleep(PROBE_INTERVAL)

    def client_for(self, key):
        for name, k in self.clients.items():
            if k.public_data == key.public_data:
                return name
        return None

    async def run_command(self, process):
        conn = process.channel.get_extra_info("connection")
        client, target_name = conn.get_extra_info("lucid_client"), conn.get_extra_info("username")
        command = process.command
        target = self.targets.get(target_name)
        if process.command is None or process.get_terminal_type():
            self.log("ssh_refused", {"client": client, "target": target_name, "reason": "interactive shell/pty not allowed"})
            process.stderr.write(b"lucid-logger: only non-interactive commands are allowed (ssh host 'cmd')\n")
            return process.exit(126)
        if target is None:
            self.log("ssh_refused", {"client": client, "target": target_name, "command": command, "reason": "unknown target"})
            process.stderr.write(f"lucid-logger: unknown target {target_name!r}\n".encode())
            return process.exit(126)
        if not target["hostkey"]:
            process.stderr.write(f"lucid-logger: target {target_name!r} has no pinned host key\n".encode())
            return process.exit(126)

        started = self.log("ssh_exec", {"client": client, "target": target_name,
                                        "upstream": f"{target['user']}@{target['host']}:{target['port']}",
                                        "command": command})
        t0 = time.time()
        out, err, inp, exit_status, error = bytearray(), bytearray(), bytearray(), None, None
        try:
            known = self.known_hosts(target)
            async with asyncssh.connect(target["host"], port=target["port"], username=target["user"],
                                        client_keys=[self.upstream_key], known_hosts=known,
                                        agent_path=None, connect_timeout=20) as up:
                upstream = await up.create_process(command, encoding=None)

                async def pump(src, dst, buf):
                    while chunk := await src.read(65536):
                        dst.write(chunk)
                        if len(buf) < MAX_CAPTURE:
                            buf += chunk[:MAX_CAPTURE - len(buf)]

                async def feed():
                    await pump(process.stdin, upstream.stdin, inp)
                    upstream.stdin.write_eof()

                feeder = asyncio.ensure_future(feed())
                await asyncio.wait_for(asyncio.gather(pump(upstream.stdout, process.stdout, out),
                                                      pump(upstream.stderr, process.stderr, err)), TIMEOUT)
                feeder.cancel()
                result = await upstream.wait()
                exit_status = result.exit_status if result.exit_status is not None else 255
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            process.stderr.write(f"lucid-logger: upstream error: {error}\n".encode())
            exit_status = 255
        self.log("ssh_result", {"exec_seq": started["seq"], "exec_hash": started["hash"], "target": target_name,
                                "exit_status": exit_status, "duration_ms": int((time.time() - t0) * 1000),
                                "stdin": inp.decode(errors="replace"), "stdin_truncated": len(inp) >= MAX_CAPTURE,
                                "stdout": out.decode(errors="replace"), "stderr": err.decode(errors="replace"),
                                "stdout_truncated": len(out) >= MAX_CAPTURE, "error": error})
        process.exit(exit_status)

    def server_factory(self):
        gw = self

        class Server(asyncssh.SSHServer):
            def connection_made(self, conn):
                self.conn = conn

            def begin_auth(self, username):
                return True

            def public_key_auth_supported(self):
                return True

            def validate_public_key(self, username, key):
                name = gw.client_for(key)
                if name:
                    self.conn.set_extra_info(lucid_client=name)
                return name is not None

        return Server

    async def serve(self):
        await asyncssh.create_server(
            self.server_factory(), "", PORT, server_host_keys=[self.host_key], encoding=None,
            process_factory=self.run_command, sftp_factory=None, allow_scp=False,
            agent_forwarding=False, x11_forwarding=False, allow_pty=False,
            line_editor=False, keepalive_interval=30)

    def start(self, loop):
        loop.run_until_complete(self.serve())
        loop.create_task(self.probe_loop())
        loop.run_forever()
