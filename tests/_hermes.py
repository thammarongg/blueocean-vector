"""Helpers for the Hermes provider tests.

The plugin's package __init__ imports Hermes, which this repo's environment
does not have. Its sibling modules deliberately import neither Hermes nor each
other, so the tests load each one directly by path.
"""

import importlib.util
import sys
from types import ModuleType

from ._helpers import REPO_ROOT

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
