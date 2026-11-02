"""Proves the server speaks HTTPS when given a certificate, and only then.

The point of TLS here is keeping the bearer token off the wire: three of the
five supported clients send it in the URL. So beyond "a client can connect",
this pins the failure modes that would quietly undo that: half a configuration
falling back to plaintext, a client accepting a certificate nobody vouched
for, and the Docker healthcheck marking a working server unhealthy.

The server certificate deliberately names 127.0.0.1 and not localhost, which
is what a real deployment looks like from inside its own container: the
healthcheck's hostname does not match the certificate.

Run with:
    uv run python -m tests.tls_transport
"""

import asyncio
import datetime
import http.client
import ipaddress
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from ._helpers import BIN, REPO_ROOT, reset_project
from ._hermes import load_plugin_module

bridge = load_plugin_module("bridge")

ADMIN_BIN = str(Path(BIN).with_name("blueocean-admin"))
TLS_PORT = 8794
PLAIN_PORT = 8793
BASE = f"https://127.0.0.1:{TLS_PORT}"
TOKEN = "tls-test-token"
PROJECT = "tls-transport-project"
SERVE = ["--transport", "streamable-http", "--host", "127.0.0.1"]


@dataclass
class Pki:
    ca: Path
    cert: Path
    key: Path
    other_key: Path
    encrypted_key: Path


def _pem_key(key: ec.EllipticCurvePrivateKey, password: bytes | None = None) -> bytes:
    encryption = (
        serialization.BestAvailableEncryption(password)
        if password
        else serialization.NoEncryption()
    )
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
    )


