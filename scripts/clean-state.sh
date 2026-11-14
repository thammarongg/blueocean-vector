#!/usr/bin/env bash
# Clock-out check: bash scripts/clean-state.sh   (read-only)
# Passes only when the session leaves nothing behind:
#   1. the working tree is clean (everything committed, or reverted/stashed),
#   2. no throwaway database container is still running. Test DBs are
#      started with `docker run --rm` and stopped before the session ends;
#      a DB container outside any compose project is treated as a leftover.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 1
fail=0

printf '== working tree\n'
dirty="$(git status --porcelain)"
if [ -n "$dirty" ]; then
  echo "FAIL: uncommitted changes (commit, or revert/stash and note it in PROGRESS.md):"
  echo "$dirty"
  fail=1
else
  echo "ok: clean ($(git branch --show-current) @ $(git rev-parse --short HEAD))"
fi

printf '\n== leftover test containers\n'
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "skip: docker not running"
else
  leftovers="$(docker ps --format '{{.Names}}|{{.Image}}|{{.Label "com.docker.compose.project"}}|{{.Ports}}' \
    | awk -F'|' '$2 ~ /^(postgres|mysql|mariadb|redis|valkey|mongo)/ && $3 == "" {print $1"  "$2"  "$4}')"
  if [ -n "$leftovers" ]; then
    echo "FAIL: throwaway DB still running (docker stop NAME):"
    echo "$leftovers"
    fail=1
  else
    echo "ok: none"
  fi
fi

if [ "$fail" -eq 0 ]; then
  printf '\nclean-state.sh: CLEAN\n'
else
  printf '\nclean-state.sh: NOT CLEAN\n'
fi
exit "$fail"
