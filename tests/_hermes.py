"""Helpers for the Hermes provider tests.

The plugin's package __init__ imports Hermes, which this repo's environment
does not have. Its sibling modules deliberately import neither Hermes nor each
other, so the tests load each one directly by path.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from types import ModuleType

from ._helpers import BIN, REPO_ROOT

PLUGIN_DIR = REPO_ROOT / "integrations" / "hermes" / "blueocean"


def load_plugin_module(name: str) -> ModuleType:
    """Import integrations/hermes/blueocean/<name>.py without its package."""
    module_name = f"blueocean_hermes_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


def start_server(port: int, token: str) -> subprocess.Popen:
    """A real BlueOcean server over streamable HTTP, requiring ``token``.

    The token goes in through the environment, not --auth-token, which `ps`
    would show to other local users.
    """
    log = tempfile.TemporaryFile(mode="w+")  # noqa: SIM115 - outlives this call; stop_server closes it
    env = {**os.environ, "BLUEOCEAN_AUTH_TOKEN": token}
    proc = subprocess.Popen(
        [BIN, "--transport", "streamable-http", "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=log, env=env, text=True,
    )
    proc.log = log  # type: ignore[attr-defined]
    health = f"http://127.0.0.1:{port}/health"
    deadline = time.time() + 90  # the embedding model loads before the port opens
    while time.time() < deadline:
        if proc.poll() is not None:
            log.seek(0)
            output = log.read()
            log.close()
            raise RuntimeError(f"server exited early:\n{output}")
        try:
            urllib.request.urlopen(health, timeout=1)
            return proc
        except urllib.error.HTTPError:
            return proc  # any HTTP answer means the port is serving
        except OSError:
            time.sleep(0.3)
    stop_server(proc)
    raise TimeoutError(f"server on port {port} did not start")


def stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    proc.log.close()  # type: ignore[attr-defined]
