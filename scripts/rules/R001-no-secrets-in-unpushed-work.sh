#!/usr/bin/env bash
# R001 — no secrets in unpushed commits or uncommitted changes
# WHAT: gitleaks finds a secret in commits not yet on the remote default
#       branch, or in the working tree / staged diff.
# WHY:  security reviews keep running gitleaks by hand at the end; a pushed
#       secret has to be rotated, a caught one only removed.
# FIX:  remove the value, load it from the environment or a git-ignored 0600
#       file, rewrite the unpushed commit, and report it if the value was real
#       (it must be rotated).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

if ! command -v gitleaks >/dev/null 2>&1; then
  echo "R001: SKIP (gitleaks not installed: brew install gitleaks)"
  exit 0
fi

remote="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || echo origin/main)"
if git rev-parse --verify --quiet "$remote" >/dev/null; then
  base="$remote"
else
  base="$(git rev-list --max-parents=0 HEAD | tail -1)"
fi

# Quiet when clean. On a leak, re-run verbose so the finding is printed
# (value redacted): with a quiet log level gitleaks exits 1 and says nothing.
scan() {
  if ! gitleaks git --no-banner --redact --log-level error "$@" . >/dev/null 2>&1; then
    echo "R001: FAIL ($*)"
    gitleaks git --no-banner --redact --verbose "$@" .
    return 1
  fi
}
scan --log-opts="$base..HEAD"
scan --pre-commit
scan --pre-commit --staged
echo "R001: pass"
