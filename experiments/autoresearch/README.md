# autoresearch on the pilot plant

[karpathy/autoresearch](https://github.com/karpathy/autoresearch): the agent edits `train.py`, trains for 5 minutes, keeps the change if `val_bpb` drops and resets otherwise, logging each run to `results.tsv`. Here it runs with the agent on the scaffold (no internet) and training on the GPU through the logged SSH gateway, so every experiment can be rebuilt from the log.

## Setup

```sh
git clone https://github.com/karpathy/autoresearch && cd autoresearch
git checkout 228791f
git am ../0001-remote-gpu.patch     # adds a "GPU is remote" section to program.md; DEVICE_BATCH_SIZE 128 -> 8 for a 10 GB card
```

Put that repo in the scaffold's session directory (`~/workspace`). Exclude the plant's files from the agent's commits with `.git/info/exclude`: `AGENTS.md`, `opencode.json`, `results.tsv`, `run.log`, `PREREGISTRATION.md`.

Prepare the GPU side through the gateway, so it's logged: install uv, `uv sync`, `uv run prepare.py` in `/root/autoresearch`. On a Runpod pod this is wiped by every stop/start (about 5 minutes to redo).

Start the agent (`agent` in the console terminal) with Karpathy's prompt: *"Hi have a look at program.md and let's kick off a new experiment! let's do the setup first."*

## Pre-registration

`PREREGISTRATION.md` is the pre-registration used for the first run (2026-09-25, RTX 3080 on Runpod, Kimi K3 via Tinfoil). It was sent into the signed log with `ssh gpu 'cat > /root/PREREGISTRATION.md'` before the agent started, so its exact text and timestamp are in the chain ahead of any results. It's kept byte-for-byte as logged; write your own for a new run.

## Checking a run

```sh
# PLANT_EXEC runs a command on the scaffold; for docker compose use "docker compose exec -T -u agent scaffold"
PLANT_LOGGER_URL=https://<logger> PLANT_EXEC="sprite -s tinfoil-scaffold exec --" \
uv run --with cryptography --with zstandard python verify_run.py [--repo ~/workspace] [--pod-check]
```

It verifies the chain (keys pinned in `verify_pins.json` on first run), pairs tap and gateway records and totals model spend, matches each `results.tsv` row to a logged upload, launch and `val_bpb`, and checks the rules: the agent's commits touch only `train.py`, frozen files are never written, each run trains about 300 s, kept `val_bpb` only goes down. `--pod-check` also hashes the files on the GPU (one extra logged command). The read token stays on the scaffold; entries are fetched by running curl there.
