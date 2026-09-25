"""Sprite dashboard: status JSON + page. The terminal (ttyd) is served separately at /term/.

Runs on 127.0.0.1:8000 behind Caddy on :8080. The Sprite holds no Tinfoil key and (with the egress policy on)
can only reach the logger, so everything about Tinfoil (verification result, proxy state) is read from what the
logger publishes at /v1/info. The browser fetches and verifies the log itself; this server only tells it where.
"""

import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
HOME = Path.home()
PORT = int(os.environ.get("DASHBOARD_PORT", "8000"))
TAP_CFG = Path.home() / ".config/tap"
POLICY = Path("/.sprite/policy/network.json")
SESSIONS = Path.home() / "sessions"
WORKSPACE = Path.home() / "workspace"
TEMPLATE_DIR = HERE / "session-template"
TEMPLATE = TEMPLATE_DIR / "opencode.json"
WATCHED = [Path.home() / p for p in ("tap/tap.py", "dashboard/server.py", "dashboard/index.html",
                                     "dashboard/Caddyfile", "workspace/opencode.json", ".local/bin/agent")]
# View-only link: /view/<secret>/ serves the page without the terminal or any write action, with no password
# (Caddy forwards /view/* without basic auth). The log's read token never goes to the browser: entries are fetched
# through /view/<secret>/api/logger/..., and the browser still verifies every hash and signature itself.
# Revoke by changing or deleting this file; no restart needed.
VIEW_SECRET = Path.home() / ".config/dashboard/view_secret"
VIEW_LOGGER_PATHS = ("/v1/info", "/v1/head", "/v1/entries")
session_lock = threading.Lock()
_logger_cache = {"at": 0, "info": None, "ms": None, "error": None}


def tap_cfg():
    p = TAP_CFG / "config.json"
    return json.loads(p.read_text()) if p.exists() else {}


def logger_info(max_age=5):
    """The logger's public /v1/info (Tinfoil verification, proxy state, keys, head), cached briefly."""
    if time.time() - _logger_cache["at"] < max_age:
        return _logger_cache
    t0 = time.time()
    try:
        with urllib.request.urlopen(tap_cfg()["logger_url"].rstrip("/") + "/v1/info", timeout=10) as r:
            _logger_cache.update(info=json.load(r), error=None)
    except Exception as e:
        _logger_cache.update(info=None, error=str(e))
    _logger_cache.update(at=time.time(), ms=round((time.time() - t0) * 1000))
    return _logger_cache


def sha256_of(path):
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def run(*cmd, timeout=20):
    env = {**os.environ, "PATH": f"{HOME}/.elan/bin:{HOME}/.local/bin:/.sprite/bin:" + os.environ.get("PATH", "")}
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env).stdout.strip() or None
    except Exception:
        return None


def services():
    """Sprite services, or supervisord programs when running in the Docker image."""
    if shutil.which("sprite-env"):
        try:
            out = subprocess.run(["sprite-env", "services", "list"], capture_output=True, text=True, timeout=10).stdout
            return [{"name": s["name"], "status": s["state"]["status"], "started": s["state"].get("started_at")}
                    for s in json.loads(out)]
        except Exception as e:
            return [{"name": "error", "status": str(e)}]
    try:
        out = subprocess.run(["supervisorctl", "status"], capture_output=True, text=True, timeout=10).stdout
        return [{"name": line.split()[0], "status": "running" if line.split()[1] == "RUNNING" else line.split()[1].lower(),
                 "started": None} for line in out.splitlines() if len(line.split()) > 1]
    except Exception as e:
        return [{"name": "error", "status": str(e)}]


def resolves(host):
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


def egress_policy():
    """The Sprite's outbound allowlist. It's set from outside via the Sprites API and can't be changed in here.
    We report the rules file if the platform provides one, and always what we observe: blocked domains shouldn't resolve."""
    logger_host = urllib.parse.urlparse(tap_cfg().get("logger_url", "")).hostname
    probes = {h: resolves(h) for h in ("inference.tinfoil.sh", "pypi.org", "github.com")}
    observed = {"logger_resolves": resolves(logger_host) if logger_host else None,
                "others_resolve": probes}
    active = observed["logger_resolves"] and not any(probes.values())
    rules = []
    if POLICY.exists():
        try:
            rules = json.loads(POLICY.read_text()).get("rules", [])
        except Exception:
            pass
    return {"active": bool(active), "rules": rules or ([{"domain": logger_host, "action": "allow"}] if active else []),
            "source": "rules file" if POLICY.exists() else "observed from inside (DNS)", "observed": observed}


