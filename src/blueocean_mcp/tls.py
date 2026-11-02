"""HTTPS for the streamable-http transport.

Three of the five supported clients send the bearer token in the URL, so on a
network anything less than TLS hands the token to whoever can watch the wire.
That makes a broken configuration dangerous if it degrades quietly: everything
here fails at startup rather than serving plaintext, and does so before the
slow embedding model load so the mistake costs seconds.
"""

import ssl
from dataclasses import dataclass

from mcp.server.transport_security import TransportSecuritySettings

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class TLSConfigError(ValueError):
    """The certificate settings cannot be served. The message is for the operator."""


class _EncryptedKey(Exception):
    pass


def _refuse_password() -> str:
    # Without a callback OpenSSL prompts on the terminal for the passphrase,
    # which in a container or service manager is a silent hang.
    raise _EncryptedKey


@dataclass(frozen=True)
class TLSFiles:
    certfile: str
    keyfile: str


def load_tls_files(certfile: str | None, keyfile: str | None) -> TLSFiles | None:
    """The files to serve with, or None for plain HTTP (neither is set).

    Loads them once here, purely to report a problem as one clear line. uvicorn
    loads them again when it starts.
    """
    if not certfile and not keyfile:
        return None
    if not certfile:
        raise TLSConfigError("--tls-keyfile needs --tls-certfile (BLUEOCEAN_TLS_CERTFILE) as well")
    if not keyfile:
        raise TLSConfigError("--tls-certfile needs --tls-keyfile (BLUEOCEAN_TLS_KEYFILE) as well")
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(
            certfile, keyfile, password=_refuse_password
        )
    except _EncryptedKey:
        raise TLSConfigError(
            f"TLS key {keyfile} is encrypted, which is not supported: "
            "nothing could enter its passphrase at startup"
        ) from None
    except OSError as e:  # includes ssl.SSLError
        raise TLSConfigError(
            f"could not load TLS certificate {certfile} with key {keyfile}: {e}"
        ) from None
    return TLSFiles(certfile, keyfile)


def transport_security_for(host: str, tls: TLSFiles | None) -> TransportSecuritySettings | None:
    """What to give streamable_http_app; None keeps the library's own choice.

    On loopback the library turns on DNS rebinding protection and allows only
    http:// origins, so a local page reaching this server over https would get
    a 403. Same hosts and same protection, with the https spellings added.
    """
    if tls is None or host not in LOOPBACK_HOSTS:
        return None
    hosts = ["127.0.0.1", "localhost", "[::1]"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{h}:*" for h in hosts],
        allowed_origins=[f"{scheme}://{h}:*" for scheme in ("http", "https") for h in hosts],
    )
