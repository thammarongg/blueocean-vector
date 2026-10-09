"""install_hermes_plugin.sh links the provider into a Hermes home, safely.

The link must point back at this repo so `git pull` updates the plugin, a
second run must be a no-op, and a real directory at the link path (a manual
install, or another plugin of the same name) must never be overwritten.

Run with:
    uv run python -m tests.install_hermes_plugin
"""

import subprocess
import tempfile
from pathlib import Path

from ._helpers import REPO_ROOT
from ._hermes import PLUGIN_DIR

SCRIPT = REPO_ROOT / "scripts" / "install_hermes_plugin.sh"


def run(hermes_home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), *args],
        env={"HOME": str(hermes_home.parent), "HERMES_HOME": str(hermes_home),
             "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        capture_output=True, text=True, check=False,
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d) / ".hermes"
        link = home / "plugins" / "blueocean"

        print("== no Hermes home: refuse ==")
        res = run(home)
        assert res.returncode == 1 and "not found" in res.stdout, res.stdout
        print("  OK")

        print("== install links back to the repo ==")
        home.mkdir()
        res = run(home)
        assert res.returncode == 0, res.stdout + res.stderr
        assert link.is_symlink() and link.resolve() == PLUGIN_DIR.resolve()
        assert "hermes memory setup" in res.stdout, "next steps not printed"
        print("  OK")

        print("== second run is a no-op ==")
        res = run(home)
        assert res.returncode == 0 and "already installed" in res.stdout, res.stdout
        print("  OK")

        print("== remove ==")
        res = run(home, "--remove")
        assert res.returncode == 0 and not link.exists() and not link.is_symlink()
        print("  OK")

        print("== --remove leaves a link to another checkout alone ==")
        other = Path(d) / "other-checkout" / "blueocean"
        other.mkdir(parents=True)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(other)
        res = run(home, "--remove")
        assert res.returncode == 1, res.stdout + res.stderr
        assert link.is_symlink() and link.resolve() == other.resolve(), "another checkout's link was removed"
        assert str(other) in res.stdout, res.stdout
        link.unlink()
        print("  OK")

        print("== a real directory at the link path is never overwritten ==")
        link.mkdir(parents=True)
        (link / "keep.txt").write_text("mine")
        res = run(home)
        assert res.returncode == 1, res.stdout
        assert (link / "keep.txt").read_text() == "mine"
        print("  OK")

        print("== bad usage ==")
        assert run(home, "--bogus").returncode == 2
        print("  OK")

    print("\nINSTALL HERMES PLUGIN TEST PASSED")


if __name__ == "__main__":
    main()
