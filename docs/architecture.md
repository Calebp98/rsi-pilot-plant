# Architecture

Three machines and one rule: the agent's machine (the **scaffold**) can reach only the **logger**, and the logger is the only way to reach the model or the **GPU server**. So everything the agent does to the outside world passes through one place that records it.

## Components

### Logger (`logger/`)

One Python process with three parts that share a single log.

**Log (`app.py`).** An append-only JSONL file, one entry per line:

| Field | Meaning |
|---|---|
| `seq` | 1, 2, 3, … with no gaps |
| `received_at` | logger's clock |
| `client` | which registered client signed the payload (`scaffold`, `gateway`) |
| `payload` | the exact JSON string the client signed: `client_seq`, `ts`, `kind`, `data` |
| `client_sig` | client's ed25519 signature over the payload bytes. The logger can't forge client entries |
| `prev` | previous entry's `hash` (64 zeros for the first) |
| `hash` | sha256 of the canonical record: `json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False)` |
| `logger_sig` | logger's ed25519 signature over `hash`. Editing or deleting an entry breaks the chain |

`client_seq` must increase per client, which blocks replayed entries. The logger's signing key is generated on first boot in the data volume and never leaves it.

Endpoints:

- Public: `/v1/info` (keys, head, source file hashes, image ref, gateway state, Tinfoil verification result), `/v1/head`, `/healthz`
- Read token: `/v1/entries?after=N&limit=M`
- Signed by a registered client: `/v1/append`, `/v1/rotate`

**Rotation.** A new agent session closes the current chain and opens a linked one. The client's signed `rotate` request becomes the old chain's last entry; the file is archived; the new chain's entry #1 is a gateway-signed `log_opened` that names the previous log ID, its final hash, the session's manifest (model, tool versions, config hashes, Tinfoil release, GPU target), and every client's last `client_seq` so replay protection carries over. There is deliberately no delete endpoint.

**Model gateway (`model_gateway.py`).** `/v1/model/*` is an OpenAI-compatible relay. Clients authenticate with a model token that works only here; the gateway swaps it for the Tinfoil API key (which exists only on the logger) and forwards to `tinfoil-proxy` on localhost. `tinfoil-proxy` checks the Tinfoil router enclave's attestation and pins the connection to the attested TLS key. After each exchange the gateway logs a gateway-signed `model_call` with the request and response bodies (never either key). Tinfoil's Python SDK verifier also runs every 10 minutes and its result is published in `/v1/info`.

**SSH gateway (`ssh_gateway.py`).** Port 2222, command-only: `ssh gpu 'cmd'` works; shells, PTYs, scp/sftp, and port or agent forwarding are refused. The SSH username picks the target. It logs `ssh_exec` before running the command, then `ssh_result` (exit status, stdin, stdout, stderr up to 1 MB each, duration) linked back by seq and hash. Refusals are logged as `ssh_refused`. The agent holds a key for the gateway only; the gateway holds the only key the GPU server accepts, and pins the GPU server's host key. Every 15 s it probes each target (connect, authenticate, run nothing) and publishes the result, so the dashboard shows whether the GPU is reachable now.

Config is all environment variables; see the docstrings at the top of each file and [`tools/init.py`](../tools/init.py) for a complete example.

### Scaffold (`scaffold/`)

The agent's machine. On Fly it's a Sprite; locally it's the `scaffold` container.

- **OpenCode** with a session template (`session-template/opencode.json`, `AGENTS.md`). Its model provider points at the tap.
- **Tap (`tap/tap.py`)** on 127.0.0.1:3300. Streams model traffic through to the logger's model gateway unchanged, then sends its own scaffold-signed `model_call` record. That's a second, independent witness: the dashboard pairs tap and gateway records by request body and flags any difference. A gateway-only call means something skipped the tap; a tap-only call means traffic didn't go through the gateway. Records spool to disk while the logger is unreachable.
- **Dashboard (`dashboard/`)**. `server.py` serves status and the **New session** button (stop the agent, rotate the log, fresh directory and OpenCode state, restart). `index.html` fetches log entries from the logger directly and verifies every hash and signature in the browser with WebCrypto. It records keys and heads it has seen in localStorage and flags changed keys or rewritten history. The Map tab draws every connection in the system, with live status and trust gaps.
- **Browser terminal**: ttyd attached to tmux session `main`, behind Caddy with a password. Optional read-only view link (`/view/<secret>/`) for sharing: no terminal input, no write actions.

### GPU server

Anything running sshd that accepts the gateway's upstream key. In this repo:

- `gpu-standin/`: Debian + sshd on port 2200 + a fake `nvidia-smi`. Used by Docker Compose and CI.
- A Runpod pod (see [deploy-fly.md](deploy-fly.md#gpu-runpod-pod)), with `gpu-runpod/watchdog.sh` stopping it after an hour idle.

## Traffic paths

**Model call:** OpenCode → tap (scaffold) → logger `/v1/model/…` → model gateway (adds key) → tinfoil-proxy (verifies, pins) → Tinfoil router enclave → model enclave. Logged twice: by the tap (scaffold-signed) and by the gateway (gateway-signed).

**GPU command:** OpenCode runs `ssh gpu 'cmd'` → logger :2222 → SSH gateway → GPU server sshd. Logged as `ssh_exec` then `ssh_result`, both gateway-signed.

**Log read:** your browser or `tools/verify_log.py` → logger `/v1/info` + `/v1/entries` → verified locally.

## Local (Docker Compose) vs Fly

| | Compose | Fly |
|---|---|---|
| Scaffold egress restriction | Docker `internal` network: only the logger is on it | Sprites egress policy set through the Sprites API: allowlist of the logger's domain |
| Logger URL from scaffold | `http://logger:8080` (private network) | `https://<app>.fly.dev` (TLS terminated at Fly's edge) |
| GPU | stand-in container on a second internal network | Runpod pod, or the stand-in on Fly's private network |
| Services | supervisord | `sprite-env services` |
| Keys | `docker compose run --rm init` | generated on each machine, registered through Fly secrets |

The dashboard map is drawn for the Fly deployment; in Compose the Fly edge and control-plane boxes don't exist.