def make_pki(directory: Path) -> Pki:
    """A throwaway private CA and a server certificate for 127.0.0.1 only."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "blueocean test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    pki = Pki(
        ca=directory / "ca.pem",
        cert=directory / "cert.pem",
        key=directory / "key.pem",
        other_key=directory / "other-key.pem",
        encrypted_key=directory / "encrypted-key.pem",
    )
    pki.ca.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    pki.cert.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    pki.key.write_bytes(_pem_key(server_key))
    pki.other_key.write_bytes(_pem_key(ec.generate_private_key(ec.SECP256R1())))
    pki.encrypted_key.write_bytes(_pem_key(server_key, b"hunter2"))
    return pki


@contextmanager
def trusting(ca: Path | None) -> Iterator[None]:
    """Run the block as a client whose only addition to the default trust store
    is ``ca``: the documented way of trusting a private CA."""
    saved = os.environ.get("SSL_CERT_FILE")
    os.environ.pop("SSL_CERT_FILE", None)
    if ca is not None:
        os.environ["SSL_CERT_FILE"] = str(ca)
    try:
        yield
    finally:
        os.environ.pop("SSL_CERT_FILE", None)
        if saved is not None:
            os.environ["SSL_CERT_FILE"] = saved


def http_status(req: urllib.request.Request, context: ssl.SSLContext | None = None) -> int:
    try:
        with urllib.request.urlopen(req, timeout=5, context=context) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def test_bad_config_never_serves(pki: Pki) -> None:
    print("== a bad TLS configuration stops the server, it does not fall back to plaintext ==")
    missing = str(pki.cert.parent / "missing.pem")
    cert, key = str(pki.cert), str(pki.key)
    cases = [
        ("cert without key", [*SERVE, "--tls-certfile", cert], {}, "--tls-keyfile"),
        ("key without cert", [*SERVE, "--tls-keyfile", key], {}, "--tls-certfile"),
        ("cert from env alone", SERVE, {"BLUEOCEAN_TLS_CERTFILE": cert}, "--tls-keyfile"),
        ("missing file", [*SERVE, "--tls-certfile", missing, "--tls-keyfile", key], {}, missing),
        (
            "flag beats env",
            [*SERVE, "--tls-certfile", missing],
            {"BLUEOCEAN_TLS_CERTFILE": cert, "BLUEOCEAN_TLS_KEYFILE": key},
            missing,
        ),
        (
            "key belongs to another cert",
            [*SERVE, "--tls-certfile", cert, "--tls-keyfile", str(pki.other_key)],
            {},
            str(pki.other_key),
        ),
        (
            "encrypted key",
            [*SERVE, "--tls-certfile", cert, "--tls-keyfile", str(pki.encrypted_key)],
            {},
            "encrypted",
        ),
        (
            "stdio takes no certificate",
            ["--transport", "stdio", "--tls-certfile", cert, "--tls-keyfile", key],
            {},
            "stdio",
        ),
    ]
    for name, args, extra_env, expected in cases:
        # An invalid provider makes build_server() fail with a different message, so
        # each case also proves the check runs before the slow embedding model load.
        env = {**os.environ, "BLUEOCEAN_EMBEDDING": "no-such-provider", **extra_env}
        result = subprocess.run(
            [BIN, "--port", str(TLS_PORT), *args],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=False,
        )
        assert result.returncode != 0, f"{name}: server started anyway"
        assert expected in result.stderr, f"{name}: wanted {expected!r} in:\n{result.stderr}"
        assert "Traceback" not in result.stderr, f"{name}: traceback:\n{result.stderr}"
        print(f"  OK: {name}")


def start_server(args: list[str], env: dict[str, str], health: str,
                 context: ssl.SSLContext | None) -> subprocess.Popen:
    proc = subprocess.Popen(
        [BIN, *args], env={**os.environ, **env},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.time() + 120  # the embedding model loads before the port opens
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early:\n{proc.stderr.read()}")
        try:
            urllib.request.urlopen(health, timeout=1, context=context)
            return proc
        except OSError:
            time.sleep(0.3)
    stop_server(proc)
    raise TimeoutError(f"{health} did not answer")


def stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def test_plaintext_refused() -> None:
    print("== the TLS port does not answer plaintext ==")
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{TLS_PORT}/health", timeout=5)
    except (OSError, http.client.HTTPException) as e:
        print(f"  OK: {type(e).__name__}")
    else:
        raise AssertionError("a plaintext request was answered on the TLS port")


def test_token_still_required(ctx: ssl.SSLContext) -> None:
    print("== TLS does not replace the bearer token ==")
    mcp_url = f"{BASE}/mcp"
    assert http_status(urllib.request.Request(f"{BASE}/health"), ctx) == 200
    assert http_status(urllib.request.Request(mcp_url), ctx) == 401
    assert http_status(urllib.request.Request(f"{mcp_url}?token=wrong"), ctx) == 401
    assert http_status(urllib.request.Request(f"{mcp_url}?token={TOKEN}"), ctx) != 401
    print("  OK")


def test_browser_origins(ctx: ssl.SSLContext) -> None:
    print("== on loopback, https origins for localhost pass and foreign ones do not ==")

    def post(origin: str) -> int:
        req = urllib.request.Request(
            f"{BASE}/mcp", data=b"{}", method="POST",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Origin": origin,
            },
        )
        return http_status(req, ctx)

    local = post(f"https://localhost:{TLS_PORT}")
    assert local != 403, "a local https page was refused by DNS rebinding protection"
    assert post(f"https://evil.example:{TLS_PORT}") == 403, "DNS rebinding protection was lost"
    print("  OK")


async def _mcp_round_trip() -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}

    async def connect(stack: AsyncExitStack) -> ClientSession:
        client = await stack.enter_async_context(create_mcp_http_client(headers=headers))
        streams = await stack.enter_async_context(
            streamable_http_client(f"{BASE}/mcp", http_client=client)
        )
        session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
        await session.initialize()
        return session

    async with AsyncExitStack() as stack:
        writer = await connect(stack)
        stored = await writer.call_tool("memory_store", {
            "project": PROJECT, "area": "infra", "module": "tls",
            "content": "This entry was written over HTTPS.",
            "summary": "Written over HTTPS", "importance": 3,
        })
        point_id = stored.content[0].text
        reader = await connect(stack)
        found = await reader.call_tool("memory_search", {"project": PROJECT, "query": "HTTPS"})
        ids = {r["id"] for r in json.loads(found.content[0].text)["summary"]}
        assert point_id in ids, f"a second HTTPS session could not read {point_id}: {ids}"


def test_mcp_over_tls(pki: Pki) -> None:
    print("== MCP store and search over HTTPS, trusting the CA through SSL_CERT_FILE ==")
    with trusting(pki.ca):
        asyncio.run(_mcp_round_trip())
    print("  OK")


def run_admin_usage(ca: Path | None) -> subprocess.CompletedProcess:
    """The real CLI in its own process, as an operator runs it. Not in-process:
    urllib builds its TLS context on the first urlopen and keeps it, so setting
    SSL_CERT_FILE midway through this script would test nothing real."""
    env = {k: v for k, v in os.environ.items() if k != "SSL_CERT_FILE"}
    env |= {"BLUEOCEAN_SERVER_URL": BASE, "BLUEOCEAN_AUTH_TOKEN": TOKEN}
    if ca is not None:
        env["SSL_CERT_FILE"] = str(ca)
    return subprocess.run(
        [ADMIN_BIN, "usage"], env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=30, check=False,
    )


def test_clients_check_the_certificate(pki: Pki) -> None:
    print("== the repo's own clients trust SSL_CERT_FILE and refuse anything else ==")
    trusted = run_admin_usage(pki.ca)
    assert trusted.returncode == 0, f"blueocean-admin failed over HTTPS:\n{trusted.stderr}"

    untrusted = run_admin_usage(None)
    assert untrusted.returncode != 0, "blueocean-admin accepted an untrusted certificate"
    output = untrusted.stdout + untrusted.stderr
    assert "CERTIFICATE_VERIFY_FAILED" in output, f"the real cause was hidden:\n{output}"

    with trusting(pki.ca):
        hermes = bridge.Bridge(f"{BASE}/mcp", TOKEN)
        found = hermes.call("memory_search", {"project": PROJECT, "query": "HTTPS"})
        assert found["summary"], "Hermes bridge read nothing over HTTPS"

    with trusting(None):
        hermes = bridge.Bridge(f"{BASE}/mcp", TOKEN)
        try:
            hermes.call("memory_search", {"project": PROJECT, "query": "HTTPS"})
        except bridge.BridgeUnavailable as e:
            # "unreachable" would send someone debugging the network for a trust problem.
            assert "certificate" in str(e).lower(), f"Hermes hid the real cause: {e}"
        else:
            raise AssertionError("Hermes bridge accepted an untrusted certificate")
    print("  OK")


def run_compose_healthcheck(port: int, tls_env: str | None) -> int:
    """Run docker-compose.yml's own healthcheck, as it would run in the container
    (where SSL_CERT_FILE is unset), against ``port`` rather than 8765."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    test = compose["services"]["blueocean-mcp"]["healthcheck"]["test"]
    assert test[:3] == ["CMD", "python3", "-c"], test
    code, count = re.subn(r"localhost:8765", f"localhost:{port}", test[3])
    assert count == 1, f"expected the probe to name localhost:8765 once:\n{test[3]}"
    env = {"PATH": os.environ["PATH"]}
    if tls_env is not None:
        env["BLUEOCEAN_TLS_CERTFILE"] = tls_env
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        env=env, capture_output=True, text=True, timeout=10, check=False,
    )
    return result.returncode


