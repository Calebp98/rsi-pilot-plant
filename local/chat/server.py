"""Minimal local chat + coding-agent app for Tinfoil models, over a verified connection.

Setup: python3 -m venv .venv && .venv/bin/pip install tinfoil
Run:   TINFOIL_API_KEY=... .venv/bin/python server.py   then open http://127.0.0.1:8765
       WORKSPACE=/some/dir sets the folder the agent can touch (default ../workspace).

The Tinfoil SDK verifies the enclave's attestation at startup and pins the
connection to the attested keys; if verification fails the server won't start.
The key stays on this server; the browser only talks to localhost.
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tinfoil import TinfoilAI

DEFAULT_MODEL = "gpt-oss-120b"
PORT = 8765
KEY = os.environ.get("TINFOIL_API_KEY")
INDEX = Path(__file__).with_name("index.html")
WORKSPACE = Path(os.environ.get("WORKSPACE", Path(__file__).parent.parent / "workspace")).resolve()
MAX_STEPS = 25
MAX_READ = 100_000

SYSTEM_PROMPT = f"""You are a coding agent working in a local workspace folder.
Use the tools to inspect and change files. Paths are relative to the workspace root.
Read a file before editing it. Keep changes minimal and say briefly what you changed."""

TOOLS = [
    {"type": "function", "function": {
        "name": "list_files",
        "description": "List files and folders under a directory in the workspace (recursive, max 500 entries).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory relative to workspace root. Default '.'"}}},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file from the workspace.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a text file in the workspace. Creates parent folders.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace one exact occurrence of old_text with new_text in a workspace file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
            "required": ["path", "old_text", "new_text"]},
    }},
]

tf = None
models = {}


def connect():
    """Create a verified client and load model metadata through it."""
    global tf, models
    tf = TinfoilAI(api_key=KEY)
    models = {m.id: m.model_dump() for m in tf.client.models.list().data if getattr(m, "type", None) == "chat"}


def safe_path(rel):
    """Resolve a workspace-relative path, refusing anything that escapes the workspace."""
    p = (WORKSPACE / (rel or ".")).resolve()
    if p != WORKSPACE and WORKSPACE not in p.parents:
        raise ValueError(f"path outside workspace: {rel}")
    return p


def run_tool(name, args):
    try:
        if name == "list_files":
            root = safe_path(args.get("path", "."))
            entries = []
            for p in sorted(root.rglob("*")):
                if any(part.startswith(".") for part in p.relative_to(WORKSPACE).parts):
                    continue
                entries.append(str(p.relative_to(WORKSPACE)) + ("/" if p.is_dir() else ""))
                if len(entries) >= 500:
                    entries.append("... truncated")
                    break
            return "\n".join(entries) or "(empty)"
        if name == "read_file":
            text = safe_path(args["path"]).read_text(errors="replace")
            return text if len(text) <= MAX_READ else text[:MAX_READ] + "\n... truncated"
        if name == "write_file":
            p = safe_path(args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"])
            return f"wrote {len(args['content'])} chars to {args['path']}"
        if name == "edit_file":
            p = safe_path(args["path"])
            text = p.read_text()
            count = text.count(args["old_text"])
            if count != 1:
                return f"error: old_text found {count} times, need exactly 1"
            p.write_text(text.replace(args["old_text"], args["new_text"]))
            return f"edited {args['path']}"
        return f"error: unknown tool {name}"
    except Exception as e:
        return f"error: {e}"


def cost_of(model, usage):
    price = models.get(model, {}).get("pricing", {})
    return (
        usage.get("prompt_tokens", 0) * price.get("inputTokenPricePer1M", 0)
        + usage.get("completion_tokens", 0) * price.get("outputTokenPricePer1M", 0)
    ) / 1e6


def chat(messages, model, agent):
    """Run one user turn. In agent mode, loop through tool calls until the model answers.

    Returns the new messages to append to history plus display metadata.
    """
    if model not in models:
        return 400, {"error": f"unknown model {model}"}
    convo = ([{"role": "system", "content": SYSTEM_PROMPT}] if agent else []) + messages
    new, steps = [], []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    start = time.time()
    raw = data = None

    for _ in range(MAX_STEPS if agent else 1):
        kwargs = {"model": model, "messages": convo}
        if agent:
            kwargs["tools"] = TOOLS
        try:
            raw = tf.chat.completions.with_raw_response.create(**kwargs)
        except Exception as e:
            return 502, {"error": str(e)}
        data = raw.parse()
        msg = data.choices[0].message.model_dump()
        for k in usage_total:
            usage_total[k] += (data.usage.model_dump() if data.usage else {}).get(k, 0) or 0

        calls = msg.get("tool_calls") or []
        reasoning = msg.get("reasoning") or msg.get("reasoning_content")
        entry = {"role": "assistant", "content": msg.get("content") or ""}
        if calls:
            entry["tool_calls"] = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["function"]["name"], "arguments": c["function"]["arguments"]}}
                for c in calls
            ]
        convo.append(entry)
        new.append(entry)
        if not calls:
            break

        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except ValueError:
                args = {}
            result = run_tool(name, args)
            steps.append({"tool": name, "args": args, "result": result[:2000], "reasoning": reasoning})
            reasoning = None
            tool_msg = {"role": "tool", "tool_call_id": c["id"], "content": result}
            convo.append(tool_msg)
            new.append(tool_msg)
    else:
        new.append({"role": "assistant", "content": f"(stopped after {MAX_STEPS} steps)"})

    final = new[-1] if new and new[-1]["role"] == "assistant" else {"content": ""}
    return 200, {
        "new_messages": new,
        "content": final.get("content") or "",
        "reasoning": reasoning,
        "steps": steps,
        "finish_reason": data.choices[0].finish_reason if data else None,
        "usage": usage_total,
        "cost_usd": cost_of(model, usage_total),
        "latency_s": round(time.time() - start, 2),
        "id": data.id if data else None,
        "system_fingerprint": data.system_fingerprint if data else None,
        "enclave": raw.headers.get("tinfoil-enclave") if raw else None,
        "predicates": raw.headers.get_list("tinfoil-pt") if raw else [],
    }


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/info":
            doc = tf.get_verification_document()
            self.send_json(200, {
                "models": models, "default_model": DEFAULT_MODEL, "workspace": str(WORKSPACE),
                "verification": doc.to_dict() if doc else None,
            })
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/verify":
            try:
                connect()
            except Exception as e:
                return self.send_json(502, {"error": str(e)})
            return self.send_json(200, tf.get_verification_document().to_dict())
        if self.path != "/api/chat":
            return self.send_json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        self.send_json(*chat(body.get("messages", []), body.get("model", DEFAULT_MODEL), bool(body.get("agent"))))

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    if not KEY:
        raise SystemExit("Set TINFOIL_API_KEY first.")
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    print("Verifying enclave...", flush=True)
    connect()
    print(f"Verified. Workspace: {WORKSPACE}\nhttp://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
