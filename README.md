# RSI pilot plant

A small, inspectable setup for running an AI coding agent where model calls and GPU commands go through a logging gateway that writes a signed, hash-chained record. Model inference runs in [Tinfoil](https://tinfoil.sh) confidential-computing enclaves.

See [docs/architecture.md](docs/architecture.md) for the full picture and [docs/trust.md](docs/trust.md) of what is and isn't proven.

```
            ┌──────────── scaffold (no internet) ──────────--──┐
            │  OpenCode agent ──► tap :3300 (witness record)   │
            │        │                  │                      │
            │     ssh gpu 'cmd'         │ model calls          │
            └────────┼──────────────────┼──────────────────────┘
                     ▼                  ▼
            ┌──────────────────── logger ─────────────────-────┐
            │  SSH gateway :2222      model gateway /v1/model  │──► tinfoil-proxy ──► Tinfoil enclaves
            │  (commands only)        (adds the Tinfoil key)   │    (verifies attestation, pins TLS key)
            │            └──► signed hash-chained log ◄──┘     │
            └────────┼─────────────────────────────────────────┘
                     ▼
               GPU server (sshd; only the gateway's key)
```

You can also see this somewhat AI sloppy diagram with the implementation we're using at Lucid at the time of publishing.

<img width="1002" height="614" alt="image" src="https://github.com/user-attachments/assets/72a2ad76-cf77-40a9-aa94-0b3a50f60dbe" />


## Quick start (Docker)

Needs Docker with Compose v2.24+. Without a tinfoil API key everything works except the model replies (they come back as errors, and those are logged too).

```sh
git clone https://github.com/Calebp98/rsi-pilot-plant && cd rsi-pilot-plant
docker compose run --rm init                  # generates every key and token into ./state
export TINFOIL_API_KEY=...                    # optional, from https://tinfoil.sh
docker compose up -d                          # pulls images from GHCR; add --build to build locally
```

Open http://127.0.0.1:8080 (user `lucid`, password printed by `init`, also in `state/scaffold/console_password`). The left pane is a terminal on the scaffold: run `agent` to start OpenCode. The right pane shows the log, verified in your browser, and a live map of the system.

From the terminal, try:

```sh
ssh gpu nvidia-smi          # goes through the logger's SSH gateway; the stand-in prints a fake GPU
curl -m5 https://pypi.org   # fails: the scaffold can only reach the logger
```

Then check the log from your own machine, without trusting the dashboard:

```sh
uv run --with cryptography python tools/verify_log.py \
  --logger http://127.0.0.1:8081 --token-file state/scaffold/read_token
```

`tests/smoke.sh` runs all of this end to end with no credentials; CI runs it on every push.

## Other ways to run it

| Setup | What you get | Guide |
|---|---|---|
| Docker Compose | logger + scaffold + stand-in GPU on your machine | above |
| Fly.io + Sprite + Runpod | Logger on a Fly Machine, scaffold on a Fly Sprite with an egress allowlist, a GPU pod | [docs/deploy-fly.md](docs/deploy-fly.md) |
| Laptop only | OpenCode or a minimal chat app talking to Tinfoil through a local verifying proxy; no logger | [local/](local/README.md) |

## Layout

| Path | What |
|---|---|
| `logger/` | Append-only signed log (`app.py`), model gateway to Tinfoil (`model_gateway.py`), command-only SSH gateway (`ssh_gateway.py`). Image: `ghcr.io/calebp98/rsi-pilot-plant-logger` |
| `scaffold/` | The agent's machine: tap, dashboard + map, OpenCode session template, browser terminal. Image: `ghcr.io/calebp98/rsi-pilot-plant-scaffold` |
| `gpu-standin/` | sshd with a fake `nvidia-smi`, for testing without a GPU. Image: `ghcr.io/calebp98/rsi-pilot-plant-gpu-standin` |
| `gpu-runpod/` | Idle watchdog for a Runpod GPU pod |
| `tools/` | `init.py` (local keys), `verify_log.py` (independent chain check) |
| `local/` | Laptop-only agent and chat app |
| `experiments/autoresearch/` | Running [karpathy/autoresearch](https://github.com/karpathy/autoresearch) on the plant, with a pre-registration and a run checker |

## Security notes

- `state/` holds the local plant's private keys and tokens. It's gitignored; don't commit it.
- The console terminal is a full shell on the scaffold. Compose binds it to 127.0.0.1 only.
- The Tinfoil key is only ever given to the logger, from your shell environment. The scaffold has a model token that works only at the logger.
- Found a security problem? Report it privately through the repo's Security tab ("Report a vulnerability") rather than a public issue.

## License

MIT. See [LICENSE](LICENSE).
