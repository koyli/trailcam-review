#!/usr/bin/env bash
# One scheduled run: purge old to-delete files, review new footage with Claude Code
# (billed to your Claude plan, not the API), file the results, send Pushover messages,
# and tell Healthchecks.io whether the run worked.
set -uo pipefail
cd "$(dirname "$0")"

# An API key in the environment would make Claude Code bill per token instead of using the plan.
unset ANTHROPIC_API_KEY

# Never run two at once.
exec 9>run.lock
flock -n 9 || { echo "$(date) already running"; exit 0; }

mkdir -p logs
LOG="logs/run-$(date +%F).log"
exec >> "$LOG" 2>&1
echo "=== $(date) ==="

HC=$(python3 -c "import json; print(json.load(open('config.json')).get('healthchecks', {}).get('ping_url', ''))")
ping_hc() {  # ping_hc [suffix] [message]
  [ -n "$HC" ] || return 0
  curl -fsS -m 10 --retry 3 --data-raw "${2:-}" "$HC${1:-}" > /dev/null || echo "Healthchecks ping failed"
}
failed=""
ping_hc /start

purged=$(python3 trailcam.py purge) || { failed="purge failed"; purged=0; }
max=$(python3 -c "import json; print(json.load(open('config.json')).get('max_batches_per_run', 2))")

for _ in $(seq 1 "$max"); do
  batch=$(python3 trailcam.py next) || { failed="scan failed"; break; }
  [ -z "$batch" ] && break
  echo "Reviewing $batch"
  claude -p "$(python3 trailcam.py prompt "$batch")" \
    --allowedTools "Read,Write,Edit,Bash(python3 trailcam.py:*),Bash(cp:*),Bash(ls:*)" \
    || echo "claude exited with an error"
  # Leaves the batch pending if Claude didn't finish (e.g. usage limit); the next run carries on.
  python3 trailcam.py done "$batch" || { failed="review not finished: $(basename "$batch")"; break; }
done

python3 trailcam.py notify --purged "$purged" || failed="${failed:+$failed; }Pushover notification failed"

if [ -n "$failed" ]; then
  echo "FAILED: $failed"
  ping_hc /fail "$failed
$(tail -n 40 "$LOG")"
else
  ping_hc "" "ok"
fi
