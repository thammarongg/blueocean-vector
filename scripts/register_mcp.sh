#!/usr/bin/env bash
# Register the blueocean MCP server with Claude Code, Cursor, Codex,
# Gemini/Antigravity, and Kiro -- all pointed at ONE shared, persistently
# running server via its network URL (http://localhost:8765/mcp).
#
# This uses each tool's OWN native "add MCP server" CLI command wherever
# possible, instead of us writing bespoke logic to edit that tool's specific
# config file format/location. That means it also works, with zero extra
# code from us, for any other MCP-http-capable tool not listed here (the
# user just runs that tool's own "add remote server" command/UI with the
# same URL) -- including niche/non-mainstream tools like Kiro.
#
# If BLUEOCEAN_AUTH_TOKEN is set (in .env or the environment), the server
# requires it on every request. Tools with native header support (Claude,
# Gemini) get it via `Authorization: Bearer <token>`. Tools with no header
# option when registering a remote URL (Codex, Kiro, and the Cursor direct
# file edit) get it baked into the URL as `?token=<token>` instead -- our
# server accepts either.
#
# Requires the server to actually be running first:
#   docker compose up -d          (recommended: runs in the background)
# or:
#   uv run blueocean-mcp --transport streamable-http --port 8765 --auth-token ...
set -euo pipefail

cd "$(dirname "$0")/.."

BASE_URL="${BLUEOCEAN_URL:-http://localhost:8765/mcp}"

TOKEN="${BLUEOCEAN_AUTH_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -f .env ]; then
  TOKEN="$(grep -E '^BLUEOCEAN_AUTH_TOKEN=' .env | tail -1 | cut -d= -f2- || true)"
fi

if [ -n "$TOKEN" ]; then
  URL_WITH_TOKEN="${BASE_URL}?token=${TOKEN}"
  echo "Registering MCP server at: $BASE_URL (auth token found -- will be sent)"
else
  URL_WITH_TOKEN="$BASE_URL"
  echo "Registering MCP server at: $BASE_URL (no auth token -- server is unauthenticated)"
fi
echo "(override URL with BLUEOCEAN_URL=..., token with BLUEOCEAN_AUTH_TOKEN=...)"
echo ""

# ---------- Claude Code ----------
if command -v claude >/dev/null 2>&1; then
  claude mcp remove blueocean >/dev/null 2>&1 || true
  if [ -n "$TOKEN" ]; then
    ok=$(claude mcp add --transport http --scope user blueocean "$BASE_URL" --header "Authorization: Bearer $TOKEN" >/dev/null 2>&1 && echo 1 || echo 0)
  else
    ok=$(claude mcp add --transport http --scope user blueocean "$BASE_URL" >/dev/null 2>&1 && echo 1 || echo 0)
  fi
  if [ "$ok" = "1" ]; then
    echo "  + Claude Code: registered via 'claude mcp add' (user scope)"
  else
    echo "  ! Claude Code: 'claude mcp add' failed"
  fi
else
  echo "  ! 'claude' CLI not found, skipping Claude Code"
fi

# ---------- Codex ----------
if command -v codex >/dev/null 2>&1; then
  codex mcp remove blueocean >/dev/null 2>&1 || true
  if codex mcp add blueocean --url "$URL_WITH_TOKEN" >/dev/null 2>&1; then
    echo "  + Codex: registered via 'codex mcp add'"
  else
    echo "  ! Codex: 'codex mcp add' failed"
  fi
else
  echo "  ! 'codex' CLI not found, skipping Codex"
fi

# ---------- Gemini / Antigravity ----------
if command -v gemini >/dev/null 2>&1; then
  gemini mcp remove blueocean -s user >/dev/null 2>&1 || true
  if [ -n "$TOKEN" ]; then
    ok=$(gemini mcp add blueocean "$BASE_URL" --transport http -s user -H "Authorization: Bearer $TOKEN" >/dev/null 2>&1 && echo 1 || echo 0)
  else
    ok=$(gemini mcp add blueocean "$BASE_URL" --transport http -s user >/dev/null 2>&1 && echo 1 || echo 0)
  fi
  if [ "$ok" = "1" ]; then
    echo "  + Gemini/Antigravity: registered via 'gemini mcp add' (user scope)"
  else
    echo "  ! Gemini/Antigravity: 'gemini mcp add' failed"
  fi
else
  echo "  ! 'gemini' CLI not found, skipping Gemini/Antigravity"
fi

# ---------- Kiro ----------
# kiro-cli's `mcp add` has no --header/--bearer flag, so the token (if any)
# must be baked into the URL.
if command -v kiro-cli >/dev/null 2>&1; then
  if kiro-cli mcp add --name blueocean --url "$URL_WITH_TOKEN" --scope global --force >/dev/null 2>&1; then
    echo "  + Kiro: registered via 'kiro-cli mcp add' (global scope)"
  else
    echo "  ! Kiro: 'kiro-cli mcp add' failed"
  fi
else
  echo "  ! 'kiro-cli' CLI not found, skipping Kiro"
fi

# ---------- Cursor ----------
# Cursor's `--add-mcp` CLI flag only takes effect while the Cursor app is
# running (it appears to relay through the app rather than write the file
# directly), so it can't be reliably driven headlessly. We fall back to
# editing ~/.cursor/mcp.json directly -- this is the one tool where we still
# touch a config file, and only because its own CLI requires a running GUI.
CURSOR_MCP="$HOME/.cursor/mcp.json"
if [ -f "$CURSOR_MCP" ]; then
  python3 - "$CURSOR_MCP" "$URL_WITH_TOKEN" <<'PY'
import json, sys
path, url = sys.argv[1], sys.argv[2]
with open(path) as f:
    cfg = json.load(f)
servers = cfg.setdefault("mcpServers", {})
servers["blueocean"] = {"url": url}
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print("  + Cursor: ~/.cursor/mcp.json (direct edit -- CLI requires the app to be running)")
PY
else
  echo "  ! ~/.cursor/mcp.json not found, skipping Cursor"
fi

echo ""
echo "==> Done. For any other MCP-http-capable tool (mainstream or not),"
echo "    just use its own 'add remote MCP server' command/UI with:"
echo "        $URL_WITH_TOKEN"
echo "    (or set the Authorization: Bearer header if it supports one)"
echo "==> Restart your agents to pick up the new MCP server."