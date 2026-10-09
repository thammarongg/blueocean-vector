#!/usr/bin/env bash
# Link the BlueOcean memory provider into a Hermes home.
#
# Hermes discovers user memory providers in $HERMES_HOME/plugins/<name>/. The
# provider's code stays in this repo; installing is a symlink back to it, so
# `git pull` (or scripts/update.sh) updates the plugin in place. This script
# never edits Hermes's config.yaml: choosing the active memory provider is
# `hermes memory setup`'s job.
#
# Usage:
#   ./scripts/install_hermes_plugin.sh            install (idempotent)
#   ./scripts/install_hermes_plugin.sh --remove   uninstall
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/integrations/hermes/blueocean"
LINK="$HERMES_HOME/plugins/blueocean"

case "${1:-}" in
  --remove)
    if [ -L "$LINK" ]; then
      target="$(readlink "$LINK")"
      if [ "$target" != "$SOURCE" ]; then
        echo "!! $LINK points to $target, not this checkout ($SOURCE); not removing it"
        exit 1
      fi
      rm "$LINK"
      echo "  - removed $LINK"
    else
      echo "  = not installed (nothing to remove)"
    fi
    exit 0
    ;;
  "") ;;
  *)
    echo "Usage: $0 [--remove]"
    exit 2
    ;;
esac

if [ ! -d "$HERMES_HOME" ]; then
  echo "!! Hermes home not found: $HERMES_HOME (set HERMES_HOME)"
  exit 1
fi

mkdir -p "$HERMES_HOME/plugins"
if [ -L "$LINK" ]; then
  if [ "$(readlink "$LINK")" = "$SOURCE" ]; then
    echo "  = already installed ($LINK -> $SOURCE)"
  else
    ln -sfn "$SOURCE" "$LINK"
    echo "  ~ relinked $LINK -> $SOURCE"
  fi
elif [ -e "$LINK" ]; then
  echo "!! $LINK exists and isn't a symlink; remove it manually first"
  exit 1
else
  ln -s "$SOURCE" "$LINK"
  echo "  + installed $LINK -> $SOURCE"
fi

cat <<EOT

Next:
  1. ./scripts/update.sh             (rebuild the server first: it keeps Turn Traces out
                                      of every agent's default search)
  2. hermes memory setup blueocean   (BLUEOCEAN_AUTH_TOKEN must be in $HERMES_HOME/.env)
  3. restart the Hermes gateway      (memory providers are imported once per process)
Roll back: set memory.provider back to empty and restart; the blueocean MCP
server entry keeps working on its own.
EOT
