# Pre-registration: autoresearch on the pilot plant

Written 2026-09-25 ~15:50 UTC, before the noise reruns finished and before the agent started. It's sent to the GPU with `ssh gpu 'cat > /root/PREREGISTRATION.md'`, so its exact text sits in the signed log ahead of any results. Check that with `verify_run.py` and the log entry's timestamp.

## Setup

- Reference: karpathy/autoresearch @ 228791f. Human platform commit 6b6e390: remote-GPU section in `program.md`, and `DEVICE_BATCH_SIZE` 128 → 8. Nothing else changed.
- GPU: RTX 3080 10 GB (Runpod Community pod 7ugnpeavuvapws). Agent: OpenCode + Kimi K3 via Tinfoil, on the Sprite, with no internet.
- Baseline (one run, log 20260924T143705Z…, 15:30 UTC): val_bpb 1.251312, training 302.1 s, total 404.3 s, peak VRAM 3374 MB, 126 steps, 66.1M tokens.

## Hypotheses and predictions

**H1: every experiment can be rebuilt from the log.** Each `results.tsv` row has a logged upload of exactly that commit's `train.py`, then a launch, then logged output with the same val_bpb. Model calls pair tap/gateway with 0 differences, 0 gateway-only and 0 tap-only. The GPU's `train.py` equals the last logged upload.
*Falsified by* any row or file `verify_run.py` can't match.

**H2: the loop improves the model on its own.**
- ≥ 6 experiments per hour, with no human input after the go-ahead.
- Kept val_bpb goes down monotonically.
- The first kept change alters `TOTAL_BATCH_SIZE` (down) and/or `DEVICE_BATCH_SIZE` (up). The baseline is starved of steps: 126 steps vs 953 on the H100, with 3.4 of 10 GB used.
- Best ≤ 1.231 (baseline − 0.02) within the first 10 experiments; best ≤ 1.20 within 3 hours.

*Falsified by* stalls or permission requests, or no improvement beyond 3σ after 20 experiments.

**H3: the improvements are real.**
- Run-to-run σ of the unchanged baseline ≤ 0.003 (measured from 3 runs: the baseline plus 2 reruns).
- Final best vs baseline, each rerun twice from scratch: gap > 3σ.
- `prepare.py`, `pyproject.toml` and `uv.lock` are never changed. The agent's commits touch only `train.py`. Every run trains 290–330 s.

*Falsified by* the rerun gap falling within 3σ, or any rule break.

**H4: cost is as estimated.**
- GPU $0.17/hr.
- Kimi K3: $0.50–1.50 per experiment, $4–12/hr.

*Checked after 2 experiments* from the token counts in the log. If it's over $2 per experiment, pause and review.

## Stop conditions

- The human interrupts.
- Model spend passes $25, or the checker reports a problem.
- The GPU watchdog stops the pod after 60 idle minutes.
