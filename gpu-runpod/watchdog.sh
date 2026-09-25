#!/bin/bash
# Stops this Runpod pod after IDLE_MIN consecutive minutes with no GPU work, so a forgotten pod doesn't keep billing.
# Busy = GPU utilisation above 5%, any process on the GPU, or a train.py / prepare.py / uv sync process running.
# The stop uses the API key Runpod puts in the container's environment (PID 1). It's read here at stop time only,
# never printed or written anywhere.
# State: /root/lucid-watchdog.state (overwritten each minute). Events: /root/lucid-watchdog.log.
# Installed through the logger's SSH gateway; a pod stop wipes the container disk, so reinstall after a restart.
IDLE_MIN=${IDLE_MIN:-60}
idle=0
while true; do
  util=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -dc 0-9)
  apps=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | grep -c .)
  work=$(pgrep -fc 'train\.py|prepare\.py|uv sync')
  if [ "${util:-0}" -gt 5 ] || [ "$apps" -gt 0 ] || [ "$work" -gt 0 ]; then idle=0; else idle=$((idle + 1)); fi
  echo "$(date -u +%FT%TZ) util=${util:-?}% gpu_procs=$apps work_procs=$work idle=${idle}/${IDLE_MIN}min" > /root/lucid-watchdog.state
  if [ "$idle" -ge "$IDLE_MIN" ]; then
    echo "$(date -u +%FT%TZ) GPU idle for ${IDLE_MIN} min: stopping pod" >> /root/lucid-watchdog.log
    env $(tr '\0' '\n' < /proc/1/environ | grep -E '^RUNPOD_(API_KEY|POD_ID)=') \
      sh -c 'runpodctl stop pod "$RUNPOD_POD_ID"' >> /root/lucid-watchdog.log 2>&1
    echo "$(date -u +%FT%TZ) stop command exited $?" >> /root/lucid-watchdog.log
    idle=0
  fi
  sleep 60
done
