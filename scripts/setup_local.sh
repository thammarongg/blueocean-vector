#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Starting Qdrant via docker compose..."
docker compose up -d

echo "==> Waiting for Qdrant to be ready..."
for i in $(seq 1 30); do
  if curl -fsS http://localhost:6333/healthz >/dev/null 2>&1; then
    echo "    Qdrant is up."
    break
  fi
  sleep 1
done

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Created .env from .env.example"
fi

echo "==> Verifying package install..."
uv sync --extra dev

echo "==> Done. Run the MCP server with:"
echo "    uv run blueocean-mcp"
echo "  Or inspect tools with:"
echo "    uv run blueocean-mcp --help"