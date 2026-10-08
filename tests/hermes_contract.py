"""The provider, loaded the way Hermes loads it, against a real server.

Runs a child process on the Python of Hermes's installed dependency
environment, with a throwaway HERMES_HOME whose plugins/blueocean links to
this repo. Hermes discovers the provider
through plugins.memory.load_memory_provider, exactly as `memory.provider:
blueocean` would. The child then drives one session: an explicit project
switch, a recall, the same recall again (nothing new to inject), and a turn
whose text carries a memory-context fence and a password. A second child
blocks the `mcp` package and checks the provider still loads, reporting
itself unavailable rather than vanishing from discovery.

The child never imports hermes_bootstrap and never goes through the `hermes`
launcher: with a throwaway HERMES_HOME, bootstrap treats the install as
out of date and runs Hermes's self-update, which rewrites the real install's
launchers. HERMES_DISABLE_LAZY_INSTALLS=1 closes the remaining install paths,
and a before/after snapshot of the Hermes install fails the test if anything
in it changed anyway.

Skipped only when Hermes is absent (no HERMES_AGENT_DIR). An install with no
interpreter to run fails instead, so a broken install never reads as a pass.

Run with:
    uv run python -m tests.hermes_contract
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from ._helpers import reset_project
from ._hermes import PLUGIN_DIR, load_plugin_module, start_server, stop_server

PORT = 8795
URL = f"http://127.0.0.1:{PORT}/mcp"
TOKEN = "contract-test-token"
PROJECT = "hermes-contract-project"
HERMES_AGENT_DIR = Path(os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes" / "hermes-agent"))
HERMES_INSTALLS = Path.home() / ".hermes" / "installs"
SNAPSHOT_SKIP = {"__pycache__", ".git", "node_modules"}

CHILD = r'''
import json, os, sys
sys.path.insert(0, os.environ["HERMES_AGENT_DIR"])
from agent.memory_provider import MemoryProvider
from plugins.memory import load_memory_provider

project = os.environ["CONTRACT_PROJECT"]
p = load_memory_provider("blueocean")
assert isinstance(p, MemoryProvider), f"discovery returned {p!r}"
assert p.name == "blueocean"
assert p.is_available(), p.unavailable_reason()
assert [s["name"] for s in p.get_tool_schemas()] == ["blueocean_set_project"]

p.initialize("contract-session", hermes_home=os.environ["HERMES_HOME"],
             platform="cli", agent_context="primary")
trivial = p.prefetch("ok", session_id="contract-session")
set_out = json.loads(p.handle_tool_call("blueocean_set_project", {"project": project}))
set_upper = json.loads(p.handle_tool_call("blueocean_set_project", {"project": project.upper()}))
first = p.prefetch("which cache did we choose", session_id="contract-session")
status = p.recall_status()
second = p.prefetch("which cache did we choose", session_id="contract-session")
p.sync_turn("<memory-context>recalled stuff</memory-context> which cache? password=hunter2",
            "We chose Redis.", session_id="contract-session")
p.shutdown()
print("RESULT " + json.dumps({
    "trivial": trivial, "set": set_out, "set_upper": set_upper, "first": first,
    "count": status.count if status else 0,
    "label": status.provider_label if status else "",
    "second": second,
}))
'''

CHILD_NO_MCP = r'''
import json, os, sys
sys.modules["mcp"] = None  # any `import mcp...` now raises ImportError
sys.path.insert(0, os.environ["HERMES_AGENT_DIR"])
from agent.memory_provider import MemoryProvider
from plugins.memory import load_memory_provider

p = load_memory_provider("blueocean")
assert isinstance(p, MemoryProvider), f"discovery returned {p!r}"
print("RESULT " + json.dumps({"available": p.is_available(), "reason": p.unavailable_reason()}))
'''


def child_result(child: subprocess.CompletedProcess) -> dict:
    line = next((ln for ln in child.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert child.returncode == 0 and line, f"child failed:\n{child.stdout}\n{child.stderr}"
    return json.loads(line[len("RESULT "):])


def hermes_python() -> list[str]:
    """Hermes's dependency-environment Python, run isolated (-I)."""
    override = os.environ.get("HERMES_PYTHON")
    if override:
        return [override, "-I"]
    # Not resolved: the venv's python is a symlink, and resolving it would
    # lose the venv (and with it Hermes's dependencies).
    found = sorted(HERMES_INSTALLS.glob("*/environments/*/venv/bin/python"),
                   key=lambda path: path.lstat().st_mtime, reverse=True)
    assert found, (f"Hermes is installed at {HERMES_AGENT_DIR} but no interpreter was found "
                   f"under {HERMES_INSTALLS}/*/environments/*/venv/bin/python (set HERMES_PYTHON)")
    if len(found) > 1:
        print(f"using the most recently modified of {len(found)} Hermes environments: {found[0]}")
    return [str(found[0]), "-I"]


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """(size, mtime_ns) of every file under root, skipping caches and VCS data."""
    files: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SNAPSHOT_SKIP]
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            files[os.path.relpath(path, root)] = (st.st_size, st.st_mtime_ns)
    return files


