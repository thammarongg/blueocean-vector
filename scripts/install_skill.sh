#!/usr/bin/env bash
# Install the "blueocean-memory" skill into whichever coding tool(s) you pick.
#
# Follows the same pattern as this machine's other portable skills
# (~/.agents/skills/<name>): the skill content lives in ONE canonical place,
# and "installing" it into a tool is just a symlink from that tool's own
# skills/ directory back to the canonical source. All 5 tools below already
# use the identical SKILL.md (YAML frontmatter + markdown) convention, just
# under different base directories -- so one skill, install anywhere.
#
# Usage:
#   ./scripts/install_skill.sh                 interactive picker
#   ./scripts/install_skill.sh claude codex     install into specific tools
#   ./scripts/install_skill.sh all              install into every tool found
#   ./scripts/install_skill.sh --list           show what's installed where
#   ./scripts/install_skill.sh --remove claude  uninstall from a tool
set -euo pipefail

SKILL_NAME="blueocean-memory"
CANONICAL_DIR="$HOME/.agents/skills/$SKILL_NAME"
CANONICAL_RELATIVE="../../.agents/skills/$SKILL_NAME"

ALL_TOOLS=(claude cursor codex gemini kiro)

# Plain function instead of an associative array: macOS ships bash 3.2 by
# default, which doesn't support `declare -A`.
tool_dir() {
  case "$1" in
    claude) echo "$HOME/.claude/skills" ;;
    cursor) echo "$HOME/.cursor/skills" ;;
    codex)  echo "$HOME/.codex/skills" ;;
    gemini) echo "$HOME/.gemini/config/skills" ;;
    kiro)   echo "$HOME/.kiro/skills" ;;
    *)      echo "" ;;
  esac
}

ensure_canonical() {
  if [ ! -f "$CANONICAL_DIR/SKILL.md" ]; then
    echo "!! Canonical skill not found at $CANONICAL_DIR/SKILL.md"
    echo "   (this script only links to it, it doesn't create the content)"
    exit 1
  fi
}

status_for() {
  local tool="$1" dir dir link
  dir="$(tool_dir "$tool")"
  link="$dir/$SKILL_NAME"
  if [ ! -d "$dir" ]; then
    echo "not found (tool not installed on this machine)"
  elif [ -L "$link" ]; then
    echo "installed -> $(readlink "$link")"
  elif [ -e "$link" ]; then
    echo "installed (not a symlink -- was placed manually)"
  else
    echo "not installed"
  fi
}

cmd_list() {
  echo "Skill: $SKILL_NAME (canonical: $CANONICAL_DIR)"
  for tool in "${ALL_TOOLS[@]}"; do
    printf "  %-8s %s\n" "$tool" "$(status_for "$tool")"
  done
}

install_for() {
  local tool="$1" dir link
  dir="$(tool_dir "$tool")"
  if [ -z "$dir" ]; then
    echo "  ! unknown tool: $tool (expected one of: ${ALL_TOOLS[*]})"
    return
  fi
  if [ ! -d "$(dirname "$dir")" ]; then
    echo "  ! $tool: not found on this machine, skipping"
    return
  fi
  mkdir -p "$dir"
  local link="$dir/$SKILL_NAME"
  if [ -L "$link" ]; then
    echo "  = $tool: already installed"
    return
  fi
  if [ -e "$link" ]; then
    echo "  ! $tool: $link exists and isn't a symlink, skipping (remove it manually first)"
    return
  fi
  ln -s "$CANONICAL_RELATIVE" "$link"
  echo "  + $tool: installed ($link -> $CANONICAL_RELATIVE)"
}

remove_for() {
  local tool="$1" link
  link="$(tool_dir "$tool")/$SKILL_NAME"
  if [ -L "$link" ]; then
    rm "$link"
    echo "  - $tool: removed"
  else
    echo "  = $tool: not installed (nothing to remove)"
  fi
}

ensure_canonical

case "${1:-}" in
  --list)
    cmd_list
    exit 0
    ;;
  --remove)
    shift
    for tool in "$@"; do remove_for "$tool"; done
    exit 0
    ;;
  all)
    for tool in "${ALL_TOOLS[@]}"; do install_for "$tool"; done
    exit 0
    ;;
  "")
    echo "Install '$SKILL_NAME' skill into which tool(s)?"
    echo "Available: ${ALL_TOOLS[*]} (or 'all')"
    read -r -p "> " -a picked
    for tool in "${picked[@]}"; do
      [ "$tool" = "all" ] && { for t in "${ALL_TOOLS[@]}"; do install_for "$t"; done; continue; }
      install_for "$tool"
    done
    ;;
  *)
    for tool in "$@"; do install_for "$tool"; done
    ;;
esac