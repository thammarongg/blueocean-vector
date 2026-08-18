"""Tests for GET /health: Qdrant reachability, embedding-provider
diagnostics, and the TTL cache that keeps cloud-provider self-tests off
docker-compose.yml's 10s healthcheck hot path (an OpenAI/Bedrock self-test
call on every single probe would risk rate limits for no benefit).

Run with:
    uv run python -m tests.health
"""

import json
import subprocess
import time
import urllib.error
import urllib.request

from blueocean_mcp.health import (
    _run_provider_check,
    _SelfTestCache,
    resolve_embedding_model,
)

from ._helpers import BIN

PORT = 8797


def wait_for_server(proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{PORT}/health"
    while time.time() < deadline:
        if proc.poll() is not None:
            _, err = proc.communicate()
            raise RuntimeError(f"Server process exited early:\n{err}")
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return  # any HTTP response proves the port is bound & serving
        except OSError:
            time.sleep(0.3)
    raise TimeoutError("Server did not start listening in time")


def test_selftest_cache_ttl() -> None:
    """The cache must call the check function at most once per TTL window,
    however many times .check() is called -- this is what protects a chatty
    probe from turning into a cloud-provider API call on every hit."""
    print("== self-test cache respects TTL ==")
    calls: list[tuple[str, str]] = []
    clock = [0.0]

    def fake_check(provider: str, model: str) -> tuple[bool, str | None]:
        calls.append((provider, model))
        return True, None

    cache = _SelfTestCache(ttl=10.0, now_fn=lambda: clock[0])

    cache.check("openai", "text-embedding-3-small", check_fn=fake_check)
    cache.check("openai", "text-embedding-3-small", check_fn=fake_check)
    cache.check("openai", "text-embedding-3-small", check_fn=fake_check)
    assert len(calls) == 1, f"expected exactly 1 call within the TTL window, got {len(calls)}"

    clock[0] += 10.1  # advance past the TTL
    cache.check("openai", "text-embedding-3-small", check_fn=fake_check)
    assert len(calls) == 2, f"expected a fresh call after TTL expiry, got {len(calls)}"
    print("  OK: 1 call within TTL, 1 more after expiry")


def test_selftest_cache_is_per_provider() -> None:
    print("== self-test cache tracks providers independently ==")
    calls: list[str] = []
    clock = [0.0]

    def fake_check(provider: str, model: str) -> tuple[bool, str | None]:
        calls.append(provider)
        return True, None

    cache = _SelfTestCache(ttl=10.0, now_fn=lambda: clock[0])
    cache.check("openai", "m", check_fn=fake_check)
    cache.check("bedrock", "m", check_fn=fake_check)
    assert calls == ["openai", "bedrock"], calls
    print("  OK: openai and bedrock cached independently")


def test_selftest_cache_reports_failure() -> None:
    print("== self-test cache propagates failure + error message ==")
    clock = [0.0]

    def failing_check(provider: str, model: str) -> tuple[bool, str | None]:
        return False, "invalid api key"

    cache = _SelfTestCache(ttl=10.0, now_fn=lambda: clock[0])
    ok, error = cache.check("openai", "m", check_fn=failing_check)
    assert ok is False
    assert error == "invalid api key"
    print("  OK: failure and error message surfaced")


def test_run_provider_check_skips_fastembed_and_unknown() -> None:
    """fastembed is proven working by the time /health can be hit at all --
    the model loads synchronously at server startup, so a broken load
    crashes the process before it could ever serve a request. No network
    call should be attempted for it, nor for a provider we don't recognize."""
    print("== _run_provider_check is a no-op for fastembed/unknown providers ==")
    assert _run_provider_check("fastembed", "intfloat/multilingual-e5-large") == (True, None)
    assert _run_provider_check("some-future-provider", "whatever") == (True, None)
    print("  OK: no network attempted for non-cloud providers")


def test_resolve_embedding_model() -> None:
    print("== resolve_embedding_model mirrors create_embedder()'s own defaults ==")
    assert resolve_embedding_model("fastembed") == "intfloat/multilingual-e5-large"
    assert resolve_embedding_model("openai") == "text-embedding-3-small"
    assert resolve_embedding_model("bedrock") == "amazon.titan-embed-text-v2:0"
    print("  OK")


def test_health_endpoint_reports_embedding_diagnostics() -> None:
    """End-to-end against the live server (default fastembed provider):
    /health must expose provider/model without adding a network self-test
    for a provider that's already proven working by startup succeeding."""
    print("== GET /health includes embedding diagnostics (live server) ==")
    proc = subprocess.Popen(
        [BIN, "--transport", "streamable-http", "--host", "127.0.0.1", "--port", str(PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for_server(proc)
        resp = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=5)
        assert resp.status == 200
        body = json.loads(resp.read())
        print(f"  {body}")
        assert body["status"] == "ok"
        assert body["qdrant"] == "reachable"
        assert body["embedding"] == {
            "provider": "fastembed",
            "model": "intfloat/multilingual-e5-large",
            "ok": True,
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("  OK")


def main() -> None:
    test_selftest_cache_ttl()
    test_selftest_cache_is_per_provider()
    test_selftest_cache_reports_failure()
    test_run_provider_check_skips_fastembed_and_unknown()
    test_resolve_embedding_model()
    test_health_endpoint_reports_embedding_diagnostics()
    print("\nHEALTH TEST PASSED")


if __name__ == "__main__":
    main()
