#!/usr/bin/env bash
# Update the current checkout, runtime, and shared agent skill.
set -euo pipefail

update_skill() {
  local source="$PWD/skills/blueocean-memory/SKILL.md"
  local target="$HOME/.agents/skills/blueocean-memory/SKILL.md"
  local backup staged

  if [ ! -f "$source" ]; then
    echo "!! Repository skill is missing: $source" >&2
    return 1
  fi
  if [ -f "$target" ] && cmp -s "$source" "$target"; then
    echo "==> Skill unchanged; skipped."
    return
  fi

  mkdir -p "$(dirname "$target")"
  if [ -e "$target" ] || [ -L "$target" ]; then
    backup="$(mktemp "${target}.bak.XXXXXX")"
    cp -p "$target" "$backup"
    echo "==> Previous skill backed up to $backup"
  fi
  staged="$(mktemp "${target}.tmp.XXXXXX")"
  cp "$source" "$staged"
  chmod 644 "$staged"
  mv -f "$staged" "$target"
  echo "==> Skill updated: $target"
}

main() {
  local mode="docker"
  case "${1:-}" in
    "") ;;
    --stdio) mode="stdio" ;;
    --help|-h)
      echo "Usage: $0 [--stdio]"
      echo "Pull code, update the runtime, and sync the shared skill (with backup)."
      return ;;
    *) echo "Unknown option: $1 (use --help)" >&2; return 1 ;;
  esac
  if [ "$#" -gt 1 ]; then
    echo "Too many arguments (use --help)." >&2
    return 1
  fi

  cd "$(dirname "${BASH_SOURCE[0]}")/.."
  command -v git >/dev/null
  command -v uv >/dev/null
  if [ "$mode" = docker ]; then
    docker compose version >/dev/null
  fi
  if [ -n "$(git status --porcelain)" ]; then
    echo "!! Checkout has local changes. Commit or stash them, then retry." >&2
    return 1
  fi

  echo "==> Pulling code..."
  git pull --ff-only
  echo "==> Updating local Python dependencies..."
  uv sync --extra dev
  if [ "$mode" = docker ]; then
    echo "==> Rebuilding MCP server and waiting for healthy services..."
    docker compose up -d --build --wait --wait-timeout 180 blueocean-mcp
  fi
  update_skill
  if [ "$mode" = stdio ]; then
    echo "==> Update complete. Restart your client's MCP process to load the new code."
  else
    echo "==> Update complete. Reconnect MCP clients if needed."
  fi
  echo "==> Start a new agent session to load the skill."
}

# Parse the whole workflow before git pull can replace this script on disk.
main "$@"
