#!/usr/bin/env bash
# The repo's verification gate for agents and humans: bash scripts/check.sh
# Exit 0 = GREEN. Anything else = red base: fix it (or stop and report)
# before starting new work. Keep it equal to what CI runs, minus reinstalls.
#
# Fill in the STAGES section for this repo (see the examples). The script
# refuses to say GREEN when no stage ran, so an empty template can't pass.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
STAGES_RUN=0

step() { printf '\n== %s\n' "$1"; STAGES_RUN=$((STAGES_RUN + 1)); }

# Base for "what did this branch touch": merge-base with the remote default
# branch, falling back to HEAD.
default_remote_branch() {
  git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || echo origin/main
}
BASE="$(git merge-base HEAD "$(default_remote_branch)" 2>/dev/null || git rev-parse HEAD)"

# touched DIR REGEX -> prints files under DIR (relative to DIR) changed vs BASE,
# in the working tree, or untracked, that match REGEX and still exist.
# Use it for debt mode: gate lint on touched files while legacy debt remains.
touched() {
  local dir="$1" re="$2"
  ( cd "$ROOT/$dir" && {
      git diff --name-only --relative "$BASE"
      git diff --name-only --relative
      git ls-files --others --exclude-standard
    } | grep -E "$re" | sort -u | while IFS= read -r f; do [ -f "$f" ] && echo "$f"; done ) || true
}

# ---------------------------------------------------------------- STAGES ----
# Uncomment / adapt. One `step` per CI job step. Scope dummy env vars to the
# command (env VAR=... cmd); never export them.
#
# Node (npm workspaces / single package):
#   step "node: install check"
#   [ -d node_modules ] || npm ci --ignore-scripts --no-audit --no-fund
#   step "node: typecheck";  npm run typecheck --if-present
#   step "node: lint";       npm run lint --if-present
#   step "node: unit tests"; npm test --if-present -- --ci
#
# Lint in debt mode (legacy errors not yours; switch back once clean):
#   step "app: lint (touched files)"
#   files=(); while IFS= read -r f; do files+=("$f"); done < <(touched apps/app '^(src|test)/.*\.tsx?$')
#   if [ "${#files[@]}" -gt 0 ]; then (cd apps/app && npx eslint "${files[@]}"); else echo "no touched files"; fi
#
# Python (uv):
#   step "py: tests"; (cd apps/worker && uv sync --locked --quiet && uv run pytest -q)
#   step "py: lint";  (cd apps/worker && uv run ruff check .)
#
# Go:     step "go: vet+test"; go vet ./... && go test ./...
# Rust:   step "rust: test";   cargo clippy --all-targets -- -D warnings && cargo test
# Terraform (no plan/apply, no live creds):
#   step "tf: fmt+validate"; terraform fmt -check -recursive && terraform validate
# ---------------------------------------------------------------------------

# No CI in this repo: these stages ARE the definition of green.

step "py: venv in sync with uv.lock"
uv sync --locked --quiet || { echo "venv drifted from uv.lock: run 'uv sync'" >&2; exit 1; }

step "py: lint (ruff, whole repo)"
uv run ruff check .

# Tests are standalone integration scripts (not pytest), run against the
# already-running local Qdrant (docker compose up -d qdrant). Each one resets
# its own project collection and binds its own port (879x), so they don't
# touch the live server on 8765 or real projects' memory.
step "py: Qdrant reachable"
curl -fsS --max-time 5 "${BLUEOCEAN_QDRANT_URL:-http://localhost:6333}/readyz" >/dev/null \
  || { echo "Qdrant not reachable: run 'docker compose up -d qdrant'" >&2; exit 1; }

step "py: test scripts (tests/*.py)"
failed=0; ran=0
for t in tests/*.py; do
  name="$(basename "$t" .py)"
  case "$name" in _*|__init__) continue ;; esac
  ran=$((ran + 1))
  if out="$(uv run python -m "tests.$name" 2>&1)"; then
    printf 'ok    %s\n' "$name"
  else
    printf 'FAIL  %s\n' "$name"; printf '%s\n' "$out" | tail -20 | sed 's/^/      /'
    failed=$((failed + 1))
  fi
done
echo "$ran test scripts, $failed failed"
[ "$failed" -eq 0 ]

step "promoted rules (scripts/rules/)"
for rule in scripts/rules/R*.sh; do
  [ -e "$rule" ] || continue
  printf -- '-- %s\n' "$(basename "$rule")"
  bash "$rule"
done

if [ "$STAGES_RUN" -le 1 ]; then
  echo "check.sh: NOT CONFIGURED (only the rules stage ran; add this repo's CI stages)" >&2
  exit 4
fi
printf '\ncheck.sh: GREEN (%s @ %s)\n' "$(git branch --show-current)" "$(git rev-parse --short HEAD)"
