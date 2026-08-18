import argparse
import os

from .config import DEFAULT_QDRANT_URL
from .server import build_server


def _serve_http(mcp, host: str, port: int, token: str | None, qdrant_url: str) -> None:
    """Run the streamable-http transport ourselves (instead of mcp.run()) so
    we can wrap the ASGI app with bearer-token auth and add an unauthenticated
    /health route. This mirrors what MCPServer.run_streamable_http_async()
    does internally, since that method builds and runs everything in one call
    with no hook for wrapping/extending the app.

    /health is added directly to the SAME app instance mcp.streamable_http_app()
    returns (not nested inside an outer Starlette Mount) -- nesting it inside
    another Mount was found to break the transport's session-id/path handling.
    """
    import anyio
    import uvicorn
    from starlette.routing import Route

    from .auth import wrap_with_auth
    from .health import build_health_route, resolve_embedding_model

    async def _run() -> None:
        embedding_provider = os.getenv("BLUEOCEAN_EMBEDDING", "fastembed")
        embedding_model = resolve_embedding_model(embedding_provider)
        mcp_app = mcp.streamable_http_app(host=host)
        mcp_app.router.routes.insert(
            0,
            Route(
                "/health",
                build_health_route(qdrant_url, embedding_provider, embedding_model),
            ),
        )
        app = wrap_with_auth(mcp_app, token, exempt_paths=frozenset({"/health"}))
        # access_log=False: uvicorn's default access log prints the full
        # request line, including query string. Codex/Kiro/Cursor can't set
        # a custom Authorization header when registering a remote MCP server
        # by URL (see auth.py), so they send the bearer token as ?token=...
        # on every request -- a plain access log would put that token in
        # stdout/container logs on every single tool call.
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=mcp.settings.log_level.lower(),
            access_log=False,
        )
        server = uvicorn.Server(config)
        await server.serve()

    anyio.run(_run)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="blueocean-mcp",
        description="Unified agent memory MCP server backed by Qdrant.",
    )
    parser.add_argument(
        "--qdrant-url",
        default=None,
        help="Qdrant URL (default: BLUEOCEAN_QDRANT_URL or http://localhost:6333)",
    )
    parser.add_argument(
        "--embedding",
        default=None,
        help="Embedding provider: fastembed (default), openai, bedrock",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help=(
            "Transport to serve over. 'stdio' (default) is spawned per-agent by "
            "an MCP client. 'streamable-http' runs one persistent server that "
            "any MCP-http-capable client can register by URL, with no per-tool "
            "config-file editing required."
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind when --transport streamable-http (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port to bind when --transport streamable-http (default: 8765)",
    )
    parser.add_argument(
        "--auth-token",
        default=None,
        help=(
            "Require this bearer token on every streamable-http request "
            "(default: BLUEOCEAN_AUTH_TOKEN env var, or no auth if unset). "
            "Ignored for stdio. Prefer the env var over this flag: a value "
            "passed here is visible to any other local user via `ps`."
        ),
    )
    args = parser.parse_args()

    # CLI flags take precedence over any pre-existing environment variables.
    if args.qdrant_url:
        os.environ["BLUEOCEAN_QDRANT_URL"] = args.qdrant_url
    if args.embedding:
        os.environ["BLUEOCEAN_EMBEDDING"] = args.embedding

    mcp = build_server()
    if args.transport == "streamable-http":
        token = args.auth_token or os.getenv("BLUEOCEAN_AUTH_TOKEN")
        qdrant_url = args.qdrant_url or os.getenv("BLUEOCEAN_QDRANT_URL", DEFAULT_QDRANT_URL)
        _serve_http(mcp, args.host, args.port, token, qdrant_url)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()