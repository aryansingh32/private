#!/usr/bin/env bash
# Keeps the monitor's state + event log on a dedicated branch (default
# "monitor-state") as a SINGLE rewritten commit, so 288 polls a day do not
# bloat the repository or spam the main branch history.
#
#   state_sync.sh restore   -> clone the state branch into .state/ (or init it)
#   state_sync.sh save      -> amend + force-push whatever changed
set -euo pipefail

BRANCH="${STATE_BRANCH:-monitor-state}"
DIR="${STATE_DIR:-.state}"
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}"
REMOTE="https://x-access-token:${GH_TOKEN:?GH_TOKEN is not set}@github.com/${REPO}.git"

case "${1:-}" in
  restore)
    rm -rf "$DIR"
    if git ls-remote --exit-code --heads "$REMOTE" "$BRANCH" >/dev/null 2>&1; then
      git clone --quiet --depth 1 --branch "$BRANCH" "$REMOTE" "$DIR"
      echo "restored state from branch '$BRANCH'"
    else
      mkdir -p "$DIR"
      git -C "$DIR" init --quiet --initial-branch "$BRANCH"
      git -C "$DIR" remote add origin "$REMOTE"
      echo "branch '$BRANCH' does not exist yet - starting fresh"
    fi
    git -C "$DIR" config user.name  "tailscale-monitor[bot]"
    git -C "$DIR" config user.email "41898282+github-actions[bot]@users.noreply.github.com"
    ;;

  save)
    [ -d "$DIR" ] || { echo "nothing to save"; exit 0; }
    cd "$DIR"
    git add -A
    if git diff --cached --quiet; then
      echo "state unchanged"
      exit 0
    fi
    MSG="state @ $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    if git rev-parse --verify HEAD >/dev/null 2>&1; then
      git commit --quiet --amend -m "$MSG"     # keep exactly one commit
    else
      git commit --quiet -m "$MSG"
    fi
    git push --quiet --force origin "HEAD:refs/heads/$BRANCH"
    echo "state pushed to '$BRANCH'"
    ;;

  *)
    echo "usage: $0 {restore|save}" >&2
    exit 64
    ;;
esac