def current_session():
    if WORKSPACE.is_symlink():
        target = WORKSPACE.resolve()
        meta = target.parent / f"{target.name}.state" / "SESSION.json"
        return {"id": target.name, "dir": str(target), **(json.loads(meta.read_text()) if meta.exists() else {})}
    return {"id": None, "dir": str(WORKSPACE), "note": "pre-session workspace (no New session run yet)"}


def manifest(session_id):
    """What this session runs with, so a run can be reproduced or compared later."""
    info = logger_info(max_age=0)["info"] or {}
    v = (info.get("model") or {}).get("verification") or {}
    tpl = json.loads(TEMPLATE.read_text())
    return {
        "session": session_id,
        "model": tpl.get("model"),
        "model_path": "opencode -> tap (sprite) -> logger model gateway -> tinfoil-proxy (logger) -> tinfoil",
        "opencode_config_sha256": sha256_of(TEMPLATE),
        "opencode_version": run(shutil.which("opencode") or str(HOME / ".local/bin/opencode"), "--version"),
        "agent_script_sha256": sha256_of(Path.home() / ".local/bin/agent"),
        "tap_sha256": sha256_of(Path.home() / "tap/tap.py"),
        "egress_policy": egress_policy(),
        "tinfoil": {"verified": v.get("ok"), "release": v.get("release"), "repo": v.get("repo"),
                    "measurement": v.get("measurement"), "tls_key": v.get("tls_key"), "verified_by": "logger"},
        "logger": {"source_sha256": info.get("source_sha256"), "image_ref": info.get("image_ref")},
        "gpu_targets": (info.get("ssh") or {}).get("targets"),
        "session_files_sha256": {f.name: sha256_of(f) for f in sorted(TEMPLATE_DIR.iterdir()) if f.is_file()},
        "lean": {"lean": run("lean", "--version"), "lake": run("lake", "--version"), "elan": run("elan", "--version"),
                 "lean_binary_sha256": sha256_of(run("elan", "which", "lean") or "/nonexistent")},
        "gpu_state_reset": False,
    }


