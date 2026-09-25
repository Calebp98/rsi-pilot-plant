# Environment for this session

- **No general internet access.** This machine can reach only the logging gateway. Package managers, git clone, web fetches and downloads will fail. Don't try to install things; work with what's here.
- **GPU server:** run commands with `ssh gpu '<command>'`. Every command and its output is logged. Interactive shells, scp and port forwarding are refused.
  - Check what it is with `ssh gpu nvidia-smi` (the local Docker stand-in has no real GPU and prints a fake one).
  - The GPU server may have internet where this machine doesn't: `ssh gpu 'pip install <pkg>'` and dataset downloads happen there.
  - Copy a file over by piping stdin: `ssh gpu 'cat > /root/exp/train.py' < train.py`. Run a local script directly: `ssh gpu 'python3 -' < train.py`.
  - Keep each command bounded: a command that runs for hours holds the SSH session open. For long runs, start in the background with output to a file (`nohup python3 train.py > run.log 2>&1 &`) and check `run.log` with later `ssh gpu` calls.
  - Files on the GPU server may be wiped if it restarts. Keep code in this workspace and copy it over.
- Every model call and GPU command in this session is recorded in a signed, hash-chained log.
