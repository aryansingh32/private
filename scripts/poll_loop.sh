#!/usr/bin/env bash
# Polls monitor.py on a fixed interval for LOOP_MINUTES, persisting state after
# every poll, then exits so the queued watchdog run takes over.
#
# Sleeping is what buys reliable 5-minute cadence: GitHub's scheduler cannot be
# trusted to fire '*/5', but a job that is already running can time itself.
set -uo pipefail

INTERVAL="${POLL_INTERVAL:-300}"
LOOP_MINUTES="${LOOP_MINUTES:-50}"
SINGLE="${SINGLE_RUN:-false}"
MAX_CONSECUTIVE_FAILURES=3

deadline=$(( $(date +%s) + LOOP_MINUTES * 60 ))
poll=0
failures=0

while :; do
  poll=$(( poll + 1 ))
  echo "::group::poll #${poll} ($(date -u +'%H:%M:%SZ'))"

  if python3 monitor.py; then
    failures=0
  else
    failures=$(( failures + 1 ))
    echo "poll failed (${failures}/${MAX_CONSECUTIVE_FAILURES} consecutive)"
  fi

  # Persist even after a failure: earlier polls in this job may have advanced state.
  ./scripts/state_sync.sh save || echo "state push failed, will retry next poll"
  echo "::endgroup::"

  if [ "$failures" -ge "$MAX_CONSECUTIVE_FAILURES" ]; then
    echo "giving up after ${failures} consecutive failures - letting the watchdog restart us"
    exit 1
  fi

  # FORCE_DIGEST/first-run flags must not repeat on every poll of this job.
  unset FORCE_DIGEST
  export FORCE_DIGEST=false

  if [ "$SINGLE" = "true" ]; then
    echo "single_run requested - done after one poll"
    break
  fi

  next=$(( $(date +%s) + INTERVAL ))
  if [ "$next" -ge "$deadline" ]; then
    echo "loop window finished after ${poll} poll(s) - exiting so the next run takes over"
    break
  fi

  sleep_for=$(( next - $(date +%s) ))
  echo "sleeping ${sleep_for}s until the next poll"
  sleep "$sleep_for"
done