def test_compose_healthcheck_on_tls() -> None:
    print("== docker-compose healthcheck passes on a TLS server whose cert names another host ==")
    rc = run_compose_healthcheck(TLS_PORT, "/data/tls/fullchain.pem")
    assert rc == 0, f"a healthy TLS server would be reported unhealthy (exit {rc})"
    print("  OK")


def test_plain_http_stays_default() -> None:
    print("== compose's empty TLS variables mean plain HTTP, with the healthcheck still passing ==")
    url = f"http://127.0.0.1:{PLAIN_PORT}/health"
    proc = start_server(
        [*SERVE, "--port", str(PLAIN_PORT)],
        {"BLUEOCEAN_TLS_CERTFILE": "", "BLUEOCEAN_TLS_KEYFILE": "", "BLUEOCEAN_AUTH_TOKEN": ""},
        url, None,
    )
    try:
        assert http_status(urllib.request.Request(url)) == 200
        rc = run_compose_healthcheck(PLAIN_PORT, "")
        assert rc == 0, f"a healthy plain server would be reported unhealthy (exit {rc})"
    finally:
        stop_server(proc)
    print("  OK")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        pki = make_pki(directory)
        test_bad_config_never_serves(pki)

        reset_project(PROJECT)
        ctx = ssl.create_default_context(cafile=str(pki.ca))
        proc = start_server(
            [*SERVE, "--port", str(TLS_PORT)],
            {
                "BLUEOCEAN_TLS_CERTFILE": str(pki.cert),
                "BLUEOCEAN_TLS_KEYFILE": str(pki.key),
                "BLUEOCEAN_AUTH_TOKEN": TOKEN,
                "BLUEOCEAN_TELEMETRY_DB": str(directory / "telemetry.db"),
                "BLUEOCEAN_PRICING_FILE": str(directory / "pricing.json"),
            },
            f"{BASE}/health", ctx,
        )
        try:
            test_plaintext_refused()
            test_token_still_required(ctx)
            test_browser_origins(ctx)
            test_mcp_over_tls(pki)
            test_clients_check_the_certificate(pki)
            test_compose_healthcheck_on_tls()
        finally:
            stop_server(proc)

        test_plain_http_stays_default()
    print("\nTLS TRANSPORT TEST PASSED")


if __name__ == "__main__":
    main()