def tap_post(path, body):
    req = urllib.request.Request("http://127.0.0.1:3300" + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def new_session():
    """Stop the agent, rotate the log (linked), give the agent a fresh directory and state, restart the terminal."""
    if not session_lock.acquire(blocking=False):
        return 409, {"error": "a new session is already being created"}
    try:
        sid = time.strftime("s-%Y%m%d-%H%M%S", time.gmtime())
        man = manifest(sid)
        subprocess.run(["tmux", "kill-session", "-t", "main"], capture_output=True)  # stops OpenCode
        time.sleep(1.5)  # let the tap deliver anything the agent's last call produced
        code, rot = 409, {}
        for _ in range(10):
            code, rot = tap_post("/_tap/rotate", {"session": sid, "manifest": man})
            if code != 409:
                break
            time.sleep(2)
        if code != 200:
            subprocess.run(["tmux", "new-session", "-d", "-s", "main", "-c", str(WORKSPACE)], capture_output=True)
            return 502, {"error": f"log rotation failed ({code}): {rot.get('error')}; nothing else changed"}
        SESSIONS.mkdir(exist_ok=True)
        if WORKSPACE.exists() and not WORKSPACE.is_symlink():  # first run: keep the old workspace as a session
            WORKSPACE.rename(SESSIONS / time.strftime("legacy-%Y%m%d-%H%M%S", time.gmtime()))
        d = SESSIONS / sid
        d.mkdir()
        for f in TEMPLATE_DIR.iterdir():  # opencode.json, AGENTS.md
            if f.is_file():
                (d / f.name).write_text(f.read_text())
        st = SESSIONS / f"{sid}.state"
        st.mkdir()
        (st / "SESSION.json").write_text(json.dumps({"created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                                    "log": rot, "manifest": man}, indent=1))
        tmp = Path.home() / ".workspace.tmp"
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        tmp.symlink_to(d)
        tmp.replace(WORKSPACE)  # atomic switch of ~/workspace
        subprocess.run(["tmux", "new-session", "-d", "-s", "main", "-c", str(d)], capture_output=True)
        return 200, {"session": sid, "dir": str(d), "log": rot, "manifest": man}
    finally:
        session_lock.release()


def opencode_base_url():
    try:
        cfg = json.loads((WORKSPACE / "opencode.json").read_text())
        return cfg["provider"]["tinfoil"]["options"]["baseURL"]
    except Exception:
        return None


def machine():
    load = os.getloadavg()
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0]) * 1024
    disk = shutil.disk_usage("/")
    up = float(Path("/proc/uptime").read_text().split()[0])
    return {"load": [round(x, 2) for x in load], "cpus": os.cpu_count(),
            "mem_used": mem["MemTotal"] - mem["MemAvailable"], "mem_total": mem["MemTotal"],
            "disk_used": disk.used, "disk_total": disk.total, "uptime_s": int(up)}


def tap():
    """Tap health plus what the browser needs to read and verify the logger directly."""
    token = (TAP_CFG / "read_token").read_text().strip() if (TAP_CFG / "read_token").exists() else None
    try:
        with urllib.request.urlopen("http://127.0.0.1:3300/_tap/status", timeout=3) as r:
            live = json.load(r)
    except Exception as e:
        live = {"error": str(e)}
    # browser_logger_url: where the browser reaches the logger, when that differs from the tap's URL
    # (docker compose: the tap uses http://logger:8080, the browser http://127.0.0.1:8081).
    cfg = tap_cfg()
    return {"logger_url": cfg.get("browser_logger_url") or cfg.get("logger_url"), "read_token": token, "status": live}


def status():
    lg = logger_info()
    model = (lg["info"] or {}).get("model") or {}
    return {
        "tap": tap(),
        "session": current_session(),
        "files": {str(p.relative_to(Path.home())): sha256_of(p) for p in WATCHED},
        "opencode_base_url": opencode_base_url(),
        "egress": egress_policy(),
        "tinfoil_key_on_sprite": (Path.home() / ".config/tinfoil/api_key").exists(),
        "machine": machine(),
        "services": services(),
        "logger_reach": {"ok": lg["info"] is not None, "ms": lg["ms"], "error": lg["error"]},
        # Tinfoil facts, as published by the logger (which runs tinfoil-proxy and Tinfoil's SDK verifier)
        "verification": model.get("verification"),
        "verify_error": model.get("verify_error"),
        "model_gateway": {k: model.get(k) for k in ("clients", "key_configured", "proxy", "proxy_log")},
    }


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="application/json"):
        body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def view(self):
        """Serve /view/<secret>/...: the page, status minus the read token, and read-only logger requests."""
        parts = self.path.split("/", 3)  # ['', 'view', secret, rest]
        secret = VIEW_SECRET.read_text().strip() if VIEW_SECRET.exists() else ""
        if len(parts) < 4 or not secret or not hmac.compare_digest(parts[2].encode(), secret.encode()):
            return self.send(404, {"error": "not found"})
        rest = "/" + parts[3]
        if rest in ("/", "/index.html"):
            return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if rest == "/api/status":
            st = status()
            st["tap"] = {k: v for k, v in (st.get("tap") or {}).items() if k != "read_token"}
            st["view_only"] = True
            return self.send(200, st)
        if rest.startswith("/api/logger/"):
            path = rest[len("/api/logger"):]
            if path.split("?")[0] not in VIEW_LOGGER_PATHS:
                return self.send(404, {"error": "not found"})
            cfg = tap_cfg()
            req = urllib.request.Request(cfg["logger_url"].rstrip("/") + path)
            if path.startswith("/v1/entries"):
                req.add_header("Authorization", "Bearer " + (TAP_CFG / "read_token").read_text().strip())
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return self.send(r.status, r.read())
            except urllib.error.HTTPError as e:
                return self.send(e.code, {"error": f"logger HTTP {e.code}"})
            except Exception as e:
                return self.send(502, {"error": str(e)})
        return self.send(404, {"error": "not found"})

    def view_auth(self):
        """Caddy forward_auth for the read-only terminal: 200 only if the original URI carries the view secret."""
        uri = self.headers.get("X-Forwarded-Uri", "")
        parts = uri.split("/", 3)
        secret = VIEW_SECRET.read_text().strip() if VIEW_SECRET.exists() else ""
        good = len(parts) >= 3 and parts[1] == "view" and secret and hmac.compare_digest(parts[2].encode(), secret.encode())
        return self.send(200 if good else 403, {"ok": bool(good)})

    def do_GET(self):
        if self.path == "/_view_auth":
            return self.view_auth()
        if self.path.startswith("/view/"):
            return self.view()
        if self.path in ("/", "/index.html"):
            return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/status":
            return self.send(200, status())
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/new-session":
            return self.send(*new_session())
        if self.path == "/api/refresh":
            _logger_cache["at"] = 0
            return self.send(200, status())
        self.send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