def assert_unchanged(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> None:
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    print(f"== Hermes install guard: {len(changed)} of {len(after)} files changed ==")
    assert not changed, ("the contract test modified the Hermes install:\n  "
                         + "\n  ".join(changed[:20]))


def main() -> None:
    if not HERMES_AGENT_DIR.is_dir():
        print(f"SKIP: Hermes not installed (no {HERMES_AGENT_DIR}; set HERMES_AGENT_DIR)")
        return
    python = hermes_python()

    reset_project(PROJECT)
    proc = start_server(PORT, TOKEN)
    try:
        bridge = load_plugin_module("bridge").Bridge(URL, TOKEN)
        bridge.call("memory_store", {
            "project": PROJECT, "area": "backend", "module": "cache",
            "content": "Redis won on latency for the session cache.",
            "summary": "Chose Redis for the cache", "importance": 4,
        })

        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "plugins").mkdir()
            (Path(home) / "plugins" / "blueocean").symlink_to(PLUGIN_DIR)
            (Path(home) / "blueocean.json").write_text(json.dumps({"url": URL}))
            env = {**os.environ, "HERMES_HOME": home, "HERMES_AGENT_DIR": str(HERMES_AGENT_DIR),
                   "BLUEOCEAN_AUTH_TOKEN": TOKEN, "CONTRACT_PROJECT": PROJECT,
                   "HERMES_DISABLE_LAZY_INSTALLS": "1"}
            # Hermes's launcher drops these; `uv run` sets VIRTUAL_ENV to this repo's venv.
            for var in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"):
                env.pop(var, None)
            before = snapshot(HERMES_AGENT_DIR)
            try:
                children = [subprocess.run([*python, "-c", script], env=env, capture_output=True,
                                           text=True, timeout=180, check=False)
                            for script in (CHILD_NO_MCP, CHILD)]
            finally:
                assert_unchanged(before, snapshot(HERMES_AGENT_DIR))

        print("== without mcp: loads, reports itself unavailable ==")
        no_mcp = child_result(children[0])
        assert no_mcp["available"] is False, no_mcp
        assert "mcp" in no_mcp["reason"], no_mcp
        print("  OK")

        out = child_result(children[1])

        print("== discovery, availability, trivial prompt ==")
        assert out["trivial"] == "", "a trivial prompt must not recall"
        print("  OK")

        print("== blueocean_set_project then recall ==")
        assert out["set"] == {**out["set"], "ok": True, "project": PROJECT, "has_memory": True}
        assert out["set_upper"] == {**out["set_upper"], "ok": True, "project": PROJECT,
                                    "has_memory": True}, out["set_upper"]
        assert "BlueOcean project memory - " + PROJECT in out["first"], out["first"]
        assert "Chose Redis for the cache" in out["first"]
        assert out["count"] == 1 and out["label"] == f"BlueOcean ({PROJECT})"
        print("  OK")

        print("== the same recall twice injects nothing new ==")
        assert out["second"] == "", out["second"]
        print("  OK")

        print("== the Turn Trace: stored, fence stripped, password redacted ==")
        traces = bridge.call("memory_search", {"project": PROJECT, "query": "which cache",
                                               "kind": "hermes_turn"})
        assert len(traces["summary"]) == 1, traces
        content = traces["full"][0]["content"]
        assert "We chose Redis." in content
        assert "recalled stuff" not in content, "recalled memory was stored back"
        assert "hunter2" not in content, "a password was stored"
        meta = traces["summary"][0]["metadata"]
        assert (meta["area"], meta["module"], meta["importance"]) == ("hermes", "turns", 1)
        print("  OK")

        print("== the trace stays out of default search ==")
        default = bridge.call("memory_search", {"project": PROJECT, "query": "which cache"})
        assert all(r["metadata"].get("kind") != "hermes_turn" for r in default["summary"])
        print("  OK")
    finally:
        stop_server(proc)
    print("\nHERMES CONTRACT TEST PASSED")


if __name__ == "__main__":
    main()
